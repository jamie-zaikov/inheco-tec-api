"""Tests for the C9 safety paths: stop and emergency-off (Task 13; sequences S5, S6).

Requirements: FR-12.6, FR-12.7, FR-12.8, FR-17, FR-17.4, FR-17.6, FR-17.7, FR-15.2, plus
carry-forward T7-C3 / T8-C2 (the transient open and close happen under the controller lock, checked
with ``LockCheckingTransport`` and a read that blocks; the transient handle is always closed),
T7-C2 (any open failure is "not delivered"), T11-C2 (stop and emergency-off never call the store).

Executor decisions covered: ``StopNotDelivered`` carries ``slots`` and ``operation`` (emergency-off
names every configured slot); the ``tec_stop_not_delivered`` ``slots`` field is a flat string; a
stop while released logs ``tec_state_change`` ``released -> released`` (``stop_while_released``);
a loss during a transient emergency-off raises ``ControllerNotConnected`` with state ``released``;
a stop needs at least one configured slot (``ValueError`` before any I/O, duplicates dropped).
Re-initialize, the EO gating and restart are in ``test_tec_controller_reinit.py``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from inheco_tec_api.controller import (
    ControllerLost,
    ControllerNotConnected,
    ControllerReleased,
    SlotStopOutcome,
    StopNotDelivered,
)
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.transport.fake_actions import (
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
)
from structlog.testing import capture_logs
from tec_controller_helpers import (
    CID,
    STOP_ALL,
    Rig,
    events_named,
    lock_free_elsewhere,
)

AEO = ["0AEO", "0RDC1"]


def released_rig(tmp_path: Path) -> Rig:
    """Connected at startup, then released; writes, events and store calls forgotten."""
    rig = Rig(tmp_path).connected()
    rig.manager.release()
    rig.mark()
    rig.store.calls.clear()
    return rig


def assert_released_untouched(rig: Rig) -> None:
    """Still ``released``, handle closed, persisted entry kept, store never called (T11-C2)."""
    assert rig.manager.state == "released"
    assert not rig.handle_open
    assert rig.store.calls == []
    assert rig.store.released() == {CID}
    assert rig.factory.unlocked_io == []
    assert rig.factory.open_count == rig.factory.close_count
    with pytest.raises(ControllerReleased):
        rig.manager.gate("read", 1)


# --- FR-12.6 / FR-12.8: stop while released ------------------------------------------------------


def test_fr126_stop_while_released_writes_only_ate0_and_resync(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.slots[1].enabled = True  # VENUS left slot 1 on
    with capture_logs() as events:
        outcomes = rig.manager.stop_slots([1])
    assert outcomes == [SlotStopOutcome(1, "stopped")]
    assert rig.commands == ["1ATE0", "0RDC1"]  # no identification, no other slot
    assert rig.dev.slots[1].enabled is False
    assert rig.factory.events == ["create", "open", "close"]
    assert_released_untouched(rig)
    stops = events_named(events, "tec_slot_stop")
    assert [(e["slot"], e["purpose"], e["outcome"]) for e in stops] == [
        (1, "stop_while_released", "stopped")
    ]
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("released", "released", "stop_while_released")
    ]


def test_fr126_stop_all_while_released_stops_every_requested_slot(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    outcomes = rig.manager.stop_slots([1, 2])
    assert outcomes == [SlotStopOutcome(1, "stopped"), SlotStopOutcome(2, "stopped")]
    assert rig.commands == STOP_ALL
    assert_released_untouched(rig)


def test_fr126_stop_while_released_firmware_failure_still_closes(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"), times=1)
    outcomes = rig.manager.stop_slots([1, 2])
    assert outcomes == [SlotStopOutcome(1, "failed", "3"), SlotStopOutcome(2, "stopped")]
    assert_released_untouched(rig)


def test_fr126_stop_while_released_loss_stays_released_and_closes(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    outcomes = rig.manager.stop_slots([1, 2])
    assert outcomes == [
        SlotStopOutcome(1, "failed", "device_lost"),
        SlotStopOutcome(2, "not_attempted"),
    ]
    assert_released_untouched(rig)


def test_fr128_stop_while_released_never_calls_the_store(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.store.fail_add = rig.store.fail_remove = True  # any store call would raise
    rig.manager.stop_slots([1, 2])
    assert_released_untouched(rig)


def test_t7c3_transient_stop_holds_the_lock_while_in_io(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    gate = threading.Event()
    rig.factory.read_gate = gate
    errors: list[BaseException] = []

    def stop() -> None:
        try:
            rig.manager.stop_slots([1])
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    worker = threading.Thread(target=stop)
    worker.start()
    try:
        assert rig.factory.in_read.wait(5)
        assert rig.handle_open
        assert not lock_free_elsewhere(rig.manager)  # the transient I/O holds the lock
    finally:
        gate.set()
        worker.join(10)
    assert not worker.is_alive()
    assert errors == []
    assert lock_free_elsewhere(rig.manager)
    assert_released_untouched(rig)


# --- FR-12.7: stop not delivered -----------------------------------------------------------------


def test_fr127_open_failure_raises_not_delivered_with_error_log(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(action=OpenFailure(), times=1)
    with capture_logs() as events, pytest.raises(StopNotDelivered) as info:
        rig.manager.stop_slots([1])
    assert info.value.slots == [1]
    assert info.value.operation == "stop"
    assert str(info.value).startswith("Stop was NOT delivered")
    assert "controller 0, slot 1" in str(info.value)
    assert rig.commands == []
    assert_released_untouched(rig)
    lines = events_named(events, "tec_stop_not_delivered")
    assert len(lines) == 1
    assert lines[0]["log_level"] == "error"
    assert (lines[0]["controller_id"], lines[0]["operation"]) == (CID, "stop")
    assert lines[0]["slots"] == "1"
    assert events_named(events, "tec_state_change") == []


def test_fr127_not_delivered_names_every_requested_slot_as_flat_string(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.present = False  # e.g. VENUS holds the device
    with capture_logs() as events, pytest.raises(StopNotDelivered) as info:
        rig.manager.stop_slots([2, 1])
    assert info.value.slots == [2, 1]
    assert "slots 2, 1" in str(info.value)
    line = events_named(events, "tec_stop_not_delivered")[0]
    assert line["slots"] == "2,1"
    assert isinstance(line["slots"], str)
    assert_released_untouched(rig)


@pytest.mark.parametrize("error", [ImportError("x"), OSError("y"), RuntimeError("z")])
def test_t7c2_any_create_failure_is_not_delivered(tmp_path: Path, error: Exception) -> None:
    rig = released_rig(tmp_path)
    rig.factory.create_error = error
    with pytest.raises(StopNotDelivered):
        rig.manager.stop_slots([1])
    assert rig.commands == []
    assert_released_untouched(rig)


# --- stop on a connected controller and argument checks (executor decision 8) -------------------


def test_stop_connected_uses_the_open_handle(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    outcomes = rig.manager.stop_slots([2])
    assert outcomes == [SlotStopOutcome(2, "stopped")]
    assert rig.commands == ["2ATE0", "0RDC1"]
    assert rig.factory.events == []  # no reopen
    assert rig.manager.state == "connected" and rig.handle_open
    assert rig.store.calls == []


def test_stop_connected_loss_returns_outcomes_and_goes_lost(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=DeviceLoss(), times=1)
    outcomes = rig.manager.stop_slots([1, 2])
    assert outcomes == [
        SlotStopOutcome(1, "failed", "device_lost"),
        SlotStopOutcome(2, "not_attempted"),
    ]
    assert rig.manager.state == "lost"
    assert not rig.handle_open
    assert rig.store.calls == []
    assert rig.factory.unlocked_io == []


@pytest.mark.parametrize("slots", [[], [3], [1, 5]])
def test_stop_needs_configured_slots_before_any_io(tmp_path: Path, slots: list[int]) -> None:
    rig = released_rig(tmp_path)
    with pytest.raises(ValueError):
        rig.manager.stop_slots(slots)
    assert rig.factory.events == []
    assert rig.commands == []


def test_stop_drops_duplicate_slots(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    assert rig.manager.stop_slots([1, 1]) == [SlotStopOutcome(1, "stopped")]
    assert rig.commands == ["1ATE0", "0RDC1"]


@pytest.mark.parametrize("present", [False, True])
def test_stop_disconnected_or_lost_is_refused_without_io(tmp_path: Path, present: bool) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = present
    if present:  # lost: lose the device on the first startup read
        rig.dev.script(action=DeviceLoss(), times=1)
    rig.manager.startup(False)
    assert rig.manager.state == ("lost" if present else "disconnected")
    rig.mark()
    with pytest.raises(ControllerNotConnected):
        rig.manager.stop_slots([1])
    with pytest.raises(ControllerNotConnected):
        rig.manager.emergency_off()
    assert rig.factory.events == []
    assert rig.commands == []


# --- FR-17 / FR-17.7: emergency-off on a connected controller -------------------------------------


def test_fr177_emergency_off_connected_writes_0aeo_and_sets_flag(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.slots[1].enabled = True
    with capture_logs() as events:
        rig.manager.emergency_off()
    assert rig.commands == AEO
    assert rig.dev.writes[0].command_str[0] == "0"  # mainboard address 0
    assert rig.manager.emergency_off_active is True
    assert rig.dev.slots[1].enabled is False
    assert rig.manager.state == "connected" and rig.handle_open
    assert rig.factory.events == []
    assert rig.store.calls == []
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("connected", "connected", "emergency_off_set")
    ]


def test_fr177_emergency_off_again_while_active_is_allowed(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.manager.emergency_off()
    rig.manager.emergency_off()
    assert rig.commands == AEO + AEO
    assert rig.manager.emergency_off_active is True


@pytest.mark.parametrize("action", [FirmwareCode("3"), NoReply()])
def test_fr176_emergency_off_failure_leaves_flag_clear(
    tmp_path: Path, action: FirmwareCode | NoReply
) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.script(match=Match(mnemonic="AEO"), action=action, times=None)
    with capture_logs() as events, pytest.raises(TransactionFailed):
        rig.manager.emergency_off()
    assert rig.manager.emergency_off_active is False
    assert rig.manager.state == "connected" and rig.handle_open
    assert events_named(events, "tec_state_change") == []
    rig.manager.gate("set_target", 1)  # control still allowed


def test_fr176_emergency_off_loss_goes_lost_with_flag_clear(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.script(match=Match(mnemonic="AEO"), action=DeviceLoss(), times=1)
    with pytest.raises(ControllerLost):
        rig.manager.emergency_off()
    assert rig.manager.state == "lost"
    assert rig.manager.emergency_off_active is False
    assert not rig.handle_open
    assert rig.store.calls == []


# --- FR-17.4: emergency-off while released ------------------------------------------------------


def test_fr174_emergency_off_released_transient_open_only_0aeo(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    with capture_logs() as events:
        rig.manager.emergency_off()
    assert rig.commands == AEO
    assert rig.factory.events == ["create", "open", "close"]
    assert rig.manager.emergency_off_active is True
    assert_released_untouched(rig)
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("released", "released", "emergency_off_set")
    ]


def test_fr174_emergency_off_released_open_failure_not_delivered(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(action=OpenFailure(), times=1)
    with capture_logs() as events, pytest.raises(StopNotDelivered) as info:
        rig.manager.emergency_off()
    assert info.value.operation == "emergency_off"
    assert info.value.slots == [1, 2]  # every configured slot
    assert str(info.value).startswith("Emergency-off was NOT delivered")
    assert rig.manager.emergency_off_active is False
    assert rig.commands == []
    assert_released_untouched(rig)
    line = events_named(events, "tec_stop_not_delivered")[0]
    assert line["log_level"] == "error"
    assert (line["operation"], line["slots"]) == ("emergency_off", "1,2")


def test_fr176_emergency_off_released_firmware_failure_closes_flag_clear(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(match=Match(mnemonic="AEO"), action=FirmwareCode("3"), times=1)
    with pytest.raises(TransactionFailed):
        rig.manager.emergency_off()
    assert rig.manager.emergency_off_active is False
    assert_released_untouched(rig)


def test_fr174_emergency_off_released_loss_stays_released(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.script(match=Match(mnemonic="AEO"), action=DeviceLoss(), times=1)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.emergency_off()
    assert info.value.state == "released"
    assert rig.manager.emergency_off_active is False
    assert_released_untouched(rig)


def test_t11c2_emergency_off_released_never_calls_the_store(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.store.fail_add = rig.store.fail_remove = True
    rig.manager.emergency_off()
    assert rig.manager.emergency_off_active is True
    assert_released_untouched(rig)

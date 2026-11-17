"""Tests for emergency-off gating, re-initialize and restart (Task 13; sequence S6).

Requirements: FR-6.2, FR-17.7 (gating while active), FR-17.8, FR-17.9, FR-15.2, plus carry-forward
T12-S1 (``run`` refuses ``STT``/``ATE1`` when the flag is set between ``gate`` and ``run`` with no
lock held by the caller), T12-C2 (an ``ATE0`` failure in the re-initialize of a ``connected``
controller keeps the handle, the state ``connected`` and the flag; a loss moves to ``lost``) and
T11-C2 (only the unpersist at the end of a reacquire touches the store).

Executor decisions covered: ``reinitialize_released`` clears the flag only after the unpersist
succeeded (a ``StateStoreError`` keeps ``released``, the persisted entry and the flag); a loss in
the re-initialize identification of a ``connected`` controller raises ``ControllerLost``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import (
    ConnectResult,
    ControllerLost,
    ControllerManager,
    ControllerNotConnected,
    ControllerReleased,
    EmergencyOffActive,
    Operation,
    ReconnectStopFailed,
    SlotNotPermitted,
    SlotStopOutcome,
)
from inheco_tec_api.protocol import commands
from inheco_tec_api.state_store import StateStoreError
from inheco_tec_api.transport.fake import FakeSlot
from inheco_tec_api.transport.fake_actions import DeviceLoss, FirmwareCode, Match, OpenFailure
from structlog.testing import capture_logs
from tec_controller_helpers import (
    CID,
    IDENTIFY_1,
    IDENTIFY_2,
    STARTUP_1,
    STARTUP_2,
    STOP_ALL,
    Rig,
    events_named,
    make_manifest,
)

UNMAPPED_ARTICLE = "8123456"


def eo_connected(tmp_path: Path, slots: dict[int, FakeSlot] | None = None) -> Rig:
    """A connected rig after a delivered emergency-off (writes and events forgotten)."""
    rig = Rig(tmp_path, slots=slots).connected()
    rig.manager.emergency_off()
    assert rig.manager.emergency_off_active
    rig.mark()
    return rig


def eo_released(tmp_path: Path) -> Rig:
    """A released rig after a transient emergency-off (writes, events, calls forgotten)."""
    rig = Rig(tmp_path).connected()
    rig.manager.release()
    rig.manager.emergency_off()
    assert rig.manager.emergency_off_active and rig.manager.state == "released"
    rig.mark()
    rig.store.calls.clear()
    return rig


def changes(events: list[MutableMapping[str, Any]]) -> list[tuple[Any, Any, Any]]:
    """The ``(from_state, to_state, reason)`` of every captured ``tec_state_change``."""
    return [
        (e["from_state"], e["to_state"], e["reason"])
        for e in events_named(events, "tec_state_change")
    ]


# --- FR-17.7: gating while emergency-off is active ----------------------------------------------


@pytest.mark.parametrize("op", ["set_target", "start"])
def test_fr177_eo_active_gate_refuses_control_with_zero_writes(
    tmp_path: Path, op: Operation
) -> None:
    rig = eo_connected(tmp_path)
    for slot in (1, 2):
        with pytest.raises(EmergencyOffActive):
            rig.manager.gate(op, slot)
    assert rig.commands == []
    assert rig.factory.events == []


def test_fr177_eo_active_allows_reads_stop_and_emergency_off(tmp_path: Path) -> None:
    rig = eo_connected(tmp_path)
    allowed: list[Operation] = ["read", "stop", "stop_all", "emergency_off"]
    for op in allowed:
        rig.manager.gate(op, 1)
    assert rig.manager.run(commands.rtt(1)).error_char == "0"
    assert rig.manager.stop_slots([1]) == [SlotStopOutcome(1, "stopped")]
    assert rig.commands == ["1RTT", "0RDC1", "1ATE0", "0RDC1"]
    assert rig.manager.emergency_off_active is True


def test_t12s1_run_refuses_stt_and_ate1_when_eo_set_after_gate(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.manager.gate("set_target", 1)  # passes: the flag is still clear
    rig.manager.gate("start", 1)
    rig.manager.emergency_off()  # arrives between gate and run; the caller holds no lock
    rig.mark()
    with pytest.raises(EmergencyOffActive):
        rig.manager.run(commands.stt(1, 370))
    with pytest.raises(EmergencyOffActive):
        rig.manager.run(commands.ate(1, True))
    assert rig.commands == []  # zero writes
    assert rig.manager.state == "connected"
    rig.manager.run(commands.ate(1, False))  # stop still passes
    assert rig.commands == ["1ATE0", "0RDC1"]


def test_t12s1_run_refuses_control_on_a_not_permitted_slot(tmp_path: Path) -> None:
    slots = {1: FakeSlot(article_number=UNMAPPED_ARTICLE), 2: FakeSlot()}
    rig = Rig(tmp_path, slots=slots).connected()
    with pytest.raises(SlotNotPermitted):
        rig.manager.run(commands.stt(1, 370))
    with pytest.raises(SlotNotPermitted):
        rig.manager.run(commands.ate(1, True))
    assert rig.commands == []


# --- FR-17.8 / FR-6.2: re-initialize through connect ---------------------------------------------


def test_fr178_connect_with_eo_active_stops_every_slot_identifies_and_clears(
    tmp_path: Path,
) -> None:
    rig = eo_connected(tmp_path)
    with capture_logs() as events:
        result = rig.manager.connect()
    assert result == ConnectResult("connected", stopped_slots=[1, 2], emergency_off_cleared=True)
    assert rig.commands == STOP_ALL + IDENTIFY_1 + IDENTIFY_2
    assert rig.manager.emergency_off_active is False
    assert rig.manager.state == "connected" and rig.handle_open
    assert rig.factory.events == []  # same handle, no reopen
    assert rig.store.calls == []
    assert changes(events) == [("connected", "connected", "emergency_off_cleared")]
    rig.manager.gate("set_target", 1)  # control allowed again


def test_fr62_connect_after_reinitialize_is_idempotent_again(tmp_path: Path) -> None:
    rig = eo_connected(tmp_path)
    rig.manager.connect()
    rig.mark()
    assert rig.manager.connect() == ConnectResult("connected")
    assert rig.commands == []


def test_t12c2_connect_reinit_ate0_failure_keeps_handle_state_and_flag(tmp_path: Path) -> None:
    rig = eo_connected(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=2), action=FirmwareCode("3"), times=1)
    with capture_logs() as events, pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.outcomes == [SlotStopOutcome(1, "stopped"), SlotStopOutcome(2, "failed", "3")]
    assert rig.manager.state == "connected"
    assert rig.manager.emergency_off_active is True
    assert rig.handle_open
    assert rig.factory.events == []  # never closed
    assert all(cmd[1:4] != "RTD" for cmd in rig.commands)  # no identification
    assert changes(events) == []
    with pytest.raises(EmergencyOffActive):
        rig.manager.gate("start", 1)
    assert rig.manager.run(commands.rtt(1)).error_char == "0"  # the kept handle still works
    assert rig.manager.connect().emergency_off_cleared is True  # a retry can succeed


def test_t12c2_connect_reinit_loss_in_sweep_goes_lost(tmp_path: Path) -> None:
    rig = eo_connected(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=DeviceLoss(), times=1)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.outcomes[0] == SlotStopOutcome(1, "failed", "device_lost")
    assert rig.manager.state == "lost"
    assert not rig.handle_open
    assert rig.factory.unlocked_io == []
    assert rig.store.calls == []
    with pytest.raises(ControllerNotConnected):
        rig.manager.gate("read", 1)


def test_t12c2_connect_reinit_loss_in_identification_goes_lost(tmp_path: Path) -> None:
    rig = eo_connected(tmp_path)
    rig.dev.script(match=Match(mnemonic="RTD"), action=DeviceLoss(), times=1)
    with pytest.raises(ControllerLost):
        rig.manager.connect()
    assert rig.commands[:4] == STOP_ALL
    assert rig.manager.state == "lost"
    assert not rig.handle_open
    assert rig.factory.unlocked_io == []


# --- FR-17.8: re-initialize through reacquire ----------------------------------------------------


def test_fr178_reacquire_with_eo_active_stops_identifies_clears_and_unpersists(
    tmp_path: Path,
) -> None:
    rig = eo_released(tmp_path)
    with capture_logs() as events:
        state = rig.manager.reacquire()
    assert state == "connected" == rig.manager.state
    assert rig.commands == STOP_ALL + IDENTIFY_1 + IDENTIFY_2
    assert rig.manager.emergency_off_active is False
    assert rig.handle_open
    assert rig.store.calls == [("remove", CID)]
    assert rig.store.released() == set()
    assert rig.factory.events == ["create", "open", "store_remove"]
    assert rig.factory.unlocked_io == []
    assert changes(events) == [
        ("released", "connected", "reacquire"),
        ("connected", "connected", "emergency_off_cleared"),
    ]
    rig.manager.gate("start", 1)


def test_fr178_reacquire_reinit_open_failure_keeps_released_and_flag(tmp_path: Path) -> None:
    rig = eo_released(tmp_path)
    rig.dev.script(action=OpenFailure(), times=1)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.reacquire()
    assert info.value.state == "released"
    assert rig.manager.state == "released"
    assert rig.manager.emergency_off_active is True
    assert rig.store.calls == []
    assert rig.store.released() == {CID}
    assert rig.commands == []


def test_fr178_reacquire_reinit_ate0_failure_closes_and_keeps_released(tmp_path: Path) -> None:
    rig = eo_released(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"), times=1)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.reacquire()
    assert info.value.outcomes == [SlotStopOutcome(1, "failed", "3"), SlotStopOutcome(2, "stopped")]
    assert rig.manager.state == "released"
    assert rig.manager.emergency_off_active is True
    assert not rig.handle_open
    assert rig.store.calls == []
    assert rig.store.released() == {CID}
    assert all(cmd[1:4] != "RTD" for cmd in rig.commands)
    assert rig.factory.unlocked_io == []


def test_fr178_reacquire_reinit_store_failure_keeps_flag_and_released(tmp_path: Path) -> None:
    rig = eo_released(tmp_path)
    rig.store.fail_remove = True
    with pytest.raises(StateStoreError):
        rig.manager.reacquire()
    assert rig.manager.state == "released"
    assert rig.manager.emergency_off_active is True
    assert not rig.handle_open
    assert rig.store.released() == {CID}
    assert rig.factory.unlocked_io == []


def test_fr178_reacquire_reinit_loss_in_identification_stays_released(tmp_path: Path) -> None:
    rig = eo_released(tmp_path)
    rig.dev.script(match=Match(mnemonic="RTD"), action=DeviceLoss(), times=1)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.reacquire()
    assert info.value.state == "released"
    assert rig.manager.state == "released"
    assert rig.manager.emergency_off_active is True
    assert not rig.handle_open
    assert rig.store.calls == []


def test_fr178_connect_on_released_with_eo_is_still_refused(tmp_path: Path) -> None:
    rig = eo_released(tmp_path)
    with pytest.raises(ControllerReleased):
        rig.manager.connect()
    assert rig.factory.events == []
    assert rig.manager.emergency_off_active is True


# --- FR-17.9: the flag is in memory only ----------------------------------------------------------


@pytest.mark.parametrize("release", [False, True])
def test_fr179_restart_starts_with_flag_clear_and_sends_no_ate0(
    tmp_path: Path, release: bool
) -> None:
    rig = Rig(tmp_path).connected()
    if release:
        rig.manager.release()
    rig.manager.emergency_off()
    assert rig.manager.emergency_off_active is True
    state_file = tmp_path / "state.json"
    if state_file.exists():
        assert "emergency" not in state_file.read_text(encoding="utf-8")
    rig.mark()
    restarted = ControllerManager(CID, make_manifest(), rig.factory, rig.store)
    rig.factory.manager = restarted
    assert restarted.emergency_off_active is False
    restarted.startup(CID in rig.store.released())
    assert restarted.emergency_off_active is False
    if release:
        assert restarted.state == "released"
        assert rig.commands == []
    else:
        assert restarted.state == "connected"
        assert rig.commands == IDENTIFY_1 + STARTUP_1 + IDENTIFY_2 + STARTUP_2
        assert all("ATE" not in cmd for cmd in rig.commands)
        restarted.gate("set_target", 1)

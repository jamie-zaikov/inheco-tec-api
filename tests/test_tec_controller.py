"""Tests for C9 ``ControllerManager``: construction, startup, connect, gating (Task 12).

Requirements: FR-6, FR-6.1, FR-6.2, FR-6.3, FR-6.7, FR-6.8, FR-7, FR-7.5, FR-8.2 (gating runs
before any value check), FR-12.2, FR-12.4, FR-15.2, plus carry-forward T7-C2 (raw ImportError /
OSError at open is an open failure), T10-V (``tec_slot_not_permitted`` / ``tec_startup_slot_state``
logged), T10-S2 (``article`` never logged) and T11-C2 (the manager never builds a store).

Loss/reconnect, release/reacquire and lock tests live in ``test_tec_controller_loss.py``,
``test_tec_controller_release.py`` and ``test_tec_controller_lock.py``. Every test runs the real
engine over the fake transport (Task 8) with virtual time.
"""

from __future__ import annotations

from pathlib import Path

import inheco_tec_api.state_store as state_store
import pytest
from inheco_tec_api.controller import (
    ConnectResult,
    ControllerManager,
    ControllerNotConnected,
    ControllerReleased,
    EmergencyOffActive,
    Operation,
    SlotNotPermitted,
)
from inheco_tec_api.controller_startup import REASON_NOT_IDENTIFIED
from inheco_tec_api.protocol import commands
from inheco_tec_api.transport.fake import FakeSlot
from inheco_tec_api.transport.fake_actions import DeviceLoss
from structlog.testing import capture_logs
from tec_controller_helpers import (
    CID,
    IDENTIFY_1,
    IDENTIFY_2,
    STARTUP_1,
    STARTUP_2,
    LifecycleFactory,
    RecordingStore,
    Rig,
    events_named,
    make_manifest,
)

ALL_OPS: list[Operation] = ["set_target", "start", "stop", "stop_all", "emergency_off", "read"]
UNMAPPED_ARTICLE = "8123456"


def assert_no_slot_change(commands_written: list[str]) -> None:
    """FR-6.7: no STT, ATE or AEO was written."""
    for command in commands_written:
        assert command[1:4] not in ("STT", "ATE", "AEO"), commands_written


# --- construction --------------------------------------------------------------------------------


def test_fr6_new_manager_is_disconnected_with_eo_inactive_and_unidentified_slots(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    m = rig.manager
    assert m.state == "disconnected"
    assert m.emergency_off_active is False
    assert sorted(m.slots) == [1, 2]
    assert all(not s.permitted for s in m.slots.values())
    assert m.slots[1].not_permitted_reason == REASON_NOT_IDENTIFIED
    assert m.transport_fake is True
    assert rig.factory.events == []


def test_fr6_unconfigured_controller_id_is_refused(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path / "s.json")
    with pytest.raises(ValueError):
        ControllerManager(5, make_manifest(), LifecycleFactory(), store)


def test_t11c2_manager_never_constructs_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("ControllerManager constructed a ReleaseStateStore")

    monkeypatch.setattr(state_store.ReleaseStateStore, "__init__", forbidden)
    m = rig.manager
    m.startup(False)
    m.release()
    m.reacquire()
    rig.dev.script(action=DeviceLoss(), times=1)
    with pytest.raises(ControllerNotConnected):
        m.run(commands.rtt(1))
    m.connect()
    assert m.state == "connected"


# --- FR-6.1 / FR-6.7 / FR-6.8: startup -----------------------------------------------------------


def test_fr61_startup_with_absent_controller_is_disconnected_without_io(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    with capture_logs() as events:
        rig.manager.startup(False)
    assert rig.manager.state == "disconnected"
    assert rig.commands == []
    assert rig.store.calls == []
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("disconnected", "disconnected", "startup")
    ]
    failed = events_named(events, "tec_open_failed")
    assert failed and failed[0]["error"] == "DeviceNotFound"
    assert failed[0]["purpose"] == "startup"


@pytest.mark.parametrize("where", ["create", "open"])
@pytest.mark.parametrize("error", [ImportError("no hid"), OSError("dll load failed")])
def test_t7c2_raw_import_or_os_error_at_open_is_open_failure(
    tmp_path: Path, where: str, error: Exception
) -> None:
    rig = Rig(tmp_path)
    if where == "create":
        rig.factory.create_error = error
    else:
        rig.factory.open_error = error
    with capture_logs() as events:
        rig.manager.startup(False)
    assert rig.manager.state == "disconnected"
    assert not rig.handle_open
    failed = events_named(events, "tec_open_failed")
    assert failed[0]["error"] == type(error).__name__
    assert str(error) not in repr(events)  # type name only, never the message


@pytest.mark.parametrize("error", [ImportError("no hid"), OSError("dll load failed")])
def test_t7c2_connect_with_raw_open_error_stays_disconnected(
    tmp_path: Path, error: Exception
) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    rig.factory.open_error = error
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.connect()
    assert info.value.state == "disconnected"
    assert rig.manager.state == "disconnected"


def test_fr68_startup_identifies_and_reads_each_slot_in_order(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(False)
    assert rig.manager.state == "connected"
    assert rig.commands == IDENTIFY_1 + STARTUP_1 + IDENTIFY_2 + STARTUP_2
    assert all(s.permitted for s in rig.manager.slots.values())
    assert rig.handle_open
    assert rig.store.calls == []


def test_fr67_fr68_leave_as_found_slot_on_at_4c_is_not_changed_and_is_logged(
    tmp_path: Path,
) -> None:
    slots = {1: FakeSlot(enabled=True, target=40, actual=40), 2: FakeSlot()}
    rig = Rig(tmp_path, slots=slots)
    with capture_logs() as events:
        rig.manager.startup(False)
    assert_no_slot_change(rig.commands)
    assert rig.dev.slots[1].enabled is True
    assert rig.dev.slots[1].target == 40
    state = rig.manager.slots[1].startup_state
    assert state is not None
    assert (state.control_state, state.target_c, state.actual_c) == ("heating", 4.0, 4.0)
    off = rig.manager.slots[2].startup_state
    assert off is not None and off.control_state == "off"
    logged = {e["slot"]: e for e in events_named(events, "tec_startup_slot_state")}
    assert sorted(logged) == [1, 2]
    one = logged[1]
    assert (one["controller_id"], one["control_state"], one["target_c"], one["actual_c"]) == (
        CID,
        "heating",
        4.0,
        4.0,
    )
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("disconnected", "connected", "startup")
    ]


def test_fr124_startup_released_does_not_open(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with capture_logs() as events:
        rig.manager.startup(True)
    assert rig.manager.state == "released"
    assert rig.factory.events == []
    assert rig.dev.open_count == 0
    assert rig.store.calls == []
    changes = events_named(events, "tec_state_change")
    assert [(e["to_state"], e["reason"]) for e in changes] == [("released", "startup")]


def test_fr75_t10v_not_permitted_slot_logged_with_reason_and_article_never_logged(
    tmp_path: Path,
) -> None:
    slots = {1: FakeSlot(), 2: FakeSlot(article_number=UNMAPPED_ARTICLE)}
    rig = Rig(tmp_path, slots=slots)
    with capture_logs() as events:
        rig.manager.startup(False)
        with pytest.raises(SlotNotPermitted):
            rig.manager.gate("set_target", 2)
    runtime = rig.manager.slots[2]
    ident = runtime.identification
    assert ident is not None and ident.article == UNMAPPED_ARTICLE
    assert runtime.permitted is False
    reason = ident.limits.reason
    assert reason
    logged = events_named(events, "tec_slot_not_permitted")
    assert len(logged) == 2  # once at identification, once at the gate
    for event in logged:
        assert (event["controller_id"], event["slot"], event["reason"]) == (CID, 2, reason)
    assert rig.manager.slots[1].permitted is True
    # T10-S2: neither the unmapped nor the mapped article appears in any log line.
    text = repr(events)
    assert UNMAPPED_ARTICLE not in text
    assert "7000179" not in text


# --- FR-6.2: connect ------------------------------------------------------------------------------


def test_fr62_connect_from_disconnected_opens_identifies_and_connects(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    rig.dev.present = True
    rig.mark()
    with capture_logs() as events:
        result = rig.manager.connect()
    assert result == ConnectResult("connected")
    assert result.stopped_slots == []
    assert rig.manager.state == "connected"
    assert rig.commands == IDENTIFY_1 + IDENTIFY_2  # no ATE0, no startup reads
    assert all(s.permitted for s in rig.manager.slots.values())
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("disconnected", "connected", "connect")
    ]


def test_fr62_connect_open_failure_stays_disconnected(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.connect()
    assert info.value.state == "disconnected"
    assert rig.manager.state == "disconnected"
    assert rig.commands == []


def test_fr62_connect_when_connected_is_idempotent_without_io(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    with capture_logs() as events:
        first = rig.manager.connect()
        second = rig.manager.connect()
    assert first == second == ConnectResult("connected")
    assert rig.commands == []
    assert rig.factory.events == []
    assert events_named(events, "tec_state_change") == []


def test_fr122_connect_when_released_is_refused_without_io(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(True)
    with pytest.raises(ControllerReleased):
        rig.manager.connect()
    assert rig.factory.events == []
    assert rig.manager.state == "released"


# --- C9 gating steps 2-5 (FR-6.3, FR-12.2, FR-17.7 order, FR-7.5) ---------------------------------


@pytest.mark.parametrize("op", ["set_target", "start", "read"])
def test_fr122_gate_released_refuses_other_operations(tmp_path: Path, op: Operation) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(True)
    with pytest.raises(ControllerReleased):
        rig.manager.gate(op, 1)


@pytest.mark.parametrize("op", ["stop", "stop_all", "emergency_off"])
def test_fr122_gate_released_allows_stop_stop_all_emergency_off(
    tmp_path: Path, op: Operation
) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(True)
    rig.manager.emergency_off_active = True
    rig.manager.gate(op, 1)
    assert rig.factory.events == []


def test_gate_released_wins_over_emergency_off(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(True)
    rig.manager.emergency_off_active = True
    with pytest.raises(ControllerReleased):
        rig.manager.gate("set_target", 1)


@pytest.mark.parametrize("op", ALL_OPS)
def test_fr63_gate_disconnected_refuses_every_operation_naming_state(
    tmp_path: Path, op: Operation
) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.gate(op, 1)
    assert info.value.state == "disconnected"
    assert "disconnected" in str(info.value)


@pytest.mark.parametrize("op", ["set_target", "start"])
def test_gate_eo_active_refuses_control_before_slot_permission(
    tmp_path: Path, op: Operation
) -> None:
    slots = {1: FakeSlot(article_number=UNMAPPED_ARTICLE), 2: FakeSlot()}
    rig = Rig(tmp_path, slots=slots).connected()
    rig.manager.emergency_off_active = True
    with pytest.raises(EmergencyOffActive):
        rig.manager.gate(op, 1)  # slot 1 is also not permitted: EO is checked first


@pytest.mark.parametrize("op", ["stop", "stop_all", "emergency_off", "read"])
def test_gate_eo_active_passes_other_operations(tmp_path: Path, op: Operation) -> None:
    rig = Rig(tmp_path).connected()
    rig.manager.emergency_off_active = True
    rig.manager.gate(op, 1)


@pytest.mark.parametrize("op", ["set_target", "start"])
def test_fr75_gate_unidentified_slot_is_not_permitted(tmp_path: Path, op: Operation) -> None:
    rig = Rig(tmp_path).connected()
    rig.manager.slots[2].identification = None
    with capture_logs() as events, pytest.raises(SlotNotPermitted) as info:
        rig.manager.gate(op, 2)
    assert (info.value.slot, info.value.reason) == (2, REASON_NOT_IDENTIFIED)
    assert events_named(events, "tec_slot_not_permitted")[0]["reason"] == REASON_NOT_IDENTIFIED


@pytest.mark.parametrize("op", ["stop", "read"])
def test_fr75_gate_not_permitted_slot_still_allows_reads_and_stop(
    tmp_path: Path, op: Operation
) -> None:
    slots = {1: FakeSlot(article_number=UNMAPPED_ARTICLE), 2: FakeSlot()}
    rig = Rig(tmp_path, slots=slots).connected()
    rig.manager.gate(op, 1)


def test_gate_permitted_slot_passes_and_does_no_io(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    for op in ALL_OPS:
        rig.manager.gate(op, 1)
    assert rig.commands == []
    assert rig.factory.events == []


@pytest.mark.parametrize("slot", [None, 3])
def test_gate_control_operation_needs_a_configured_slot(tmp_path: Path, slot: int | None) -> None:
    rig = Rig(tmp_path).connected()
    with pytest.raises(ValueError):
        rig.manager.gate("set_target", slot)


def test_fr63_run_refuses_without_io_when_not_connected(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    with pytest.raises(ControllerNotConnected):
        rig.manager.run(commands.rtt(1))
    released = Rig(tmp_path / "r")
    released.manager.startup(True)
    with pytest.raises(ControllerReleased):
        released.manager.run(commands.rtt(1))
    assert rig.commands == [] and released.commands == []


def test_fr51_run_executes_on_the_connected_controller(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    reply = rig.manager.run(commands.stt(1, 370))
    assert reply.error_char == "0"
    assert rig.commands == ["1STT370", "0RDC1"]
    assert rig.dev.slots[1].target == 370

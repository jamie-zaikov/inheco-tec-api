"""Tests for C11 ``TecService``: IDs, set target, start, status and connect (Task 15).

Requirements: FR-6.2, FR-6.5 (range 422 before configured 404, bool rejected), FR-6.6 / FR-13.4
(status without I/O), FR-8.1, FR-8.2 (limits compared in integer tenths incl. the exact
boundaries; rejection carries the limits and writes nothing), FR-8.3 (condensation warning below
12.0 C, logged at warning, never blocking), FR-8.4, FR-11.4 (connect after loss reports
``stopped_slots``), FR-17.8 (connect re-initialize), NFR-3 (one transaction per operation, no
server-side wait). Read operations: ``test_tec_operations_read.py``; safety operations:
``test_tec_operations_safety.py``; locking: ``test_tec_operations_lock.py``. Every test runs the
real engine over the fake transport (Task 8) with virtual time.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import (
    ControllerNotConnected,
    ControllerReleased,
    EmergencyOffActive,
    SlotNotPermitted,
)
from inheco_tec_api.operations import (
    ConnectOutcome,
    InvalidRequest,
    NotConfigured,
    OperationWarning,
    SetTargetResult,
    SlotCommandResult,
)
from inheco_tec_api.registry_status import EffectiveLimits
from inheco_tec_api.transport.fake_actions import OpenFailure
from structlog.testing import capture_logs
from tec_controller_helpers import IDENTIFY_1, IDENTIFY_2, events_named
from tec_operations_helpers import RECONNECT_STOP, UNMAPPED_ARTICLE, OpsRig, flat_values

LIMITS = EffectiveLimits(4.0, 70.0)

# --- FR-6.5: ID validation order ----------------------------------------------------------------

BAD_RANGE_IDS: list[Any] = [8, -1, True, False, "0", 1.0, None]


@pytest.mark.parametrize("cid", BAD_RANGE_IDS)
@pytest.mark.parametrize(
    "call",
    [
        lambda s, c: s.connect(c),
        lambda s, c: s.release(c),
        lambda s, c: s.reacquire(c),
        lambda s, c: s.sensor_values(c, 0),
        lambda s, c: s.device_type(c, 0),
        lambda s, c: s.firmware_version(c, 0),
        lambda s, c: s.emergency_off(c, True),
        lambda s, c: s.set_target(c, 1, 37.0),
        lambda s, c: s.start(c, 1),
        lambda s, c: s.stop(c, 1),
        lambda s, c: s.actual(c, 1),
        lambda s, c: s.target(c, 1),
        lambda s, c: s.at_target(c, 1),
    ],
)
def test_fr65_controller_id_out_of_range_or_bool_is_invalid_with_no_io(
    tmp_path: Path, cid: Any, call: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest):
        call(rig.service, cid)
    assert rig.commands == []
    assert rig.reg.factory.events == []


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.connect(3),
        lambda s: s.release(3),
        lambda s: s.reacquire(3),
        lambda s: s.sensor_values(3, 0),
        lambda s: s.device_type(3, 0),
        lambda s: s.firmware_version(3, 0),
        lambda s: s.emergency_off(3, True),
        lambda s: s.set_target(3, 1, 37.0),
        lambda s: s.start(3, 1),
        lambda s: s.stop(3, 1),
        lambda s: s.actual(3, 1),
    ],
)
def test_fr65_unconfigured_controller_is_not_found_with_no_io(tmp_path: Path, call: Any) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(NotConfigured) as info:
        call(rig.service)
    assert info.value.controller_id == 3
    assert rig.commands == []


@pytest.mark.parametrize("sid", [0, 7, -1, True, False, "1", 1.0, None])
def test_fr65_slot_id_out_of_range_or_bool_is_invalid(tmp_path: Path, sid: Any) -> None:
    rig = OpsRig(tmp_path)
    for call in (
        lambda: rig.service.set_target(0, sid, 37.0),
        lambda: rig.service.start(0, sid),
        lambda: rig.service.stop(0, sid),
        lambda: rig.service.actual(0, sid),
        lambda: rig.service.target(0, sid),
        lambda: rig.service.at_target(0, sid),
    ):
        with pytest.raises(InvalidRequest):
            call()
    assert rig.commands == []


def test_fr65_range_checks_come_before_configured_checks(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    # unconfigured controller 3 with an out-of-range slot: the slot range check wins (422)
    with pytest.raises(InvalidRequest):
        rig.service.set_target(3, 7, 37.0)
    # out-of-range controller with an out-of-range slot: 422
    with pytest.raises(InvalidRequest):
        rig.service.stop(8, 7)
    # both in range: controller configured check, then slot configured check (404)
    with pytest.raises(NotConfigured) as info:
        rig.service.stop(3, 1)
    assert (info.value.controller_id, info.value.slot_id) == (3, None)
    with pytest.raises(NotConfigured) as info:
        rig.service.stop(0, 5)
    assert (info.value.controller_id, info.value.slot_id) == (0, 5)
    assert rig.commands == []


# --- FR-8.1 / FR-8.2: set target ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("t", "cmd", "target_c"),
    [
        (37.0, "1STT370", 37.0),
        (36.66, "1STT367", 36.7),  # FR-5.10 rounding; target decoded from the sent tenths
        (70.0, "1STT700", 70.0),  # exact upper bound
        (70.04, "1STT700", 70.0),  # encodes to 700 tenths: inside
        (4.0, "1STT40", 4.0),  # exact lower bound
        (3.95, "1STT40", 4.0),  # rounds half up to 40 tenths: inside
        (25, "1STT250", 25.0),  # int accepted
    ],
)
def test_fr81_set_target_writes_stt_and_returns_decoded_target(
    tmp_path: Path, t: float, cmd: str, target_c: float
) -> None:
    rig = OpsRig(tmp_path)
    result = rig.service.set_target(0, 1, t)
    assert rig.commands == [cmd, "0RDC1"]
    assert result.controller_id == 0 and result.slot_id == 1
    assert result.target_c == target_c
    assert rig.dev.slots[1].target == int(cmd[4:])


def test_fr81_set_target_on_slot_2_addresses_slot_2(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.set_target(0, 2, 50.0) == SetTargetResult(0, 2, 50.0, [])
    assert rig.commands == ["2STT500", "0RDC1"]


@pytest.mark.parametrize("t", [70.05, 70.1, 80.0, 3.94, 3.9, 0.0, -5.0, 1000.0])
def test_fr82_target_outside_limits_is_rejected_with_limits_and_zero_writes(
    tmp_path: Path, t: float
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest) as info:
        rig.service.set_target(0, 1, t)
    assert info.value.effective_limits == LIMITS
    assert (info.value.controller_id, info.value.slot_id) == (0, 1)
    assert rig.commands == []
    assert rig.dev.slots[1].target == 0


@pytest.mark.parametrize("t", [math.nan, math.inf, -math.inf, True, False, "37", None])
def test_fr82_non_finite_or_non_number_target_is_rejected_with_zero_writes(
    tmp_path: Path, t: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest) as info:
        rig.service.set_target(0, 1, t)
    assert info.value.effective_limits == LIMITS
    assert rig.commands == []


def test_fr82_gate_runs_before_the_value_check(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    with pytest.raises(ControllerReleased):
        rig.service.set_target(0, 1, 80.0)  # released wins over out-of-range
    assert rig.commands == []
    assert rig.reg.factory.events == []


# --- FR-8.3: condensation warning ---------------------------------------------------------------


def test_fr83_target_below_12_warns_and_logs_but_is_sent(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    with capture_logs() as events:
        result = rig.service.set_target(0, 1, 11.9)
    assert rig.commands == ["1STT119", "0RDC1"]
    assert result.target_c == 11.9
    assert [w.kind for w in result.warnings] == ["condensation_risk"]
    assert isinstance(result.warnings[0], OperationWarning)
    lines = events_named(events, "tec_condensation_warning")
    assert len(lines) == 1
    assert lines[0]["log_level"] == "warning"
    assert (lines[0]["controller_id"], lines[0]["slot"], lines[0]["target_c"]) == (0, 1, 11.9)


@pytest.mark.parametrize("t", [12.0, 12.05, 37.0, 70.0])
def test_fr83_target_at_or_above_12_has_no_warning(tmp_path: Path, t: float) -> None:
    rig = OpsRig(tmp_path)
    with capture_logs() as events:
        result = rig.service.set_target(0, 1, t)
    assert result.warnings == []
    assert events_named(events, "tec_condensation_warning") == []


def test_fr83_lowest_permitted_target_warns(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    result = rig.service.set_target(0, 1, 4.0)
    assert [w.kind for w in result.warnings] == ["condensation_risk"]
    assert rig.commands == ["1STT40", "0RDC1"]


def test_fr83_decision6_warning_uses_requested_value(tmp_path: Path) -> None:
    # 11.95 encodes to 120 tenths (12.0 C) but the requested t is below 12.0 (executor Decision 6)
    rig = OpsRig(tmp_path)
    result = rig.service.set_target(0, 1, 11.95)
    assert rig.commands == ["1STT120", "0RDC1"]
    assert result.target_c == 12.0
    assert [w.kind for w in result.warnings] == ["condensation_risk"]


def test_fr83_rejected_target_logs_no_condensation_warning(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    with capture_logs() as events, pytest.raises(InvalidRequest):
        rig.service.set_target(0, 1, 3.0)
    assert events_named(events, "tec_condensation_warning") == []


# --- set target / start refused by the gate -----------------------------------------------------


def test_fr81_fr84_not_permitted_slot_refuses_set_target_and_start(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, articles={2: UNMAPPED_ARTICLE})
    with pytest.raises(SlotNotPermitted):
        rig.service.set_target(0, 2, 37.0)
    with pytest.raises(SlotNotPermitted):
        rig.service.start(0, 2)
    assert rig.commands == []


def test_fr84_start_writes_ate1(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.start(0, 1) == SlotCommandResult(0, 1)
    assert rig.commands == ["1ATE1", "0RDC1"]
    assert rig.dev.slots[1].enabled is True


def test_fr84_start_refused_while_released_or_lost(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.make_lost()
    with pytest.raises(ControllerNotConnected):
        rig.service.start(0, 1)
    assert rig.commands == []
    rig2 = OpsRig(tmp_path / "b")
    rig2.release()
    with pytest.raises(ControllerReleased):
        rig2.service.start(0, 1)
    assert rig2.commands == [] and rig2.reg.factory.events == []


def test_fr177_set_target_and_start_refused_after_emergency_off(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.service.emergency_off(0, True)
    rig.mark()
    with pytest.raises(EmergencyOffActive):
        rig.service.set_target(0, 1, 37.0)
    with pytest.raises(EmergencyOffActive):
        rig.service.start(0, 2)
    assert rig.commands == []


# --- FR-6.6 / FR-13.4: status -------------------------------------------------------------------


def test_fr66_status_matches_registry_and_does_no_io(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    status = rig.service.status()
    assert status == rig.reg.registry.status()
    assert [s.controller_id for s in status] == [0, 1]
    assert all(s.transport_fake for s in status)
    assert rig.commands == [] and rig.reg.commands(1) == []
    assert rig.reg.factory.events == []


def test_fr66_status_works_while_stopping(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.reg.registry.shutdown()
    rig.mark()
    assert rig.service.status()[0].controller_id == 0
    assert rig.commands == []


# --- FR-6.2 / FR-11.4 / FR-17.8: connect --------------------------------------------------------


def test_fr62_connect_when_connected_is_idempotent_without_io(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    outcome = rig.service.connect(0)
    assert isinstance(outcome, ConnectOutcome)
    assert (outcome.state, outcome.stopped_slots, outcome.emergency_off_cleared) == (
        "connected",
        [],
        False,
    )
    assert [s.slot_id for s in outcome.slots] == [1, 2]
    assert all(s.control_permitted for s in outcome.slots)
    assert rig.commands == [] and rig.reg.factory.events == []


def test_fr62_connect_from_disconnected_identifies_and_reports_slots(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, start=False)
    rig.dev.script(action=OpenFailure(), times=1)
    rig.reg.started()
    assert rig.manager.state == "disconnected"
    outcome = rig.service.connect(0)
    assert outcome.state == "connected" and outcome.stopped_slots == []
    assert rig.commands == IDENTIFY_1 + IDENTIFY_2
    assert outcome.slots[0].effective_limits == LIMITS


def test_fr114_connect_after_loss_stops_every_slot_first_and_reports_it(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.make_lost()
    outcome = rig.service.connect(0)
    assert outcome.state == "connected"
    assert outcome.stopped_slots == [1, 2]
    assert outcome.emergency_off_cleared is False
    assert rig.commands[:4] == RECONNECT_STOP  # ATE0 to every slot before anything else
    assert all(s.control_permitted for s in outcome.slots)
    # the next user command follows the sweep
    rig.service.start(0, 1)
    assert rig.commands[-2:] == ["1ATE1", "0RDC1"]


def test_fr178_connect_with_emergency_off_reinitializes_and_reports_clear(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.service.emergency_off(0, True)
    rig.mark()
    outcome = rig.service.connect(0)
    assert outcome.emergency_off_cleared is True
    assert outcome.stopped_slots == [1, 2]
    assert rig.commands[:4] == RECONNECT_STOP
    assert rig.manager.emergency_off_active is False
    rig.service.start(0, 1)  # control allowed again
    assert rig.commands[-2:] == ["1ATE1", "0RDC1"]


def test_fr62_connect_on_released_is_refused(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    with pytest.raises(ControllerReleased):
        rig.service.connect(0)
    assert rig.commands == [] and rig.reg.factory.events == []


# --- T10-S2: raw device data never logged -------------------------------------------------------


def test_t10s2_raw_device_data_is_never_logged(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, start=False)
    board = rig.dev.mainboard
    board.firmware = {sel: f"FWRAW{sel}X" for sel in range(5)}
    board.sensor_values = {sel: f"SVRAW{sel}X" for sel in range(9)}
    rig.reg.started()
    rig.make_lost()
    with capture_logs() as events:
        outcome = rig.service.connect(0)
        rig.service.status()
        rig.service.set_target(0, 1, 11.0)
        for sel in range(5):
            rig.service.firmware_version(0, sel)
        for sel in range(9):
            rig.service.sensor_values(0, sel)
        rig.service.device_type(0, 1)
    article = rig.dev.slots[1].article_number
    assert outcome.slots[0].detected_article_number == article  # returned as a field
    text = " ".join(flat_values(events))
    assert article not in text
    assert "FWRAW" not in text and "SVRAW" not in text

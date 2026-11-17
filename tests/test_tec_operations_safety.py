"""Tests for the C11 safety and lifecycle operations of ``TecService`` (Task 15).

Requirements: FR-8.5 (stop writes ``ATE0`` whatever the permitted state), FR-8.6 / FR-12.6 (stop-all
and its status rule), FR-17.1, FR-17.2 (confirm must be exactly ``True``; zero writes), FR-17.4,
FR-17.5 (``tec_emergency_off`` request + outcome lines, warning; error when not delivered),
FR-17.6, FR-17.8 (reacquire reports the re-initialize), plus carry-forward T13-S3 (no failed stop
or emergency-off maps to success), T13-C1 (reacquire reports ``emergency_off_cleared``), T14-S3
(``ensure_release_state_persisted`` before every release/reacquire; its ``StateStoreError``
propagates and the state is unchanged) and T9-C1 / T9-C6 (no message says "not applied").
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import (
    ControllerBusy,
    ControllerNotConnected,
    StopNotDelivered,
)
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.operations import (
    EmergencyOffOutcomeUnknown,
    EmergencyOffResult,
    InvalidRequest,
    LifecycleResult,
    ReacquireResult,
    SlotCommandResult,
    StopFailed,
)
from inheco_tec_api.registry import ServiceStopping, StopAllSlotOutcome
from inheco_tec_api.state_store import StateStoreError
from inheco_tec_api.transport.fake_actions import (
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
)
from structlog.testing import capture_logs
from tec_controller_helpers import IDENTIFY_1, IDENTIFY_2, events_named
from tec_operations_helpers import RECONNECT_STOP, UNMAPPED_ARTICLE, OpsRig

EO = "tec_emergency_off"


def eo_lines(events: list[Any]) -> list[tuple[str, str | None, str | None]]:
    """``(log_level, phase, outcome)`` of every ``tec_emergency_off`` line."""
    return [(e["log_level"], e["phase"], e.get("outcome")) for e in events_named(events, EO)]


# --- FR-8.5: stop -------------------------------------------------------------------------------


def test_fr85_stop_writes_ate0(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].enabled = True
    assert rig.service.stop(0, 1) == SlotCommandResult(0, 1)
    assert rig.commands == ["1ATE0", "0RDC1"]
    assert rig.dev.slots[1].enabled is False


def test_fr85_stop_on_a_not_permitted_slot_still_writes_ate0(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, articles={2: UNMAPPED_ARTICLE})
    assert rig.manager.slots[2].permitted is False
    assert rig.service.stop(0, 2) == SlotCommandResult(0, 2)
    assert rig.commands == ["2ATE0", "0RDC1"]


def test_fr85_stop_while_emergency_off_active_is_allowed(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.service.emergency_off(0, True)
    rig.mark()
    rig.service.stop(0, 1)
    assert rig.commands == ["1ATE0", "0RDC1"]


def test_fr126_stop_while_released_uses_the_transient_open(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    assert rig.service.stop(0, 2) == SlotCommandResult(0, 2)
    assert rig.commands == ["2ATE0", "0RDC1"]
    assert rig.manager.state == "released" and not rig.dev.is_open


def test_fr127_stop_while_released_open_failure_is_not_delivered(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    rig.dev.script(action=OpenFailure(), times=1)
    with pytest.raises(StopNotDelivered):
        rig.service.stop(0, 1)
    assert rig.commands == []


@pytest.mark.parametrize(
    ("action", "kind", "code"),
    [
        (FirmwareCode("3"), "firmware", "3"),
        (FirmwareCode("Z"), "parse", None),  # outside the 8.2 table: fail closed (FR-9.3)
        (NoReply(), "no_reply", None),
    ],
)
@pytest.mark.parametrize("released", [False, True])
def test_t13s3_failed_stop_raises_stop_failed_never_success(
    tmp_path: Path, action: Any, kind: str, code: str | None, released: bool
) -> None:
    rig = OpsRig(tmp_path)
    if released:
        rig.release()
    rig.dev.script(match=Match(mnemonic="ATE"), action=action, times=None)
    with pytest.raises(StopFailed) as info:
        rig.service.stop(0, 1)
    err = info.value
    assert (err.controller_id, err.slot_id, err.kind, err.code) == (0, 1, kind, code)
    assert "unknown" in str(err)
    assert "not applied" not in str(err).lower()
    assert rig.commands[0] == "1ATE0"


def test_t13s3_device_loss_during_stop_raises_stop_failed_and_controller_is_lost(
    tmp_path: Path,
) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=DeviceLoss(), times=1)
    with pytest.raises(StopFailed) as info:
        rig.service.stop(0, 1)
    assert info.value.failure == "device_lost"
    assert info.value.code is None
    assert "not applied" not in str(info.value).lower()
    assert rig.manager.state == "lost"


def test_fr85_stop_on_a_lost_controller_is_refused_without_io(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.make_lost()
    with pytest.raises(ControllerNotConnected):
        rig.service.stop(0, 1)
    assert rig.commands == [] and rig.reg.factory.events == []


# --- FR-8.6 / FR-12.6: stop-all -----------------------------------------------------------------


def test_fr86_stop_all_writes_only_ate0_and_status_ok(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    result = rig.service.stop_all()
    assert result.status == "ok"
    assert result.outcomes == [
        StopAllSlotOutcome(cid, s, "stopped") for cid in (0, 1) for s in (1, 2)
    ]
    for cid in (0, 1):
        assert rig.reg.commands(cid) == RECONNECT_STOP
        assert not any("ASE" in c for c in rig.reg.commands(cid))


def test_fr86_stop_all_with_a_failed_slot_is_failed_and_attempts_every_slot(
    tmp_path: Path,
) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"), times=None)
    result = rig.service.stop_all()
    assert result.status == "failed"
    assert result.outcomes[0] == StopAllSlotOutcome(0, 1, "failed", "3")
    assert [o.outcome for o in result.outcomes[1:]] == ["stopped"] * 3


def test_fr86_stop_all_released_open_failure_is_not_delivered(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    rig.release()
    rig.dev.script(action=OpenFailure(), times=1)
    rig.reg.dev(1).script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"))
    result = rig.service.stop_all()
    assert result.status == "not_delivered"  # not_delivered wins over failed
    assert [o.outcome for o in result.outcomes if o.controller_id == 0] == ["not_delivered"] * 2
    assert StopAllSlotOutcome(1, 1, "failed", "3") in result.outcomes
    assert rig.commands == []


def test_fr86_skipped_lost_controller_does_not_change_status(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    rig.make_lost()
    result = rig.service.stop_all()
    assert result.status == "ok"
    assert [o.outcome for o in result.outcomes if o.controller_id == 0] == ["skipped"] * 2
    assert rig.commands == []


def test_fr86_stop_all_while_stopping_raises_service_stopping(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.reg.registry.shutdown()
    rig.mark()
    with pytest.raises(ServiceStopping):
        rig.service.stop_all()
    assert rig.commands == []


# --- FR-17.1 / FR-17.2 / FR-17.5: emergency-off -------------------------------------------------


def test_fr171_emergency_off_writes_aeo_and_logs_request_and_outcome(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].enabled = True
    with capture_logs() as events:
        result = rig.service.emergency_off(0, True)
    assert result == EmergencyOffResult(0, True)
    assert rig.commands == ["0AEO", "0RDC1"]
    assert rig.manager.emergency_off_active is True
    assert rig.service.status()[0].emergency_off_active is True
    assert eo_lines(events) == [
        ("warning", "request", None),
        ("warning", "outcome", "emergency_off_active"),
    ]
    assert all(e["controller_id"] == 0 for e in events_named(events, EO))


@pytest.mark.parametrize("confirm", [False, None, "true", 1, "True", [True]])
def test_fr172_emergency_off_without_exact_true_is_invalid_with_zero_writes(
    tmp_path: Path, confirm: Any
) -> None:
    rig = OpsRig(tmp_path)
    with capture_logs() as events, pytest.raises(InvalidRequest):
        rig.service.emergency_off(0, confirm)
    assert rig.commands == [] and rig.reg.factory.events == []
    assert rig.manager.emergency_off_active is False
    assert eo_lines(events) == [
        ("warning", "request", None),
        ("warning", "outcome", "rejected_unconfirmed"),
    ]


def test_fr172_unconfirmed_emergency_off_on_released_controller_does_not_open(
    tmp_path: Path,
) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    with pytest.raises(InvalidRequest):
        rig.service.emergency_off(0, False)
    assert rig.commands == [] and rig.reg.factory.events == []


def test_fr174_emergency_off_while_released_is_transient(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    with capture_logs() as events:
        rig.service.emergency_off(0, True)
    assert rig.commands == ["0AEO", "0RDC1"]
    assert rig.manager.state == "released" and not rig.dev.is_open
    assert rig.manager.emergency_off_active is True
    assert eo_lines(events)[-1] == ("warning", "outcome", "emergency_off_active")


def test_fr175_emergency_off_not_delivered_logs_error(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    rig.dev.script(action=OpenFailure(), times=1)
    with capture_logs() as events, pytest.raises(StopNotDelivered) as info:
        rig.service.emergency_off(0, True)
    assert info.value.operation == "emergency_off"
    assert rig.manager.emergency_off_active is False
    assert eo_lines(events) == [("warning", "request", None), ("error", "outcome", "not_delivered")]


@pytest.mark.parametrize(
    ("action", "kind", "failure"),
    [(FirmwareCode("3"), "firmware", "3"), (FirmwareCode("Z"), "parse", "parse"),
     (NoReply(), "no_reply", "no_reply")],
)  # fmt: skip
def test_fr176_failed_aeo_propagates_flag_unchanged_and_logs_warning(
    tmp_path: Path, action: Any, kind: str, failure: str
) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.script(match=Match(mnemonic="AEO"), action=action, times=None)
    with capture_logs() as events, pytest.raises(TransactionFailed) as info:
        rig.service.emergency_off(0, True)
    assert info.value.kind == kind
    assert rig.manager.emergency_off_active is False
    line = events_named(events, EO)[-1]
    assert (line["log_level"], line["outcome"], line["failure"]) == ("warning", "failed", failure)


@pytest.mark.parametrize("released", [False, True])
def test_t13s3_device_loss_during_emergency_off_is_outcome_unknown(
    tmp_path: Path, released: bool
) -> None:
    rig = OpsRig(tmp_path)
    if released:
        rig.release()
    rig.dev.script(match=Match(mnemonic="AEO"), action=DeviceLoss(), times=1)
    with capture_logs() as events, pytest.raises(EmergencyOffOutcomeUnknown) as info:
        rig.service.emergency_off(0, True)
    err = info.value
    assert not isinstance(err, ControllerNotConnected | StopNotDelivered)
    expected_state = "released" if released else "lost"
    assert err.state == expected_state == rig.manager.state
    assert rig.manager.emergency_off_active is False
    assert "unknown" in str(err) and "not applied" not in str(err).lower()
    assert eo_lines(events)[-1] == ("error", "outcome", "outcome_unknown")


def test_fr175_emergency_off_refused_before_io_is_not_sent_at_error(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.make_lost()
    with capture_logs() as events, pytest.raises(ControllerNotConnected):
        rig.service.emergency_off(0, True)
    assert rig.commands == []
    line = events_named(events, EO)[-1]
    assert (line["log_level"], line["outcome"]) == ("error", "not_sent")
    assert line["failure"] == "ControllerNotConnected"


def test_fr175_emergency_off_while_stopping_is_not_sent_at_error(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.reg.registry.shutdown()
    rig.mark()
    with capture_logs() as events, pytest.raises(ServiceStopping):
        rig.service.emergency_off(0, True)
    assert rig.commands == []
    assert eo_lines(events)[-1] == ("error", "outcome", "not_sent")


def test_fr175_emergency_off_busy_is_not_sent_at_error(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, lock_wait_s=1)
    with rig.reg.held(0), capture_logs() as events, pytest.raises(ControllerBusy):
        rig.service.emergency_off(0, True)
    assert "0AEO" not in rig.commands
    line = events_named(events, EO)[-1]
    assert (line["log_level"], line["outcome"], line["failure"]) == (
        "error",
        "not_sent",
        "ControllerBusy",
    )


# --- T14-S3: release / reacquire guard the DD-11 released set -----------------------------------


def test_t14s3_release_writes_pending_set_first(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    rig.reg.registry._release_state_pending = frozenset({0, 1})  # simulate a failed DD-11 write
    assert rig.service.release(0) == LifecycleResult(0, "released")
    assert rig.reg.factory.events.index("store_replace_all") < rig.reg.factory.events.index(
        "store_add"
    )
    assert rig.reg.store.released() == {0, 1}


def test_t14s3_release_refused_on_state_store_error_state_unchanged(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, ids=(0, 1))
    rig.reg.registry._release_state_pending = frozenset({0, 1})
    rig.reg.store.fail_replace = True
    with pytest.raises(StateStoreError):
        rig.service.release(0)
    assert rig.manager.state == "connected" and rig.dev.is_open
    assert ("add", 0) not in rig.reg.store.calls


def test_t14s3_reacquire_after_corrupt_load_refused_then_keeps_other_controller(
    tmp_path: Path,
) -> None:
    (tmp_path / "state.json").write_text("{ not json", encoding="utf-8")
    rig = OpsRig(tmp_path, ids=(0, 1), start=False)
    rig.reg.store.fail_replace = True
    rig.reg.started()
    assert {m.state for m in rig.reg.registry.managers.values()} == {"released"}
    with pytest.raises(StateStoreError):
        rig.service.reacquire(0)
    assert rig.manager.state == "released"
    assert rig.reg.factory.events.count("open") == 0 and rig.commands == []
    assert ("remove", 0) not in rig.reg.store.calls
    rig.reg.store.fail_replace = False
    result = rig.service.reacquire(0)
    assert result == ReacquireResult(0, "connected", [], False)
    assert rig.reg.store.released() == {1}  # controller 1 stays released on disk


def test_fr125_release_and_reacquire_round_trip(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.release(0) == LifecycleResult(0, "released")
    assert rig.service.release(0) == LifecycleResult(0, "released")  # idempotent
    rig.mark()
    assert rig.service.reacquire(0) == ReacquireResult(0, "connected", [], False)
    assert rig.commands == IDENTIFY_1 + IDENTIFY_2  # no ATE0 without emergency-off
    assert rig.service.reacquire(0) == ReacquireResult(0, "connected", [], False)


# --- T13-C1 / FR-17.8: reacquire reports the re-initialize --------------------------------------


def test_t13c1_reacquire_with_emergency_off_reports_cleared_and_stopped_slots(
    tmp_path: Path,
) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    rig.service.emergency_off(0, True)
    rig.mark()
    result = rig.service.reacquire(0)
    assert result == ReacquireResult(0, "connected", [1, 2], True)
    assert rig.commands[:4] == RECONNECT_STOP
    assert rig.manager.emergency_off_active is False


def test_t13c1_failed_reacquire_with_emergency_off_reports_nothing_cleared(
    tmp_path: Path,
) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    rig.service.emergency_off(0, True)
    rig.mark()
    rig.dev.script(action=OpenFailure(), times=1)
    with pytest.raises(ControllerNotConnected) as info:
        rig.service.reacquire(0)
    assert info.value.state == "released"
    assert rig.manager.state == "released"
    assert rig.manager.emergency_off_active is True


# --- T9-C1 / T9-C6: wording ---------------------------------------------------------------------


def test_t9c1_no_runtime_string_in_operations_says_not_applied(package_dir: Path) -> None:
    for name in ("operations.py", "operations_read.py"):
        tree = ast.parse((package_dir / name).read_text(encoding="utf-8"))
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    assert "not applied" not in node.value.lower(), (name, node.value)

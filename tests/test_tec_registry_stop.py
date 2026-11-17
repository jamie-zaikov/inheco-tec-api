"""Tests for C9 ``ControllerRegistry`` stop-all and graceful shutdown (Task 14; C13, S7, P6).

Requirements: FR-8.6, FR-11, FR-11.1, FR-11.2, plus carry-forward T13-S3 (stop-all never reports a
failed / not-delivered / not-attempted slot as ``stopped``; ``disconnected``/``lost`` slots are
``skipped`` and logged at error level), T14-E (``ControllerBusy`` or an unexpected exception in
stop-all is ``failed``; ``ServiceStopping`` while stopping) and T7-C3 (shutdown never closes a
handle whose lock another thread holds; with a free lock and a passed deadline the handle is closed
under the lock and no ``ATE0`` is sent). Lock contention uses a read that really blocks (T8-C2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from inheco_tec_api.registry import ServiceStopping, ShutdownSlotOutcome, StopAllSlotOutcome
from inheco_tec_api.transport.fake_actions import DeviceLoss, FirmwareCode, Match, OpenFailure
from structlog.testing import capture_logs
from tec_controller_helpers import events_named
from tec_registry_helpers import IDS, STOP_SWEEP, RegRig


def stopped_all(cids: tuple[int, ...] = IDS) -> list[StopAllSlotOutcome]:
    return [StopAllSlotOutcome(cid, slot, "stopped") for cid in cids for slot in (1, 2)]


def shut_all(cids: tuple[int, ...] = IDS) -> list[ShutdownSlotOutcome]:
    return [ShutdownSlotOutcome(cid, slot, "stopped") for cid in cids for slot in (1, 2)]


def make_lost(rig: RegRig, cid: int) -> None:
    rig.dev(cid).script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    rig.manager(cid).stop_slots([1])
    assert rig.manager(cid).state == "lost"


# --- FR-8.6: stop-all ---------------------------------------------------------------------------


def test_fr86_stop_all_writes_only_ate0_to_every_connected_slot(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    for cid in IDS:
        rig.dev(cid).slots[1].enabled = rig.dev(cid).slots[2].enabled = True
    assert rig.registry.stop_all() == stopped_all()
    for cid in IDS:
        assert rig.commands(cid) == STOP_SWEEP  # never ASE or any other command
        assert not any("ASE" in c for c in rig.commands(cid))
        assert not rig.dev(cid).slots[1].enabled and not rig.dev(cid).slots[2].enabled
        assert rig.manager(cid).state == "connected"


def test_fr86_one_failing_slot_does_not_stop_the_others(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.dev(0).script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"), times=None)
    outcomes = rig.registry.stop_all()
    assert outcomes[0] == StopAllSlotOutcome(0, 1, "failed", "3")
    assert outcomes[1:] == stopped_all()[1:]
    assert "2ATE0" in rig.commands(0)
    assert rig.commands(1) == STOP_SWEEP


def test_fr86_released_controller_is_stopped_through_the_transient_open(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.release(0)
    rig.mark()
    outcomes = rig.registry.stop_all()
    # connected controllers first, then the released one
    assert outcomes == stopped_all((1,)) + stopped_all((0,))
    assert rig.commands(0) == STOP_SWEEP
    assert rig.manager(0).state == "released"
    assert not rig.dev(0).is_open
    assert rig.store.released() == {0}
    assert rig.factory.unlocked_io == []


def test_fr86_released_controller_open_failure_is_not_delivered(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.release(0)
    rig.mark()
    rig.dev(0).script(action=OpenFailure(), times=1)
    with capture_logs() as events:
        outcomes = rig.registry.stop_all()
    assert outcomes == stopped_all((1,)) + [
        StopAllSlotOutcome(0, 1, "not_delivered"),
        StopAllSlotOutcome(0, 2, "not_delivered"),
    ]
    assert rig.commands(0) == []
    [line] = events_named(events, "tec_stop_not_delivered")
    assert line["log_level"] == "error"


def test_p6_disconnected_and_lost_slots_are_skipped_with_error_logs(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    rig.dev(0).present = False
    rig.started()
    make_lost(rig, 1)
    rig.mark()
    with capture_logs() as events:
        outcomes = rig.registry.stop_all()
    assert outcomes == [
        StopAllSlotOutcome(0, 1, "skipped", "disconnected"),
        StopAllSlotOutcome(0, 2, "skipped", "disconnected"),
        StopAllSlotOutcome(1, 1, "skipped", "lost"),
        StopAllSlotOutcome(1, 2, "skipped", "lost"),
    ]
    assert rig.factory.events == [] and rig.commands(0) == rig.commands(1) == []
    lines = events_named(events, "tec_slot_stop")
    assert [(e["controller_id"], e["slot"]) for e in lines] == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert all(e["log_level"] == "error" and e["outcome"] == "skipped" for e in lines)
    assert all(e["purpose"] == "stop_all" for e in lines)


def test_t13s3_loss_during_stop_all_is_never_reported_as_stopped(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.dev(0).script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    outcomes = rig.registry.stop_all()
    assert outcomes[:2] == [
        StopAllSlotOutcome(0, 1, "failed", "device_lost"),
        StopAllSlotOutcome(0, 2, "failed", "not_attempted"),
    ]
    assert outcomes[2:] == stopped_all((1,))
    assert rig.manager(0).state == "lost"


def test_t13s3_loss_on_a_released_controller_is_failed(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.release(0)
    rig.dev(0).script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    outcomes = rig.registry.stop_all()
    assert [o for o in outcomes if o.controller_id == 0] == [
        StopAllSlotOutcome(0, 1, "failed", "device_lost"),
        StopAllSlotOutcome(0, 2, "failed", "not_attempted"),
    ]
    assert rig.manager(0).state == "released"


def test_t14e_busy_controller_in_stop_all_is_failed_controller_busy(tmp_path: Path) -> None:
    rig = RegRig(tmp_path, lock_wait_s=1).started()
    with rig.held(0), capture_logs() as events:
        outcomes = rig.registry.stop_all()
    assert outcomes[:2] == [
        StopAllSlotOutcome(0, 1, "failed", "controller_busy"),
        StopAllSlotOutcome(0, 2, "failed", "controller_busy"),
    ]
    assert outcomes[2:] == stopped_all((1,))
    failed = [e for e in events_named(events, "tec_slot_stop") if e["controller_id"] == 0]
    assert [e["log_level"] for e in failed] == ["error", "error"]


def test_t14e_unexpected_error_in_stop_all_is_failed_with_type_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = RegRig(tmp_path).started()

    def boom(slot_ids: object) -> object:
        raise RuntimeError("secret-ish detail")

    monkeypatch.setattr(rig.manager(0), "stop_slots", boom)
    with capture_logs() as events:
        outcomes = rig.registry.stop_all()
    assert outcomes[:2] == [
        StopAllSlotOutcome(0, 1, "failed", "RuntimeError"),
        StopAllSlotOutcome(0, 2, "failed", "RuntimeError"),
    ]
    assert outcomes[2:] == stopped_all((1,))
    assert "secret-ish detail" not in repr(events)


def test_c9_stop_all_and_device_requests_are_refused_while_stopping(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    assert rig.registry.device_manager(0) is rig.manager(0)
    with pytest.raises(KeyError):
        rig.registry.device_manager(7)
    assert not rig.registry.stopping
    rig.registry.shutdown()
    rig.mark()
    assert rig.registry.stopping
    with pytest.raises(ServiceStopping):
        rig.registry.stop_all()
    with pytest.raises(ServiceStopping):
        rig.registry.device_manager(0)
    with pytest.raises(ServiceStopping):
        rig.registry.check_accepting()
    assert rig.commands(0) == rig.commands(1) == []


# --- FR-11.1 / FR-11.2: shutdown ----------------------------------------------------------------


def test_fr111_shutdown_writes_ate0_for_slots_1_and_2_and_closes(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    with capture_logs() as events:
        outcomes = rig.registry.shutdown(rig.vt.now + 10)
    assert outcomes == shut_all()
    for cid in IDS:
        assert rig.commands(cid) == STOP_SWEEP
        assert not rig.dev(cid).is_open
        assert rig.manager(cid).state == "disconnected"
    assert rig.factory.unlocked_io == []
    lines = events_named(events, "tec_slot_stop")
    assert [(e["controller_id"], e["slot"], e["outcome"]) for e in lines] == [
        (0, 1, "stopped"),
        (0, 2, "stopped"),
        (1, 1, "stopped"),
        (1, 2, "stopped"),
    ]
    assert all(e["purpose"] == "shutdown_stop" for e in lines)


def test_fr111_default_deadline_comes_from_the_manifest(tmp_path: Path) -> None:
    rig = RegRig(tmp_path, shutdown_timeout_s=10).started()
    assert rig.registry.shutdown() == shut_all()


def test_fr112_shutdown_while_released_writes_nothing(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.release(0)
    rig.mark()
    outcomes = rig.registry.shutdown(rig.vt.now + 10)
    assert outcomes == shut_all((1,))
    assert rig.commands(0) == []
    assert rig.factory.io_of(0) == []
    assert rig.manager(0).state == "released"
    assert rig.store.released() == {0}


def test_fr11_shutdown_sends_nothing_to_disconnected_or_lost(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    rig.dev(0).present = False
    rig.started()
    make_lost(rig, 1)
    rig.mark()
    assert rig.registry.shutdown(rig.vt.now + 10) == []
    assert rig.factory.events == [] and rig.commands(0) == rig.commands(1) == []
    assert (rig.manager(0).state, rig.manager(1).state) == ("disconnected", "lost")


def test_fr111_deadline_stops_early_and_logs_unattempted_slots(tmp_path: Path) -> None:
    rig = RegRig(tmp_path, ids=(0,), pre_send_delay_ms=200).started()
    with capture_logs() as events:
        outcomes = rig.registry.shutdown(rig.vt.now + 0.05)
    assert outcomes == [
        ShutdownSlotOutcome(0, 1, "stopped"),
        ShutdownSlotOutcome(0, 2, "not_attempted", "shutdown_deadline"),
    ]
    assert "1ATE0" in rig.commands(0) and "2ATE0" not in rig.commands(0)
    assert not rig.dev(0).is_open
    [line] = [e for e in events_named(events, "tec_slot_stop") if e["slot"] == 2]
    assert (line["log_level"], line["outcome"]) == ("error", "not_attempted")


def test_t7c3_passed_deadline_with_free_lock_closes_under_lock_without_ate0(
    tmp_path: Path,
) -> None:
    rig = RegRig(tmp_path).started()
    outcomes = rig.registry.shutdown(rig.vt.now - 1)
    assert outcomes == [
        ShutdownSlotOutcome(cid, slot, "not_attempted", "shutdown_deadline")
        for cid in IDS
        for slot in (1, 2)
    ]
    assert rig.commands(0) == rig.commands(1) == []
    assert rig.factory.events == ["close", "close"]
    assert rig.factory.unlocked_io == []
    assert all(not rig.dev(cid).is_open for cid in IDS)


def test_t7c3_shutdown_never_closes_a_handle_whose_lock_is_held(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    with rig.held(0), capture_logs() as events:
        outcomes = rig.registry.shutdown(rig.vt.now + 0.3)
        assert rig.dev(0).is_open  # still open while the other thread is in its read
        assert "close" not in rig.factory.io_of(0)
    assert outcomes == [
        ShutdownSlotOutcome(0, 1, "not_attempted", "controller_busy"),
        ShutdownSlotOutcome(0, 2, "not_attempted", "controller_busy"),
    ] + shut_all((1,))
    assert rig.factory.unlocked_io == []
    busy = [e for e in events_named(events, "tec_slot_stop") if e["controller_id"] == 0]
    assert all(e["log_level"] == "error" for e in busy) and len(busy) == 2


def test_fr111_device_loss_during_shutdown_marks_the_rest_not_attempted(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.dev(0).script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    outcomes = rig.registry.shutdown(rig.vt.now + 10)
    assert outcomes[:2] == [
        ShutdownSlotOutcome(0, 1, "failed", "device_lost"),
        ShutdownSlotOutcome(0, 2, "not_attempted", "device_lost"),
    ]
    assert outcomes[2:] == shut_all((1,))
    assert rig.manager(0).state == "lost"
    assert not rig.dev(0).is_open
    assert rig.factory.unlocked_io == []


def test_fr111_second_shutdown_sends_nothing(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.registry.shutdown(rig.vt.now + 10)
    rig.mark()
    assert rig.registry.shutdown(rig.vt.now + 10) == []
    assert rig.factory.events == []

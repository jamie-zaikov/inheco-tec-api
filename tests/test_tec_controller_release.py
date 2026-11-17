"""Tests for C9 release and reacquire, emergency-off inactive paths (Task 12).

Requirements: FR-12, FR-12.1, FR-12.2, FR-12.3, FR-12.4, FR-12.5, FR-15.2, plus carry-forward
T11-C2 (the shared store is used, never built; ``StateStoreError`` on release leaves the controller
``connected`` with its handle open; on reacquire it leaves ``released`` with the entry persisted).

Executor deviations covered: release persists BEFORE it closes the handle; release/reacquire from
``disconnected`` / ``lost`` raise ``ControllerNotConnected``; a loss during reacquire keeps
``released``. Reacquire with emergency-off active (FR-17.8) is in ``test_tec_controller_reinit.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from inheco_tec_api.controller import (
    ControllerNotConnected,
    ControllerReleased,
)
from inheco_tec_api.protocol import commands
from inheco_tec_api.state_store import StateStoreError
from inheco_tec_api.transport.fake import FakeSlot
from inheco_tec_api.transport.fake_actions import DeviceLoss, Match
from structlog.testing import capture_logs
from tec_controller_helpers import CID, IDENTIFY_1, IDENTIFY_2, Rig, events_named


def released_rig(tmp_path: Path) -> Rig:
    """A rig that connected at startup and then released (writes and events forgotten)."""
    rig = Rig(tmp_path).connected()
    rig.manager.release()
    rig.mark()
    rig.store.calls.clear()
    return rig


# --- FR-12.1 / FR-12.4: release ------------------------------------------------------------------


def test_fr121_release_persists_closes_and_changes_no_slot(tmp_path: Path) -> None:
    slots = {1: FakeSlot(enabled=True, target=370), 2: FakeSlot()}
    rig = Rig(tmp_path, slots=slots).connected()
    with capture_logs() as events:
        state = rig.manager.release()
    assert state == "released" == rig.manager.state
    assert rig.store.calls == [("add", CID)]
    assert rig.store.released() == {CID}
    assert not rig.handle_open
    assert rig.commands == []  # no ATE0, no STT
    assert (rig.dev.slots[1].enabled, rig.dev.slots[1].target) == (True, 370)
    assert rig.factory.unlocked_io == []
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("connected", "released", "release")
    ]


def test_fr121_release_persists_before_closing_the_handle(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.manager.release()
    assert rig.factory.events == ["store_add", "close"]


def test_fr122_released_controller_refuses_commands_without_io(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    with pytest.raises(ControllerReleased):
        rig.manager.run(commands.rtt(1))
    with pytest.raises(ControllerReleased):
        rig.manager.gate("set_target", 1)
    assert rig.factory.events == []
    assert rig.commands == []


def test_fr125_release_of_released_is_a_no_op(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    with capture_logs() as events:
        assert rig.manager.release() == "released"
    assert rig.store.calls == []
    assert rig.factory.events == []
    assert events_named(events, "tec_state_change") == []


def test_fr125_release_after_released_startup_is_a_no_op(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.manager.startup(True)
    assert rig.manager.release() == "released"
    assert rig.store.calls == []
    assert rig.factory.events == []


@pytest.mark.parametrize("state", ["disconnected", "lost"])
def test_release_from_not_connected_states_raises(tmp_path: Path, state: str) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    if state == "lost":
        rig.dev.present = True
        rig.dev.script(match=Match(mnemonic="RTD"), action=DeviceLoss(), times=1)
        with pytest.raises(ControllerNotConnected):
            rig.manager.connect()
    assert rig.manager.state == state
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.release()
    assert info.value.state == state
    assert rig.store.calls == []


def test_t11c2_release_store_error_keeps_connected_with_open_handle(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.store.fail_add = True
    with pytest.raises(StateStoreError):
        rig.manager.release()
    assert rig.manager.state == "connected"
    assert rig.handle_open
    assert "close" not in rig.factory.events
    assert rig.store.released() == set()
    assert rig.manager.run(commands.rtt(1)).error_char == "0"  # still usable


# --- FR-12.3 / FR-12.5: reacquire ----------------------------------------------------------------


def test_fr123_reacquire_opens_identifies_unpersists_and_writes_no_ate0(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.slots[1].enabled = True  # VENUS left slot 1 on: a hand-back is not a loss
    with capture_logs() as events:
        state = rig.manager.reacquire()
    assert state == "connected" == rig.manager.state
    assert rig.commands == IDENTIFY_1 + IDENTIFY_2
    assert rig.dev.slots[1].enabled is True
    assert rig.store.calls == [("remove", CID)]
    assert rig.store.released() == set()
    assert rig.handle_open
    assert all(s.permitted for s in rig.manager.slots.values())
    assert rig.factory.unlocked_io == []
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("released", "connected", "reacquire")
    ]


def test_fr123_reacquire_after_released_startup_connects(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.store.add(CID)
    rig.store.calls.clear()
    rig.manager.startup(True)
    assert rig.manager.reacquire() == "connected"
    assert rig.store.released() == set()
    assert all(cmd[1:4] != "ATE" for cmd in rig.commands)


def test_fr125_reacquire_of_connected_is_a_no_op(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    with capture_logs() as events:
        assert rig.manager.reacquire() == "connected"
    assert rig.store.calls == []
    assert rig.factory.events == []
    assert rig.commands == []
    assert events_named(events, "tec_state_change") == []


@pytest.mark.parametrize("state", ["disconnected", "lost"])
def test_reacquire_from_not_connected_states_raises(tmp_path: Path, state: str) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    if state == "lost":
        rig.dev.present = True
        rig.dev.script(match=Match(mnemonic="RTD"), action=DeviceLoss(), times=1)
        with pytest.raises(ControllerNotConnected):
            rig.manager.connect()
    rig.mark()
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.reacquire()
    assert info.value.state == state
    assert rig.factory.events == []
    assert rig.store.calls == []


def test_fr123_reacquire_open_failure_stays_released_and_persisted(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    rig.dev.present = False
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.reacquire()
    assert info.value.state == "released"
    assert rig.manager.state == "released"
    assert rig.store.calls == []
    assert rig.store.released() == {CID}
    assert not rig.handle_open


def test_t10c_reacquire_loss_mid_identification_stays_released_keeps_old_data(
    tmp_path: Path,
) -> None:
    rig = released_rig(tmp_path)
    before = {sid: s.identification for sid, s in rig.manager.slots.items()}
    rig.dev.script(match=Match(mnemonic="RAN", address=0, param="2"), action=DeviceLoss(), times=1)
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.reacquire()
    assert info.value.state == "released"
    assert rig.manager.state == "released"
    assert not rig.handle_open
    assert rig.store.calls == []  # never reached the unpersist
    assert rig.store.released() == {CID}
    assert all(cmd[1:4] != "ATE" for cmd in rig.commands)  # no loss stop for a released one
    after = {sid: s.identification for sid, s in rig.manager.slots.items()}
    assert after == before and all(after[sid] is before[sid] for sid in before)


def test_t11c2_reacquire_store_error_stays_released_with_entry_persisted(tmp_path: Path) -> None:
    rig = released_rig(tmp_path)
    before = {sid: s.identification for sid, s in rig.manager.slots.items()}
    rig.store.fail_remove = True
    with pytest.raises(StateStoreError):
        rig.manager.reacquire()
    assert rig.manager.state == "released"
    assert rig.store.released() == {CID}
    assert not rig.handle_open
    assert rig.factory.events[-1] == "close"
    assert all(rig.manager.slots[sid].identification is before[sid] for sid in before)
    rig.store.fail_remove = False
    assert rig.manager.reacquire() == "connected"

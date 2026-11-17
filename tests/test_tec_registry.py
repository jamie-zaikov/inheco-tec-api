"""Tests for C9 ``ControllerRegistry`` startup, status and restart (Task 14; sequence S1).

Requirements: FR-6.1, FR-6.6, FR-12.4, FR-13.4, FR-17.9, DD-11, plus carry-forward T11-E/T11-V (a
corrupt state file is rewritten with every controller in ONE ``replace_all`` before any open; a
failed write still starts everything released and is retried by
``ensure_release_state_persisted``), T11-C2 (one shared store), T12-C1 (each manager starts once;
a second ``startup`` raises), T12-E (an unexpected manager-startup exception never stops the
service; logged with the type name only; no stray handle) and UA-1 (status reports
``emergency_off_active`` on a ``lost`` controller). Status does no I/O and takes no lock.
Stop-all and shutdown are in ``test_tec_registry_stop.py``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import ControllerManager
from inheco_tec_api.controller_startup import read_startup_states
from inheco_tec_api.identification import StartupSlotState
from inheco_tec_api.registry import (
    EVENT_TEC_CONTROLLER_STARTUP_FAILED,
    ControllerRegistry,
    ControllerStatus,
    DetectedDeviceType,
    EffectiveLimits,
    SlotStatus,
)
from inheco_tec_api.state_store import ReleaseStateStore, StateStoreError
from inheco_tec_api.transport.fake_actions import DeviceLoss, Match
from structlog.testing import capture_logs
from tec_controller_helpers import events_named
from tec_registry_helpers import IDS, MultiFactory, RegRig, registry_manifest

CORRUPT = "{ not json"


def write_corrupt(tmp_path: Path) -> Path:
    path = tmp_path / "state.json"
    path.write_text(CORRUPT, encoding="utf-8")
    return path


def all_states(rig: RegRig) -> dict[int, str]:
    return {cid: m.state for cid, m in rig.registry.managers.items()}


# --- DD-11 / T11-E / T11-V: corrupt state file -------------------------------------------------


def test_dd11_corrupt_state_file_starts_every_controller_released_without_open(
    tmp_path: Path,
) -> None:
    write_corrupt(tmp_path)
    rig = RegRig(tmp_path)
    rig.registry.startup()
    assert all_states(rig) == {0: "released", 1: "released"}
    assert "open" not in rig.factory.events and "create" not in rig.factory.events
    assert all(rig.commands(cid) == [] for cid in IDS)
    assert rig.registry.release_state_persisted


def test_t11e_corrupt_load_writes_full_set_once_before_any_open(tmp_path: Path) -> None:
    write_corrupt(tmp_path)
    rig = RegRig(tmp_path, ids=(0, 1, 3))
    rig.registry.startup()
    assert rig.store.replace_calls == [(0, 1, 3)]
    assert rig.store.calls == []  # no per-controller add/remove
    assert rig.factory.events == ["store_replace_all"]
    assert ReleaseStateStore(tmp_path / "state.json").load().released == {0, 1, 3}


def test_t11v_failed_replace_all_still_starts_released_and_retries(tmp_path: Path) -> None:
    path = write_corrupt(tmp_path)
    rig = RegRig(tmp_path)
    rig.store.fail_replace = True
    rig.registry.startup()
    assert all_states(rig) == {0: "released", 1: "released"}
    assert rig.factory.open_count == 0 and "create" not in rig.factory.events
    assert rig.registry.release_state_persisted is False
    with pytest.raises(StateStoreError):
        rig.registry.ensure_release_state_persisted()
    assert rig.store.replace_calls == [(0, 1), (0, 1)]
    assert rig.registry.release_state_persisted is False
    assert path.read_text(encoding="utf-8") == CORRUPT
    rig.store.fail_replace = False
    rig.registry.ensure_release_state_persisted()
    assert rig.registry.release_state_persisted is True
    assert rig.store.released() == {0, 1}
    rig.registry.ensure_release_state_persisted()  # nothing pending: no write
    assert len(rig.store.replace_calls) == 3


def test_t11e_ensure_release_state_is_a_noop_after_a_clean_load(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.registry.ensure_release_state_persisted()
    assert rig.store.replace_calls == []
    assert rig.registry.release_state_persisted


# --- FR-12.4 / FR-6.1: startup ----------------------------------------------------------------


def test_fr124_listed_controller_stays_released_and_is_not_opened(tmp_path: Path) -> None:
    ReleaseStateStore(tmp_path / "state.json").add(0)
    rig = RegRig(tmp_path)
    rig.registry.startup()
    assert all_states(rig) == {0: "released", 1: "connected"}
    assert rig.factory.io_of(0) == []
    assert rig.commands(0) == []
    assert rig.store.replace_calls == [] and rig.store.calls == []


def test_fr61_missing_state_file_connects_every_controller(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    rig.registry.startup()
    assert all_states(rig) == {0: "connected", 1: "connected"}
    assert not (tmp_path / "state.json").exists()


def test_fr61_absent_controller_is_disconnected_and_the_service_starts(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    rig.dev(0).present = False
    rig.registry.startup()
    assert all_states(rig) == {0: "disconnected", 1: "connected"}
    assert [s.state for s in rig.registry.status()] == ["disconnected", "connected"]


def test_fr61_unexpected_manager_startup_error_is_logged_and_service_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def flaky(engine: Any, slot_ids: Any) -> Any:
        if engine.controller_id == 0:
            raise RuntimeError("sensitive-detail-xyz")
        return read_startup_states(engine, slot_ids)

    monkeypatch.setattr("inheco_tec_api.controller.read_startup_states", flaky)
    rig = RegRig(tmp_path)
    with capture_logs() as events:
        rig.registry.startup()
    assert all_states(rig) == {0: "disconnected", 1: "connected"}
    assert not rig.dev(0).is_open
    assert rig.factory.io_of(0).count("open") == rig.factory.io_of(0).count("close")
    assert rig.factory.unlocked_io == []
    [line] = events_named(events, EVENT_TEC_CONTROLLER_STARTUP_FAILED)
    assert line["log_level"] == "error"
    assert (line["controller_id"], line["error"]) == (0, "RuntimeError")
    assert "sensitive-detail-xyz" not in repr(events)


def test_t12c1_failed_startup_leaves_no_stray_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = RegRig(tmp_path)
    manager = rig.manager(0)

    def boom(initially_released: bool) -> None:
        with manager.locked():
            transport = rig.factory.create(0)
            transport.open()
            manager._attach(transport)
        raise ValueError("boom")

    monkeypatch.setattr(manager, "startup", boom)
    rig.registry.startup()
    assert manager.state == "disconnected"
    assert not rig.dev(0).is_open
    assert rig.factory.unlocked_io == []
    assert rig.manager(1).state == "connected"


def test_t12c1_failed_startup_of_a_released_controller_stays_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ReleaseStateStore(tmp_path / "state.json").add(0)
    rig = RegRig(tmp_path)

    def boom(initially_released: bool) -> None:
        raise KeyError("boom")

    monkeypatch.setattr(rig.manager(0), "startup", boom)
    rig.registry.startup()
    assert all_states(rig) == {0: "released", 1: "connected"}
    assert rig.factory.io_of(0) == []


def test_t12c1_each_manager_starts_once_and_a_second_startup_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = ControllerManager.startup

    def counting(self: ControllerManager, initially_released: bool) -> None:
        calls.append(self.cid)
        real(self, initially_released)

    monkeypatch.setattr(ControllerManager, "startup", counting)
    rig = RegRig(tmp_path).started()
    assert sorted(calls) == [0, 1]
    with pytest.raises(RuntimeError):
        rig.registry.startup()
    assert sorted(calls) == [0, 1]
    assert rig.factory.events == []
    assert all(rig.dev(cid).open_count == 1 for cid in IDS)


# --- T11-C2: one store ------------------------------------------------------------------------


def test_t11c2_one_store_instance_is_shared_by_every_manager(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    assert rig.registry.state_store is rig.store
    assert all(m._store is rig.store for m in rig.registry.managers.values())


def test_t11c2_default_store_is_one_instance_at_the_manifest_path(tmp_path: Path) -> None:
    manifest = registry_manifest(tmp_path)
    registry = ControllerRegistry(manifest, MultiFactory(RegRig(tmp_path).vt))
    store = registry.state_store
    assert store.path == Path(manifest.service.state_file)
    assert all(m._store is store for m in registry.managers.values())


# --- FR-6.6: status ---------------------------------------------------------------------------


def _connected_slot(slot_id: int) -> SlotStatus:
    return SlotStatus(
        slot_id=slot_id,
        configured_class="cpac",
        detected_device_type=DetectedDeviceType(1, "CPAC"),
        detected_article_number="7000179",
        variant="CPAC Microplate",
        effective_limits=EffectiveLimits(4.0, 70.0),
        control_permitted=True,
        not_permitted_reason=None,
        startup_state=StartupSlotState("off", 0.0, 25.0),
    )


def test_fr66_status_reports_every_field_of_a_connected_controller(tmp_path: Path) -> None:
    rig = RegRig(tmp_path, ids=(0,)).started()
    assert rig.registry.status() == [
        ControllerStatus(0, "connected", True, False, [_connected_slot(1), _connected_slot(2)])
    ]


def test_fr66_status_of_a_released_controller_has_unidentified_slots(tmp_path: Path) -> None:
    ReleaseStateStore(tmp_path / "state.json").add(0)
    rig = RegRig(tmp_path).started()
    released, connected = rig.registry.status()
    assert (released.controller_id, released.state) == (0, "released")
    assert connected.state == "connected"
    for slot in released.slots:
        assert slot.configured_class == "cpac"
        assert slot.detected_device_type is None and slot.detected_article_number is None
        assert slot.variant is None and slot.effective_limits is None
        assert slot.control_permitted is False
        assert slot.not_permitted_reason == "slot_not_identified"
        assert slot.startup_state is None


def test_fr66_status_reports_a_not_permitted_slot_with_its_reason(tmp_path: Path) -> None:
    rig = RegRig(tmp_path, ids=(0,))
    rig.dev(0).slots[2].article_number = "7999999"
    rig.started()
    [status] = rig.registry.status()
    slot1, slot2 = status.slots
    assert slot1.control_permitted is True
    assert slot2.detected_article_number == "7999999"
    assert slot2.variant is None
    assert slot2.effective_limits is None
    assert slot2.control_permitted is False
    assert slot2.not_permitted_reason


def test_ua1_status_reports_emergency_off_on_a_lost_controller(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    manager = rig.manager(0)
    manager.emergency_off()
    rig.dev(0).script(match=Match(mnemonic="ATE"), action=DeviceLoss(), times=1)
    manager.stop_slots([1])
    assert manager.state == "lost"
    lost, other = rig.registry.status()
    assert (lost.state, lost.emergency_off_active) == ("lost", True)
    assert (other.state, other.emergency_off_active) == ("connected", False)


def test_fr66_status_does_no_io_and_takes_no_lock(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    with rig.held(0):
        writes = len(rig.dev(0).writes) + len(rig.dev(1).writes)
        events = list(rig.factory.events)
        rig.factory.create_error = RuntimeError("status must not open")
        start = time.monotonic()
        result = rig.registry.status()
        assert time.monotonic() - start < 1.0  # lock_wait_s is 16 s
        assert len(rig.dev(0).writes) + len(rig.dev(1).writes) == writes
        assert rig.factory.events == events
        rig.factory.create_error = None
    assert [s.state for s in result] == ["connected", "connected"]


# --- FR-13.4: fake transport -------------------------------------------------------------------


def test_fr134_fake_factory_logs_warning_and_status_reports_it(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    with capture_logs() as events:
        rig.registry.startup()
    [line] = events_named(events, "transport_fake_active")
    assert line["log_level"] == "warning"
    assert line["controllers"] == "0,1"
    assert all(s.transport_fake for s in rig.registry.status())


def test_fr134_real_factory_logs_no_fake_warning(tmp_path: Path) -> None:
    rig = RegRig(tmp_path)
    rig.factory.is_fake = False
    with capture_logs() as events:
        rig.registry.startup()
    assert events_named(events, "transport_fake_active") == []
    assert not any(s.transport_fake for s in rig.registry.status())


# --- FR-12.4 / FR-17.9: simulated restart -------------------------------------------------------


def test_fr124_released_state_survives_a_restart(tmp_path: Path) -> None:
    rig = RegRig(tmp_path).started()
    rig.release(0)
    rig.mark()
    again = rig.restart()
    again.registry.startup()
    assert all_states(again) == {0: "released", 1: "connected"}
    assert again.factory.io_of(0) == []
    assert again.commands(0) == []
    assert again.registry.state_store is rig.store


def test_fr179_emergency_off_is_clear_after_a_restart_and_startup_sends_no_ate0(
    tmp_path: Path,
) -> None:
    rig = RegRig(tmp_path).started()
    rig.registry.device_manager(0).emergency_off()
    assert rig.manager(0).emergency_off_active
    rig.mark()
    again = rig.restart()
    again.registry.startup()
    assert all(not m.emergency_off_active for m in again.registry.managers.values())
    assert not any(s.emergency_off_active for s in again.registry.status())
    for cid in IDS:
        assert not any(c[1:4] in ("ATE", "STT", "AEO") for c in again.commands(cid))

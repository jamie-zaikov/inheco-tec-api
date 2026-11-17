"""Shared helpers for the controller-lifecycle suites (not a test module; Task 12).

- :func:`make_manifest` builds a fake-kind :class:`Manifest` (controller 0, slots 1 and 2) with
  zero delays and a chosen ``lock_wait_s``.
- :class:`LifecycleFactory` is a ``TransportFactory`` over one persistent ``FakeTecDevice`` per
  controller. Every ``create`` returns a new :class:`LockCheckingTransport` wrapper; the factory
  keeps them in order, can raise on ``create`` or ``open`` (carry-forward T7-C2), and logs every
  open/close to a shared event list.
- :class:`LockCheckingTransport` records whether the manager's lock was held by the calling thread
  at every ``open``/``close`` (carry-forward T7-C3) and can block inside ``read_report`` on a
  ``threading.Event`` so a second thread really contends for the lock (carry-forward T8-C2).
- :class:`RecordingStore` is a real :class:`ReleaseStateStore` under ``tmp_path`` that records each
  ``add``/``remove`` and can be told to raise ``StateStoreError`` (carry-forward T11-C2).

The tests directory has no ``__init__.py`` (design Risk R8); modules import this file by its unique
top-level name.
"""

from __future__ import annotations

import threading
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from typing import Any

from inheco_tec_api.controller import ControllerManager
from inheco_tec_api.manifest import Manifest
from inheco_tec_api.state_store import ReleaseStateStore, StateStoreError
from inheco_tec_api.transport.base import HidTransport
from inheco_tec_api.transport.fake import FakeSlot, FakeTecDevice
from tec_engine_helpers import RecordingTransport, VirtualTime
from tec_manifest_helpers import base_manifest

CID = 0
IDENTIFY_1 = ["0RTD1", "0RDC1", "0RAN1", "0RDC1", "1RLT", "0RDC1", "1RMT1", "0RDC1"]
IDENTIFY_2 = ["0RTD2", "0RDC1", "0RAN2", "0RDC1", "2RLT", "0RDC1", "2RMT1", "0RDC1"]
STARTUP_1 = ["1RHE", "0RDC1", "1RTT", "0RDC1", "1RAT3", "0RDC1"]
STARTUP_2 = ["2RHE", "0RDC1", "2RTT", "0RDC1", "2RAT3", "0RDC1"]
STOP_ALL = ["1ATE0", "0RDC1", "2ATE0", "0RDC1"]


def make_manifest(*, lock_wait_s: int = 16, max_attempts: int = 3) -> Manifest:
    """A valid fake-kind manifest: controller 0 with CPAC slots 1 and 2, zero delays."""
    data = base_manifest()
    data["transport"].update(
        kind="fake",
        pre_send_delay_ms=0,
        retry_delay_ms=0,
        lock_wait_s=lock_wait_s,
        max_attempts=max_attempts,
    )
    return Manifest.model_validate(data)


def lock_held_by_me(manager: ControllerManager) -> bool:
    """Whether the calling thread owns the manager's RLock (test-only peek at ``_lock``)."""
    owned: bool = manager._lock._is_owned()  # type: ignore[attr-defined]
    return owned


def lock_free_elsewhere(manager: ControllerManager) -> bool:
    """Whether another thread can take the manager's lock at once (then gives it back)."""
    result: list[bool] = []

    def probe() -> None:
        got = manager._lock.acquire(blocking=False)
        if got:
            manager._lock.release()
        result.append(got)

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(5)
    return result == [True]


class LockCheckingTransport(RecordingTransport):
    """A recording wrapper that checks the lock at open/close and can block in a read."""

    def __init__(self, device: FakeTecDevice, factory: LifecycleFactory) -> None:
        super().__init__(device)
        self._factory = factory

    def open(self) -> None:
        self._factory.note("open", self)
        if self._factory.open_error is not None:
            error, self._factory.open_error = self._factory.open_error, None
            raise error
        super().open()

    def close(self) -> None:
        self._factory.note("close", self)
        super().close()

    def read_report(self, timeout_ms: int) -> bytes | None:
        gate = self._factory.read_gate
        if gate is not None:
            self._factory.in_read.set()
            if not gate.wait(10):
                raise AssertionError("read gate was never opened")
        return super().read_report(timeout_ms)


class LifecycleFactory:
    """A ``TransportFactory`` with one persistent fake device per controller (see module doc)."""

    is_fake: bool = True

    def __init__(self, device: FakeTecDevice | None = None) -> None:
        self.dev = device if device is not None else FakeTecDevice(CID)
        self.transports: list[LockCheckingTransport] = []
        self.events: list[str] = []
        self.unlocked_io: list[str] = []
        self.manager: ControllerManager | None = None
        self.create_error: BaseException | None = None
        self.open_error: BaseException | None = None
        self.read_gate: threading.Event | None = None
        self.in_read = threading.Event()

    def note(self, what: str, transport: LockCheckingTransport) -> None:
        """Record an open/close and whether it happened without the manager's lock."""
        self.events.append(what)
        if self.manager is not None and not lock_held_by_me(self.manager):
            self.unlocked_io.append(what)

    def create(self, controller_id: int) -> HidTransport:
        assert controller_id == self.dev.controller_id
        self.events.append("create")
        if self.create_error is not None:
            error, self.create_error = self.create_error, None
            raise error
        transport = LockCheckingTransport(self.dev, self)
        self.transports.append(transport)
        return transport

    @property
    def open_count(self) -> int:
        return self.events.count("open")

    @property
    def close_count(self) -> int:
        return self.events.count("close")


class RecordingStore(ReleaseStateStore):
    """A real store under ``tmp_path`` that records calls and can fail them."""

    def __init__(self, path: Path, events: list[str] | None = None) -> None:
        super().__init__(path)
        self.calls: list[tuple[str, int]] = []
        self.events = events if events is not None else []
        self.fail_add = False
        self.fail_remove = False

    def add(self, cid: int) -> None:
        self.calls.append(("add", cid))
        self.events.append("store_add")
        if self.fail_add:
            raise StateStoreError("simulated write failure")
        super().add(cid)

    def remove(self, cid: int) -> None:
        self.calls.append(("remove", cid))
        self.events.append("store_remove")
        if self.fail_remove:
            raise StateStoreError("simulated write failure")
        super().remove(cid)

    def released(self) -> set[int]:
        return set(self.load().released)


class Rig:
    """One manager wired to a :class:`LifecycleFactory`, a :class:`RecordingStore` and time."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        lock_wait_s: int = 16,
        max_attempts: int = 3,
        slots: dict[int, FakeSlot] | None = None,
    ) -> None:
        self.vt = VirtualTime()
        self.dev = FakeTecDevice(CID, slots=slots, clock=self.vt.clock)
        self.factory = LifecycleFactory(self.dev)
        self.store = RecordingStore(tmp_path / "state.json", self.factory.events)
        self.manager = ControllerManager(
            CID,
            make_manifest(lock_wait_s=lock_wait_s, max_attempts=max_attempts),
            self.factory,
            self.store,
            sleep=self.vt.sleep,
            clock=self.vt.clock,
        )
        self.factory.manager = self.manager

    def connected(self) -> Rig:
        """Start up as connected and forget the startup writes and events."""
        self.manager.startup(False)
        assert self.manager.state == "connected"
        self.mark()
        return self

    def mark(self) -> None:
        """Forget writes and events so far; later assertions see only new activity."""
        self.dev.writes.clear()
        self.factory.events.clear()

    @property
    def commands(self) -> list[str]:
        return self.dev.commands

    @property
    def handle_open(self) -> bool:
        return self.dev.is_open


def events_named(
    events: Sequence[MutableMapping[str, Any]], name: str
) -> list[MutableMapping[str, Any]]:
    """The captured structlog events called ``name``."""
    return [e for e in events if e["event"] == name]

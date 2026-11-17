"""Shared helpers for the controller-registry suites (not a test module; Task 14).

- :func:`registry_manifest` builds a fake-kind :class:`Manifest` with several controllers (default
  0 and 1, each with CPAC slots 1 and 2), zero delays and the state file under ``tmp_path``.
- :class:`MultiFactory` is a :class:`LifecycleFactory` with one persistent ``FakeTecDevice`` per
  controller. Every open/close is logged per controller and checked against that controller's
  lock (carry-forward T7-C3). :meth:`MultiFactory.gate` makes ``read_report`` of ONE controller
  block on a ``threading.Event`` so another thread really holds its lock (carry-forward T8-C2).
- :class:`RegistryStore` is a :class:`RecordingStore` that also records ``replace_all`` and can be
  told to fail it (carry-forward T11-E/T11-V).
- :class:`RegRig` wires a :class:`ControllerRegistry` to these with virtual time; :meth:`restart`
  builds a new registry over the same factory and store (simulated service restart).

The tests directory has no ``__init__.py`` (design Risk R8).
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from inheco_tec_api.controller import ControllerManager
from inheco_tec_api.manifest import Manifest
from inheco_tec_api.registry import ControllerRegistry
from inheco_tec_api.state_store import StateStoreError
from inheco_tec_api.transport.base import HidTransport
from inheco_tec_api.transport.fake import FakeTecDevice
from tec_controller_helpers import (
    LifecycleFactory,
    LockCheckingTransport,
    RecordingStore,
    lock_held_by_me,
)
from tec_engine_helpers import RecordingTransport, VirtualTime
from tec_manifest_helpers import base_manifest

IDS = (0, 1)
STOP_SWEEP = ["1ATE0", "0RDC1", "2ATE0", "0RDC1"]


def registry_manifest(
    tmp_path: Path,
    *,
    ids: Iterable[int] = IDS,
    lock_wait_s: int = 16,
    pre_send_delay_ms: int = 0,
    shutdown_timeout_s: int = 10,
) -> Manifest:
    """A valid fake-kind manifest: each controller in ``ids`` has CPAC slots 1 and 2."""
    data = base_manifest()
    data["transport"].update(
        kind="fake",
        pre_send_delay_ms=pre_send_delay_ms,
        retry_delay_ms=0,
        lock_wait_s=lock_wait_s,
        max_attempts=3,
    )
    data["service"].update(
        state_file=str(tmp_path / "state.json"), shutdown_timeout_s=shutdown_timeout_s
    )
    slots = data["controllers"][0]["slots"]
    data["controllers"] = [{"id": cid, "slots": copy.deepcopy(slots)} for cid in ids]
    return Manifest.model_validate(data)


class GatedTransport(LockCheckingTransport):
    """A lock-checking wrapper whose reads block only while its controller is gated."""

    def __init__(self, device: FakeTecDevice, factory: MultiFactory) -> None:
        super().__init__(device, factory)
        self._multi = factory

    def read_report(self, timeout_ms: int) -> bytes | None:
        cid = self.device.controller_id
        gate = self._multi.gates.get(cid)
        if gate is not None:
            self._multi.in_read.set()
            if not gate.wait(10):
                raise AssertionError("read gate was never opened")
        return RecordingTransport.read_report(self, timeout_ms)


class MultiFactory(LifecycleFactory):
    """A ``TransportFactory`` with one persistent fake device per controller."""

    def __init__(self, vt: VirtualTime, ids: Iterable[int] = IDS) -> None:
        self.devs = {cid: FakeTecDevice(cid, clock=vt.clock) for cid in ids}
        super().__init__(next(iter(self.devs.values())))
        self.registry: ControllerRegistry | None = None
        self.io_log: list[tuple[int, str]] = []
        self.gates: dict[int, threading.Event] = {}

    def note(self, what: str, transport: LockCheckingTransport) -> None:
        cid = transport.device.controller_id
        self.events.append(what)
        self.io_log.append((cid, what))
        if self.registry is not None and not lock_held_by_me(self.registry.managers[cid]):
            self.unlocked_io.append(f"{what}{cid}")

    def create(self, controller_id: int) -> HidTransport:
        self.events.append("create")
        self.io_log.append((controller_id, "create"))
        if self.create_error is not None:
            error, self.create_error = self.create_error, None
            raise error
        transport = GatedTransport(self.devs[controller_id], self)
        self.transports.append(transport)
        return transport

    def io_of(self, cid: int) -> list[str]:
        """The create/open/close events of one controller, in order."""
        return [what for c, what in self.io_log if c == cid]


class RegistryStore(RecordingStore):
    """A recording store that also records (and can fail) ``replace_all``."""

    def __init__(self, path: Path, events: list[str] | None = None) -> None:
        super().__init__(path, events)
        self.replace_calls: list[tuple[int, ...]] = []
        self.fail_replace = False

    def replace_all(self, cids: Iterable[int]) -> None:
        released = tuple(sorted(cids))
        self.replace_calls.append(released)
        self.events.append("store_replace_all")
        if self.fail_replace:
            raise StateStoreError("simulated write failure")
        super().replace_all(released)


class RegRig:
    """One registry over a :class:`MultiFactory`, a :class:`RegistryStore` and virtual time."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        ids: Iterable[int] = IDS,
        factory: MultiFactory | None = None,
        store: RegistryStore | None = None,
        vt: VirtualTime | None = None,
        **manifest_kw: Any,
    ) -> None:
        ids = tuple(ids)
        self.tmp_path = tmp_path
        self.ids = ids
        self.manifest_kw = manifest_kw
        self.vt = vt if vt is not None else VirtualTime()
        self.factory = factory if factory is not None else MultiFactory(self.vt, ids)
        self.store = (
            store
            if store is not None
            else RegistryStore(tmp_path / "state.json", self.factory.events)
        )
        self.manifest = registry_manifest(tmp_path, ids=ids, **manifest_kw)
        self.registry = ControllerRegistry(
            self.manifest, self.factory, self.store, sleep=self.vt.sleep, clock=self.vt.clock
        )
        self.factory.registry = self.registry

    def started(self) -> RegRig:
        """Run ``startup()`` and forget the startup writes and events."""
        self.registry.startup()
        self.mark()
        return self

    def mark(self) -> None:
        """Forget writes and events so far; later assertions see only new activity."""
        for dev in self.factory.devs.values():
            dev.writes.clear()
        self.factory.events.clear()
        self.factory.io_log.clear()

    def restart(self) -> RegRig:
        """A new registry (not started) over the same factory, store and time."""
        return RegRig(
            self.tmp_path,
            ids=self.ids,
            factory=self.factory,
            store=self.store,
            vt=self.vt,
            **self.manifest_kw,
        )

    def manager(self, cid: int) -> ControllerManager:
        return self.registry.managers[cid]

    def dev(self, cid: int) -> FakeTecDevice:
        return self.factory.devs[cid]

    def commands(self, cid: int) -> list[str]:
        return self.factory.devs[cid].commands

    def release(self, cid: int) -> None:
        """Release a controller the way the operations layer will (carry-forward T14-E)."""
        self.registry.ensure_release_state_persisted()
        self.registry.device_manager(cid).release()

    @contextmanager
    def held(self, cid: int) -> Iterator[None]:
        """Another thread holds ``cid``'s lock, blocked in a read of a stop on slot 1."""
        gate = threading.Event()
        self.factory.gates[cid] = gate
        self.factory.in_read.clear()
        errors: list[BaseException] = []

        def stop() -> None:
            try:
                self.manager(cid).stop_slots([1])
            except BaseException as exc:  # pragma: no cover - surfaced by the assert below
                errors.append(exc)

        worker = threading.Thread(target=stop)
        worker.start()
        try:
            assert self.factory.in_read.wait(5)
            yield
        finally:
            gate.set()
            worker.join(10)
            del self.factory.gates[cid]
        assert not worker.is_alive()
        assert errors == []

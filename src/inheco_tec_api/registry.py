"""Controller registry: startup, status, stop-all and graceful shutdown (design C9, C13, S1, S7).

:class:`ControllerRegistry` owns one :class:`~.controller.ControllerManager` per configured
controller, the ``TransportFactory``, the ONE :class:`~.state_store.ReleaseStateStore` of the
service (shared with every manager, carry-forward T11-C2) and the ``stopping`` flag.

- :meth:`ControllerRegistry.startup` loads the persisted released set and starts every manager
  exactly once (carry-forward T12-C1): a released controller is not opened (FR-12.4); the others
  start as found (FR-6.1, FR-6.7, FR-6.8). A corrupt state file makes every configured controller
  released (DD-11), and that set is written back in ONE atomic write before any manager starts
  (carry-forward T11-E/T11-V). A manager whose startup raises is logged and left not connected;
  the service still starts (FR-6.1, carry-forward T12-E). A fake factory logs
  ``transport_fake_active`` at warning level (FR-13.4).
- :meth:`ControllerRegistry.status` returns the raw FR-6.6 data per controller and slot without
  any I/O.
- :meth:`ControllerRegistry.stop_all` sends ``<slot>ATE0`` to every configured slot of every
  ``connected`` controller, then of every ``released`` controller through the transient open of
  FR-12.6; never ``ASE`` or any other command; every slot is attempted; one outcome per slot
  (FR-8.6, design C11 outcome set, P6).
- :meth:`ControllerRegistry.shutdown` sets ``stopping`` and sends ``<slot>ATE0`` to every configured
  slot of every ``connected`` controller within the deadline, then closes its handle; ``released``,
  ``lost`` and ``disconnected`` controllers get no command (FR-11.1, FR-11.2). A handle whose lock
  was not acquired is never closed (carry-forward T7-C3).
- While ``stopping`` is set, :meth:`ControllerRegistry.device_manager` and
  :meth:`ControllerRegistry.stop_all` raise :class:`ServiceStopping` (HTTP 503
  ``service_stopping``).

Like ``controller_safety.py``, the shutdown path uses the manager's internals (its lock, engine and
handle) because it needs a lock wait bounded by the shutdown deadline instead of ``lock_wait_s``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from inheco_tec_api.controller import (
    ControllerBusy,
    ControllerManager,
    ControllerNotConnected,
    SlotStopOutcome,
    StopNotDelivered,
)
from inheco_tec_api.controller_startup import EVENT_TEC_SLOT_STOP, stop_slots_on
from inheco_tec_api.logging_setup import EVENT_TRANSPORT_FAKE_ACTIVE, get_logger
from inheco_tec_api.manifest import Manifest
from inheco_tec_api.registry_status import (
    ControllerStatus,
    DetectedDeviceType,
    EffectiveLimits,
    SlotStatus,
    controller_status,
)
from inheco_tec_api.state_store import ReleaseStateStore, StateStoreError
from inheco_tec_api.transport.base import TransportFactory

__all__ = [
    "EVENT_TEC_CONTROLLER_STARTUP_FAILED",
    "ControllerRegistry",
    "ControllerStatus",
    "DetectedDeviceType",
    "EffectiveLimits",
    "ServiceStopping",
    "ShutdownSlotOutcome",
    "SlotStatus",
    "StopAllOutcome",
    "StopAllSlotOutcome",
]

StopAllOutcome = Literal["stopped", "failed", "not_delivered", "skipped"]
ShutdownOutcome = Literal["stopped", "failed", "not_attempted"]

EVENT_TEC_CONTROLLER_STARTUP_FAILED: Final = "tec_controller_startup_failed"
"""Error event of a manager startup that raised (FR-6.1). It is not one of the C14 events; it
carries ``controller_id``, ``state`` and ``error`` (the exception type name only)."""

_STOP_ALL: Final = "stop_all"
_SHUTDOWN: Final = "shutdown_stop"

_log = get_logger(__name__)


class ServiceStopping(Exception):
    """A device request arrived while the service is stopping (HTTP 503 ``service_stopping``)."""

    def __init__(self) -> None:
        super().__init__("the service is stopping")


@dataclass(frozen=True)
class StopAllSlotOutcome:
    """Outcome of one slot in :meth:`ControllerRegistry.stop_all` (FR-8.6, design C11).

    ``failure`` names the cause of a ``failed`` slot (failure kind, firmware code,
    ``device_lost``, ``not_attempted``, ``controller_busy``), or the controller state of a
    ``skipped`` slot; ``None`` for ``stopped`` and ``not_delivered``.
    """

    controller_id: int
    slot_id: int
    outcome: StopAllOutcome
    failure: str | None = None


@dataclass(frozen=True)
class ShutdownSlotOutcome:
    """Outcome of one slot of a ``connected`` controller at shutdown (FR-11.1).

    ``not_attempted`` carries ``shutdown_deadline`` or ``controller_busy`` (lock not acquired in
    time) or ``device_lost`` (an earlier slot lost the device) as ``failure``.
    """

    controller_id: int
    slot_id: int
    outcome: ShutdownOutcome
    failure: str | None = None


class ControllerRegistry:
    """All controller managers of the service (design C9 ``ControllerRegistry``).

    ``state_store`` defaults to one store at ``manifest.service.state_file``; either way exactly
    one store exists and every manager shares it (T11-C2). ``transport_factory`` is chosen by the
    caller from the manifest transport kind (C2); the registry never selects the fake itself
    (carry-forward T8-S). ``sleep`` and ``clock`` go to every manager; ``clock`` also measures the
    shutdown deadline.
    """

    def __init__(
        self,
        manifest: Manifest,
        transport_factory: TransportFactory,
        state_store: ReleaseStateStore | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manifest = manifest
        self._factory = transport_factory
        self._store = (
            state_store
            if state_store is not None
            else ReleaseStateStore(manifest.service.state_file)
        )
        self._clock = clock
        self._stopping = threading.Event()
        self._started = False
        self._start_lock = threading.Lock()
        self._release_state_pending: frozenset[int] = frozenset()
        self._managers: dict[int, ControllerManager] = {
            c.id: ControllerManager(
                c.id, manifest, transport_factory, self._store, sleep=sleep, clock=clock
            )
            for c in manifest.controllers
        }

    # -- accessors ---------------------------------------------------------------------------

    @property
    def managers(self) -> Mapping[int, ControllerManager]:
        """Read-only view of the managers by ControllerID."""
        return MappingProxyType(self._managers)

    @property
    def state_store(self) -> ReleaseStateStore:
        """The one shared release state store (T11-C2)."""
        return self._store

    @property
    def stopping(self) -> bool:
        """Whether :meth:`shutdown` has started; device requests are then refused."""
        return self._stopping.is_set()

    @property
    def release_state_persisted(self) -> bool:
        """``False`` while the DD-11 full released set from a corrupt load is not yet on disk."""
        return not self._release_state_pending

    def check_accepting(self) -> None:
        """Raise :class:`ServiceStopping` while the service is stopping."""
        if self._stopping.is_set():
            raise ServiceStopping()

    def device_manager(self, cid: int) -> ControllerManager:
        """The manager for a device request; :class:`ServiceStopping` while stopping.

        Raises ``KeyError`` for an unconfigured ID (the caller validates IDs first, FR-6.5).
        """
        self.check_accepting()
        return self._managers[cid]

    def ensure_release_state_persisted(self) -> None:
        """Write the pending DD-11 released set now, if a startup write failed.

        Call before any release or reacquire: while the corrupt file is still on disk, a single
        ``add``/``remove`` would replace it with a set that drops the other controllers
        (carry-forward T11-E). Raises :class:`StateStoreError` when the write fails again, so the
        caller refuses the operation.
        """
        pending = self._release_state_pending
        if not pending:
            return
        self._store.replace_all(pending)
        self._release_state_pending = frozenset()

    # -- startup (S1) ------------------------------------------------------------------------

    def startup(self) -> None:
        """Load the released set and start every manager once (FR-6.1, FR-12.4, DD-11).

        Raises ``RuntimeError`` on a second call: a second manager startup would leak a handle
        (T12-C1).
        """
        with self._start_lock:
            if self._started:
                raise RuntimeError("controller registry already started")
            self._started = True
        if self._factory.is_fake:
            _log.warning(
                EVENT_TRANSPORT_FAKE_ACTIVE,
                controllers=",".join(str(cid) for cid in self._managers),
            )
        released = self._load_released()
        for cid, manager in self._managers.items():
            self._start_manager(manager, cid in released)

    def _load_released(self) -> set[int]:
        """The released set to start with; a corrupt file → every configured controller."""
        result = self._store.load()
        if not result.corrupt:
            return set(result.released)
        released = set(self._managers)
        try:
            self._store.replace_all(released)
        except StateStoreError:
            # Fail closed: every controller still starts released and nothing is opened. Release
            # and reacquire are refused until ensure_release_state_persisted() succeeds.
            self._release_state_pending = frozenset(released)
        return released

    def _start_manager(self, manager: ControllerManager, initially_released: bool) -> None:
        """``manager.startup`` exactly once; any exception is logged, never raised (FR-6.1)."""
        try:
            manager.startup(initially_released)
        except Exception as exc:  # noqa: BLE001 — T12-E: the service must still start
            self._recover_failed_startup(manager, initially_released)
            _log.error(
                EVENT_TEC_CONTROLLER_STARTUP_FAILED,
                controller_id=manager.cid,
                state=manager.state,
                error=type(exc).__name__,
            )

    @staticmethod
    def _recover_failed_startup(manager: ControllerManager, initially_released: bool) -> None:
        """Leave a failed manager in a safe state: no stray handle, released kept released."""
        if not manager._lock.acquire(timeout=manager._manifest.transport.lock_wait_s):
            return  # never touch a handle without its lock (T7-C3)
        try:
            if initially_released:
                if manager.state != "released":
                    manager._detach()
                    manager._set_state("released", "startup")
            elif manager.state != "connected":
                manager._detach()
        finally:
            manager._lock.release()

    # -- status (FR-6.6) ---------------------------------------------------------------------

    def status(self) -> list[ControllerStatus]:
        """FR-6.6 data per controller and slot; no I/O and no lock (``registry_status.py``).

        ``emergency_off_active`` is reported as held, also on a ``lost`` controller (UA-1).
        """
        return [controller_status(manager) for manager in self._managers.values()]

    # -- stop-all (FR-8.6) -------------------------------------------------------------------

    def stop_all(self) -> list[StopAllSlotOutcome]:
        """``ATE0`` to every configured slot: ``connected`` controllers first, then the rest.

        Every slot gets one outcome; a failed stop is never reported as ``stopped`` (T13-S3).
        Raises :class:`ServiceStopping` while stopping.
        """
        self.check_accepting()
        first = [m for m in self._managers.values() if m.state == "connected"]
        rest = [m for m in self._managers.values() if m not in first]
        outcomes: list[StopAllSlotOutcome] = []
        for manager in first + rest:
            outcomes.extend(_stop_all_on(manager))
        return outcomes

    # -- shutdown (FR-11.1, FR-11.2, C13, S7) ------------------------------------------------

    def shutdown(self, deadline: float | None = None) -> list[ShutdownSlotOutcome]:
        """Set ``stopping``; ``ATE0`` every slot of every ``connected`` controller; close handles.

        ``deadline`` is a ``clock`` value; ``None`` means now plus ``service.shutdown_timeout_s``.
        Slots not attempted before the deadline are logged at error level. Never raises for a
        device failure; returns the per-slot outcomes of the ``connected`` controllers.
        """
        self._stopping.set()
        if deadline is None:
            deadline = self._clock() + self._manifest.service.shutdown_timeout_s
        outcomes: list[ShutdownSlotOutcome] = []
        for manager in self._managers.values():
            outcomes.extend(self._shutdown_one(manager, deadline))
        return outcomes

    def _shutdown_one(
        self, manager: ControllerManager, deadline: float
    ) -> list[ShutdownSlotOutcome]:
        slot_ids = list(manager.slots)
        with self._lock_until(manager, deadline) as acquired:
            if not acquired:
                if manager.state == "connected":
                    return _not_attempted(manager.cid, slot_ids, "controller_busy")
                return []
            if manager.state != "connected":
                return []  # released / lost / disconnected: no command (FR-11.2)
            outcomes = self._shutdown_stop_slots(manager, slot_ids, deadline)
            if manager.state == "connected":
                manager._detach()
                manager._set_state("disconnected", _SHUTDOWN)
            return outcomes

    def _shutdown_stop_slots(
        self, manager: ControllerManager, slot_ids: list[int], deadline: float
    ) -> list[ShutdownSlotOutcome]:
        """``ATE0`` slot by slot until the deadline; the caller holds the lock (T7-C3)."""
        cid = manager.cid
        outcomes: list[ShutdownSlotOutcome] = []
        for index, slot in enumerate(slot_ids):
            engine = manager._engine
            if self._clock() >= deadline or engine is None:
                outcomes.extend(_not_attempted(cid, slot_ids[index:], "shutdown_deadline"))
                break
            (result,) = stop_slots_on(engine, cid, [slot], _SHUTDOWN)
            outcomes.append(ShutdownSlotOutcome(cid, slot, result.outcome, result.failure))
            if result.failure == "device_lost":
                outcomes.extend(_not_attempted(cid, slot_ids[index + 1 :], "device_lost"))
                # C9 loss handling; the FR-11.3 best-effort stop only while time remains.
                manager._enter_lost(best_effort_stop=self._clock() < deadline)
                break
        return outcomes

    @contextmanager
    def _lock_until(self, manager: ControllerManager, deadline: float) -> Iterator[bool]:
        """Try the manager's lock until ``deadline``; yield whether it is held; always release."""
        remaining = deadline - self._clock()
        if remaining > 0:
            acquired = manager._lock.acquire(timeout=remaining)
        else:
            acquired = manager._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                manager._lock.release()


# -- helpers ----------------------------------------------------------------------------------


def _stop_all_on(manager: ControllerManager) -> list[StopAllSlotOutcome]:
    """Stop every configured slot of one controller for stop-all; never raises."""
    cid = manager.cid
    slot_ids = list(manager.slots)
    try:
        with manager.locked():
            state = manager.state
            if state not in ("connected", "released"):
                return _skipped(cid, slot_ids, state)
            results = manager.stop_slots(slot_ids)
    except StopNotDelivered:
        # controller_safety already logged tec_stop_not_delivered at error level (FR-12.7).
        return [StopAllSlotOutcome(cid, slot, "not_delivered") for slot in slot_ids]
    except ControllerNotConnected as exc:
        return _skipped(cid, slot_ids, exc.state)
    except ControllerBusy:
        return _failed(cid, slot_ids, "controller_busy")
    except Exception as exc:  # noqa: BLE001 — every other controller must still be attempted
        return _failed(cid, slot_ids, type(exc).__name__)
    return [_map_stop(cid, r) for r in results]


def _map_stop(cid: int, result: SlotStopOutcome) -> StopAllSlotOutcome:
    """``stopped`` only for a confirmed stop; anything else is ``failed`` (T13-C, T13-S3)."""
    if result.outcome == "stopped":
        return StopAllSlotOutcome(cid, result.slot, "stopped")
    failure = result.failure or result.outcome
    return StopAllSlotOutcome(cid, result.slot, "failed", failure)


def _skipped(cid: int, slot_ids: list[int], state: str) -> list[StopAllSlotOutcome]:
    """``disconnected``/``lost``: no I/O; each slot logged at error level (P6)."""
    for slot in slot_ids:
        _log.error(
            EVENT_TEC_SLOT_STOP,
            controller_id=cid,
            slot=slot,
            purpose=_STOP_ALL,
            outcome="skipped",
            state=state,
        )
    return [StopAllSlotOutcome(cid, slot, "skipped", state) for slot in slot_ids]


def _failed(cid: int, slot_ids: list[int], failure: str) -> list[StopAllSlotOutcome]:
    """Slots that could not be attempted at all (busy lock, unexpected error); error log."""
    for slot in slot_ids:
        _log.error(
            EVENT_TEC_SLOT_STOP,
            controller_id=cid,
            slot=slot,
            purpose=_STOP_ALL,
            outcome="failed",
            failure=failure,
        )
    return [StopAllSlotOutcome(cid, slot, "failed", failure) for slot in slot_ids]


def _not_attempted(cid: int, slot_ids: list[int], failure: str) -> list[ShutdownSlotOutcome]:
    """Shutdown slots not attempted (deadline, busy lock, earlier loss); error log (FR-11.1)."""
    for slot in slot_ids:
        _log.error(
            EVENT_TEC_SLOT_STOP,
            controller_id=cid,
            slot=slot,
            purpose=_SHUTDOWN,
            outcome="not_attempted",
            failure=failure,
        )
    return [ShutdownSlotOutcome(cid, slot, "not_attempted", failure) for slot in slot_ids]

"""Controller lifecycle: states, lock, startup, connect, loss, release, gating (design C9, DD-1).

One :class:`ControllerManager` per configured controller owns the state (``disconnected``,
``connected``, ``released``, ``lost``; FR-6), the open transport and engine, the per-slot runtime
data, and one ``threading.RLock`` (DD-1). The lock serialises lifecycle transitions and
transactions together, so "release waits for the in-flight transaction" is plain lock acquisition
(FR-5.1, FR-12.1). :meth:`ControllerManager.locked` waits at most ``lock_wait_s`` and then raises
:class:`ControllerBusy` (Risk R5); the lock is always released in ``finally`` (NFR-7). Every open
and close of the handle happens while the lock is held: the transport is not thread-safe,
``close()`` included (carry-forward T7-C3).

The emergency-off flag is held here in memory only (FR-17.9). The safety paths (stop, the
transient open of a released controller, emergency-off and the FR-17.8 re-initialize run by
:meth:`connect` and :meth:`reacquire` while the flag is set) live in ``controller_safety.py``.
:meth:`run` repeats the emergency-off and slot-permission checks for ``STT`` and ``ATE1`` inside
the lock, so a flag set between :meth:`gate` and :meth:`run` never lets a control command through
(carry-forward T12-S1).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Final, Literal

from inheco_tec_api.controller_safety import (
    StopNotDelivered,
    reinitialize_connected,
    reinitialize_released,
)
from inheco_tec_api.controller_safety import emergency_off as _emergency_off
from inheco_tec_api.controller_safety import stop_slots as _stop_slots
from inheco_tec_api.controller_startup import (
    ConnectResult,
    ControllerBusy,
    ControllerError,
    ControllerLost,
    ControllerNotConnected,
    ControllerReleased,
    ControllerState,
    EmergencyOffActive,
    ReconnectStopFailed,
    SlotNotPermitted,
    SlotRuntime,
    SlotStopOutcome,
    best_effort_loss_stop,
    identify_slots,
    log_state_change,
    open_transport,
    read_startup_states,
    stop_slots_on,
)
from inheco_tec_api.engine import DeviceLost, TransactionEngine
from inheco_tec_api.identification import SlotIdentification, StartupSlotState
from inheco_tec_api.logging_setup import EVENT_TEC_SLOT_NOT_PERMITTED, get_logger
from inheco_tec_api.manifest import Manifest, SlotConfig
from inheco_tec_api.protocol.commands import Command
from inheco_tec_api.protocol.framing import Reply
from inheco_tec_api.state_store import ReleaseStateStore
from inheco_tec_api.transport.base import HidTransport, TransportFactory

__all__ = [
    "ConnectResult",
    "ControllerBusy",
    "ControllerError",
    "ControllerLost",
    "ControllerManager",
    "ControllerNotConnected",
    "ControllerReleased",
    "ControllerState",
    "EmergencyOffActive",
    "Operation",
    "ReconnectStopFailed",
    "SlotNotPermitted",
    "SlotRuntime",
    "SlotStopOutcome",
    "StopNotDelivered",
]

Operation = Literal["set_target", "start", "stop", "stop_all", "emergency_off", "read"]

_ALLOWED_WHILE_RELEASED: Final = frozenset({"stop", "stop_all", "emergency_off"})
_CONTROL_OPERATIONS: Final = frozenset({"set_target", "start"})

_log = get_logger(__name__)


class ControllerManager:
    """Lifecycle of one configured controller (design C9).

    ``state_store`` is the ONE shared :class:`ReleaseStateStore` of the service (carry-forward
    T11-C2); this class never creates one. ``sleep`` and ``clock`` are passed to every engine.
    """

    def __init__(
        self,
        cid: int,
        manifest: Manifest,
        transport_factory: TransportFactory,
        state_store: ReleaseStateStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        config = next((c for c in manifest.controllers if c.id == cid), None)
        if config is None:
            raise ValueError(f"controller {cid} is not configured")
        self.cid = cid
        self.state: ControllerState = "disconnected"
        self.emergency_off_active: bool = False  # never persisted (FR-17.9)
        self.slots: dict[int, SlotRuntime] = {s.id: SlotRuntime(s) for s in config.slots}
        self._manifest = manifest
        self._factory = transport_factory
        self._store = state_store
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.RLock()
        self._transport: HidTransport | None = None
        self._engine: TransactionEngine | None = None

    @property
    def transport_fake(self) -> bool:
        """Whether the transport factory is the fake (FR-13, FR-6.6)."""
        return bool(self._factory.is_fake)

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the controller lock; raise :class:`ControllerBusy` after ``lock_wait_s`` (R5)."""
        wait_s = self._manifest.transport.lock_wait_s
        if not self._lock.acquire(timeout=wait_s):
            raise ControllerBusy(self.cid, f"controller {self.cid} busy after {wait_s} s")
        try:
            yield
        finally:
            self._lock.release()

    # -- lifecycle ---------------------------------------------------------------------------

    def startup(self, initially_released: bool) -> None:
        """Start as found (sequence S1): released → no open (FR-12.4); else open and identify.

        Per slot ``identify_slot`` then ``read_startup_state``; no ``ATE``/``STT`` (FR-6.7,
        FR-6.8). An open failure gives ``disconnected`` (FR-6.1). A loss during startup gives
        ``lost`` without the best-effort ``ATE0``: FR-6.7 forbids changing a slot at startup.
        """
        with self.locked():
            if initially_released:
                self._set_state("released", "startup")
                return
            transport = open_transport(self._factory, self.cid, "startup")
            if transport is None:
                self._set_state("disconnected", "startup")
                return
            engine = self._attach(transport)
            idents: dict[int, SlotIdentification] = {}
            states: dict[int, StartupSlotState] = {}
            try:
                with self._closing_on_error():
                    for slot_cfg in self._slot_configs():
                        idents.update(
                            identify_slots(engine, [slot_cfg], self._manifest.article_numbers)
                        )
                        states.update(read_startup_states(engine, [slot_cfg.id]))
            except DeviceLost:
                self._enter_lost(best_effort_stop=False)
                return
            for sid, state in states.items():
                self.slots[sid].startup_state = state
            self._apply_identification(idents)
            self._set_state("connected", "startup")

    def connect(self) -> ConnectResult:
        """Connect operation (FR-6.2): ``disconnected``/``lost`` → open → … → ``connected``.

        Raises :class:`ControllerNotConnected` when the open fails (state unchanged),
        :class:`ReconnectStopFailed` when an ``ATE0`` after loss fails (FR-11.4), and
        :class:`ControllerReleased` for a released controller. ``connected`` with emergency-off
        active runs the FR-17.8 re-initialize (``controller_safety.reinitialize_connected``).
        """
        with self.locked():
            if self.state == "connected":
                if self.emergency_off_active:
                    return reinitialize_connected(self)
                return ConnectResult("connected")
            if self.state == "released":
                raise ControllerReleased(self.cid)
            if self.state == "disconnected":
                self._connect_from_disconnected()
                return ConnectResult("connected")
            return self._reconnect_after_loss()

    def release(self) -> ControllerState:
        """Release for VENUS (FR-12.1): wait for the lock, persist, close; idempotent (FR-12.5).

        The ID is persisted BEFORE the handle is closed: a ``StateStoreError`` from the store then
        propagates with the controller still ``connected`` and its handle open (carry-forward
        T11-C2), and a crash in between leaves it persisted as released (safe, DD-11).
        """
        with self.locked():
            if self.state == "released":
                return self.state
            if self.state != "connected":
                raise ControllerNotConnected(self.cid, self.state)
            self._store.add(self.cid)
            self._detach()
            self._set_state("released", "release")
            return self.state

    def reacquire(self) -> ControllerState:
        """Reacquire (FR-12.3): open → identify → unpersist → ``connected``; no ``ATE0``.

        ``connected`` is a no-op (FR-12.5). Any failure before the unpersist (open failure, loss
        during identification, ``StateStoreError`` from the store) closes the handle, keeps the
        controller ``released`` with its persisted entry and earlier slot data, and raises
        (:class:`ControllerNotConnected` with state ``released``, or the ``StateStoreError``).
        With emergency-off active, the FR-17.8 re-initialize runs instead (``ATE0`` every slot
        before the identification; ``controller_safety.reinitialize_released``).
        """
        with self.locked():
            if self.state == "connected":
                return self.state
            if self.state != "released":
                raise ControllerNotConnected(self.cid, self.state)
            if self.emergency_off_active:
                reinitialize_released(self)
                return self.state
            transport = open_transport(self._factory, self.cid, "reacquire")
            if transport is None:
                raise ControllerNotConnected(self.cid, "released", "open failed")
            engine = self._attach(transport)
            try:
                with self._closing_on_error():
                    idents = identify_slots(
                        engine, self._slot_configs(), self._manifest.article_numbers
                    )
                    self._store.remove(self.cid)
            except DeviceLost as exc:
                raise ControllerNotConnected(self.cid, "released", "device lost") from exc
            self._apply_identification(idents)
            self._set_state("connected", "reacquire")
            return self.state

    # -- safety paths (controller_safety.py) -------------------------------------------------

    def stop_slots(self, slot_ids: Iterable[int]) -> list[SlotStopOutcome]:
        """``<slot>ATE0`` per slot; transient open while released (FR-8.5, FR-12.6, FR-12.7)."""
        with self.locked():
            return _stop_slots(self, slot_ids)

    def emergency_off(self) -> None:
        """``0AEO``; code ``0`` sets ``emergency_off_active`` (FR-17.4, FR-17.6, FR-17.7)."""
        with self.locked():
            _emergency_off(self)

    # -- commands ----------------------------------------------------------------------------

    def gate(self, operation: Operation, slot: int | None = None) -> None:
        """C9 gating steps 2–5, in order, before any I/O (step 1, the IDs, is the caller's).

        Callers hold :meth:`locked` around ``gate`` and :meth:`run` for an atomic check;
        :meth:`run` re-checks the state, emergency-off and slot permission anyway.
        """
        if self.state == "released":
            if operation not in _ALLOWED_WHILE_RELEASED:
                raise ControllerReleased(self.cid)
            return
        if self.state != "connected":
            raise ControllerNotConnected(self.cid, self.state)
        if operation not in _CONTROL_OPERATIONS:
            return
        if self.emergency_off_active:
            raise EmergencyOffActive(self.cid)
        if slot is None or slot not in self.slots:
            raise ValueError(f"operation {operation} needs a configured slot")
        runtime = self.slots[slot]
        if not runtime.permitted:
            reason = runtime.not_permitted_reason or "unknown"
            _log.warning(
                EVENT_TEC_SLOT_NOT_PERMITTED, controller_id=self.cid, slot=slot, reason=reason
            )
            raise SlotNotPermitted(self.cid, slot, reason)

    def run(self, cmd: Command, *, max_attempts: int | None = None) -> Reply:
        """Execute ``cmd`` on the connected controller under the lock (FR-5.1).

        On ``DeviceLost``: close, one best-effort reopen and single-attempt ``ATE0`` per slot,
        close, ``lost`` (FR-6.4, FR-11.3), then :class:`ControllerLost`. ``TransactionFailed``
        propagates. ``max_attempts`` must stay within 1..11 (carry-forward T9-S2). ``STT`` and
        ``ATE1`` are refused here too, without I/O, while emergency-off is active or the slot is
        not permitted (C9 gating steps 4-5 repeated inside the lock, carry-forward T12-S1).
        """
        with self.locked():
            if self.state == "released":
                raise ControllerReleased(self.cid)
            if self.state != "connected" or self._engine is None:
                raise ControllerNotConnected(self.cid, self.state)
            self._recheck_control(cmd)
            try:
                return self._engine.execute(cmd, max_attempts=max_attempts)
            except DeviceLost as exc:
                self._enter_lost(best_effort_stop=True)
                raise ControllerLost(self.cid) from exc

    # -- internals (the caller holds the lock) -----------------------------------------------

    def _recheck_control(self, cmd: Command) -> None:
        """Refuse ``STT``/``ATE1`` while emergency-off is active or the slot is not permitted."""
        if not (cmd.mnemonic == "STT" or (cmd.mnemonic == "ATE" and cmd.param == "1")):
            return
        if self.emergency_off_active:
            raise EmergencyOffActive(self.cid)
        runtime = self.slots.get(cmd.address)
        if runtime is None or not runtime.permitted:
            reason = runtime.not_permitted_reason if runtime is not None else None
            raise SlotNotPermitted(self.cid, cmd.address, reason or "slot_not_configured")

    def _connect_from_disconnected(self) -> None:
        transport = open_transport(self._factory, self.cid, "connect")
        if transport is None:
            raise ControllerNotConnected(self.cid, "disconnected", "open failed")
        engine = self._attach(transport)
        try:
            with self._closing_on_error():
                idents = identify_slots(
                    engine, self._slot_configs(), self._manifest.article_numbers
                )
        except DeviceLost as exc:
            self._enter_lost(best_effort_stop=True)
            raise ControllerLost(self.cid) from exc
        self._apply_identification(idents)
        self._set_state("connected", "connect")

    def _reconnect_after_loss(self) -> ConnectResult:
        """FR-11.4: ``ATE0`` to every configured slot before identification and any command."""
        transport = open_transport(self._factory, self.cid, "reconnect")
        if transport is None:
            raise ControllerNotConnected(self.cid, "lost", "open failed")
        engine = self._attach(transport)
        slot_ids = list(self.slots)
        try:
            with self._closing_on_error():
                outcomes = stop_slots_on(engine, self.cid, slot_ids, "reconnect_stop")
                if any(o.outcome != "stopped" for o in outcomes):
                    raise ReconnectStopFailed(self.cid, outcomes)
                idents = identify_slots(
                    engine, self._slot_configs(), self._manifest.article_numbers
                )
        except DeviceLost as exc:
            raise ControllerNotConnected(self.cid, "lost", "device lost") from exc
        self._apply_identification(idents)
        self._set_state("connected", "connect")
        return ConnectResult("connected", stopped_slots=slot_ids)

    def _enter_lost(self, *, best_effort_stop: bool) -> None:
        """Close, discard identification (T9-C1, T10-C), optional FR-11.3 stop, then ``lost``."""
        self._detach()
        for runtime in self.slots.values():
            runtime.identification = None
        if best_effort_stop:
            best_effort_loss_stop(self._factory, self.cid, list(self.slots), self._new_engine)
        self._set_state("lost", "loss")

    @contextmanager
    def _closing_on_error(self) -> Iterator[None]:
        """Close the handle when the block raises: a half-done transition never keeps it open."""
        try:
            yield
        except BaseException:
            self._detach()
            raise

    def _new_engine(self, transport: HidTransport) -> TransactionEngine:
        return TransactionEngine(
            transport, self.cid, self._manifest.transport, sleep=self._sleep, clock=self._clock
        )

    def _attach(self, transport: HidTransport) -> TransactionEngine:
        self._transport = transport
        self._engine = self._new_engine(transport)
        return self._engine

    def _detach(self) -> None:
        if self._transport is not None:
            self._transport.close()
        self._transport = None
        self._engine = None

    def _slot_configs(self) -> list[SlotConfig]:
        return [runtime.config for runtime in self.slots.values()]

    def _apply_identification(self, idents: dict[int, SlotIdentification]) -> None:
        for sid, ident in idents.items():
            self.slots[sid].identification = ident

    def _set_state(self, to_state: ControllerState, reason: str) -> None:
        from_state, self.state = self.state, to_state
        log_state_change(self.cid, from_state, to_state, reason)

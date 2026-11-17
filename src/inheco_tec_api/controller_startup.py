"""Helpers of the controller lifecycle: open, identification, stop sweeps, logging (design C9).

:class:`~.controller.ControllerManager` owns the state machine and the controller lock; this module
holds the steps it composes, so ``controller.py`` stays small (tasks.md 12.7):

- :func:`open_transport`: create and open the HID transport; ANY open failure (``DeviceNotFound``,
  ``DeviceOpenError``, and the raw ``ImportError``/``OSError`` a failed native-library load can
  raise, carry-forward T7-C2) is an open failure, logged with the exception type name only.
- :func:`identify_slots` and :func:`read_startup_states`: run C8 identification (FR-7) and the
  startup slot-state reads (FR-6.8) for every configured slot. Both only read; neither sends
  ``STT`` or ``ATE`` (FR-6.7). A ``DeviceLost`` propagates before any result is returned, so the
  caller discards that call's reads (carry-forward T10-C).
- :func:`stop_slots_on`: send ``<slot>ATE0`` to each slot and record a per-slot outcome (FR-11.3,
  FR-11.4).
- :func:`best_effort_loss_stop`: the FR-11.3 stop after a loss.
- :func:`log_state_change`: the ``tec_state_change`` event (FR-15.2, design C14).
- The lifecycle and gating exceptions and :class:`ConnectResult`; ``controller.py`` re-exports
  them, so callers import them from there.

Every function here does HID I/O through a transport or engine that the caller opened, and the
caller holds the controller lock for the whole call (FR-5.1; carry-forward T7-C3: the transport is
not thread-safe, ``close()`` included).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from inheco_tec_api.engine import DeviceLost, TransactionEngine, TransactionFailed
from inheco_tec_api.identification import (
    SlotIdentification,
    StartupSlotState,
    identify_slot,
    read_startup_state,
)
from inheco_tec_api.logging_setup import (
    EVENT_TEC_SLOT_NOT_PERMITTED,
    EVENT_TEC_STARTUP_SLOT_STATE,
    EVENT_TEC_STATE_CHANGE,
    get_logger,
)
from inheco_tec_api.manifest import ArticleEntry, SlotConfig
from inheco_tec_api.protocol import commands
from inheco_tec_api.transport.base import HidTransport, TransportFactory

__all__ = [
    "EVENT_TEC_OPEN_FAILED",
    "ConnectResult",
    "ControllerBusy",
    "ControllerError",
    "ControllerLost",
    "ControllerNotConnected",
    "ControllerReleased",
    "ControllerState",
    "EmergencyOffActive",
    "ReconnectStopFailed",
    "SlotNotPermitted",
    "best_effort_loss_stop",
    "EVENT_TEC_SLOT_STOP",
    "REASON_NOT_IDENTIFIED",
    "SlotRuntime",
    "SlotStopOutcome",
    "StopOutcome",
    "identify_slots",
    "log_state_change",
    "open_transport",
    "read_startup_states",
    "stop_slots_on",
]

StopOutcome = Literal["stopped", "failed", "not_attempted"]

REASON_NOT_IDENTIFIED: Final = "slot_not_identified"
"""Not-permitted reason of a slot without a current identification (FR-7.5 fail closed): never
identified, or its identification was discarded after a loss (carry-forward T9-C1, T10-C)."""

EVENT_TEC_OPEN_FAILED: Final = "tec_open_failed"
"""Warning event of a failed HID open. It is not one of the C14 events in
``logging_setup.EVENT_NAMES``; it carries ``controller_id``, ``purpose`` and ``error`` (the
exception type name only, never a message, which could name a device path, FR-3.7)."""

EVENT_TEC_SLOT_STOP: Final = "tec_slot_stop"
"""Event of one ``ATE0`` sent by a lifecycle sweep (FR-11.3 loss, FR-11.4 reconnect). It is not
one of the C14 events; it carries ``controller_id``, ``slot``, ``purpose``, ``outcome`` and, on a
failure, ``failure`` (failure kind or firmware code). Error level unless ``outcome`` is
``stopped``."""

_log = get_logger(__name__)


ControllerState = Literal["disconnected", "connected", "released", "lost"]


@dataclass
class SlotRuntime:
    """In-memory runtime data of one configured slot (design C9 / data model).

    ``identification`` holds the limits, detected type, article and variant of the last complete
    identification (``None`` before one, or after a loss discarded it). ``startup_state`` is the
    state read at service startup (FR-6.8), ``None`` if it was not read.
    """

    config: SlotConfig
    identification: SlotIdentification | None = None
    startup_state: StartupSlotState | None = None

    @property
    def permitted(self) -> bool:
        """Whether set target and start are permitted (FR-7.5); ``False`` when unidentified."""
        return self.identification is not None and self.identification.limits.permitted

    @property
    def not_permitted_reason(self) -> str | None:
        """The fail-closed reason (FR-7.5), or ``None`` when the slot is permitted."""
        if self.identification is None:
            return REASON_NOT_IDENTIFIED
        return self.identification.limits.reason


@dataclass(frozen=True)
class SlotStopOutcome:
    """Outcome of one ``ATE0`` in a lifecycle sweep (FR-11.3, FR-11.4).

    ``failure`` is the failure kind (``no_reply``/``crc``/``parse``/``device_lost``) or the
    firmware error character, or ``open_failed`` when the device could not be opened; ``None``
    when ``outcome`` is ``stopped``, or ``not_attempted`` after an earlier ``device_lost``.
    """

    slot: int
    outcome: StopOutcome
    failure: str | None = None


def log_state_change(controller_id: int, from_state: str, to_state: str, reason: str) -> None:
    """Log ``tec_state_change`` with the C14 fields (FR-15.2)."""
    _log.info(
        EVENT_TEC_STATE_CHANGE,
        controller_id=controller_id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
    )


def open_transport(
    factory: TransportFactory, controller_id: int, purpose: str
) -> HidTransport | None:
    """Create and open the controller's transport; ``None`` (after a warning) when it fails.

    Any exception from ``create`` or ``open`` is an open failure (carry-forward T7-C2). The
    transport is closed again after a failed open (``close`` is idempotent and never raises). The
    caller holds the controller lock (T7-C3).
    """
    transport: HidTransport | None = None
    try:
        transport = factory.create(controller_id)
        transport.open()
    except Exception as exc:  # noqa: BLE001 — T7-C2: any open failure, never a crash
        if transport is not None:
            transport.close()
        _log.warning(
            EVENT_TEC_OPEN_FAILED,
            controller_id=controller_id,
            purpose=purpose,
            error=type(exc).__name__,
        )
        return None
    return transport


def identify_slots(
    engine: TransactionEngine,
    slot_cfgs: Iterable[SlotConfig],
    article_map: Mapping[str, ArticleEntry],
) -> dict[int, SlotIdentification]:
    """Identify every configured slot (design C8; FR-7) and log each fail-closed reason (FR-7.5).

    Returns the identifications only when every slot completed; a ``DeviceLost`` propagates and the
    reads made so far are discarded (carry-forward T10-C). ``article`` is raw device data and is
    never put into a log message (T10-S2); it is not logged here at all.
    """
    result: dict[int, SlotIdentification] = {}
    for slot_cfg in slot_cfgs:
        ident = identify_slot(engine, slot_cfg, article_map)
        if not ident.limits.permitted:
            _log.warning(
                EVENT_TEC_SLOT_NOT_PERMITTED,
                controller_id=engine.controller_id,
                slot=slot_cfg.id,
                reason=ident.limits.reason,
            )
        result[slot_cfg.id] = ident
    return result


def read_startup_states(
    engine: TransactionEngine, slot_ids: Iterable[int]
) -> dict[int, StartupSlotState]:
    """Read and log the startup state of every slot (FR-6.8): ``RHE``, ``RTT``, ``RAT3`` only.

    No ``ATE`` or ``STT`` is sent (FR-6.7). A ``DeviceLost`` propagates (T10-C).
    """
    result: dict[int, StartupSlotState] = {}
    for slot in slot_ids:
        state = read_startup_state(engine, slot)
        _log.info(
            EVENT_TEC_STARTUP_SLOT_STATE,
            controller_id=engine.controller_id,
            slot=slot,
            control_state=state.control_state,
            target_c=state.target_c,
            actual_c=state.actual_c,
        )
        result[slot] = state
    return result


def stop_slots_on(
    engine: TransactionEngine | None,
    controller_id: int,
    slot_ids: Iterable[int],
    purpose: str,
    *,
    max_attempts: int | None = None,
) -> list[SlotStopOutcome]:
    """Send ``<slot>ATE0`` to each slot in order and return one outcome per slot.

    ``engine`` is ``None`` when the device could not be opened: every slot is then
    ``not_attempted`` with failure ``open_failed``. Every slot is attempted after a
    ``TransactionFailed`` (stop as many slots as possible). After a ``DeviceLost`` the handle is
    dead, so the remaining slots are ``not_attempted``. Each outcome is logged as
    ``tec_slot_stop``. ``max_attempts`` must stay within 1..11 (carry-forward T9-S2).
    """
    outcomes: list[SlotStopOutcome] = []
    lost = False
    for slot in slot_ids:
        if engine is None:
            outcome = SlotStopOutcome(slot, "not_attempted", "open_failed")
        elif lost:
            outcome = SlotStopOutcome(slot, "not_attempted")
        else:
            try:
                engine.execute(commands.ate(slot, False), max_attempts=max_attempts)
                outcome = SlotStopOutcome(slot, "stopped")
            except TransactionFailed as exc:
                failure = exc.code if exc.kind == "firmware" and exc.code else exc.kind
                outcome = SlotStopOutcome(slot, "failed", failure)
            except DeviceLost:
                lost = True
                outcome = SlotStopOutcome(slot, "failed", "device_lost")
        log = _log.info if outcome.outcome == "stopped" else _log.error
        fields: dict[str, object] = {"outcome": outcome.outcome}
        if outcome.failure is not None:
            fields["failure"] = outcome.failure
        log(
            EVENT_TEC_SLOT_STOP,
            controller_id=controller_id,
            slot=slot,
            purpose=purpose,
            **fields,
        )
        outcomes.append(outcome)
    return outcomes


def best_effort_loss_stop(
    factory: TransportFactory,
    controller_id: int,
    slot_ids: list[int],
    new_engine: Callable[[HidTransport], TransactionEngine],
) -> None:
    """FR-11.3: one reopen, one single-attempt ``ATE0`` per slot, close; never raises.

    The caller holds the controller lock (T7-C3) and has already closed the lost handle. Every
    outcome is logged; an unexpected exception is logged too, so the caller still marks the
    controller ``lost``.
    """
    transport = open_transport(factory, controller_id, "loss_stop")
    try:
        engine = new_engine(transport) if transport is not None else None
        stop_slots_on(engine, controller_id, slot_ids, "loss_stop", max_attempts=1)
    except Exception as exc:  # noqa: BLE001 — best effort: the state must still become lost
        _log.error(
            EVENT_TEC_SLOT_STOP,
            controller_id=controller_id,
            purpose="loss_stop",
            outcome="failed",
            failure=type(exc).__name__,
        )
    finally:
        if transport is not None:
            transport.close()


class ControllerError(Exception):
    """Base of the lifecycle and gating errors; ``controller_id`` names the controller."""

    def __init__(self, controller_id: int, message: str) -> None:
        super().__init__(message)
        self.controller_id = controller_id


class ControllerBusy(ControllerError):
    """The controller lock was not acquired within ``lock_wait_s`` (HTTP 503, Risk R5)."""


class ControllerNotConnected(ControllerError):
    """The controller cannot be talked to now; ``state`` names its state (FR-6.3, HTTP 503)."""

    def __init__(self, controller_id: int, state: ControllerState, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(controller_id, f"controller {controller_id} is {state}{suffix}")
        self.state: ControllerState = state


class ControllerLost(ControllerNotConnected):
    """The device was lost during a command; the controller is now ``lost`` (FR-6.4).

    The command may already have been applied, so its slot state is unknown (carry-forward T9-C1).
    """

    def __init__(self, controller_id: int) -> None:
        super().__init__(controller_id, "lost", "device lost during the command, outcome unknown")


class ControllerReleased(ControllerError):
    """A command other than stop/stop-all/emergency-off for a released controller (FR-12.2)."""

    def __init__(self, controller_id: int) -> None:
        super().__init__(controller_id, f"controller {controller_id} is released")


class EmergencyOffActive(ControllerError):
    """The operation is refused while emergency-off is active (FR-17.7)."""

    def __init__(self, controller_id: int) -> None:
        super().__init__(controller_id, f"emergency-off is active on controller {controller_id}")


class SlotNotPermitted(ControllerError):
    """Set target or start on a slot that failed closed (FR-7.5); ``reason`` says why."""

    def __init__(self, controller_id: int, slot: int, reason: str) -> None:
        super().__init__(controller_id, f"slot {slot} is not permitted: {reason}")
        self.slot = slot
        self.reason = reason


class ReconnectStopFailed(ControllerError):
    """A reconnect or re-initialize could not stop every slot (FR-11.4, FR-17.8).

    After a loss the controller stays ``lost``. On the FR-17.8 re-initialize it stays ``connected``
    (handle kept) or ``released`` with emergency-off still active, or it becomes ``lost`` when the
    device was lost during the sweep. ``outcomes`` holds the per-slot result (HTTP 502 with the
    per-slot outcome, design C9). ``reinitialize`` is ``True`` only when raised by the FR-17.8
    re-initialize (emergency-off active, ``stop_slots_on`` purpose ``reinitialize_stop``), ``False``
    for the FR-11.4 reconnect after a loss (FR-17.11, DD-16).
    """

    def __init__(
        self, controller_id: int, outcomes: list[SlotStopOutcome], *, reinitialize: bool = False
    ) -> None:
        super().__init__(controller_id, f"controller {controller_id}: stop on reconnect failed")
        self.outcomes = outcomes
        self.reinitialize = reinitialize


@dataclass(frozen=True)
class ConnectResult:
    """Result of :meth:`ControllerManager.connect` (design C11 ``connect``)."""

    state: ControllerState
    stopped_slots: list[int] = field(default_factory=list)
    emergency_off_cleared: bool = False

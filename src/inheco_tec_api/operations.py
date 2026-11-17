"""Domain operations: :class:`TecService` (design C11; FR-8, FR-11.4, FR-12, FR-17).

``TecService`` composes the C9 gating, the C7 transactions and the C8 identification into typed
results. It has no HTTP knowledge and no server-side wait: every operation returns after its own
transaction(s) (FR-8, NFR-3). The read operations, the shared request checks and the locked device
access live in ``operations_read.py`` (:class:`~.operations_read.ReadOperations`, tasks.md 15.5);
this module adds the status, lifecycle, control and safety operations.

Rules every operation here follows (carry-forward constraints of Tasks 9–14):

- IDs are checked before anything else, a bool ID included (FR-6.5; T12-E, T12-S1); then the
  manager comes from ``registry.device_manager`` and ``manager.locked()`` is held around the gate
  and the transaction; ``registry.check_accepting()`` runs again inside the lock (T14-S4).
- ``registry.ensure_release_state_persisted()`` runs before every release and reacquire and its
  ``StateStoreError`` propagates, so the operation is refused instead of writing a released set
  that drops other controllers after a corrupt load (DD-11; T14-E, T14-S3).
- A failed safety result is never reported as success (T13-C, T13-S3): a stop outcome other than
  ``stopped`` raises :class:`StopFailed`; a device loss during emergency-off raises
  :class:`EmergencyOffOutcomeUnknown`. After a loss or any failed command the slot state is
  unknown and no result says "not applied" (T9-C1, T9-C6).
- ``max_attempts`` is never passed (T9-S2). Log events carry flat fields only, never raw device
  data in a message (T10-S2, T2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from inheco_tec_api.controller import (
    ControllerError,
    ControllerLost,
    ControllerNotConnected,
    ControllerState,
    StopNotDelivered,
)
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.identification import SlotIdentification
from inheco_tec_api.logging_setup import (
    EVENT_TEC_CONDENSATION_WARNING,
    EVENT_TEC_EMERGENCY_OFF,
    get_logger,
)
from inheco_tec_api.operations_read import (
    ActualTemperature,
    AtTargetResult,
    DeviceTypeResult,
    FirmwareVersion,
    InvalidRequest,
    NotConfigured,
    ReadOperations,
    ReplyDataInvalid,
    SensorValues,
    TargetTemperature,
    is_number,
)
from inheco_tec_api.protocol import commands
from inheco_tec_api.protocol.encoding import encode_tenths
from inheco_tec_api.registry import StopAllSlotOutcome
from inheco_tec_api.registry_status import (
    ControllerStatus,
    EffectiveLimits,
    SlotStatus,
    controller_status,
)

__all__ = [
    "CONDENSATION_THRESHOLD_C",
    "ActualTemperature",
    "AtTargetResult",
    "ConnectOutcome",
    "DeviceTypeResult",
    "EmergencyOffOutcomeUnknown",
    "EmergencyOffResult",
    "FirmwareVersion",
    "InvalidRequest",
    "LifecycleResult",
    "NotConfigured",
    "OperationWarning",
    "ReacquireResult",
    "ReplyDataInvalid",
    "SensorValues",
    "SetTargetResult",
    "SlotCommandResult",
    "StopAllResult",
    "StopFailed",
    "TargetTemperature",
    "TecService",
]

CONDENSATION_THRESHOLD_C: Final = 12.0
"""A target below this value adds a ``condensation_risk`` warning (FR-8.3)."""

StopAllStatus = Literal["ok", "not_delivered", "failed"]

_log = get_logger(__name__)


class StopFailed(ControllerError):
    """A stop was attempted but not confirmed for ``slot_id`` (FR-8.5, FR-9; T13-S3).

    ``failure`` is the stop outcome's cause: a firmware error character (``kind`` ``firmware``,
    ``code`` set; it can be outside the §8.2 table, T9-V2), a transport kind (``no_reply``,
    ``crc``, ``parse``), ``device_lost`` or ``not_attempted``. The slot state is unknown: the
    ``ATE0`` may have been applied (T9-C1, T9-C6). Never a success.
    """

    def __init__(self, controller_id: int, slot_id: int, failure: str) -> None:
        super().__init__(
            controller_id,
            f"stop was not confirmed on controller {controller_id} slot {slot_id} ({failure});"
            " the slot state is unknown",
        )
        self.slot_id = slot_id
        self.failure = failure
        transport_kinds = ("no_reply", "crc", "parse", "device_lost", "not_attempted")
        self.kind = failure if failure in transport_kinds else "firmware"
        self.code: str | None = None if failure in transport_kinds else failure


class EmergencyOffOutcomeUnknown(ControllerError):
    """The device was lost while ``0AEO`` was in flight (FR-17.6; T13-S3).

    The emergency-off may or may not have reached the device, so the outcome is unknown; it is
    neither "delivered" nor "not sent". The emergency-off flag was not set. ``state`` is the
    controller state afterwards (``released`` or ``lost``).
    """

    def __init__(self, controller_id: int, state: ControllerState) -> None:
        super().__init__(
            controller_id,
            f"emergency-off outcome unknown on controller {controller_id}: the device was lost"
            " during the command",
        )
        self.state: ControllerState = state


@dataclass(frozen=True)
class OperationWarning:
    """A non-blocking warning in a result, e.g. ``condensation_risk`` (FR-8.3)."""

    kind: str
    message: str


@dataclass(frozen=True)
class ConnectOutcome:
    """Result of ``connect`` (FR-6.2, FR-11.4, FR-17.8)."""

    controller_id: int
    state: ControllerState
    stopped_slots: list[int]
    emergency_off_cleared: bool
    slots: list[SlotStatus]


@dataclass(frozen=True)
class SetTargetResult:
    """Result of ``set_target`` (FR-8.1, FR-8.3); ``target_c`` is decoded from the sent tenths."""

    controller_id: int
    slot_id: int
    target_c: float
    warnings: list[OperationWarning] = field(default_factory=list)


@dataclass(frozen=True)
class SlotCommandResult:
    """Result of ``start`` and ``stop`` (FR-8.4, FR-8.5): the command was confirmed."""

    controller_id: int
    slot_id: int


@dataclass(frozen=True)
class StopAllResult:
    """Result of ``stop_all`` (FR-8.6). ``status`` follows the C11 rule: ``not_delivered`` if any
    slot is ``not_delivered``, else ``failed`` if any slot is ``failed``, else ``ok`` (skipped
    slots do not change it)."""

    outcomes: list[StopAllSlotOutcome]
    status: StopAllStatus


@dataclass(frozen=True)
class LifecycleResult:
    """Result of ``release`` (FR-12.1, FR-12.5)."""

    controller_id: int
    state: ControllerState


@dataclass(frozen=True)
class ReacquireResult:
    """Result of ``reacquire`` (FR-12.3, FR-12.5, FR-17.8).

    ``emergency_off_cleared`` is ``True`` when the FR-17.8 re-initialize ran and cleared the flag;
    ``stopped_slots`` then lists the slots that got ``ATE0`` (T13-C1).
    """

    controller_id: int
    state: ControllerState
    stopped_slots: list[int]
    emergency_off_cleared: bool


@dataclass(frozen=True)
class EmergencyOffResult:
    """Result of ``emergency_off`` (FR-17.1, FR-17.7): ``0AEO`` completed with code ``0``."""

    controller_id: int
    emergency_off_active: bool


class TecService(ReadOperations):
    """The C11 domain operations over a :class:`~.registry.ControllerRegistry`."""

    # -- status and lifecycle ----------------------------------------------------------------

    def status(self) -> list[ControllerStatus]:
        """FR-6.6 status of every controller and slot; no I/O, no lock (FR-13.4, FR-17.7)."""
        return self._registry.status()

    def connect(self, cid: int) -> ConnectOutcome:
        """Connect or re-initialize (FR-6.2, FR-11.4, FR-17.8; C9 table).

        The manager enforces the state rules (``released`` → ``ControllerReleased``); the gate is
        not used because connect is the operation that leaves ``disconnected``/``lost``.
        """
        cid = self._check_controller(cid)
        with self._device(cid) as manager:
            result = manager.connect()
            slots = controller_status(manager).slots
        return ConnectOutcome(
            cid, result.state, list(result.stopped_slots), result.emergency_off_cleared, slots
        )

    def release(self, cid: int) -> LifecycleResult:
        """Release for VENUS (FR-12.1, FR-12.5); refused on ``StateStoreError`` (T14-S3)."""
        cid = self._check_controller(cid)
        with self._device(cid) as manager:
            self._registry.ensure_release_state_persisted()
            state = manager.release()
        return LifecycleResult(cid, state)

    def reacquire(self, cid: int) -> ReacquireResult:
        """Reacquire (FR-12.3, FR-12.5) or the FR-17.8 re-initialize while emergency-off is active.

        The flag is read under the lock before the call, so the result can report the clear
        (T13-C1). ``ensure_release_state_persisted`` runs first (T14-E, T14-S3).
        """
        cid = self._check_controller(cid)
        with self._device(cid) as manager:
            self._registry.ensure_release_state_persisted()
            eo_before = manager.emergency_off_active
            state = manager.reacquire()
            cleared = eo_before and not manager.emergency_off_active
            stopped = list(manager.slots) if cleared else []
        return ReacquireResult(cid, state, stopped, cleared)

    # -- control -----------------------------------------------------------------------------

    def set_target(self, cid: int, sid: int, t: float) -> SetTargetResult:
        """``<sid>STT<tenths>`` for a target inside the effective limits (FR-8.1–FR-8.3).

        After the gate (released, not connected, emergency-off, not permitted → refused), a
        non-finite ``t`` or ``encode_tenths(t) / 10`` outside the effective window raises
        :class:`InvalidRequest` carrying the limits, with no I/O (FR-8.2). The bounds are compared
        in integer tenths. ``t < 12.0`` adds a ``condensation_risk`` warning and logs
        ``tec_condensation_warning`` at warning level; it never blocks (FR-8.3).
        """
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("set_target", sid)
            tenths = self._target_tenths(manager.slots[sid].identification, cid, sid, t)
            manager.run(commands.stt(sid, tenths))
        target_c = tenths / 10
        warnings: list[OperationWarning] = []
        if t < CONDENSATION_THRESHOLD_C:
            warnings.append(
                OperationWarning("condensation_risk", "target below 12.0 C may condense")
            )
            _log.warning(
                EVENT_TEC_CONDENSATION_WARNING, controller_id=cid, slot=sid, target_c=target_c
            )
        return SetTargetResult(cid, sid, target_c, warnings)

    def start(self, cid: int, sid: int) -> SlotCommandResult:
        """``<sid>ATE1`` on a permitted slot (FR-8.4)."""
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("start", sid)
            manager.run(commands.ate(sid, True))
        return SlotCommandResult(cid, sid)

    def stop(self, cid: int, sid: int) -> SlotCommandResult:
        """``<sid>ATE0`` whatever the slot's permitted state (FR-8.5); transient open if released.

        A released controller that cannot be opened raises ``StopNotDelivered`` (FR-12.6,
        FR-12.7). Any outcome other than ``stopped`` raises :class:`StopFailed` (T13-C, T13-S3).
        """
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("stop", sid)
            outcomes = manager.stop_slots([sid])
        for outcome in outcomes:
            if outcome.outcome != "stopped":
                raise StopFailed(cid, outcome.slot, outcome.failure or outcome.outcome)
        return SlotCommandResult(cid, sid)

    def stop_all(self) -> StopAllResult:
        """``ATE0`` to every configured slot of every controller, never ``ASE`` (FR-8.6).

        ``registry.stop_all`` raises ``ServiceStopping`` while the service stops.
        """
        outcomes = self._registry.stop_all()
        kinds = {o.outcome for o in outcomes}
        status: StopAllStatus = "ok"
        if "not_delivered" in kinds:
            status = "not_delivered"
        elif "failed" in kinds:
            status = "failed"
        return StopAllResult(outcomes, status)

    # -- emergency-off -----------------------------------------------------------------------

    def emergency_off(self, cid: int, confirm: object) -> EmergencyOffResult:
        """``0AEO`` (FR-17.1); transient open if released (FR-17.4).

        ``confirm`` must be exactly ``True``, else :class:`InvalidRequest` with no I/O (FR-17.2).
        The request and its outcome are logged as ``tec_emergency_off`` at warning level; an
        outcome where ``0AEO`` was not sent or its delivery is unknown is logged at error level
        (FR-17.5). A failure leaves the flag unchanged and propagates (FR-17.6).
        """
        cid = self._check_controller(cid)
        _log.warning(EVENT_TEC_EMERGENCY_OFF, controller_id=cid, phase="request")
        if confirm is not True:
            _log_eo_outcome(cid, "rejected_unconfirmed", error=False)
            raise InvalidRequest("emergency-off needs confirm=true", controller_id=cid)
        try:
            with self._device(cid) as manager:
                manager.gate("emergency_off")
                try:
                    manager.emergency_off()
                except ControllerLost as exc:
                    raise EmergencyOffOutcomeUnknown(cid, manager.state) from exc
                except ControllerNotConnected as exc:
                    if exc.state != "released":
                        raise
                    # Transient open of a released controller lost the device mid-command.
                    raise EmergencyOffOutcomeUnknown(cid, "released") from exc
        except StopNotDelivered:
            _log_eo_outcome(cid, "not_delivered", error=True)
            raise
        except EmergencyOffOutcomeUnknown:
            _log_eo_outcome(cid, "outcome_unknown", error=True)
            raise
        except TransactionFailed as exc:
            _log_eo_outcome(cid, "failed", error=False, failure=exc.code or exc.kind)
            raise
        except Exception as exc:
            # Refused before any I/O (busy, stopping, not connected): 0AEO was not sent.
            _log_eo_outcome(cid, "not_sent", error=True, failure=type(exc).__name__)
            raise
        _log_eo_outcome(cid, "emergency_off_active", error=False)
        return EmergencyOffResult(cid, emergency_off_active=True)

    # -- internals ---------------------------------------------------------------------------

    @staticmethod
    def _target_tenths(ident: SlotIdentification | None, cid: int, sid: int, t: object) -> int:
        """C9 step 6 for set target: the encoded target inside the effective window (FR-8.2)."""
        min_c = ident.limits.min_c if ident is not None else None
        max_c = ident.limits.max_c if ident is not None else None
        if min_c is None or max_c is None:
            # The gate passed, so the slot is permitted; fail closed if the window is missing.
            raise InvalidRequest("the slot has no effective limits", controller_id=cid, slot_id=sid)
        window = EffectiveLimits(min_c, max_c)
        try:
            if not is_number(t):
                raise ValueError("temperature must be a number")
            tenths = encode_tenths(t)
        except ValueError as exc:
            raise InvalidRequest(
                "temperature_c must be a finite number",
                controller_id=cid,
                slot_id=sid,
                effective_limits=window,
            ) from exc
        if not encode_tenths(min_c) <= tenths <= encode_tenths(max_c):
            raise InvalidRequest(
                f"temperature_c {tenths / 10} is outside the effective limits"
                f" {min_c}-{max_c} C",
                controller_id=cid,
                slot_id=sid,
                effective_limits=window,
            )
        return tenths


def _log_eo_outcome(cid: int, outcome: str, *, error: bool, failure: str | None = None) -> None:
    """``tec_emergency_off`` outcome line: warning, or error when not delivered (FR-17.5)."""
    log = _log.error if error else _log.warning
    fields: dict[str, object] = {"controller_id": cid, "phase": "outcome", "outcome": outcome}
    if failure is not None:
        fields["failure"] = failure
    log(EVENT_TEC_EMERGENCY_OFF, **fields)

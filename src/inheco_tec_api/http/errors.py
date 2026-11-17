"""API error model: maps every domain exception to its :class:`ApiError` (design C12).

The error types and the fixed status table live in ``http/error_types.py`` and are re-exported
here. :func:`to_api_error` maps each domain exception to one ``kind`` of the C12 table; the
specific ``ControllerError`` subclasses (``StopFailed``, ``EmergencyOffOutcomeUnknown``,
``StopNotDelivered``, ``ReconnectStopFailed``, ...) are matched before any general fallback
(carry-forward T15-E). :func:`stop_all_error` applies the C11 stop-all status rule and
:func:`request_validation_error` renders a schema 422.

Messages are fixed or composed here from typed fields (ints, state literals, fixed reason
tokens); raw exception text is used only for the domain messages known to be built from such
fields, and only after :func:`~.error_types.safe_message` finds no host, IP, port, path, token or
header pattern in it (FR-3.7, T7-S1). A firmware code outside the §8.2 table gets the fallback
description (T9-V2). A failed safety result never reads as success, and an unknown safety outcome
never reads as "not connected" or "not sent" (T13-S3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from inheco_tec_api._vendor.instrument_api_core import Unauthorized
from inheco_tec_api.controller import (
    ControllerBusy,
    ControllerError,
    ControllerLost,
    ControllerNotConnected,
    ControllerReleased,
    EmergencyOffActive,
    ReconnectStopFailed,
    SlotNotPermitted,
    StopNotDelivered,
)
from inheco_tec_api.engine import DeviceLost, TransactionFailed
from inheco_tec_api.http.error_types import (
    STATUS_BY_KIND,
    UNKNOWN_FIRMWARE_CODE,
    ApiError,
    ControllerBusyError,
    ControllerNotConnectedError,
    ControllerReleasedError,
    EmergencyOffActiveError,
    ErrorKind,
    FirmwareError,
    InternalError,
    NotFoundError,
    ServiceStoppingError,
    SlotNotPermittedError,
    StopAllFailedError,
    StopNotDeliveredError,
    TransportError,
    UnauthorizedError,
    ValidationFailed,
    code_description,
    code_text,
    error_responses,
    safe_message,
)
from inheco_tec_api.http.schemas import SlotOutcomeBody, SlotOutcomeName
from inheco_tec_api.operations import (
    EmergencyOffOutcomeUnknown,
    InvalidRequest,
    NotConfigured,
    StopAllResult,
    StopFailed,
)
from inheco_tec_api.protocol import tables
from inheco_tec_api.registry import ServiceStopping
from inheco_tec_api.registry_status import EffectiveLimits
from inheco_tec_api.state_store import StateStoreError

__all__ = [
    "STATUS_BY_KIND",
    "UNKNOWN_FIRMWARE_CODE",
    "ApiError",
    "ControllerBusyError",
    "ControllerNotConnectedError",
    "ControllerReleasedError",
    "EmergencyOffActiveError",
    "ErrorKind",
    "FirmwareError",
    "InternalError",
    "NotFoundError",
    "ServiceStoppingError",
    "SlotNotPermittedError",
    "StopAllFailedError",
    "StopNotDeliveredError",
    "TransportError",
    "UnauthorizedError",
    "ValidationFailed",
    "code_description",
    "error_responses",
    "request_validation_error",
    "stop_all_error",
    "to_api_error",
]

_INTERNAL_MESSAGE: Final = "internal error"
_STATE_STORE_MESSAGE: Final = (
    "the release state could not be saved; the request was refused and nothing was changed"
)
_BUSY_RETRY_AFTER_S: Final = "1"
_POWER_CYCLE_STATEMENT: Final = (
    ". Firmware code I after emergency-off: release the controller if it is connected,"
    " power-cycle the TEC controller, then call Reacquire; emergency-off stays active."
)


# -- mapping -------------------------------------------------------------------------------------


def _limits(value: object) -> EffectiveLimits | None:
    return value if isinstance(value, EffectiveLimits) else None


def _slot_rows(
    cid: int, slots: Iterable[int], outcome: SlotOutcomeName, failure: str | None
) -> list[SlotOutcomeBody]:
    return [
        SlotOutcomeBody(controller_id=cid, slot_id=s, outcome=outcome, failure=failure)
        for s in slots
    ]


def _transaction_failed(exc: TransactionFailed, cid: int | None, sid: int | None) -> ApiError:
    """FR-9.1 / FR-9.2: firmware → 502 with code and §8.2 description; transport kinds → 504."""
    if exc.kind == "firmware":
        description = code_description(exc.code)
        return FirmwareError(
            f"firmware error {code_text(exc.code)}: {description}",
            code=exc.code,
            code_description=description,
            attempts=exc.attempts,
            controller_id=cid,
            slot_id=sid,
        )
    return TransportError(
        f"transport failure: {exc.kind} after {exc.attempts} attempt(s)",
        failure=exc.kind,
        attempts=exc.attempts,
        controller_id=cid,
        slot_id=sid,
    )


def _stop_failed(exc: StopFailed) -> ApiError:
    """A stop that was attempted but not confirmed: never a success (T13-S3, T15-E, T15-T2)."""
    cid, sid = exc.controller_id, exc.slot_id
    rows = _slot_rows(cid, [sid], "failed", exc.failure)
    unknown = " the slot state is unknown"
    if exc.kind == "firmware":
        description = code_description(exc.code)
        return FirmwareError(
            f"Stop was not confirmed: firmware error {code_text(exc.code)}: {description};"
            + unknown,
            code=exc.code,
            code_description=description,
            controller_id=cid,
            slot_id=sid,
            slots=rows,
        )
    if exc.kind in ("no_reply", "crc", "parse"):
        message = f"Stop was not confirmed: transport failure: {exc.kind};" + unknown
    else:  # device_lost / not_attempted: the ATE0 may or may not have been applied
        message = f"Stop outcome UNKNOWN: {exc.kind} during the stop;" + unknown
    return TransportError(message, failure=exc.kind, controller_id=cid, slot_id=sid, slots=rows)


def _stop_not_delivered(exc: StopNotDelivered) -> ApiError:
    """FR-12.7 / FR-17.4: nothing was sent; the controller stays released (HTTP 409)."""
    cid = exc.controller_id
    if exc.operation == "emergency_off":
        message = (
            f"Emergency-off was NOT delivered (controller {cid}): the device could not be opened"
        )
    else:
        listed = ", ".join(str(s) for s in exc.slots)
        message = (
            f"Stop was NOT delivered (controller {cid}, slot(s) {listed}):"
            " the device could not be opened"
        )
    return StopNotDeliveredError(
        message,
        failure="open_failed",
        controller_id=cid,
        slots=_slot_rows(cid, exc.slots, "not_delivered", "open_failed"),
    )


def _power_cycle_required(exc: ReconnectStopFailed) -> bool:
    """FR-17.11 / P10: a FR-17.8 re-initialize slot failed with firmware code ``I``."""
    return exc.reinitialize and any(
        o.outcome == "failed" and o.failure == tables.CODE_CPAC_VOLTAGE_NOT_OK for o in exc.outcomes
    )


def _reconnect_stop_failed(exc: ReconnectStopFailed) -> ApiError:
    """FR-11.4 / FR-17.8: the reconnect sweep did not stop every slot (502, per-slot outcome).

    FR-17.11 / P10: the power-cycle statement is appended to the unchanged base message only when
    :func:`_power_cycle_required` is true; kind, status code and ``slots`` stay unchanged. The
    message holds only constants and the integer ``cid`` (FR-3.7).
    """
    cid = exc.controller_id
    rows = [
        SlotOutcomeBody(controller_id=cid, slot_id=o.slot, outcome=o.outcome, failure=o.failure)
        for o in exc.outcomes
    ]
    message = (
        f"Stop on reconnect failed on controller {cid}: not every slot was confirmed stopped;"
        " the slot state is unknown"
    )
    if _power_cycle_required(exc):
        message += _POWER_CYCLE_STATEMENT
    return StopAllFailedError(message, controller_id=cid, slots=rows)


def _not_connected(exc: ControllerNotConnected) -> ApiError:
    """FR-6.3: the message names the state; a loss mid-command is "outcome unknown" (T9-C1)."""
    cid = exc.controller_id
    fallback = f"controller {cid} is {exc.state}"
    failure = "device_lost" if isinstance(exc, ControllerLost) else None
    return ControllerNotConnectedError(
        safe_message(str(exc), fallback), failure=failure, controller_id=cid
    )


def _controller_error(exc: ControllerError, sid: int | None) -> ApiError:
    """Map a ``ControllerError``; the specific subclasses are checked before the fallback."""
    cid = exc.controller_id
    if isinstance(exc, StopFailed):
        return _stop_failed(exc)
    if isinstance(exc, EmergencyOffOutcomeUnknown):
        # Not "not connected" and not "not sent": the 0AEO may or may not have arrived (T13-S3).
        return TransportError(
            f"Emergency-off outcome UNKNOWN on controller {cid}: the device was lost during the"
            f" command; emergency-off is not marked active and the controller is {exc.state}",
            failure="device_lost",
            controller_id=cid,
        )
    if isinstance(exc, StopNotDelivered):
        return _stop_not_delivered(exc)
    if isinstance(exc, ReconnectStopFailed):
        return _reconnect_stop_failed(exc)
    if isinstance(exc, ControllerBusy):
        return ControllerBusyError(
            f"controller {cid} is busy; retry the request",
            controller_id=cid,
            slot_id=sid,
            headers={"Retry-After": _BUSY_RETRY_AFTER_S},
        )
    if isinstance(exc, ControllerNotConnected):  # ControllerLost included
        return _not_connected(exc)
    if isinstance(exc, ControllerReleased):
        return ControllerReleasedError(
            f"controller {cid} is released; only stop, stop-all and emergency-off are allowed",
            controller_id=cid,
            slot_id=sid,
        )
    if isinstance(exc, EmergencyOffActive):
        return EmergencyOffActiveError(
            f"emergency-off is active on controller {cid}; set target and start are refused",
            controller_id=cid,
            slot_id=sid,
        )
    if isinstance(exc, SlotNotPermitted):
        reason = safe_message(exc.reason, "unknown")
        return SlotNotPermittedError(
            f"slot {exc.slot} is not permitted: {reason}",
            failure=reason,
            controller_id=cid,
            slot_id=exc.slot,
        )
    return InternalError(_INTERNAL_MESSAGE, controller_id=cid, slot_id=sid)


def to_api_error(
    exc: BaseException, *, controller_id: int | None = None, slot_id: int | None = None
) -> ApiError:
    """Map any exception to its :class:`ApiError` (C12 status table).

    ``controller_id`` / ``slot_id`` are the request's IDs; they fill the body when the exception
    does not carry its own. An unmapped exception becomes ``internal_error`` with a fixed message.
    """
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, Unauthorized):
        return UnauthorizedError(Unauthorized().args[0])
    if isinstance(exc, ServiceStopping):
        return ServiceStoppingError("the service is stopping", controller_id=controller_id)
    if isinstance(exc, InvalidRequest):
        cid = exc.controller_id if exc.controller_id is not None else controller_id
        return ValidationFailed(
            safe_message(str(exc), "invalid request"),
            controller_id=cid,
            slot_id=exc.slot_id if exc.slot_id is not None else slot_id,
            effective_limits=_limits(exc.effective_limits),
        )
    if isinstance(exc, NotConfigured):
        return NotFoundError(
            safe_message(str(exc), "controller or slot is not configured"),
            controller_id=exc.controller_id,
            slot_id=exc.slot_id,
        )
    if isinstance(exc, ControllerError):
        return _controller_error(exc, slot_id)
    if isinstance(exc, TransactionFailed):  # ReplyDataInvalid included (parse, 504)
        return _transaction_failed(exc, controller_id, slot_id)
    if isinstance(exc, DeviceLost):
        return TransportError(
            "device lost during the command; the outcome is unknown",
            failure="device_lost",
            controller_id=controller_id,
            slot_id=slot_id,
        )
    if isinstance(exc, StateStoreError):
        return InternalError(
            _STATE_STORE_MESSAGE, failure="state_store", controller_id=controller_id
        )
    return InternalError(_INTERNAL_MESSAGE, controller_id=controller_id, slot_id=slot_id)


def stop_all_error(result: StopAllResult) -> ApiError | None:
    """Status rule of stop-all (design C11; FR-8.6, FR-12.7): ``None`` means HTTP 200.

    Any ``not_delivered`` slot → 409 ``stop_not_delivered``; else any ``failed`` slot (or a
    non-``ok`` status) → 502 ``stop_all_failed``. ``slots`` lists every outcome. A failed or
    undelivered slot is never a success (T13-S3).
    """
    rows = [
        SlotOutcomeBody(
            controller_id=o.controller_id, slot_id=o.slot_id, outcome=o.outcome, failure=o.failure
        )
        for o in result.outcomes
    ]
    kinds = {o.outcome for o in result.outcomes}
    if "not_delivered" in kinds or result.status == "not_delivered":
        cids = sorted({o.controller_id for o in result.outcomes if o.outcome == "not_delivered"})
        listed = ", ".join(str(c) for c in cids)
        return StopNotDeliveredError(
            f"Stop was NOT delivered to every slot (controller(s) {listed}): see slots",
            slots=rows,
        )
    if "failed" in kinds or result.status != "ok":
        return StopAllFailedError(
            "stop-all failed on at least one slot; the state of those slots is unknown",
            slots=rows,
        )
    return None


def request_validation_error(errors: Sequence[Mapping[str, Any]]) -> ValidationFailed:
    """A schema 422 from FastAPI ``RequestValidationError.errors()`` (FR-6.5, FR-17.2).

    Only each error's location and type are used, never its ``input`` or ``msg``; a location that
    fails the leak check (it can name a client-sent field) gives a fixed message (FR-3.7).
    """
    parts = []
    for err in errors[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc} ({err.get('type', 'invalid')})")
    return ValidationFailed(safe_message("invalid request: " + "; ".join(parts), "invalid request"))

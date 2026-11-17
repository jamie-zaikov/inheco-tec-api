"""API error types: the :class:`ApiError` hierarchy and the fixed status table (design C12).

Every non-2xx response carries the unified body ``{"error": {kind, message, code,
code_description, failure, attempts, controller_id, slot_id, effective_limits, slots}}`` with the
fixed HTTP status of its ``kind`` (C12 status table, DD-10, FR-9.4):

======================== ==== ========================================================
kind                     HTTP when
======================== ==== ========================================================
unauthorized             401  missing/invalid token
not_found                404  controller/slot ID not configured
validation_error         422  bad ID, selector, tolerance, target or EO confirmation
controller_released      409  non-stop command to a released controller
emergency_off_active     409  set target / start while emergency-off is active
slot_not_permitted       409  set target / start on a not-permitted slot
stop_not_delivered       409  released controller could not be opened (FR-12.7)
firmware_error           502  a firmware error code (``code``, ``code_description``)
stop_all_failed          502  a stop sweep with a ``failed`` slot and none not delivered
controller_not_connected 503  ``disconnected`` / ``lost`` (the message names the state)
controller_busy          503  controller lock not acquired in ``lock_wait_s`` (retry)
service_stopping         503  request during shutdown
transport_error          504  no valid reply (``failure`` names the kind)
internal_error           500  unexpected exception, or the release state not saved
======================== ==== ========================================================

The domain-exception mapping lives in ``http/errors.py``, which re-exports these types; this module
is split from it to keep both under the module size limit (tech.md).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Final, Literal

from inheco_tec_api.http.schemas import (
    EffectiveLimitsBody,
    ErrorDetail,
    ErrorResponse,
    SlotOutcomeBody,
)
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS
from inheco_tec_api.registry_status import EffectiveLimits

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
    "code_text",
    "error_responses",
    "safe_message",
]

ErrorKind = Literal[
    "unauthorized",
    "not_found",
    "validation_error",
    "controller_released",
    "emergency_off_active",
    "slot_not_permitted",
    "stop_not_delivered",
    "firmware_error",
    "stop_all_failed",
    "controller_not_connected",
    "controller_busy",
    "service_stopping",
    "transport_error",
    "internal_error",
]

UNKNOWN_FIRMWARE_CODE: Final = "unknown firmware code"
"""Description of a firmware code outside the §8.2 table (carry-forward T9-V2)."""


# A host, IPv4 address, :port, path separator, USB/HID device path, or credential/header word.
_LEAK_PATTERN: Final = re.compile(
    r"\d{1,3}(?:\.\d{1,3}){3}"
    r"|:\d+"
    r"|[\\/]"
    r"|(?:usb|hid|vid_|pid_|host|bearer|token|secret|password|authori[sz]ation|cookie)",
    re.IGNORECASE,
)
_MAX_MESSAGE_LEN: Final = 300


def safe_message(text: str, fallback: str) -> str:
    """``text`` if it is short printable ASCII with no leak pattern, else ``fallback`` (FR-3.7)."""
    if (
        len(text) > _MAX_MESSAGE_LEN
        or not text.isascii()
        or not text.isprintable()
        or _LEAK_PATTERN.search(text)
    ):
        return fallback
    return text


def code_description(code: str | None) -> str:
    """§8.2 description of a firmware code, with a fallback for an unlisted code (T9-V2)."""
    if code is None:
        return UNKNOWN_FIRMWARE_CODE
    return FIRMWARE_ERRORS.get(code, UNKNOWN_FIRMWARE_CODE)


def code_text(code: str | None) -> str:
    """The code for a message: a listed §8.2 code, else a fixed word (raw device data, T10-S2)."""
    return code if code is not None and code in FIRMWARE_ERRORS else "(unlisted)"


# -- the ApiError hierarchy ----------------------------------------------------------------------


class ApiError(Exception):
    """An error rendered as the unified error body with the fixed status of its ``kind``.

    ``headers`` holds optional response headers (``Retry-After`` for ``controller_busy``).
    """

    kind: ClassVar[ErrorKind] = "internal_error"
    status_code: ClassVar[int] = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        code_description: str | None = None,
        failure: str | None = None,
        attempts: int | None = None,
        controller_id: int | None = None,
        slot_id: int | None = None,
        effective_limits: EffectiveLimits | None = None,
        slots: Sequence[SlotOutcomeBody] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.code_description = code_description
        self.failure = failure
        self.attempts = attempts
        self.controller_id = controller_id
        self.slot_id = slot_id
        self.effective_limits = effective_limits
        self.slots = list(slots) if slots is not None else None
        self.headers: dict[str, str] = dict(headers or {})

    def detail(self) -> ErrorDetail:
        """The ``error`` object of the unified body."""
        limits = self.effective_limits
        return ErrorDetail(
            kind=self.kind,
            message=self.message,
            code=self.code,
            code_description=self.code_description,
            failure=self.failure,
            attempts=self.attempts,
            controller_id=self.controller_id,
            slot_id=self.slot_id,
            effective_limits=(
                EffectiveLimitsBody(min_c=limits.min_c, max_c=limits.max_c) if limits else None
            ),
            slots=self.slots,
        )

    def body(self) -> dict[str, Any]:
        """The unified error body as JSON-ready data."""
        return ErrorResponse(error=self.detail()).model_dump(mode="json")


class UnauthorizedError(ApiError):
    """Missing or invalid bearer token (FR-3.5); the message is the fixed ``Unauthorized`` text."""

    kind = "unauthorized"
    status_code = 401


class NotFoundError(ApiError):
    """Controller or slot ID not configured (FR-6.5)."""

    kind = "not_found"
    status_code = 404


class ValidationFailed(ApiError):
    """Invalid request value (FR-6.5, FR-8.2, FR-8.7, FR-8.9, FR-8.10, FR-8.12, FR-17.2)."""

    kind = "validation_error"
    status_code = 422


class ControllerReleasedError(ApiError):
    """Non-stop command to a released controller (FR-12.2)."""

    kind = "controller_released"
    status_code = 409


class EmergencyOffActiveError(ApiError):
    """Set target / start while emergency-off is active (FR-17.7)."""

    kind = "emergency_off_active"
    status_code = 409


class SlotNotPermittedError(ApiError):
    """Set target / start on a not-permitted slot (FR-7.5)."""

    kind = "slot_not_permitted"
    status_code = 409


class StopNotDeliveredError(ApiError):
    """A released controller could not be opened for stop/stop-all/EO (FR-12.7, FR-17.4)."""

    kind = "stop_not_delivered"
    status_code = 409


class FirmwareError(ApiError):
    """A transaction failed with a firmware error code (FR-9.1)."""

    kind = "firmware_error"
    status_code = 502


class StopAllFailedError(ApiError):
    """A stop sweep with at least one ``failed`` slot and none not delivered (FR-8.6, FR-11.4)."""

    kind = "stop_all_failed"
    status_code = 502


class ControllerNotConnectedError(ApiError):
    """The controller is ``disconnected`` or ``lost`` (FR-6.3)."""

    kind = "controller_not_connected"
    status_code = 503


class ControllerBusyError(ApiError):
    """The controller lock was not acquired in ``lock_wait_s``; the request can be retried."""

    kind = "controller_busy"
    status_code = 503


class ServiceStoppingError(ApiError):
    """A request arrived while the service is stopping."""

    kind = "service_stopping"
    status_code = 503


class TransportError(ApiError):
    """No valid reply (FR-9.2); ``failure`` names the kind."""

    kind = "transport_error"
    status_code = 504


class InternalError(ApiError):
    """Unexpected exception (fixed message; the stack goes to the log only)."""

    kind = "internal_error"
    status_code = 500


_ERROR_CLASSES: Final[tuple[type[ApiError], ...]] = (
    UnauthorizedError,
    NotFoundError,
    ValidationFailed,
    ControllerReleasedError,
    EmergencyOffActiveError,
    SlotNotPermittedError,
    StopNotDeliveredError,
    FirmwareError,
    StopAllFailedError,
    ControllerNotConnectedError,
    ControllerBusyError,
    ServiceStoppingError,
    TransportError,
    InternalError,
)

STATUS_BY_KIND: Final[dict[str, int]] = {cls.kind: cls.status_code for cls in _ERROR_CLASSES}
"""The fixed C12 status table, ``kind`` → HTTP status (FR-9.4)."""


def error_responses(*kinds: ErrorKind) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` documenting the given error kinds by status (FR-9.4)."""
    by_status: dict[int, list[str]] = {}
    for kind in kinds:
        by_status.setdefault(STATUS_BY_KIND[kind], []).append(kind)
    return {
        status: {"model": ErrorResponse, "description": "error kind: " + ", ".join(names)}
        for status, names in sorted(by_status.items())
    }

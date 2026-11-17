"""Request and response models of the HTTP layer (design C12; FR-6.6, FR-8, FR-9, FR-10.2, FR-17).

Every response model mirrors one C11 result type and is built from it with :meth:`_Response.of`
(``from_attributes``), so the HTTP layer never re-derives a domain value. The unified error body
(:class:`ErrorResponse`) is rendered by ``http/errors.py``.

Rules these models follow:

- No model has a free-form command field: every request body is a fixed, closed shape
  (``extra="forbid"``) and no field carries a raw firmware string to send (FR-10.2).
- Raw device data (the article number, sensor and firmware reply data) appears only as a defined
  structured field, never inside a message string (carry-forward T10-S2).
- ``emergency_off_active`` is reported for every controller state, ``lost`` included (UA-1).
- ``temperature_c`` is a float (JSON integers are accepted, strings and booleans are not). NaN and
  infinity pass the schema so the set-target operation rejects them with the effective limits
  (FR-8.2). A finite value whose magnitude exceeds :data:`TEMPERATURE_ABS_BOUND_C` is rejected by
  the schema (HTTP 422), because ``encode_tenths`` cannot quantise values of 1e27 and above and
  would otherwise raise ``decimal.InvalidOperation`` (carry-forward T15-C1, T15-V).
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool

__all__ = [
    "TEMPERATURE_ABS_BOUND_C",
    "ActualTemperatureResponse",
    "AtTargetResponse",
    "ConnectResponse",
    "ControllerStatusBody",
    "DetectedDeviceTypeBody",
    "DeviceTypeResponse",
    "EffectiveLimitsBody",
    "EmergencyOffRequest",
    "EmergencyOffResponse",
    "ErrorDetail",
    "ErrorResponse",
    "FirmwareVersionResponse",
    "LifecycleResponse",
    "ReacquireResponse",
    "SensorValuesResponse",
    "SetTargetRequest",
    "SetTargetResponse",
    "SlotCommandResponse",
    "SlotOutcomeBody",
    "SlotStatusBody",
    "StartupStateBody",
    "StatusResponse",
    "StopAllResponse",
    "TargetTemperatureResponse",
    "WarningBody",
]

TEMPERATURE_ABS_BOUND_C: Final = 1000.0
"""Largest finite ``|temperature_c|`` the set-target schema accepts, in °C (T15-C1).

It lies far outside every effective window (FR-7), so a value between the slot limits and this
bound still reaches the operation and gets the FR-8.2 rejection with the effective limits; only an
absurd magnitude (e.g. 1e30) is rejected by the schema itself, as HTTP 422 instead of a 500."""

ControllerStateName = Literal["disconnected", "connected", "released", "lost"]
ControlStateName = Literal["heating", "cooling", "off", "unknown"]
SlotOutcomeName = Literal["stopped", "failed", "not_delivered", "skipped", "not_attempted"]


def _bounded_temperature(value: float) -> float:
    """Reject a finite value beyond :data:`TEMPERATURE_ABS_BOUND_C`; keep NaN/inf for FR-8.2."""
    if math.isfinite(value) and abs(value) > TEMPERATURE_ABS_BOUND_C:
        raise ValueError(
            f"temperature_c must lie within -{TEMPERATURE_ABS_BOUND_C}..{TEMPERATURE_ABS_BOUND_C}"
        )
    return value


# -- requests ------------------------------------------------------------------------------------


class _Request(BaseModel):
    """Closed request body: unknown fields are rejected (FR-10.2)."""

    model_config = ConfigDict(extra="forbid")


class SetTargetRequest(_Request):
    """``PUT .../slots/{sid}/target`` body ``{"temperature_c": <number>}`` (FR-8.1, FR-8.2)."""

    temperature_c: Annotated[
        float,
        Field(strict=True, allow_inf_nan=True),
        AfterValidator(_bounded_temperature),
    ]


class EmergencyOffRequest(_Request):
    """``POST .../emergency-off`` body ``{"confirm": true}`` (FR-17.1, FR-17.2).

    ``confirm`` is a required ``StrictBool``: a missing field or a non-boolean is a schema 422; a
    ``false`` reaches the operation, which rejects it with 422 and no I/O.
    """

    confirm: StrictBool


# -- shared response parts -----------------------------------------------------------------------


class _Response(BaseModel):
    """Response model built from a C11 result dataclass."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    @classmethod
    def of(cls, result: Any) -> Self:
        """Build the response from the domain result (attributes of the same names)."""
        return cls.model_validate(result, from_attributes=True)


class EffectiveLimitsBody(_Response):
    """Effective target window of a slot in °C (FR-7.4, FR-8.2)."""

    min_c: float
    max_c: float


class DetectedDeviceTypeBody(_Response):
    """``RTD`` code of a slot and its name (``None`` when not in the §5.3 enum)."""

    code: int
    name: str | None


class StartupStateBody(_Response):
    """Slot state read at service startup (FR-6.8)."""

    control_state: ControlStateName
    target_c: float | None
    actual_c: float | None


class SlotStatusBody(_Response):
    """FR-6.6 data of one configured slot. ``detected_article_number`` is raw device data."""

    slot_id: int
    configured_class: str
    detected_device_type: DetectedDeviceTypeBody | None
    detected_article_number: str | None
    variant: str | None
    effective_limits: EffectiveLimitsBody | None
    control_permitted: bool
    not_permitted_reason: str | None
    startup_state: StartupStateBody | None


class ControllerStatusBody(_Response):
    """FR-6.6 data of one controller; ``emergency_off_active`` for every state (UA-1)."""

    controller_id: int
    state: ControllerStateName
    transport_fake: bool
    emergency_off_active: bool
    slots: list[SlotStatusBody]


class WarningBody(_Response):
    """A non-blocking warning, e.g. ``condensation_risk`` (FR-8.3)."""

    kind: str
    message: str


class SlotOutcomeBody(_Response):
    """Per-slot outcome of a stop sweep (FR-8.6, FR-11.4, FR-12.7; design C11).

    ``outcome`` is one of the four C11 stop-all values, or ``not_attempted`` for a reconnect sweep
    slot after an earlier device loss. ``failure`` is the cause of a ``failed`` slot or the state of
    a ``skipped`` controller.
    """

    controller_id: int
    slot_id: int
    outcome: SlotOutcomeName
    failure: str | None = None


# -- responses -----------------------------------------------------------------------------------


class StatusResponse(BaseModel):
    """``GET /v1/status`` (FR-6.6, FR-13.4, FR-17.7)."""

    model_config = ConfigDict(extra="forbid")

    controllers: list[ControllerStatusBody]

    @classmethod
    def of(cls, statuses: list[Any]) -> Self:
        """Build from ``TecService.status()`` (a list of ``ControllerStatus``)."""
        return cls(controllers=[ControllerStatusBody.of(s) for s in statuses])


class ConnectResponse(_Response):
    """``POST .../connect`` (FR-6.2, FR-11.4, FR-17.8)."""

    controller_id: int
    state: ControllerStateName
    stopped_slots: list[int]
    emergency_off_cleared: bool
    slots: list[SlotStatusBody]


class LifecycleResponse(_Response):
    """``POST .../release`` (FR-12.1, FR-12.5)."""

    controller_id: int
    state: ControllerStateName


class ReacquireResponse(_Response):
    """``POST .../reacquire`` (FR-12.3, FR-12.5, FR-17.8)."""

    controller_id: int
    state: ControllerStateName
    stopped_slots: list[int]
    emergency_off_cleared: bool


class SetTargetResponse(_Response):
    """``PUT .../target`` -> 200 (FR-8.1, FR-8.3); ``target_c`` decoded from the sent tenths."""

    controller_id: int
    slot_id: int
    target_c: float
    warnings: list[WarningBody]


class SlotCommandResponse(_Response):
    """``POST .../start`` and ``.../stop`` -> 200: the command was confirmed (FR-8.4, FR-8.5)."""

    controller_id: int
    slot_id: int


class ActualTemperatureResponse(_Response):
    """``GET .../temperature`` (FR-8.7)."""

    controller_id: int
    slot_id: int
    temperature_c: float
    selector: int


class TargetTemperatureResponse(_Response):
    """``GET .../target`` (FR-8.8)."""

    controller_id: int
    slot_id: int
    temperature_c: float


class AtTargetResponse(_Response):
    """``GET .../at-target`` (FR-8.9): one instantaneous reading, selector 3."""

    controller_id: int
    slot_id: int
    at_target: bool
    actual_c: float
    target_c: float
    tolerance_c: float
    selector: int


class SensorValuesResponse(_Response):
    """``GET .../sensor-values`` (FR-8.10); ``data`` is the raw reply data."""

    controller_id: int
    selector: int
    data: str


class DeviceTypeResponse(_Response):
    """``GET .../device-type`` (FR-8.11); ``name`` is ``None`` for a code outside the table."""

    controller_id: int
    slot: int
    code: int
    name: str | None


class FirmwareVersionResponse(_Response):
    """``GET .../firmware-version`` (FR-8.12); ``data`` is the raw reply data."""

    controller_id: int
    selector: int
    name: str
    data: str


class StopAllResponse(_Response):
    """``POST /v1/stop-all`` -> 200: no slot ``failed`` or ``not_delivered`` (FR-8.6).

    A non-200 stop-all renders as the unified error body with the same outcomes in ``slots``
    (``http/errors.py``; T13-S3).
    """

    outcomes: list[SlotOutcomeBody]


class EmergencyOffResponse(_Response):
    """``POST .../emergency-off`` -> 200: ``0AEO`` completed with code ``0`` (FR-17.1, FR-17.7)."""

    controller_id: int
    emergency_off_active: bool


# -- unified error body --------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    """The ``error`` object of the unified error body (design C12; FR-9.1, FR-9.2, FR-12.7).

    ``message`` is a fixed or field-composed text that never contains a host, IP address, port,
    USB path, token or header (FR-3.7).
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    code: str | None = None
    code_description: str | None = None
    failure: str | None = None
    attempts: int | None = None
    controller_id: int | None = None
    slot_id: int | None = None
    effective_limits: EffectiveLimitsBody | None = None
    slots: list[SlotOutcomeBody] | None = None


class ErrorResponse(BaseModel):
    """Unified error body ``{"error": {...}}`` for every non-2xx response (design C12)."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail

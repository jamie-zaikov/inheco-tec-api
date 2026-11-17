"""HTTP routes of the inheco-tec api (design C12 endpoints table).

:func:`build_router` returns the functional router; ``app.py`` attaches it to the authenticated
router, so every route here requires the bearer token (FR-3.5, FR-17.3). ``/healthz`` is not here:
it is mounted unauthenticated by ``app.py`` (FR-3.6).

Rules every route follows:

- Handlers are plain ``def`` functions; FastAPI runs them in its thread pool, so a transaction that
  holds a controller lock never blocks the event loop.
- Path IDs are typed ``int`` only (no pydantic range constraint). The range and configured checks
  run in the domain operation, so an out-of-range ID gets 422 before a not-configured ID gets 404
  (FR-6.5). A non-integer path ID is a schema 422.
- A handler returns the C12 response model built from the C11 result, or lets the domain exception
  propagate; the exception handlers in ``app.py`` render it as the unified error body with the fixed
  status of its kind (FR-9.4). Stop-all raises its :func:`~.errors.stop_all_error` (409 / 502) so a
  failed or undelivered slot never reads as success (FR-8.6, FR-12.7, T13-S3).
- Every route documents its error kinds through ``error_responses`` (FR-9.4). No route has a
  free-form command field: request bodies are the closed models of ``http/schemas.py`` (FR-10.2).
- No response carries a host, IP address, port, USB path or secret (FR-3.7), and no route exposes
  the fake transport's scripting or device model (carry-forward T8-S).
- Query values (selectors, ``slot``, ``tolerance_c``) are typed only; their ranges are checked by
  the operation after the gate (C9 step 6), in the same order as every other value check. Every
  accepted value is bounded there (a finite 0.1–5.0 tolerance, a selector or slot in its table),
  so no query value reaches an arithmetic step that could fail (carry-forward T16-S).
- Set target: the schema rejects a finite ``|temperature_c|`` above
  :data:`~.schemas.TEMPERATURE_ABS_BOUND_C` and a JSON integer too large for a float. That 422
  alone would not name the slot's effective limits, which FR-8.2 requires, so the set-target route
  uses :func:`_set_target_route_class`: on exactly that schema rejection it runs the set-target
  operation's own checks and answers with its 422 and the effective limits (or with the refusal
  that comes first, e.g. 404 or 409), with no HID I/O (carry-forward T15-C1, T16-V1).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Coroutine, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from inheco_tec_api.http.error_types import ErrorKind
from inheco_tec_api.http.errors import error_responses, stop_all_error
from inheco_tec_api.http.schemas import (
    TEMPERATURE_ABS_BOUND_C,
    ActualTemperatureResponse,
    AtTargetResponse,
    ConnectResponse,
    DeviceTypeResponse,
    EmergencyOffRequest,
    EmergencyOffResponse,
    FirmwareVersionResponse,
    LifecycleResponse,
    ReacquireResponse,
    SensorValuesResponse,
    SetTargetRequest,
    SetTargetResponse,
    SlotCommandResponse,
    StatusResponse,
    StopAllResponse,
    TargetTemperatureResponse,
)
from inheco_tec_api.operations import InvalidRequest, TecService

__all__ = ["build_router", "responses_for"]

# Kinds every authenticated route can return.
_COMMON: Final[tuple[ErrorKind, ...]] = ("unauthorized", "internal_error")
# Kinds of any request that names a controller and reaches its lock.
_DEVICE: Final[tuple[ErrorKind, ...]] = (
    "validation_error",
    "not_found",
    "controller_busy",
    "service_stopping",
)
# Kinds of a request that may run transactions on the device.
_IO: Final[tuple[ErrorKind, ...]] = ("firmware_error", "transport_error")
# Kinds of a read: refused while released or not connected (C9 steps 2-3).
_READ: Final[tuple[ErrorKind, ...]] = (
    *_DEVICE,
    *_IO,
    "controller_released",
    "controller_not_connected",
)
# Kinds of set target and start: a read's kinds plus the control gates (C9 steps 4-5).
_CONTROL: Final[tuple[ErrorKind, ...]] = (*_READ, "emergency_off_active", "slot_not_permitted")
# Kinds of a slot stop: allowed while released, so it can be "not delivered" (FR-12.6, FR-12.7).
_STOP: Final[tuple[ErrorKind, ...]] = (
    *_DEVICE,
    *_IO,
    "controller_not_connected",
    "stop_not_delivered",
)

_SLOT_PATH: Final = "/v1/controllers/{cid}/slots/{sid}"
_CONTROLLER_PATH: Final = "/v1/controllers/{cid}"
_TEMPERATURE_LOC: Final = ("body", "temperature_c")
# Longest decimal path ID the set-target fallback parses; real IDs have one digit.
_MAX_FALLBACK_ID_DIGITS: Final = 9

_Handler = Callable[[Request], Coroutine[Any, Any, Response]]


def responses_for(*kinds: ErrorKind) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` of a route: the common kinds plus ``kinds`` (FR-9.4)."""
    return error_responses(*dict.fromkeys(_COMMON + kinds))


def build_router(service: TecService) -> APIRouter:
    """The functional routes over ``service``; the caller adds the auth dependency."""
    router = APIRouter()
    _add_controller_routes(router, service)
    _add_slot_routes(router, service)
    _add_query_routes(router, service)
    return router


def _add_controller_routes(router: APIRouter, service: TecService) -> None:
    """``/v1/status``, connect, release, reacquire, emergency-off and stop-all (C12)."""

    @router.get("/v1/status", responses=responses_for())
    def get_status() -> StatusResponse:
        """Status of every controller and slot; no device I/O (FR-6.6, FR-13.4, FR-17.7)."""
        return StatusResponse.of(service.status())

    @router.post(
        "/v1/controllers/{cid}/connect",
        responses=responses_for(
            *_DEVICE,
            *_IO,
            "controller_released",
            "controller_not_connected",
            "stop_all_failed",
        ),
    )
    def connect(cid: int) -> ConnectResponse:
        """Connect, reconnect after a loss, or re-initialize after emergency-off (FR-6.2)."""
        return ConnectResponse.of(service.connect(cid))

    @router.post(
        "/v1/controllers/{cid}/release",
        responses=responses_for(*_DEVICE, "controller_not_connected"),
    )
    def release(cid: int) -> LifecycleResponse:
        """Release the controller for VENUS (FR-12.1, FR-12.5)."""
        return LifecycleResponse.of(service.release(cid))

    @router.post(
        "/v1/controllers/{cid}/reacquire",
        responses=responses_for(*_DEVICE, *_IO, "controller_not_connected", "stop_all_failed"),
    )
    def reacquire(cid: int) -> ReacquireResponse:
        """Reacquire a released controller; reports ``emergency_off_cleared`` (FR-12.3, T13-C1)."""
        return ReacquireResponse.of(service.reacquire(cid))

    @router.post(
        "/v1/controllers/{cid}/emergency-off",
        responses=responses_for(
            *_DEVICE,
            *_IO,
            "controller_released",
            "controller_not_connected",
            "stop_not_delivered",
        ),
    )
    def emergency_off(cid: int, body: EmergencyOffRequest) -> EmergencyOffResponse:
        """``0AEO``; ``{"confirm": true}`` is required, anything else is 422 (FR-17.1, FR-17.2)."""
        return EmergencyOffResponse.of(service.emergency_off(cid, body.confirm))

    @router.post(
        "/v1/stop-all",
        responses=responses_for("stop_not_delivered", "stop_all_failed", "service_stopping"),
    )
    def stop_all() -> StopAllResponse:
        """``ATE0`` to every configured slot: 200, 409 not delivered or 502 failed (FR-8.6)."""
        result = service.stop_all()
        error = stop_all_error(result)
        if error is not None:
            raise error
        return StopAllResponse.of(result)


def _add_slot_routes(router: APIRouter, service: TecService) -> None:
    """Set/get target, start, stop, actual temperature and at-target of one slot (C12)."""
    router.add_api_route(
        f"{_SLOT_PATH}/target",
        _set_target_endpoint(service),
        methods=["PUT"],
        responses=responses_for(*_CONTROL),
        route_class_override=_set_target_route_class(service),
    )

    @router.get(f"{_SLOT_PATH}/target", responses=responses_for(*_READ))
    def get_target(cid: int, sid: int) -> TargetTemperatureResponse:
        """``<sid>RTT``: the target temperature in °C (FR-8.8)."""
        return TargetTemperatureResponse.of(service.target(cid, sid))

    @router.post(f"{_SLOT_PATH}/start", responses=responses_for(*_CONTROL))
    def start(cid: int, sid: int) -> SlotCommandResponse:
        """``<sid>ATE1`` on a permitted slot (FR-8.4)."""
        return SlotCommandResponse.of(service.start(cid, sid))

    @router.post(f"{_SLOT_PATH}/stop", responses=responses_for(*_STOP))
    def stop(cid: int, sid: int) -> SlotCommandResponse:
        """``<sid>ATE0``, also while released; 200 only when the stop was confirmed (FR-8.5).

        A stop that was not delivered or not confirmed raises and is rendered as 409 / 502 / 504
        with the per-slot outcome, never as 200 (FR-12.7, carry-forward T13-S3).
        """
        return SlotCommandResponse.of(service.stop(cid, sid))

    @router.get(f"{_SLOT_PATH}/temperature", responses=responses_for(*_READ))
    def get_temperature(
        cid: int,
        sid: int,
        selector: Annotated[int, Query(description="RAT selector 1-3")] = 3,
    ) -> ActualTemperatureResponse:
        """``<sid>RAT<selector>``: the actual temperature in °C, selector 1–3 (FR-8.7)."""
        return ActualTemperatureResponse.of(service.actual(cid, sid, selector))

    @router.get(f"{_SLOT_PATH}/at-target", responses=responses_for(*_READ))
    def get_at_target(
        cid: int,
        sid: int,
        tolerance_c: Annotated[
            float | None, Query(description="tolerance override 0.1-5.0 C")
        ] = None,
    ) -> AtTargetResponse:
        """``<sid>RAT3`` + ``<sid>RTT``: one instantaneous at-target check (FR-8.9)."""
        return AtTargetResponse.of(service.at_target(cid, sid, tolerance_c))


def _add_query_routes(router: APIRouter, service: TecService) -> None:
    """Controller-level reads: sensor values, device type and firmware version (C12)."""

    @router.get(f"{_CONTROLLER_PATH}/sensor-values", responses=responses_for(*_READ))
    def get_sensor_values(
        cid: int, selector: Annotated[int, Query(description="RHV selector 0-8")]
    ) -> SensorValuesResponse:
        """``0RHV<selector>``, selector 0–8 (FR-8.10)."""
        return SensorValuesResponse.of(service.sensor_values(cid, selector))

    @router.get(f"{_CONTROLLER_PATH}/device-type", responses=responses_for(*_READ))
    def get_device_type(
        cid: int, slot: Annotated[int, Query(description="slot 0-6 (0 = controller)")]
    ) -> DeviceTypeResponse:
        """``0RTD<slot>``, slot 0–6 with a range check only, no configured check (FR-8.11, P3)."""
        return DeviceTypeResponse.of(service.device_type(cid, slot))

    @router.get(f"{_CONTROLLER_PATH}/firmware-version", responses=responses_for(*_READ))
    def get_firmware_version(
        cid: int, selector: Annotated[int, Query(description="RFV selector 0-4")]
    ) -> FirmwareVersionResponse:
        """``0RFV<selector>``, selector 0–4 (FR-8.12)."""
        return FirmwareVersionResponse.of(service.firmware_version(cid, selector))


# -- set target and its out-of-bound fallback (FR-8.2) -------------------------------------------


def _set_target_endpoint(service: TecService) -> Callable[..., SetTargetResponse]:
    """The ``PUT .../target`` handler (a plain ``def``, run in the thread pool)."""

    def set_target(cid: int, sid: int, body: SetTargetRequest) -> SetTargetResponse:
        """``<sid>STT<tenths>``; outside the effective limits → 422 with the limits (FR-8.1–8.3)."""
        return SetTargetResponse.of(service.set_target(cid, sid, body.temperature_c))

    return set_target


def _set_target_route_class(service: TecService) -> type[APIRoute]:
    """An ``APIRoute`` that turns the schema's out-of-bound target 422 into the FR-8.2 422.

    Only a rejection whose every error is the ``temperature_c`` magnitude bound (see
    :func:`_out_of_bound_ids`) is handled; any other schema error, and every other request, is
    left unchanged. The router's dependencies (the bearer check) run before the body is
    validated, so an unauthenticated request still gets 401.
    """

    class SetTargetRoute(APIRoute):
        def get_route_handler(self) -> _Handler:
            handler = super().get_route_handler()

            async def route_handler(request: Request) -> Response:
                try:
                    return await handler(request)
                except RequestValidationError as exc:
                    ids = _out_of_bound_ids(request, exc.errors())
                    if ids is None:
                        raise
                    await run_in_threadpool(_reject_out_of_bound, service, *ids)
                    raise  # not reached: _reject_out_of_bound always raises

            return route_handler

    return SetTargetRoute


def _out_of_bound_ids(request: Request, errors: Sequence[Any]) -> tuple[int, int] | None:
    """``(cid, sid)`` when the ONLY schema errors are an out-of-bound ``temperature_c``.

    Out of bound means a finite number with ``|t| > TEMPERATURE_ABS_BOUND_C``: the schema's bound
    (``value_error``) or a JSON integer too large for a float (``float_type`` with an ``int``
    input). A string, a boolean, a missing field or any error elsewhere returns ``None``.
    """
    if not errors:
        return None
    for error in errors:
        if not isinstance(error, dict) or tuple(error.get("loc", ())) != _TEMPERATURE_LOC:
            return None
        if error.get("type") not in ("value_error", "float_type"):
            return None
        value = error.get("input")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if abs(value) <= TEMPERATURE_ABS_BOUND_C:
            return None
    cid, sid = _fallback_id(request, "cid"), _fallback_id(request, "sid")
    if cid is None or sid is None:
        return None
    return cid, sid


def _fallback_id(request: Request, name: str) -> int | None:
    """A short decimal path ID as an int (the operation checks its range), else ``None``."""
    value = request.path_params.get(name)
    if not isinstance(value, str) or len(value) > _MAX_FALLBACK_ID_DIGITS:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _reject_out_of_bound(service: TecService, cid: int, sid: int) -> None:
    """Raise the set-target refusal for an out-of-bound target; never sends a command.

    The operation is called with ``NaN``: it runs the same ID checks, gate and value check as any
    target (C9 steps 1–6), and a non-finite target is always refused at step 6 with
    :class:`InvalidRequest` carrying the effective limits, before any HID I/O (FR-8.2). The
    refusals of steps 1–5 (422 range, 404, 409, 503) propagate unchanged; the step-6 refusal is
    re-raised with a message that names the limits instead of "must be a finite number".
    """
    try:
        service.set_target(cid, sid, math.nan)
    except InvalidRequest as exc:
        limits = exc.effective_limits
        if limits is None:
            raise
        raise InvalidRequest(
            f"temperature_c is outside the effective limits {limits.min_c}-{limits.max_c} C",
            controller_id=exc.controller_id,
            slot_id=exc.slot_id,
            effective_limits=limits,
        ) from None
    # Fail closed: a NaN target is never accepted, so this line is not reached.
    raise InvalidRequest("temperature_c is out of range", controller_id=cid, slot_id=sid)

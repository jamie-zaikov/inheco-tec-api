"""The FastAPI application factory of the inheco-tec api (design C12, C13; FR-3, NFR-6).

:func:`create_app` wires the ``instrument_api_core`` base (health, bearer auth) and the domain layer
(:class:`~.registry.ControllerRegistry`, :class:`~.operations.TecService`) into one app, in the
reference ``zebra_printer`` style:

- Fail-closed startup: the expected bearer token is read at ``create_app`` time from the env var
  NAMED by ``manifest.auth.bearer_token_ref`` (``load_expected_token``); an unset, empty or
  whitespace-only var raises ``AuthConfigError`` and no app is built (FR-3.4). The token value is
  never logged or returned (NFR-1, FR-15.3).
- Auth: every functional route, ``/openapi.json`` included, depends on
  :func:`verify_bearer_token` (constant-time compare in ``instrument_api_core``); a missing or
  wrong token gets 401 with the fixed ``Unauthorized`` message (FR-3.5, FR-17.3). ``/healthz`` is
  unauthenticated and touches no device (FR-3.6). FastAPI's own ``docs_url``, ``redoc_url`` and
  ``openapi_url`` are disabled; the unauthenticated Swagger UI page ``/docs`` comes from
  :mod:`~.http.docs` and embeds the API description, so ``/openapi.json`` keeps its token.
- Lifespan: ``registry.startup()`` once at startup, ``registry.shutdown()`` once at shutdown with
  the default deadline ``service.shutdown_timeout_s`` (C13 graceful shutdown; carry-forward
  T12-C1, T14-E). Both run in the thread pool, so the device I/O never blocks the event loop.
  The registry owns the ONE release state store of the service (T11-C2).
- Transport: an injected factory is used as given (tests); otherwise the kind comes from the
  manifest with the ``INHECO_TEC_TRANSPORT`` override applied (``hidapi`` → the real HID factory,
  ``fake`` → the fake). No other path selects the fake, and no route exposes it (FR-13.4, T8-S).
- Errors: every error is the unified body with the fixed status of its kind (C12, FR-9.4). Domain
  exceptions go through :func:`~.http.errors.to_api_error` (``Retry-After`` kept for
  ``controller_busy``); a schema error through :func:`~.http.errors.request_validation_error`;
  FastAPI/Starlette ``HTTPException`` (the 401, an unknown path, a body that cannot be parsed)
  through :func:`_http_error`. An unexpected exception becomes 500 ``internal_error`` with a fixed
  message; the log line carries the exception type and the stack frames only, never the exception
  text (carry-forward T16-E, T16-S).
- One ``http_request`` event per request with flat fields only: ``method``, ``route`` (the path
  template, never the concrete path) and ``status_class`` (C14; carry-forward T2).
- OpenAPI documents each route's error kinds with the unified ``ErrorResponse``; FastAPI's default
  ``HTTPValidationError`` 422 schema is removed (FR-9.4, carry-forward T16-C).

Logging is configured by the service entry point (C13 step 1), not here.
"""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inheco_tec_api._vendor.instrument_api_core import (
    HEALTH_PATH,
    Unauthorized,
    health_payload,
    load_expected_token,
    verify_bearer_token,
)
from inheco_tec_api.controller import ControllerError
from inheco_tec_api.engine import DeviceLost, TransactionFailed
from inheco_tec_api.http.docs import DocsTrailingSlashGuard, build_docs_router
from inheco_tec_api.http.errors import (
    ApiError,
    InternalError,
    NotFoundError,
    UnauthorizedError,
    ValidationFailed,
    request_validation_error,
    to_api_error,
)
from inheco_tec_api.http.routes import build_router
from inheco_tec_api.http.schemas import ErrorDetail, ErrorResponse
from inheco_tec_api.logging_setup import EVENT_HTTP_REQUEST, get_logger
from inheco_tec_api.manifest import Manifest, _effective_transport, load_manifest
from inheco_tec_api.operations import InvalidRequest, NotConfigured, TecService
from inheco_tec_api.registry import ControllerRegistry, ServiceStopping
from inheco_tec_api.state_store import ReleaseStateStore, StateStoreError
from inheco_tec_api.transport.base import TransportFactory
from inheco_tec_api.transport.fake import FakeTransportFactory
from inheco_tec_api.transport.hidapi_transport import HidapiTransportFactory

__all__ = ["EVENT_HTTP_INTERNAL_ERROR", "OPENAPI_PATH", "create_app"]

EVENT_HTTP_INTERNAL_ERROR: Final = "http_internal_error"
"""Error event of an unexpected exception (500). It carries ``method``, ``route``, ``error`` (the
exception type name) and ``stack`` (the traceback frames without the exception text)."""

OPENAPI_PATH: Final = "/openapi.json"
"""The API description, served on the authenticated router (FR-9.4)."""

_UNAUTHORIZED_MESSAGE: Final = Unauthorized().args[0]
_PARSE_MESSAGE: Final = "invalid request: the request could not be parsed"
_MAX_ID_DIGITS: Final = 3
_DEFAULT_422_SCHEMAS: Final = ("HTTPValidationError", "ValidationError")
_ERROR_REF: Final = "#/components/schemas/ErrorResponse"

# Domain exceptions rendered by to_api_error (C12 status table).
_DOMAIN_ERRORS: Final[tuple[type[Exception], ...]] = (
    ApiError,
    Unauthorized,
    ServiceStopping,
    InvalidRequest,
    NotConfigured,
    ControllerError,
    TransactionFailed,
    DeviceLost,
    StateStoreError,
)

_log = get_logger(__name__)


# -- the app ------------------------------------------------------------------------------------


class _TecFastAPI(FastAPI):
    """FastAPI whose OpenAPI has no default ``HTTPValidationError`` 422 (carry-forward T16-C)."""

    def openapi(self) -> dict[str, Any]:
        schema = super().openapi()
        _scrub_default_422(schema)
        return schema


def create_app(
    manifest: Manifest | None = None,
    transport_factory: TransportFactory | None = None,
    state_store: ReleaseStateStore | None = None,
    clock: Callable[[], float] | None = None,
) -> FastAPI:
    """Build the app (design C12). Raises ``AuthConfigError`` when the token var is not set.

    ``manifest`` defaults to :func:`~.manifest.load_manifest` (fail closed on a ``ManifestError``).
    ``transport_factory`` defaults to the manifest transport kind after the ``INHECO_TEC_TRANSPORT``
    override; ``state_store`` defaults to one store at ``service.state_file``; ``clock`` (a
    monotonic clock in seconds) goes to the registry and to the default fake factory. The registry
    is started by the lifespan, so a test client must enter the app's context (``with
    TestClient(app)``) for startup and shutdown to run.
    """
    if manifest is None:
        manifest = load_manifest()
    # Fail closed before anything else is built: no token, no app (FR-3.4).
    expected_token = load_expected_token(manifest.auth.bearer_token_ref)  # pragma: allowlist secret
    if transport_factory is None:
        manifest, transport_factory = _default_factory(manifest, clock)
    registry_kwargs: dict[str, Any] = {} if clock is None else {"clock": clock}
    registry = ControllerRegistry(manifest, transport_factory, state_store, **registry_kwargs)
    service = TecService(registry)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(registry.startup)
        try:
            yield
        finally:
            await run_in_threadpool(registry.shutdown)

    app = _TecFastAPI(
        title="Inheco TEC API",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.registry = registry
    app.state.service = service

    def require_bearer_token(authorization: Annotated[str | None, Header()] = None) -> None:
        """401 unless the ``Authorization`` header holds the expected bearer token (FR-3.5)."""
        try:
            verify_bearer_token(authorization, expected_token)
        except Unauthorized:
            raise HTTPException(status_code=401, detail=_UNAUTHORIZED_MESSAGE) from None

    health_router = APIRouter()

    @health_router.get(HEALTH_PATH)
    def healthz() -> dict[str, str]:
        """Liveness only: ``{"status": "ok"}``, no auth, no device I/O (FR-3.6)."""
        return health_payload()

    authenticated_router = APIRouter(dependencies=[Depends(require_bearer_token)])

    @authenticated_router.get(OPENAPI_PATH, include_in_schema=False)
    def openapi_json() -> JSONResponse:
        """The API description with the error kinds of each route (FR-9.4)."""
        return JSONResponse(app.openapi())

    authenticated_router.include_router(build_router(service))

    app.include_router(health_router)
    app.include_router(build_docs_router(app.openapi))
    app.include_router(authenticated_router)
    for error_type in _DOMAIN_ERRORS:
        app.add_exception_handler(error_type, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    # Added first, so it runs inside _RequestMiddleware and its 404 is logged like any other.
    app.add_middleware(DocsTrailingSlashGuard)
    app.add_middleware(_RequestMiddleware)
    return app


def _default_factory(
    manifest: Manifest, clock: Callable[[], float] | None
) -> tuple[Manifest, TransportFactory]:
    """The factory of the effective transport kind (manifest + ``INHECO_TEC_TRANSPORT``, T8-S).

    The override is applied again here, so a manifest built in code follows the same single
    selection path as one from :func:`~.manifest.load_manifest` (applying it twice is a no-op).
    """
    transport = _effective_transport(manifest.transport)
    if transport != manifest.transport:
        manifest = manifest.model_copy(update={"transport": transport})
    if transport.kind == "fake":
        return manifest, FakeTransportFactory() if clock is None else FakeTransportFactory(
            clock=clock
        )
    return manifest, HidapiTransportFactory()


# -- error rendering ------------------------------------------------------------------------------


def _json(error: ApiError) -> JSONResponse:
    """The unified body with the fixed status of the error's kind and its headers (T16-E)."""
    return JSONResponse(error.body(), status_code=error.status_code, headers=error.headers or None)


def _path_id(request: Request, name: str) -> int | None:
    """A short decimal path parameter as an int, else ``None`` (it fills the body IDs only)."""
    value = request.path_params.get(name)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        if len(value) <= _MAX_ID_DIGITS:
            return int(value)
    return None


def _route_template(scope: Scope) -> str | None:
    """The matched route's path template (``/v1/controllers/{cid}/connect``), never the path."""
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) else None


def _log_internal(scope: Scope, exc: BaseException) -> None:
    """Log an unexpected exception: type and stack frames only, never its text (T16-S)."""
    _log.error(
        EVENT_HTTP_INTERNAL_ERROR,
        method=scope.get("method"),
        route=_route_template(scope),
        error=type(exc).__name__,
        stack="".join(traceback.format_tb(exc.__traceback__)),
    )


async def _domain_error(request: Request, exc: Exception) -> JSONResponse:
    """A domain exception → its C12 kind and status (carry-forward T16-E)."""
    error = to_api_error(
        exc, controller_id=_path_id(request, "cid"), slot_id=_path_id(request, "sid")
    )
    if isinstance(error, InternalError):
        _log_internal(request.scope, exc)
    return _json(error)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """A schema error → 422 ``validation_error``; no client input is echoed (FR-3.7)."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    return _json(request_validation_error(errors))


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """A FastAPI/Starlette ``HTTPException`` → the unified body (carry-forward T16-S).

    401 is ``unauthorized`` with the fixed message; 404 (unknown path) is ``not_found``; 400 (a
    body FastAPI cannot parse) and 422 are ``validation_error`` with HTTP 422. Any other status is
    kept, with a fixed message from the status phrase. The exception's ``detail`` is never echoed.
    """
    status = exc.status_code if isinstance(exc, HTTPException) else 500
    headers = exc.headers if isinstance(exc, HTTPException) else None
    if status == 401:
        return _json(UnauthorizedError(_UNAUTHORIZED_MESSAGE))
    if status == 404:
        return _json(NotFoundError("not found"))
    if status in (400, 422):
        return _json(ValidationFailed(_PARSE_MESSAGE))
    kind = "validation_error" if status < 500 else "internal_error"
    try:
        message = HTTPStatus(status).phrase.lower()
    except ValueError:
        message = "request failed"
    body = ErrorResponse(error=ErrorDetail(kind=kind, message=message)).model_dump(mode="json")
    return JSONResponse(body, status_code=status, headers=headers)


# -- per-request middleware ----------------------------------------------------------------------


class _RequestMiddleware:
    """Logs one ``http_request`` per request and turns an unexpected exception into a 500.

    It runs inside Starlette's server-error middleware, so an exception that no handler took is
    answered here with the fixed ``internal_error`` body instead of reaching the server log with
    its text (T16-S).
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        status = 500
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, started
            if message["type"] == "http.response.start":
                status = int(message["status"])
                started = True
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 — fixed 500 body; the stack goes to the log only
            _log_internal(scope, exc)
            if not started:
                response = _json(InternalError("internal error"))
                await response(scope, receive, send_wrapper)
        finally:
            _log.info(
                EVENT_HTTP_REQUEST,
                method=scope.get("method"),
                route=_route_template(scope),
                status_class=f"{status // 100}xx",
            )


# -- OpenAPI -------------------------------------------------------------------------------------


def _scrub_default_422(schema: dict[str, Any]) -> None:
    """Point every default 422 at ``ErrorResponse`` and drop FastAPI's validation schemas."""
    for operations in schema.get("paths", {}).values():
        for operation in operations.values():
            responses = operation.get("responses", {}) if isinstance(operation, dict) else {}
            default = responses.get("422")
            if default is not None and "HTTPValidationError" in str(default):
                responses["422"] = {
                    "description": "error kind: validation_error",
                    "content": {"application/json": {"schema": {"$ref": _ERROR_REF}}},
                }
    components = schema.get("components", {}).get("schemas", {})
    for name in _DEFAULT_422_SCHEMAS:
        components.pop(name, None)

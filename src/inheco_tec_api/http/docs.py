"""The Swagger UI page of the inheco-tec api (``/docs``).

:func:`build_docs_router` returns an UNAUTHENTICATED router with four fixed routes. ``app.py``
mounts it next to ``/healthz``:

- ``GET /docs`` — a static HTML shell. It holds no inline script, so the Content-Security-Policy
  allows scripts from the same origin only.
- ``GET /docs/init.js`` — starts Swagger UI with the API description EMBEDDED in the script. A
  browser cannot send the bearer token when it loads a page, so the page never fetches
  ``/openapi.json``, which stays on the authenticated router (FR-3.5). The embedded copy adds a
  ``bearerAuth`` scheme so the "Authorize" button takes the token; every functional call from the
  page still sends it and gets 401 without it. The token stays in page memory only
  (``persistAuthorization`` off).
- ``GET /docs/assets/swagger-ui-bundle.js`` and ``GET /docs/assets/swagger-ui.css`` — the vendored
  ``swagger-ui-dist`` files of ``docs_assets/`` (see its README), read from the installed package,
  so the lab host loads no script from a CDN. Only these two names exist; there is no directory
  route and no path parameter, so no other file can be reached.

The API description is readable without the token on this page (user decision 2026-10-12: public
``/docs``, token for calls). It names routes and schemas only; no host, IP address, port, device
path or secret is in it (FR-3.7). ``/redoc`` and ``/docs/oauth2-redirect`` do not exist.
"""

from __future__ import annotations

import copy
import functools
import json
from collections.abc import Callable
from importlib import resources
from typing import Any, Final

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from inheco_tec_api.http.error_types import NotFoundError

__all__ = [
    "DOCS_ASSET_CSS",
    "DOCS_ASSET_JS",
    "DOCS_CSP",
    "DOCS_INIT_PATH",
    "DOCS_PATH",
    "DocsTrailingSlashGuard",
    "build_docs_router",
    "docs_spec",
    "js_literal",
]

DOCS_PATH: Final = "/docs"
DOCS_INIT_PATH: Final = "/docs/init.js"
DOCS_ASSET_JS: Final = "/docs/assets/swagger-ui-bundle.js"
DOCS_ASSET_CSS: Final = "/docs/assets/swagger-ui.css"

DOCS_CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
"""Swagger UI sets inline styles, so styles allow ``'unsafe-inline'``; scripts never do."""

_COMMON_HEADERS: Final = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_NO_STORE: Final = {"Cache-Control": "no-store"}
_ASSET_CACHE: Final = {"Cache-Control": "public, max-age=3600"}
_ASSETS_PACKAGE: Final = "inheco_tec_api"
_ASSETS_DIR: Final = "docs_assets"
_HEALTH_PATH: Final = "/healthz"
_DOCS_PREFIX: Final = DOCS_PATH + "/"

_PAGE: Final = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Inheco TEC API</title>
  <link rel="stylesheet" href="/docs/assets/swagger-ui.css">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/docs/assets/swagger-ui-bundle.js"></script>
  <script src="/docs/init.js"></script>
</body>
</html>
"""

_INIT_TEMPLATE: Final = """window.ui = SwaggerUIBundle({
  spec: %s,
  dom_id: "#swagger-ui",
  deepLinking: false,
  persistAuthorization: false,
  tryItOutEnabled: false,
  docExpansion: "list",
  defaultModelsExpandDepth: 0
});
"""

_JS_ESCAPES: Final = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def js_literal(value: Any) -> str:
    """``value`` as a JSON literal that is also safe inside a script (no ``</script>``)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    for raw, escaped in _JS_ESCAPES.items():
        text = text.replace(raw, escaped)
    return text


def docs_spec(schema: dict[str, Any]) -> dict[str, Any]:
    """A copy of the API description with the ``bearerAuth`` scheme the page needs.

    Every operation requires the bearer token except ``/healthz`` (FR-3.5, FR-3.6). The token
    check reads an ``Authorization`` header parameter, so FastAPI lists that header on every
    operation; the copy drops it, so the one "Authorize" entry at the top of the page applies to
    every call and no operation asks for the token again. The input is not changed, so
    ``/openapi.json`` keeps its own content.
    """
    spec = copy.deepcopy(schema)
    components = spec.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes["bearerAuth"] = {"type": "http", "scheme": "bearer"}
    spec["security"] = [{"bearerAuth": []}]
    for path, item in spec.get("paths", {}).items():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            params = [p for p in operation.get("parameters", []) if not _is_auth_header(p)]
            if params:
                operation["parameters"] = params
            else:
                operation.pop("parameters", None)
            if path == _HEALTH_PATH:
                operation["security"] = []
    return spec


def _is_auth_header(parameter: Any) -> bool:
    return (
        isinstance(parameter, dict)
        and parameter.get("in") == "header"
        and str(parameter.get("name", "")).lower() == "authorization"
    )


@functools.cache
def _asset(name: str) -> bytes:
    return resources.files(_ASSETS_PACKAGE).joinpath(_ASSETS_DIR, name).read_bytes()


def build_docs_router(openapi: Callable[[], dict[str, Any]]) -> APIRouter:
    """The unauthenticated ``/docs`` router; ``openapi`` is the app's ``app.openapi``."""
    router = APIRouter(include_in_schema=False)

    @router.get(DOCS_PATH, response_class=HTMLResponse)
    def docs_page() -> HTMLResponse:
        headers = {**_COMMON_HEADERS, **_NO_STORE, "Content-Security-Policy": DOCS_CSP}
        return HTMLResponse(_PAGE, headers=headers)

    @router.get(DOCS_INIT_PATH)
    def docs_init() -> Response:
        body = _INIT_TEMPLATE % js_literal(docs_spec(openapi()))
        headers = {**_COMMON_HEADERS, **_NO_STORE}
        return Response(body, media_type="text/javascript; charset=utf-8", headers=headers)

    @router.get(DOCS_ASSET_JS)
    def docs_asset_js() -> Response:
        headers = {**_COMMON_HEADERS, **_ASSET_CACHE}
        body = _asset("swagger-ui-bundle.js")
        return Response(body, media_type="text/javascript; charset=utf-8", headers=headers)

    @router.get(DOCS_ASSET_CSS)
    def docs_asset_css() -> Response:
        headers = {**_COMMON_HEADERS, **_ASSET_CACHE}
        body = _asset("swagger-ui.css")
        return Response(body, media_type="text/css; charset=utf-8", headers=headers)

    return router


class DocsTrailingSlashGuard:
    """Pure-ASGI guard: a path under ``/docs/`` that ends in ``/`` gets 404, never a redirect.

    No documentation route ends in ``/``, so such a path can never be served; it is answered
    before routing with the unified ``not_found`` body. It reads no file and touches no device.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith(_DOCS_PREFIX) and path.endswith("/"):
            response = JSONResponse(NotFoundError("not found").body(), status_code=404)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

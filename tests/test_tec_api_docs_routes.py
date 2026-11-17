"""Route rules of the Swagger UI page (amendment A5, Task 26.7; design C12 *Documentation page*).

- FR-18.12: an un-normalised traversal path sent through a raw ASGI scope (httpx normalises a
  literal ``..`` segment, so it never reaches the app through the client) gets exactly 404
  ``not_found``; ``POST``/``PUT``/``DELETE`` (and ``HEAD``, ``PATCH``, ``OPTIONS``) on the four
  routes get 405 with ``GET`` in ``Allow`` and the unified ``validation_error`` body; no write.
  A trailing-slash path under ``/docs/`` gets 404 ``not_found``, never a redirect (UA-41 resolved
  in code by ``DocsTrailingSlashGuard``).
- FR-18.13: ``/healthz/`` and ``/v1/status/`` keep the app-wide 307 to the slash-less path.
- FR-18.14: requests that carry the bearer token write no log line that holds the token.
- FR-3.5 / FR-18.1: ``PUBLIC_DOC_PATHS`` is exactly the four routes, and only those and
  ``/healthz`` answer without a token.

Error bodies go through :class:`ScanningClient` (FR-3.7); the vendored asset bodies use a plain
``TestClient`` (they are pinned byte for byte in ``test_tec_api_docs.py``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from inheco_tec_api.http.docs import (
    DOCS_ASSET_CSS,
    DOCS_ASSET_JS,
    DOCS_INIT_PATH,
    DOCS_PATH,
    DocsTrailingSlashGuard,
)
from structlog.testing import capture_logs
from tec_api_helpers import PUBLIC_DOC_PATHS, ApiRig, iter_routes, leaks

DOC_ROUTES = (DOCS_PATH, DOCS_INIT_PATH, DOCS_ASSET_JS, DOCS_ASSET_CSS)
FILE_MARKERS = ("swagger-ui", "SwaggerUIBundle", "Apache License", "def ", "import ", "{color:")


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


def _no_file_content(text: str) -> None:
    assert not [m for m in FILE_MARKERS if m in text], text[:200]


def _asgi_get(app: Any, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """One raw ASGI ``GET`` with ``path`` exactly as given (no client-side normalisation)."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")]
        + [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
    }
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return int(start["status"]), body


# -- FR-18.12: raw-scope traversal ----------------------------------------------------------------


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", ["/docs/assets/../LICENSE", "/docs/assets/../../app.py"])
def test_fr18_12_raw_scope_traversal_is_404_not_found(
    rig: ApiRig, path: str, with_token: bool
) -> None:
    status, body = _asgi_get(rig.app, path, rig.auth if with_token else {})
    assert status == 404
    payload = json.loads(body)
    assert set(payload) == {"error"} and payload["error"]["kind"] == "not_found"
    text = body.decode("utf-8")
    _no_file_content(text)
    assert leaks(text, rig.markers) == []


# -- FR-18.12: the 405 rule (design C12) ----------------------------------------------------------


def _assert_405(rig: ApiRig, response: Any) -> None:
    assert response.status_code == 405
    allowed = {item.strip() for item in response.headers["allow"].split(",")}
    assert "GET" in allowed and allowed <= {"GET", "HEAD"}
    assert "detail" not in response.json()
    assert rig.error(response)["kind"] == "validation_error"
    _no_file_content(response.text)


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", DOC_ROUTES)
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_fr18_12_write_methods_on_docs_routes_are_405(
    rig: ApiRig, method: str, path: str, with_token: bool
) -> None:
    with rig.client() as client:
        rig.mark()
        response = client.request(method, path, headers=rig.auth if with_token else {})
        _assert_405(rig, response)
        assert rig.commands() == []


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", DOC_ROUTES)
@pytest.mark.parametrize("method", ["PATCH", "OPTIONS"])
def test_fr18_12_other_methods_on_docs_routes_are_405(
    rig: ApiRig, method: str, path: str, with_token: bool
) -> None:
    with rig.client() as client:
        rig.mark()
        _assert_405(rig, client.request(method, path, headers=rig.auth if with_token else {}))
        assert rig.commands() == []


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", DOC_ROUTES)
def test_fr18_12_head_on_docs_routes_is_405_allow_get(
    rig: ApiRig, path: str, with_token: bool
) -> None:
    # FastAPI's APIRoute adds no HEAD to a GET route, so HEAD is a 405 case (Task 26 executor
    # finding; matches FR-18.12). A HEAD response has no body to check.
    with rig.client() as client:
        rig.mark()
        response = client.head(path, headers=rig.auth if with_token else {})
        assert response.status_code == 405
        assert "GET" in {item.strip() for item in response.headers["allow"].split(",")}
        assert response.content == b""
        assert rig.commands() == []


# -- FR-18.12: trailing-slash forms under /docs/ (UA-41 resolved in code) -------------------------

TRAILING_SLASH_DOC_PATHS = (
    "/docs/",
    "/docs/init.js/",
    "/docs/assets/swagger-ui-bundle.js/",
    "/docs/assets/swagger-ui.css/",
    "/docs//",
    "/docs/x/",
)


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", TRAILING_SLASH_DOC_PATHS)
def test_fr18_12_trailing_slash_docs_paths_are_404_not_found(
    rig: ApiRig, path: str, method: str, with_token: bool
) -> None:
    """FR-18.12: a trailing-slash path under ``/docs/`` is "any other path under /docs/".

    It answers exactly 404 with the unified ``not_found`` body (the same body as an unknown
    ``/docs/...`` path), no redirect and no ``Location`` header, no file content, and no device
    write, with and without the token. UA-41 (Starlette's app-wide 307) is resolved in code by
    ``DocsTrailingSlashGuard``; this test replaces the earlier pin of the 307.
    """
    headers = rig.auth if with_token else {}
    with rig.client() as client:
        reference = client.get("/docs/nope", headers=headers)
        rig.mark()
        response = client.request(method, path, headers=headers, follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers
        assert rig.error(response)["kind"] == "not_found"
        assert response.json() == reference.json()
        _no_file_content(response.text)
        assert rig.commands() == []
        assert client.scanned == 2


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", ["/docs/", "/docs/init.js/", "/docs/assets/swagger-ui.css/"])
def test_fr18_12_trailing_slash_raw_scope_is_404_not_found(
    rig: ApiRig, path: str, with_token: bool
) -> None:
    """FR-18.12: the same 404 when the path reaches the app un-normalised (raw ASGI scope)."""
    status, body = _asgi_get(rig.app, path, rig.auth if with_token else {})
    assert status == 404
    payload = json.loads(body)
    assert set(payload) == {"error"} and payload["error"]["kind"] == "not_found"
    _no_file_content(body.decode("utf-8"))


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
def test_fr18_12_docs_without_slash_still_200(rig: ApiRig, with_token: bool) -> None:
    """FR-18.1 / FR-18.12: the guard matches ``/docs/`` only; ``GET /docs`` still works."""
    with rig.client() as client:
        response = client.get(DOCS_PATH, headers=rig.auth if with_token else {})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="swagger-ui"></div>' in response.text


# -- FR-18.13: the app-wide trailing-slash redirect is unchanged elsewhere ----------------------


@pytest.mark.parametrize("with_token", [False, True], ids=["no-token", "token"])
@pytest.mark.parametrize("path", ["/healthz", "/v1/status"])
def test_fr18_13_existing_trailing_slash_redirect_unchanged(
    rig: ApiRig, path: str, with_token: bool
) -> None:
    """FR-18.13: ``/healthz/`` and ``/v1/status/`` keep Starlette's 307 to the slash-less path.

    The docs guard is limited to ``/docs/``, so existing routes keep the pre-A5 redirect: 307,
    empty body, ``Location`` ending in the slash-less path, and no device write.
    """
    headers = rig.auth if with_token else {}
    with rig.client() as client:
        rig.mark()
        response = client.get(path + "/", headers=headers, follow_redirects=False)
        assert response.status_code == 307
        assert response.content == b""
        assert response.headers["location"].endswith(path)
        assert not response.headers["location"].endswith(path + "/")
        assert rig.commands() == []


def test_fr18_12_guard_is_middleware_not_a_route(rig: ApiRig) -> None:
    """FR-18.1: the trailing-slash guard adds no route; the ``/docs*`` routes stay the four."""
    paths = {path for path, _route in iter_routes(rig.app.routes) if path.startswith("/docs")}
    assert paths == PUBLIC_DOC_PATHS
    assert not [p for p in paths if p.endswith("/")]
    classes: list[Any] = [m.cls for m in rig.app.user_middleware]
    assert DocsTrailingSlashGuard in classes


# -- FR-18.14: no token in the log ----------------------------------------------------------------


def test_fr18_14_docs_requests_with_token_log_no_token(
    rig: ApiRig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    with TestClient(rig.app) as client, capture_logs() as events:
        for path in DOC_ROUTES:
            assert client.get(path, headers=rig.auth).status_code == 200
            client.post(path, headers=rig.auth)
    requests = [e for e in events if e["event"] == "http_request"]
    assert {e["route"] for e in requests} >= set(DOC_ROUTES)
    out = capsys.readouterr()
    for text in (repr(events), caplog.text, out.out, out.err):
        assert rig.bearer not in text


# -- FR-3.5 / FR-18.1: the public route set -------------------------------------------------------


def test_fr18_1_public_doc_paths_is_exactly_the_four_routes(rig: ApiRig) -> None:
    assert PUBLIC_DOC_PATHS == {
        "/docs",
        "/docs/init.js",
        "/docs/assets/swagger-ui-bundle.js",
        "/docs/assets/swagger-ui.css",
    }
    docs_routes = {
        (path, frozenset(route.methods))
        for path, route in iter_routes(rig.app.routes)
        if path.startswith("/docs") and getattr(route, "methods", None)
    }
    assert docs_routes == {(path, frozenset({"GET"})) for path in PUBLIC_DOC_PATHS}


def test_fr3_5_only_healthz_and_docs_routes_answer_without_token(rig: ApiRig) -> None:
    public = set()
    with TestClient(rig.app) as client:
        for path, route in iter_routes(rig.app.routes):
            for method in sorted((getattr(route, "methods", None) or set()) - {"HEAD"}):
                concrete = path.replace("{cid}", "0").replace("{sid}", "1")
                if client.request(method, concrete).status_code != 401:
                    public.add(path)
    assert public == {"/healthz"} | PUBLIC_DOC_PATHS

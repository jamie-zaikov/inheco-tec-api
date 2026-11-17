"""Shared helpers for the HTTP API suites (not a test module; Task 17).

- :func:`sentinel` builds a unique marker at runtime (a test token or an exception text that must
  never appear in a response or a log). It is not a literal secret, so the secret scanner has
  nothing to flag.
- :class:`ScanningClient` is a ``TestClient`` that checks EVERY response body it receives for an
  IPv4 address, a ``host:port`` pair, a USB/HID device path and every registered sentinel
  (FR-3.7, NFR-1). The scan runs inside ``request``, so no test can forget it.
- :class:`ApiRig` builds the app with ``create_app`` over a fake-kind manifest (zero delays, the
  state file under ``tmp_path``) and an injected :class:`FakeTransportFactory`, with the bearer
  token set through ``monkeypatch`` on the env var NAMED by ``auth.bearer_token_ref``.

Only the in-process ``TestClient`` is used: no network port is bound, and the real ``hid`` module
is never imported. The tests directory has no ``__init__.py`` (design Risk R8).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from inheco_tec_api.app import create_app
from inheco_tec_api.http.docs import DOCS_ASSET_CSS, DOCS_ASSET_JS, DOCS_INIT_PATH, DOCS_PATH
from inheco_tec_api.manifest import TRANSPORT_ENV_VAR, Manifest
from inheco_tec_api.transport.fake import FakeTecDevice, FakeTransportFactory
from tec_registry_helpers import registry_manifest

PUBLIC_DOC_PATHS: frozenset[str] = frozenset(
    {DOCS_PATH, DOCS_INIT_PATH, DOCS_ASSET_JS, DOCS_ASSET_CSS}
)
"""The unauthenticated Swagger UI routes (``http/docs.py``); covered by ``test_tec_api_docs.py``."""

LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"),  # IPv4
    re.compile(r"[A-Za-z0-9\]]:\d{2,5}\b"),  # host:port (a JSON value follows '"', not this)
    re.compile(r"\\\\\?\\|/dev/hidraw|/dev/usb|usb#vid_|vid_[0-9a-f]{4}|iousb|hid#", re.I),
    re.compile(r"\bCOM\d+\b"),
)
"""FR-3.7 leak shapes: IPv4, ``host:port``, USB/HID device paths, COM ports."""


def sentinel(label: str) -> str:
    """A unique runtime marker such as ``t17-token-3f...``; never a literal in the source."""
    return "-".join(("t17", label, secrets.token_hex(12)))


def leaks(text: str, markers: Iterable[str] = ()) -> list[str]:
    """Every FR-3.7 leak shape or marker found in ``text``."""
    found = [m.group(0) for p in LEAK_PATTERNS for m in p.finditer(text)]
    return found + [marker for marker in markers if marker and marker in text]


def iter_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[Any]:
    """Every leaf route with its full path, through included routers.

    FastAPI 0.143 keeps ``include_router`` lazy: ``app.routes`` holds ``_IncludedRouter`` wrappers
    (no ``path``/``methods``), so a flat walk of ``app.routes`` finds no route at all. This walks
    ``original_router.routes`` recursively and yields ``(full_path, route)`` pairs.
    """
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            yield from iter_routes(inner.routes, prefix + getattr(context, "prefix", ""))
        elif isinstance(getattr(route, "path", None), str):
            yield prefix + route.path, route


class ScanningClient(TestClient):
    """A ``TestClient`` that asserts no response body leaks a host, port, path or sentinel."""

    def __init__(self, app: FastAPI, markers: list[str], **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self.markers = markers
        self.scanned = 0

    def request(self, *args: Any, **kwargs: Any) -> Any:
        response = super().request(*args, **kwargs)
        found = leaks(response.text, self.markers)
        assert found == [], f"response body leaks {found}: {response.text[:300]}"
        self.scanned += 1
        return response


class ApiRig:
    """One app over a fake factory, with its token, devices and a scanning client."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        ids: Iterable[int] = (0,),
        manifest: Manifest | None = None,
        factory: FakeTransportFactory | None = None,
    ) -> None:
        monkeypatch.delenv(TRANSPORT_ENV_VAR, raising=False)
        self.manifest = manifest if manifest is not None else registry_manifest(tmp_path, ids=ids)
        self.bearer_env = self.manifest.auth.bearer_token_ref
        self.bearer = sentinel("token")
        self.wrong_bearer = sentinel("wrong")
        monkeypatch.setenv(self.bearer_env, self.bearer)
        self.factory = factory if factory is not None else FakeTransportFactory()
        self.app = create_app(self.manifest, self.factory)
        self.markers = [self.bearer, self.wrong_bearer]
        self.auth = {"Authorization": f"Bearer {self.bearer}"}

    def client(self, **kwargs: Any) -> ScanningClient:
        """A scanning client; use it as a context manager to run the lifespan."""
        return ScanningClient(self.app, self.markers, **kwargs)

    def dev(self, cid: int = 0) -> FakeTecDevice:
        return self.factory.device(cid)

    def mark(self) -> None:
        """Forget the writes so far (startup identification reads, earlier requests)."""
        for cid in (c.id for c in self.manifest.controllers):
            self.dev(cid).writes.clear()

    def commands(self, cid: int = 0) -> list[str]:
        return self.dev(cid).commands

    def error(self, response: Any) -> dict[str, Any]:
        """The unified error body's ``error`` object (fails if the body is not unified)."""
        body = response.json()
        assert set(body) == {"error"}, body
        detail: dict[str, Any] = body["error"]
        assert isinstance(detail["kind"], str) and isinstance(detail["message"], str)
        return detail

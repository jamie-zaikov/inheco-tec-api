"""The routes of the API match the route-contract snapshot (FR-3.4, design C-13).

``contract/api-routes.json`` is the route contract that this API shares with its n8n node repo:
the node tests read the snapshot instead of this repo's ``routes.py``. This module is the API half
of that check. It builds the shipped app with ``create_app`` (fake transport, a test token; the
lifespan is not entered, so no device is opened) and compares the snapshot with the functional
routes of the app, in both directions:

- the route set is every leaf route of the app, walked through the included routers;
- the health route, the authenticated OpenAPI route and the public Swagger UI routes are not
  functional routes and are left out, as the snapshot leaves them out;
- every other leaf must be a FastAPI ``APIRoute``: a mount, a plain Starlette route or a websocket
  route fails the test, so a route in another form cannot slip past it;
- a route gives one entry per HTTP method: the method, the full path template and the sorted
  query parameter names that the app's own OpenAPI document lists for that operation (FastAPI's
  own classification: model query parameters are flattened and aliases are applied). A functional
  route that is missing from the OpenAPI document fails the test.

The snapshot itself must be in its canonical form (routes sorted by path then method, query names
sorted, two-space indent, ASCII only, one trailing newline), so the node repo and this repo read
the same bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from inheco_tec_api._vendor.instrument_api_core import HEALTH_PATH
from inheco_tec_api.app import OPENAPI_PATH
from tec_api_helpers import PUBLIC_DOC_PATHS, ApiRig, iter_routes

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = REPO_ROOT / "contract" / "api-routes.json"
SCHEMA_VERSION = 1

NON_FUNCTIONAL_PATHS: frozenset[str] = frozenset({HEALTH_PATH, OPENAPI_PATH}) | PUBLIC_DOC_PATHS
"""The health, OpenAPI and Swagger UI routes: outside the route contract."""

# One contract entry: (path template, HTTP method, sorted query names).
Entry = tuple[str, str, tuple[str, ...]]


def _render(entries: list[Entry]) -> str:
    """The canonical snapshot text of ``entries``."""
    body = {
        "schema": SCHEMA_VERSION,
        "routes": [
            {"method": method, "path": path, "query": sorted(query)}
            for path, method, query in sorted(entries)
        ],
    }
    return json.dumps(body, indent=2, ensure_ascii=True) + "\n"


def _snapshot_entries() -> list[Entry]:
    """The entries of the committed snapshot; any other shape fails."""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "the snapshot is not a JSON object"
    assert set(data) == {"schema", "routes"}, f"snapshot keys: {sorted(data)}"
    assert data["schema"] == SCHEMA_VERSION, f"unknown snapshot schema {data['schema']!r}"
    routes = data["routes"]
    assert isinstance(routes, list) and routes, "the snapshot lists no route"
    entries: list[Entry] = []
    for item in routes:
        assert isinstance(item, dict), f"a snapshot route is not an object: {item!r}"
        assert set(item) == {"method", "path", "query"}, f"snapshot route keys: {item!r}"
        method, path, query = item["method"], item["path"], item["query"]
        assert isinstance(method, str) and isinstance(path, str), f"wrong types: {item!r}"
        assert isinstance(query, list), f"query is not a list: {item!r}"
        assert all(isinstance(name, str) for name in query), f"query names: {item!r}"
        entries.append((path, method, tuple(query)))
    return entries


def _app_entries(rig: ApiRig) -> tuple[list[Entry], set[str]]:
    """The functional-route entries of the app, and the non-functional paths it serves."""
    schema_paths: dict[str, Any] = rig.app.openapi()["paths"]
    entries: list[Entry] = []
    skipped: set[str] = set()
    for full_path, route in iter_routes(rig.app.routes):
        if full_path in NON_FUNCTIONAL_PATHS:
            skipped.add(full_path)
            continue
        form = type(route).__name__
        assert isinstance(route, APIRoute), f"{full_path}: unread route form {form}"
        prefix = full_path[: len(full_path) - len(route.path)]
        operations = schema_paths.get(prefix + route.path_format)
        assert operations is not None, f"{full_path}: the route is not in the OpenAPI document"
        assert route.methods, f"{full_path}: the route has no HTTP method"
        for method in sorted(route.methods):
            operation = operations.get(method.lower())
            assert operation is not None, f"{method} {full_path}: no OpenAPI operation"
            names = sorted(
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter.get("in") == "query"
            )
            entries.append((full_path, method, tuple(names)))
    return entries, skipped


@pytest.fixture
def app_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Entry], set[str]]:
    return _app_entries(ApiRig(tmp_path, monkeypatch))


def test_snapshot_is_in_canonical_form() -> None:
    entries = _snapshot_entries()
    assert len(set(entries)) == len(entries), "the snapshot lists a route twice"
    for path, method, query in entries:
        assert list(query) == sorted(set(query)), f"{method} {path}: query names not sorted/unique"
    canonical = _render(entries)
    assert SNAPSHOT.read_text(encoding="utf-8") == canonical, "the snapshot is not canonical"


def test_every_snapshot_route_is_served_by_the_app(
    app_entries: tuple[list[Entry], set[str]],
) -> None:
    served = set(app_entries[0])
    missing = [entry for entry in _snapshot_entries() if entry not in served]
    assert missing == [], f"snapshot routes the app does not serve (method, path, query): {missing}"


def test_every_functional_route_of_the_app_is_in_the_snapshot(
    app_entries: tuple[list[Entry], set[str]],
) -> None:
    entries = app_entries[0]
    assert entries, "the app serves no functional route"
    assert len(set(entries)) == len(entries), "the app serves a route twice"
    listed = set(_snapshot_entries())
    extra = [entry for entry in entries if entry not in listed]
    assert extra == [], f"app routes the snapshot does not list (method, path, query): {extra}"


def test_the_left_out_paths_are_routes_of_the_app(
    app_entries: tuple[list[Entry], set[str]],
) -> None:
    # Each left-out path names a route the app serves, so the exclusion hides no renamed route.
    assert app_entries[1] == set(NON_FUNCTIONAL_PATHS)

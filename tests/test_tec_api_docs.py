"""Swagger UI page tests (``/docs``; user decision 2026-10-12: public page, token for calls).

The page, its init script and the error cases go through :class:`ScanningClient`, so those bodies
are also scanned for the FR-3.7 leak shapes and the sentinel token. The two vendored Swagger UI
assets are fixed third-party text that contains example addresses (``127.0.0.1``) and CSS values
in the ``x:NN`` shape, so they are fetched with a plain ``TestClient`` and compared byte for byte
with the packaged files instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from inheco_tec_api.http.docs import (
    DOCS_ASSET_CSS,
    DOCS_ASSET_JS,
    DOCS_CSP,
    DOCS_INIT_PATH,
    DOCS_PATH,
    docs_spec,
    js_literal,
)
from tec_api_helpers import PUBLIC_DOC_PATHS, ApiRig

FILE_MARKERS = ("Apache License", "swagger-ui-dist", "SwaggerUIBundle", "def ", "import ")


def assert_no_file_content(text: str) -> None:
    """No vendored file, source file or page text in an error body (FR-18.12)."""
    assert not [m for m in FILE_MARKERS if m in text], text[:200]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


def _embedded_spec(init_js: str) -> dict[str, object]:
    match = re.search(r"spec: (\{.*\}),\n  dom_id", init_js, re.S)
    assert match is not None
    loaded: dict[str, object] = json.loads(match.group(1))
    return loaded


def test_docs_page_is_public_html_with_strict_headers(rig: ApiRig) -> None:
    response = rig.client().get(DOCS_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == DOCS_CSP
    assert "script-src 'self';" in DOCS_CSP
    assert "unsafe-eval" not in DOCS_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    scripts = re.findall(r"<script[^>]*>", response.text)
    assert scripts and all('src="/docs/' in tag for tag in scripts)  # no inline script
    assert "unpkg" not in response.text and "cdn" not in response.text.lower()


def test_docs_init_embeds_the_spec_with_bearer_scheme(rig: ApiRig) -> None:
    response = rig.client().get(DOCS_INIT_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-store"
    assert "SwaggerUIBundle(" in response.text
    assert "persistAuthorization: false" in response.text
    assert rig.bearer not in response.text
    spec = _embedded_spec(response.text)
    assert spec == docs_spec(rig.app.openapi())
    paths = spec["paths"]
    assert isinstance(paths, dict)
    assert "/v1/status" in paths and "/healthz" in paths
    assert not PUBLIC_DOC_PATHS & set(paths)
    assert spec["security"] == [{"bearerAuth": []}]
    assert paths["/healthz"]["get"]["security"] == []


def test_docs_spec_asks_for_the_token_once_not_per_operation(rig: ApiRig) -> None:
    source = rig.app.openapi()
    per_op = [
        p
        for item in source["paths"].values()
        for op in item.values()
        for p in op.get("parameters", [])
        if p.get("in") == "header" and p.get("name", "").lower() == "authorization"
    ]
    assert per_op  # FastAPI lists the token header on each operation of /openapi.json
    spec = docs_spec(source)
    for path, item in spec["paths"].items():
        for method, op in item.items():
            names = [(p.get("in"), p.get("name", "").lower()) for p in op.get("parameters", [])]
            assert ("header", "authorization") not in names, (method, path)
            assert "security" not in op or path == "/healthz", (method, path)
    target = spec["paths"]["/v1/controllers/{cid}/slots/{sid}/target"]["put"]
    assert {p["name"] for p in target["parameters"]} == {"cid", "sid"}
    assert spec["security"] == [{"bearerAuth": []}]


def test_openapi_json_keeps_token_and_content(rig: ApiRig) -> None:
    client = rig.client()
    assert client.get("/openapi.json").status_code == 401
    spec = client.get("/openapi.json", headers=rig.auth).json()
    assert spec == rig.app.openapi()
    assert "securitySchemes" not in spec.get("components", {})
    assert not PUBLIC_DOC_PATHS & set(spec["paths"])


def test_docs_spec_does_not_change_its_input(rig: ApiRig) -> None:
    schema = rig.app.openapi()
    before = json.dumps(schema, sort_keys=True)
    docs_spec(schema)
    assert json.dumps(schema, sort_keys=True) == before


_PINNED_SHA256 = {
    "swagger-ui-bundle.js": "6b3bfcaff3fc82c0e6992ca4b370c4f22970dec35fff60d229302b3ee7688715",
    "swagger-ui.css": "1ac324f7dcd27e4b9386b4bd6421271ec147e922a22c05ba24b11515e9aa6321",
}


@pytest.mark.parametrize(
    ("path", "name", "media"),
    [
        (DOCS_ASSET_JS, "swagger-ui-bundle.js", "text/javascript"),
        (DOCS_ASSET_CSS, "swagger-ui.css", "text/css"),
    ],
)
def test_docs_assets_are_the_packaged_files(rig: ApiRig, path: str, name: str, media: str) -> None:
    """FR-3.7 as scoped (OQ-9 (a)): the two vendored bodies skip the scan, so they are pinned."""
    response = TestClient(rig.app).get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == f"{media}; charset=utf-8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "public, max-age=3600"
    packaged = resources.files("inheco_tec_api").joinpath("docs_assets", name).read_bytes()
    assert response.content == packaged
    assert hashlib.sha256(response.content).hexdigest() == _PINNED_SHA256[name]
    assert len(packaged) > 10_000


@pytest.mark.parametrize(("name", "digest"), sorted(_PINNED_SHA256.items()))
def test_vendored_assets_match_the_pinned_sha256(name: str, digest: str) -> None:
    """The secret scan skips the bundle (one minified line), so its bytes are pinned here."""
    data = resources.files("inheco_tec_api").joinpath("docs_assets", name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    readme = resources.files("inheco_tec_api").joinpath("docs_assets", "README.md").read_text()
    assert digest in readme


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_fr18_8_license_and_notice_ship_in_the_package(name: str) -> None:
    asset = resources.files("inheco_tec_api").joinpath("docs_assets", name)
    assert asset.is_file()
    assert asset.read_bytes().strip()


@pytest.mark.parametrize(
    "path",
    [
        "/redoc",
        "/docs/oauth2-redirect",
        "/docs/assets/LICENSE",
        "/docs/assets/NOTICE",
        "/docs/assets/README.md",
        "/docs/assets/other.js",
        "/docs/assets",
        "/docs/assets/",
        "/docs/assets/%2e%2e/%2e%2e/app.py",
        "/docs/assets/%2e%2e%2fLICENSE",
        "/docs/assets/..%2Fapp.py",
    ],
)
def test_no_other_docs_path_exists(rig: ApiRig, path: str) -> None:
    """FR-18.12: exactly 404 ``not_found`` with and without a token, never a file (no 401)."""
    client = rig.client()  # scanning: the error bodies stay under the FR-3.7 scan
    for headers in ({}, rig.auth):
        response = client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code == 404, (path, headers != {})
        assert rig.error(response)["kind"] == "not_found"
        assert_no_file_content(response.text)


def test_docs_routes_touch_no_device(rig: ApiRig) -> None:
    """FR-18.2: all four routes answer 200 without a token and write nothing to the device."""
    with TestClient(rig.app) as client:  # plain client: the asset bodies skip the scan (FR-3.7)
        rig.mark()
        for path in (DOCS_PATH, DOCS_INIT_PATH, DOCS_ASSET_JS, DOCS_ASSET_CSS):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "authorization" not in {k.lower() for k in response.request.headers}
        assert rig.commands() == []


@pytest.mark.parametrize("path", [DOCS_PATH, DOCS_INIT_PATH, DOCS_ASSET_JS, DOCS_ASSET_CSS])
def test_fr18_11_every_docs_route_sets_nosniff_and_no_referrer(rig: ApiRig, path: str) -> None:
    response = TestClient(rig.app).get(path)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    expected = "no-store" if path in (DOCS_PATH, DOCS_INIT_PATH) else "public, max-age=3600"
    assert response.headers["cache-control"] == expected


def test_fr3_7_page_and_init_bodies_stay_on_the_scanning_client(rig: ApiRig) -> None:
    client = rig.client()
    for path in (DOCS_PATH, DOCS_INIT_PATH):
        assert client.get(path).status_code == 200
    assert client.scanned == 2


def test_js_literal_cannot_close_a_script() -> None:
    text = js_literal({"a": "</script><b>&  "})
    assert "</" not in text and "<" not in text and ">" not in text and "&" not in text
    assert " " not in text and " " not in text
    assert json.loads(text) == {"a": "</script><b>&  "}

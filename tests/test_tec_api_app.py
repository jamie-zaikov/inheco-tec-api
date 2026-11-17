"""App factory, auth, health, lifespan and OpenAPI tests (Task 17; design C12, C13).

Split from ``test_tec_api_controller.py`` (17.6) to keep each file under 500 lines. Every request
goes through :class:`ScanningClient`, so every response body here is also scanned for an IPv4
address, ``host:port``, a USB/HID path and the sentinel tokens (FR-3.7, NFR-1). The fake transport
and the in-process ``TestClient`` only; no network port, no real ``hid``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import inheco_tec_api.app as app_module
import pytest
from fastapi.routing import APIRoute
from inheco_tec_api._vendor import instrument_api_core
from inheco_tec_api._vendor.instrument_api_core import AuthConfigError
from inheco_tec_api.manifest import TRANSPORT_ENV_VAR, Manifest
from inheco_tec_api.state_store import ReleaseStateStore
from inheco_tec_api.transport.fake import FakeTransportFactory
from structlog.testing import capture_logs
from tec_api_helpers import PUBLIC_DOC_PATHS, ApiRig, ScanningClient, iter_routes, leaks
from tec_manifest_helpers import base_manifest


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


def _functional_routes(rig: ApiRig) -> list[tuple[str, str]]:
    """(method, concrete path) of every route except ``/healthz`` and the public docs routes."""
    pairs = []
    for path, route in iter_routes(rig.app.routes):
        methods = getattr(route, "methods", None)
        if not methods or path == "/healthz" or path in PUBLIC_DOC_PATHS:
            continue
        for method in sorted(methods - {"HEAD"}):
            pairs.append((method, path.replace("{cid}", "0").replace("{sid}", "1")))
    return pairs


# -- FR-3.4: fail-closed token ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_fr_3_4_create_app_fails_closed_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    if value is None:
        monkeypatch.delenv(rig.bearer_env)
    else:
        monkeypatch.setenv(rig.bearer_env, value)
    built: list[object] = []
    monkeypatch.setattr(app_module, "ControllerRegistry", lambda *a, **k: built.append(a))
    with pytest.raises(AuthConfigError):
        app_module.create_app(rig.manifest, FakeTransportFactory())
    assert built == []  # no registry (and so no device or state store) before the token check


def test_fr_3_4_token_read_from_env_var_named_by_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    other = rig.manifest.model_copy(
        update={"auth": rig.manifest.auth.model_copy(update={"bearer_token_ref": "T17_OTHER_REF"})}
    )
    monkeypatch.setenv("T17_OTHER_REF", rig.wrong_bearer)
    app = app_module.create_app(other, FakeTransportFactory())
    client = ScanningClient(app, rig.markers)
    assert client.get("/v1/status", headers=rig.auth).status_code == 401
    ok = client.get("/v1/status", headers={"Authorization": f"Bearer {rig.wrong_bearer}"})
    assert ok.status_code == 200


# -- FR-3.5 / FR-17.3 / NFR-6: bearer auth -----------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [None, "Bearer {wrong}", "{good}", "Basic {good}", "Bearer {good}x", "Bearer "],
    ids=["missing", "wrong", "no-scheme", "basic", "suffix", "empty"],
)
def test_fr_3_5_every_functional_route_requires_token(rig: ApiRig, header: str | None) -> None:
    routes = _functional_routes(rig)
    paths = {path for _, path in routes}
    assert {"/openapi.json", "/v1/status", "/v1/stop-all"} <= paths
    assert "/v1/controllers/0/emergency-off" in paths  # FR-17.3
    headers = {}
    if header is not None:
        headers["Authorization"] = header.format(good=rig.bearer, wrong=rig.wrong_bearer)
    with rig.client() as client:
        rig.mark()
        for method, path in routes:
            response = client.request(method, path, headers=headers)
            assert response.status_code == 401, (method, path)
            error = rig.error(response)
            assert error["kind"] == "unauthorized"
            assert error["message"] == "missing or invalid bearer token"
        assert rig.commands() == []  # no request reached the device


def test_fr_3_5_unauthenticated_emergency_off_with_valid_body_is_401(rig: ApiRig) -> None:
    with rig.client() as client:
        rig.mark()
        response = client.post("/v1/controllers/0/emergency-off", json={"confirm": True})
        assert response.status_code == 401
        assert rig.commands() == []
        assert (
            client.get("/v1/status", headers=rig.auth).json()["controllers"][0][
                "emergency_off_active"
            ]
            is False
        )


def test_nfr_6_auth_uses_core_constant_time_verifier(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, str]] = []
    real = instrument_api_core.verify_bearer_token

    def spy(header: str | None, expected: str) -> None:
        calls.append((header, expected))
        real(header, expected)

    monkeypatch.setattr(app_module, "verify_bearer_token", spy)
    client = rig.client()
    assert client.get("/v1/status", headers=rig.auth).status_code == 200
    assert calls == [(f"Bearer {rig.bearer}", rig.bearer)]


# -- FR-3.6: /healthz ----------------------------------------------------------------------------


def test_fr_3_6_healthz_exact_body_without_auth_and_no_device_io(rig: ApiRig) -> None:
    with rig.client() as client:
        rig.mark()
        opens = rig.dev().open_count
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert rig.commands() == []
        assert rig.dev().open_count == opens


def test_fr_3_6_healthz_ignores_a_wrong_token_and_needs_no_startup(rig: ApiRig) -> None:
    response = rig.client().get("/healthz", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert rig.dev().open_count == 0
    assert rig.commands() == []


# -- NFR-1: the token never appears in a log or a response --------------------------------------


def test_nfr_1_token_never_logged_or_returned(
    rig: ApiRig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    wrong = {"Authorization": f"Bearer {rig.wrong_bearer}"}

    def requests(client: ScanningClient) -> None:
        client.get("/v1/status", headers=rig.auth)
        client.get("/v1/status", headers=wrong)
        client.post("/v1/controllers/0/connect", headers=rig.auth)
        client.post("/v1/controllers/0/emergency-off", headers=wrong, json={"confirm": True})
        client.get("/openapi.json", headers=rig.auth)

    with rig.client() as client:
        requests(client)  # rendered logs go to stdout / the log handlers
        with capture_logs() as events:
            requests(client)  # the raw event dicts
    assert client.scanned == 10  # each body was scanned for both tokens
    assert any(e["event"] == "http_request" for e in events)
    out = capsys.readouterr()
    for text in (repr(events), caplog.text, out.out, out.err):
        assert rig.bearer not in text
        assert rig.wrong_bearer not in text
    assert rig.bearer_env not in repr(events)  # the ref is not logged either


def test_fr_3_7_leak_scanner_detects_each_shape() -> None:
    for text in (
        "at 192.0.2.10 now",
        "host:8743",
        "[::1]:8743",
        r"\\?\hid#vid_1234&pid_5678",
        "/dev/hidraw3",
        "on COM3",
    ):
        assert leaks(text), text
    clean = '{"controllers":[{"controller_id":0,"target_c":37.0,"max_c":70.0,"code":"7000179"}]}'
    assert leaks(clean) == []
    assert leaks("x t17-token-ab y", ["t17-token-ab"]) == ["t17-token-ab"]


# -- C13 lifespan (T12-C1, T14-E, T11-C2) --------------------------------------------------------


def test_lifespan_runs_startup_and_shutdown_once(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = rig.app.state.registry
    calls: list[tuple[str, tuple[Any, ...]]] = []
    real_startup, real_shutdown = registry.startup, registry.shutdown

    def startup(*args: Any) -> None:
        calls.append(("startup", args))
        real_startup(*args)

    def shutdown(*args: Any) -> Any:
        calls.append(("shutdown", args))
        return real_shutdown(*args)

    monkeypatch.setattr(registry, "startup", startup)
    monkeypatch.setattr(registry, "shutdown", shutdown)
    client = rig.client()
    assert calls == []  # building the app and a client starts nothing
    with client:
        assert calls == [("startup", ())]
        assert (
            client.get("/v1/status", headers=rig.auth).json()["controllers"][0]["state"]
            == "connected"
        )
    assert calls == [("startup", ()), ("shutdown", ())]  # default deadline (shutdown_timeout_s)
    assert registry.stopping
    assert not rig.dev().is_open


def test_lifespan_uses_one_state_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[ReleaseStateStore] = []
    real_init = ReleaseStateStore.__init__

    def counting_init(self: ReleaseStateStore, *args: Any, **kwargs: Any) -> None:
        made.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(ReleaseStateStore, "__init__", counting_init)
    rig = ApiRig(tmp_path, monkeypatch, ids=(0, 1))
    with rig.client() as client:
        client.post("/v1/controllers/0/release", headers=rig.auth)
        client.post("/v1/controllers/1/release", headers=rig.auth)
        client.post("/v1/controllers/0/reacquire", headers=rig.auth)
    assert len(made) == 1
    registry = rig.app.state.registry
    assert all(m._store is made[0] for m in registry.managers.values())  # noqa: SLF001


def test_injected_state_store_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    store = ReleaseStateStore(tmp_path / "injected.json")
    app = app_module.create_app(rig.manifest, FakeTransportFactory(), state_store=store)
    assert app.state.registry.state_store is store


# -- T8-S: the default factory comes only from the manifest / INHECO_TEC_TRANSPORT ----------------


def _hidapi_manifest(tmp_path: Path) -> Manifest:
    data = base_manifest()
    data["service"]["state_file"] = str(tmp_path / "state.json")
    return Manifest.model_validate(data)


class _NoHidFactory:
    """Stands in for ``HidapiTransportFactory`` so no test can reach the real ``hid``."""

    is_fake = False
    built = 0

    def __init__(self) -> None:
        type(self).built += 1

    def create(self, controller_id: int) -> Any:  # pragma: no cover - must never be called
        raise AssertionError("the real HID transport must not be created")


@pytest.fixture
def no_hid(monkeypatch: pytest.MonkeyPatch) -> type[_NoHidFactory]:
    _NoHidFactory.built = 0
    monkeypatch.setattr(app_module, "HidapiTransportFactory", _NoHidFactory)
    return _NoHidFactory


def test_t8s_hidapi_manifest_with_env_fake_gets_the_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_hid: type[_NoHidFactory]
) -> None:
    rig = ApiRig(tmp_path, monkeypatch, manifest=_hidapi_manifest(tmp_path))
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "fake")
    app = app_module.create_app(rig.manifest)
    assert no_hid.built == 0
    registry = app.state.registry
    assert isinstance(registry._factory, FakeTransportFactory)  # noqa: SLF001
    body = ScanningClient(app, rig.markers).get("/v1/status", headers=rig.auth).json()
    assert body["controllers"][0]["transport_fake"] is True


def test_t8s_fake_manifest_without_env_gets_the_fake(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch, no_hid: type[_NoHidFactory]
) -> None:
    app = app_module.create_app(rig.manifest)
    assert no_hid.built == 0
    with ScanningClient(app, rig.markers) as client:
        body = client.get("/v1/status", headers=rig.auth).json()
    assert [c["transport_fake"] for c in body["controllers"]] == [True]
    assert body["controllers"][0]["state"] == "connected"


@pytest.mark.parametrize("env", [None, "", "hidapi"])
def test_t8s_hidapi_manifest_gets_hidapi_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_hid: type[_NoHidFactory],
    env: str | None,
) -> None:
    rig = ApiRig(tmp_path, monkeypatch, manifest=_hidapi_manifest(tmp_path))
    if env is not None:
        monkeypatch.setenv(TRANSPORT_ENV_VAR, env)
    app = app_module.create_app(rig.manifest)
    assert no_hid.built == 1
    assert isinstance(app.state.registry._factory, _NoHidFactory)  # noqa: SLF001


def test_t8s_fake_manifest_with_env_hidapi_gets_hidapi(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch, no_hid: type[_NoHidFactory]
) -> None:
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "hidapi")
    data = rig.manifest.model_dump(by_alias=True)
    data["transport"].update(pre_send_delay_ms=200, retry_delay_ms=1000)
    app_module.create_app(Manifest.model_validate(data))
    assert no_hid.built == 1


def test_t8s_injected_factory_is_used_as_given(rig: ApiRig, no_hid: type[_NoHidFactory]) -> None:
    assert rig.app.state.registry._factory is rig.factory  # noqa: SLF001
    assert no_hid.built == 0


def test_t8s_no_route_exposes_fake_internals(rig: ApiRig) -> None:
    paths = [path for path, _ in iter_routes(rig.app.routes)]
    assert "/v1/status" in paths
    for word in ("script", "mainboard", "fake", "debug", "raw", "command"):
        assert not any(word in p.lower() for p in paths), (word, paths)
    spec = rig.client().get("/openapi.json", headers=rig.auth).json()
    names = set(spec["components"]["schemas"])
    for schema in spec["components"]["schemas"].values():
        names |= set(schema.get("properties", {}))
    assert not names & {"script", "clear_script", "mainboard", "command", "raw"}


# -- FR-9.4 / FR-10.2: OpenAPI -------------------------------------------------------------------


def test_fr_9_4_redoc_and_oauth2_redirect_are_off(rig: ApiRig) -> None:
    client = rig.client()
    for path in ("/redoc", "/docs/oauth2-redirect"):
        for headers in ({}, rig.auth):
            response = client.get(path, headers=headers)
            assert response.status_code == 404
            assert rig.error(response)["kind"] == "not_found"


def test_fr_9_4_openapi_requires_auth(rig: ApiRig) -> None:
    response = rig.client().get("/openapi.json")
    assert response.status_code == 401
    assert rig.error(response)["kind"] == "unauthorized"


_EXPECTED_STATUSES = {
    ("/v1/status", "get"): {401, 500},
    ("/v1/controllers/{cid}/connect", "post"): {401, 404, 409, 422, 500, 502, 503, 504},
    ("/v1/controllers/{cid}/release", "post"): {401, 404, 422, 500, 503},
    ("/v1/controllers/{cid}/reacquire", "post"): {401, 404, 422, 500, 502, 503, 504},
    ("/v1/controllers/{cid}/emergency-off", "post"): {401, 404, 409, 422, 500, 502, 503, 504},
    ("/v1/stop-all", "post"): {401, 409, 500, 502, 503},
}


def test_fr_9_4_openapi_documents_error_responses(rig: ApiRig) -> None:
    response = rig.client().get("/openapi.json", headers=rig.auth)
    assert response.status_code == 200
    assert "HTTPValidationError" not in response.text
    spec = response.json()
    assert "ErrorResponse" in spec["components"]["schemas"]
    assert "ValidationError" not in spec["components"]["schemas"]
    assert "/healthz" in spec["paths"]
    assert "/openapi.json" not in spec["paths"]
    for (path, method), statuses in _EXPECTED_STATUSES.items():
        responses = spec["paths"][path][method]["responses"]
        errors = {int(s) for s in responses if not s.startswith("2")}
        assert statuses <= errors, (path, method, statuses - errors)
        for status in errors:
            ref = responses[str(status)]["content"]["application/json"]["schema"]["$ref"]
            assert ref == "#/components/schemas/ErrorResponse", (path, status)
            assert responses[str(status)]["description"].startswith("error kind: ")
    eo = spec["paths"]["/v1/controllers/{cid}/emergency-off"]["post"]["responses"]["409"]
    assert "stop_not_delivered" in eo["description"]
    assert "unauthorized" in spec["paths"]["/v1/status"]["get"]["responses"]["401"]["description"]


_CLOSED_BODIES = {
    ("/v1/controllers/{cid}/emergency-off", "post"): ("EmergencyOffRequest", {"confirm"}),
    ("/v1/controllers/{cid}/slots/{sid}/target", "put"): ("SetTargetRequest", {"temperature_c"}),
}


def test_fr_10_2_only_closed_request_bodies(rig: ApiRig) -> None:
    spec = rig.app.openapi()
    bodies = {}
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            if "requestBody" in op:
                ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                name = ref.rsplit("/", 1)[1]
                schema = spec["components"]["schemas"][name]
                assert schema.get("additionalProperties") is False, (path, method)
                bodies[(path, method)] = (name, set(schema["properties"]))
    assert bodies == _CLOSED_BODIES


def test_routes_are_plain_def_handlers(rig: ApiRig) -> None:
    routes = [r for p, r in iter_routes(rig.app.routes) if p.startswith("/v1")]
    assert all(isinstance(r, APIRoute) for r in routes)
    assert {(r.path, m) for r in routes for m in r.methods} >= {
        (p, m.upper()) for p, m in _EXPECTED_STATUSES
    }
    assert not any(inspect.iscoroutinefunction(r.endpoint) for r in routes)

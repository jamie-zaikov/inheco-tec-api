"""Exception-handler and request-log tests of the app (Task 17; design C12, C14).

Split from ``test_tec_api_controller.py`` (17.6) to keep each file under 500 lines. Every handler
path must render the unified body ``{"error": {...}}``: domain errors (``Retry-After`` kept on
``controller_busy``), schema errors, FastAPI's own 400 parse error, an unknown path, a wrong method
(``Allow`` kept) and the 500 fallback. The 500 log carries the exception type and frames only;
an exception-text sentinel must appear in neither the body nor any log. Every response body is
scanned by :class:`ScanningClient` (FR-3.7).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import ControllerBusy
from inheco_tec_api.registry import ServiceStopping
from inheco_tec_api.state_store import StateStoreError
from structlog.testing import capture_logs
from tec_api_helpers import ApiRig, sentinel


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


def _raise(exc: BaseException) -> Any:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return boom


# -- domain errors (T16-E) -------------------------------------------------------------------


def test_fr_9_4_controller_busy_is_503_with_retry_after(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = rig.app.state.service
    monkeypatch.setattr(service, "connect", _raise(ControllerBusy(0, "lock wait expired")))
    response = rig.client().post("/v1/controllers/0/connect", headers=rig.auth)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    error = rig.error(response)
    assert error["kind"] == "controller_busy"
    assert error["controller_id"] == 0


def test_fr_9_4_service_stopping_is_503(rig: ApiRig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rig.app.state.service, "stop_all", _raise(ServiceStopping()))
    response = rig.client().post("/v1/stop-all", headers=rig.auth)
    assert response.status_code == 503
    assert rig.error(response)["kind"] == "service_stopping"


def test_fr_9_4_requests_after_shutdown_are_service_stopping(rig: ApiRig) -> None:
    client = rig.client()
    with client:
        pass  # startup, then shutdown
    response = client.post("/v1/controllers/0/connect", headers=rig.auth)
    assert response.status_code == 503
    assert rig.error(response)["kind"] == "service_stopping"
    assert not rig.dev().is_open


def test_fr_9_4_state_store_error_is_fixed_500_without_text(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = sentinel("store")
    rig.markers.append(hidden)
    monkeypatch.setattr(rig.app.state.service, "release", _raise(StateStoreError(hidden)))
    with capture_logs() as events:
        response = rig.client().post("/v1/controllers/0/release", headers=rig.auth)
    assert response.status_code == 500
    error = rig.error(response)
    assert error["kind"] == "internal_error"
    assert error["failure"] == "state_store"
    logged = [e for e in events if e["event"] == "http_internal_error"]
    assert len(logged) == 1
    assert logged[0]["error"] == "StateStoreError"
    assert logged[0]["route"] == "/v1/controllers/{cid}/release"
    assert hidden not in repr(events)


# -- the 500 fallback (T16-S) --------------------------------------------------------------------


def test_fr_9_4_unexpected_exception_is_fixed_500_type_and_frames_only(
    rig: ApiRig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    hidden = sentinel("boom")
    rig.markers.append(hidden)
    monkeypatch.setattr(rig.app.state.service, "status", _raise(RuntimeError(hidden)))
    # raise_server_exceptions stays True: the middleware must answer, not re-raise.
    with capture_logs() as events:
        response = rig.client().get("/v1/status", headers=rig.auth)
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "kind": "internal_error",
            "message": "internal error",
            "code": None,
            "code_description": None,
            "failure": None,
            "attempts": None,
            "controller_id": None,
            "slot_id": None,
            "effective_limits": None,
            "slots": None,
        }
    }
    logged = [e for e in events if e["event"] == "http_internal_error"]
    assert len(logged) == 1
    assert logged[0]["log_level"] == "error"
    assert logged[0]["error"] == "RuntimeError"
    assert logged[0]["method"] == "GET"
    assert logged[0]["route"] == "/v1/status"
    assert "boom" in logged[0]["stack"]  # the frame of the raising function
    assert "File " in logged[0]["stack"]
    out = capsys.readouterr()
    for text in (repr(events), caplog.text, out.out, out.err):
        assert hidden not in text
    request = [e for e in events if e["event"] == "http_request"]
    assert [e["status_class"] for e in request] == ["5xx"]


def test_fr_9_4_unexpected_exception_on_a_controller_route(
    rig: ApiRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = sentinel("value")
    rig.markers.append(hidden)
    monkeypatch.setattr(rig.app.state.service, "connect", _raise(KeyError(hidden)))
    with capture_logs() as events:
        response = rig.client().post("/v1/controllers/0/connect", headers=rig.auth)
    assert response.status_code == 500
    assert rig.error(response)["message"] == "internal error"
    assert hidden not in repr(events)


# -- schema and framework errors (T16-S) ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/controllers/abc/connect", "/v1/controllers/1.5/release"])
def test_fr_6_5_non_integer_id_is_unified_422(rig: ApiRig, path: str) -> None:
    response = rig.client().post(path, headers=rig.auth)
    assert response.status_code == 422
    error = rig.error(response)
    assert error["kind"] == "validation_error"
    assert "abc" not in error["message"] and "1.5" not in error["message"]  # input not echoed


def test_fr_3_7_validation_error_does_not_echo_input(rig: ApiRig) -> None:
    marker = sentinel("input")
    rig.markers.append(marker)
    response = rig.client().post(
        "/v1/controllers/0/emergency-off", headers=rig.auth, json={"confirm": marker}
    )
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"


def test_fr_9_4_unparseable_huge_int_body_is_unified_422(rig: ApiRig) -> None:
    body = '{"confirm": ' + "9" * 5000 + "}"
    with rig.client() as client:
        rig.mark()
        response = client.post(
            "/v1/controllers/0/emergency-off",
            headers={**rig.auth, "Content-Type": "application/json"},
            content=body,
        )
        assert response.status_code == 422
        error = rig.error(response)
        assert error["kind"] == "validation_error"
        assert "9999" not in response.text
        assert rig.commands() == []


@pytest.mark.parametrize("body", ['{"confirm": ', "not json", "[" * 3000 + "]" * 3000])
def test_fr_9_4_malformed_json_body_is_unified_422(rig: ApiRig, body: str) -> None:
    with rig.client() as client:
        rig.mark()
        response = client.post(
            "/v1/controllers/0/emergency-off",
            headers={**rig.auth, "Content-Type": "application/json"},
            content=body,
        )
        assert response.status_code == 422
        assert rig.error(response)["kind"] == "validation_error"
        assert rig.commands() == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer x"}, None])
def test_fr_9_4_unknown_path_is_unified_404(rig: ApiRig, headers: dict[str, str] | None) -> None:
    response = rig.client().get(
        "/v1/nothing/here", headers=rig.auth if headers is None else headers
    )
    assert response.status_code == 404
    error = rig.error(response)
    assert error["kind"] == "not_found"
    assert "nothing" not in error["message"]


def test_fr_9_4_wrong_method_is_unified_405_with_allow(rig: ApiRig) -> None:
    response = rig.client().get("/v1/stop-all", headers=rig.auth)
    assert response.status_code == 405
    assert "POST" in response.headers["Allow"]
    error = rig.error(response)
    assert error["kind"] == "validation_error"
    assert error["message"] == "method not allowed"


# -- C14: one http_request event per request, flat fields only (carry-forward T2) ----------------


def test_c14_http_request_event_has_flat_fields_only(rig: ApiRig) -> None:
    wrong = {"Authorization": f"Bearer {rig.wrong_bearer}", "X-Custom": "t17-header"}
    with rig.client() as client, capture_logs() as events:
        client.get("/v1/status", headers={**rig.auth, "X-Custom": "t17-header"})
        client.post("/v1/controllers/0/connect", headers=wrong)
        client.post("/v1/controllers/9/release", headers=rig.auth)
        client.get("/healthz")
        client.get("/v1/unknown", headers=rig.auth)
    requests = [e for e in events if e["event"] == "http_request"]
    assert [(e["method"], e["route"], e["status_class"]) for e in requests] == [
        ("GET", "/v1/status", "2xx"),
        ("POST", "/v1/controllers/{cid}/connect", "4xx"),
        ("POST", "/v1/controllers/{cid}/release", "4xx"),
        ("GET", "/healthz", "2xx"),
        ("GET", None, "4xx"),
    ]
    for event in requests:
        assert set(event) == {"event", "log_level", "method", "route", "status_class"}
        assert event["log_level"] == "info"
        assert all(not isinstance(v, dict | list | tuple) for v in event.values())
    text = repr(events)
    for forbidden in ("t17-header", "authorization", "Authorization", "/controllers/9/"):
        assert forbidden not in text

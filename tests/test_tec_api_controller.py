"""Controller-level route tests (Task 17.6; design C12 endpoints table, C11).

``TestClient`` (in process, no port) over ``create_app`` with an injected fake factory. Every
response body is scanned for IPv4 / ``host:port`` / USB-path shapes and the sentinel tokens by
:class:`ScanningClient` (FR-3.7). App factory, auth and OpenAPI tests are in
``test_tec_api_app.py``; exception handlers and the request log in ``test_tec_api_errors.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.transport.fake_actions import FirmwareCode, Match, OpenFailure
from structlog.testing import capture_logs
from tec_api_helpers import ApiRig, ScanningClient


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    """A started app (lifespan entered) with the startup writes forgotten."""
    with rig.client() as started:
        rig.mark()
        yield started


def _status(rig: ApiRig, client: ScanningClient, cid: int = 0) -> dict[str, Any]:
    body = client.get("/v1/status", headers=rig.auth).json()
    return next(c for c in body["controllers"] if c["controller_id"] == cid)


def _post(rig: ApiRig, client: ScanningClient, path: str, **kwargs: Any) -> Any:
    return client.post(path, headers=rig.auth, **kwargs)


# -- FR-6.6: status ----------------------------------------------------------------------------


def test_fr_6_6_status_shape(rig: ApiRig, client: ScanningClient) -> None:
    response = client.get("/v1/status", headers=rig.auth)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"controllers"}
    (controller,) = body["controllers"]
    assert set(controller) == {
        "controller_id",
        "state",
        "transport_fake",
        "emergency_off_active",
        "slots",
    }
    assert controller["controller_id"] == 0
    assert controller["state"] == "connected"
    assert controller["transport_fake"] is True
    assert controller["emergency_off_active"] is False
    assert [s["slot_id"] for s in controller["slots"]] == [1, 2]
    for slot in controller["slots"]:
        assert set(slot) == {
            "slot_id",
            "configured_class",
            "detected_device_type",
            "detected_article_number",
            "variant",
            "effective_limits",
            "control_permitted",
            "not_permitted_reason",
            "startup_state",
        }
        assert slot["configured_class"] == "cpac"
    assert rig.commands() == []  # status does no device I/O


def test_fr_6_6_status_without_startup_reports_disconnected(rig: ApiRig) -> None:
    body = rig.client().get("/v1/status", headers=rig.auth).json()
    assert [(c["state"], c["transport_fake"]) for c in body["controllers"]] == [
        ("disconnected", True)
    ]
    assert rig.dev().open_count == 0


def test_fr_6_6_status_lists_every_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch, ids=(0, 3))
    rig.dev(3).present = False
    with rig.client() as client:
        body = client.get("/v1/status", headers=rig.auth).json()
    assert [(c["controller_id"], c["state"]) for c in body["controllers"]] == [
        (0, "connected"),
        (3, "disconnected"),
    ]


# -- FR-6.2: connect ---------------------------------------------------------------------------


def test_fr_6_2_connect_on_connected_is_idempotent(rig: ApiRig, client: ScanningClient) -> None:
    opens = rig.dev().open_count
    response = _post(rig, client, "/v1/controllers/0/connect")
    assert response.status_code == 200
    body = response.json()
    assert body["controller_id"] == 0
    assert body["state"] == "connected"
    assert body["emergency_off_cleared"] is False
    assert body["stopped_slots"] == []
    assert [s["slot_id"] for s in body["slots"]] == [1, 2]
    assert rig.dev().open_count == opens
    assert "ATE0" not in "".join(rig.commands())


def test_fr_6_2_connect_opens_a_disconnected_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    rig.dev().present = False
    with rig.client() as client:
        assert _status(rig, client)["state"] == "disconnected"
        rig.dev().present = True
        response = _post(rig, client, "/v1/controllers/0/connect")
        assert response.status_code == 200
        assert response.json()["state"] == "connected"
        assert rig.dev().is_open
        assert _status(rig, client)["state"] == "connected"


def test_fr_6_2_connect_device_missing_is_an_error_not_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    rig.dev().present = False
    with rig.client() as client:
        response = _post(rig, client, "/v1/controllers/0/connect")
        assert response.status_code >= 400
        assert rig.error(response)["controller_id"] == 0
        assert _status(rig, client)["state"] == "disconnected"


# -- FR-12.1 / FR-12.2 / FR-12.3 / FR-12.5: release and reacquire --------------------------------


def test_release_closes_handle_and_is_idempotent(rig: ApiRig, client: ScanningClient) -> None:
    response = _post(rig, client, "/v1/controllers/0/release")
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "state": "released"}
    assert not rig.dev().is_open
    assert rig.commands() == []  # no ATE0 or other write on release
    again = _post(rig, client, "/v1/controllers/0/release")
    assert again.status_code == 200
    assert again.json() == {"controller_id": 0, "state": "released"}
    assert _status(rig, client)["state"] == "released"


def test_fr_12_2_connect_while_released_refused_without_io(
    rig: ApiRig, client: ScanningClient
) -> None:
    _post(rig, client, "/v1/controllers/0/release")
    opens = rig.dev().open_count
    response = _post(rig, client, "/v1/controllers/0/connect")
    assert response.status_code == 409
    error = rig.error(response)
    assert error["kind"] == "controller_released"
    assert error["controller_id"] == 0
    assert rig.dev().open_count == opens
    assert rig.commands() == []
    assert _status(rig, client)["state"] == "released"


def test_reacquire_reopens_without_ate0(rig: ApiRig, client: ScanningClient) -> None:
    _post(rig, client, "/v1/controllers/0/release")
    response = _post(rig, client, "/v1/controllers/0/reacquire")
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "state": "connected",
        "stopped_slots": [],
        "emergency_off_cleared": False,
    }
    assert rig.dev().is_open
    assert not any("ATE" in c for c in rig.commands())
    assert _status(rig, client)["state"] == "connected"


def test_reacquire_of_connected_is_idempotent(rig: ApiRig, client: ScanningClient) -> None:
    opens = rig.dev().open_count
    response = _post(rig, client, "/v1/controllers/0/reacquire")
    assert response.status_code == 200
    assert response.json()["state"] == "connected"
    assert response.json()["emergency_off_cleared"] is False
    assert rig.dev().open_count == opens
    assert rig.commands() == []


def test_reacquire_after_emergency_off_reports_cleared(rig: ApiRig, client: ScanningClient) -> None:
    eo = _post(rig, client, "/v1/controllers/0/emergency-off", json={"confirm": True})
    assert eo.status_code == 200
    _post(rig, client, "/v1/controllers/0/release")
    response = _post(rig, client, "/v1/controllers/0/reacquire")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "connected"
    assert body["emergency_off_cleared"] is True
    assert body["stopped_slots"] == [1, 2]
    assert _status(rig, client)["emergency_off_active"] is False


# -- FR-17.1 / FR-17.2: emergency-off ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"json": {}},
        {"json": {"confirm": False}},
        {"json": {"confirm": "true"}},
        {"json": {"confirm": 1}},
        {"json": {"confirm": None}},
        {"json": {"confirm": True, "command": "0AEO"}},
        {"json": [True]},
    ],
    ids=["no-body", "empty", "false", "string", "int", "null", "extra-field", "list"],
)
def test_fr_17_2_emergency_off_without_strict_true_is_422_without_io(
    rig: ApiRig, client: ScanningClient, kwargs: dict[str, object]
) -> None:
    opens = rig.dev().open_count
    response = _post(rig, client, "/v1/controllers/0/emergency-off", **kwargs)
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"
    assert rig.commands() == []
    assert rig.dev().open_count == opens
    assert _status(rig, client)["emergency_off_active"] is False


def test_fr_17_1_emergency_off_true_sends_0aeo(rig: ApiRig, client: ScanningClient) -> None:
    with capture_logs() as events:
        response = _post(rig, client, "/v1/controllers/0/emergency-off", json={"confirm": True})
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "emergency_off_active": True}
    assert rig.commands()[0] == "0AEO"
    assert all(c in ("0AEO", "0RDC1") for c in rig.commands())
    assert _status(rig, client)["emergency_off_active"] is True
    assert any(e["event"] == "tec_emergency_off" for e in events)


def test_fr_17_4_emergency_off_while_released_open_failure_is_409(
    rig: ApiRig, client: ScanningClient
) -> None:
    _post(rig, client, "/v1/controllers/0/release")
    rig.dev().script(action=OpenFailure())
    response = _post(rig, client, "/v1/controllers/0/emergency-off", json={"confirm": True})
    assert response.status_code == 409
    error = rig.error(response)
    assert error["kind"] == "stop_not_delivered"
    assert error["message"].startswith("Emergency-off was NOT delivered")
    assert rig.commands() == []
    assert _status(rig, client)["state"] == "released"


# -- FR-6.5: ID validation (422 before 404) ------------------------------------------------------


@pytest.mark.parametrize("action", ["connect", "release", "reacquire", "emergency-off"])
@pytest.mark.parametrize(("cid", "status"), [(8, 422), (-1, 422), (99, 422), (1, 404), (7, 404)])
def test_fr_6_5_controller_id_range_before_configured(
    rig: ApiRig, client: ScanningClient, action: str, cid: int, status: int
) -> None:
    kwargs = {"json": {"confirm": True}} if action == "emergency-off" else {}
    response = _post(rig, client, f"/v1/controllers/{cid}/{action}", **kwargs)
    assert response.status_code == status
    error = rig.error(response)
    assert error["kind"] == ("validation_error" if status == 422 else "not_found")
    assert rig.commands() == []


# -- FR-8.6 / FR-12.7: stop-all ----------------------------------------------------------------


def test_fr_8_6_stop_all_200_sends_only_ate0(rig: ApiRig, client: ScanningClient) -> None:
    response = _post(rig, client, "/v1/stop-all")
    assert response.status_code == 200
    outcomes = response.json()["outcomes"]
    assert [(o["controller_id"], o["slot_id"], o["outcome"]) for o in outcomes] == [
        (0, 1, "stopped"),
        (0, 2, "stopped"),
    ]
    assert [c for c in rig.commands() if c != "0RDC1"] == ["1ATE0", "2ATE0"]


def test_fr_12_7_stop_all_released_open_failure_is_409(rig: ApiRig, client: ScanningClient) -> None:
    _post(rig, client, "/v1/controllers/0/release")
    rig.dev().script(action=OpenFailure())
    with capture_logs() as events:
        response = _post(rig, client, "/v1/stop-all")
    assert response.status_code == 409
    error = rig.error(response)
    assert error["kind"] == "stop_not_delivered"
    assert error["message"].startswith("Stop was NOT delivered")
    assert [(s["controller_id"], s["slot_id"], s["outcome"]) for s in error["slots"]] == [
        (0, 1, "not_delivered"),
        (0, 2, "not_delivered"),
    ]
    assert rig.commands() == []
    assert _status(rig, client)["state"] == "released"
    assert any(e["log_level"] == "error" for e in events)


def test_fr_12_6_stop_all_while_released_delivers_and_stays_released(
    rig: ApiRig, client: ScanningClient
) -> None:
    _post(rig, client, "/v1/controllers/0/release")
    response = _post(rig, client, "/v1/stop-all")
    assert response.status_code == 200
    assert {o["outcome"] for o in response.json()["outcomes"]} == {"stopped"}
    assert [c for c in rig.commands() if c != "0RDC1"] == ["1ATE0", "2ATE0"]
    assert not rig.dev().is_open
    assert _status(rig, client)["state"] == "released"


def test_fr_8_6_stop_all_firmware_failure_is_502(rig: ApiRig, client: ScanningClient) -> None:
    rig.dev().script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("2"), times=None)
    response = _post(rig, client, "/v1/stop-all")
    assert response.status_code == 502
    error = rig.error(response)
    assert error["kind"] == "stop_all_failed"
    rows = {s["slot_id"]: s["outcome"] for s in error["slots"]}
    assert rows == {1: "failed", 2: "stopped"}  # every slot attempted even if one fails
    assert "2ATE0" in rig.commands()


def test_fr_8_6_stop_all_mixed_controllers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = ApiRig(tmp_path, monkeypatch, ids=(0, 1))
    with rig.client() as client:
        _post(rig, client, "/v1/controllers/1/release")
        rig.mark()
        rig.dev(1).script(action=OpenFailure())
        response = _post(rig, client, "/v1/stop-all")
        assert response.status_code == 409
        rows = {
            (s["controller_id"], s["slot_id"]): s["outcome"] for s in rig.error(response)["slots"]
        }
        assert rows == {
            (0, 1): "stopped",
            (0, 2): "stopped",
            (1, 1): "not_delivered",
            (1, 2): "not_delivered",
        }
        assert [c for c in rig.commands(0) if c != "0RDC1"] == ["1ATE0", "2ATE0"]
        assert rig.commands(1) == []

"""Slot route tests (Task 18.2; design C12 endpoints table, C11, C9).

``TestClient`` (in process, no port) over ``create_app`` with an injected fake factory. Every
response body is scanned for IPv4 / ``host:port`` / USB-path shapes and the sentinel tokens by
:class:`ScanningClient` (FR-3.7). Set-target limits and the out-of-bound body fallback (T16-V1)
are in ``test_tec_api_slot_target.py``; the controller queries in ``test_tec_api_query.py``;
cross-cutting conformance in ``test_tec_api_conformance.py``.

Covered here: each slot route writes its exact command string and returns the decoded body
(FR-8.1, FR-8.4, FR-8.5, FR-8.7 – FR-8.9); ID 422 before 404 (FR-6.5); selector and tolerance
bounds with zero writes; the released / emergency-off / not-permitted gates; the stop safety paths
(FR-12.6, FR-12.7, T13-S3); firmware and transport failures (FR-9.1, FR-9.2).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.transport.fake_actions import (
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
)
from tec_api_helpers import ApiRig, ScanningClient

RDC = "0RDC1"
UNMAPPED_ARTICLE = "7000999"
SLOT = "/v1/controllers/{cid}/slots/{sid}"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    """A started app (lifespan entered) with the startup writes forgotten."""
    with rig.client() as started:
        rig.mark()
        yield started


def _slot(cid: int = 0, sid: int = 1, tail: str = "") -> str:
    return SLOT.format(cid=cid, sid=sid) + tail


def _call(rig: ApiRig, client: ScanningClient, method: str, path: str, **kw: Any) -> Any:
    return client.request(method, path, headers=rig.auth, **kw)


# Every slot route: (method, tail, kwargs). Set target uses a valid in-range body.
SLOT_ROUTES = [
    ("PUT", "/target", {"json": {"temperature_c": 37.0}}),
    ("GET", "/target", {}),
    ("POST", "/start", {}),
    ("POST", "/stop", {}),
    ("GET", "/temperature", {}),
    ("GET", "/at-target", {}),
]
SLOT_IDS = [f"{m} {t}" for m, t, _ in SLOT_ROUTES]
CONTROL_ROUTES = [("PUT", "/target", {"json": {"temperature_c": 37.0}}), ("POST", "/start", {})]
READ_ROUTES = [
    ("GET", "/v1/controllers/0/slots/1/target"),
    ("GET", "/v1/controllers/0/slots/1/temperature"),
    ("GET", "/v1/controllers/0/slots/1/at-target"),
    ("GET", "/v1/controllers/0/sensor-values?selector=0"),
    ("GET", "/v1/controllers/0/device-type?slot=1"),
    ("GET", "/v1/controllers/0/firmware-version?selector=1"),
]


# -- FR-8.1 / FR-8.4 / FR-8.5 / FR-8.8: exact command and decoded body ------------------------


def test_fr_8_1_set_target_writes_stt_and_returns_body(rig: ApiRig, client: ScanningClient) -> None:
    response = _call(rig, client, "PUT", _slot(tail="/target"), json={"temperature_c": 37.0})
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "slot_id": 1, "target_c": 37.0, "warnings": []}
    assert rig.commands() == ["1STT370", RDC]
    assert rig.dev().slots[1].target == 370


def test_fr_8_8_get_target_writes_rtt(rig: ApiRig, client: ScanningClient) -> None:
    rig.dev().slots[2].target = 455
    response = _call(rig, client, "GET", _slot(sid=2, tail="/target"))
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "slot_id": 2, "temperature_c": 45.5}
    assert rig.commands() == ["2RTT", RDC]


def test_fr_8_4_start_writes_ate1(rig: ApiRig, client: ScanningClient) -> None:
    response = _call(rig, client, "POST", _slot(tail="/start"))
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "slot_id": 1}
    assert rig.commands() == ["1ATE1", RDC]
    assert rig.dev().slots[1].enabled is True


def test_fr_8_5_stop_writes_ate0(rig: ApiRig, client: ScanningClient) -> None:
    rig.dev().slots[2].enabled = True
    response = _call(rig, client, "POST", _slot(sid=2, tail="/stop"))
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "slot_id": 2}
    assert rig.commands() == ["2ATE0", RDC]
    assert rig.dev().slots[2].enabled is False


# -- FR-8.7: actual temperature --------------------------------------------------------------


@pytest.mark.parametrize(("query", "selector"), [("", 3), ("?selector=1", 1), ("?selector=2", 2)])
def test_fr_8_7_temperature_writes_rat_with_selector(
    rig: ApiRig, client: ScanningClient, query: str, selector: int
) -> None:
    rig.dev().slots[1].actual = 371
    response = _call(rig, client, "GET", _slot(tail="/temperature" + query))
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "slot_id": 1,
        "temperature_c": 37.1,
        "selector": selector,
    }
    assert rig.commands() == [f"1RAT{selector}", RDC]


@pytest.mark.parametrize("query", ["?selector=0", "?selector=4", "?selector=-1", "?selector=x"])
def test_fr_8_7_temperature_selector_out_of_range_is_422_without_io(
    rig: ApiRig, client: ScanningClient, query: str
) -> None:
    response = _call(rig, client, "GET", _slot(tail="/temperature" + query))
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"
    assert rig.commands() == []


# -- FR-8.9: at-target -----------------------------------------------------------------------


def _at_target(rig: ApiRig, client: ScanningClient, query: str = "") -> Any:
    return _call(rig, client, "GET", _slot(tail="/at-target" + query))


def test_fr_8_9_at_target_reads_rat3_and_rtt_with_default_tolerance(
    rig: ApiRig, client: ScanningClient
) -> None:
    rig.dev().slots[1].actual, rig.dev().slots[1].target = 250, 260  # |delta| = 1.0 = default
    response = _at_target(rig, client)
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "slot_id": 1,
        "at_target": True,
        "actual_c": 25.0,
        "target_c": 26.0,
        "tolerance_c": 1.0,
        "selector": 3,
    }
    assert rig.commands() == ["1RAT3", RDC, "1RTT", RDC]


@pytest.mark.parametrize(
    ("target", "query", "expected", "tolerance"),
    [
        (251, "?tolerance_c=0.1", True, 0.1),  # |delta| = tolerance at the lower bound
        (252, "?tolerance_c=0.1", False, 0.1),  # just above
        (300, "?tolerance_c=5.0", True, 5.0),  # |delta| = tolerance at the upper bound
        (301, "?tolerance_c=5", False, 5.0),
        (261, "", False, 1.0),  # just above the configured default
        (239, "?tolerance_c=1.1", True, 1.1),  # actual above target
    ],
)
def test_fr_8_9_at_target_tolerance_edges(
    rig: ApiRig,
    client: ScanningClient,
    target: int,
    query: str,
    expected: bool,
    tolerance: float,
) -> None:
    rig.dev().slots[1].actual, rig.dev().slots[1].target = 250, target
    body = _at_target(rig, client, query).json()
    assert (body["at_target"], body["tolerance_c"]) == (expected, tolerance)


@pytest.mark.parametrize(
    "value", ["nan", "inf", "-inf", "1e400", "0.05", "0.09", "5.1", "5.01", "0", "-1", "abc", ""]
)
def test_fr_8_9_at_target_tolerance_out_of_bounds_is_422_without_io(
    rig: ApiRig, client: ScanningClient, value: str
) -> None:
    response = _at_target(rig, client, f"?tolerance_c={value}")
    assert response.status_code == 422
    error = rig.error(response)
    assert error["kind"] == "validation_error"
    assert error["effective_limits"] is None
    assert rig.commands() == []


# -- FR-6.5: 422 (range) before 404 (not configured) -----------------------------------------


@pytest.mark.parametrize(("method", "tail", "kw"), SLOT_ROUTES, ids=SLOT_IDS)
@pytest.mark.parametrize(
    ("cid", "sid", "status"),
    [(8, 1, 422), (-1, 1, 422), (0, 0, 422), (0, 7, 422), (1, 9, 422), (8, 3, 422),
     (1, 1, 404), (7, 2, 404), (0, 3, 404), (0, 6, 404)],
)  # fmt: skip
def test_fr_6_5_slot_route_ids_range_before_configured(
    rig: ApiRig,
    client: ScanningClient,
    method: str,
    tail: str,
    kw: dict[str, Any],
    cid: int,
    sid: int,
    status: int,
) -> None:
    response = _call(rig, client, method, _slot(cid, sid, tail), **kw)
    assert response.status_code == status
    assert rig.error(response)["kind"] == ("validation_error" if status == 422 else "not_found")
    assert rig.commands() == []


# -- gates: released, emergency-off, not permitted (C9) --------------------------------------


@pytest.mark.parametrize(("method", "tail", "kw"), CONTROL_ROUTES, ids=["set-target", "start"])
def test_control_while_released_is_409_without_io(
    rig: ApiRig, client: ScanningClient, method: str, tail: str, kw: dict[str, Any]
) -> None:
    _call(rig, client, "POST", "/v1/controllers/0/release")
    opens = rig.dev().open_count
    response = _call(rig, client, method, _slot(tail=tail), **kw)
    assert response.status_code == 409
    error = rig.error(response)
    assert (error["kind"], error["controller_id"]) == ("controller_released", 0)
    assert rig.commands() == []
    assert rig.dev().open_count == opens


@pytest.mark.parametrize(("method", "path"), READ_ROUTES)
def test_reads_while_released_are_409_without_io(
    rig: ApiRig, client: ScanningClient, method: str, path: str
) -> None:
    _call(rig, client, "POST", "/v1/controllers/0/release")
    response = _call(rig, client, method, path)
    assert response.status_code == 409
    assert rig.error(response)["kind"] == "controller_released"
    assert rig.commands() == []


@pytest.mark.parametrize(("method", "tail", "kw"), CONTROL_ROUTES, ids=["set-target", "start"])
def test_control_while_emergency_off_is_409_without_io(
    rig: ApiRig, client: ScanningClient, method: str, tail: str, kw: dict[str, Any]
) -> None:
    eo = _call(rig, client, "POST", "/v1/controllers/0/emergency-off", json={"confirm": True})
    assert eo.status_code == 200
    rig.mark()
    response = _call(rig, client, method, _slot(tail=tail), **kw)
    assert response.status_code == 409
    error = rig.error(response)
    assert (error["kind"], error["controller_id"]) == ("emergency_off_active", 0)
    assert rig.commands() == []


@pytest.mark.parametrize(("method", "path"), READ_ROUTES)
def test_reads_and_stop_allowed_while_emergency_off(
    rig: ApiRig, client: ScanningClient, method: str, path: str
) -> None:
    _call(rig, client, "POST", "/v1/controllers/0/emergency-off", json={"confirm": True})
    rig.mark()
    assert _call(rig, client, method, path).status_code == 200
    assert rig.commands() and rig.commands()[-1] == RDC
    stop = _call(rig, client, "POST", _slot(tail="/stop"))
    assert stop.status_code == 200
    assert rig.commands()[-2:] == ["1ATE0", RDC]


@pytest.fixture
def np_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    """Slot 2 carries an unmapped article number, so it is not permitted (FR-7.5)."""
    rig = ApiRig(tmp_path, monkeypatch)
    rig.dev().slots[2].article_number = UNMAPPED_ARTICLE
    return rig


@pytest.mark.parametrize(("method", "tail", "kw"), CONTROL_ROUTES, ids=["set-target", "start"])
def test_control_on_not_permitted_slot_is_409_with_reason(
    np_rig: ApiRig, method: str, tail: str, kw: dict[str, Any]
) -> None:
    with np_rig.client() as client:
        np_rig.mark()
        response = _call(np_rig, client, method, _slot(sid=2, tail=tail), **kw)
        assert response.status_code == 409
        error = np_rig.error(response)
        assert (error["kind"], error["controller_id"], error["slot_id"]) == (
            "slot_not_permitted",
            0,
            2,
        )
        assert error["failure"] and error["failure"] != "unknown"  # the reason is named
        assert error["failure"] in error["message"]
        assert np_rig.commands() == []
        ok = _call(np_rig, client, method, _slot(sid=1, tail=tail), **kw)
        assert ok.status_code == 200  # the permitted slot is unaffected


def test_fr_8_5_stop_on_not_permitted_slot_still_writes_ate0(np_rig: ApiRig) -> None:
    with np_rig.client() as client:
        np_rig.mark()
        response = _call(np_rig, client, "POST", _slot(sid=2, tail="/stop"))
        assert response.status_code == 200
        assert response.json() == {"controller_id": 0, "slot_id": 2}
        assert np_rig.commands() == ["2ATE0", RDC]


# -- stop safety: released, not delivered, failed stop never 200 (FR-12.6, FR-12.7, T13-S3) ----


def test_fr_12_6_stop_while_released_writes_only_ate0_and_stays_released(
    rig: ApiRig, client: ScanningClient
) -> None:
    _call(rig, client, "POST", "/v1/controllers/0/release")
    response = _call(rig, client, "POST", _slot(tail="/stop"))
    assert response.status_code == 200
    assert response.json() == {"controller_id": 0, "slot_id": 1}
    assert rig.commands() == ["1ATE0", RDC]
    assert not rig.dev().is_open
    status = _call(rig, client, "GET", "/v1/status").json()
    assert status["controllers"][0]["state"] == "released"


def test_fr_12_7_stop_while_released_open_failure_is_409_not_delivered(
    rig: ApiRig, client: ScanningClient
) -> None:
    _call(rig, client, "POST", "/v1/controllers/0/release")
    rig.dev().script(action=OpenFailure())
    response = _call(rig, client, "POST", _slot(tail="/stop"))
    assert response.status_code == 409
    error = rig.error(response)
    assert error["kind"] == "stop_not_delivered"
    assert error["message"].startswith("Stop was NOT delivered")
    assert [(s["slot_id"], s["outcome"]) for s in error["slots"]] == [(1, "not_delivered")]
    assert rig.commands() == []


@pytest.mark.parametrize("released", [False, True], ids=["connected", "released"])
@pytest.mark.parametrize(
    ("action", "status", "kind", "failure"),
    [
        (FirmwareCode("5"), 502, "firmware_error", "5"),
        (FirmwareCode("W"), 502, "firmware_error", "W"),
        (NoReply(), 504, "transport_error", "no_reply"),
        (DeviceLoss(), 504, "transport_error", "device_lost"),
    ],
    ids=["fw-5", "fw-W", "no-reply", "device-lost"],
)
def test_t13_s3_failed_stop_is_never_200(
    rig: ApiRig,
    client: ScanningClient,
    released: bool,
    action: Any,
    status: int,
    kind: str,
    failure: str,
) -> None:
    if released:
        _call(rig, client, "POST", "/v1/controllers/0/release")
    rig.dev().script(match=Match(mnemonic="ATE", address=1), action=action, times=None)
    response = _call(rig, client, "POST", _slot(tail="/stop"))
    assert response.status_code == status
    error = rig.error(response)
    assert error["kind"] == kind
    assert [(s["slot_id"], s["outcome"], s["failure"]) for s in error["slots"]] == [
        (1, "failed", failure)
    ]
    assert "state is unknown" in error["message"]


# -- FR-9.1 / FR-9.2: device failures on slot and query routes --------------------------------


@pytest.mark.parametrize(
    ("method", "path", "mnemonic"),
    [
        ("GET", "/v1/controllers/0/slots/1/target", "RTT"),
        ("POST", "/v1/controllers/0/slots/1/start", "ATE"),
        ("GET", "/v1/controllers/0/slots/1/temperature", "RAT"),
        ("GET", "/v1/controllers/0/sensor-values?selector=2", "RHV"),
        ("GET", "/v1/controllers/0/device-type?slot=3", "RTD"),
        ("GET", "/v1/controllers/0/firmware-version?selector=0", "RFV"),
    ],
)
def test_fr_9_route_failures_map_to_502_and_504(
    rig: ApiRig, client: ScanningClient, method: str, path: str, mnemonic: str
) -> None:
    rig.dev().script(match=Match(mnemonic=mnemonic), action=FirmwareCode("C"), times=1)
    firmware = _call(rig, client, method, path)
    assert firmware.status_code == 502
    error = rig.error(firmware)
    assert (error["kind"], error["code"]) == ("firmware_error", "C")
    assert error["code_description"] == "Housing temperature not OK"
    rig.dev().clear_script()
    rig.dev().script(match=Match(mnemonic=mnemonic), action=NoReply(), times=None)
    transport = _call(rig, client, method, path)
    assert transport.status_code == 504
    error = rig.error(transport)
    assert (error["kind"], error["failure"], error["code"]) == ("transport_error", "no_reply", None)
    assert error["attempts"] == 3

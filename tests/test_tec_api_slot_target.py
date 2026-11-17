"""Set-target route tests: effective limits, condensation warning and the out-of-bound body
fallback (Task 18.2; FR-8.1, FR-8.2, FR-8.3; carry-forward T15-C1, T16-V1, T16-C(e)).

Split from ``test_tec_api_slot.py`` to keep each file under 500 lines. ``TestClient`` (in
process, no port) over ``create_app`` with an injected fake factory; every response body is
scanned by :class:`ScanningClient` (FR-3.7). The CPAC slots of the test manifest have the
effective limits 4.0–70.0 °C (manifest window, ``RLT`` 40, ``RMT1`` 700).

T16-V1: the schema rejects a finite ``|temperature_c|`` above 1000 and a JSON integer too large for
a float. FR-8.2 requires that 422 to name the effective limits, so the route answers it with the
set-target operation's own refusal (the limits, or an earlier gate refusal), with zero writes.
Every other schema error keeps the plain schema 422.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tec_api_helpers import ApiRig, ScanningClient

RDC = "0RDC1"
LIMITS = {"min_c": 4.0, "max_c": 70.0}
TARGET = "/v1/controllers/0/slots/1/target"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    with rig.client() as started:
        rig.mark()
        yield started


def _put(rig: ApiRig, client: ScanningClient, path: str = TARGET, **kw: Any) -> Any:
    return client.put(path, headers=rig.auth, **kw)


def _put_raw(rig: ApiRig, client: ScanningClient, body: str, path: str = TARGET) -> Any:
    """Send ``body`` verbatim as JSON (a number ``json=`` cannot encode, e.g. a 400-digit int)."""
    return client.put(path, headers={**rig.auth, **JSON}, content=body)


def _assert_limits_422(rig: ApiRig, response: Any) -> dict[str, Any]:
    assert response.status_code == 422, response.text
    error = rig.error(response)
    assert error["kind"] == "validation_error"
    assert error["effective_limits"] == LIMITS
    assert (error["controller_id"], error["slot_id"]) == (0, 1)
    assert "4.0" in error["message"] and "70.0" in error["message"]
    assert rig.commands() == []
    return error


# -- FR-8.1 / FR-8.2: the effective window at the exact integer-tenths edges -----------------


@pytest.mark.parametrize(
    ("value", "sent", "target_c"),
    [
        (4.0, "1STT40", 4.0),
        (3.95, "1STT40", 4.0),  # encodes to 40 tenths (half away from zero): inside
        (70.0, "1STT700", 70.0),
        (70.04, "1STT700", 70.0),  # encodes to 700 tenths: inside
        (37, "1STT370", 37.0),  # a JSON integer is accepted
        (36.66, "1STT367", 36.7),
        (12.0, "1STT120", 12.0),
    ],
)
def test_fr_8_1_target_inside_limits_at_tenths_edges(
    rig: ApiRig, client: ScanningClient, value: float, sent: str, target_c: float
) -> None:
    response = _put(rig, client, json={"temperature_c": value})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["controller_id"], body["slot_id"], body["target_c"]) == (0, 1, target_c)
    assert rig.commands() == [sent, RDC]


@pytest.mark.parametrize("value", [3.94, 3.9, 70.05, 70.1, 0.0, -5.0, 100.0, 1000, -1000.0])
def test_fr_8_2_target_outside_limits_is_422_with_limits_without_io(
    rig: ApiRig, client: ScanningClient, value: float
) -> None:
    _assert_limits_422(rig, _put(rig, client, json={"temperature_c": value}))
    assert rig.dev().slots[1].target == 0


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_fr_8_2_non_finite_target_is_422_with_limits(
    rig: ApiRig, client: ScanningClient, token: str
) -> None:
    response = _put_raw(rig, client, '{"temperature_c": ' + token + "}")
    assert response.status_code == 422
    error = rig.error(response)
    assert error["effective_limits"] == LIMITS
    assert rig.commands() == []


# -- FR-8.3: condensation warning ------------------------------------------------------------


def test_fr_8_3_target_below_12_has_warning_and_is_sent(
    rig: ApiRig, client: ScanningClient
) -> None:
    with capture_logs() as events:
        response = _put(rig, client, json={"temperature_c": 11.9})
    assert response.status_code == 200
    body = response.json()
    assert body["target_c"] == 11.9
    assert [w["kind"] for w in body["warnings"]] == ["condensation_risk"]
    assert all(isinstance(w["message"], str) and w["message"] for w in body["warnings"])
    assert rig.commands() == ["1STT119", RDC]  # the warning does not block the command
    warnings = [e for e in events if e["event"] == "tec_condensation_warning"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"


def test_fr_8_3_target_of_12_has_no_warning(rig: ApiRig, client: ScanningClient) -> None:
    response = _put(rig, client, json={"temperature_c": 12.0})
    assert response.json()["warnings"] == []


# -- T16-V1 / T15-C1: out-of-bound magnitudes get the FR-8.2 422 with limits -----------------


@pytest.mark.parametrize("value", [1e30, -1e30, 1e300, 1000.1, -1001, 10**20])
def test_t16_v1_out_of_bound_float_is_422_with_limits(
    rig: ApiRig, client: ScanningClient, value: float
) -> None:
    error = _assert_limits_422(rig, _put(rig, client, json={"temperature_c": value}))
    assert "outside the effective limits" in error["message"]


@pytest.mark.parametrize(
    "number",
    ["9" * 400, "-" + "9" * 30, "1" + "0" * 309, "123456789012345678901234567890"],
    ids=["400-digit", "neg-30-digit", "1e309-int", "30-digit"],
)
def test_t16_v1_json_int_too_large_for_float_is_422_with_limits(
    rig: ApiRig, client: ScanningClient, number: str
) -> None:
    response = _put_raw(rig, client, '{"temperature_c": ' + number + "}")
    _assert_limits_422(rig, response)
    assert number[-12:] not in response.text  # the input is not echoed


def test_t15_c1_out_of_bound_is_never_500(rig: ApiRig, client: ScanningClient) -> None:
    for body in ('{"temperature_c": 1e30}', '{"temperature_c": 1e26}', '{"temperature_c": 1e27}'):
        response = _put_raw(rig, client, body)
        assert response.status_code == 422, body
    assert rig.commands() == []


def test_t16_v1_out_of_bound_while_released_is_409(rig: ApiRig, client: ScanningClient) -> None:
    client.post("/v1/controllers/0/release", headers=rig.auth)
    opens = rig.dev().open_count
    for body in ('{"temperature_c": 1e30}', '{"temperature_c": -' + "9" * 30 + "}"):
        response = _put_raw(rig, client, body)
        assert response.status_code == 409
        assert rig.error(response)["kind"] == "controller_released"
    assert rig.commands() == []
    assert rig.dev().open_count == opens


def test_t16_v1_out_of_bound_while_emergency_off_is_409(
    rig: ApiRig, client: ScanningClient
) -> None:
    client.post("/v1/controllers/0/emergency-off", headers=rig.auth, json={"confirm": True})
    rig.mark()
    response = _put(rig, client, json={"temperature_c": 1e30})
    assert response.status_code == 409
    assert rig.error(response)["kind"] == "emergency_off_active"
    assert rig.commands() == []


def test_t16_v1_out_of_bound_on_not_permitted_slot_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    rig.dev().slots[2].article_number = "7000999"
    with rig.client() as client:
        rig.mark()
        response = _put(
            rig, client, "/v1/controllers/0/slots/2/target", json={"temperature_c": 1e30}
        )
        assert response.status_code == 409
        assert rig.error(response)["kind"] == "slot_not_permitted"
        assert rig.commands() == []


def test_t16_v1_out_of_bound_unauthenticated_is_401(rig: ApiRig, client: ScanningClient) -> None:
    for headers in ({}, {"Authorization": f"Bearer {rig.wrong_bearer}"}):
        response = client.put(TARGET, headers=headers, json={"temperature_c": 1e30})
        assert response.status_code == 401
        assert rig.error(response)["kind"] == "unauthorized"
        raw = client.put(
            TARGET, headers={**headers, **JSON}, content='{"temperature_c": ' + "9" * 400 + "}"
        )
        assert raw.status_code == 401
    assert rig.commands() == []


@pytest.mark.parametrize(
    ("path", "status", "kind"),
    [
        ("/v1/controllers/8/slots/1/target", 422, "validation_error"),
        ("/v1/controllers/0/slots/7/target", 422, "validation_error"),
        ("/v1/controllers/1/slots/1/target", 404, "not_found"),
        ("/v1/controllers/0/slots/3/target", 404, "not_found"),
        ("/v1/controllers/1234567890/slots/1/target", 422, "validation_error"),
        ("/v1/controllers/abc/slots/1/target", 422, "validation_error"),
    ],
)
def test_t16_v1_out_of_bound_keeps_id_order(
    rig: ApiRig, client: ScanningClient, path: str, status: int, kind: str
) -> None:
    response = _put(rig, client, path, json={"temperature_c": 1e30})
    assert response.status_code == status
    error = rig.error(response)
    assert error["kind"] == kind
    assert error["effective_limits"] is None
    assert rig.commands() == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {"temperature_c": "37"}},
        {"json": {"temperature_c": "1e30"}},
        {"json": {"temperature_c": True}},
        {"json": {"temperature_c": None}},
        {"json": {"temperature_c": [1e30]}},
        {"json": {}},
        {"json": {"temperature_c": 37.0, "command": "1STT370"}},
        {"json": {"temperature_c": 1e30, "command": "1STT370"}},
        {"json": [37.0]},
        {},
    ],
    ids=["string", "string-big", "bool", "null", "list", "missing", "extra", "extra-big",
         "array", "no-body"],
)  # fmt: skip
def test_t16_v1_ordinary_invalid_body_keeps_plain_schema_422(
    rig: ApiRig, client: ScanningClient, kwargs: dict[str, Any]
) -> None:
    response = _put(rig, client, **kwargs)
    assert response.status_code == 422
    error = rig.error(response)
    assert error["kind"] == "validation_error"
    assert error["effective_limits"] is None
    assert "outside the effective limits" not in error["message"]
    assert rig.commands() == []

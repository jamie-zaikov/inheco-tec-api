"""Tests for the C12 request/response schemas (Task 16, sub-task 16.2).

Requirements: FR-10.2 (no free-form command field; every request body is closed), FR-17.2 (the
emergency-off ``confirm`` is required and only a JSON boolean passes the schema; ``true`` only is
accepted by the operation), FR-6.6 / UA-1 (``emergency_off_active`` for every controller state,
``lost`` included), FR-8.2 / carry-forwards T15-C1 and T15-V (``temperature_c``: an absurd finite
value or a huge JSON integer is a 422, never a 500; NaN / infinity pass the schema so the operation
applies the effective-limit check; |t| <= 1000 passes). A minimal FastAPI app shows the parser path
answers 422, not 500 (the app's own routes come with Tasks 17/18).
"""

from __future__ import annotations

import inspect
import math
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from inheco_tec_api.http import schemas
from inheco_tec_api.http.schemas import (
    TEMPERATURE_ABS_BOUND_C,
    ControllerStatusBody,
    EmergencyOffRequest,
    SetTargetRequest,
    StatusResponse,
)
from inheco_tec_api.registry_status import ControllerStatus
from pydantic import BaseModel, ValidationError

_MODELS = [
    obj
    for _, obj in inspect.getmembers(schemas, inspect.isclass)
    if issubclass(obj, BaseModel) and obj.__module__ == schemas.__name__
]
_REQUESTS = [
    m for m in _MODELS if m.__name__.endswith("Request") and not m.__name__.startswith("_")
]
_COMMAND_WORDS = ("command", "cmd", "raw", "mnemonic", "firmware_string")


# -- FR-10.2: closed request bodies, no command field ---------------------------------------------


def test_fr10_2_request_models_found() -> None:
    assert {m.__name__ for m in _REQUESTS} == {"SetTargetRequest", "EmergencyOffRequest"}


@pytest.mark.parametrize("model", _REQUESTS, ids=lambda m: m.__name__)
def test_fr10_2_every_request_body_forbids_extra(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid"
    assert model.model_json_schema().get("additionalProperties") is False


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_fr10_2_no_command_field_anywhere(model: type[BaseModel]) -> None:
    for name in model.model_fields:
        assert not any(word in name.lower() for word in _COMMAND_WORDS), name


def test_fr10_2_request_fields_are_exactly_the_defined_shapes() -> None:
    assert set(SetTargetRequest.model_fields) == {"temperature_c"}
    assert set(EmergencyOffRequest.model_fields) == {"confirm"}


@pytest.mark.parametrize(
    "payload",
    [
        {"temperature_c": 37.0, "command": "1STT"},
        {"temperature_c": 37.0, "raw": "0SMT"},
        {"temperature_c": 37.0, "extra": 1},
    ],
)
def test_fr10_2_set_target_rejects_extra_fields(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as info:
        SetTargetRequest.model_validate(payload)
    assert {e["type"] for e in info.value.errors()} == {"extra_forbidden"}


def test_fr10_2_emergency_off_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EmergencyOffRequest.model_validate({"confirm": True, "command": "0AEO"})


# -- FR-17.2: confirm is a required StrictBool ----------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_fr17_2_confirm_accepts_json_booleans(value: bool) -> None:
    assert EmergencyOffRequest.model_validate({"confirm": value}).confirm is value


@pytest.mark.parametrize("value", ["true", "True", 1, 0, 1.0, None, "yes", [], {}])
def test_fr17_2_confirm_rejects_non_boolean(value: Any) -> None:
    with pytest.raises(ValidationError):
        EmergencyOffRequest.model_validate({"confirm": value})


@pytest.mark.parametrize(
    "raw", ['{"confirm": "true"}', '{"confirm": 1}', '{"confirm": null}', "{}"]
)
def test_fr17_2_confirm_rejects_non_boolean_json(raw: str) -> None:
    with pytest.raises(ValidationError):
        EmergencyOffRequest.model_validate_json(raw)


def test_fr17_2_confirm_is_required() -> None:
    with pytest.raises(ValidationError) as info:
        EmergencyOffRequest.model_validate({})
    assert info.value.errors()[0]["type"] == "missing"
    assert EmergencyOffRequest.model_json_schema()["required"] == ["confirm"]


# -- FR-8.2 / T15-C1 / T15-V: temperature_c -----------------------------------------------------


@pytest.mark.parametrize("value", [0, 25, 37.5, -20.0, 1000, 1000.0, -1000.0, 999.99])
def test_fr8_2_temperature_within_bound_passes_schema(value: float) -> None:
    assert SetTargetRequest.model_validate({"temperature_c": value}).temperature_c == value


def test_fr8_2_out_of_window_but_within_bound_reaches_operation() -> None:
    # 500 C lies outside every slot window, but the operation gives the 422 with limits.
    assert SetTargetRequest.model_validate({"temperature_c": 500.0}).temperature_c == 500.0


@pytest.mark.parametrize("value", [1e30, -1e30, 1e27, 1000.0001, -1000.5, 1.7e308])
def test_t15_c1_absurd_finite_temperature_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        SetTargetRequest.model_validate({"temperature_c": value})


def test_t15_v_huge_python_int_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SetTargetRequest.model_validate({"temperature_c": 10**400})


def test_t15_v_huge_json_int_never_raises_non_validation_error() -> None:
    raw = '{"temperature_c": 1' + "0" * 400 + "}"
    try:
        value = SetTargetRequest.model_validate_json(raw).temperature_c
    except ValidationError:
        return
    # Accepted only as a non-finite float, which the operation rejects with FR-8.2's 422.
    assert not math.isfinite(value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_fr8_2_non_finite_passes_schema_for_the_operation(value: float) -> None:
    got = SetTargetRequest.model_validate({"temperature_c": value}).temperature_c
    assert got != got if math.isnan(value) else got == value


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_fr8_2_non_finite_json_passes_schema(raw: str) -> None:
    value = SetTargetRequest.model_validate_json('{"temperature_c": ' + raw + "}").temperature_c
    assert not math.isfinite(value)


@pytest.mark.parametrize("value", ["37.0", True, False, None, [37.0], {"v": 1}])
def test_fr8_2_temperature_rejects_non_numbers(value: Any) -> None:
    with pytest.raises(ValidationError):
        SetTargetRequest.model_validate({"temperature_c": value})


def test_fr8_2_temperature_is_required() -> None:
    with pytest.raises(ValidationError):
        SetTargetRequest.model_validate({})


def test_temperature_bound_is_1000() -> None:
    assert TEMPERATURE_ABS_BOUND_C == 1000.0


# -- the parser path through FastAPI: 422, never 500 ---------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()

    @app.put("/target")
    def put_target(body: SetTargetRequest) -> dict[str, str]:
        return {"temperature_c": repr(body.temperature_c)}

    @app.post("/eo")
    def post_eo(body: EmergencyOffRequest) -> dict[str, bool]:
        return {"confirm": body.confirm}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "raw",
    [
        '{"temperature_c": 1e30}',
        '{"temperature_c": 1' + "0" * 400 + "}",
        '{"temperature_c": "37"}',
        '{"temperature_c": true}',
        '{"temperature_c": 37, "command": "1STT"}',
        "{}",
    ],
)
def test_t15_c1_set_target_bad_body_is_422_not_500(client: TestClient, raw: str) -> None:
    response = client.put("/target", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422


def test_set_target_valid_body_is_200(client: TestClient) -> None:
    response = client.put("/target", json={"temperature_c": 37})
    assert response.status_code == 200
    assert response.json() == {"temperature_c": "37.0"}


@pytest.mark.parametrize(
    "raw", ['{"confirm": "true"}', '{"confirm": 1}', "{}", '{"confirm": true, "x": 1}']
)
def test_fr17_2_eo_bad_body_is_422(client: TestClient, raw: str) -> None:
    response = client.post("/eo", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422


def test_fr17_2_eo_true_passes(client: TestClient) -> None:
    assert client.post("/eo", json={"confirm": True}).json() == {"confirm": True}


# -- FR-6.6 / UA-1: emergency_off_active in every status ------------------------------------------


@pytest.mark.parametrize("state", ["disconnected", "connected", "released", "lost"])
@pytest.mark.parametrize("active", [True, False])
def test_fr6_6_status_has_emergency_off_active_for_every_state(state: str, active: bool) -> None:
    status = ControllerStatus(
        controller_id=2,
        state=state,  # type: ignore[arg-type]
        transport_fake=True,
        emergency_off_active=active,
        slots=[],
    )
    dumped = StatusResponse.of([status]).model_dump(mode="json")
    entry = dumped["controllers"][0]
    assert entry["emergency_off_active"] is active
    assert entry["state"] == state
    assert "emergency_off_active" in ControllerStatusBody.model_json_schema()["required"]


def test_fr6_6_status_without_emergency_off_active_is_invalid() -> None:
    with pytest.raises(ValidationError):
        ControllerStatusBody.model_validate(
            {"controller_id": 1, "state": "lost", "transport_fake": False, "slots": []}
        )

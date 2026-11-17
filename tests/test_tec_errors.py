"""Tests for the C12 exception -> HTTP status / ``kind`` mapping (Task 16).

Requirements: FR-9 (no failure reads as success), FR-9.1 (every §8.2 firmware code -> 502 with the
code and its description), FR-9.2 (transport kinds -> 504 with ``failure``), FR-9.4 (fixed status
table, documented through ``error_responses()``), FR-12.7 / FR-17.4 (409 ``stop_not_delivered``,
"Stop was NOT delivered" / "Emergency-off was NOT delivered") and carry-forwards T9-V2 (firmware
code outside §8.2 -> fallback description), T12-E (``StateStoreError`` -> a defined 500, no path)
and T15-E (``StopFailed`` / ``EmergencyOffOutcomeUnknown`` mapped by their specific class).
The safety-result and leak-guard rows are in ``test_tec_errors_safety.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from inheco_tec_api._vendor.instrument_api_core import Unauthorized
from inheco_tec_api.controller import (
    ControllerBusy,
    ControllerError,
    ControllerLost,
    ControllerNotConnected,
    ControllerReleased,
    EmergencyOffActive,
    ReconnectStopFailed,
    SlotNotPermitted,
    StopNotDelivered,
)
from inheco_tec_api.controller_startup import SlotStopOutcome
from inheco_tec_api.engine import DeviceLost, TransactionFailed
from inheco_tec_api.http.errors import (
    STATUS_BY_KIND,
    ApiError,
    FirmwareError,
    error_responses,
    to_api_error,
)
from inheco_tec_api.http.schemas import ErrorResponse
from inheco_tec_api.operations import (
    EmergencyOffOutcomeUnknown,
    InvalidRequest,
    NotConfigured,
    ReplyDataInvalid,
    StopFailed,
)
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS
from inheco_tec_api.registry import ServiceStopping
from inheco_tec_api.registry_status import EffectiveLimits
from inheco_tec_api.state_store import StateStoreError

# The protocol report §8.2 error codes (everything except the success code ``0``).
SECTION_8_2_CODES = [*"123456789", *"ABCDEFGHIJKLMNO", "R", "T", "W"]
UNLISTED_CODES = ["P", "Q", "S", "U", "X", "Z", "a", "#"]
TRANSPORT_KINDS = ["no_reply", "crc", "parse"]
BODY_KEYS = {
    "kind",
    "message",
    "code",
    "code_description",
    "failure",
    "attempts",
    "controller_id",
    "slot_id",
    "effective_limits",
    "slots",
}

# The design C12 status table, transcribed from design.md (FR-9.4).
C12_TABLE = {
    "unauthorized": 401,
    "not_found": 404,
    "validation_error": 422,
    "controller_released": 409,
    "emergency_off_active": 409,
    "slot_not_permitted": 409,
    "stop_not_delivered": 409,
    "firmware_error": 502,
    "stop_all_failed": 502,
    "controller_not_connected": 503,
    "controller_busy": 503,
    "service_stopping": 503,
    "transport_error": 504,
    "internal_error": 500,
}


def _render(exc: BaseException, **ids: int) -> tuple[ApiError, dict[str, Any]]:
    """Map ``exc`` and return the error and the ``error`` object of its body."""
    err = to_api_error(exc, **ids)
    body = err.body()
    assert set(body) == {"error"}
    detail = body["error"]
    assert set(detail) == BODY_KEYS
    assert detail["kind"] == err.kind
    assert STATUS_BY_KIND[err.kind] == err.status_code
    ErrorResponse.model_validate(body)  # the body matches the documented schema
    return err, detail


# -- FR-9.4: fixed status table --------------------------------------------------------------------


def test_fr9_4_status_table_matches_design_c12() -> None:
    assert STATUS_BY_KIND == C12_TABLE


def test_fr9_4_stop_not_delivered_is_409() -> None:
    assert STATUS_BY_KIND["stop_not_delivered"] == 409


def test_fr9_4_error_responses_documents_every_class() -> None:
    responses = error_responses(*C12_TABLE)  # type: ignore[arg-type]
    assert set(responses) == set(C12_TABLE.values())
    for status, entry in responses.items():
        assert entry["model"] is ErrorResponse
        listed = {k for k, s in C12_TABLE.items() if s == status}
        for kind in listed:
            assert kind in entry["description"]


def test_fr9_4_error_responses_groups_kinds_by_status() -> None:
    responses = error_responses("controller_released", "slot_not_permitted", "firmware_error")
    assert set(responses) == {409, 502}
    assert "controller_released" in responses[409]["description"]
    assert "slot_not_permitted" in responses[409]["description"]
    assert "firmware_error" not in responses[409]["description"]


def test_fr9_4_error_responses_empty() -> None:
    assert error_responses() == {}


# -- FR-9.1: firmware codes ----------------------------------------------------------------------


@pytest.mark.parametrize("code", SECTION_8_2_CODES)
def test_fr9_1_firmware_code_is_502_with_code_and_description(code: str) -> None:
    err, detail = _render(TransactionFailed("firmware", code, 3), controller_id=0, slot_id=1)
    assert err.status_code == 502
    assert not 200 <= err.status_code < 300
    assert detail["kind"] == "firmware_error"
    assert detail["code"] == code
    assert detail["code_description"] == FIRMWARE_ERRORS[code]
    assert detail["attempts"] == 3
    assert detail["controller_id"] == 0
    assert detail["slot_id"] == 1
    assert code in detail["message"]
    assert FIRMWARE_ERRORS[code] in detail["message"]


@pytest.mark.parametrize("code", SECTION_8_2_CODES)
def test_fr9_1_stop_failed_firmware_code_is_502_never_2xx(code: str) -> None:
    err, detail = _render(StopFailed(2, 3, code))
    assert err.status_code == 502
    assert detail["kind"] == "firmware_error"
    assert detail["code"] == code
    assert detail["code_description"] == FIRMWARE_ERRORS[code]
    assert detail["controller_id"] == 2
    assert detail["slot_id"] == 3
    assert detail["slots"] == [
        {"controller_id": 2, "slot_id": 3, "outcome": "failed", "failure": code}
    ]
    assert "not confirmed" in detail["message"]


@pytest.mark.parametrize("code", UNLISTED_CODES)
def test_t9_v2_unlisted_firmware_code_gets_fallback_description(code: str) -> None:
    err, detail = _render(TransactionFailed("firmware", code, 1))
    assert err.status_code == 502
    assert detail["code"] == code
    assert detail["code_description"] == "unknown firmware code"
    assert "(unlisted)" in detail["message"]
    assert f" {code}:" not in detail["message"]


@pytest.mark.parametrize("code", UNLISTED_CODES)
def test_t9_v2_stop_failed_unlisted_code_gets_fallback(code: str) -> None:
    err, detail = _render(StopFailed(0, 1, code))
    assert err.status_code == 502
    assert detail["code_description"] == "unknown firmware code"
    assert "(unlisted)" in detail["message"]


def test_t9_v2_firmware_without_code_gets_fallback() -> None:
    err, detail = _render(TransactionFailed("firmware", None, 1))
    assert err.status_code == 502
    assert detail["code_description"] == "unknown firmware code"


def test_firmware_error_class_is_502() -> None:
    assert FirmwareError("x").status_code == 502


# -- FR-9.2: transport kinds -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", TRANSPORT_KINDS)
def test_fr9_2_transport_failure_is_504_with_failure(kind: str) -> None:
    err, detail = _render(TransactionFailed(kind, None, 3), controller_id=4, slot_id=2)  # type: ignore[arg-type]
    assert err.status_code == 504
    assert detail["kind"] == "transport_error"
    assert detail["failure"] == kind
    assert detail["attempts"] == 3
    assert detail["code"] is None
    assert detail["code_description"] is None
    assert detail["controller_id"] == 4
    assert detail["slot_id"] == 2


def test_fr9_2_reply_data_invalid_is_504_parse() -> None:
    err, detail = _render(ReplyDataInvalid(), controller_id=1)
    assert err.status_code == 504
    assert detail["failure"] == "parse"
    assert detail["attempts"] == 1


@pytest.mark.parametrize("kind", TRANSPORT_KINDS)
def test_fr9_2_stop_failed_transport_is_504(kind: str) -> None:
    err, detail = _render(StopFailed(1, 2, kind))
    assert err.status_code == 504
    assert detail["kind"] == "transport_error"
    assert detail["failure"] == kind
    assert detail["code"] is None


def test_fr9_3_unknown_firmware_char_via_parse_is_504() -> None:
    # An error character outside §8.2 fails the transaction closed as kind ``parse`` (FR-9.3).
    err, detail = _render(StopFailed(1, 2, "parse"))
    assert err.status_code == 504
    assert detail["failure"] == "parse"


def test_fr9_2_raw_device_lost_is_504_outcome_unknown() -> None:
    err, detail = _render(DeviceLost(), controller_id=3, slot_id=1)
    assert err.status_code == 504
    assert detail["failure"] == "device_lost"
    assert "unknown" in detail["message"]


# -- the remaining rows of the mapping table -----------------------------------------------------


def test_api_error_passes_through_unchanged() -> None:
    original = FirmwareError("x", code="5")
    assert to_api_error(original) is original


def test_unauthorized_is_401_with_fixed_message() -> None:
    err, detail = _render(Unauthorized())
    assert err.status_code == 401
    assert detail["kind"] == "unauthorized"
    assert detail["message"] == Unauthorized().args[0]


def test_service_stopping_is_503() -> None:
    err, detail = _render(ServiceStopping(), controller_id=2)
    assert err.status_code == 503
    assert detail["kind"] == "service_stopping"
    assert detail["controller_id"] == 2


def test_invalid_request_is_422_with_effective_limits() -> None:
    exc = InvalidRequest(
        "target 200.0 C is outside the effective limits",
        controller_id=1,
        slot_id=2,
        effective_limits=EffectiveLimits(min_c=4.0, max_c=110.0),
    )
    err, detail = _render(exc)
    assert err.status_code == 422
    assert detail["kind"] == "validation_error"
    assert detail["message"] == "target 200.0 C is outside the effective limits"
    assert detail["effective_limits"] == {"min_c": 4.0, "max_c": 110.0}
    assert (detail["controller_id"], detail["slot_id"]) == (1, 2)


def test_invalid_request_uses_request_ids_when_absent() -> None:
    err, detail = _render(InvalidRequest("selector out of range"), controller_id=5, slot_id=6)
    assert err.status_code == 422
    assert (detail["controller_id"], detail["slot_id"]) == (5, 6)
    assert detail["effective_limits"] is None


def test_not_configured_is_404() -> None:
    err, detail = _render(NotConfigured("controller 1 is not configured", controller_id=1))
    assert err.status_code == 404
    assert detail["kind"] == "not_found"
    assert detail["controller_id"] == 1


def test_controller_busy_is_503_with_retry_after() -> None:
    err, detail = _render(ControllerBusy(2, "busy"), slot_id=1)
    assert err.status_code == 503
    assert detail["kind"] == "controller_busy"
    assert detail["controller_id"] == 2
    assert int(err.headers["Retry-After"]) >= 1
    assert "retry" in detail["message"]


def test_other_errors_carry_no_headers() -> None:
    assert to_api_error(ControllerReleased(1)).headers == {}


@pytest.mark.parametrize("state", ["disconnected", "lost"])
def test_controller_not_connected_is_503_naming_state(state: str) -> None:
    err, detail = _render(ControllerNotConnected(3, state))  # type: ignore[arg-type]
    assert err.status_code == 503
    assert detail["kind"] == "controller_not_connected"
    assert state in detail["message"]
    assert detail["failure"] is None


def test_controller_lost_is_503_with_device_lost() -> None:
    err, detail = _render(ControllerLost(3))
    assert err.status_code == 503
    assert detail["kind"] == "controller_not_connected"
    assert detail["failure"] == "device_lost"
    assert "lost" in detail["message"]


def test_controller_released_is_409() -> None:
    err, detail = _render(ControllerReleased(4), slot_id=2)
    assert err.status_code == 409
    assert detail["kind"] == "controller_released"
    assert (detail["controller_id"], detail["slot_id"]) == (4, 2)


def test_emergency_off_active_is_409() -> None:
    err, detail = _render(EmergencyOffActive(4), slot_id=1)
    assert err.status_code == 409
    assert detail["kind"] == "emergency_off_active"


def test_slot_not_permitted_is_409_with_reason() -> None:
    err, detail = _render(SlotNotPermitted(1, 5, "article_mismatch"))
    assert err.status_code == 409
    assert detail["kind"] == "slot_not_permitted"
    assert detail["slot_id"] == 5
    assert detail["failure"] == "article_mismatch"
    assert "article_mismatch" in detail["message"]


def test_fr12_7_stop_not_delivered_is_409_naming_slots() -> None:
    err, detail = _render(StopNotDelivered(3, [1, 2], "stop"))
    assert err.status_code == 409
    assert detail["kind"] == "stop_not_delivered"
    assert detail["message"].startswith("Stop was NOT delivered")
    assert "1, 2" in detail["message"]
    assert detail["controller_id"] == 3
    assert detail["slots"] == [
        {"controller_id": 3, "slot_id": s, "outcome": "not_delivered", "failure": "open_failed"}
        for s in (1, 2)
    ]


def test_fr17_4_emergency_off_not_delivered_is_409() -> None:
    err, detail = _render(StopNotDelivered(3, [1, 2, 3], "emergency_off"))
    assert err.status_code == 409
    assert detail["kind"] == "stop_not_delivered"
    assert detail["message"].startswith("Emergency-off was NOT delivered")
    assert [row["slot_id"] for row in detail["slots"]] == [1, 2, 3]


def test_reconnect_stop_failed_is_502_with_per_slot_outcome() -> None:
    outcomes = [
        SlotStopOutcome(1, "stopped"),
        SlotStopOutcome(2, "failed", "no_reply"),
        SlotStopOutcome(3, "not_attempted"),
    ]
    err, detail = _render(ReconnectStopFailed(6, outcomes))
    assert err.status_code == 502
    assert detail["kind"] == "stop_all_failed"
    assert detail["slots"] == [
        {"controller_id": 6, "slot_id": 1, "outcome": "stopped", "failure": None},
        {"controller_id": 6, "slot_id": 2, "outcome": "failed", "failure": "no_reply"},
        {"controller_id": 6, "slot_id": 3, "outcome": "not_attempted", "failure": None},
    ]


def test_t15_e_specific_classes_not_mapped_by_generic_fallback() -> None:
    assert to_api_error(StopFailed(1, 1, "5")).kind == "firmware_error"
    assert to_api_error(StopFailed(1, 1, "device_lost")).kind == "transport_error"
    assert to_api_error(EmergencyOffOutcomeUnknown(1, "lost")).kind == "transport_error"
    assert to_api_error(ControllerError(1, "other")).kind == "internal_error"


def test_generic_controller_error_is_500_fixed_message() -> None:
    err, detail = _render(ControllerError(1, "something odd happened"))
    assert err.status_code == 500
    assert detail["message"] == "internal error"


@pytest.mark.parametrize(
    "exc", [RuntimeError("boom"), ValueError("bad"), KeyError(3), OSError(5, "io"), Exception()]
)
def test_unexpected_exception_is_500_fixed_message(exc: BaseException) -> None:
    err, detail = _render(exc, controller_id=1, slot_id=2)
    assert err.status_code == 500
    assert detail["kind"] == "internal_error"
    assert detail["message"] == "internal error"


def test_t12_e_state_store_error_is_defined_500_without_path() -> None:
    path = "C:" + "\\ProgramData\\inheco\\release-state.json"
    err, detail = _render(StateStoreError(f"could not write {path}"), controller_id=2)
    assert err.status_code == 500
    assert detail["kind"] == "internal_error"
    assert detail["failure"] == "state_store"
    assert detail["message"] == (
        "the release state could not be saved; the request was refused and nothing was changed"
    )
    assert detail["message"] != "internal error"
    assert "release-state" not in str(detail)
    assert "ProgramData" not in str(detail)

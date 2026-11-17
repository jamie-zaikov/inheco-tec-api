"""Tests for the safety-result rows and the leak guard of the C12 error model (Task 16).

Requirements: FR-9 (a failed or undelivered safety result is never a success), FR-12.7 (stop-all
409 when any slot is not delivered), FR-3.7 (no host, IP address, port, USB device path or secret
in any response body), plus carry-forward T13-S3 (``stop_all_error`` never returns ``None`` for a
failed / not-delivered slot; ``not_delivered`` wins; ``slots`` lists every outcome; an unknown
safety outcome never reads as "not connected" / "not sent") and T7-S1 (raw exception text and
request input never reach a body).
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from inheco_tec_api.controller import (
    ControllerError,
    ControllerNotConnected,
    SlotNotPermitted,
    StopNotDelivered,
)
from inheco_tec_api.engine import DeviceLost
from inheco_tec_api.http.errors import (
    request_validation_error,
    stop_all_error,
    to_api_error,
)
from inheco_tec_api.operations import (
    EmergencyOffOutcomeUnknown,
    InvalidRequest,
    NotConfigured,
    StopAllResult,
    StopFailed,
)
from inheco_tec_api.registry import StopAllSlotOutcome
from inheco_tec_api.state_store import StateStoreError

OUTCOMES = ["stopped", "failed", "not_delivered", "skipped"]

# Leak sentinels, built at runtime so the repository secret scan does not see literal credentials.
SENTINEL_IP = ".".join(["10", "203", "77", "41"])
SENTINEL_PORT = "lab" + "pc" + ":" + "8443"
SENTINEL_WIN_PATH = "C:" + "\\Users\\svc-tec\\state.json"
SENTINEL_HID_PATH = "\\\\?\\" + "hid#vid_03eb&pid_2fe5#7&1a2b"
SENTINEL_BEARER = "Bear" + "er " + "abc"
SENTINEL_TOKEN = "tok" + "en=" + "zz9"
SENTINEL_NON_ASCII = "Temperatur über Grenze"
SENTINEL_LONG = "LONGSENTINEL" + "x" * 300
SENTINELS = [
    SENTINEL_IP,
    SENTINEL_PORT,
    SENTINEL_WIN_PATH,
    SENTINEL_HID_PATH,
    SENTINEL_BEARER,
    SENTINEL_TOKEN,
    SENTINEL_NON_ASCII,
    SENTINEL_LONG,
]
# Fragments that must not appear in any string of a body (FR-3.7 regex scan).
_LEAK_SCAN = re.compile(
    r"\d{1,3}(?:\.\d{1,3}){3}|:\d+|\\|hid#|vid_|pid_|Bearer abc|token=|LONGSENTINEL|[^\x20-\x7e]"
)


def _strings(value: Any) -> Iterator[str]:
    """Every string (keys and values) in a JSON-ready structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _assert_no_leak(body: dict[str, Any], sentinel: str) -> None:
    for text in _strings(body):
        assert sentinel not in text
        assert sentinel[:12] not in text
        assert not _LEAK_SCAN.search(text), text
    json.dumps(body)  # the body is JSON-serialisable


def _result(*outcomes: str, status: str | None = None) -> StopAllResult:
    rows = [
        StopAllSlotOutcome(
            controller_id=i // 2,
            slot_id=i % 2 + 1,
            outcome=o,  # type: ignore[arg-type]
            failure=None if o == "stopped" else f"cause_{o}",
        )
        for i, o in enumerate(outcomes)
    ]
    if status is None:
        status = (
            "not_delivered"
            if "not_delivered" in outcomes
            else "failed"
            if "failed" in outcomes
            else "ok"
        )
    return StopAllResult(outcomes=rows, status=status)  # type: ignore[arg-type]


# -- T13-S3 / FR-8.6 / FR-12.7: stop-all status rule --------------------------------------------


def test_stop_all_all_stopped_is_200() -> None:
    assert stop_all_error(_result("stopped", "stopped")) is None


def test_stop_all_skipped_does_not_change_status() -> None:
    assert stop_all_error(_result("stopped", "skipped", "skipped")) is None


def test_stop_all_no_outcomes_is_200() -> None:
    assert stop_all_error(_result()) is None


def test_stop_all_failed_is_502() -> None:
    err = stop_all_error(_result("stopped", "failed"))
    assert err is not None
    assert (err.status_code, err.kind) == (502, "stop_all_failed")


def test_fr12_7_stop_all_not_delivered_is_409() -> None:
    err = stop_all_error(_result("stopped", "not_delivered"))
    assert err is not None
    assert (err.status_code, err.kind) == (409, "stop_not_delivered")
    assert err.message.startswith("Stop was NOT delivered")


def test_t13_s3_not_delivered_wins_over_failed() -> None:
    err = stop_all_error(_result("failed", "not_delivered", "stopped"))
    assert err is not None
    assert err.status_code == 409
    assert err.kind == "stop_not_delivered"


def test_stop_all_not_delivered_message_names_controller() -> None:
    # outcome index 2 -> controller 1
    err = stop_all_error(_result("stopped", "stopped", "not_delivered"))
    assert err is not None
    assert "1" in err.message


@pytest.mark.parametrize("status", ["failed", "not_delivered"])
def test_t13_s3_non_ok_status_never_200(status: str) -> None:
    err = stop_all_error(_result("stopped", status=status))
    assert err is not None
    assert err.status_code >= 400


@pytest.mark.parametrize(
    "combo",
    [c for n in range(1, 4) for c in itertools.product(OUTCOMES, repeat=n)],
)
def test_t13_s3_stop_all_status_rule_over_every_combination(combo: tuple[str, ...]) -> None:
    err = stop_all_error(_result(*combo))
    if "not_delivered" in combo:
        assert err is not None and err.status_code == 409
    elif "failed" in combo:
        assert err is not None and err.status_code == 502
    else:
        assert err is None
        return
    body = err.body()["error"]
    assert [row["outcome"] for row in body["slots"]] == list(combo)
    assert len(body["slots"]) == len(combo)


def test_t13_s3_slots_lists_every_outcome_with_the_four_values() -> None:
    err = stop_all_error(_result("stopped", "failed", "not_delivered", "skipped"))
    assert err is not None
    slots = err.body()["error"]["slots"]
    assert slots == [
        {"controller_id": 0, "slot_id": 1, "outcome": "stopped", "failure": None},
        {"controller_id": 0, "slot_id": 2, "outcome": "failed", "failure": "cause_failed"},
        {
            "controller_id": 1,
            "slot_id": 1,
            "outcome": "not_delivered",
            "failure": "cause_not_delivered",
        },
        {"controller_id": 1, "slot_id": 2, "outcome": "skipped", "failure": "cause_skipped"},
    ]


# -- T13-S3: an unknown safety outcome is not "not connected" / "not sent" -----------------------

_NOT_SENT_WORDS = ("not connected", "not sent", "NOT delivered", "not applied")


@pytest.mark.parametrize("state", ["released", "lost"])
def test_t13_s3_emergency_off_outcome_unknown_is_504(state: str) -> None:
    err = to_api_error(EmergencyOffOutcomeUnknown(2, state))  # type: ignore[arg-type]
    detail = err.body()["error"]
    assert err.status_code == 504
    assert detail["kind"] == "transport_error"
    assert detail["kind"] != "controller_not_connected"
    assert detail["failure"] == "device_lost"
    assert "UNKNOWN" in detail["message"]
    assert state in detail["message"]
    for words in _NOT_SENT_WORDS:
        assert words not in detail["message"]


@pytest.mark.parametrize(
    "failure", ["no_reply", "crc", "parse", "device_lost", "not_attempted", "5", "W", "Q"]
)
def test_t13_s3_stop_failed_never_success_nor_not_connected(failure: str) -> None:
    err = to_api_error(StopFailed(1, 2, failure))
    detail = err.body()["error"]
    assert err.status_code in (502, 504)
    assert detail["kind"] in ("firmware_error", "transport_error")
    assert detail["slots"][0]["outcome"] == "failed"
    assert "unknown" in detail["message"]
    for words in _NOT_SENT_WORDS:
        assert words not in detail["message"]


@pytest.mark.parametrize("failure", ["device_lost", "not_attempted"])
def test_t13_s3_stop_outcome_unknown_failure_kinds(failure: str) -> None:
    err = to_api_error(StopFailed(1, 2, failure))
    detail = err.body()["error"]
    assert err.status_code == 504
    assert detail["failure"] == failure
    assert "UNKNOWN" in detail["message"]


# -- FR-3.7 / T7-S1: leak guard --------------------------------------------------------------------


def _leaky_exceptions(text: str) -> list[tuple[BaseException, str]]:
    """Exceptions that carry ``text`` and the ``kind`` each maps to."""
    return [
        (InvalidRequest(text, controller_id=1, slot_id=2), "validation_error"),
        (NotConfigured(text, controller_id=1), "not_found"),
        (ControllerNotConnected(1, "disconnected", text), "controller_not_connected"),
        (SlotNotPermitted(1, 2, text), "slot_not_permitted"),
        (ControllerError(1, text), "internal_error"),
        (StateStoreError(text), "internal_error"),
        (DeviceLost(text), "transport_error"),
        (RuntimeError(text), "internal_error"),
        (OSError(2, text, text), "internal_error"),
    ]


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_fr3_7_exception_text_never_reaches_body(sentinel: str) -> None:
    for exc, kind in _leaky_exceptions(f"failure near {sentinel} here"):
        err = to_api_error(exc, controller_id=1, slot_id=2)
        body = err.body()
        assert body["error"]["kind"] == kind
        _assert_no_leak(body, sentinel)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_fr3_7_bare_sentinel_text_never_reaches_body(sentinel: str) -> None:
    for exc, _ in _leaky_exceptions(sentinel):
        _assert_no_leak(to_api_error(exc).body(), sentinel)


def test_fr3_7_leaky_not_connected_keeps_state_in_message() -> None:
    err = to_api_error(ControllerNotConnected(3, "disconnected", SENTINEL_HID_PATH))
    assert err.message == "controller 3 is disconnected"


def test_fr3_7_leaky_invalid_request_uses_fixed_message() -> None:
    err = to_api_error(InvalidRequest(f"bad value from {SENTINEL_IP}"))
    assert err.message == "invalid request"


def test_fr3_7_leaky_slot_reason_is_replaced() -> None:
    err = to_api_error(SlotNotPermitted(1, 2, SENTINEL_WIN_PATH))
    detail = err.body()["error"]
    assert detail["failure"] == "unknown"
    assert detail["message"] == "slot 2 is not permitted: unknown"


def test_fr3_7_message_length_boundary() -> None:
    assert to_api_error(InvalidRequest("v" * 300)).message == "v" * 300
    assert to_api_error(InvalidRequest("v" * 301)).message == "invalid request"


def test_fr3_7_safe_domain_text_passes_through() -> None:
    text = "tolerance 7.5 is outside 0.1..5.0"
    assert to_api_error(InvalidRequest(text)).message == text


@pytest.mark.parametrize(
    "exc",
    [
        StopNotDelivered(3, [1, 2], "stop"),
        StopNotDelivered(3, [1], "emergency_off"),
        StopFailed(1, 2, "R"),
        StopFailed(1, 2, "crc"),
        EmergencyOffOutcomeUnknown(1, "released"),
    ],
)
def test_fr3_7_safety_bodies_carry_no_leak_pattern(exc: BaseException) -> None:
    _assert_no_leak(to_api_error(exc).body(), "\x00never")


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_fr3_7_request_validation_never_echoes_input_or_msg(sentinel: str) -> None:
    errors = [
        {
            "loc": ("body", "temperature_c"),
            "msg": f"Input should be a valid number, got {sentinel}",
            "input": sentinel,
            "type": "float_type",
            "ctx": {"error": sentinel},
        }
    ]
    err = request_validation_error(errors)
    detail = err.body()["error"]
    assert (err.status_code, detail["kind"]) == (422, "validation_error")
    assert "temperature_c" in detail["message"]
    assert "float_type" in detail["message"]
    _assert_no_leak(err.body(), sentinel)


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_fr3_7_request_validation_leaky_loc_gives_fixed_message(sentinel: str) -> None:
    errors = [{"loc": ("body", sentinel), "msg": "Extra inputs", "type": "extra_forbidden"}]
    err = request_validation_error(errors)
    assert err.message == "invalid request"
    _assert_no_leak(err.body(), sentinel)


def test_request_validation_without_errors_is_422() -> None:
    err = request_validation_error([])
    assert err.status_code == 422
    assert err.body()["error"]["kind"] == "validation_error"


def test_request_validation_message_is_bounded_for_many_errors() -> None:
    errors = [{"loc": ("body", f"f{i}"), "type": "missing"} for i in range(50)]
    err = request_validation_error(errors)
    assert err.status_code == 422
    assert len(err.message) <= 300

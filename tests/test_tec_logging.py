"""Tests for C14 ``logging_setup`` (Task 2; FR-1.2, FR-15, FR-15.3, NFR-6).

Every test that calls ``configure`` uses the ``restore_logging`` fixture, which saves and restores
the root logger's handlers/level and the global ``structlog`` configuration, so no test leaks its
logging setup into another suite.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from inheco_tec_api import logging_setup

# A synthetic, non-secret marker value. It only checks that redaction keeps it out of the output.
TEST_TOKEN = "test-token-value-7f3a9c"  # pragma: allowlist secret


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Save and restore the root logger and the global structlog configuration."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_structlog = structlog.get_config()
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        structlog.reset_defaults()
        structlog.configure(**saved_structlog)


def _json_lines(text: str) -> list[dict[str, Any]]:
    """Parse every non-empty captured line as one JSON object."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_fr15_output_is_json_with_timestamp_and_level(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("INFO")
    log = logging_setup.get_logger("inheco_tec_api.test")
    log.info(logging_setup.EVENT_TEC_TRANSACTION, controller_id=1, slot=2, mnemonic="ATE")

    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    event = lines[0]
    assert event["event"] == "tec_transaction"
    assert event["level"] == "info"
    assert isinstance(event["timestamp"], str)
    # ISO-8601 in UTC (TimeStamper(fmt="iso", utc=True) renders a trailing "Z").
    assert "T" in event["timestamp"]
    assert event["timestamp"].endswith("Z")
    assert event["controller_id"] == 1
    assert event["slot"] == 2
    assert event["mnemonic"] == "ATE"


def test_fr15_level_filtering_drops_events_below_configured_level(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("warning")  # case is ignored
    log = logging_setup.get_logger("inheco_tec_api.test")
    log.info("below_threshold")
    log.warning(logging_setup.EVENT_TRANSPORT_FAKE_ACTIVE)

    lines = _json_lines(capsys.readouterr().out)
    assert [line["event"] for line in lines] == ["transport_fake_active"]
    assert lines[0]["level"] == "warning"


def test_fr1_2_configure_uses_core_configure_logging(
    restore_logging: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(logging_setup, "configure_logging", calls.append)
    logging_setup.configure("DEBUG")
    assert calls == ["DEBUG"]


def test_fr15_3_sensitive_keys_are_removed(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("INFO")
    log = logging_setup.get_logger("inheco_tec_api.test")
    log.info(
        logging_setup.EVENT_HTTP_REQUEST,
        method="GET",
        route="/healthz",
        status_class="2xx",
        token=TEST_TOKEN,  # pragma: allowlist secret
        Authorization=f"Bearer {TEST_TOKEN}",
        key_pem=f"pem-body-{TEST_TOKEN}",
        client_secret=TEST_TOKEN,  # pragma: allowlist secret
    )

    lines = _json_lines(capsys.readouterr().out)
    assert len(lines) == 1
    event = lines[0]
    for key in ("token", "Authorization", "key_pem", "client_secret"):
        assert key not in event
    assert event["method"] == "GET"
    assert event["route"] == "/healthz"
    assert event["status_class"] == "2xx"


def test_fr15_3_test_token_value_never_appears_in_output(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("DEBUG")
    log = logging_setup.get_logger("inheco_tec_api.test")
    log.debug("debug_event", token=TEST_TOKEN)  # pragma: allowlist secret
    log.info(
        "info_event",
        bearer_token=TEST_TOKEN,  # pragma: allowlist secret
        AUTHORIZATION=f"Bearer {TEST_TOKEN}",
    )
    log.warning("nested_event", headers={"authorization": f"Bearer {TEST_TOKEN}", "accept": "x"})
    log.error("bound_event_secret", api_secret=TEST_TOKEN)  # pragma: allowlist secret
    log.bind(access_token=TEST_TOKEN).info("bound_event")  # pragma: allowlist secret

    captured = capsys.readouterr()
    assert TEST_TOKEN not in captured.out
    assert TEST_TOKEN not in captured.err
    lines = _json_lines(captured.out)
    assert len(lines) == 5
    nested = next(line for line in lines if line["event"] == "nested_event")
    assert nested["headers"] == {"accept": "x"}


@pytest.mark.parametrize(
    "key",
    ["token", "Authorization", "key_pem", "client_secret", "TOKEN", "x_Authorization_hdr"],
)
def test_fr15_3_redact_processor_removes_matching_keys(key: str) -> None:
    event: dict[str, Any] = {"event": "e", key: TEST_TOKEN, "slot": 1}
    result = logging_setup.redact(None, "info", event)
    assert key not in result
    assert result == {"event": "e", "slot": 1}


def test_fr15_3_redact_keeps_non_matching_keys_and_handles_empty() -> None:
    assert logging_setup.redact(None, "info", {}) == {}
    event: dict[str, Any] = {"event": "e", "controller_id": 3, "param": "366", "key": "k"}
    assert logging_setup.redact(None, "info", dict(event)) == event


def test_fr15_3_redact_scrubs_nested_dicts() -> None:
    event: dict[str, Any] = {
        "event": "e",
        "outer": {"Authorization": "Bearer x", "inner": {"key_pem": "pem", "ok": 1}},
    }
    result = logging_setup.redact(None, "info", event)
    assert result == {"event": "e", "outer": {"inner": {"ok": 1}}}


def test_nfr6_event_name_constants_match_c14() -> None:
    assert logging_setup.EVENT_NAMES == frozenset(
        {
            "tec_transaction",
            "tec_state_change",
            "tec_slot_not_permitted",
            "tec_startup_slot_state",
            "tec_condensation_warning",
            "tec_emergency_off",
            "tec_stop_not_delivered",
            "transport_fake_active",
            "http_request",
        }
    )


def test_fr15_reconfigure_applies_to_logger_obtained_earlier(
    restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("ERROR")
    log = logging_setup.get_logger("inheco_tec_api.test")
    log.info("hidden")
    logging_setup.configure("INFO")
    log.info("shown")

    lines = _json_lines(capsys.readouterr().out)
    assert [line["event"] for line in lines] == ["shown"]
    assert lines[0]["logger"] == "inheco_tec_api.test"

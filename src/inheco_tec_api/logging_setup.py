"""Structured logging for the inheco-tec api (design C14; FR-1.2, FR-15, FR-15.3, NFR-6).

``configure`` installs the shared stdout handler through
``instrument_api_core.configure_logging`` (FR-1.2: the api does not re-implement the handler) and
then configures ``structlog`` on top of the standard library: each event is rendered to one JSON
line carrying an ISO-8601 ``timestamp`` and a ``level``. WinSW captures stdout into the rolled log
files.

A ``redact`` processor removes every event key that matches
``authorization|token|secret|key_pem`` (case-insensitive) before rendering. It is a backstop for
FR-15.3: no module is meant to pass the bearer token, the ``Authorization`` header, or the TLS key
contents to a logger in the first place.

Every module logs through :func:`get_logger` and uses the event-name constants below, so the event
vocabulary of C14 lives in one place:

- ``tec_transaction`` (FR-15.1): ``controller_id``, ``slot``, ``mnemonic``, ``param``, ``result``,
  ``attempts``, ``duration_ms``.
- ``tec_state_change`` (FR-15.2): ``controller_id``, ``from_state``, ``to_state``, ``reason``.
- ``tec_slot_not_permitted`` (FR-15.2): ``reason``.
- ``tec_startup_slot_state``, ``tec_condensation_warning``.
- ``tec_emergency_off``: warning; error when the stop is not delivered.
- ``tec_stop_not_delivered``: error.
- ``transport_fake_active``: warning.
- ``http_request``: ``method``, ``route``, ``status_class``.

No event carries the token, the ``Authorization`` header, the TLS key, or the USB device path.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

import structlog

from inheco_tec_api._vendor.instrument_api_core import configure_logging

# Event names (C14). Modules pass these as the structlog event, never an ad-hoc string.
EVENT_TEC_TRANSACTION = "tec_transaction"
EVENT_TEC_STATE_CHANGE = "tec_state_change"
EVENT_TEC_SLOT_NOT_PERMITTED = "tec_slot_not_permitted"
EVENT_TEC_STARTUP_SLOT_STATE = "tec_startup_slot_state"
EVENT_TEC_CONDENSATION_WARNING = "tec_condensation_warning"
EVENT_TEC_EMERGENCY_OFF = "tec_emergency_off"
EVENT_TEC_STOP_NOT_DELIVERED = "tec_stop_not_delivered"
EVENT_TRANSPORT_FAKE_ACTIVE = "transport_fake_active"
EVENT_HTTP_REQUEST = "http_request"

EVENT_NAMES: frozenset[str] = frozenset(
    {
        EVENT_TEC_TRANSACTION,
        EVENT_TEC_STATE_CHANGE,
        EVENT_TEC_SLOT_NOT_PERMITTED,
        EVENT_TEC_STARTUP_SLOT_STATE,
        EVENT_TEC_CONDENSATION_WARNING,
        EVENT_TEC_EMERGENCY_OFF,
        EVENT_TEC_STOP_NOT_DELIVERED,
        EVENT_TRANSPORT_FAKE_ACTIVE,
        EVENT_HTTP_REQUEST,
    }
)

# Any event key containing one of these fragments is dropped before rendering (FR-15.3).
REDACT_KEY_PATTERN = re.compile(r"authorization|token|secret|key_pem", re.IGNORECASE)


def _scrub(mapping: MutableMapping[str, Any]) -> None:
    """Delete matching keys from ``mapping`` in place, descending into nested dict values."""
    for key in list(mapping):
        if REDACT_KEY_PATTERN.search(str(key)):
            del mapping[key]
            continue
        value = mapping[key]
        if isinstance(value, MutableMapping):
            _scrub(value)


def redact(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: remove every key matching ``REDACT_KEY_PATTERN`` (FR-15.3 backstop).

    The match is a case-insensitive substring search, so ``Authorization``, ``token``,
    ``client_secret`` and ``key_pem`` are all removed. Nested dict values are scrubbed too.
    """
    _scrub(event_dict)
    return event_dict


def configure(level: str) -> None:
    """Configure process logging: core stdout handler, then structlog JSON rendering (C14).

    ``level`` is a standard logging level name (for example ``"INFO"``); case is ignored.
    Loggers are not cached on first use, so a later ``configure`` call takes effect for every
    logger obtained through :func:`get_logger`.
    """
    configure_logging(level)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the standard-library logger ``name``."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger

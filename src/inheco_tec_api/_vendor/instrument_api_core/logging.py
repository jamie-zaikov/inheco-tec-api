"""The structured-logging base shared by every instruments/*/api (C11, FR-5.1, NFR-1).

The reference ``zebra_printer`` service logs one structured JSON event per request through
``structlog`` (a JSON renderer plus an ISO timestamp and a log level), carrying ``method`` /
``path`` / ``status_class`` (``2xx``/``4xx``/``5xx``) only — never a token, a host, a port, or a
request body (NFR-1).

This base keeps that contract dependency-free: :func:`configure_logging` and :func:`get_logger` use
the standard library so the scaffold imports and runs with no extra dependency. The concrete api
(with the ``api`` extra installed) replaces the standard-library backend with the ``structlog``
JSON-renderer chain documented above; the field contract — ``method`` / ``path`` / ``status_class``
only, and never a secret, host, or port — is the same either way.

Note: this module is named ``logging`` for surface clarity; the standard-library ``logging`` it
imports resolves absolutely (Python 3), so there is no self-shadow.
"""

import logging
import sys
from typing import Final

LOG_LEVEL_ENV_VAR: Final[str] = "LOG_LEVEL"
"""Name of the env var that overrides the log level (a name, not a secret)."""

DEFAULT_LOG_LEVEL: Final[str] = "INFO"


def configure_logging(level: str = DEFAULT_LOG_LEVEL) -> None:
    """Configure process logging to emit one line per event to stdout (FR-5.1).

    Installs a single stdout handler on the root logger at ``level``. This is the dependency-free
    stand-in for the reference ``structlog`` JSON chain; the concrete api swaps in that renderer.
    The logging contract is unchanged: an event carries ``method`` / ``path`` / ``status_class``
    only, never a token, host, port, or request body (NFR-1).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger for the api to emit its per-request events through.

    The concrete api emits ``method`` / ``path`` / ``status_class`` only and never a secret, host,
    or port (NFR-1).
    """
    return logging.getLogger(name)

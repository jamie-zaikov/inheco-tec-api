"""instrument_api_core: the shared FastAPI service base for every instruments/*/api (C11, FR-5.1).

This package exposes the documented base surface every owned instrument API imports, matching the
reference ``zebra_printer`` service style (structlog, constant-time secret compare, fail-closed
startup, ``/healthz`` unauthenticated, no host/port in any response body):

- :mod:`instrument_api_core.health` — the unauthenticated ``/healthz`` probe contract.
- :mod:`instrument_api_core.auth` — the fail-closed, constant-time bearer-token base (secret by
  reference).
- :mod:`instrument_api_core.config` — the fail-closed config base (secrets by name, never stored).
- :mod:`instrument_api_core.logging` — the structured per-request logging contract.
- :mod:`instrument_api_core.transport` — the TCP/protobuf transport base.
- :mod:`instrument_api_core.port` — the Cloud Run port contract (``container_port = 8080``).

The base modules are dependency-free (standard library only), so the scaffold imports, type-checks,
and tests cleanly before any runtime dependency is installed; the FastAPI / structlog / protobuf
integration points are declared as optional dependencies and documented per module for the concrete
api that extends this base.
"""

from .auth import AuthConfigError, Unauthorized, load_expected_token, verify_bearer_token
from .config import ConfigError, ServiceConfig, require_env
from .health import HEALTH_PATH, HEALTH_STATUS_OK, HealthStatus, health_payload
from .logging import DEFAULT_LOG_LEVEL, LOG_LEVEL_ENV_VAR, configure_logging, get_logger
from .port import CONTAINER_PORT, PORT_ENV_VAR, resolve_port
from .transport import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    ConnectionRefused,
    HostNotFound,
    MessageTooLarge,
    TcpProtobufTransport,
    TransportError,
    TransportTimeout,
)

__version__ = "0.1.0"

__all__ = [
    "CONTAINER_PORT",
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "HEALTH_PATH",
    "HEALTH_STATUS_OK",
    "LOG_LEVEL_ENV_VAR",
    "MAX_MESSAGE_BYTES",
    "PORT_ENV_VAR",
    "AuthConfigError",
    "ConfigError",
    "ConnectionRefused",
    "HealthStatus",
    "HostNotFound",
    "MessageTooLarge",
    "ServiceConfig",
    "TcpProtobufTransport",
    "TransportError",
    "TransportTimeout",
    "Unauthorized",
    "configure_logging",
    "get_logger",
    "health_payload",
    "load_expected_token",
    "require_env",
    "resolve_port",
    "verify_bearer_token",
    "__version__",
]

"""The Cloud Run port contract shared by every instruments/*/api (C11, FR-5.1).

Cloud Run injects the listen port through the ``PORT`` environment variable and defaults it to
``8080``; the ``cloud-run-service`` module (C8) mirrors the same ``container_port = 8080`` default.
This module is the single source of truth for that contract, so every api binds the same way as the
reference ``zebra_printer`` service entry point.

:data:`CONTAINER_PORT` is the documented default; :func:`resolve_port` reads the ``PORT`` env var,
falling back to it. The host address is never part of this contract and never appears in a response
body (the no-host/port-in-body rule carried over from ``zebra_printer``).
"""

import os
from collections.abc import Mapping

CONTAINER_PORT: int = 8080
"""The Cloud Run container port contract; the ``cloud-run-service`` module default mirrors it."""

PORT_ENV_VAR: str = "PORT"
"""The environment variable Cloud Run injects with the listen port."""

_MIN_PORT: int = 1
_MAX_PORT: int = 65535


def resolve_port(env: Mapping[str, str] | None = None) -> int:
    """Return the listen port from ``PORT``, defaulting to :data:`CONTAINER_PORT` (FR-5.1).

    Reads ``PORT`` from ``env`` (the process environment when ``env`` is ``None``). An unset or
    empty value falls back to :data:`CONTAINER_PORT`. A present value must be an integer in
    ``1..65535``; anything else raises :class:`ValueError` so a misconfigured port fails fast at
    startup rather than binding an unexpected socket. The value is not a secret and carries no host.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(PORT_ENV_VAR)
    if raw is None or not raw.strip():
        return CONTAINER_PORT
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{PORT_ENV_VAR} must be an integer, got {raw!r}") from exc
    if not (_MIN_PORT <= port <= _MAX_PORT):
        raise ValueError(f"{PORT_ENV_VAR} must be in {_MIN_PORT}..{_MAX_PORT}, got {port}")
    return port

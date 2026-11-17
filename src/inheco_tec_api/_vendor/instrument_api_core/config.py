"""The runtime-configuration base shared by every instruments/*/api (C11, FR-5.1, NFR-1).

The concrete api reads its non-secret runtime configuration from the environment. This base holds
the shared pieces:

- :func:`require_env` — read a required env var and fail closed (raise :class:`ConfigError`) when it
  is unset or empty, mirroring the fail-closed startup of the reference ``zebra_printer`` service.
- :class:`ServiceConfig` — the shared, non-secret service settings (currently the Cloud Run
  container port), built from the environment by :meth:`ServiceConfig.from_env`.

Secrets are NEVER stored on the config object. A secret (for example the bearer token) is
referenced by env-var NAME and read at the point of use through
:func:`instrument_api_core.auth.load_expected_token`, so no secret value is held on a config
instance, logged, or serialized (NFR-1). The concrete api may layer a ``pydantic-settings`` model
on top of this base; the base stays dependency-free.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .port import resolve_port


class ConfigError(RuntimeError):
    """A required configuration value is missing or empty.

    Raised at startup so the api fails closed. Names the env var only, never a value (NFR-1).
    """


def require_env(name: str, env: Mapping[str, str] | None = None) -> str:
    """Return the value of required env var ``name`` or raise :class:`ConfigError` (fail closed).

    Reads ``name`` from ``env`` (the process environment when ``env`` is ``None``). An unset, empty,
    or whitespace-only value raises :class:`ConfigError`. Use this for required NON-secret settings;
    read secrets by name through :func:`instrument_api_core.auth.load_expected_token` instead, so no
    secret value is stored on a config object (NFR-1).
    """
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"{name} is required but is not set")
    return value


@dataclass(frozen=True)
class ServiceConfig:
    """Shared, non-secret runtime settings for an owned api.

    Holds the resolved Cloud Run container port. It carries no secret and no host, so it is safe to
    log or echo. The concrete api extends this shape with its own non-secret settings and keeps
    secrets off the object (NFR-1).
    """

    container_port: int

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceConfig":
        """Build the shared config from the environment (FR-5.1).

        Resolves the Cloud Run listen port through
        :func:`instrument_api_core.port.resolve_port`. Reads no secret.
        """
        return cls(container_port=resolve_port(env))

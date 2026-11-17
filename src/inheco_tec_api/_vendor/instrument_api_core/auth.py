"""The bearer-token authentication base shared by every instruments/*/api (C11, FR-5.1, NFR-1).

This mirrors the reference ``zebra_printer`` auth contract:

- The expected token is read from an environment variable BY REFERENCE (its name), never hard-coded;
  the secret VALUE never appears in code, logs, or a response body (NFR-1).
- Startup is fail-closed: :func:`load_expected_token` raises :class:`AuthConfigError` when the env
  var is unset or empty, so the api never serves a request with authentication disabled.
- The presented token is compared to the expected token in CONSTANT TIME with
  :func:`hmac.compare_digest`, so the comparison run time does not reveal the token.
- A missing, malformed, or non-matching token raises :class:`Unauthorized`, which the concrete api
  maps to HTTP 401. Neither token value is ever placed in an exception message or log.

The concrete api wires :func:`verify_bearer_token` into a FastAPI dependency attached to every
functional route; the health probe (see :mod:`instrument_api_core.health`) stays exempt.
"""

import hmac
import os
from collections.abc import Mapping

DEFAULT_TOKEN_ENV_VAR: str = "API_TOKEN"  # pragma: allowlist secret
"""Default name of the env var that holds the expected bearer token (a name, not a value)."""

_BEARER_SCHEME: str = "bearer"


class AuthConfigError(RuntimeError):
    """The expected token is not configured: the referenced env var is unset or empty.

    Raised at startup so the api fails closed and never serves a request with authentication
    disabled. Carries the env-var NAME only, never a token value (NFR-1).
    """


class Unauthorized(Exception):
    """A bearer-token check failed: the header is missing/malformed or the token does not match.

    Carries a fixed, safe message with NO token value and no request detail (NFR-1). The concrete
    api maps this to HTTP 401 with its unified error body.
    """

    def __init__(self) -> None:
        super().__init__("missing or invalid bearer token")


def load_expected_token(
    env_var: str = DEFAULT_TOKEN_ENV_VAR,
    env: Mapping[str, str] | None = None,
) -> str:
    """Load the expected bearer token from ``env_var`` by reference; fail closed if absent (NFR-1).

    Reads ``env_var`` from ``env`` (the process environment when ``env`` is ``None``). An unset,
    empty, or whitespace-only value raises :class:`AuthConfigError` so startup fails fast rather
    than running with authentication effectively disabled. The returned value is the secret; the
    caller keeps it out of logs and bodies. The raised error names the env var only, never a value.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    value = source.get(env_var)
    if value is None or not value.strip():
        raise AuthConfigError(
            f"{env_var} is not set; refusing to start with authentication disabled"
        )
    return value


def verify_bearer_token(authorization: str | None, expected_token: str) -> None:
    """Require a valid bearer token in the ``Authorization`` header or raise :class:`Unauthorized`.

    Rejects an absent/empty header, a non-``Bearer`` scheme (matched case-insensitively per
    RFC 7235), and a ``Bearer`` with an empty or whitespace-only token. The token value is then
    compared to ``expected_token`` with :func:`hmac.compare_digest` on UTF-8 bytes, so the compare
    is constant-time and a non-ASCII presented token compares False (a clean 401) not a raise.
    On success returns ``None`` and the request proceeds; no token is logged or echoed (NFR-1).
    """
    if not authorization:
        raise Unauthorized()

    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != _BEARER_SCHEME:
        raise Unauthorized()

    # A bearer token is opaque: compare exactly what the client sent after the delimiter space.
    # The whitespace check gates on a copy and never mutates the compared value.
    if not presented.strip():
        raise Unauthorized()

    if not hmac.compare_digest(presented.encode("utf-8"), expected_token.encode("utf-8")):
        raise Unauthorized()

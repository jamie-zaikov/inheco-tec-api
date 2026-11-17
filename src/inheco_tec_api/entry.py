"""Service entry point of the inheco-tec api (design C13, S7; FR-2.5, FR-3.1-FR-3.4, NFR-1, NFR-6).

:func:`main` is the ``inheco-tec-api`` console script that WinSW runs as the Windows service
(``service/inheco-tec-api.xml``). It is an ordinary console program (DD-5): every startup step
fails closed with a non-zero exit code and one log line that names the offending key or file,
never a value (zebra_printer style, NFR-1):

1. Configure logging (C14) at ``LOG_LEVEL`` (default ``INFO``). An unknown level name exits 1.
2. Load the manifest (C2) → exit 2 with ``manifest_invalid key=<path> reason=<text>`` (FR-2.5).
3. If ``service.env_file`` is set and the file exists, load it with ``python-dotenv``
   (``override=False``: a variable already in the process environment wins). The file holds the
   bearer token (DD-6); its contents are never logged. An unreadable file exits 3.
4. ``load_expected_token(manifest.auth.bearer_token_ref)`` → exit 3 on ``AuthConfigError``
   (FR-3.4). The value is discarded at once; ``create_app`` reads it again for the auth check.
5. Open the certificate and key files and build an ``ssl.SSLContext`` with ``load_cert_chain``
   → exit 4 on a missing/unreadable file, an invalid pair or a password-protected key (FR-3.3).
   A password-protected key is refused because OpenSSL would otherwise prompt on the console.
6. ``uvicorn.run`` with TLS as the only listener (FR-3.2): the manifest port on
   ``service.bind``, ``timeout_graceful_shutdown=5`` (S7 step 1), no uvicorn access log (the
   service logs its own ``http_request`` event; the access log records the raw request line) and
   no ``Server`` header. ``lifespan="on"`` makes a lifespan failure stop the server instead of
   being treated as "lifespan unsupported", so the registry startup and the shutdown stop (FR-11.1)
   always run.

Stop budget (S7, carry-forward T14-C3): WinSW sends Ctrl+C and waits ``stoptimeout`` 30 s. Uvicorn
drains requests for at most 5 s, then the lifespan runs ``registry.shutdown`` with the deadline
``service.shutdown_timeout_s`` (10 s in the shipped manifest). That deadline is soft by up to one
transaction in flight (about 15.8 s with retries), so the common case ends well inside 30 s and the
worst case is about 5 + 10 + 15.8 s.
"""

from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Final, NoReturn

import dotenv
import uvicorn

from inheco_tec_api import logging_setup
from inheco_tec_api._vendor.instrument_api_core import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV_VAR,
    AuthConfigError,
    load_expected_token,
)
from inheco_tec_api.app import create_app
from inheco_tec_api.manifest import Manifest, ManifestError, load_manifest

__all__ = [
    "EXIT_AUTH_INVALID",
    "EXIT_LOG_LEVEL_INVALID",
    "EXIT_MANIFEST_INVALID",
    "EXIT_TLS_INVALID",
    "GRACEFUL_SHUTDOWN_S",
    "main",
]

EXIT_LOG_LEVEL_INVALID: Final = 1
EXIT_MANIFEST_INVALID: Final = 2
EXIT_AUTH_INVALID: Final = 3
EXIT_TLS_INVALID: Final = 4

GRACEFUL_SHUTDOWN_S: Final = 5
"""Seconds uvicorn drains open requests before the lifespan shutdown (C13 step 6, S7 step 1)."""

USAGE: Final = (
    "usage: inheco-tec-api [-h | --help]\n"
    "\n"
    "Runs the inheco-tec API service. The command takes no arguments.\n"
    "It loads the manifest from the file named by INHECO_TEC_MANIFEST\n"
    "(default: the shipped instrument.yaml), reads its settings from the\n"
    "environment, and serves the API until it is stopped.\n"
)
"""Help text of exactly ``-h`` or ``--help`` (FR-2.12), printed before any startup step."""

EVENT_LOG_LEVEL_INVALID: Final = "log_level_invalid"
EVENT_MANIFEST_INVALID: Final = "manifest_invalid"
EVENT_ENV_FILE_LOADED: Final = "env_file_loaded"
EVENT_ENV_FILE_ABSENT: Final = "env_file_absent"
EVENT_ENV_FILE_UNREADABLE: Final = "env_file_unreadable"
EVENT_AUTH_CONFIG_INVALID: Final = "auth_config_invalid"
EVENT_TLS_INVALID: Final = "tls_invalid"
EVENT_SERVICE_STARTING: Final = "service_starting"

_log = logging_setup.get_logger(__name__)


class _KeyPasswordRequired(Exception):
    """Raised by the password callback: the private key is encrypted (refused, see step 5)."""


def _refuse_password() -> str:
    raise _KeyPasswordRequired


def _fail(code: int, event: str, **fields: str) -> NoReturn:
    """Log one error line (key and file names only) and exit with ``code``."""
    _log.error(event, **fields)
    raise SystemExit(code)


def _configure_logging() -> None:
    """Step 1: logging at ``LOG_LEVEL``; an unknown level logs at INFO and exits 1."""
    level = os.environ.get(LOG_LEVEL_ENV_VAR, "") or DEFAULT_LOG_LEVEL
    if not isinstance(logging.getLevelNamesMapping().get(level.upper()), int):
        logging_setup.configure(DEFAULT_LOG_LEVEL)
        _fail(EXIT_LOG_LEVEL_INVALID, EVENT_LOG_LEVEL_INVALID, key=LOG_LEVEL_ENV_VAR)
    logging_setup.configure(level)


def _load_manifest() -> Manifest:
    """Step 2: the validated manifest, or exit 2 naming the key (FR-2.5)."""
    try:
        return load_manifest()
    except ManifestError as exc:
        _fail(EXIT_MANIFEST_INVALID, EVENT_MANIFEST_INVALID, key=exc.key_path, reason=exc.reason)


def _load_env_file(manifest: Manifest) -> None:
    """Step 3: load ``service.env_file`` without overriding the environment (DD-6).

    Only the file path is logged, never a variable name or value from the file.
    """
    env_file = manifest.service.env_file
    if env_file is None:
        return
    path = Path(env_file)
    if not path.exists():
        _log.info(EVENT_ENV_FILE_ABSENT, key="service.env_file", file=env_file)
        return
    try:
        dotenv.load_dotenv(path, override=False)
    except (OSError, UnicodeDecodeError) as exc:
        _fail(
            EXIT_AUTH_INVALID,
            EVENT_ENV_FILE_UNREADABLE,
            key="service.env_file",
            file=env_file,
            reason=type(exc).__name__,
        )
    _log.info(EVENT_ENV_FILE_LOADED, key="service.env_file", file=env_file)


def _check_token(manifest: Manifest) -> None:
    """Step 4: the env var named by ``auth.bearer_token_ref`` is set (FR-3.4); value discarded."""
    env_var = manifest.auth.bearer_token_ref  # pragma: allowlist secret
    try:
        load_expected_token(env_var)
    except AuthConfigError:
        _fail(
            EXIT_AUTH_INVALID,
            EVENT_AUTH_CONFIG_INVALID,
            key="auth.bearer_token_ref",
            env_var=env_var,
            reason="env var is unset, empty or whitespace-only",
        )


def _check_tls(manifest: Manifest) -> None:
    """Step 5: both files open for reading and form a valid unencrypted pair (FR-3.3).

    Errors carry the manifest key, the file path and the exception type, never file content.
    """
    cert_file, key_file = manifest.tls.cert_file, manifest.tls.key_file
    for key, file in (("tls.cert_file", cert_file), ("tls.key_file", key_file)):
        try:
            with open(file, "rb"):
                pass
        except OSError as exc:
            _fail(
                EXIT_TLS_INVALID,
                EVENT_TLS_INVALID,
                key=key,
                file=file,
                reason=f"cannot open file: {type(exc).__name__}",
            )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        # Third argument: the key passphrase callback, which refuses an encrypted key.
        context.load_cert_chain(cert_file, key_file, _refuse_password)
    except _KeyPasswordRequired:
        _fail(
            EXIT_TLS_INVALID,
            EVENT_TLS_INVALID,
            key="tls.key_file",
            file=key_file,
            reason="password-protected private key is not supported",
        )
    except (ssl.SSLError, OSError, ValueError) as exc:
        _fail(
            EXIT_TLS_INVALID,
            EVENT_TLS_INVALID,
            key="tls",
            file=f"{cert_file}, {key_file}",
            reason=f"invalid certificate/key pair: {type(exc).__name__}",
        )


def main() -> None:
    """Run the fail-closed startup (C13 steps 1-5), then serve over TLS only (step 6)."""
    if sys.argv[1:] in (["-h"], ["--help"]):
        sys.stdout.write(USAGE)
        raise SystemExit(0)
    _configure_logging()
    manifest = _load_manifest()
    _load_env_file(manifest)
    _check_token(manifest)
    _check_tls(manifest)
    app = create_app(manifest)
    _log.info(EVENT_SERVICE_STARTING, transport=manifest.transport.kind)
    uvicorn.run(
        app,
        host=manifest.service.bind,
        port=manifest.port,
        ssl_certfile=manifest.tls.cert_file,
        ssl_keyfile=manifest.tls.key_file,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        access_log=False,
        server_header=False,
        lifespan="on",
    )

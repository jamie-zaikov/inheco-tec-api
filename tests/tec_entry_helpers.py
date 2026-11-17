"""Shared helpers for the C13 entry-point suites (not a test module; Task 19).

- :class:`Rig` runs ``entry.main()`` in-process: the manifest is written under ``tmp_path`` (fake
  transport, zero delays), ``uvicorn.run`` is monkeypatched to record its call (no port is bound),
  and the captured output is checked for every registered sentinel (NFR-1).
- :func:`make_tls_pairs` makes throwaway self-signed pairs with the ``openssl`` CLI in a tmp dir
  (``cryptography`` is not a dependency), so no PEM material is ever in the repository.
- :func:`restored_logging` saves and restores the root logger and structlog config around a run.

The tests directory has no ``__init__.py`` (design Risk R8).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from inheco_tec_api import entry
from inheco_tec_api._vendor.instrument_api_core import LOG_LEVEL_ENV_VAR
from inheco_tec_api.manifest import MANIFEST_ENV_VAR, TRANSPORT_ENV_VAR
from tec_api_helpers import sentinel
from tec_manifest_helpers import base_manifest, write_manifest

BEARER_ENV: str = base_manifest()["auth"]["bearer_token_ref"]
"""The env var NAME the manifest points at (never a value)."""

OPENSSL = shutil.which("openssl")
needs_openssl = pytest.mark.skipif(
    OPENSSL is None, reason="openssl CLI not on PATH; cannot make a throwaway TLS pair"
)


# -- helpers ------------------------------------------------------------------------------------


@contextmanager
def restored_logging() -> Iterator[None]:
    """``main`` reconfigures logging; save and restore root handlers and structlog config."""
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


def _openssl(*args: str, env: dict[str, str] | None = None) -> None:
    assert OPENSSL is not None
    run_env = dict(os.environ if env is None else env)
    run_env["MSYS_NO_PATHCONV"] = "1"  # keep "/CN=..." literal under Git Bash
    subprocess.run(
        [OPENSSL, *args],
        check=True,
        capture_output=True,
        env=run_env,
        cwd=tempfile.gettempdir(),
        timeout=60,
    )


def _make_pair(directory: Path, name: str) -> tuple[Path, Path]:
    cert, key = directory / f"{name}.crt", directory / f"{name}.key"
    _openssl(
        "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost",
    )  # fmt: skip
    return cert, key


def make_tls_pairs(directory: Path) -> dict[str, Path]:
    """Two valid pairs (``a``, ``b``) and an encrypted copy of key ``a``, all under ``directory``.

    The encryption phrase is random and passed to openssl through an env var, never a literal.
    """
    cert_a, key_a = _make_pair(directory, "a")
    cert_b, key_b = _make_pair(directory, "b")
    encrypted = directory / "a-encrypted.key"
    phrase_env = "T19_KEY_PHRASE"
    env = dict(os.environ)
    env[phrase_env] = secrets.token_hex(16)
    _openssl(
        "pkey", "-in", str(key_a), "-out", str(encrypted), "-aes256",
        "-passout", f"env:{phrase_env}", env=env,
    )  # fmt: skip
    return {
        "cert_a": cert_a, "key_a": key_a, "cert_b": cert_b, "key_b": key_b,
        "key_a_encrypted": encrypted,
    }  # fmt: skip


class Rig:
    """One ``main()`` run: manifest under ``tmp_path``, stubbed ``uvicorn.run``, captured output."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.run_lifespan = False
        self.registry_stopping: bool | None = None
        self.markers: list[str] = []
        for name in (LOG_LEVEL_ENV_VAR, TRANSPORT_ENV_VAR, BEARER_ENV, MANIFEST_ENV_VAR):
            monkeypatch.delenv(name, raising=False)
        self.data = base_manifest()
        self.data["transport"].update(kind="fake", pre_send_delay_ms=0, retry_delay_ms=0)
        self.data["service"].update(
            env_file=None, state_file=str(tmp_path / "state.json"), shutdown_timeout_s=2
        )
        self.data["tls"].update(
            cert_file=str(tmp_path / "missing.crt"), key_file=str(tmp_path / "missing.key")
        )
        monkeypatch.setattr("inheco_tec_api.entry.uvicorn.run", self._fake_run)
        self.output = ""

    def _fake_run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        if self.run_lifespan:
            app = args[0]
            with TestClient(app) as client:
                assert client.get("/healthz").json() == {"status": "ok"}
            self.registry_stopping = app.state.registry.stopping

    def set_bearer(self, value: str | None = None) -> str:
        value = sentinel("bearer") if value is None else value
        self.monkeypatch.setenv(BEARER_ENV, value)
        if value.strip():
            self.markers.append(value)
        return value

    def use_tls(self, cert: Path, key: Path) -> None:
        self.data["tls"].update(cert_file=str(cert), key_file=str(key))

    def write(self) -> Path:
        path = write_manifest(self.tmp_path, self.data)
        self.monkeypatch.setenv(MANIFEST_ENV_VAR, str(path))
        return path

    def main(self, expect_exit: int | None = None) -> list[dict[str, Any]]:
        """Run ``main``; return the JSON log events. Assert the exit code and no marker leak."""
        if MANIFEST_ENV_VAR not in os.environ:
            self.write()
        if expect_exit is None:
            entry.main()
        else:
            with pytest.raises(SystemExit) as info:
                entry.main()
            assert info.value.code == expect_exit
            assert self.calls == [], "uvicorn.run must not be called after a startup failure"
        captured = self.capsys.readouterr()
        self.output = captured.out + captured.err
        for marker in self.markers:
            assert marker not in self.output, "a secret value reached the captured output"
        return [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]


def one(events: list[dict[str, Any]], name: str) -> dict[str, Any]:
    found = [e for e in events if e.get("event") == name]
    assert len(found) == 1, f"expected one {name!r} event, got {events}"
    return found[0]

"""Tests for C13 ``entry.main`` (Task 19; FR-2.5, FR-3.2, FR-3.4, FR-11.1, FR-13.4, NFR-1, NFR-6).

Every test runs ``main()`` in-process through ``tec_entry_helpers.Rig``: ``uvicorn.run`` is
monkeypatched, so no port is bound and no server starts. The manifest and the env file live under
``tmp_path``; TLS pairs are throwaway ``openssl`` pairs in a tmp dir. When ``openssl`` is not on
``PATH`` only the cases that need a valid pair are skipped. The bearer value and every env-file
value are runtime sentinels; each run asserts that none reaches the captured output.

The manifest always selects the fake transport, so the real ``hid`` module is never imported.
The TLS checks (FR-3.3) are in ``test_tec_entry_tls.py``, the WinSW XML in
``test_tec_entry_service.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api import entry
from inheco_tec_api._vendor.instrument_api_core import LOG_LEVEL_ENV_VAR
from inheco_tec_api.manifest import MANIFEST_ENV_VAR, TRANSPORT_ENV_VAR
from tec_api_helpers import sentinel
from tec_entry_helpers import (
    BEARER_ENV,
    OPENSSL,
    Rig,
    make_tls_pairs,
    needs_openssl,
    one,
    restored_logging,
)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    with restored_logging():
        yield


@pytest.fixture(scope="session")
def tls_dir(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if OPENSSL is None:
        pytest.skip("openssl CLI not on PATH; cannot make a throwaway TLS pair")
    return make_tls_pairs(tmp_path_factory.mktemp("tls"))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Rig:
    return Rig(tmp_path, monkeypatch, capsys)


# -- step 1: logging level -----------------------------------------------------------------------


def test_nfr6_unknown_log_level_exits_1_naming_the_key(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    level = "T19-NOT-A-LEVEL"
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, level)
    events = rig.main(expect_exit=entry.EXIT_LOG_LEVEL_INVALID)
    event = one(events, entry.EVENT_LOG_LEVEL_INVALID)
    assert event["level"] == "error"
    assert event["key"] == LOG_LEVEL_ENV_VAR
    assert level not in rig.output


def test_nfr6_log_level_name_is_case_insensitive(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tls_dir: dict[str, Path]
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "debug")
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])
    rig.main()
    assert len(rig.calls) == 1


# -- step 2: manifest ----------------------------------------------------------------------------


def test_fr2_5_missing_manifest_exits_2_with_key(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MANIFEST_ENV_VAR, str(rig.tmp_path / "absent.yaml"))
    rig.set_bearer()
    events = rig.main(expect_exit=entry.EXIT_MANIFEST_INVALID)
    event = one(events, entry.EVENT_MANIFEST_INVALID)
    assert event["level"] == "error"
    assert event["key"] == "manifest"
    assert event["reason"]


def test_fr2_5_invalid_manifest_key_exits_2_naming_the_key(rig: Rig) -> None:
    rig.data["controllers"][0]["id"] = 9  # ControllerID outside 0-7
    rig.set_bearer()
    events = rig.main(expect_exit=entry.EXIT_MANIFEST_INVALID)
    event = one(events, entry.EVENT_MANIFEST_INVALID)
    assert event["key"].startswith("controllers[0]") or event["key"].startswith("controllers.0")
    assert "id" in event["key"]
    assert event["reason"]


def test_fr2_5_manifest_checked_before_token(rig: Rig) -> None:
    """An invalid manifest exits 2 even when the token is also unset (startup order, C13)."""
    rig.data["port"] = "not-a-port"
    events = rig.main(expect_exit=entry.EXIT_MANIFEST_INVALID)
    assert one(events, entry.EVENT_MANIFEST_INVALID)["key"] == "port"
    assert not [e for e in events if e.get("event") == entry.EVENT_AUTH_CONFIG_INVALID]


# -- step 3: env file ----------------------------------------------------------------------------


@needs_openssl
def test_nfr1_env_file_supplies_the_token_and_is_not_logged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tls_dir: dict[str, Path]
) -> None:
    from_file = sentinel("from-file")
    other_name, other_value = "T19_OTHER_SETTING", sentinel("other")
    monkeypatch.delenv(other_name, raising=False)  # restored (removed) at teardown
    env_file = rig.tmp_path / "service.env"
    env_file.write_text(f"{BEARER_ENV}={from_file}\n{other_name}={other_value}\n", "utf-8")
    rig.data["service"]["env_file"] = str(env_file)
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])
    rig.markers += [from_file, other_value]

    events = rig.main()

    assert os.environ[BEARER_ENV] == from_file
    assert os.environ[other_name] == other_value
    loaded = one(events, entry.EVENT_ENV_FILE_LOADED)
    assert loaded["key"] == "service.env_file"
    assert loaded["file"] == str(env_file)
    assert other_name not in rig.output
    assert len(rig.calls) == 1


@needs_openssl
def test_nfr1_env_file_does_not_override_an_existing_var(
    rig: Rig, tls_dir: dict[str, Path]
) -> None:
    preset = rig.set_bearer()
    from_file = sentinel("from-file")
    env_file = rig.tmp_path / "service.env"
    env_file.write_text(f"{BEARER_ENV}={from_file}\n", "utf-8")
    rig.data["service"]["env_file"] = str(env_file)
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])
    rig.markers.append(from_file)

    rig.main()

    assert os.environ[BEARER_ENV] == preset  # override=False: the process value wins
    assert len(rig.calls) == 1


def test_fr3_4_absent_env_file_is_logged_and_the_token_check_decides(rig: Rig) -> None:
    rig.data["service"]["env_file"] = str(rig.tmp_path / "no-such.env")
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    absent = one(events, entry.EVENT_ENV_FILE_ABSENT)
    assert absent["key"] == "service.env_file"
    assert absent["level"] == "info"
    assert one(events, entry.EVENT_AUTH_CONFIG_INVALID)["key"] == "auth.bearer_token_ref"


def test_nfr1_undecodable_env_file_exits_3_without_content(rig: Rig) -> None:
    marker = sentinel("raw")
    env_file = rig.tmp_path / "service.env"
    env_file.write_bytes(b"\xff\xfe\x80" + f"{BEARER_ENV}={marker}".encode() + b"\x81\n")
    rig.data["service"]["env_file"] = str(env_file)
    rig.markers.append(marker)
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    event = one(events, entry.EVENT_ENV_FILE_UNREADABLE)
    assert event["level"] == "error"
    assert event["key"] == "service.env_file"
    assert event["file"] == str(env_file)
    assert event["reason"] == "UnicodeDecodeError"


def test_nfr1_env_file_that_cannot_be_opened_exits_3(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError from the dotenv read (e.g. an ACL denial) exits 3 with the type name only."""
    env_file = rig.tmp_path / "service.env"
    env_file.write_text("", "utf-8")
    rig.data["service"]["env_file"] = str(env_file)
    marker = sentinel("os-error")
    rig.markers.append(marker)

    def deny(*_args: Any, **_kwargs: Any) -> bool:
        raise PermissionError(marker)

    monkeypatch.setattr("inheco_tec_api.entry.dotenv.load_dotenv", deny)
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    event = one(events, entry.EVENT_ENV_FILE_UNREADABLE)
    assert event["key"] == "service.env_file"
    assert event["reason"] == "PermissionError"


def test_fr3_4_env_file_path_that_is_a_directory_still_fails_closed(rig: Rig) -> None:
    """python-dotenv skips a non-file path silently; the token check still exits 3."""
    directory = rig.tmp_path / "service.env"
    directory.mkdir()
    rig.data["service"]["env_file"] = str(directory)
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    assert one(events, entry.EVENT_AUTH_CONFIG_INVALID)["key"] == "auth.bearer_token_ref"


# -- step 4: token -------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   \t "], ids=["unset", "empty", "whitespace"])
def test_fr3_4_unset_or_blank_token_exits_3(rig: Rig, value: str | None) -> None:
    if value is not None:
        rig.set_bearer(value)
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    event = one(events, entry.EVENT_AUTH_CONFIG_INVALID)
    assert event["level"] == "error"
    assert event["key"] == "auth.bearer_token_ref"
    assert event["env_var"] == BEARER_ENV  # the NAME only


def test_fr3_4_token_checked_before_tls(rig: Rig) -> None:
    """Token unset and TLS files missing: exit 3, no TLS check ran (startup order, C13)."""
    events = rig.main(expect_exit=entry.EXIT_AUTH_INVALID)
    assert not [e for e in events if e.get("event") == entry.EVENT_TLS_INVALID]


# -- step 6: the TLS-only listener ---------------------------------------------------------------


@needs_openssl
def test_fr3_2_valid_config_runs_uvicorn_once_tls_only(rig: Rig, tls_dir: dict[str, Path]) -> None:
    rig.set_bearer()
    rig.data["port"] = 18743
    rig.data["service"]["bind"] = "127.0.0.1"
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])

    events = rig.main()

    assert len(rig.calls) == 1
    args, kwargs = rig.calls[0]
    assert len(args) == 1 and hasattr(args[0], "state")  # the app built by create_app
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 18743,
        "ssl_certfile": str(tls_dir["cert_a"]),
        "ssl_keyfile": str(tls_dir["key_a"]),
        "timeout_graceful_shutdown": entry.GRACEFUL_SHUTDOWN_S,
        "access_log": False,
        "server_header": False,
        "lifespan": "on",
    }
    assert entry.GRACEFUL_SHUTDOWN_S == 5
    for plain in ("uds", "fd", "http"):  # no second or plain-HTTP listener
        assert plain not in kwargs
    starting = one(events, entry.EVENT_SERVICE_STARTING)
    assert starting["transport"] == "fake"


@needs_openssl
def test_fr13_4_fake_transport_warns_at_startup_and_fr11_1_lifespan_stops(
    rig: Rig, tls_dir: dict[str, Path]
) -> None:
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])
    rig.run_lifespan = True

    events = rig.main()

    warning = one(events, "transport_fake_active")
    assert warning["level"] == "warning"
    assert rig.registry_stopping is True  # the lifespan shutdown stop ran (FR-11.1, S7)


@needs_openssl
def test_fr13_4_transport_env_override_selects_fake(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, tls_dir: dict[str, Path]
) -> None:
    rig.data["transport"]["kind"] = "hidapi"
    monkeypatch.setenv(TRANSPORT_ENV_VAR, "fake")
    rig.set_bearer()
    rig.use_tls(tls_dir["cert_a"], tls_dir["key_a"])
    events = rig.main()
    assert one(events, entry.EVENT_SERVICE_STARTING)["transport"] == "fake"


def test_nfr1_entry_import_does_not_load_hid(api_root: Path) -> None:
    """The console-script module never imports the native ``hid`` module (T7-S4)."""
    code = "import sys, inheco_tec_api.entry; print('hid' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=api_root,
        check=True,
    )
    assert result.stdout.strip() == "False"

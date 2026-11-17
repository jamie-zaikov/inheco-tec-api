"""Tests of the console-script help handler of ``inheco_tec_api.entry.main``.

Exactly one argument ``-h`` or ``--help`` prints the usage text and exits with status 0 before
any startup step; every other invocation keeps the startup path of the service. Recording stubs
replace the startup steps, ``create_app`` and ``uvicorn.run``, so no manifest, token, TLS file or
socket is touched. The console-script name comes from the repo's own ``pyproject.toml``.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from inheco_tec_api import entry

ENTRY_POINT = "inheco_tec_api.entry:main"

STARTUP_STEPS = (
    "_configure_logging",
    "_load_manifest",
    "_load_env_file",
    "_check_token",
    "_check_tls",
    "create_app",
)


def _script_name(api_root: Path) -> str:
    data = tomllib.loads((api_root / "pyproject.toml").read_text("utf-8"))
    names = [name for name, target in data["project"]["scripts"].items() if target == ENTRY_POINT]
    assert len(names) == 1, f"one console script runs {ENTRY_POINT}: {names}"
    return names[0]


def _fake_manifest() -> SimpleNamespace:
    return SimpleNamespace(
        transport=SimpleNamespace(kind="fake"),
        service=SimpleNamespace(bind="localhost"),
        port=0,
        tls=SimpleNamespace(cert_file="unused-cert", key_file="unused-key"),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub every startup step and ``uvicorn.run``; return the names of the stubs called."""
    recorded: list[str] = []
    manifest = _fake_manifest()

    def stub(name: str, result: object = None) -> object:
        def _call(*_args: object, **_kwargs: object) -> object:
            recorded.append(name)
            return result

        return _call

    for name in STARTUP_STEPS:
        result: object = manifest if name == "_load_manifest" else None
        if name == "create_app":
            result = object()
        monkeypatch.setattr(entry, name, stub(name, result))
    quiet = SimpleNamespace(info=lambda *_a, **_k: None, error=lambda *_a, **_k: None)
    monkeypatch.setattr(entry, "_log", quiet)
    monkeypatch.setattr(entry.uvicorn, "run", stub("uvicorn.run"))
    return recorded


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flag_prints_usage_and_exits_zero_without_startup(
    flag: str,
    calls: list[str],
    api_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rejects: a handler that exits non-zero, prints no usage, or still starts the service."""
    script = _script_name(api_root)
    monkeypatch.setattr(sys, "argv", [script, flag])

    with pytest.raises(SystemExit) as exc_info:
        entry.main()

    assert exc_info.value.code == 0
    assert script in capsys.readouterr().out
    assert calls == []


@pytest.mark.parametrize("args", [[], ["--help", "extra"], ["-x"]])
def test_other_invocations_keep_the_startup_path(
    args: list[str],
    calls: list[str],
    api_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejects: a handler that intercepts more than exactly one ``-h`` / ``--help`` argument."""
    monkeypatch.setattr(sys, "argv", [_script_name(api_root), *args])

    entry.main()

    assert calls.count("uvicorn.run") == 1
    assert calls[: len(STARTUP_STEPS)] == list(STARTUP_STEPS)

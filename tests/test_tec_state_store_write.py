"""Tests for C10 ``state_store`` write failures and arguments (Task 11; FR-12, FR-12.4; DD-11).

Split from ``test_tec_state_store.py`` for the module size limit. The retry pause is patched to a
recorder (``no_sleep``), so the Windows sharing-violation retries cost no wall time.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api import state_store
from inheco_tec_api.state_store import (
    EVENT_TEC_STATE_FILE_WRITE_FAILED,
    LoadResult,
    ReleaseStateStore,
    StateStoreError,
)
from structlog.testing import capture_logs

_REAL_REPLACE = os.replace


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[float]]:
    """Replace the retry pause with a recorder (the default argument is bound at import)."""
    pauses: list[float] = []
    monkeypatch.setattr(state_store._retry_on_permission_error, "__defaults__", (pauses.append,))
    yield pauses


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


# --- Write failures ------------------------------------------------------------------------------


def test_fr12_persistent_replace_permission_error_raises_and_keeps_file(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    store = ReleaseStateStore(path)
    store.add(0)
    before = path.read_bytes()
    calls: list[int] = []

    def deny(src: Any, dst: Any) -> None:
        calls.append(1)
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(os, "replace", deny)
    with capture_logs() as events, pytest.raises(StateStoreError):
        store.add(1)
    assert len(calls) == 5
    assert len(no_sleep) == 4
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]
    [event] = [e for e in events if e["event"] == EVENT_TEC_STATE_FILE_WRITE_FAILED]
    assert event == {
        "event": EVENT_TEC_STATE_FILE_WRITE_FAILED,
        "log_level": "error",
        "state_file": str(path),
        "operation": "add",
        "controller_id": 1,
        "error": "PermissionError",
    }
    monkeypatch.setattr(os, "replace", _REAL_REPLACE)
    assert ReleaseStateStore(path).load() == LoadResult(released={0}, corrupt=False)


def test_fr12_failed_remove_raises_and_keeps_id_persisted(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReleaseStateStore(path)
    store.add(0)

    def deny(src: Any, dst: Any) -> None:
        raise PermissionError(13, "sharing violation")

    monkeypatch.setattr(os, "replace", deny)
    with pytest.raises(StateStoreError):
        store.remove(0)
    monkeypatch.setattr(os, "replace", _REAL_REPLACE)
    assert store.load().released == {0}
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_fr12_transient_replace_permission_error_succeeds(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    attempts: list[int] = []

    def flaky(src: Any, dst: Any) -> None:
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "sharing violation")
        _REAL_REPLACE(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with capture_logs() as events:
        ReleaseStateStore(path).add(6)
    assert len(attempts) == 3
    assert len(no_sleep) == 2
    assert [e for e in events if e["event"] == EVENT_TEC_STATE_FILE_WRITE_FAILED] == []
    assert ReleaseStateStore(path).load() == LoadResult(released={6}, corrupt=False)
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_fr12_other_os_error_on_replace_is_not_retried(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    calls: list[int] = []

    def fail(src: Any, dst: Any) -> None:
        calls.append(1)
        raise OSError(28, "no space")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StateStoreError):
        ReleaseStateStore(path).add(0)
    assert len(calls) == 1
    assert no_sleep == []
    assert list(path.parent.iterdir()) == []


def test_fr12_temp_file_creation_failure_raises_and_keeps_file(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ReleaseStateStore(path)
    store.add(0)
    before = path.read_bytes()

    def no_temp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise PermissionError(13, "directory not writable")

    monkeypatch.setattr(tempfile, "mkstemp", no_temp)
    with capture_logs() as events, pytest.raises(StateStoreError):
        store.add(1)
    assert path.read_bytes() == before
    assert [e["event"] for e in events] == [EVENT_TEC_STATE_FILE_WRITE_FAILED]


# --- Argument validation -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, 1.5, "1", None, [0]])
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_fr12_bad_controller_id_raises_value_error(path: Path, bad: Any, operation: str) -> None:
    store = ReleaseStateStore(path)
    with pytest.raises(ValueError):
        getattr(store, operation)(bad)
    assert not path.exists()

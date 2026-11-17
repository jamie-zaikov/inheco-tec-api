"""Tests for C10 ``ReleaseStateStore.replace_all`` (Task 14 addition; DD-11, FR-12.4).

Carry-forward T11-E/T11-V: after a corrupt load the registry writes the full released set in ONE
atomic write. ``replace_all`` always writes, runs under the store lock, validates every ID before
any I/O, and on failure raises ``StateStoreError`` with the file on disk unchanged.
"""

from __future__ import annotations

import json
import os
import threading
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
    pauses: list[float] = []
    monkeypatch.setattr(state_store._retry_on_permission_error, "__defaults__", (pauses.append,))
    yield pauses


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def replaces(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def counting(src: Any, dst: Any) -> None:
        calls.append(dst)
        _REAL_REPLACE(src, dst)

    monkeypatch.setattr(os, "replace", counting)
    return calls


def test_dd11_replace_all_writes_exactly_the_given_set(path: Path) -> None:
    store = ReleaseStateStore(path)
    store.add(5)
    store.replace_all([3, 0, 1])
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "released_controllers": [0, 1, 3],
    }
    assert store.load() == LoadResult(released={0, 1, 3}, corrupt=False)
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_dd11_replace_all_always_writes_even_when_unchanged(
    path: Path, replaces: list[Any]
) -> None:
    store = ReleaseStateStore(path)
    store.replace_all({0, 1})
    store.replace_all({0, 1})
    assert len(replaces) == 2


def test_dd11_replace_all_with_an_empty_set_writes_an_empty_list(path: Path) -> None:
    store = ReleaseStateStore(path)
    store.add(2)
    store.replace_all([])
    assert store.load() == LoadResult(released=set(), corrupt=False)


def test_dd11_replace_all_over_a_corrupt_file_makes_it_valid(path: Path) -> None:
    path.write_text("{ broken", encoding="utf-8")
    store = ReleaseStateStore(path)
    assert store.load().corrupt
    store.replace_all([0, 1])
    assert store.load() == LoadResult(released={0, 1}, corrupt=False)


@pytest.mark.parametrize("bad", [-1, True, "1", 1.0])
def test_dd11_replace_all_rejects_an_invalid_id_before_any_write(
    path: Path, replaces: list[Any], bad: Any
) -> None:
    store = ReleaseStateStore(path)
    with pytest.raises(ValueError):
        store.replace_all([0, bad])
    assert replaces == []
    assert not path.exists()


def test_dd11_failed_replace_all_raises_and_keeps_the_file(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path.write_text("{ broken", encoding="utf-8")
    before = path.read_bytes()

    def deny(src: Any, dst: Any) -> None:
        raise OSError(5, "io error")

    monkeypatch.setattr(os, "replace", deny)
    store = ReleaseStateStore(path)
    with capture_logs() as events, pytest.raises(StateStoreError):
        store.replace_all([0, 1])
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]
    [event] = [e for e in events if e["event"] == EVENT_TEC_STATE_FILE_WRITE_FAILED]
    assert event == {
        "event": EVENT_TEC_STATE_FILE_WRITE_FAILED,
        "log_level": "error",
        "state_file": str(path),
        "operation": "replace_all",
        "error": "OSError",
    }


def test_dd11_replace_all_waits_for_the_store_lock(path: Path) -> None:
    store = ReleaseStateStore(path)
    done = threading.Event()

    def write() -> None:
        store.replace_all([0, 1])
        done.set()

    with store._lock:
        worker = threading.Thread(target=write)
        worker.start()
        assert not done.wait(0.2)  # blocked while another holder has the lock
        assert not path.exists()
    worker.join(5)
    assert done.is_set()
    assert store.load().released == {0, 1}

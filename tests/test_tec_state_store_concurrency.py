"""Concurrency tests for C10 ``state_store`` (Task 11 attempt 2; FR-12.4, FR-12.1, FR-12.3).

HTTP handlers hold only per-controller locks, so releases of different controllers can update the
state file at the same time. A lost update would drop a ``released`` controller from the file, and
a restart would then open a device that VENUS holds (FR-12.4). These tests use real threads that
start together at a :class:`threading.Barrier`.

The negative control replaces the instance lock with a no-op and forces the read/read/write/write
interleave with a barrier inside the patched read, so it proves deterministically that the
scenario detects a lost update when the lock is absent.

``os.fsync`` is patched to a no-op: it costs several milliseconds per write on Windows and has no
effect on the in-process ordering under test. The temp-file write and ``os.replace`` stay real.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from inheco_tec_api.state_store import ReleaseStateStore

_TRIALS = 30
_THREADS = 8
_JOIN_TIMEOUT_S = 10.0


@pytest.fixture(autouse=True)
def no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _run_together(actions: list[Callable[[], None]]) -> list[BaseException]:
    """Run each action in its own thread, released together by a barrier; return the errors."""
    barrier = threading.Barrier(len(actions))
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(action: Callable[[], None]) -> None:
        try:
            barrier.wait(timeout=_JOIN_TIMEOUT_S)
            action()
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT_S)
        assert not thread.is_alive(), "worker thread did not finish"
    return errors


def _bind_add(store: ReleaseStateStore, cid: int) -> Callable[[], None]:
    return lambda: store.add(cid)


def _bind_remove(store: ReleaseStateStore, cid: int) -> Callable[[], None]:
    return lambda: store.remove(cid)


def test_fr_12_4_concurrent_adds_lose_no_update(tmp_path: Path) -> None:
    """8 threads add 0..7 on one store at once; every trial persists exactly {0..7}."""
    for trial in range(_TRIALS):
        store = ReleaseStateStore(tmp_path / f"state-{trial}.json")
        errors = _run_together([_bind_add(store, cid) for cid in range(_THREADS)])
        assert errors == []
        result = store.load()
        assert result.corrupt is False, f"trial {trial}"
        assert result.released == set(range(_THREADS)), f"trial {trial}"
        # A fresh instance (a restart, FR-12.4) reads the same set from disk.
        assert ReleaseStateStore(store.path).load().released == set(range(_THREADS))


def test_fr_12_1_fr_12_3_concurrent_mixed_add_remove_reach_expected_set(tmp_path: Path) -> None:
    """From {0..3}: 4 threads remove 0..3 and 4 threads add 4..7 at once; the file holds {4..7}."""
    start = set(range(4))
    expected = set(range(4, _THREADS))
    for trial in range(_TRIALS):
        store = ReleaseStateStore(tmp_path / f"state-{trial}.json")
        for cid in sorted(start):
            store.add(cid)
        actions = [_bind_remove(store, cid) for cid in sorted(start)]
        actions += [_bind_add(store, cid) for cid in sorted(expected)]
        errors = _run_together(actions)
        assert errors == []
        result = store.load()
        assert result.corrupt is False, f"trial {trial}"
        assert result.released == expected, f"trial {trial}"


def test_fr_12_4_lock_prevents_loss_with_delay_between_read_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delay between the read and the write (a wide race window) still loses no update."""
    store = ReleaseStateStore(tmp_path / "state.json")
    original = store._base_for_update

    def slow_read(cid: int, operation: str) -> set[int] | None:
        current = original(cid, operation)
        time.sleep(0.002)
        return current

    monkeypatch.setattr(store, "_base_for_update", slow_read)
    for trial in range(5):
        for cid in range(_THREADS):
            store.remove(cid)
        errors = _run_together([_bind_add(store, cid) for cid in range(_THREADS)])
        assert errors == []
        assert store.load().released == set(range(_THREADS)), f"trial {trial}"


def test_fr_12_4_concurrent_loads_during_adds_are_never_corrupt(tmp_path: Path) -> None:
    """A load that runs during adds sees a valid subset of the final set, never a corrupt file."""
    store = ReleaseStateStore(tmp_path / "state.json")
    loads: list[tuple[bool, frozenset[int]]] = []

    def reader() -> None:
        for _ in range(20):
            result = store.load()
            loads.append((result.corrupt, frozenset(result.released)))

    actions = [_bind_add(store, cid) for cid in range(_THREADS)] + [reader, reader]
    errors = _run_together(actions)
    assert errors == []
    assert len(loads) == 40
    for corrupt, released in loads:
        assert corrupt is False
        assert released <= set(range(_THREADS))
    assert store.load().released == set(range(_THREADS))


def test_negative_control_without_lock_the_scenario_detects_a_lost_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: no-op lock + forced interleave → an update is lost in every trial.

    Both threads read the empty file, wait at a barrier inside the patched read, then each writes
    a set with only its own ID. The second ``os.replace`` wins, so one ID is lost. This proves the
    assertions above can detect the bug that the instance lock fixes.
    """
    lost_trials = 0
    for trial in range(5):
        store = ReleaseStateStore(tmp_path / f"state-{trial}.json")
        monkeypatch.setattr(store, "_lock", contextlib.nullcontext())
        original = store._base_for_update
        interleave = threading.Barrier(2)

        def racing_read(
            cid: int,
            operation: str,
            original: Callable[[int, str], set[int] | None] = original,
            interleave: threading.Barrier = interleave,
        ) -> set[int] | None:
            current = original(cid, operation)
            interleave.wait(timeout=_JOIN_TIMEOUT_S)
            return current

        monkeypatch.setattr(store, "_base_for_update", racing_read)
        errors = _run_together([_bind_add(store, 0), _bind_add(store, 1)])
        assert errors == []
        result = store.load()
        assert result.corrupt is False
        assert len(result.released) == 1
        assert result.released <= {0, 1}
        if result.released != {0, 1}:
            lost_trials += 1
    assert lost_trials == 5

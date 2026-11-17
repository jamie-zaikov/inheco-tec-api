"""Tests for C10 ``state_store`` (Task 11; FR-12, FR-12.4, FR-12.8; design C10, DD-11).

Every test works under ``tmp_path``. The retry pause is patched to a recorder (``no_sleep``), so the
Windows sharing-violation retries cost no wall time and their count can be asserted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api import state_store
from inheco_tec_api.state_store import (
    EVENT_TEC_STATE_FILE_CORRUPT,
    LoadResult,
    ReleaseStateStore,
)
from structlog.testing import capture_logs

# A marker that must never reach a log line (the file contents are never logged).
CONTENT_MARKER = "MARKER-do-not-log-4b9e1d"

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


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def _count_replace(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, Any]]:
    calls: list[tuple[Any, Any]] = []

    def recording_replace(src: Any, dst: Any) -> None:
        calls.append((src, dst))
        _REAL_REPLACE(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    return calls


def _corrupt_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e["event"] == EVENT_TEC_STATE_FILE_CORRUPT]


# --- FR-12 / FR-12.4: round trip and file format -------------------------------------------------


def test_fr12_4_round_trip_survives_new_store_instance(path: Path) -> None:
    store = ReleaseStateStore(str(path))
    assert store.path == path
    store.add(3)
    store.add(0)
    restarted = ReleaseStateStore(path)
    assert restarted.load() == LoadResult(released={0, 3}, corrupt=False)


def test_fr12_4_file_format_is_versioned_sorted_list(path: Path) -> None:
    store = ReleaseStateStore(path)
    store.add(5)
    store.add(1)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "released_controllers": [1, 5],
    }


def test_fr12_remove_persists_reacquire(path: Path) -> None:
    store = ReleaseStateStore(path)
    store.add(0)
    store.add(1)
    store.remove(0)
    assert ReleaseStateStore(path).load() == LoadResult(released={1}, corrupt=False)
    store.remove(1)
    assert ReleaseStateStore(path).load() == LoadResult(released=set(), corrupt=False)
    assert path.exists()


def test_fr12_4_parent_directory_created_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "state.json"
    ReleaseStateStore(target).add(4)
    assert ReleaseStateStore(target).load() == LoadResult(released={4}, corrupt=False)


def test_fr12_4_missing_file_means_none_released(path: Path) -> None:
    with capture_logs() as events:
        result = ReleaseStateStore(path).load()
    assert result == LoadResult(released=set(), corrupt=False)
    assert not path.exists()
    assert _corrupt_events(events) == []


def test_fr12_4_empty_list_loads_as_none_released(path: Path) -> None:
    _write_json(path, {"version": 1, "released_controllers": []})
    assert ReleaseStateStore(path).load() == LoadResult(released=set(), corrupt=False)


# --- Idempotence: no write when the set is unchanged --------------------------------------------


def test_fr12_add_listed_id_writes_nothing(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ReleaseStateStore(path)
    store.add(2)
    before = path.read_bytes()
    calls = _count_replace(monkeypatch)
    store.add(2)
    assert calls == []
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_fr12_remove_absent_id_writes_nothing(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ReleaseStateStore(path)
    store.add(2)
    calls = _count_replace(monkeypatch)
    store.remove(7)
    assert calls == []
    assert ReleaseStateStore(path).load().released == {2}


def test_fr12_remove_on_missing_file_writes_nothing(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_replace(monkeypatch)
    ReleaseStateStore(path).remove(0)
    assert calls == []
    assert not path.exists()


def test_fr12_each_change_writes_once(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_replace(monkeypatch)
    store = ReleaseStateStore(path)
    store.add(0)
    store.remove(0)
    assert len(calls) == 2
    assert all(Path(dst) == path for _src, dst in calls)
    assert all(Path(src).parent == path.parent for src, _dst in calls)


# --- FR-12.8: load never writes (a stop while released never touches the persisted set) --------


def test_fr12_8_load_does_not_modify_valid_file(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_json(path, {"version": 1, "released_controllers": [0]})
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    calls = _count_replace(monkeypatch)
    for _ in range(3):
        ReleaseStateStore(path).load()
    assert calls == []
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_dd11_load_does_not_rewrite_corrupt_file(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path.write_bytes(b"{not json")
    calls = _count_replace(monkeypatch)
    assert ReleaseStateStore(path).load().corrupt is True
    assert calls == []
    assert path.read_bytes() == b"{not json"


# --- No temp file left after a write -------------------------------------------------------------


def test_fr12_4_no_temp_file_left_after_writes(path: Path) -> None:
    store = ReleaseStateStore(path)
    for cid in (0, 1, 2):
        store.add(cid)
    store.remove(1)
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


# --- DD-11: corrupt / wrong version / wrong types → corrupt=True ---------------------------------

CORRUPT_CONTENTS: list[tuple[str, bytes]] = [
    ("invalid_json", b"{not json"),
    ("empty_file", b""),
    ("truncated", b'{"version": 1, "released_controllers": [0'),
    ("top_level_list", b"[0, 1]"),
    ("top_level_null", b"null"),
    ("missing_version", b'{"released_controllers": [0]}'),
    ("version_2", b'{"version": 2, "released_controllers": [0]}'),
    ("version_string", b'{"version": "1", "released_controllers": [0]}'),
    ("version_true", b'{"version": true, "released_controllers": [0]}'),
    ("version_null", b'{"version": null, "released_controllers": [0]}'),
    ("missing_list", b'{"version": 1}'),
    ("list_is_object", b'{"version": 1, "released_controllers": {"0": 1}}'),
    ("list_is_int", b'{"version": 1, "released_controllers": 0}'),
    ("list_is_null", b'{"version": 1, "released_controllers": null}'),
    ("item_string", b'{"version": 1, "released_controllers": ["0"]}'),
    ("item_float", b'{"version": 1, "released_controllers": [1.5]}'),
    ("item_negative", b'{"version": 1, "released_controllers": [-1]}'),
    ("item_null", b'{"version": 1, "released_controllers": [null]}'),
    ("item_true", b'{"version": 1, "released_controllers": [true]}'),
    ("item_false", b'{"version": 1, "released_controllers": [0, false]}'),
    ("item_nested_list", b'{"version": 1, "released_controllers": [[0]]}'),
    ("invalid_utf8", b'{"version": 1, "released_controllers": [0]}\xff\xfe'),
    ("utf8_bom", b'\xef\xbb\xbf{"version": 1, "released_controllers": [0]}'),
]


@pytest.mark.parametrize(
    "contents", [c for _, c in CORRUPT_CONTENTS], ids=[n for n, _ in CORRUPT_CONTENTS]
)
def test_dd11_invalid_file_loads_corrupt_and_logs_error(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    with capture_logs() as events:
        result = ReleaseStateStore(path).load()
    assert result == LoadResult(released=set(), corrupt=True)
    [event] = _corrupt_events(events)
    assert event["log_level"] == "error"
    assert event["state_file"] == str(path)
    assert isinstance(event["reason"], str)
    assert set(event) <= {"event", "log_level", "state_file", "reason", "error"}


@pytest.mark.parametrize(
    ("contents", "reason"),
    [
        (b"{not json", "invalid_json"),
        (b"\xef\xbb\xbf{}", "invalid_json"),
        (b"\xff\xfe\x00", "invalid_encoding"),
        (b"[]", "not_an_object"),
        (b'{"version": true, "released_controllers": []}', "wrong_version"),
        (b'{"version": 1, "released_controllers": [true]}', "wrong_type"),
    ],
)
def test_dd11_corrupt_reason_token(path: Path, contents: bytes, reason: str) -> None:
    path.write_bytes(contents)
    with capture_logs() as events:
        ReleaseStateStore(path).load()
    [event] = _corrupt_events(events)
    assert event["reason"] == reason


@pytest.mark.parametrize(
    "contents",
    [
        f"{{not json {CONTENT_MARKER}".encode(),
        json.dumps({"version": 1, "released_controllers": [CONTENT_MARKER]}).encode(),
        json.dumps({"version": CONTENT_MARKER, "released_controllers": []}).encode(),
        CONTENT_MARKER.encode() + b"\xff",
    ],
    ids=["invalid_json", "wrong_item", "wrong_version", "invalid_utf8"],
)
def test_dd11_file_contents_never_logged(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    store = ReleaseStateStore(path)
    with capture_logs() as events:
        assert store.load().corrupt is True
        store.add(1)
    assert len(_corrupt_events(events)) == 2
    assert CONTENT_MARKER not in repr(events)


@pytest.mark.parametrize(
    "obj",
    [
        {"version": 1, "released_controllers": [0, 1, 1]},  # duplicates collapse to a set
        {"version": 1, "released_controllers": [1, 0], "note": "x"},  # extra keys tolerated
    ],
)
def test_fr12_4_tolerated_valid_variants(path: Path, obj: Any) -> None:
    _write_json(path, obj)
    assert ReleaseStateStore(path).load() == LoadResult(released={0, 1}, corrupt=False)


# --- DD-11: unreadable file (OS errors on read) --------------------------------------------------


def _patch_read_text(
    monkeypatch: pytest.MonkeyPatch, target: Path, behaviour: Callable[[], str]
) -> list[int]:
    calls: list[int] = []
    real_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            calls.append(1)
            return behaviour()
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    return calls


def test_dd11_persistent_permission_error_on_read_is_corrupt_after_retries(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    path.write_bytes(f"{{not json {CONTENT_MARKER}".encode())

    def deny() -> str:
        raise PermissionError(13, f"denied {CONTENT_MARKER}")

    calls = _patch_read_text(monkeypatch, path, deny)
    with capture_logs() as events:
        result = ReleaseStateStore(path).load()
    assert result == LoadResult(released=set(), corrupt=True)
    assert len(calls) == 5
    assert len(no_sleep) == 4
    [event] = _corrupt_events(events)
    assert event["log_level"] == "error"
    assert event == {
        "event": EVENT_TEC_STATE_FILE_CORRUPT,
        "log_level": "error",
        "state_file": str(path),
        "reason": "unreadable",
        "error": "PermissionError",
    }
    assert CONTENT_MARKER not in repr(events)


def test_dd11_other_os_error_on_read_is_corrupt_without_retry(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    _write_json(path, {"version": 1, "released_controllers": [0]})

    def fail() -> str:
        raise OSError(5, "I/O error")

    calls = _patch_read_text(monkeypatch, path, fail)
    with capture_logs() as events:
        result = ReleaseStateStore(path).load()
    assert result.corrupt is True
    assert len(calls) == 1
    assert no_sleep == []
    [event] = _corrupt_events(events)
    assert (event["reason"], event["error"]) == ("unreadable", "OSError")


def test_dd11_transient_permission_error_on_read_recovers(
    path: Path, monkeypatch: pytest.MonkeyPatch, no_sleep: list[float]
) -> None:
    _write_json(path, {"version": 1, "released_controllers": [2]})
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) <= 2:
            raise PermissionError(13, "sharing violation")
        return path.read_bytes().decode("utf-8")

    _patch_read_text(monkeypatch, path, flaky)
    with capture_logs() as events:
        result = ReleaseStateStore(path).load()
    assert result == LoadResult(released={2}, corrupt=False)
    assert len(attempts) == 3
    assert len(no_sleep) == 2
    assert _corrupt_events(events) == []


def test_dd11_path_is_a_directory_is_corrupt(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.mkdir()
    with capture_logs() as events:
        result = ReleaseStateStore(target).load()
    assert result == LoadResult(released=set(), corrupt=True)
    [event] = _corrupt_events(events)
    assert event["reason"] == "unreadable"


# --- Updates on a corrupt file -------------------------------------------------------------------


def test_dd11_remove_on_corrupt_file_writes_valid_file_without_id(path: Path) -> None:
    path.write_bytes(b"{garbage")
    store = ReleaseStateStore(path)
    with capture_logs() as events:
        store.remove(3)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "released_controllers": [],
    }
    assert store.load() == LoadResult(released=set(), corrupt=False)
    [event] = _corrupt_events(events)
    assert event["log_level"] == "error"
    assert (event["operation"], event["controller_id"]) == ("remove", 3)


def test_dd11_add_on_corrupt_file_writes_valid_file_with_id(path: Path) -> None:
    _write_json(path, {"version": 9, "released_controllers": [0, 1]})
    store = ReleaseStateStore(path)
    with capture_logs() as events:
        store.add(1)
    assert store.load() == LoadResult(released={1}, corrupt=False)
    [event] = _corrupt_events(events)
    assert (event["operation"], event["controller_id"], event["reason"]) == (
        "add",
        1,
        "wrong_version",
    )

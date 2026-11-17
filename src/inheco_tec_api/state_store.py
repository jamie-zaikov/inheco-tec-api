"""Persisted ``released`` controller set (design C10, DD-11; FR-12, FR-12.4, FR-12.8).

The file at ``service.state_file`` holds ``{"version": 1, "released_controllers": [0, ...]}``.
:meth:`ReleaseStateStore.add` (release, FR-12.1) and :meth:`ReleaseStateStore.remove` (reacquire,
FR-12.3) are the per-controller writers; :meth:`ReleaseStateStore.replace_all` writes the full set
once after a corrupt load (DD-11). A stop or emergency-off while released never calls them, so the
persisted set is unchanged by FR-12.6 (FR-12.8). On restart the registry calls
:meth:`ReleaseStateStore.load` and keeps every listed controller ``released`` without opening it
(FR-12.4).

Load outcomes:

- missing file → no controller released (``corrupt=False``);
- unreadable file, invalid JSON, wrong ``version`` or wrong value types → ``corrupt=True``. The
  registry then treats **every** configured controller as released (DD-11): no device that VENUS
  may hold is opened, and stop/emergency-off still work through the transient-open path. The event
  is logged at error level with structured fields only; the file contents are never logged.

Writes are atomic: the new contents go to a temp file in the same directory, are flushed to disk,
and replace the target with :func:`os.replace` (atomic on NTFS for a same-volume rename).

Windows: another process (an antivirus scanner, a backup agent, an editor) can hold the file open
without delete sharing, so a read or :func:`os.replace` raises :class:`PermissionError` for a short
time. Both are retried a few times with a short pause. A read that still fails is reported as
``corrupt=True`` (fail safe, DD-11); a write that still fails removes its temp file and raises
:class:`StateStoreError`, so the caller (C9) can refuse the state change instead of silently losing
it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, TypeVar

from inheco_tec_api.logging_setup import get_logger

__all__ = [
    "EVENT_TEC_STATE_FILE_CORRUPT",
    "EVENT_TEC_STATE_FILE_WRITE_FAILED",
    "STATE_FILE_VERSION",
    "LoadResult",
    "ReleaseStateStore",
    "StateStoreError",
]

STATE_FILE_VERSION: Final = 1

EVENT_TEC_STATE_FILE_CORRUPT: Final = "tec_state_file_corrupt"
"""Error event of an unreadable or invalid state file (DD-11). It is not one of the C14 events in
``logging_setup.EVENT_NAMES``; it carries ``state_file``, ``reason`` and, for an OS error,
``error`` (the exception type name) only — never the file contents."""

EVENT_TEC_STATE_FILE_WRITE_FAILED: Final = "tec_state_file_write_failed"
"""Error event of a state-file write that failed after its retries. It carries ``state_file``,
``operation``, ``controller_id`` and ``error`` (the exception type name) only."""

_RETRY_ATTEMPTS: Final = 5
_RETRY_PAUSE_S: Final = 0.05

_log = get_logger(__name__)

_T = TypeVar("_T")


class StateStoreError(Exception):
    """The state file could not be written; the persisted set is unchanged."""


class _CorruptStateFile(Exception):
    """Internal: the file exists but is unreadable or invalid. ``reason`` is a fixed token."""

    def __init__(self, reason: str, error: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error


@dataclass(frozen=True)
class LoadResult:
    """Outcome of :meth:`ReleaseStateStore.load`.

    ``released`` is empty when ``corrupt`` is true; the caller must then treat every configured
    controller as released (DD-11).
    """

    released: set[int] = field(default_factory=set)
    corrupt: bool = False


def _retry_on_permission_error(
    action: Callable[[], _T], sleep: Callable[[float], None] = time.sleep
) -> _T:
    """Run ``action``; retry a transient Windows sharing violation (``PermissionError``)."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return action()
        except PermissionError:
            if attempt == _RETRY_ATTEMPTS:
                raise
            sleep(_RETRY_PAUSE_S * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _is_controller_id(value: Any) -> bool:
    # ``bool`` is a subclass of ``int``; ``true`` in the file is a wrong type, not controller 1.
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse(text: str) -> set[int]:
    """Validate the file contents and return the released set; raise ``_CorruptStateFile``."""
    try:
        data = json.loads(text)
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise _CorruptStateFile("invalid_json") from exc
    if not isinstance(data, dict):
        raise _CorruptStateFile("not_an_object")
    version = data.get("version")
    if isinstance(version, bool) or version != STATE_FILE_VERSION:
        raise _CorruptStateFile("wrong_version")
    released = data.get("released_controllers")
    if not isinstance(released, list):
        raise _CorruptStateFile("wrong_type")
    if not all(_is_controller_id(item) for item in released):
        raise _CorruptStateFile("wrong_type")
    return set(released)


def _check_controller_id(cid: int) -> None:
    if not _is_controller_id(cid):
        raise ValueError("controller id must be a non-negative int")


class ReleaseStateStore:
    """Atomic JSON store of the ``released`` controller IDs at ``path`` (design C10).

    Thread safety: one instance is safe across threads in one process. HTTP handlers run in the
    worker pool and hold only per-controller locks, so a release of controller 0 and of
    controller 1 can update the file at the same time. An instance lock is held across the
    read and the write of :meth:`add` and :meth:`remove` (and across :meth:`load`), so no update
    is lost and a load never observes a half-applied update (FR-12.4).

    The store assumes a single process: there is no cross-process lock. Two processes (or two
    instances) on the same file can still lose an update.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> LoadResult:
        """Read the persisted set: missing → empty; unreadable/invalid → ``corrupt=True``."""
        try:
            with self._lock:
                released = self._read()
        except _CorruptStateFile as exc:
            fields: dict[str, Any] = {"state_file": str(self._path), "reason": exc.reason}
            if exc.error is not None:
                fields["error"] = exc.error
            _log.error(EVENT_TEC_STATE_FILE_CORRUPT, **fields)
            return LoadResult(released=set(), corrupt=True)
        return LoadResult(released=released, corrupt=False)

    def add(self, cid: int) -> None:
        """Persist ``cid`` as released (FR-12.1, FR-12.4). Adding a listed ID writes nothing."""
        _check_controller_id(cid)
        with self._lock:
            current = self._base_for_update(cid, "add")
            if current is not None and cid in current:
                return
            self._write((current or set()) | {cid}, cid, "add")

    def remove(self, cid: int) -> None:
        """Remove ``cid`` from the released set (FR-12.3). Removing an absent ID writes nothing.

        A corrupt file is replaced by a valid file that no longer lists ``cid``; the controllers
        it may have listed are unknown, so the new file lists none (the operator's reacquire is
        the documented way out of DD-11).
        """
        _check_controller_id(cid)
        with self._lock:
            current = self._base_for_update(cid, "remove")
            if current is not None and cid not in current:
                return
            self._write((current or set()) - {cid}, cid, "remove")

    def replace_all(self, cids: Iterable[int]) -> None:
        """Persist exactly ``cids`` as the released set in ONE atomic write (DD-11).

        Used by the registry after a corrupt :meth:`load`: every configured controller is treated
        as released, and this records that before any open, release or reacquire, so a later
        single :meth:`add` or :meth:`remove` does not silently drop the other controllers
        (carry-forward T11-E/T11-V). Always writes; raises :class:`StateStoreError` on failure,
        with the file on disk unchanged.
        """
        released = set(cids)
        for cid in released:
            _check_controller_id(cid)
        with self._lock:
            self._write(released, None, "replace_all")

    # -- internals ---------------------------------------------------------------------------

    def _read(self) -> set[int]:
        """Return the persisted set; ``set()`` for a missing file; raise ``_CorruptStateFile``."""
        try:
            text = _retry_on_permission_error(lambda: self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except UnicodeDecodeError as exc:
            raise _CorruptStateFile("invalid_encoding") from exc
        except OSError as exc:
            raise _CorruptStateFile("unreadable", type(exc).__name__) from exc
        return _parse(text)

    def _base_for_update(self, cid: int, operation: str) -> set[int] | None:
        """Current set for an update, or ``None`` (after an error log) when the file is corrupt."""
        try:
            return self._read()
        except _CorruptStateFile as exc:
            fields: dict[str, Any] = {
                "state_file": str(self._path),
                "reason": exc.reason,
                "operation": operation,
                "controller_id": cid,
            }
            if exc.error is not None:
                fields["error"] = exc.error
            _log.error(EVENT_TEC_STATE_FILE_CORRUPT, **fields)
            return None

    def _write(self, released: Iterable[int], cid: int | None, operation: str) -> None:
        """Write ``released`` atomically: same-directory temp file, fsync, ``os.replace``."""
        payload = json.dumps(
            {"version": STATE_FILE_VERSION, "released_controllers": sorted(released)}
        )
        directory = self._path.parent
        tmp_name: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.", suffix=".tmp", dir=directory
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path = tmp_name
            _retry_on_permission_error(lambda: os.replace(temp_path, self._path))
            tmp_name = None
        except OSError as exc:
            fields: dict[str, Any] = {"state_file": str(self._path), "operation": operation}
            if cid is not None:
                fields["controller_id"] = cid
            _log.error(EVENT_TEC_STATE_FILE_WRITE_FAILED, error=type(exc).__name__, **fields)
            target = f" for controller {cid}" if cid is not None else ""
            raise StateStoreError(f"state file {operation} failed{target}") from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

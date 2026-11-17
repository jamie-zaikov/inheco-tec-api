"""Tests for the C9 controller lock: serialisation, bounded wait, release after errors (Task 12).

Requirements: FR-5.1, FR-12.1 (release waits for the in-flight transaction), NFR-3 / Risk R5
(``ControllerBusy`` after ``lock_wait_s``), NFR-7 (the lock is released after any failure), plus
carry-forward T8-C2 (the transport blocks inside ``read_report`` on a ``threading.Event`` so a
second thread really contends) and T7-C3 (no open/close outside the lock).

Executor deviation covered: ``gate`` takes no lock (the caller holds :meth:`locked` around
``gate`` + ``run``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from inheco_tec_api.controller import (
    ControllerBusy,
    ControllerLost,
    ControllerManager,
    ControllerNotConnected,
)
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.protocol import commands
from inheco_tec_api.transport.fake_actions import DeviceLoss, FirmwareCode, Match
from tec_controller_helpers import Rig, lock_free_elsewhere

JOIN_S = 10


class Worker(threading.Thread):
    """Run ``fn`` in a thread and keep its result or exception."""

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__(daemon=True)
        self.fn = fn
        self.result: object = None
        self.error: BaseException | None = None
        self.started_at = 0.0
        self.finished_at = 0.0

    def run(self) -> None:
        self.started_at = time.monotonic()
        try:
            self.result = self.fn()
        except BaseException as exc:  # noqa: BLE001 — reported to the test thread
            self.error = exc
        finally:
            self.finished_at = time.monotonic()


def hold_lock(manager: ControllerManager) -> tuple[Worker, threading.Event, threading.Event]:
    """Start a thread that holds ``manager.locked()`` until the returned ``done`` event is set."""
    held, done = threading.Event(), threading.Event()

    def body() -> None:
        with manager.locked():
            held.set()
            done.wait(JOIN_S)

    worker = Worker(body)
    worker.start()
    assert held.wait(JOIN_S)
    return worker, held, done


def block_reads(rig: Rig) -> threading.Event:
    """Make every later ``read_report`` wait on the returned event (T8-C2)."""
    gate = threading.Event()
    rig.factory.in_read.clear()
    rig.factory.read_gate = gate
    return gate


# --- Risk R5 / NFR-3: bounded wait ----------------------------------------------------------------


@pytest.mark.parametrize(
    "operation",
    [
        lambda m: m.locked().__enter__(),
        lambda m: m.connect(),
        lambda m: m.release(),
        lambda m: m.reacquire(),
        lambda m: m.run(commands.rtt(1)),
    ],
    ids=["locked", "connect", "release", "reacquire", "run"],
)
def test_r5_controller_busy_after_lock_wait_s_with_held_lock(
    tmp_path: Path, operation: Callable[[ControllerManager], object]
) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    holder, _, done = hold_lock(rig.manager)
    try:
        start = time.monotonic()
        with pytest.raises(ControllerBusy) as info:
            operation(rig.manager)
        elapsed = time.monotonic() - start
    finally:
        done.set()
        holder.join(JOIN_S)
    assert 0.9 <= elapsed < 5.0
    assert info.value.controller_id == rig.manager.cid
    assert rig.manager.state == "connected"
    assert rig.commands == [] and rig.factory.events == []  # nothing happened outside the lock
    assert lock_free_elsewhere(rig.manager)


def test_fr51_second_transaction_contends_during_a_blocked_read_then_busy(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    gate = block_reads(rig)
    first = Worker(lambda: rig.manager.run(commands.stt(1, 370)))
    first.start()
    assert rig.factory.in_read.wait(JOIN_S)  # the first transaction is inside read_report
    second = Worker(lambda: rig.manager.run(commands.stt(2, 370)))
    second.start()
    second.join(JOIN_S)
    gate.set()
    first.join(JOIN_S)
    assert isinstance(second.error, ControllerBusy)
    assert second.finished_at - second.started_at >= 0.9
    assert first.error is None
    assert rig.commands == ["1STT370", "0RDC1"]  # no write of the second command interleaved


def test_fr51_waiting_transaction_runs_after_the_first_completes(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=16).connected()
    gate = block_reads(rig)
    first = Worker(lambda: rig.manager.run(commands.stt(1, 370)))
    first.start()
    assert rig.factory.in_read.wait(JOIN_S)
    second = Worker(lambda: rig.manager.run(commands.stt(2, 380)))
    second.start()
    time.sleep(0.2)
    assert second.is_alive()  # blocked on the lock, not on I/O
    assert rig.commands == ["1STT370"]
    rig.factory.read_gate = None
    gate.set()
    first.join(JOIN_S)
    second.join(JOIN_S)
    assert first.error is None and second.error is None
    assert rig.commands == ["1STT370", "0RDC1", "2STT380", "0RDC1"]


def test_fr121_release_waits_for_the_in_flight_transaction(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=16).connected()
    gate = block_reads(rig)
    first = Worker(lambda: rig.manager.run(commands.stt(1, 370)))
    first.start()
    assert rig.factory.in_read.wait(JOIN_S)
    releaser = Worker(rig.manager.release)
    releaser.start()
    time.sleep(0.2)
    assert releaser.is_alive()
    assert rig.manager.state == "connected"
    assert "close" not in rig.factory.events and rig.handle_open  # no close under the read
    rig.factory.read_gate = None
    gate.set()
    first.join(JOIN_S)
    releaser.join(JOIN_S)
    assert first.error is None and releaser.error is None
    assert releaser.result == "released"
    assert rig.dev.slots[1].target == 370  # the transaction completed before the close
    assert rig.factory.events == ["store_add", "close"]
    assert rig.factory.unlocked_io == []


def test_t7c3_no_open_or_close_outside_the_lock_over_the_lifecycle(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    m = rig.manager
    m.startup(False)
    m.release()
    m.reacquire()
    rig.dev.script(action=DeviceLoss(), times=1)
    with pytest.raises(ControllerLost):
        m.run(commands.rtt(1))
    m.connect()
    assert rig.factory.open_count >= 4 and rig.factory.close_count >= 3
    assert rig.factory.unlocked_io == []


# --- NFR-7: the lock is released after a failure --------------------------------------------------


def test_nfr7_lock_released_after_transaction_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    rig.dev.script(match=Match(mnemonic="STT"), action=FirmwareCode("3"), times=1)
    with pytest.raises(TransactionFailed):
        rig.manager.run(commands.stt(1, 370))
    assert lock_free_elsewhere(rig.manager)
    other = Worker(lambda: rig.manager.run(commands.rtt(1)))
    other.start()
    other.join(JOIN_S)
    assert other.error is None


def test_nfr7_lock_released_after_exception_in_locked_block(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    with pytest.raises(RuntimeError), rig.manager.locked():
        raise RuntimeError("caller failure")
    assert lock_free_elsewhere(rig.manager)


def test_nfr7_lock_released_after_lifecycle_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1)
    rig.dev.present = False
    rig.manager.startup(False)
    with pytest.raises(ControllerNotConnected):
        rig.manager.connect()
    assert lock_free_elsewhere(rig.manager)


def test_locked_is_reentrant_for_gate_then_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    with rig.manager.locked():
        rig.manager.gate("set_target", 1)
        rig.manager.run(commands.stt(1, 370))
    assert rig.commands == ["1STT370", "0RDC1"]
    assert lock_free_elsewhere(rig.manager)


def test_gate_takes_no_lock(tmp_path: Path) -> None:
    rig = Rig(tmp_path, lock_wait_s=1).connected()
    holder, _, done = hold_lock(rig.manager)
    try:
        start = time.monotonic()
        rig.manager.gate("set_target", 1)  # returns at once although another thread holds it
        elapsed = time.monotonic() - start
    finally:
        done.set()
        holder.join(JOIN_S)
    assert elapsed < 0.5

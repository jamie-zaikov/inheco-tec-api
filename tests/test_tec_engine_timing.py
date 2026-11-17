"""Tests for C7 ``engine.py``: serialization, real delays and transaction logging (Task 9).

Requirements: FR-5.1 (lock around ``execute``; DD-1), FR-5.2 (inter-command gap, real 100 ms
delay), FR-5.6 (``A`` retried with a real 1.0 s retry delay), FR-15.1 (log fields). The other
engine tests are in ``test_tec_engine.py``.

Only two tests here wait on the wall clock (the real 100 ms pre-send delay and the real 1.0 s
retry delay). The concurrency tests block inside ``read_report`` with ``threading.Event`` so a
missing lock would interleave deterministically (carry-forward T8-C2); a negative control proves
the scenario interleaves without the lock.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from inheco_tec_api.engine import (
    CommandNotAllowed,
    DeviceLost,
    TransactionEngine,
    TransactionFailed,
)
from inheco_tec_api.logging_setup import EVENT_TEC_TRANSACTION
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.protocol.commands import Command
from inheco_tec_api.transport.fake import FakeTecDevice
from inheco_tec_api.transport.fake_actions import (
    BadCrc,
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
)
from structlog.testing import capture_logs
from tec_engine_helpers import RecordingTransport, VirtualTime, make_engine, settings

RTT1 = Match(mnemonic="RTT", address=1)
FR15_1_FIELDS = {"controller_id", "slot", "mnemonic", "param", "result", "attempts", "duration_ms"}
CLOCK_GRANULARITY_S = 0.002  # tolerance for timer resolution between sleep() and the clock

# --- FR-5.1: two threads sharing one lock never interleave ---------------------------------------


class _InterleaveProbe:
    """Blocks thread A inside its first read until thread B writes, or until ``hold_s`` passes.

    With the lock, B cannot write while A holds it, so A waits the full ``hold_s`` and then
    finishes. Without the lock, B writes during A's read and A resumes at once — the write log
    then shows B's command between A's command and A's resync.
    """

    def __init__(self, transport: RecordingTransport, hold_s: float) -> None:
        self.transport = transport
        self.hold_s = hold_s
        self.a_in_read = threading.Event()
        self.b_wrote = threading.Event()
        self.b_wrote_during_a = False
        self._a_reads = 0
        original_write = transport.write_report
        original_read = transport.read_report

        def write(report: bytes) -> None:
            original_write(report)
            if threading.current_thread().name == "B":
                self.b_wrote.set()

        def read(timeout_ms: int) -> bytes | None:
            if threading.current_thread().name == "A":
                self._a_reads += 1
                if self._a_reads == 1:
                    self.a_in_read.set()
                    self.b_wrote_during_a = self.b_wrote.wait(self.hold_s)
            return original_read(timeout_ms)

        transport.write_report = write  # type: ignore[method-assign]
        transport.read_report = read  # type: ignore[method-assign]


def _run_pair(
    *, use_lock: bool, hold_s: float
) -> tuple[FakeTecDevice, _InterleaveProbe, list[Any]]:
    engine, transport, dev, _ = make_engine(max_attempts=1)
    probe = _InterleaveProbe(transport, hold_s)
    lock = threading.Lock()
    outcomes: list[Any] = []

    def call(cmd: Command) -> None:
        try:
            if use_lock:
                with lock:
                    outcomes.append(engine.execute(cmd))
            else:
                outcomes.append(engine.execute(cmd))
        except Exception as exc:  # the unlocked control may fail; only the write log matters
            outcomes.append(exc)

    thread_a = threading.Thread(target=call, args=(c.rtt(1),), name="A")
    thread_b = threading.Thread(target=call, args=(c.rtt(2),), name="B")
    thread_a.start()
    assert probe.a_in_read.wait(5.0), "thread A never reached its read"
    thread_b.start()
    thread_a.join(10.0)
    thread_b.join(10.0)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    return dev, probe, outcomes


def test_fr5_1_shared_lock_prevents_interleaving() -> None:
    dev, probe, outcomes = _run_pair(use_lock=True, hold_s=0.3)
    assert not probe.b_wrote_during_a
    # From the fake write log: each transaction (command + resync) is contiguous.
    assert dev.commands == ["1RTT", "0RDC1", "2RTT", "0RDC1"]
    assert all(not isinstance(o, Exception) for o in outcomes)
    assert sorted(o.name for o in outcomes) == ["1rtt", "2rtt"]


def test_fr5_1_negative_control_without_lock_interleaves() -> None:
    dev, probe, _ = _run_pair(use_lock=False, hold_s=5.0)
    assert probe.b_wrote_during_a
    assert dev.commands[:2] == ["1RTT", "2RTT"]  # B's write lands inside A's transaction


def test_fr5_1_many_threads_with_lock_keep_transactions_contiguous() -> None:
    engine, transport, dev, _ = make_engine(max_attempts=1)
    original_read = transport.read_report

    def yielding_read(timeout_ms: int) -> bytes | None:
        time.sleep(0.001)  # yield inside the transaction so another thread could run
        return original_read(timeout_ms)

    transport.read_report = yielding_read  # type: ignore[method-assign]
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(cmd: Command) -> None:
        try:
            for _ in range(5):
                with lock:
                    engine.execute(cmd)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    cmds = [c.rtt(1), c.rtt(2), c.rat(1, 3), c.rat(2, 3)]  # the fake has slots 1 and 2
    threads = [threading.Thread(target=worker, args=(cmd,)) for cmd in cmds]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30.0)
    assert errors == []
    commands = dev.commands
    assert len(commands) == 4 * 5 * 2
    for index in range(0, len(commands), 2):
        assert commands[index] in {"1RTT", "2RTT", "1RAT3", "2RAT3"}
        assert commands[index + 1] == "0RDC1"


# --- FR-5.2 / FR-5.6: the two real wall-clock delays ---------------------------------------------


def test_fr5_2_write_gaps_at_least_real_100ms_pre_send_delay() -> None:
    dev = FakeTecDevice(0, clock=time.monotonic)
    dev.open()
    engine = TransactionEngine(
        RecordingTransport(dev), 0, settings(pre_send_delay_ms=100, retry_delay_ms=0)
    )
    engine.execute(c.rtt(1))
    engine.execute(c.rat(1, 3))
    times = [w.monotonic_s for w in dev.writes]
    assert dev.commands == ["1RTT", "0RDC1", "1RAT3", "0RDC1"]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= 0.1 - CLOCK_GRANULARITY_S for gap in gaps), gaps


def test_fr5_6_code_a_retried_with_real_1s_retry_delay() -> None:
    dev = FakeTecDevice(0, clock=time.monotonic)
    dev.open()
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    engine = TransactionEngine(
        RecordingTransport(dev),
        0,
        settings(pre_send_delay_ms=0, retry_delay_ms=1000, max_attempts=2),
    )
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.code, exc.value.attempts) == ("A", 2)
    times = [w.monotonic_s for w in dev.writes]
    assert len(times) == 2
    assert times[1] - times[0] >= 1.0 - CLOCK_GRANULARITY_S


# --- FR-15.1: one tec_transaction event per call with every field --------------------------------


def _transactions(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e["event"] == EVENT_TEC_TRANSACTION]


def test_fr15_1_success_logs_all_fields() -> None:
    vt = VirtualTime()
    engine, _, _, _ = make_engine(
        time_source=vt, controller_id=3, pre_send_delay_ms=200, retry_delay_ms=1000
    )
    with capture_logs() as events:
        engine.execute(c.stt(2, 370))
    [event] = _transactions(events)
    assert FR15_1_FIELDS <= set(event)
    assert event["controller_id"] == 3
    assert event["slot"] == 2
    assert event["mnemonic"] == "STT"
    assert event["param"] == "370"
    assert event["result"] == "ok"
    assert event["attempts"] == 1
    assert event["duration_ms"] == 400  # two pre-send delays on the virtual clock
    assert event["log_level"] == "info"


def test_fr15_1_firmware_failure_logs_code_and_attempts() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3, retry_delay_ms=1000)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    with capture_logs() as events, pytest.raises(TransactionFailed):
        engine.execute(c.rtt(1))
    [event] = _transactions(events)
    assert (event["result"], event["attempts"]) == ("A", 3)
    assert event["duration_ms"] == 2000
    assert event["param"] == ""
    assert event["log_level"] == "warning"


@pytest.mark.parametrize(
    ("action", "result", "attempts"),
    [
        (FirmwareCode("3"), "3", 1),
        (NoReply(), "no_reply", 2),
        (BadCrc(), "crc", 2),
        (FirmwareCode("Z"), "parse", 1),
        (DeviceLoss(), "device_lost", 1),
    ],
)
def test_fr15_1_failure_kinds_logged(action: Any, result: str, attempts: int) -> None:
    engine, _, dev, _ = make_engine(max_attempts=2)
    dev.script(match=RTT1, action=action, times=None)
    with capture_logs() as events, pytest.raises((TransactionFailed, DeviceLost)):
        engine.execute(c.rtt(1))
    [event] = _transactions(events)
    assert FR15_1_FIELDS <= set(event)
    assert (event["result"], event["attempts"]) == (result, attempts)
    assert (event["slot"], event["mnemonic"], event["controller_id"]) == (1, "RTT", 0)


def test_fr15_1_not_allowed_logs_one_event_without_attempts() -> None:
    engine, _, _, _ = make_engine()
    with capture_logs() as events, pytest.raises(CommandNotAllowed):
        engine.execute(Command(1, "XYZ", ""))
    [event] = _transactions(events)
    assert (event["result"], event["attempts"]) == ("not_allowed", 0)


def test_fr15_1_exactly_one_event_per_call_including_resync() -> None:
    engine, _, _, _ = make_engine()
    with capture_logs() as events:
        engine.execute(c.rtt(1))
        engine.execute(c.rdc1())
    transactions = _transactions(events)
    assert [e["mnemonic"] for e in transactions] == ["RTT", "RDC"]


def test_fr5_5_resync_failure_logs_warning_but_command_ok() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=Match(mnemonic="RDC"), action=NoReply())
    with capture_logs() as events:
        engine.execute(c.rtt(1))
    [transaction] = _transactions(events)
    assert transaction["result"] == "ok"
    warnings = [e for e in events if e["event"] != EVENT_TEC_TRANSACTION]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["reason"] == "no_reply"

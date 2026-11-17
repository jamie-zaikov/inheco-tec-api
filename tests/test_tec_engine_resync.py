"""Tests for amendment A2 of C7 ``engine.py``: second flush and checked resync (Task 9.7).

Requirements: FR-5.3 (second discard after the pre-send delay, immediately before the command
write; none before the resync ``0RDC1`` write), FR-5.5 (the resync reads past replies until
``0rdc``; an echo-matching reply with a non-``0`` code fails the transaction with that code; a
no-reply, CRC or frame error, or an exhausted stale budget only logs a warning), FR-4.8 (the resync
reads keep integer-millisecond timeouts in ``1..read_timeout_ms`` within one read timeout) and
FR-15.1 (the ``tec_transaction`` ``result`` is the resync's firmware code).

The ordering and pre-send-delay tests drive the engine over the fake through
:class:`~tec_engine_helpers.RecordingTransport` with an injected ``sleep``. The resync tests drive
it over :class:`~tec_vclock_helpers.VirtualClockTransport`, which schedules the replies of each
written command (the command, then ``0RDC1``) on a virtual clock and refuses any read timeout that
is not an ``int`` in ``1..read_timeout_ms``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from inheco_tec_api.engine import EVENT_TEC_RESYNC_FAILED, TransactionFailed
from inheco_tec_api.logging_setup import EVENT_TEC_TRANSACTION
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS
from inheco_tec_api.transport.fake_actions import BadCrc, Match, NoReply, StaleReply
from structlog.testing import capture_logs
from tec_engine_helpers import VirtualTime, make_engine
from tec_fake_helpers import raw_command_reports, write_reports
from tec_vclock_helpers import Arrival, make_vclock_engine, reply_at

OK = "1ate0"
"""The ``0`` reply echoing ``c.ate(1, True)`` (``1ATE1``)."""

RDC_OK = "0rdc0"
STALE = "2stt0"
"""A non-matching reply that fits in one input report (one read per reply)."""
EPS = 1e-6
RTT1 = Match(mnemonic="RTT", address=1)


class HookedTime(VirtualTime):
    """Virtual time whose ``sleep`` also runs a test hook after it advances the clock."""

    def __init__(self) -> None:
        super().__init__()
        self.hook: Callable[[float], None] | None = None

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        if self.hook is not None:
            self.hook(seconds)


def _transactions(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e["event"] == EVENT_TEC_TRANSACTION]


def _resync_warnings(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [e for e in events if e["event"] == EVENT_TEC_RESYNC_FAILED]


def _replies(*texts: str, step_s: float = 0.01) -> list[Arrival]:
    """Replies ``texts`` arriving one after another, ``step_s`` apart, after the write."""
    arrivals: list[Arrival] = []
    for index, text in enumerate(texts, start=1):
        arrivals += reply_at(index * step_s, text)
    return arrivals


def _assert_resync_reads_bounded(transport: Any, read_timeout_ms: int = 3000) -> None:
    """FR-4.8: the resync's first read gets the full timeout, all reads one timeout in total."""
    resync_timeouts = transport.timeouts_per_exchange()[-1]
    assert all(type(t) is int and 1 <= t <= read_timeout_ms for t in resync_timeouts)
    assert resync_timeouts[0] == read_timeout_ms
    assert transport.read_time_per_exchange()[-1] <= read_timeout_ms / 1000 + EPS


# --- FR-5.3: the second flush --------------------------------------------------------------------


def test_fr5_3_reply_arriving_during_pre_send_delay_is_discarded() -> None:
    vt = HookedTime()
    engine, transport, dev, _ = make_engine(time_source=vt, pre_send_delay_ms=200)
    dev.slots[1].target = 370
    injected: list[int] = []

    def inject(_seconds: float) -> None:
        if not injected:
            # A reply that arrives during the first pre-send delay: it echoes 1rtt, wrong data.
            dev.slots[1].target = 999
            write_reports(dev, raw_command_reports("1RTT"))
            dev.slots[1].target = 370
            injected.append(dev.pending_reports)

    vt.hook = inject
    reply = engine.execute(c.rtt(1))
    assert injected and injected[0] > 0
    assert reply.data == "0370"  # not the injected 1rtt00999
    # The second flush of the attempt removed the injected reply.
    assert transport.flush_results[:2] == [0, injected[0]]
    assert dev.pending_reports == 0


def test_fr5_3_order_per_attempt_is_flush_sleep_flush_write() -> None:
    vt = HookedTime()
    engine, transport, dev, _ = make_engine(time_source=vt, max_attempts=3, pre_send_delay_ms=200)
    dev.script(match=RTT1, action=NoReply(), times=2)
    vt.hook = lambda _seconds: transport.events.append(("sleep", None))
    engine.execute(c.rtt(1))
    names = transport.names()
    command_writes = [i for i, n in enumerate(names) if n == "write:1RTT"]
    assert len(command_writes) == 3
    for index in command_writes:
        assert names[index - 3 : index] == ["flush", "sleep", "flush"]
    # Each attempt has exactly two flushes, both before its write.
    starts = [0, *(i + 1 for i in command_writes[:-1])]
    for start, index in zip(starts, command_writes, strict=True):
        assert names[start:index].count("flush") == 2
    assert names.count("flush") == 6


def test_fr5_3_no_flush_before_resync_write() -> None:
    vt = HookedTime()
    engine, transport, _, _ = make_engine(time_source=vt, pre_send_delay_ms=200)
    vt.hook = lambda _seconds: transport.events.append(("sleep", None))
    engine.execute(c.rtt(1))
    names = transport.names()
    resync = names.index("write:0RDC1")
    command = names.index("write:1RTT")
    between = names[command + 1 : resync]
    assert "flush" not in between
    assert between[-1] == "sleep"  # the pre-send delay applies to the resync write (FR-5.2)
    assert names.count("flush") == 2


def test_fr5_3_late_echo_during_resync_delay_is_checked_not_flushed() -> None:
    """A rejection arriving after the stale ``0`` and before the resync write fails the command."""
    engine, transport, _ = make_vclock_engine()
    transport.plan(reply_at(0.01, OK) + reply_at(0.05, "1ate3"), _replies(RDC_OK))
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code) == ("firmware", "3")
    assert transport.commands == ["1ATE1", "0RDC1"]


# --- FR-5.5: an echo-matching resync reply with a non-0 code fails the transaction -------------


@pytest.mark.parametrize("code", ["3", "A", "1"])
def test_fr5_5_resync_echo_with_error_code_fails_firmware(code: str) -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies(f"1ate{code}", RDC_OK))
    with capture_logs() as events, pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("firmware", code, 1)
    [event] = _transactions(events)
    assert (event["result"], event["attempts"]) == (code, 1)
    assert event["log_level"] == "warning"
    assert _resync_warnings(events) == []
    _assert_resync_reads_bounded(transport)


def test_fr5_5_resync_echo_match_is_case_insensitive() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies("1ATE3", RDC_OK))
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code) == ("firmware", "3")


def test_fr5_5_resync_failure_reports_attempts_made() -> None:
    engine, transport, _ = make_vclock_engine(retry_delay_ms=1000)
    transport.plan(_replies("1ateA"), _replies(OK), _replies("1ate3", RDC_OK))
    with capture_logs() as events, pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("firmware", "3", 2)
    [event] = _transactions(events)
    assert (event["result"], event["attempts"]) == ("3", 2)
    assert transport.commands == ["1ATE1", "1ATE1", "0RDC1"]


def test_fr5_5_resync_reads_past_stale_to_echo_error() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies(STALE, "1ate3", RDC_OK))
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code) == ("firmware", "3")


def test_fr5_5_resync_error_from_other_command_does_not_fail() -> None:
    """Only the command's own echo counts; a rejection of another address/mnemonic is stale."""
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies("2ate3", "1stt3", RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    assert _resync_warnings(events) == []


# --- FR-5.5: read past to 0rdc; no-reply and exhausted budget only warn -------------------------


def test_fr5_5_resync_reads_past_stale_reply_to_rdc() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies(STALE, RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.name == "1ate" and reply.error_char == "0"
    assert _resync_warnings(events) == []
    [event] = _transactions(events)
    assert event["result"] == "ok"
    assert transport.timeouts_per_exchange()[-1] == [3000, 2990]
    _assert_resync_reads_bounded(transport)


def test_fr5_5_rdc_with_any_code_ends_resync_successfully() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies("0rdc3", "1ate3"))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    assert _resync_warnings(events) == []
    assert len(transport.timeouts_per_exchange()[-1]) == 1  # stops at the 0rdc reply


def test_fr5_5_resync_no_reply_warns_and_command_succeeds() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), [])
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert (warning["reason"], warning["log_level"]) == ("no_reply", "warning")
    assert (warning["slot"], warning["mnemonic"]) == (1, "ATE")
    [event] = _transactions(events)
    assert event["result"] == "ok"
    assert transport.timeouts_per_exchange()[-1] == [3000]
    _assert_resync_reads_bounded(transport)


def test_fr5_5_resync_rdc_after_deadline_warns_within_one_timeout() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), reply_at(1.0, STALE) + reply_at(3.5, RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert warning["reason"] == "no_reply"
    assert transport.timeouts_per_exchange()[-1] == [3000, 2000]
    _assert_resync_reads_bounded(transport)


def test_fr4_8_resync_reads_use_remaining_time() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(
        _replies(OK), reply_at(1.0, STALE) + reply_at(2.0, "3stt0") + reply_at(2.5, RDC_OK)
    )
    with capture_logs() as events:
        engine.execute(c.ate(1, True))
    assert _resync_warnings(events) == []
    assert transport.timeouts_per_exchange()[-1] == [3000, 2000, 1000]
    _assert_resync_reads_bounded(transport)


@pytest.mark.parametrize("budget", [1, 3])
def test_fr5_5_resync_stale_budget_exhausted_warns_and_succeeds(budget: int) -> None:
    engine, transport, _ = make_vclock_engine(max_stale_reads=budget)
    stale = [STALE] * (budget + 1)
    transport.plan(_replies(OK), _replies(*stale, RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert warning["reason"] == "no_reply"
    assert len(transport.timeouts_per_exchange()[-1]) == budget + 1  # 0rdc never read
    [event] = _transactions(events)
    assert event["result"] == "ok"
    _assert_resync_reads_bounded(transport)


@pytest.mark.parametrize("budget", [1, 3])
def test_fr5_5_resync_stale_reads_up_to_budget_reach_rdc(budget: int) -> None:
    engine, transport, _ = make_vclock_engine(max_stale_reads=budget)
    transport.plan(_replies(OK), _replies(*([STALE] * budget), RDC_OK))
    with capture_logs() as events:
        engine.execute(c.ate(1, True))
    assert _resync_warnings(events) == []


def test_fr5_5_resync_crc_error_warns_and_succeeds() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=Match(mnemonic="RDC"), action=BadCrc())
    with capture_logs() as events:
        reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert warning["reason"] == "crc"


def test_fr5_5_resync_frame_error_warns_and_succeeds() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=Match(mnemonic="RDC"), action=StaleReply("1rt"))  # too short to parse
    with capture_logs() as events:
        reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert warning["reason"] == "parse"


# --- Executor choices inside the A2 text (decisions.md, 2026-10-08) -----------------------------


def test_fr5_5_resync_echo_with_unknown_code_fails_firmware_with_that_char() -> None:
    assert "Z" not in FIRMWARE_ERRORS
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies("1ateZ", RDC_OK))
    with capture_logs() as events, pytest.raises(TransactionFailed) as exc:
        engine.execute(c.ate(1, True))
    assert (exc.value.kind, exc.value.code) == ("firmware", "Z")
    [event] = _transactions(events)
    assert event["result"] == "Z"


def test_fr5_5_resync_echo_with_code_0_is_read_past() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(_replies(OK), _replies(OK, RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    assert _resync_warnings(events) == []
    assert len(transport.timeouts_per_exchange()[-1]) == 2


def test_fr5_5_resync_echo_with_code_0_counts_against_stale_budget() -> None:
    engine, transport, _ = make_vclock_engine(max_stale_reads=1)
    transport.plan(_replies(OK), _replies(OK, OK, RDC_OK))
    with capture_logs() as events:
        reply = engine.execute(c.ate(1, True))
    assert reply.error_char == "0"
    [warning] = _resync_warnings(events)
    assert warning["reason"] == "no_reply"
    assert len(transport.timeouts_per_exchange()[-1]) == 2

"""Tests for C7 ``engine.py``: attempts, echo match, retry, resync, flush, loss (Task 9).

Requirements: FR-4.8, FR-5, FR-5.2, FR-5.3, FR-5.4, FR-5.5, FR-5.6, FR-5.7, FR-5.8, FR-6.4, FR-9,
FR-9.3, NFR-3, NFR-7. Concurrency (FR-5.1), the two real-delay tests and the FR-15.1 log fields are
in ``test_tec_engine_timing.py`` (split for the 500-line limit).

Every test drives the real engine over the fake transport (Task 8) through a recording wrapper
(``tec_engine_helpers``) with injected virtual time, so no test here waits on the wall clock.
"""

from __future__ import annotations

import pytest
from inheco_tec_api.engine import (
    CommandNotAllowed,
    DeviceLost,
    TransactionEngine,
    TransactionFailed,
)
from inheco_tec_api.manifest import TransportConfig
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.protocol.commands import Command
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS, TRANSIENT_CODES
from inheco_tec_api.transport.fake import FakeTecDevice
from inheco_tec_api.transport.fake_actions import (
    BadCrc,
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    RdcReply,
    StaleReply,
)
from tec_engine_helpers import RecordingTransport, VirtualTime, make_engine, settings
from tec_fake_helpers import raw_command_reports, write_reports

FAIL_FAST_CODES = sorted(set(FIRMWARE_ERRORS) - set(TRANSIENT_CODES) - {"0"})
RTT1 = Match(mnemonic="RTT", address=1)

# --- FR-5 / FR-5.5: happy path and resync -------------------------------------------------------


def test_fr5_success_returns_reply_and_resyncs() -> None:
    engine, transport, dev, _ = make_engine()
    dev.slots[1].target = 370
    reply = engine.execute(c.rtt(1))
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == (
        "1",
        "rtt",
        "0",
        "0370",
    )
    assert dev.commands == ["1RTT", "0RDC1"]
    assert dev.pending_reports == 0  # the resync reply was read and discarded


def test_fr5_5_resync_written_after_every_success() -> None:
    engine, _, dev, _ = make_engine()
    engine.execute(c.stt(2, 370))
    engine.execute(c.ate(2, True))
    assert dev.commands == ["2STT370", "0RDC1", "2ATE1", "0RDC1"]


def test_fr5_5_no_resync_after_rdc() -> None:
    engine, _, dev, _ = make_engine()
    reply = engine.execute(c.rdc1())
    assert reply.name == "0rdc"
    assert dev.commands == ["0RDC1"]


def test_fr5_5_no_resync_after_failure() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=RTT1, action=FirmwareCode("3"))
    with pytest.raises(TransactionFailed):
        engine.execute(c.rtt(1))
    assert dev.commands == ["1RTT"]


def test_fr5_5_resync_no_reply_does_not_fail_command() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=Match(mnemonic="RDC"), action=NoReply())
    reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    assert dev.commands == ["1RTT", "0RDC1"]


def test_fr5_2_pre_send_delay_before_command_and_resync_writes() -> None:
    engine, transport, dev, vt = make_engine(pre_send_delay_ms=250, retry_delay_ms=1000)
    engine.execute(c.rtt(1))
    assert vt.sleeps == [0.25, 0.25]
    # Writes are recorded on the virtual clock: the resync write follows the command write by the
    # full pre-send delay.
    gap = dev.writes[1].monotonic_s - dev.writes[0].monotonic_s
    assert gap >= 0.25


# --- FR-10.1 via C7 step 1: encode before I/O ----------------------------------------------------


def test_fr5_command_not_allowed_raises_before_any_io() -> None:
    engine, transport, dev, vt = make_engine()
    with pytest.raises(CommandNotAllowed):
        engine.execute(Command(1, "XYZ", ""))
    assert transport.events == []
    assert vt.sleeps == []
    assert dev.writes == []


def test_fr5_6_max_attempts_below_one_is_rejected() -> None:
    engine, transport, _, _ = make_engine()
    with pytest.raises(ValueError):
        engine.execute(c.rtt(1), max_attempts=0)
    assert transport.events == []


# --- FR-5.4: echo match, stale and 0rdc replies --------------------------------------------------


def test_fr5_4_rdc_stale_reply_read_past_without_resend() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1].target = 120
    dev.script(match=RTT1, action=RdcReply())
    reply = engine.execute(c.rtt(1))
    assert reply.data == "0120"
    assert dev.commands == ["1RTT", "0RDC1"]  # no resend of 1RTT


def test_fr5_4_non_matching_echo_discarded_without_resend() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=RTT1, action=StaleReply("2rtt00999"))
    reply = engine.execute(c.rtt(1))
    assert reply.name == "1rtt"
    assert reply.data == "0000"
    assert dev.commands == ["1RTT", "0RDC1"]


def test_fr5_4_echo_match_is_case_insensitive() -> None:
    engine, transport, dev, _ = make_engine()
    dev.script(match=RTT1, action=NoReply())
    transport.inject_after_next_command("1RTT00042")
    reply = engine.execute(c.rtt(1))
    assert reply.name == "1RTT"
    assert reply.data == "0042"
    assert dev.commands == ["1RTT", "0RDC1"]


def test_fr5_4_stale_reads_up_to_limit_are_read_past() -> None:
    engine, transport, dev, _ = make_engine(max_stale_reads=3)
    transport.inject_after_next_command("0rdc0", "2rat00100", "0rdc0")
    reply = engine.execute(c.rtt(1))
    assert reply.name == "1rtt"
    assert dev.commands == ["1RTT", "0RDC1"]


def test_fr5_4_stale_budget_exhausted_is_no_reply_and_retried() -> None:
    engine, transport, dev, _ = make_engine(max_stale_reads=2, max_attempts=2)
    transport.inject_after_next_command("0rdc0", "0rdc0", "0rdc0")
    reply = engine.execute(c.rtt(1))  # attempt 1 fails no_reply; attempt 2 succeeds
    assert reply.name == "1rtt"
    assert dev.commands == ["1RTT", "1RTT", "0RDC1"]


def test_fr5_4_stale_budget_exhausted_on_every_attempt_fails_no_reply() -> None:
    engine, transport, dev, _ = make_engine(max_stale_reads=1, max_attempts=1)
    transport.inject_after_next_command("0rdc0", "0rdc0")
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("no_reply", None, 1)


def test_fr5_4_multi_report_reply_assembled() -> None:
    engine, _, dev, _ = make_engine()
    dev.mainboard.firmware[4] = "(c) A LONG COPYRIGHT STRING FOR THE TEST"
    reply = engine.execute(c.rfv(4))
    assert reply.data == "(c) A LONG COPYRIGHT STRING FOR THE TEST"


# --- FR-5.6 / FR-5.7 / FR-5.8: classification and retries ----------------------------------------


def test_fr5_7_code_3_attempted_once() -> None:
    engine, _, dev, vt = make_engine(retry_delay_ms=1000)
    dev.script(match=RTT1, action=FirmwareCode("3"), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("firmware", "3", 1)
    assert dev.commands == ["1RTT"]
    assert 1.0 not in vt.sleeps  # no retry delay


@pytest.mark.parametrize("code", FAIL_FAST_CODES)
def test_fr5_7_every_non_transient_code_fails_fast(code: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=RTT1, action=FirmwareCode(code), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("firmware", code, 1)
    assert dev.commands == ["1RTT"]


@pytest.mark.parametrize("code", sorted(TRANSIENT_CODES))
def test_fr5_6_transient_code_retried_to_limit(code: str) -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode(code), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("firmware", code, 3)
    assert dev.commands == ["1RTT"] * 3


def test_fr5_6_code_a_retried_with_retry_delay_spacing() -> None:
    engine, _, dev, vt = make_engine(pre_send_delay_ms=200, retry_delay_ms=1000, max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.attempts == 3
    # pre-send, then retry delay + pre-send before each resend; no delay after the last attempt.
    assert vt.sleeps == [0.2, 1.0, 0.2, 1.0, 0.2]
    times = [w.monotonic_s for w in dev.writes]
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:], strict=False))


def test_fr5_6_code_a_then_success_returns_reply() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=2)
    reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    assert dev.commands == ["1RTT", "1RTT", "1RTT", "0RDC1"]


def test_fr5_6_max_attempts_override_per_call() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1), max_attempts=11)
    assert exc.value.attempts == 11
    assert dev.commands == ["1RTT"] * 11


def test_fr5_6_max_attempts_one_means_no_retry() -> None:
    engine, _, dev, _ = make_engine(max_attempts=1)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.attempts == 1
    assert dev.commands == ["1RTT"]


def test_fr4_8_no_reply_retried_then_fails_no_reply() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=NoReply(), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("no_reply", None, 3)
    assert dev.commands == ["1RTT"] * 3


def test_fr5_6_bad_crc_retried_then_fails_crc() -> None:
    engine, _, dev, _ = make_engine(max_attempts=2)
    dev.script(match=RTT1, action=BadCrc(), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("crc", None, 2)
    assert dev.commands == ["1RTT", "1RTT"]


def test_fr5_8_last_failure_kind_is_reported() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode("A"), times=1)
    dev.script(match=RTT1, action=BadCrc(), times=1)
    dev.script(match=RTT1, action=NoReply(), times=1)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("no_reply", None, 3)


def test_fr9_3_unknown_error_char_fails_parse_without_retry() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=FirmwareCode("Z"), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.code, exc.value.attempts) == ("parse", None, 1)
    assert dev.commands == ["1RTT"]


def test_fr9_3_frame_error_fails_parse_without_retry() -> None:
    engine, _, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=StaleReply("1rt"), times=None)  # too short to parse
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert (exc.value.kind, exc.value.attempts) == ("parse", 1)
    assert dev.commands == ["1RTT"]


def test_fr9_no_failure_is_returned_as_success() -> None:
    engine, _, dev, _ = make_engine(max_attempts=1)
    for code in [*FAIL_FAST_CODES, *sorted(TRANSIENT_CODES), "Z"]:
        dev.clear_script()
        dev.script(match=RTT1, action=FirmwareCode(code))
        with pytest.raises(TransactionFailed):
            engine.execute(c.rtt(1))


# --- FR-5.3 / NFR-7: flush -----------------------------------------------------------------------


def test_fr5_3_flush_before_each_attempt() -> None:
    engine, transport, dev, _ = make_engine(max_attempts=3)
    dev.script(match=RTT1, action=NoReply(), times=2)
    engine.execute(c.rtt(1))
    names = transport.names()
    command_writes = [i for i, n in enumerate(names) if n == "write:1RTT"]
    assert len(command_writes) == 3
    for index in command_writes:
        # The last flush before every command write comes after the previous attempt's reads.
        before = names[:index]
        last_flush = max(i for i, n in enumerate(before) if n == "flush")
        assert all(not n.startswith("write") for n in before[last_flush + 1 :])
    assert names.count("flush") == 6  # two flushes per attempt (FR-5.3, amendment A2)


def test_nfr7_next_transaction_after_failure_starts_flushed() -> None:
    engine, transport, dev, _ = make_engine()
    dev.script(match=RTT1, action=FirmwareCode("3"))
    with pytest.raises(TransactionFailed):
        engine.execute(c.rtt(1))
    # A late reply arrives after the failed transaction: it echoes 1rtt with the old target.
    write_reports(dev, raw_command_reports("1RTT"))
    assert dev.pending_reports > 0
    dev.slots[1].target = 370
    reply = engine.execute(c.rtt(1))
    assert reply.data == "0370"  # the stale 1rtt0000 reply was flushed, not returned
    # The FIRST flush of the attempt removed the late reply (the second one finds nothing).
    assert transport.flush_results[-2] > 0


def test_nfr7_engine_usable_after_device_loss_reopen() -> None:
    engine, transport, dev, _ = make_engine()
    dev.script(match=RTT1, action=DeviceLoss())
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(1))
    dev.close()  # the controller (C9) owns close/reopen, never the engine
    dev.open()
    assert engine.execute(c.rtt(1)).error_char == "0"


# --- FR-6.4: DeviceLoss -> DeviceLost ------------------------------------------------------------


def test_fr6_4_device_loss_on_write_raises_device_lost_without_retry() -> None:
    engine, transport, dev, vt = make_engine(max_attempts=3, retry_delay_ms=1000)
    dev.script(match=RTT1, action=DeviceLoss())
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(1))
    assert dev.writes == []  # the failed write is not recorded and never resent
    assert 1.0 not in vt.sleeps
    assert transport.close_count == 0
    assert dev.is_open


def test_fr6_4_device_loss_on_resync_raises_device_lost() -> None:
    engine, transport, dev, _ = make_engine()
    dev.script(match=Match(mnemonic="RDC"), action=DeviceLoss())
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(1))
    assert dev.commands == ["1RTT"]
    assert transport.close_count == 0


def test_fr6_4_dead_handle_on_flush_raises_device_lost() -> None:
    engine, transport, dev, _ = make_engine()
    dev.script(match=RTT1, action=DeviceLoss())
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(1))
    events_before = list(transport.events)
    with pytest.raises(DeviceLost):  # the handle stays dead: the next flush fails at once
        engine.execute(c.rtt(2))
    assert transport.events == events_before  # nothing was written or read
    assert dev.writes == []
    assert transport.close_count == 0


def test_fr6_4_closed_handle_raises_device_lost() -> None:
    engine, transport, dev, _ = make_engine()
    dev.close()
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(1))


# --- T8-C1 / T8 None rule: read timeouts and no busy loop ----------------------------------------


def test_fr4_8_none_read_ends_attempt_without_busy_loop() -> None:
    frozen = VirtualTime()
    frozen.sleep = lambda seconds: None  # type: ignore[method-assign]  # clock never advances
    engine, transport, dev, _ = make_engine(time_source=frozen, max_attempts=3, max_stale_reads=3)
    transport.max_reads = 3 * (3 + 1)  # guard: a spin on the clock would trip this
    dev.script(match=RTT1, action=NoReply(), times=None)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert transport.read_count == 3  # exactly one None read per attempt


def test_fr4_8_reads_per_attempt_bounded_by_stale_budget() -> None:
    engine, transport, dev, _ = make_engine(max_attempts=1, max_stale_reads=2)
    transport.inject_after_next_command(*["0rdc0"] * 6)
    with pytest.raises(TransactionFailed):
        engine.execute(c.rtt(1))
    assert transport.read_count <= 2 + 1


def test_fr4_8_read_deadline_on_clock_ends_stale_reads() -> None:
    vt = VirtualTime()
    engine, transport, dev, _ = make_engine(time_source=vt, max_attempts=1, max_stale_reads=10)
    original = transport.read_report
    spent: list[float] = []

    def slow_read(timeout_ms: int) -> bytes | None:
        cost = min(1.0, timeout_ms / 1000)  # a stale report arrives 1 s in, inside the timeout
        vt.now += cost
        spent.append(cost)
        return original(timeout_ms)

    transport.read_report = slow_read  # type: ignore[method-assign]
    transport.inject_after_next_command(*["0rdc0"] * 9)
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert transport.read_timeouts == [3000, 2000, 1000]  # the time left, not the full timeout
    assert sum(spent) <= 3000 / 1000 + 1e-6  # summed read time within read_timeout_ms


# --- engine never closes the transport -----------------------------------------------------------


def test_engine_never_closes_transport() -> None:
    engine, transport, dev, _ = make_engine(max_attempts=2)
    engine.execute(c.rtt(1))
    dev.script(match=RTT1, action=FirmwareCode("A"), times=None)
    with pytest.raises(TransactionFailed):
        engine.execute(c.rtt(1))
    with pytest.raises(CommandNotAllowed):
        engine.execute(Command(1, "XYZ", ""))
    dev.script(match=Match(), action=DeviceLoss())
    with pytest.raises(DeviceLost):
        engine.execute(c.rtt(2))
    assert transport.close_count == 0
    assert "close" not in transport.names()


# --- NFR-3: worst-case budget --------------------------------------------------------------------


def test_nfr3_budget_is_15_8_s_with_defaults() -> None:
    engine = TransactionEngine(RecordingTransport(FakeTecDevice()), 0, TransportConfig())
    assert engine.worst_case_budget_s() == pytest.approx(15.8)


def test_nfr3_budget_follows_settings_and_override() -> None:
    engine = TransactionEngine(
        RecordingTransport(FakeTecDevice()),
        0,
        settings(pre_send_delay_ms=100, read_timeout_ms=1000, retry_delay_ms=1000, max_attempts=2),
    )
    assert engine.worst_case_budget_s() == pytest.approx(2 * 2.1 + 1.1)
    assert engine.worst_case_budget_s(max_attempts=11) == pytest.approx(11 * 2.1 + 1.1)


def test_controller_id_property() -> None:
    engine, _, _, _ = make_engine(controller_id=5)
    assert engine.controller_id == 5

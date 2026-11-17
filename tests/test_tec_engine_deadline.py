"""Per-attempt read deadline of C7 ``engine.py`` on hidapi-like timing (Task 9; FR-4.8, NFR-3).

Every test but one (the timeout-type check on the plain fake) drives the real engine over
:class:`~tec_vclock_helpers.VirtualClockTransport`: a read blocks until the next scheduled input
report arrives or consumes its whole timeout, on a shared virtual clock. This exposes what the
non-blocking fake cannot: all reads of one attempt share ONE read timeout (FR-4.8, design C7
step 2.4), the resync has its own one, and the total duration of a call stays within
``worst_case_budget_s()`` (NFR-3). The transport refuses any read timeout that is not an ``int``
in ``1..read_timeout_ms`` (carry-forward T8-C1, corrected).
"""

from __future__ import annotations

import pytest
from inheco_tec_api.engine import EVENT_TEC_RESYNC_FAILED, TransactionFailed
from inheco_tec_api.logging_setup import EVENT_TEC_TRANSACTION
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.transport.fake_actions import Match, NoReply, StaleReply
from structlog.testing import capture_logs
from tec_engine_helpers import make_engine
from tec_vclock_helpers import make_vclock_engine, partial_at, reply_at

MATCH = "1rtt025"
"""A ``0`` reply echoing ``c.rtt(1)`` that fits in one input report."""

LONG_MATCH = "1rtt0" + "0250" * 4
"""A ``0`` reply echoing ``c.rtt(1)`` that spans several input reports."""

LONG_RDC = "0rdc0" + "X" * 12
"""A resync reply that spans several input reports (for a partial resync reply)."""

STALE = "0rdc0"
EPS = 1e-6


# --- FR-4.8: a reply after the attempt's one read timeout is no reply ----------------------------


def test_fr4_8_matching_reply_after_deadline_is_no_reply() -> None:
    engine, transport, vt = make_vclock_engine(max_attempts=1)
    transport.plan(reply_at(5.8, MATCH))
    start = vt.now
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert transport.read_timeouts == [3000]
    assert vt.now - start == pytest.approx(0.2 + 3.0)


def test_fr4_8_stale_just_before_deadline_then_late_match_is_no_reply() -> None:
    """The validator's probe: stale ``0rdc0`` at 2.99 s, then a match at 5.8 s."""
    engine, transport, _ = make_vclock_engine(max_attempts=1)
    transport.plan(reply_at(2.99, STALE) + reply_at(5.8, MATCH))
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert transport.read_timeouts == [3000, 10]
    [attempt] = transport.read_time_per_exchange()
    assert attempt <= 3.0 + EPS


def test_fr4_8_reply_inside_window_succeeds() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(reply_at(2.5, MATCH), reply_at(0.05, STALE))
    reply = engine.execute(c.rtt(1))
    assert (reply.name, reply.error_char, reply.data) == ("1rtt", "0", "25")
    assert transport.commands == ["1RTT", "0RDC1"]
    assert transport.timeouts_per_exchange() == [[3000], [3000]]


def test_fr4_8_stale_then_match_inside_window_uses_remaining_time() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(reply_at(1.0, STALE) + reply_at(2.0, MATCH), reply_at(0.05, STALE))
    reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    [attempt, _resync] = transport.timeouts_per_exchange()
    assert attempt == [3000, 2000]


def test_fr4_8_partial_reply_straddling_deadline_is_no_reply() -> None:
    engine, transport, _ = make_vclock_engine(max_attempts=1)
    transport.plan(partial_at(LONG_MATCH, 2.0, rest_s=3.5))
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert transport.read_timeouts == [3000, 1000]
    [attempt] = transport.read_time_per_exchange()
    assert attempt <= 3.0 + EPS


def test_fr4_8_multi_report_reply_inside_window_succeeds() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(partial_at(LONG_MATCH, 1.0, rest_s=2.5), reply_at(0.05, STALE))
    reply = engine.execute(c.rtt(1))
    assert reply.data == "0250" * 4
    [attempt, _resync] = transport.timeouts_per_exchange()
    assert attempt[0] == 3000
    assert all(1 <= value <= 3000 for value in attempt)


def test_fr4_8_every_read_timeout_is_int_within_bounds_and_first_is_configured() -> None:
    engine, transport, _ = make_vclock_engine(read_timeout_ms=2500)
    transport.plan(
        reply_at(0.5, STALE) + reply_at(1.4, STALE) + reply_at(2.499, STALE),
        reply_at(2.2, STALE) + partial_at(LONG_MATCH, 2.3, rest_s=2.45),
        reply_at(0.1, STALE),
    )
    reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    exchanges = transport.timeouts_per_exchange()
    assert len(exchanges) == 3  # one failed attempt, one good attempt, the resync
    for reads in exchanges:
        assert reads[0] == 2500
        for value in reads:
            assert type(value) is int
            assert 1 <= value <= 2500
    for total in transport.read_time_per_exchange():
        assert total <= 2.5 + EPS


def test_fr4_8_every_read_uses_configured_timeout_as_int_ms() -> None:
    """On the non-blocking fake (frozen between reads) every read gets an int in 1..configured."""
    rtt1 = Match(mnemonic="RTT", address=1)
    engine, transport, dev, _ = make_engine(read_timeout_ms=2500, max_attempts=3)
    dev.script(match=rtt1, action=NoReply(), times=1)
    dev.script(match=rtt1, action=StaleReply("0rdc0"), times=1)
    dev.mainboard.firmware[4] = "(c) A LONG COPYRIGHT STRING FOR THE TEST"
    engine.execute(c.rtt(1))
    engine.execute(c.rtt(1))
    engine.execute(c.rfv(4))  # multi-report reply: several reads in one assembly
    assert len(transport.read_timeouts) >= 8
    for value in transport.read_timeouts:
        assert type(value) is int
        assert 1 <= value <= 2500
    # The first read after each write (every attempt and every resync) gets the full timeout.
    timeouts = iter(transport.read_timeouts)
    firsts: list[int] = []
    after_write = False
    for name, _ in transport.events:
        if name == "write":
            after_write = True
        elif name == "read":
            value = next(timeouts)
            if after_write:
                firsts.append(value)
            after_write = False
    assert len(firsts) == 7  # (2 attempts + resync) + (1 + resync) + (1 + resync)
    assert firsts == [2500] * len(firsts)


# --- FR-5.5 / NFR-3: the resync has its own deadline; the call stays within budget --------------


def test_fr5_5_resync_partial_reply_bounded_by_own_read_timeout() -> None:
    engine, transport, _ = make_vclock_engine()
    transport.plan(reply_at(0.5, MATCH), partial_at(LONG_RDC, 1.0))
    with capture_logs() as events:
        reply = engine.execute(c.rtt(1))
    assert reply.error_char == "0"
    [_attempt, resync] = transport.timeouts_per_exchange()
    assert resync == [3000, 2000]
    assert transport.read_time_per_exchange()[1] <= 3.0 + EPS
    assert [e["reason"] for e in events if e["event"] == EVENT_TEC_RESYNC_FAILED] == ["no_reply"]


def test_nfr3_stale_before_deadline_every_attempt_stays_within_budget() -> None:
    engine, transport, vt = make_vclock_engine()
    transport.plan(*[reply_at(2.99, STALE) for _ in range(3)])
    start = vt.now
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    duration = vt.now - start
    assert exc.value.kind == "no_reply"
    assert exc.value.attempts == 3
    assert duration <= engine.worst_case_budget_s() + EPS
    assert duration == pytest.approx(3 * (0.2 + 3.0) + 2 * 1.0)
    assert transport.timeouts_per_exchange() == [[3000, 10]] * 3
    assert all(total <= 3.0 + EPS for total in transport.read_time_per_exchange())


def test_nfr3_worst_mix_with_partial_resync_succeeds_within_budget() -> None:
    engine, transport, vt = make_vclock_engine()
    transport.plan(
        reply_at(2.99, STALE),
        reply_at(2.99, STALE),
        reply_at(2.99, STALE) + reply_at(2.999, MATCH),
        partial_at(LONG_RDC, 1.0),
    )
    start = vt.now
    with capture_logs() as events:
        reply = engine.execute(c.rtt(1))
    duration = vt.now - start
    assert reply.error_char == "0"
    assert engine.worst_case_budget_s() == pytest.approx(15.8)
    assert duration <= 15.8 + EPS
    assert transport.commands == ["1RTT"] * 3 + ["0RDC1"]
    assert all(total <= 3.0 + EPS for total in transport.read_time_per_exchange())
    [transaction] = [e for e in events if e["event"] == EVENT_TEC_TRANSACTION]
    assert (transaction["result"], transaction["attempts"]) == ("ok", 3)
    warnings = [e for e in events if e["event"] == EVENT_TEC_RESYNC_FAILED]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["reason"] == "no_reply"


def test_nfr3_silence_everywhere_stays_within_budget() -> None:
    engine, transport, vt = make_vclock_engine()
    start = vt.now
    with pytest.raises(TransactionFailed) as exc:
        engine.execute(c.rtt(1))
    assert exc.value.kind == "no_reply"
    assert vt.now - start <= engine.worst_case_budget_s() + EPS
    assert transport.read_timeouts == [3000] * 3

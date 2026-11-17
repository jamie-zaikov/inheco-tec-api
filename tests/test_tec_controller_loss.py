"""Tests for C9 loss handling and reconnect after loss (Task 12; sequence S4).

Requirements: FR-6.4, FR-11, FR-11.3, FR-11.4, FR-15.2, FR-7.5, plus carry-forward T9-S2 (the
loss-stop ``ATE0`` is single-attempt), T9-C1 / T10-C (after ``DeviceLost`` the identifications are
discarded and the gate reports not identified; a loss mid-identification discards that call's
reads), T7-C2 (the best-effort reopen may fail in any way) and T11-C2 (loss paths never call the
store).

Executor deviations covered: a loss during startup gives ``lost`` WITHOUT the best-effort ``ATE0``
(FR-6.7 forbids a slot change at startup); a loss during the first connect from ``disconnected``
gives ``lost`` WITH the best-effort ``ATE0``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from inheco_tec_api.controller import (
    ConnectResult,
    ControllerLost,
    ControllerNotConnected,
    ReconnectStopFailed,
    SlotNotPermitted,
    SlotStopOutcome,
)
from inheco_tec_api.controller_startup import REASON_NOT_IDENTIFIED
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.protocol import commands
from inheco_tec_api.transport.fake_actions import (
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
)
from structlog.testing import capture_logs
from tec_controller_helpers import IDENTIFY_1, IDENTIFY_2, STARTUP_1, STOP_ALL, Rig, events_named


def lose_on_next_command(rig: Rig, match: Match | None = None) -> None:
    rig.dev.script(match=match, action=DeviceLoss(), times=1)


def lost_rig(tmp_path: Path) -> Rig:
    """A connected rig that has just lost its device on a command (writes forgotten)."""
    rig = Rig(tmp_path).connected()
    lose_on_next_command(rig)
    with pytest.raises(ControllerLost):
        rig.manager.run(commands.stt(1, 370))
    assert rig.manager.state == "lost"
    rig.mark()
    return rig


# --- FR-6.4 / FR-11.3: loss during a command ------------------------------------------------------


def test_fr64_fr113_loss_closes_reopens_stops_every_slot_and_goes_lost(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    lose_on_next_command(rig)
    with capture_logs() as events, pytest.raises(ControllerLost) as info:
        rig.manager.run(commands.stt(1, 370))
    assert isinstance(info.value, ControllerNotConnected)
    assert info.value.state == "lost"
    assert rig.manager.state == "lost"
    # close the dead handle -> one reopen -> ATE0 per slot -> close
    assert rig.factory.events == ["close", "create", "open", "close"]
    assert rig.commands == STOP_ALL  # the lost STT was never recorded by the fake
    assert not rig.handle_open
    assert rig.factory.unlocked_io == []
    stops = events_named(events, "tec_slot_stop")
    assert [(e["slot"], e["purpose"], e["outcome"]) for e in stops] == [
        (1, "loss_stop", "stopped"),
        (2, "loss_stop", "stopped"),
    ]
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("connected", "lost", "loss")
    ]
    assert rig.store.calls == []


def test_t9s2_loss_stop_ate0_is_single_attempt(tmp_path: Path) -> None:
    rig = Rig(tmp_path, max_attempts=3).connected()
    lose_on_next_command(rig)
    rig.dev.script(match=Match(mnemonic="ATE", address=1), action=NoReply(), times=None)
    with capture_logs() as events, pytest.raises(ControllerLost):
        rig.manager.run(commands.rtt(1))
    assert rig.commands.count("1ATE0") == 1  # configured max_attempts is 3; the stop uses 1
    assert "2ATE0" in rig.commands  # the other slot is still attempted
    outcome = {e["slot"]: e for e in events_named(events, "tec_slot_stop")}
    assert (outcome[1]["outcome"], outcome[1]["failure"]) == ("failed", "no_reply")
    assert outcome[2]["outcome"] == "stopped"
    assert rig.manager.state == "lost"


def test_fr113_best_effort_reopen_failure_still_goes_lost(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    lose_on_next_command(rig)
    rig.dev.script(action=OpenFailure(), times=1)
    with capture_logs() as events, pytest.raises(ControllerLost):
        rig.manager.run(commands.rtt(1))
    assert rig.manager.state == "lost"
    assert rig.commands == []
    stops = events_named(events, "tec_slot_stop")
    assert [(e["slot"], e["outcome"], e["failure"]) for e in stops] == [
        (1, "not_attempted", "open_failed"),
        (2, "not_attempted", "open_failed"),
    ]


@pytest.mark.parametrize("error", [ImportError("x"), OSError("y"), RuntimeError("z")])
def test_t7c2_best_effort_reopen_raw_error_never_crashes(tmp_path: Path, error: Exception) -> None:
    rig = Rig(tmp_path).connected()
    lose_on_next_command(rig)
    rig.factory.create_error = error
    with pytest.raises(ControllerLost):
        rig.manager.run(commands.rtt(1))
    assert rig.manager.state == "lost"
    assert not rig.handle_open


def test_fr64_t9c1_loss_discards_identification_and_gate_refuses(tmp_path: Path) -> None:
    rig = lost_rig(tmp_path)
    assert all(s.identification is None for s in rig.manager.slots.values())
    assert all(s.not_permitted_reason == REASON_NOT_IDENTIFIED for s in rig.manager.slots.values())
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.gate("read", 1)
    assert info.value.state == "lost"
    with pytest.raises(ControllerNotConnected):
        rig.manager.run(commands.rtt(1))
    assert rig.commands == []  # refused without HID I/O


def test_nfr7_transaction_failure_keeps_connected_and_identification(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.script(match=Match(mnemonic="STT"), action=FirmwareCode("3"), times=1)
    with pytest.raises(TransactionFailed):
        rig.manager.run(commands.stt(1, 370))
    assert rig.manager.state == "connected"
    assert rig.manager.slots[1].permitted is True
    assert rig.manager.run(commands.rtt(1)).error_char == "0"


# --- loss during startup / first connect (executor deviations) -----------------------------------


def test_startup_loss_goes_lost_without_any_ate0_and_discards_reads(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    lose_on_next_command(rig, Match(mnemonic="RAN", address=0, param="2"))
    rig.manager.startup(False)
    assert rig.manager.state == "lost"
    assert all(cmd[1:4] != "ATE" for cmd in rig.commands)  # FR-6.7 wins over FR-11.3
    assert rig.commands == IDENTIFY_1 + STARTUP_1 + ["0RTD2", "0RDC1"]
    # T10-C: slot 1 was fully read before the loss, but the whole call's reads are discarded.
    assert all(s.identification is None for s in rig.manager.slots.values())
    assert all(s.startup_state is None for s in rig.manager.slots.values())
    assert not rig.handle_open
    assert rig.store.calls == []


def test_connect_from_disconnected_loss_goes_lost_with_best_effort_stop(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.dev.present = False
    rig.manager.startup(False)
    rig.dev.present = True
    lose_on_next_command(rig, Match(mnemonic="RLT", address=1))
    with pytest.raises(ControllerLost):
        rig.manager.connect()
    assert rig.manager.state == "lost"
    assert rig.commands[-4:] == STOP_ALL
    assert all(s.identification is None for s in rig.manager.slots.values())
    assert not rig.handle_open


# --- FR-11.4: reconnect after loss ----------------------------------------------------------------


def test_fr114_reconnect_stops_every_slot_before_identification_and_any_command(
    tmp_path: Path,
) -> None:
    rig = lost_rig(tmp_path)
    rig.dev.slots[1].enabled = True  # e.g. VENUS turned it on while lost
    with capture_logs() as events:
        result = rig.manager.connect()
    assert result == ConnectResult("connected", stopped_slots=[1, 2])
    assert rig.manager.state == "connected"
    assert rig.commands == STOP_ALL + IDENTIFY_1 + IDENTIFY_2
    assert rig.dev.slots[1].enabled is False
    assert all(s.permitted for s in rig.manager.slots.values())
    rig.manager.run(commands.stt(1, 370))
    assert rig.commands.index("1STT370") > rig.commands.index("2ATE0")
    stops = events_named(events, "tec_slot_stop")
    assert [(e["slot"], e["purpose"]) for e in stops] == [
        (1, "reconnect_stop"),
        (2, "reconnect_stop"),
    ]
    changes = events_named(events, "tec_state_change")
    assert [(e["from_state"], e["to_state"], e["reason"]) for e in changes] == [
        ("lost", "connected", "connect")
    ]
    assert rig.store.calls == []


def test_fr114_reconnect_stop_failure_closes_stays_lost_with_per_slot_outcome(
    tmp_path: Path,
) -> None:
    rig = lost_rig(tmp_path)
    rig.dev.script(match=Match(mnemonic="ATE", address=2), action=FirmwareCode("3"), times=1)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.outcomes == [
        SlotStopOutcome(1, "stopped"),
        SlotStopOutcome(2, "failed", "3"),
    ]
    assert rig.manager.state == "lost"
    assert not rig.handle_open
    assert all(cmd[1:4] != "RTD" for cmd in rig.commands)  # no identification after the failure
    with pytest.raises(ControllerNotConnected):
        rig.manager.gate("read", 1)


def test_fr114_reconnect_loss_during_stop_reports_device_lost_outcome(tmp_path: Path) -> None:
    rig = lost_rig(tmp_path)
    lose_on_next_command(rig, Match(mnemonic="ATE", address=1))
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.outcomes == [
        SlotStopOutcome(1, "failed", "device_lost"),
        SlotStopOutcome(2, "not_attempted"),
    ]
    assert rig.manager.state == "lost"
    assert not rig.handle_open


def test_fr114_reconnect_open_failure_stays_lost(tmp_path: Path) -> None:
    rig = lost_rig(tmp_path)
    rig.dev.present = False
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.connect()
    assert info.value.state == "lost"
    assert rig.manager.state == "lost"
    assert rig.commands == []


def test_t10c_reconnect_loss_mid_identification_stays_lost_unidentified(tmp_path: Path) -> None:
    rig = lost_rig(tmp_path)
    lose_on_next_command(rig, Match(mnemonic="RAN", address=0, param="2"))
    with pytest.raises(ControllerNotConnected) as info:
        rig.manager.connect()
    assert info.value.state == "lost"
    assert rig.manager.state == "lost"
    assert all(s.identification is None for s in rig.manager.slots.values())
    assert not rig.handle_open
    # A later reconnect stops every slot again before identifying.
    rig.mark()
    result = rig.manager.connect()
    assert result.stopped_slots == [1, 2]
    assert rig.commands[:4] == STOP_ALL
    assert all(s.permitted for s in rig.manager.slots.values())


def test_t10c_gate_after_loss_and_failed_reconnect_reports_not_identified(tmp_path: Path) -> None:
    rig = lost_rig(tmp_path)
    lose_on_next_command(rig, Match(mnemonic="RMT", address=1))
    with pytest.raises(ControllerNotConnected):
        rig.manager.connect()
    rig.manager.state = "connected"  # force the state to reach gate step 5
    with pytest.raises(SlotNotPermitted) as info:
        rig.manager.gate("start", 1)
    assert info.value.reason == REASON_NOT_IDENTIFIED

"""Tests for the C6 scripting API (``transport/fake_actions.py``) of the fake transport (Task 8).

Requirements: FR-13.2 (inject each firmware error code, no-reply, CRC errors, stale replies,
``0rdc`` replies, device loss) plus the open failure used by FR-12.7, and the ``times``/``match``
semantics of ``FakeTecDevice.script``. Replies are decoded with the real C3 decoder.
"""

from __future__ import annotations

import pytest
from inheco_tec_api.protocol import commands as c
from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import (
    ReplyAssembler,
    ReplyCrcError,
    encode_command,
    parse_reply,
)
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS
from inheco_tec_api.transport.base import DeviceIOError, DeviceOpenError
from inheco_tec_api.transport.fake import FakeTecDevice
from inheco_tec_api.transport.fake_actions import (
    BadCrc,
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
    RdcReply,
    StaleReply,
)
from tec_fake_helpers import opened, read_texts, send, send_one, write_reports
from tec_reply_helpers import ACCEPTED_ANYWAY

# --- FirmwareCode -------------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(FIRMWARE_ERRORS))
def test_fr13_2_firmware_code_injects_each_error_char(code: str) -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="STT", address=1), action=FirmwareCode(code))
    reply = send_one(dev, c.stt(1, 370))
    assert (reply.name, reply.error_char, reply.data) == ("1stt", code, "")
    assert dev.slots[1].target == 0  # state not changed
    assert dev.commands == ["1STT370"]


def test_fr13_2_firmware_code_accepts_unknown_char() -> None:
    action = FirmwareCode("Z")
    assert action.known is False
    assert FirmwareCode("A").known is True
    dev = opened()
    dev.script(action=action)
    assert send_one(dev, c.rtt(1)).error_char == "Z"


@pytest.mark.parametrize("code", ["", "AB", "é"])
def test_fr13_2_firmware_code_rejects_non_single_ascii(code: str) -> None:
    with pytest.raises(ValueError):
        FirmwareCode(code)


# --- NoReply ------------------------------------------------------------------------------------


def test_fr13_2_no_reply_queues_nothing_but_records_command() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RAT"), action=NoReply())
    assert send(dev, c.rat(1, 1)) == []
    assert dev.pending_reports == 0
    assert dev.commands == ["1RAT1"]
    assert send_one(dev, c.rat(1, 1)).data == "0250"  # times=1 default: next is normal


# --- BadCrc -------------------------------------------------------------------------------------


def test_fr13_2_bad_crc_reply_fails_real_decoder_and_command_executes() -> None:
    body = b"1stt0"
    assert crc8(body) not in ACCEPTED_ANYWAY  # precondition: corruption is detectable
    dev = opened()
    dev.script(match=Match(mnemonic="STT"), action=BadCrc())
    write_reports(dev, encode_command(c.stt(1, 370)))
    assembler = ReplyAssembler()
    payload = dev.read_report(100)
    assert payload is not None and assembler.feed(payload)
    with pytest.raises(ReplyCrcError):
        assembler.finish()
    assert dev.read_report(100) is None
    assert dev.slots[1].target == 370


def test_fr13_2_bad_crc_wrong_crc_differs_and_avoids_nul_and_hash() -> None:
    for valid in range(256):
        bad = BadCrc.wrong_crc(valid)
        assert bad != valid
        assert bad not in (0x00, 0x23)


# --- StaleReply / RdcReply ----------------------------------------------------------------------


def test_fr13_2_stale_reply_is_queued_before_real_reply() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RTT"), action=StaleReply("2rat00111"))
    send_one(dev, c.stt(1, 370))
    replies = send(dev, c.rtt(1))
    assert [(r.name, r.data) for r in replies] == [("2rat", "0111"), ("1rtt", "0370")]


def test_fr13_2_stale_reply_long_text_is_multi_report() -> None:
    dev = opened()
    dev.script(action=StaleReply("0rfv0STALE-LONG-TEXT"))
    texts = read_texts_after(dev, c.rtt(1))
    assert texts == ["0rfv0STALE-LONG-TEXT", "1rtt00000"]


@pytest.mark.parametrize("text", ["", "é"])
def test_fr13_2_stale_reply_rejects_empty_or_non_ascii(text: str) -> None:
    with pytest.raises(ValueError):
        StaleReply(text)


def test_fr13_2_rdc_reply_queues_0rdc_before_real_reply() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="ATE"), action=RdcReply())
    replies = send(dev, c.ate(1, True))
    assert [(r.name, r.error_char) for r in replies] == [("0rdc", "0"), ("1ate", "0")]
    assert dev.slots[1].enabled is True


# --- DeviceLoss ---------------------------------------------------------------------------------


def test_fr13_2_device_loss_raises_and_keeps_handle_dead_until_reopen() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="STT"), action=DeviceLoss())
    send_one(dev, c.rtt(1))  # non-matching command unaffected
    with pytest.raises(DeviceIOError):
        write_reports(dev, encode_command(c.stt(1, 370)))
    assert dev.slots[1].target == 0
    assert dev.commands == ["1RTT"]  # lost command is not recorded
    for io in (
        lambda: dev.read_report(10),
        dev.flush_input,
        lambda: dev.write_report(encode_command(c.rtt(1))[0]),
    ):
        with pytest.raises(DeviceIOError):
            io()
    dev.close()
    dev.open()
    assert dev.open_count == 2
    assert send_one(dev, c.stt(1, 370)).error_char == "0"  # rule used up
    assert dev.slots[1].target == 370


def test_fr13_2_device_loss_on_multi_report_command_raises_on_final_write() -> None:
    dev = opened()
    dev.script(action=DeviceLoss())
    reports = encode_command(c.stt(1, 1999))
    assert len(reports) == 2
    dev.write_report(reports[0])  # continuation report: no failure yet
    with pytest.raises(DeviceIOError):
        dev.write_report(reports[1])


# --- OpenFailure --------------------------------------------------------------------------------


def test_fr13_2_open_failure_makes_next_open_raise_once() -> None:
    dev = FakeTecDevice()
    dev.script(action=OpenFailure())
    with pytest.raises(DeviceOpenError):
        dev.open()
    assert (dev.is_open, dev.open_count) == (False, 0)
    dev.open()
    assert (dev.is_open, dev.open_count) == (True, 1)


def test_fr13_2_open_failure_times_and_not_consumed_by_commands() -> None:
    dev = opened()
    dev.script(action=OpenFailure(), times=2)
    assert send_one(dev, c.rtt(1)).error_char == "0"  # commands ignore the rule
    dev.close()
    for _ in range(2):
        with pytest.raises(DeviceOpenError):
            dev.open()
    dev.open()
    assert dev.open_count == 2


# --- times --------------------------------------------------------------------------------------


def test_fr13_2_times_n_applies_exactly_n_times() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RTT"), action=FirmwareCode("A"), times=3)
    codes = [send_one(dev, c.rtt(1)).error_char for _ in range(5)]
    assert codes == ["A", "A", "A", "0", "0"]


def test_fr13_2_times_none_never_runs_out() -> None:
    dev = opened()
    dev.script(action=NoReply(), times=None)
    for _ in range(20):
        assert send(dev, c.rtt(1)) == []
    assert len(dev.writes) == 20


@pytest.mark.parametrize("times", [0, -1, True, 1.5, "2"])
def test_fr13_2_times_invalid_rejected(times: object) -> None:
    dev = FakeTecDevice()
    with pytest.raises(ValueError):
        dev.script(action=NoReply(), times=times)  # type: ignore[arg-type]


def test_fr13_2_invalid_action_and_match_rejected() -> None:
    dev = FakeTecDevice()
    with pytest.raises(TypeError):
        dev.script(action="NoReply")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dev.script(match="RTT", action=NoReply())  # type: ignore[arg-type]


def test_fr13_2_rules_tried_in_order_and_clear_script() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RTT"), action=FirmwareCode("3"))
    dev.script(match=Match(mnemonic="RTT"), action=FirmwareCode("A"))
    assert [send_one(dev, c.rtt(1)).error_char for _ in range(3)] == ["3", "A", "0"]
    dev.script(action=NoReply(), times=None)
    dev.clear_script()
    assert send_one(dev, c.rtt(1)).error_char == "0"


# --- match --------------------------------------------------------------------------------------


def test_fr13_2_match_by_address_leaves_other_slot_alone() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="ATE", address=1), action=FirmwareCode("3"), times=None)
    assert send_one(dev, c.ate(2, True)).error_char == "0"
    assert send_one(dev, c.ate(1, True)).error_char == "3"
    assert (dev.slots[1].enabled, dev.slots[2].enabled) == (False, True)


def test_fr13_2_match_by_param() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RAT", param="3"), action=FirmwareCode("9"), times=None)
    assert send_one(dev, c.rat(1, 1)).error_char == "0"
    assert send_one(dev, c.rat(1, 3)).error_char == "9"


def test_fr13_2_match_mnemonic_is_case_insensitive() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="stt"), action=FirmwareCode("5"))
    assert send_one(dev, c.stt(1, 10)).error_char == "5"


def test_fr13_2_non_matching_command_does_not_consume_rule() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RLT"), action=FirmwareCode("3"))
    for _ in range(3):
        assert send_one(dev, c.rtt(1)).error_char == "0"
    assert send_one(dev, c.rlt(1)).error_char == "3"


def test_fr13_2_match_none_and_empty_match_match_everything() -> None:
    assert Match().matches(0, "RDC", "1")
    assert Match(address=0).matches(0, "rfv", "4")
    assert not Match(address=0).matches(1, "RFV", "4")
    assert not Match(param="").matches(1, "RAT", "1")
    assert Match(param="").matches(1, "RTT", "")


def test_fr13_2_script_and_state_survive_reopen() -> None:
    dev = opened()
    dev.script(match=Match(mnemonic="RTT"), action=FirmwareCode("A"), times=2)
    assert send_one(dev, c.rtt(1)).error_char == "A"
    dev.close()
    dev.open()
    assert send_one(dev, c.rtt(1)).error_char == "A"
    assert send_one(dev, c.rtt(1)).error_char == "0"


def read_texts_after(dev: FakeTecDevice, cmd: c.Command) -> list[str]:
    """Write ``cmd`` and return the raw reply texts (each also parses)."""
    write_reports(dev, encode_command(cmd))
    texts = read_texts(dev)
    for text in texts:
        parse_reply(text)
    return texts

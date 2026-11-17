"""Tests for the C3 wire codec: CRC-8 (Task 3) and HID framing (Task 4).

Golden values come from ``<private-doc-ref>`` §1.3, never from the
implementation. The CRC byte of each worked example is the byte that follows the upper-cased
message in its HID report; for ``1RTT`` the report shows ``w`` because the raw CRC was ``#`` or
``0`` (§1.3 step 4, the substitution itself is a Task 4 framing rule).

Layout: one section per component. Task 3 owns the ``CRC-8`` section. Task 4 appends a
``Framing`` section (``encode_command`` golden vectors, ``_last_report_byte8``, ``ReplyAssembler``,
``parse_reply``) below it; the shared constants at the top of the file serve both sections.
The reply bound, frame-error, DD-9 and further ``parse_reply`` tests live in
``test_tec_framing_reply.py`` (500-line module limit); both use ``tec_reply_helpers``.
"""

from __future__ import annotations

import pytest
from inheco_tec_api.protocol import commands, crc, framing
from inheco_tec_api.protocol.commands import Command, CommandNotAllowed
from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import (
    Reply,
    ReplyAssembler,
    ReplyCrcError,
    encode_command,
    parse_reply,
)
from tec_reply_helpers import ACCEPTED_ANYWAY as _ACCEPTED_ANYWAY
from tec_reply_helpers import assemble as _assemble
from tec_reply_helpers import reply_payloads as _reply_payloads

# Protocol report §1.3 "Worked examples": message -> full HID report bytes (incl. report ID 0x00).
GOLDEN_REPORTS: dict[str, list[bytes]] = {
    "0RFV1": [bytes.fromhex("00 30 52 46 56 31 78 00 00")],
    "1RAT1": [bytes.fromhex("00 31 52 41 54 31 5E 00 00")],
    "1ATE1": [bytes.fromhex("00 31 41 54 45 31 B9 00 00")],
    "1RTT": [bytes.fromhex("00 31 52 54 54 77 00 00 00")],
    "1STT370": [bytes.fromhex("00 31 53 54 54 33 37 30 80")],
    "0RDC1": [bytes.fromhex("00 30 52 44 43 31 24 00 00")],
    "1STT1000": [
        bytes.fromhex("00 31 53 54 54 31 30 30 23"),
        bytes.fromhex("00 30 4F 00 00 00 00 00 00"),
    ],
}

# Raw CRC byte of each §1.3 example whose report carries the CRC unsubstituted.
GOLDEN_CRC: dict[str, int] = {
    "0RFV1": 0x78,
    "1RAT1": 0x5E,
    "1ATE1": 0xB9,
    "1STT370": 0x80,
    "0RDC1": 0x24,
    "1STT1000": 0x4F,
}


# ---------------------------------------------------------------------------------------------
# CRC-8 (Task 3; FR-4.3, NFR-5, NFR-8)
# ---------------------------------------------------------------------------------------------


def test_fr4_3_golden_crc_table_matches_report_bytes() -> None:
    """Guard: the CRC constants above are the bytes that follow each message in the §1.3 reports."""
    for msg, value in GOLDEN_CRC.items():
        stream = b"".join(
            report[1:8] if len(GOLDEN_REPORTS[msg]) > 1 else report[1:]
            for report in GOLDEN_REPORTS[msg]
        )
        assert stream[len(msg)] == value, msg


@pytest.mark.parametrize(("msg", "expected"), sorted(GOLDEN_CRC.items()))
def test_fr4_3_crc8_matches_protocol_report_golden_values(msg: str, expected: int) -> None:
    assert crc8(msg.encode("ascii")) == expected


@pytest.mark.parametrize(("msg", "expected"), sorted(GOLDEN_CRC.items()))
def test_fr4_3_crc8_explicit_init_0xa1_equals_default(msg: str, expected: int) -> None:
    assert crc8(msg.encode("ascii"), init=0xA1) == expected
    assert crc8(msg.encode("ascii"), 0xA1) == crc8(msg.encode("ascii"))


def test_fr4_3_crc8_1rtt_raw_crc_needs_w_substitution() -> None:
    """§1.3: the ``1RTT`` report carries ``w`` because its raw CRC is ``0x23`` or ``0x00``."""
    assert crc8(b"1RTT") in {0x23, 0x00}


def test_fr4_3_default_init_is_0xa1() -> None:
    assert crc.CRC_INIT == 0xA1
    assert crc8(b"") == 0xA1


def test_fr4_3_empty_data_returns_init() -> None:
    for init in (0x00, 0x01, 0x7F, 0xA1, 0xFF):
        assert crc8(b"", init=init) == init


def test_fr4_3_crc8_is_dallas_maxim_reflected_0x8c() -> None:
    """CRC-8/MAXIM catalogue check value: ``"123456789"`` with init 0 gives ``0xA1``."""
    assert crc8(b"123456789", init=0x00) == 0xA1


def test_fr4_3_crc8_matches_reference_reflected_algorithm() -> None:
    """Cross-check against the textbook form ``crc = (crc >> 1) ^ 0x8C`` over many inputs."""

    def reference(data: bytes, init: int) -> int:
        value = init
        for byte in data:
            value ^= byte
            for _ in range(8):
                value = (value >> 1) ^ 0x8C if value & 1 else value >> 1
        return value

    samples = [bytes([b]) for b in range(256)] + [
        b"0RFV1",
        b"1STT1999",
        b"0AEO",
        bytes(range(256)),
    ]
    for data in samples:
        for init in (0x00, 0xA1, 0xFF):
            assert crc8(data, init=init) == reference(data, init)


def test_fr4_3_crc8_result_is_one_byte() -> None:
    for data in (b"\xff" * 64, bytes(range(256)), b"1STT370"):
        value = crc8(data)
        assert isinstance(value, int)
        assert 0 <= value <= 0xFF


def test_fr4_3_crc8_accepts_bytearray_and_memoryview() -> None:
    assert crc8(bytearray(b"0RFV1")) == 0x78
    assert crc8(memoryview(b"0RFV1")) == 0x78


def test_fr4_3_crc8_is_order_sensitive() -> None:
    assert crc8(b"1RAT1") != crc8(b"1TAR1")


@pytest.mark.parametrize("init", [-1, 0x100, 0x1A1])
def test_fr4_3_crc8_rejects_init_outside_one_byte(init: int) -> None:
    with pytest.raises(ValueError):
        crc8(b"0RFV1", init=init)


# ---------------------------------------------------------------------------------------------
# Framing (Task 4) — appended below this line.
# ---------------------------------------------------------------------------------------------

# The §1.3 golden messages built through the typed C4 constructors.
GOLDEN_COMMANDS: dict[str, Command] = {
    "0RFV1": commands.rfv(1),
    "1RAT1": commands.rat(1, 1),
    "1ATE1": commands.ate(1, True),
    "1RTT": commands.rtt(1),
    "1STT370": commands.stt(1, 370),
    "0RDC1": commands.rdc1(),
    "1STT1000": commands.stt(1, 1000),
}


def _all_allowed_commands() -> list[Command]:
    """Every command the typed constructors can build (bounded parameter sweep)."""
    out: list[Command] = []
    for slot in range(1, 7):
        out += [commands.stt(slot, t) for t in (0, 9, 10, 99, 100, 370, 999, 1000, 1999)]
        out += [commands.ate(slot, True), commands.ate(slot, False)]
        out += [commands.rat(slot, sel) for sel in (1, 2, 3)]
        out += [commands.rtt(slot), commands.rlt(slot), commands.ran(slot), commands.rhe(slot)]
        out.append(commands.rmt1(slot))
    out += [commands.rhv(sel) for sel in range(9)]
    out += [commands.rtd(slot) for slot in range(7)]
    out += [commands.rfv(sel) for sel in range(5)]
    out += [commands.rdc1(), commands.aeo()]
    return out


def _data_stream(reports: list[bytes]) -> bytes:
    """Concatenate the data bytes of output reports (drop report ID and continuation byte 8)."""
    if len(reports) == 1:
        return reports[0][1:]
    return b"".join(report[1:8] for report in reports)


# --- encode_command: golden vectors (FR-4.5) ---------------------------------------------------


def test_fr4_5_golden_commands_have_the_report_message_text() -> None:
    assert sorted(GOLDEN_COMMANDS) == sorted(GOLDEN_REPORTS)
    for msg, cmd in GOLDEN_COMMANDS.items():
        assert cmd.text == msg


@pytest.mark.parametrize("msg", sorted(GOLDEN_REPORTS))
def test_fr4_5_encode_command_matches_protocol_report_byte_for_byte(msg: str) -> None:
    assert encode_command(GOLDEN_COMMANDS[msg]) == GOLDEN_REPORTS[msg]


def test_fr4_4_1stt1000_is_two_reports_with_continuation_marker() -> None:
    reports = encode_command(commands.stt(1, 1000))
    assert len(reports) == 2
    assert reports[0][8] == 0x23
    assert reports[1][8] == 0x00


# --- encode_command: generic rules (FR-4.2, FR-4.3, FR-4.4) ------------------------------------


@pytest.mark.parametrize("cmd", _all_allowed_commands(), ids=lambda c: c.text)
def test_fr4_4_every_report_is_nine_bytes_with_report_id_zero(cmd: Command) -> None:
    for report in encode_command(cmd):
        assert isinstance(report, bytes)
        assert len(report) == 9
        assert report[0] == 0x00


@pytest.mark.parametrize("cmd", _all_allowed_commands(), ids=lambda c: c.text)
def test_fr4_3_fr4_4_reports_carry_message_then_crc(cmd: Command) -> None:
    """Data = upper-cased message + CRC (or ``w``), chunked per §1.3 step 5, zero-padded."""
    message = cmd.text.encode("ascii")
    raw = crc8(message)
    expected_crc = 0x77 if raw in {0x23, 0x00} else raw
    buf = message + bytes([expected_crc])
    reports = encode_command(cmd)
    if len(buf) <= 8:
        assert reports == [b"\x00" + buf.ljust(8, b"\x00")]
    else:
        packets = (len(buf) - 1) // 7
        assert len(reports) == packets + 1
        for k, report in enumerate(reports):
            assert report[1:8] == buf[k * 7 : k * 7 + 7].ljust(7, b"\x00")
            assert report[8] == (0x23 if k < packets else 0x00)


def test_fr4_3_crc_0x23_or_0x00_is_replaced_by_0x77() -> None:
    """Every allowed command whose raw CRC is ``#`` or ``0x00`` carries ``w``; at least one does."""
    substituted = [c for c in _all_allowed_commands() if crc8(c.text.encode()) in {0x23, 0x00}]
    assert any(c.text == "1RTT" for c in substituted)
    for cmd in substituted:
        assert _data_stream(encode_command(cmd))[len(cmd.text)] == 0x77, cmd.text


@pytest.mark.parametrize("cmd", _all_allowed_commands(), ids=lambda c: c.text)
def test_fr4_3_crc_byte_is_never_hash_or_nul(cmd: Command) -> None:
    assert _data_stream(encode_command(cmd))[len(cmd.text)] not in {0x23, 0x00}


def test_fr4_2_wire_case_rule_is_verbatim() -> None:
    """§1.3 step 2: a code above 0x60 that is not ``#`` loses 0x20; anything else is copied."""
    assert framing._to_wire_case(ord("a")) == ord("A")
    assert framing._to_wire_case(ord("z")) == ord("Z")
    assert framing._to_wire_case(ord("#")) == ord("#")
    assert framing._to_wire_case(ord("A")) == ord("A")
    assert framing._to_wire_case(ord("0")) == ord("0")
    assert framing._to_wire_case(0x60) == 0x60
    assert framing._to_wire_case(0x61) == 0x41
    assert framing._to_wire_case(0x7B) == 0x5B  # verbatim quirk, not str.upper()


def test_fr10_1_encode_command_refuses_lower_case_mnemonic() -> None:
    """The allowlist is exact; a hand-built lower-case command never reaches the encoder body."""
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, "stt", "370"))


# --- _last_report_byte8 (FR-4.4, §1.3 step 5 verbatim) -----------------------------------------


def test_fr4_4_last_report_byte8_zero_when_length_rule_not_met() -> None:
    buf = b"1STT1000" + bytes([0x4F])  # n = 9, packets = 1 -> (1+1)*7+1 = 15 != 9
    assert framing._last_report_byte8(buf, 1) == 0x00


def test_fr4_4_last_report_byte8_returns_crc_when_length_rule_met() -> None:
    # packets = 1 -> the rule needs n = 15
    assert framing._last_report_byte8(b"A" * 14 + bytes([0x4F]), 1) == 0x4F
    assert framing._last_report_byte8(b"A" * 14 + bytes([0x60]), 1) == 0x60


def test_fr4_4_last_report_byte8_reduces_crc_above_0x60_by_0x20() -> None:
    assert framing._last_report_byte8(b"A" * 14 + bytes([0x78]), 1) == 0x58
    assert framing._last_report_byte8(b"A" * 14 + bytes([0x61]), 1) == 0x41
    # packets = 2 -> the rule needs n = 22
    assert framing._last_report_byte8(b"A" * 21 + bytes([0xB9]), 2) == 0x99


def test_fr4_4_last_report_byte8_crc_branch_unreachable_with_encoder_packets() -> None:
    """Design C3: with ``packets = (n-1)//7`` the CRC branch never fires."""
    for n in range(9, 300):
        buf = b"A" * (n - 1) + bytes([0x78])
        assert framing._last_report_byte8(buf, (n - 1) // 7) == 0x00


# --- ReplyAssembler round trips (FR-4.6) -------------------------------------------------------


@pytest.mark.parametrize("text", [b"0rfv1", b"1ate0", b"1stt0", b"0rdc0", b"0rtd01", b"1rlt0-5"])
def test_fr4_6_round_trip_single_report(text: bytes) -> None:
    payloads = _reply_payloads(text)
    assert len(payloads) == 1
    assert _assemble(payloads) == text.decode()


@pytest.mark.parametrize(
    "text",
    [
        b"1rat00370",  # 10 bytes with CRC -> 2 reports
        b"1rtt00370",
        b"0rfv10V2.20 2024-01-01",
        b"0rfv14(c) INHECO GmbH all rights reserved",
        b"1ran0" + b"9" * 9,  # exactly 14 bytes with CRC -> 2 full chunks
    ],
)
def test_fr4_6_round_trip_multi_report(text: bytes) -> None:
    payloads = _reply_payloads(text)
    assert len(payloads) >= 2
    assert all(p[7] == 0x23 for p in payloads[:-1])
    assert _assemble(payloads) == text.decode()


def test_fr4_6_continuation_keeps_only_bytes_0_to_6() -> None:
    assembler = ReplyAssembler()
    text = b"1rat00370"
    buf = text + bytes([crc8(text)])
    # Byte 7 of a continued payload is the marker, not data.
    assert assembler.feed(buf[:7] + b"#") is False
    assert assembler.complete is False
    assert assembler.feed(buf[7:].ljust(8, b"\x00")) is True
    assert assembler.finish() == "1rat00370"


def test_fr4_6_payload_without_trailing_padding_is_accepted() -> None:
    text = b"1ate0"
    assert _assemble([text + bytes([crc8(text)])]) == "1ate0"


# --- CRC validation (FR-4.6, §1.4 step 5) ------------------------------------------------------


def test_fr4_6_corrupted_crc_is_rejected() -> None:
    text = b"1rat00370"
    assert crc8(text) not in _ACCEPTED_ANYWAY
    bad = crc8(text) ^ 0x01
    with pytest.raises(ReplyCrcError) as info:
        _assemble(_reply_payloads(text, crc_byte=bad))
    assert info.value.kind == "crc"


def test_fr4_6_corrupted_body_is_rejected() -> None:
    good_crc = crc8(b"1rat00370")
    tampered = b"1rat00380"
    assert crc8(tampered) not in _ACCEPTED_ANYWAY | {good_crc}
    with pytest.raises(ReplyCrcError):
        _assemble(_reply_payloads(tampered, crc_byte=good_crc))


def test_fr4_6_corrupted_crc_multi_report_is_rejected() -> None:
    text = b"0rfv10V2.20 2024-01-01"
    assert crc8(text) not in _ACCEPTED_ANYWAY
    bad = crc8(text) ^ 0x01
    with pytest.raises(ReplyCrcError):
        _assemble(_reply_payloads(text, crc_byte=bad))


def _find_body_with_crc(target: int) -> bytes:
    for value in range(100000):
        body = b"1rat0" + str(value).encode()
        if crc8(body) == target:
            return body
    raise AssertionError(f"no body with CRC {target:#x}")


@pytest.mark.parametrize("computed", sorted(_ACCEPTED_ANYWAY))
def test_fr4_6_computed_crc_in_accepted_set_passes_with_any_crc_char(computed: int) -> None:
    """§1.4 step 5 accept rule: computed ``#``, 0xFF or 0x00 is accepted (ported verbatim)."""
    body = _find_body_with_crc(computed)
    assert _assemble(_reply_payloads(body, crc_byte=ord("Z"))) == body.decode()


# --- parse_reply (FR-4.7); further parse cases live in test_tec_framing_reply.py ---------------


def test_fr4_7_parse_1rat00370() -> None:
    """FR-4 acceptance (amendment A1): ``1rat00370`` -> E ``0``, data ``0370``."""
    reply = parse_reply("1rat00370")
    assert reply == Reply(address="1", mnemonic="rat", error_char="0", data="0370")
    assert reply.name == "1rat"

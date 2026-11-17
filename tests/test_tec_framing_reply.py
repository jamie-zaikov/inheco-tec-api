"""Tests for the C3 reply side: bound, frame errors, DD-9 non-ported quirks, parse_reply (Task 4).

Split out of ``test_tec_crc_framing.py`` to keep each module under the 500-line limit. That file
keeps the CRC section, the FR-4.5 golden vectors, ``_last_report_byte8``, the decoder round trips,
the CRC rejection tests and the ``1rat00370`` acceptance parse. Payload builders are shared through
``tec_reply_helpers`` (protocol report §1.4 step 3).
"""

from __future__ import annotations

import pytest
from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import (
    MAX_REPLY_BYTES,
    ReplyAssembler,
    ReplyCrcError,
    ReplyError,
    ReplyFrameError,
    parse_reply,
)
from tec_reply_helpers import ACCEPTED_ANYWAY as _ACCEPTED_ANYWAY
from tec_reply_helpers import assemble as _assemble
from tec_reply_helpers import reply_payloads as _reply_payloads

# --- Reply bound (MAX_REPLY_BYTES, fail-closed hardening) --------------------------------------


def test_reply_bound_is_256() -> None:
    assert MAX_REPLY_BYTES == 256


def test_reply_bound_exactly_256_bytes_is_allowed() -> None:
    assembler = ReplyAssembler()
    for _ in range(36):  # 36 * 7 = 252
        assert assembler.feed(b"1234567#") is False
    assert assembler.feed(b"1234") is True  # 256 bytes total


def test_reply_bound_257_bytes_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    for _ in range(36):
        assembler.feed(b"1234567#")
    with pytest.raises(ReplyFrameError):
        assembler.feed(b"12345")


def test_reply_bound_endless_continuation_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    with pytest.raises(ReplyFrameError) as info:
        for _ in range(1000):
            assembler.feed(b"1234567#")
    assert info.value.kind == "parse"


# --- Frame errors (FR-4.6, FR-9.3) -------------------------------------------------------------


def test_fr4_6_finish_before_complete_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    with pytest.raises(ReplyFrameError):
        assembler.finish()
    assembler.feed(b"1234567#")
    with pytest.raises(ReplyFrameError):
        assembler.finish()


def test_fr4_6_all_nul_reply_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    assembler.feed(b"\x00" * 8)
    with pytest.raises(ReplyFrameError):
        assembler.finish()


def test_fr4_6_single_character_reply_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    assembler.feed(b"Z" + b"\x00" * 7)
    with pytest.raises(ReplyFrameError):
        assembler.finish()


def test_fr4_6_empty_payload_raises_frame_error() -> None:
    with pytest.raises(ReplyFrameError):
        ReplyAssembler().feed(b"")


def test_fr4_6_feed_after_complete_raises_frame_error() -> None:
    assembler = ReplyAssembler()
    assembler.feed(_reply_payloads(b"1ate0")[0])
    with pytest.raises(ReplyFrameError):
        assembler.feed(b"\x00" * 8)


def test_fr9_3_non_ascii_body_with_valid_crc_raises_frame_error() -> None:
    with pytest.raises(ReplyFrameError) as info:
        _assemble(_reply_payloads(b"1rat0\xb0C"))
    assert info.value.kind == "parse"


def test_fr9_3_error_classes_and_kinds() -> None:
    assert issubclass(ReplyCrcError, ReplyError)
    assert issubclass(ReplyFrameError, ReplyError)
    assert ReplyCrcError.kind == "crc"
    assert ReplyFrameError.kind == "parse"


# --- DLL quirks NOT ported (DD-9) --------------------------------------------------------------


def test_dd9_first_char_7_does_not_bypass_crc() -> None:
    text = b"7rat00370"
    assert crc8(text) not in _ACCEPTED_ANYWAY
    bad = crc8(text) ^ 0x01
    with pytest.raises(ReplyCrcError):
        _assemble(_reply_payloads(text, crc_byte=bad))


def test_dd9_errort_reply_without_crc_is_rejected() -> None:
    """The DLL accepts any reply containing ``ErrorT`` unchecked; the port validates the CRC."""
    raw = b"1ErrorT"
    assert crc8(raw[:-1]) not in _ACCEPTED_ANYWAY | {raw[-1]}
    assembler = ReplyAssembler()
    assembler.feed(raw.ljust(8, b"\x00"))
    with pytest.raises(ReplyCrcError):
        assembler.finish()


def test_dd9_errort_reply_with_valid_crc_is_ordinary_text() -> None:
    assert _assemble(_reply_payloads(b"1ErrorT")) == "1ErrorT"


# --- parse_reply (FR-4.7, FR-9.3) --------------------------------------------------------------


def test_fr4_7_index_rule_e_at_4_data_from_5() -> None:
    reply = parse_reply("1rat0370")
    assert reply.error_char == "0"
    assert reply.data == "370"


def test_fr4_7_error_char_is_case_insensitive() -> None:
    assert parse_reply("1atea").error_char == "A"
    assert parse_reply("1ateA").error_char == "A"
    assert parse_reply("1ased").error_char == "D"


def test_fr4_7_minimum_length_five_gives_empty_data() -> None:
    reply = parse_reply("1ate0")
    assert reply.error_char == "0"
    assert reply.data == ""


@pytest.mark.parametrize("text", ["", "1", "1ra", "1rat"])
def test_fr9_3_short_reply_raises_parse_error(text: str) -> None:
    with pytest.raises(ReplyFrameError) as info:
        parse_reply(text)
    assert info.value.kind == "parse"


def test_fr4_7_echo_keeps_received_case() -> None:
    reply = parse_reply("1RAT00370")
    assert reply.mnemonic == "RAT"
    assert reply.data == "0370"


def test_fr4_6_fr4_7_full_decode_pipeline_multi_report() -> None:
    reply = parse_reply(_assemble(_reply_payloads(b"1rat00370")))
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == (
        "1",
        "rat",
        "0",
        "0370",
    )

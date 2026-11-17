"""Shared reply-building helpers for the C3 framing suites (not a test module).

``test_tec_crc_framing.py`` and ``test_tec_framing_reply.py`` both build 8-byte HID input payloads
per protocol report §1.4 step 3 and drive them through ``ReplyAssembler``. This directory has no
``__init__.py`` (design Risk R8); pytest's default ``prepend`` import mode puts it on ``sys.path``,
so the test modules import this file by its unique top-level name.
"""

from __future__ import annotations

from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import ReplyAssembler

# §1.4 step 5: a computed CRC of ``#``, 0xFF or 0x00 is accepted whatever the CRC character is.
ACCEPTED_ANYWAY = {0x23, 0xFF, 0x00}


def reply_payloads(text: bytes, crc_byte: int | None = None) -> list[bytes]:
    """Build 8-byte input payloads (report ID removed) for ``text`` + CRC per §1.4 step 3.

    Non-final payloads carry 7 data bytes and ``#`` at index 7; the final payload is NUL-padded.
    """
    buf = text + bytes([crc8(text) if crc_byte is None else crc_byte])
    if len(buf) <= 8:
        return [buf.ljust(8, b"\x00")]
    chunks = [buf[i : i + 7] for i in range(0, len(buf), 7)]
    payloads = [chunk + b"#" for chunk in chunks[:-1]]
    payloads.append(chunks[-1].ljust(8, b"\x00"))
    return payloads


def assemble(payloads: list[bytes]) -> str:
    """Feed every payload, assert the assembler completes, and return the validated reply text."""
    assembler = ReplyAssembler()
    done = False
    for payload in payloads:
        done = assembler.feed(payload)
    assert done
    assert assembler.complete
    return assembler.finish()

"""HID framing of the Inheco TEC wire protocol (design C3; FR-4.2 to FR-4.7, DD-9).

- :func:`encode_command` turns an allowlisted :class:`~.commands.Command` into 9-byte HID output
  reports: protocol report §1.3 (``GlobCom.WriteOnly`` IL_0000..IL_0195).
- :class:`ReplyAssembler` rebuilds one reply from input-report payloads and validates its CRC:
  protocol report §1.4 (``GlobCom.ReadSync``, ``WriteToGlobString``, ``CheckCRC``).
- :func:`parse_reply` splits a validated reply into address, mnemonic, error character and data:
  protocol report §2.2.

The CRC reconstruction and the outbound framing are INFERRED — see api/README.md checklist item 2.
Reply continuation with ``#`` at payload index 7 is INFERRED — see api/README.md checklist item 1.

DLL behaviours deliberately not ported (fail closed, DD-9): the "first char ``7`` bypasses the CRC"
acceptance of ``CheckCRC``, the ``ErrorT`` acceptance without a CRC check of ``ReadSync``, and the
DLL's own 150 ms automatic resend (the engine's retry policy replaces it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from inheco_tec_api.protocol.commands import Command, validate
from inheco_tec_api.protocol.crc import crc8

REPORT_ID: Final = 0x00
"""Byte 0 of every HID output report (protocol report §1.3 step 5)."""

REPORT_SIZE: Final = 9
"""HID output report size: report ID plus 8 data bytes (protocol report §1.3 step 5)."""

CHUNK_SIZE: Final = 7
"""Data bytes per report when a message spans several reports (protocol report §1.3 step 5)."""

CONTINUATION: Final = 0x23
"""``#``: byte 8 of a non-final output report, and payload index 7 of a non-final input report
(protocol report §1.3 step 5, §1.4 step 3)."""

CRC_SUBSTITUTE: Final = 0x77
"""``w``: sent instead of a CRC of ``0x23`` or ``0x00`` (protocol report §1.3 step 4)."""

_CRC_REPLACED: Final = frozenset({0x23, 0x00})
_CRC_ALWAYS_ACCEPTED: Final = frozenset({0x23, 0xFF, 0x00})

MAX_REPLY_BYTES: Final = 256
"""Upper bound on the assembled reply length, in bytes, before NUL stripping.

This is not a protocol-report rule. It is a fail-closed bound so that a device that keeps sending
``#``-continued reports, or a report of unexpected size, cannot grow a reply without limit (and
cannot feed an unbounded digit string to ``decode_tenths``). The longest reply of an allowlisted
command (a firmware or copyright string of ``RFV``) is far below it.
"""


class ReplyError(Exception):
    """Base class of reply decoding failures; ``kind`` is the transaction failure kind (C7)."""

    kind: ClassVar[str] = "parse"


class ReplyCrcError(ReplyError):
    """The reply CRC does not validate (protocol report §1.4 step 5). Retryable (FR-5.6)."""

    kind: ClassVar[str] = "crc"


class ReplyFrameError(ReplyError):
    """The reply is empty, too long, non-ASCII or too short to parse (FR-4.7, FR-9.3)."""

    kind: ClassVar[str] = "parse"


def _to_wire_case(byte: int) -> int:
    """Upper-case one character as the DLL does (protocol report §1.3 step 2).

    Verbatim rule: a code above ``0x60`` that is not ``#`` loses ``0x20``; every other code is
    copied (FR-4.2).
    """
    if byte > 0x60 and byte != CONTINUATION:
        return byte - 0x20
    return byte


def _command_crc(message: bytes) -> int:
    """CRC byte of an upper-cased message (protocol report §1.3 steps 3–4; FR-4.3).

    The CRC covers every character except ``#``; a CRC of ``0x23`` or ``0x00`` becomes ``0x77``.
    """
    crc = crc8(bytes(b for b in message if b != CONTINUATION))
    return CRC_SUBSTITUTE if crc in _CRC_REPLACED else crc


def _last_report_byte8(buf: bytes, packets: int) -> int:
    """Byte 8 of the last report of a multi-report message: verbatim protocol report §1.3 step 5.

    ``buf`` is the full message plus CRC (``n = len(buf)``). The byte is ``0x00``, except when
    ``(packets + 1) * 7 + 1 == n``: then it is the last buffer byte (the CRC), reduced by ``0x20``
    if it is above ``0x60``. With ``packets = (n - 1) // 7`` that branch cannot be reached; it is
    kept verbatim and tested directly (design C3, FR-4.4). INFERRED quirk — see api/README.md
    checklist item 2.
    """
    if (packets + 1) * CHUNK_SIZE + 1 == len(buf):
        return _to_wire_case(buf[-1])
    return 0x00


def _report(data: bytes) -> bytes:
    """One 9-byte output report: report ID ``0x00``, ``data``, zero padding (§1.3 step 5)."""
    return bytes([REPORT_ID]) + data.ljust(REPORT_SIZE - 1, b"\x00")


def encode_command(cmd: Command) -> list[bytes]:
    """Encode ``cmd`` as HID output reports (protocol report §1.3; FR-4.2, FR-4.3, FR-4.4).

    :func:`~.commands.validate` runs first, so a command off the allowlist raises
    ``CommandNotAllowed`` before any byte exists (FR-10.1). Then:

    1. upper-case every character except ``#`` (§1.3 step 2);
    2. append the CRC-8 over every character except ``#``, or ``0x77`` when it is ``0x23`` or
       ``0x00`` (§1.3 steps 3–4);
    3. with ``n = len(message) + 1``: when ``n <= 8``, one report with the ``n`` bytes zero-padded;
       otherwise ``packets = (n - 1) // 7`` and, for ``k = 0..packets``, a report carrying chunk
       ``k*7 .. k*7+6`` (zero-padded) with byte 8 = ``#`` on every report except the last, whose
       byte 8 comes from :func:`_last_report_byte8` (§1.3 step 5).

    Every report is exactly 9 bytes. INFERRED — see api/README.md checklist item 2.
    """
    validate(cmd)
    message = bytes(_to_wire_case(b) for b in cmd.text.encode("ascii"))
    buf = message + bytes([_command_crc(message)])
    n = len(buf)
    if n <= REPORT_SIZE - 1:
        return [_report(buf)]
    packets = (n - 1) // CHUNK_SIZE
    reports: list[bytes] = []
    for k in range(packets + 1):
        chunk = buf[k * CHUNK_SIZE : k * CHUNK_SIZE + CHUNK_SIZE].ljust(CHUNK_SIZE, b"\x00")
        byte8 = CONTINUATION if k < packets else _last_report_byte8(buf, packets)
        reports.append(_report(chunk + bytes([byte8])))
    return reports


class ReplyAssembler:
    """Rebuild one reply from input-report payloads (protocol report §1.4 steps 2–6; FR-4.6).

    Feed each payload as returned by the transport, with the report ID already removed (design
    C5). When payload index 7 is ``#``, bytes 0–6 are kept and another report is needed; otherwise
    every byte (including NUL padding) is kept and the reply is complete (§1.4 step 3). INFERRED —
    see api/README.md checklist item 1.

    :meth:`finish` then validates the CRC and returns the reply text without its CRC character.
    The assembled length is bounded by :data:`MAX_REPLY_BYTES` (fail closed).
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._complete = False

    @property
    def complete(self) -> bool:
        """True once a final (non-continued) payload has been fed."""
        return self._complete

    def feed(self, payload: bytes) -> bool:
        """Add one input-report payload; return True when the reply is complete (§1.4 step 3).

        Raises :class:`ReplyFrameError` for an empty payload, a payload fed after completion, or
        an assembled reply longer than :data:`MAX_REPLY_BYTES`.
        """
        if self._complete:
            raise ReplyFrameError("reply already complete")
        if not payload:
            raise ReplyFrameError("empty input report")
        if len(payload) > CHUNK_SIZE and payload[CHUNK_SIZE] == CONTINUATION:
            self._buf += payload[:CHUNK_SIZE]
        else:
            self._buf += payload
            self._complete = True
        if len(self._buf) > MAX_REPLY_BYTES:
            raise ReplyFrameError(f"reply longer than {MAX_REPLY_BYTES} bytes")
        return self._complete

    def finish(self) -> str:
        """Validate the assembled reply and return it without the CRC (§1.4 steps 5–6; FR-4.6).

        NUL padding is stripped. The CRC-8 (init ``0xA1``) over every character except the last
        must equal the last character, or be ``#``, ``0xFF`` or ``0x00`` (the DLL's acceptance
        rule, ported verbatim); else :class:`ReplyCrcError`. An incomplete, empty or non-ASCII
        reply raises :class:`ReplyFrameError`. The "first char ``7`` bypasses the CRC" quirk is
        not ported (DD-9). INFERRED — see api/README.md checklist item 2.
        """
        if not self._complete:
            raise ReplyFrameError("reply incomplete")
        stripped = bytes(b for b in self._buf if b != 0x00)
        if len(stripped) < 2:
            raise ReplyFrameError("reply empty")
        body, received = stripped[:-1], stripped[-1]
        computed = crc8(body)
        if computed != received and computed not in _CRC_ALWAYS_ACCEPTED:
            raise ReplyCrcError("reply CRC mismatch")
        if not body.isascii():
            raise ReplyFrameError("reply is not ASCII")
        return body.decode("ascii")


@dataclass(frozen=True, slots=True)
class Reply:
    """A parsed reply ``<addr><mnemonic lower-case><E><data>`` (protocol report §2.2; FR-4.7).

    ``address`` and ``mnemonic`` are the raw echo characters (index 0 and 1–3, case as received);
    echo matching is case-insensitive and belongs to the engine (FR-5.4). ``error_char`` is
    upper-cased.
    """

    address: str
    mnemonic: str
    error_char: str
    data: str

    @property
    def name(self) -> str:
        """The four-character echo ``<addr><mnemonic>`` as received, e.g. ``1rat``."""
        return self.address + self.mnemonic


def parse_reply(text: str) -> Reply:
    """Split a CRC-validated reply (protocol report §2.2; FR-4.7).

    Requires at least 5 characters; ``E = text[4].upper()`` and ``data = text[5:]`` (HSL:1996,
    HSL:1909-1912). ``1rat00370`` parses to address ``1``, mnemonic ``rat``, ``E`` ``0``, data
    ``0370``. A shorter reply raises :class:`ReplyFrameError` (kind ``parse``). The case of ``E``
    on the wire is INFERRED — see api/README.md checklist item 5.
    """
    if len(text) < 5:
        raise ReplyFrameError(f"reply too short to parse: {len(text)} characters")
    return Reply(address=text[0], mnemonic=text[1:4], error_char=text[4].upper(), data=text[5:])

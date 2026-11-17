"""Shared helpers for the fake-transport suites (not a test module).

``test_tec_fake.py`` and ``test_tec_fake_actions.py`` drive a ``FakeTecDevice`` with reports from
the real C3 ``encode_command`` and decode what it queues with the real ``ReplyAssembler`` and
``parse_reply`` (FR-13.1). Commands off the allowlist (``encode_command`` refuses them) are built
here as raw 9-byte output reports per protocol report §1.3, with a CRC computed from the public
``crc8``. The tests directory has no ``__init__.py`` (design Risk R8); modules import this file by
its unique top-level name.
"""

from __future__ import annotations

from inheco_tec_api.protocol.commands import Command
from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import (
    Reply,
    ReplyAssembler,
    encode_command,
    parse_reply,
)
from inheco_tec_api.transport.fake import FakeTecDevice

READ_TIMEOUT_MS = 100


def raw_command_reports(text: str, *, crc: int | None = None) -> list[bytes]:
    """Frame ``text`` as 9-byte output reports without the allowlist (§1.3 steps 2-5).

    The CRC is CRC-8 over every character except ``#``, with ``0x23``/``0x00`` sent as ``0x77``;
    ``crc`` overrides it. Messages longer than 8 bytes use 7-byte chunks with ``#`` at byte 8.
    """
    message = text.upper().encode("ascii")
    if crc is None:
        crc = crc8(bytes(b for b in message if b != 0x23))
        if crc in (0x23, 0x00):
            crc = 0x77
    buf = message + bytes([crc])
    if len(buf) <= 8:
        return [b"\x00" + buf.ljust(8, b"\x00")]
    chunks = [buf[i : i + 7] for i in range(0, len(buf), 7)]
    reports = [b"\x00" + chunk.ljust(7, b"\x00") + b"#" for chunk in chunks[:-1]]
    reports.append(b"\x00" + chunks[-1].ljust(7, b"\x00") + b"\x00")
    return reports


def write_reports(device: FakeTecDevice, reports: list[bytes]) -> None:
    """Write every report to ``device`` in order."""
    for report in reports:
        device.write_report(report)


def read_texts(device: FakeTecDevice) -> list[str]:
    """Drain the device's input queue into CRC-validated reply texts (real decoder)."""
    texts: list[str] = []
    assembler = ReplyAssembler()
    while True:
        payload = device.read_report(READ_TIMEOUT_MS)
        if payload is None:
            break
        assert len(payload) == 8, "fake must return 8-byte payloads (report ID removed)"
        if assembler.feed(payload):
            texts.append(assembler.finish())
            assembler = ReplyAssembler()
    assert not assembler.complete
    return texts


def read_replies(device: FakeTecDevice) -> list[Reply]:
    """Drain the input queue and parse every reply with the real ``parse_reply``."""
    return [parse_reply(text) for text in read_texts(device)]


def send(device: FakeTecDevice, cmd: Command) -> list[Reply]:
    """Write ``cmd`` through the real ``encode_command`` and return every reply queued."""
    write_reports(device, encode_command(cmd))
    return read_replies(device)


def send_one(device: FakeTecDevice, cmd: Command) -> Reply:
    """Like :func:`send`, but assert exactly one reply came back."""
    replies = send(device, cmd)
    assert len(replies) == 1, replies
    return replies[0]


def send_raw(device: FakeTecDevice, text: str, *, crc: int | None = None) -> list[Reply]:
    """Write raw ``text`` (no allowlist) and return every reply queued."""
    write_reports(device, raw_command_reports(text, crc=crc))
    return read_replies(device)


def opened(device: FakeTecDevice | None = None) -> FakeTecDevice:
    """Return ``device`` (or a default controller-0 fake) after ``open()``."""
    dev = FakeTecDevice() if device is None else device
    dev.open()
    return dev


class TickClock:
    """A deterministic clock that advances by ``step`` on every call."""

    def __init__(self, start: float = 1000.0, step: float = 0.5) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value

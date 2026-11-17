"""Scripting API of the fake transport: command matchers and injected faults (design C6; FR-13.2).

A test scripts a :class:`~.fake.FakeTecDevice` with ``device.script(match=Match(...),
action=..., times=n)``. Each time a complete command is written, the first rule whose
:class:`Match` fits and that has uses left is consumed and its action replaces or decorates the
normal emulated reply:

- :class:`FirmwareCode` — reply with firmware error character ``E`` (protocol report §8.2) and no
  data; the slot state is not changed.
- :class:`NoReply` — the command is swallowed; no input report is queued (the engine times out).
- :class:`BadCrc` — the command executes, but the reply carries a wrong CRC character (§1.4 step 5).
- :class:`StaleReply` — an extra, unsolicited reply ``text`` is queued before the real reply.
- :class:`RdcReply` — an extra ``0rdc0`` reply (a late resync answer, §3) is queued before the real
  reply.
- :class:`DeviceLoss` — the final ``write_report`` of the command raises ``DeviceIOError``; the
  handle stays dead (every later I/O raises) until it is closed and reopened (FR-6.4).
- :class:`OpenFailure` — consumed by the next ``open()`` instead of by a command: that ``open()``
  raises ``DeviceOpenError`` (FR-12.7). Its ``match`` is ignored.

This module holds no I/O; the device in ``fake.py`` interprets the actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS

RDC_STALE_TEXT: Final = "0rdc0"
"""The reply queued by :class:`RdcReply`: a successful ``0RDC1`` answer (protocol report §3)."""


@dataclass(frozen=True, slots=True)
class Match:
    """Selects commands by mnemonic, address and parameter; ``None`` matches anything.

    ``mnemonic`` compares case-insensitively; ``address`` is the address digit as an int;
    ``param`` is the exact parameter string (``""`` for none). ``Match()`` matches every command.
    """

    mnemonic: str | None = None
    address: int | None = None
    param: str | None = None

    def matches(self, address: int, mnemonic: str, param: str) -> bool:
        """Return True when every field that is set equals the command's field."""
        if self.mnemonic is not None and self.mnemonic.upper() != mnemonic.upper():
            return False
        if self.address is not None and self.address != address:
            return False
        return self.param is None or self.param == param


@dataclass(frozen=True, slots=True)
class FirmwareCode:
    """Reply ``<addr><mnemonic><code>`` with no data (protocol report §2.2, §8.2).

    ``code`` must be one character. It need not be in the §8.2 table, so tests can also inject an
    unknown error character (FR-9.3).
    """

    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or len(self.code) != 1 or not self.code.isascii():
            raise ValueError(f"firmware code must be one ASCII character, got {self.code!r}")

    @property
    def known(self) -> bool:
        """Whether ``code`` is a §8.2 firmware error character."""
        return self.code.upper() in FIRMWARE_ERRORS


@dataclass(frozen=True, slots=True)
class NoReply:
    """Queue no reply for the command."""


@dataclass(frozen=True, slots=True)
class BadCrc:
    """Execute the command but corrupt the reply's CRC character.

    The real decoder accepts any CRC character when the computed CRC is ``#``, ``0xFF`` or
    ``0x00`` (protocol report §1.4 step 5), so a reply whose body has such a CRC cannot be made to
    fail; tests pick a command whose reply body does not.
    """

    @staticmethod
    def wrong_crc(valid: int) -> int:
        """A CRC character that differs from ``valid`` and is neither NUL nor ``#``."""
        bad = (valid + 1) & 0xFF
        while bad in (0x00, 0x23):
            bad = (bad + 1) & 0xFF
        return bad


@dataclass(frozen=True, slots=True)
class StaleReply:
    """Queue the reply ``text`` (framed with a valid CRC) before the real reply."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text or not self.text.isascii():
            raise ValueError("stale reply text must be a non-empty ASCII string")


@dataclass(frozen=True, slots=True)
class RdcReply:
    """Queue a ``0rdc0`` reply before the real reply (FR-5.4: read past, no resend)."""


@dataclass(frozen=True, slots=True)
class DeviceLoss:
    """Make the command's final write raise ``DeviceIOError`` and leave the handle dead."""


@dataclass(frozen=True, slots=True)
class OpenFailure:
    """Make the next ``open()`` raise ``DeviceOpenError``."""


Action = FirmwareCode | NoReply | BadCrc | StaleReply | RdcReply | DeviceLoss | OpenFailure
"""Every scripted action the fake understands."""


@dataclass(slots=True)
class ScriptRule:
    """One scripted rule: an action applied to up to ``remaining`` matching events.

    ``remaining`` of ``None`` means the rule never runs out.
    """

    match: Match
    action: Action
    remaining: int | None

    @property
    def exhausted(self) -> bool:
        """True once the rule has no uses left."""
        return self.remaining is not None and self.remaining <= 0

    def consume(self) -> None:
        """Use the rule once."""
        if self.remaining is not None:
            self.remaining -= 1


def make_rule(match: Match, action: Action, times: int | None) -> ScriptRule:
    """Validate the arguments of ``script()`` and build a :class:`ScriptRule`."""
    if not isinstance(match, Match):
        raise TypeError(f"match must be a Match, got {type(match).__name__}")
    if not isinstance(action, Action):
        raise TypeError(f"action must be a scripted action, got {type(action).__name__}")
    if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
        raise ValueError(f"times must be a positive int or None, got {times!r}")
    return ScriptRule(match=match, action=action, remaining=times)

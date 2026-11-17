"""Fake transport: a report-level emulator of one Inheco TEC controller (design C6; FR-13).

:class:`FakeTecDevice` implements :class:`~.base.HidTransport`. It reassembles the 9-byte output
reports the engine writes (protocol report §1.3 step 5), checks the command CRC with the real C3
code (``crc8`` and the framing rules of ``framing.py``), emulates the slot and mainboard state, and
queues replies framed by :func:`encode_reply` (protocol report §1.4 step 3), so the engine's real
``ReplyAssembler`` decodes them (FR-13.1). Faults are injected through the scripting API of
``fake_actions.py`` (FR-13.2); every written command is recorded with a monotonic timestamp
(FR-13.3).

:class:`FakeTransportFactory` keeps one device per ControllerID, so the device state survives
close/reopen and a second ``create_app`` with the same factory. ``is_fake`` is True on both; the
fake is selected only by the manifest ``transport.kind`` or ``INHECO_TEC_TRANSPORT`` (FR-13.4).
This module never imports ``hid`` and never touches a real device.

The emulated firmware behaviour (reply data formats, the reply-side CRC substitution, the answer to
a corrupt command) is the fake's own model of the protocol report, not a hardware fact.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from inheco_tec_api.protocol.crc import crc8
from inheco_tec_api.protocol.framing import (
    CHUNK_SIZE,
    CONTINUATION,
    CRC_SUBSTITUTE,
    REPORT_ID,
    REPORT_SIZE,
    _command_crc,
)
from inheco_tec_api.transport.base import (
    DeviceIOError,
    DeviceNotFound,
    DeviceOpenError,
    HidTransport,
)
from inheco_tec_api.transport.fake_actions import (
    RDC_STALE_TEXT,
    Action,
    BadCrc,
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    OpenFailure,
    RdcReply,
    ScriptRule,
    StaleReply,
    make_rule,
)

PAYLOAD_SIZE: Final = REPORT_SIZE - 1
"""Input-report payload size without the report ID (protocol report §1.4 step 2)."""

MAX_CONTROLLER_ID: Final = 7
"""Highest ControllerID (protocol report §5.2)."""

EMPTY_SLOT_TYPE: Final = 128
"""``RTD`` device type of a slot with nothing in it (protocol report §5.3)."""

HEATER_HEATING: Final = 0
HEATER_COOLING: Final = 1
HEATER_OFF: Final = 2  # RHE values: 0 heating, 1 cooling, 2 off (protocol report §4)

# Firmware error characters the emulator itself produces (protocol report §8.2).
_OK: Final = "0"
_PROTOCOL_VIOLATION: Final = "1"
_UNKNOWN_COMMAND: Final = "4"
_WRONG_PARAMETER: Final = "5"
_SLOT_UNKNOWN: Final = "7"

_MAINBOARD_MNEMONICS: Final = frozenset({"RTD", "RAN", "RFV", "RHV", "RDC", "AEO"})
_STT_MAX: Final = 1999


@dataclass(frozen=True, slots=True)
class WriteRecord:
    """One command written to the fake (FR-13.3).

    ``command_str`` is the decoded request without its CRC, e.g. ``1STT370``; ``reports`` are the
    raw 9-byte output reports that carried it; ``monotonic_s`` is the clock value when the last
    report arrived.
    """

    monotonic_s: float
    command_str: str
    reports: tuple[bytes, ...]


@dataclass(slots=True)
class FakeSlot:
    """Emulated slot device. Temperatures are integer tenths of °C (protocol report §5.1).

    ``ramp_step`` > 0 moves ``actual`` toward ``target`` by that many tenths on every ``RAT`` read
    while control is enabled (a simple model for n8n development); 0 keeps ``actual`` fixed.
    """

    device_type: int = 1
    article_number: str = "7000179"
    rlt: int = 40
    rmt1: int = 700
    target: int = 0
    enabled: bool = False
    actual: int = 250
    ramp_step: int = 0

    @property
    def heater_state(self) -> int:
        """Derived ``RHE``: off when disabled, else heating at or below target, else cooling."""
        if not self.enabled:
            return HEATER_OFF
        return HEATER_HEATING if self.actual <= self.target else HEATER_COOLING

    def read_actual(self) -> int:
        """Ramp ``actual`` one step toward ``target`` (no overshoot) and return it (``RAT``)."""
        if self.enabled and self.ramp_step > 0:
            delta = self.target - self.actual
            step = min(self.ramp_step, abs(delta))
            self.actual += step if delta > 0 else -step
        return self.actual


def _default_firmware() -> dict[int, str]:
    # RFV selectors: 0 bootstrap, 1 application, 2 serial, 3 hardware, 4 copyright (§4).
    names = ["BOOT V1.00", "APP V2.00", "FAKE-SN-0001", "HW V1.0", "(c) FAKE INHECO EMULATOR"]
    return dict(enumerate(names))


def _default_sensor_values() -> dict[int, str]:
    # RHV selectors 0..8; raw strings, format INFERRED (checklist item 6).
    return {sel: str(200 + sel) for sel in range(9)}


@dataclass(slots=True)
class FakeMainboard:
    """Emulated controller mainboard: type, firmware strings, sensor values, emergency-off."""

    controller_type: int = 1
    firmware: dict[int, str] = field(default_factory=_default_firmware)
    sensor_values: dict[int, str] = field(default_factory=_default_sensor_values)
    emergency_off: bool = False


def default_slots() -> dict[int, FakeSlot]:
    """Two CPAC Microplate slots (1 and 2), matching the shipped manifest."""
    return {1: FakeSlot(), 2: FakeSlot()}


def _reply_crc(body: bytes) -> int:
    """CRC character of a reply body.

    CRC-8 over the body (protocol report §1.4 step 5). A CRC of ``0x23`` or ``0x00`` is sent as
    ``0x77``, mirroring the outbound rule of §1.3 step 4: a NUL CRC would be stripped as padding and
    a ``#`` CRC could look like a continuation. The decoder accepts such a reply because a computed
    ``#`` or ``0x00`` passes whatever the CRC character is (§1.4 step 5). INFERRED — see
    api/README.md checklist item 2.
    """
    crc = crc8(body)
    return CRC_SUBSTITUTE if crc in (CONTINUATION, 0x00) else crc


def encode_reply(text: str, *, crc: int | None = None) -> list[bytes]:
    """Frame reply ``text`` as 8-byte input-report payloads, report ID removed (§1.4 step 3).

    ``text`` plus its CRC character fit in one NUL-padded payload when they are at most 8 bytes;
    otherwise every payload but the last carries 7 bytes and ``#`` at index 7, and the last is
    NUL-padded. ``crc`` overrides the CRC character (used by :class:`BadCrc`).
    """
    body = text.encode("ascii")
    buf = body + bytes([_reply_crc(body) if crc is None else crc])
    if len(buf) <= PAYLOAD_SIZE:
        return [buf.ljust(PAYLOAD_SIZE, b"\x00")]
    chunks = [buf[i : i + CHUNK_SIZE] for i in range(0, len(buf), CHUNK_SIZE)]
    payloads = [chunk + bytes([CONTINUATION]) for chunk in chunks[:-1]]
    payloads.append(chunks[-1].ljust(PAYLOAD_SIZE, b"\x00"))
    return payloads


def _format_tenths(value: int) -> str:
    """Reply data for a temperature in tenths, zero-padded to 4 digits (e.g. ``0370``)."""
    return f"{value:04d}"


class FakeTecDevice:
    """A :class:`~.base.HidTransport` that emulates one controller (design C6).

    Not thread-safe; like the real transport it relies on the controller lock of its caller.
    """

    is_fake: bool = True

    def __init__(
        self,
        controller_id: int = 0,
        *,
        slots: Mapping[int, FakeSlot] | None = None,
        mainboard: FakeMainboard | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 <= controller_id <= MAX_CONTROLLER_ID:
            raise ValueError(f"controller_id must be 0-{MAX_CONTROLLER_ID}, got {controller_id}")
        self.controller_id = controller_id
        self.slots: dict[int, FakeSlot] = dict(default_slots() if slots is None else slots)
        self.mainboard = mainboard if mainboard is not None else FakeMainboard()
        # When False, open() raises DeviceNotFound (no device with the VID/PID is attached).
        self.present = True
        self.writes: list[WriteRecord] = []
        self.open_count = 0
        self._clock = clock
        self._is_open = False
        self._dead = False
        self._rules: list[ScriptRule] = []
        self._input: deque[bytes] = deque()
        self._partial: list[bytes] = []

    # -- scripting and inspection -------------------------------------------------------------

    def script(self, *, match: Match | None = None, action: Action, times: int | None = 1) -> None:
        """Add a scripted rule (FR-13.2); ``match=None`` matches every command.

        The rule applies to the next ``times`` matching events; ``times=None`` never runs out.
        Rules are tried in the order they were added.
        """
        self._rules.append(make_rule(Match() if match is None else match, action, times))

    def clear_script(self) -> None:
        """Remove every scripted rule."""
        self._rules.clear()

    @property
    def is_open(self) -> bool:
        """Whether the emulated handle is open (FR-13.3)."""
        return self._is_open

    @property
    def pending_reports(self) -> int:
        """Number of queued input reports not yet read."""
        return len(self._input)

    @property
    def commands(self) -> list[str]:
        """The ``command_str`` of every recorded write, in order."""
        return [record.command_str for record in self.writes]

    # -- HidTransport -----------------------------------------------------------------------

    def open(self) -> None:
        """Open the emulated handle; a scripted :class:`OpenFailure` makes it raise once.

        A second call while open does nothing, like the real transport (FR-4.1).
        """
        if self._is_open:
            return
        if not self.present:
            raise DeviceNotFound(f"no HID device found for controller {self.controller_id}")
        rule = self._take_rule(lambda r: isinstance(r.action, OpenFailure))
        if rule is not None:
            raise DeviceOpenError(f"cannot open HID device for controller {self.controller_id}")
        self._is_open = True
        self._dead = False
        self.open_count += 1

    def close(self) -> None:
        """Close the handle and drop queued input and any partial command. Never raises."""
        self._is_open = False
        self._dead = False
        self._input.clear()
        self._partial.clear()

    def write_report(self, report: bytes) -> None:
        """Accept one 9-byte output report; on the last report of a command, emulate it.

        A report of another length raises ``ValueError`` before any I/O, like the real transport.
        """
        if len(report) != REPORT_SIZE:
            raise ValueError(f"HID output report must be {REPORT_SIZE} bytes, got {len(report)}")
        self._require_io()
        report = bytes(report)
        self._partial.append(report)
        # §1.3 step 5: byte 8 is '#' on every report of a message except the last.
        if report[PAYLOAD_SIZE] == CONTINUATION:
            return
        reports, self._partial = tuple(self._partial), []
        self._handle_message(reports)

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Return the next queued payload, or ``None`` at once when nothing is queued.

        ``timeout_ms`` must be an ``int`` (not ``bool``): the real binding passes it to a C int and
        raises ``TypeError`` on a float, so the fake refuses it too. An empty queue stands for a
        timeout that has fully elapsed; the fake does not sleep.
        """
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
            raise TypeError(f"timeout_ms must be an int, got {type(timeout_ms).__name__}")
        self._require_io()
        return self._input.popleft() if self._input else None

    def flush_input(self) -> int:
        """Discard every queued input report (FR-5.3) and return how many were discarded."""
        self._require_io()
        count = len(self._input)
        self._input.clear()
        return count

    # -- internals ---------------------------------------------------------------------------

    def _require_io(self) -> None:
        if not self._is_open:
            raise DeviceIOError(f"HID device for controller {self.controller_id} is not open")
        if self._dead:
            raise DeviceIOError(f"HID I/O failed for controller {self.controller_id}")

    def _take_rule(self, wanted: Callable[[ScriptRule], bool]) -> ScriptRule | None:
        """Consume and return the first live rule accepted by ``wanted``."""
        for rule in self._rules:
            if not rule.exhausted and wanted(rule):
                rule.consume()
                return rule
        return None

    def _handle_message(self, reports: tuple[bytes, ...]) -> None:
        """Decode one complete command (protocol report §1.3 step 5) and queue its reply."""
        raw = bytearray()
        for report in reports:
            if report[0] != REPORT_ID:
                return  # not a TEC output report; the firmware ignores it (no reply)
            last = report[PAYLOAD_SIZE] != CONTINUATION
            raw += report[1:] if last else report[1 : 1 + CHUNK_SIZE]
        buf = bytes(b for b in raw if b != 0x00)
        message, crc = buf[:-1], buf[-1:]
        if len(message) < 4 or not message.isascii():
            return  # cannot carry address + mnemonic (+ CRC): no reply
        text = message.decode("ascii")
        if not text[0].isdigit():
            return  # no address digit to echo: no reply
        address, mnemonic, param = int(text[0]), text[1:4].upper(), text[4:]
        rule = self._take_rule(
            lambda r: not isinstance(r.action, OpenFailure)
            and r.match.matches(address, mnemonic, param)
        )
        if rule is not None and isinstance(rule.action, DeviceLoss):
            self._dead = True
            raise DeviceIOError(f"HID write failed for controller {self.controller_id}")
        self.writes.append(WriteRecord(self._clock(), text, reports))
        action = rule.action if rule is not None else None
        if isinstance(action, NoReply):
            return
        echo = f"{address}{mnemonic.lower()}"
        if isinstance(action, FirmwareCode):
            self._queue(echo + action.code)
            return
        if isinstance(action, StaleReply):
            self._queue(action.text)
        elif isinstance(action, RdcReply):
            self._queue(RDC_STALE_TEXT)
        # The real C3 rule: CRC over every character except '#', 0x23/0x00 sent as 0x77.
        if bytes([_command_crc(message)]) != crc:
            reply = echo + _PROTOCOL_VIOLATION
        else:
            code, data = self._execute(address, mnemonic, param)
            reply = echo + code + data
        self._queue(reply, bad_crc=isinstance(action, BadCrc))

    def _queue(self, text: str, *, bad_crc: bool = False) -> None:
        crc = BadCrc.wrong_crc(_reply_crc(text.encode("ascii"))) if bad_crc else None
        self._input.extend(encode_reply(text, crc=crc))

    def _execute(self, address: int, mnemonic: str, param: str) -> tuple[str, str]:
        """Emulate one command; return the firmware error character and the reply data."""
        if mnemonic in _MAINBOARD_MNEMONICS:
            if address != 0:
                # <slot>RAN is "Command unknown" on every slot (observed 2026-10-13, item 11).
                if address in self.slots or (mnemonic == "RAN" and 1 <= address <= 6):
                    return _UNKNOWN_COMMAND, ""
                return _SLOT_UNKNOWN, ""
            return self._execute_mainboard(mnemonic, param)
        handler = _SLOT_HANDLERS.get(mnemonic)
        if handler is None:
            return _UNKNOWN_COMMAND, ""
        slot = self.slots.get(address)
        if slot is None:
            return _SLOT_UNKNOWN, ""
        return handler(slot, param)

    def _execute_mainboard(self, mnemonic: str, param: str) -> tuple[str, str]:
        board = self.mainboard
        if mnemonic == "RDC":
            return _OK, ""
        if mnemonic == "AEO":
            # 0AEO "instantly switches off all power outputs of the slots" (FR-17).
            board.emergency_off = True
            for slot in self.slots.values():
                slot.enabled = False
            return _OK, ""
        selector = int(param) if param.isascii() and param.isdigit() and len(param) == 1 else -1
        if mnemonic == "RAN":
            # 0RAN<slot>: article number, slot 1..6 as the parameter; 0RAN / 0RAN0 -> error 5.
            if not 1 <= selector <= 6:
                return _WRONG_PARAMETER, ""
            found = self.slots.get(selector)
            return (_SLOT_UNKNOWN, "") if found is None else (_OK, found.article_number)
        if mnemonic == "RTD":
            if selector == 0:
                return _OK, str(board.controller_type)
            if 1 <= selector <= 6:
                found = self.slots.get(selector)
                return _OK, str(EMPTY_SLOT_TYPE if found is None else found.device_type)
            return _WRONG_PARAMETER, ""
        values = board.firmware if mnemonic == "RFV" else board.sensor_values
        if selector not in values:
            return _WRONG_PARAMETER, ""
        return _OK, values[selector]


def _stt(slot: FakeSlot, param: str) -> tuple[str, str]:
    if not (param.isascii() and param.isdigit()) or int(param) > _STT_MAX:
        return _WRONG_PARAMETER, ""
    slot.target = int(param)
    return _OK, ""


def _ate(slot: FakeSlot, param: str) -> tuple[str, str]:
    if param not in ("0", "1"):
        return _WRONG_PARAMETER, ""
    slot.enabled = param == "1"
    return _OK, ""


def _read(
    params: tuple[str, ...], read: Callable[[FakeSlot], str]
) -> Callable[[FakeSlot, str], tuple[str, str]]:
    """A read handler: parameter must be one of ``params``, reply data is ``read(slot)``."""

    def handler(slot: FakeSlot, param: str) -> tuple[str, str]:
        if param not in params:
            return _WRONG_PARAMETER, ""
        return _OK, read(slot)

    return handler


_SLOT_HANDLERS: Final[dict[str, Callable[[FakeSlot, str], tuple[str, str]]]] = {
    "STT": _stt,
    "ATE": _ate,
    "RAT": _read(("1", "2", "3"), lambda slot: _format_tenths(slot.read_actual())),
    "RTT": _read(("",), lambda slot: _format_tenths(slot.target)),
    "RLT": _read(("",), lambda slot: str(slot.rlt)),
    "RMT": _read(("1",), lambda slot: str(slot.rmt1)),
    # The API sends RHE without a selector; HSL sends RHE<0|1> (§4). All read the same model.
    "RHE": _read(("", "0", "1"), lambda slot: str(slot.heater_state)),
}


class FakeTransportFactory:
    """:class:`~.base.TransportFactory` that hands out one :class:`FakeTecDevice` per controller.

    ``create`` returns the same device object for a ControllerID every time, so slot state, the
    write log and scripted rules survive close/reopen and a second ``create_app`` (FR-13.3).
    Devices not passed in are built on first use with :func:`default_slots`.
    """

    is_fake: bool = True

    def __init__(
        self,
        devices: Mapping[int, FakeTecDevice] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._devices: dict[int, FakeTecDevice] = dict(devices or {})
        for controller_id, device in self._devices.items():
            if device.controller_id != controller_id:
                raise ValueError(
                    f"device for controller {controller_id} has controller_id "
                    f"{device.controller_id}"
                )

    def device(self, controller_id: int) -> FakeTecDevice:
        """Return the device of ``controller_id``, creating a default one on first use."""
        device = self._devices.get(controller_id)
        if device is None:
            device = FakeTecDevice(controller_id, clock=self._clock)
            self._devices[controller_id] = device
        return device

    def create(self, controller_id: int) -> HidTransport:
        """Return the (unopened or still open) device of ``controller_id``."""
        return self.device(controller_id)

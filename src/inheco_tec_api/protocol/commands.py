"""Command allowlist of the Inheco TEC wire protocol (design C4; FR-10, FR-10.1, FR-10.3, FR-5.9).

A :class:`Command` is the parsed form of a request string ``<addr><MNEMONIC><param>`` (protocol
report §2.1): one address digit, three upper-case letters, and a decimal parameter or nothing. It is
built only through the typed constructors below; :func:`validate` re-checks every field against the
C4 allowlist table and raises :class:`CommandNotAllowed` for anything else. ``encode_command``
(``framing.py``) calls :func:`validate` first, so a disallowed command raises before any byte exists
and therefore before any HID write (FR-10.1).

Every mnemonic outside the table is refused, including the key-protected and development commands
(``SMT``, ``SLT``, ``SDT``, ``SAP``, ``SPO``, ``SDP``, ``SPP``, ``SPI``, ``SPD``) and the shaker
commands (``ASE``, ``SSR``, ``SSS``, ``RSR``, ``RSS``, ``RSE``) of FR-10.3. No function here
accepts a free-form command string (FR-10.2).

Addressing (protocol report §2.1): slot-device commands use the slot number 1–6 as the address.
Controller-level (mainboard) commands ``RTD``, ``RAN``, ``RFV``, ``RHV``, ``RDC`` and ``AEO`` always
use address ``0``, never the ControllerID digit; the controller itself is selected only by the USB
PID (FR-5.9). ``RAN`` and ``RTD`` take the slot as their parameter (``0RAN<slot>``,
``0RTD<slot>``); ``RAN`` was observed in this form on hardware on 2026-10-13 (checklist item 11).
Address ``0`` = mainboard is INFERRED — see api/README.md checklist item 3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

MAINBOARD_ADDRESS: Final = 0
"""Address of controller-level commands (protocol report §2.1; FR-5.9).

INFERRED — see api/README.md checklist item 3.
"""

SLOT_MIN: Final = 1
SLOT_MAX: Final = 6
"""Slot (DeviceID) range of slot-device commands (protocol report §5.2, HSL:169-170)."""

STT_TENTHS_MAX: Final = 1999
"""Largest ``STT`` parameter: 199.9 °C in tenths (protocol report §4/§5.2, HSL:167-168).

This is the wire-level bound only; the effective per-slot window (C8) is narrower.
"""


class CommandNotAllowed(ValueError):
    """A command is not on the C4 allowlist, or a field breaks its rule (FR-10.1, FR-10.3)."""


@dataclass(frozen=True, slots=True)
class Command:
    """One allowlisted firmware command ``<address><mnemonic><param>`` (protocol report §2.1).

    Build instances through the typed constructors (:func:`stt`, :func:`ate`, ...). Direct
    construction is possible in Python, but :func:`validate` (run by ``encode_command``) rejects
    any instance that does not match the C4 table.
    """

    address: int
    mnemonic: str
    param: str

    @property
    def text(self) -> str:
        """The request string as sent before framing, e.g. ``1STT370`` (protocol report §2.1)."""
        return f"{self.address}{self.mnemonic}{self.param}"


def _no_param(param: str) -> bool:
    return param == ""


def _one_of(*allowed: str) -> Callable[[str], bool]:
    values = frozenset(allowed)

    def check(param: str) -> bool:
        return param in values

    return check


def _digit_range(low: int, high: int) -> Callable[[str], bool]:
    return _one_of(*(str(value) for value in range(low, high + 1)))


def _stt_param(param: str) -> bool:
    # Canonical decimal only: ASCII digits, no sign, no leading zero (so "0370" is refused).
    if not (param.isascii() and param.isdigit()):
        return False
    if len(param) > 1 and param[0] == "0":
        return False
    return 0 <= int(param) <= STT_TENTHS_MAX


_SLOT_ADDRESSES: Final = frozenset(range(SLOT_MIN, SLOT_MAX + 1))
_MAINBOARD_ONLY: Final = frozenset({MAINBOARD_ADDRESS})

# The C4 table: mnemonic -> (allowed addresses, parameter rule).
# Parameter ranges cite protocol report §4 (HSL input checks) unless marked otherwise.
_RULES: Final[dict[str, tuple[frozenset[int], Callable[[str], bool]]]] = {
    # SetTargetTemperature: integer tenths 0..1999 (§4, HSL:1134; §5.1).
    "STT": (_SLOT_ADDRESSES, _stt_param),
    # Start/StopTemperatureControl: ATE1 / ATE0 (§4, HSL:1272, 1314).
    "ATE": (_SLOT_ADDRESSES, _one_of("0", "1")),
    # GetActualDeviceTemperature selector 1..3 (§4, HSL:961; §5.2). Selector semantics
    # INFERRED — see api/README.md checklist item 8.
    "RAT": (_SLOT_ADDRESSES, _digit_range(1, 3)),
    # GetTargetTemperature (§4, HSL:1084).
    "RTT": (_SLOT_ADDRESSES, _no_param),
    # GetDeviceTemperatureLimits, minimum (§4, HSL:1024).
    "RLT": (_SLOT_ADDRESSES, _no_param),
    # Article number read: not in the HSL library (design C4, FR-7.7). Mainboard command with the
    # slot 1..6 as the parameter, 0RAN<slot>; observed on hardware 2026-10-13 (api/README.md
    # checklist item 11): <slot>RAN -> error 4, 0RAN / 0RAN0 -> error 5.
    "RAN": (_MAINBOARD_ONLY, _digit_range(SLOT_MIN, SLOT_MAX)),
    # Heater enable status without selector (design C4, C8; §4 lists RHE<sel>). Meaning of the
    # no-selector form INFERRED — see api/README.md checklist item 13.
    "RHE": (_SLOT_ADDRESSES, _no_param),
    # GetDeviceTemperatureLimits, maximum: RMT1 only (§4, HSL:1035).
    "RMT": (_SLOT_ADDRESSES, _one_of("1")),
    # GetSensorValues, the corrected mnemonic RHV (§4 note: HSL sends RFV by mistake). Selector
    # 0..8; selector 9 ("to be erased with 1") is refused. INFERRED — see api/README.md checklist
    # items 3 and 6.
    "RHV": (_MAINBOARD_ONLY, _digit_range(0, 8)),
    # GetDeviceType: parameter = slot 0..6, 0 = controller type (§4, HSL:853, 864; §5.3).
    "RTD": (_MAINBOARD_ONLY, _digit_range(0, 6)),
    # GetFirmwareVersion selector 0..4 (§4, HSL:658, 675).
    "RFV": (_MAINBOARD_ONLY, _digit_range(0, 4)),
    # Resync dummy "0RDC1" only (§3, HSL:2017). Meaning INFERRED — see api/README.md checklist
    # item 4.
    "RDC": (_MAINBOARD_ONLY, _one_of("1")),
    # Emergency off: address 0, no parameter (FR-10.1; design C4). Reply and behaviour INFERRED —
    # see api/README.md checklist item 12.
    "AEO": (_MAINBOARD_ONLY, _no_param),
}

ALLOWED_MNEMONICS: Final[frozenset[str]] = frozenset(_RULES)
"""The FR-10.1 mnemonic allowlist.

STT, ATE, RAT, RTT, RLT, RMT, RHV, RTD, RFV, RDC, RAN, RHE and AEO.
"""


def validate(cmd: Command) -> None:
    """Raise :class:`CommandNotAllowed` unless ``cmd`` matches the C4 allowlist table (FR-10.1).

    Checks, in order: field types, mnemonic on the allowlist (FR-10.3 mnemonics are refused here),
    address allowed for the mnemonic (controller-level commands only with address ``0``, FR-5.9),
    and the parameter rule of the mnemonic.
    """
    if not isinstance(cmd, Command):
        raise CommandNotAllowed(f"not a Command: {type(cmd).__name__}")
    address, mnemonic, param = cmd.address, cmd.mnemonic, cmd.param
    if isinstance(address, bool) or not isinstance(address, int):
        raise CommandNotAllowed("command address must be an int")
    if not isinstance(mnemonic, str) or not isinstance(param, str):
        raise CommandNotAllowed("command mnemonic and parameter must be str")
    rule = _RULES.get(mnemonic)
    if rule is None:
        raise CommandNotAllowed(f"mnemonic not allowed: {mnemonic!r}")
    addresses, param_ok = rule
    if address not in addresses:
        raise CommandNotAllowed(f"address {address} not allowed for {mnemonic}")
    if not param_ok(param):
        raise CommandNotAllowed(f"parameter {param!r} not allowed for {mnemonic}")


def _int_arg(name: str, value: int) -> int:
    # bool is an int subclass; refuse it so ``stt(True, 370)`` cannot address slot 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandNotAllowed(f"{name} must be an int")
    return value


def _build(address: int, mnemonic: str, param: str) -> Command:
    cmd = Command(address, mnemonic, param)
    validate(cmd)
    return cmd


def stt(slot: int, tenths: int) -> Command:
    """``<slot>STT<tenths>``: set the target temperature in integer tenths of °C (§4, §5.1)."""
    return _build(_int_arg("slot", slot), "STT", str(_int_arg("tenths", tenths)))


def ate(slot: int, on: bool) -> Command:
    """``<slot>ATE1`` / ``<slot>ATE0``: start or stop temperature control (§4)."""
    if not isinstance(on, bool):
        raise CommandNotAllowed("on must be a bool")
    return _build(_int_arg("slot", slot), "ATE", "1" if on else "0")


def rat(slot: int, sel: int) -> Command:
    """``<slot>RAT<sel>``: read the actual temperature, selector 1–3 (§4, §5.2)."""
    return _build(_int_arg("slot", slot), "RAT", str(_int_arg("sel", sel)))


def rtt(slot: int) -> Command:
    """``<slot>RTT``: read the target temperature (§4)."""
    return _build(_int_arg("slot", slot), "RTT", "")


def rlt(slot: int) -> Command:
    """``<slot>RLT``: read the firmware minimum temperature (§4)."""
    return _build(_int_arg("slot", slot), "RLT", "")


def ran(slot: int) -> Command:
    """``0RAN<slot>``: read the article number of a slot, mainboard address 0, slot 1–6 as the
    parameter (design C4, FR-7.7; observed on hardware 2026-10-13, checklist item 11).
    """
    number = _int_arg("slot", slot)
    if not SLOT_MIN <= number <= SLOT_MAX:
        raise CommandNotAllowed(f"slot {number} not allowed for RAN")
    return _build(MAINBOARD_ADDRESS, "RAN", str(number))


def rhe(slot: int) -> Command:
    """``<slot>RHE``: read the heater enable state (design C8; INFERRED — checklist item 13)."""
    return _build(_int_arg("slot", slot), "RHE", "")


def rmt1(slot: int) -> Command:
    """``<slot>RMT1``: read the firmware maximum temperature (§4)."""
    return _build(_int_arg("slot", slot), "RMT", "1")


def rhv(sel: int) -> Command:
    """``0RHV<sel>``: read a mainboard sensor value, selector 0–8 (§4 note; FR-5.9)."""
    return _build(MAINBOARD_ADDRESS, "RHV", str(_int_arg("sel", sel)))


def rtd(slot: int) -> Command:
    """``0RTD<slot>``: read the device type of a slot, or the controller type for 0 (§4, §5.3)."""
    return _build(MAINBOARD_ADDRESS, "RTD", str(_int_arg("slot", slot)))


def rfv(sel: int) -> Command:
    """``0RFV<sel>``: read a firmware version string, selector 0–4 (§4; FR-5.9)."""
    return _build(MAINBOARD_ADDRESS, "RFV", str(_int_arg("sel", sel)))


def rdc1() -> Command:
    """``0RDC1``: the resync dummy command (§3, HSL:2017; INFERRED — checklist item 4)."""
    return _build(MAINBOARD_ADDRESS, "RDC", "1")


def aeo() -> Command:
    """``0AEO``: emergency off, address 0, no parameter (FR-10.1; INFERRED — checklist item 12)."""
    return _build(MAINBOARD_ADDRESS, "AEO", "")

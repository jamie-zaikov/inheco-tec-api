"""Firmware lookup tables of the Inheco TEC protocol (design C3).

- :data:`FIRMWARE_ERRORS` and :data:`TRANSIENT_CODES`: protocol report §8.2 (FR-5.6, FR-9.1,
  FR-9.3).
- :data:`CODE_CPAC_VOLTAGE_NOT_OK`: protocol report §8.2 code ``I`` (FR-17.10, FR-17.11).
- :data:`DEVICE_TYPES` and :data:`CONTROLLER_TYPES`: protocol report §5.3 (FR-7.1, FR-8.11).
"""

from __future__ import annotations

from typing import Final

FIRMWARE_ERRORS: Final[dict[str, str]] = {
    "0": "Normal return, command successful",
    "1": "External message protocol violation",
    "2": "Internal message protocol violation",
    "3": "Command not executable",
    "4": "Command unknown",
    "5": "Wrong parameter",
    "6": "Reset detected",
    "7": "Slot Id unknown",
    "8": "Wrong keyword",
    "9": "Timeout from slot-module",
    "A": "Busy with an action command or startup",
    "B": "Reserved",
    "C": "Housing temperature not OK",
    "D": "Response time too long",
    "E": "Voltage power supply not OK",
    "F": "Housing fan not OK",
    "G": "Device temp not OK",
    "H": "RPM too high",
    "I": "CPAC voltage not OK",
    "J": "Shaker is currently busy",
    "K": "TEC current too low",
    "L": "Internal shaker communication is down",
    "M": "Shaker does not work properly",
    "N": "Shaker bus is currently busy",
    "O": "Shaker bus blocked due to a serious error",
    "R": "Cable break or short circuit PT100",
    "T": "Delta T too high",
    "W": "Wrong device connected",
}
"""Firmware error character ``E`` (reply index 4) → description (protocol report §8.2).

The table is the full §8.2 set: ``0``–``9``, ``A``–``O``, ``R``, ``T``, ``W``. ``0`` is success. A
character outside this table is not a firmware code: the caller fails the transaction closed
(FR-9.3). Unlike HSL v2.2, no non-``0`` code is ever treated as success (FR-9).
"""

TRANSIENT_CODES: Final[frozenset[str]] = frozenset({"1", "2", "6", "9", "A", "D"})
"""Firmware codes that are retried (FR-5.6; protocol report §8.2 recommendation).

§8.2 recommends retrying ``1``, ``2``, ``9``, ``A``, ``D`` and "maybe ``6``"; FR-5.6 fixes the set
with ``6`` (reset detected) included. Every other non-``0`` code fails fast (FR-5.7).
"""

# Protocol report §8.2 (FR-17.10 / FR-17.11, design C3 / P10): only for the C12 power-cycle message.
CODE_CPAC_VOLTAGE_NOT_OK: Final = "I"

DEVICE_TYPES: Final[dict[int, str]] = {
    0: "Thermoshake",
    1: "CPAC",
    2: "Teleshake",
    3: "CPLC",
    4: "CPAC 2TEC",
    5: "Heat PAC",
    6: "Cycler",
    7: "Heated Lid",
    8: "ACAC",
    9: "LCAC",
    10: "CPHF",
    12: "Thermoshake AC",
    13: "Teleshake AC",
    14: "Teleshake 95 AC",
    15: "CPLC2",
    64: "No device connected",
    128: "Empty slot",
}
"""Slot device type, the reply of ``0RTD<slot>`` for slot 1–6 (protocol report §5.3).

This is the HSL v2.2 / firmware-manual enum (HSL:77-96), not the off-by-one mapping of
CHM-DE ``GetDeviceType.html`` or of the ``InhecoTECController.hsi`` wrapper (FR-7.1). Code 4 is the
CPAC 2TEC (Ultraflat 2TEC). §5.3 lists no code 11, so none is defined here; an unlisted code has no
name. Trusting the HSL enum over CHM-DE is INFERRED — see api/README.md checklist item 9.
"""

CONTROLLER_TYPES: Final[dict[int, str]] = {0: "STC", 1: "MTC", 255: "not set"}
"""Controller type, the reply of ``0RTD0`` (protocol report §5.3; FR-8.11).

0 = STC (Single TEC), 1 = MTC (Multi TEC, up to 6 slots) (HSL:98-102, 874-878); 255 = not set
(FR-8.11).
"""

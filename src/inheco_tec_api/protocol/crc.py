"""CRC-8 of the Inheco TEC wire protocol (design C3; FR-4.3).

Ported from protocol report §1.3 ("CRC-8 routine", ``GlobCom.crc8`` IL_0000..IL_0061): the
Dallas/Maxim 1-Wire CRC-8 (reflected polynomial 0x8C), processed least-significant bit first, with
the vendor's initial value 0xA1 (§1.3 step 3, IL_002d).

The reconstruction is INFERRED — see api/README.md checklist item 2 (the ``0RFV1`` reply CRC must
validate and the outbound golden vectors must be accepted on hardware).

Which characters enter the CRC (every command character except ``#``) and the ``0x77``
substitution for a CRC of ``0x23`` or ``0x00`` (§1.3 step 4) are framing rules; they live in
``framing.py``. This module computes the plain checksum only.
"""

from __future__ import annotations

CRC_INIT = 0xA1
"""Initial CRC value used by the vendor DLL (protocol report §1.3 step 3, IL_002d)."""


def _crc8_step(byte: int, crc: int) -> int:
    """Fold one byte into ``crc``: the verbatim §1.3 bit loop ``(crc ^ 0x18) >> 1 | 0x80``."""
    data = byte
    for _ in range(8):
        if (data ^ crc) & 1:
            crc = ((crc ^ 0x18) >> 1) | 0x80
        else:
            crc >>= 1
        data >>= 1
    return crc & 0xFF


def crc8(data: bytes, init: int = CRC_INIT) -> int:
    """Return the CRC-8 of ``data`` (protocol report §1.3, Dallas/Maxim reflected 0x8C).

    Each byte is folded in order through the §1.3 bit loop, starting from ``init`` (0xA1 by
    default). The result is an integer in 0..255. INFERRED — see api/README.md checklist item 2.
    """
    if not 0 <= init <= 0xFF:
        raise ValueError(f"CRC init must be 0..255, got {init}")
    crc = init
    for byte in data:
        crc = _crc8_step(byte, crc)
    return crc

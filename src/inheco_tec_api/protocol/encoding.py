"""Temperature encoding in integer tenths of a degree Celsius (design C3; FR-5.10, FR-9.3).

Protocol report §5.1: temperatures travel on the wire as integer tenths of °C; the HSL library
multiplies by 10 on set (HSL:1134) and by 0.1 on get (HSL:983, 1033, 1044, 1093).

Encoding uses round-half-away-from-zero of ``t × 10`` on the decimal form of the float (not
``floor``, and not binary-float rounding), so ``36.6`` encodes to ``366`` (FR-5.10). Decoding is
``int(data) / 10`` and is signed, because the firmware minimum from ``RLT`` may be negative
(FR-7.2). Data that is not a plain signed decimal integer raises :class:`DataParseError`, so the
caller fails the transaction closed (FR-9.3).
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal

# Plain signed ASCII decimal integer. ``int()`` alone would also accept surrounding whitespace,
# ``_`` digit separators and non-ASCII digits; none of those is valid reply data (FR-9.3).
_TENTHS_PATTERN = re.compile(r"[+-]?[0-9]+")


class DataParseError(ValueError):
    """A reply data field cannot be parsed for the operation (FR-9.3, fail closed)."""


def encode_tenths(t: float) -> int:
    """Encode ``t`` °C as integer tenths, rounding half away from zero (protocol report §5.1).

    ``Decimal(repr(t)) * 10`` is quantised to an integer with ``ROUND_HALF_UP`` (which in
    :mod:`decimal` rounds half away from zero): ``36.6 → 366``, ``0.05 → 1``, ``-0.05 → -1``
    (FR-5.10). ``repr`` gives the shortest decimal that round-trips the float, so the result does
    not depend on binary representation error. A non-finite ``t`` raises ``ValueError``; range
    checks against the slot limits belong to the caller.
    """
    if not math.isfinite(t):
        raise ValueError(f"temperature must be finite, got {t!r}")
    tenths = (Decimal(repr(float(t))) * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(tenths)


def decode_tenths(data: str) -> float:
    """Decode a reply data field of integer tenths to °C: ``int(data) / 10`` (protocol report §5.1).

    The value is signed (``RLT`` may report a negative minimum, FR-7.2). Anything other than an
    optional sign followed by ASCII digits raises :class:`DataParseError` (FR-9.3).
    """
    if not _TENTHS_PATTERN.fullmatch(data):
        raise DataParseError(f"reply data is not an integer number of tenths: {data!r}")
    return int(data) / 10

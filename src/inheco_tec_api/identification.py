"""Slot identification and startup slot-state reads (design C8; FR-6.7, FR-6.8, FR-7).

:func:`identify_slot` reads the device type, article number and firmware limits of one configured
slot and turns them into its effective limits with :func:`~.limits.compute_limits` (FR-7,
FR-7.1, FR-7.2, FR-7.7). :func:`read_startup_state` reads the control state, target and actual
temperature that a slot had when the service connected (FR-6.8).

Both functions only read: neither sends ``STT`` nor ``ATE`` (FR-6.7), so a temperature control
that was active before a restart continues. Every read is one full transaction of the
:class:`~.engine.TransactionEngine` (design C7), including its ``0RDC1`` resync; this module never
touches the transport itself. The caller (C9) holds the controller lock around each call
(FR-5.1).

A read that fails as a transaction (:class:`~.engine.TransactionFailed`) or whose reply data cannot
be parsed (:class:`~.protocol.encoding.DataParseError`, FR-9.3) is recorded as *unread*
(``None``); it is never raised and never replaced by a default value. An unread identification
value makes the slot not permitted for set target and start (FR-7.5); an unread startup value is
reported as ``unknown`` / ``None``. Because only reads are sent, an unread value never hides a
state change on the device (carry-forward T9-C1, T9-C6). :class:`~.engine.DeviceLost` propagates
to the caller, which owns the loss handling (FR-6.4).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, TypeVar

from inheco_tec_api.engine import TransactionEngine, TransactionFailed
from inheco_tec_api.limits import SlotLimits, compute_limits
from inheco_tec_api.logging_setup import get_logger
from inheco_tec_api.manifest import ArticleEntry, SlotConfig
from inheco_tec_api.protocol import commands
from inheco_tec_api.protocol.commands import Command
from inheco_tec_api.protocol.encoding import DataParseError, decode_tenths
from inheco_tec_api.protocol.tables import DEVICE_TYPES

__all__ = [
    "REASON_LIMITS_ERROR",
    "ControlState",
    "SlotIdentification",
    "StartupSlotState",
    "identify_slot",
    "read_startup_state",
]

ControlState = Literal["heating", "cooling", "off", "unknown"]

_T = TypeVar("_T")

RAT_COMPENSATED: Final = 3
"""``RAT`` selector 3: the fully compensated actual temperature (protocol report §4, HSL:137-142).

Selector semantics INFERRED — see api/README.md checklist item 8.
"""

_HEATER_STATES: Final[dict[int, ControlState]] = {0: "heating", 1: "cooling", 2: "off"}
"""``RHE`` reply value → control state: 0 heating, 1 cooling, 2 off (protocol report §4,
HSL:156-161). The API sends ``RHE`` without a selector; whether that value reflects the ``ATE``
enable state is INFERRED — see api/README.md checklist item 13."""

REASON_LIMITS_ERROR: Final = "limits_computation_failed"
"""Not-permitted reason used when :func:`compute_limits` raises (carry-forward T6-S3).

``compute_limits`` is a pure function that is not expected to raise; if it does, the slot fails
closed instead of the identification crashing or the slot being permitted."""

EVENT_TEC_IDENTIFICATION_ERROR: Final = "tec_identification_error"
"""Error event when :func:`compute_limits` raises (T6-S3). It is not one of the C14 events in
``logging_setup.EVENT_NAMES``; it carries ``controller_id``, ``slot`` and ``error`` (the exception
class name only, never a value)."""

# Plain unsigned / signed ASCII decimal integers. ``int()`` alone also accepts whitespace, ``_``
# separators and non-ASCII digits; none of those is valid reply data (FR-9.3).
_UNSIGNED_PATTERN: Final = re.compile(r"[0-9]+")
_SIGNED_PATTERN: Final = re.compile(r"[+-]?[0-9]+")

_log = get_logger(__name__)


@dataclass(frozen=True)
class SlotIdentification:
    """Result of :func:`identify_slot` for one slot (design C8; FR-6.6, FR-7.7).

    ``limits`` holds the effective window or the fail-closed reason (FR-7.5). ``device_type`` is
    the ``RTD`` code (``None`` if unread) and ``device_type_name`` its name in the HSL v2.2 enum
    (protocol report §5.3; ``None`` if unread or not in the enum). ``article`` is the raw ``RAN``
    reply data (``None`` if unread) and ``variant`` the variant name from the article-number map
    (``None`` if the article was not mapped).
    """

    slot: int
    limits: SlotLimits
    device_type: int | None
    device_type_name: str | None
    article: str | None
    variant: str | None


@dataclass(frozen=True)
class StartupSlotState:
    """Slot state found when the service connected (FR-6.8; design C8).

    ``control_state`` comes from ``RHE`` (``unknown`` if unread or not 0/1/2); ``target_c`` from
    ``RTT`` and ``actual_c`` from ``RAT3``, in °C, each ``None`` if unread.
    """

    control_state: ControlState
    target_c: float | None
    actual_c: float | None


def _parse_unsigned(data: str) -> int:
    """Parse reply data as an unsigned decimal integer, or raise :class:`DataParseError`."""
    if not _UNSIGNED_PATTERN.fullmatch(data):
        raise DataParseError(f"reply data is not an unsigned integer: {data!r}")
    return int(data)


def _parse_signed_tenths(data: str) -> int:
    """Parse reply data as signed integer tenths of °C (protocol report §5.1; FR-7.2).

    ``RLT`` may be negative, so a sign is accepted. The value stays in integer tenths, the unit
    :func:`compute_limits` works in.
    """
    if not _SIGNED_PATTERN.fullmatch(data):
        raise DataParseError(f"reply data is not an integer number of tenths: {data!r}")
    return int(data)


def _raw(data: str) -> str:
    """Return reply data unchanged.

    Used for ``RAN``: the raw data goes to :func:`compute_limits`, which requires exactly 7 ASCII
    decimal digits (FR-7.7, FR-7.8). No trimming or padding is applied (design C8 specifies none),
    so a reply with padding or extra characters fails closed as ``article_malformed``. The reply
    format of ``RAN`` is observed: ``0ran0`` followed by the 7-digit article (2026-10-13,
    api/README.md checklist item 11).
    """
    return data


def _read(engine: TransactionEngine, cmd: Command, parse: Callable[[str], _T]) -> _T | None:
    """Run ``cmd`` as one transaction and parse its data; ``None`` when the value is unread.

    A :class:`TransactionFailed` or a :class:`DataParseError` makes the value unread (design C8).
    :class:`~.engine.DeviceLost` and every other exception propagate.
    """
    try:
        reply = engine.execute(cmd)
        return parse(reply.data)
    except (TransactionFailed, DataParseError):
        return None


def identify_slot(
    engine: TransactionEngine,
    slot_cfg: SlotConfig,
    article_map: Mapping[str, ArticleEntry],
) -> SlotIdentification:
    """Identify one configured slot and compute its effective limits (design C8; FR-7).

    Sends four separate transactions, in this order:

    - ``0RTD<slot>``: device type, controller-level command at address ``0`` (FR-7.1, FR-5.9;
      protocol report §4, §5.3). Address ``0`` = mainboard is INFERRED — see api/README.md
      checklist item 3. The code is named with the HSL v2.2 enum (``DEVICE_TYPES``), not the
      off-by-one ``InhecoTECController.hsi`` mapping; trusting that enum is INFERRED — see
      api/README.md checklist item 9.
    - ``0RAN<slot>``: article number, mainboard address ``0`` with the slot as the parameter
      (FR-7.7; observed 2026-10-13, api/README.md checklist item 11).
    - ``<slot>RLT``: firmware minimum in signed tenths of °C (FR-7.2; protocol report §4, §5.1).
    - ``<slot>RMT1``: firmware maximum in tenths of °C (FR-7.2; protocol report §4, §5.1).

    A failed or unparseable read is passed to :func:`compute_limits` as unread (``None``), which
    fails the slot closed (FR-7.5, FR-7.8). Any exception raised by :func:`compute_limits` itself
    also gives a not-permitted slot (reason :data:`REASON_LIMITS_ERROR`; carry-forward T6-S3).
    :class:`~.engine.DeviceLost` propagates. No ``STT`` or ``ATE`` is sent (FR-6.7).
    """
    slot = slot_cfg.id
    rtd = _read(engine, commands.rtd(slot), _parse_unsigned)
    ran = _read(engine, commands.ran(slot), _raw)
    rlt = _read(engine, commands.rlt(slot), _parse_signed_tenths)
    rmt1 = _read(engine, commands.rmt1(slot), _parse_signed_tenths)

    try:
        limits = compute_limits(slot_cfg, article_map, rtd, ran, rlt, rmt1)
    except Exception as exc:  # noqa: BLE001 — T6-S3: any failure here must fail closed
        _log.error(
            EVENT_TEC_IDENTIFICATION_ERROR,
            controller_id=engine.controller_id,
            slot=slot,
            error=type(exc).__name__,
        )
        limits = SlotLimits(
            permitted=False,
            min_c=None,
            max_c=None,
            limit_class=None,
            variant=None,
            reason=REASON_LIMITS_ERROR,
        )

    return SlotIdentification(
        slot=slot,
        limits=limits,
        device_type=rtd,
        device_type_name=DEVICE_TYPES.get(rtd) if rtd is not None else None,
        article=ran,
        variant=limits.variant,
    )


def _control_state(value: int | None) -> ControlState:
    if value is None:
        return "unknown"
    return _HEATER_STATES.get(value, "unknown")


def read_startup_state(engine: TransactionEngine, slot: int) -> StartupSlotState:
    """Read the state a slot had when the service connected (FR-6.8; design C8).

    Sends three separate transactions: ``<slot>RHE`` (control state 0 heating, 1 cooling, 2 off;
    protocol report §4, HSL:156-161; meaning of the no-selector form INFERRED — see api/README.md
    checklist item 13), ``<slot>RTT`` (target) and ``<slot>RAT3`` (fully compensated actual
    temperature; selector semantics INFERRED — see api/README.md checklist item 8), both decoded
    as tenths of °C (protocol report §5.1). An unread value is ``unknown`` / ``None``, never
    ``off``. :class:`~.engine.DeviceLost` propagates. No ``STT`` or ``ATE`` is sent (FR-6.7).
    """
    heater = _read(engine, commands.rhe(slot), _parse_unsigned)
    target_c = _read(engine, commands.rtt(slot), decode_tenths)
    actual_c = _read(engine, commands.rat(slot, RAT_COMPENSATED), decode_tenths)
    return StartupSlotState(
        control_state=_control_state(heater),
        target_c=target_c,
        actual_c=actual_c,
    )

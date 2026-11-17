"""Read operations of the domain service and the request checks they share (design C11, C9).

:class:`ReadOperations` is the base of :class:`~.operations.TecService`. It holds the shared
request checks (C9 gating step 1, the IDs; step 6, the value checks) and the locked device access,
and it implements the six read operations of the C11 table (tasks.md 15.5): ``actual``,
``target``, ``at_target``, ``sensor_values``, ``device_type`` and ``firmware_version``.

Every device operation runs in this order (C9 gating, carry-forward T12-E / T12-S1 / T14-S4):

1. The IDs: a bool, a non-int or an ID outside 0–7 (controller) or 1–6 (slot) →
   :class:`InvalidRequest` (HTTP 422); an ID that is not configured → :class:`NotConfigured`
   (HTTP 404) (FR-6.5). ``device_type`` checks its slot argument for the range 0–6 only (P3).
2. ``registry.device_manager(cid)`` (``ServiceStopping`` while the service stops).
3. ``manager.locked()`` is held around the gate, the value checks and every transaction, so a
   state change between the gate and the command cannot happen (T12-E, T12-S1). Inside the lock
   ``registry.check_accepting()`` runs again, so a request that passed step 2 before a shutdown
   started never reaches the device after shutdown handled the controller (T14-S4).
4. ``manager.gate(operation, slot)`` (C9 steps 2–5).
5. The value checks (selectors, tolerance) → :class:`InvalidRequest` (C9 step 6).
6. The transaction(s) through ``manager.run`` (no ``max_attempts`` is ever passed, T9-S2).

Reply data that cannot be parsed for the operation fails closed as :class:`ReplyDataInvalid`, a
``TransactionFailed`` of kind ``parse`` (FR-9.3). Raw device data (sensor values, firmware strings)
is returned only as structured fields and is never logged (carry-forward T10-S2).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, TypeGuard

from inheco_tec_api.controller import ControllerManager
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.identification import RAT_COMPENSATED
from inheco_tec_api.protocol import commands
from inheco_tec_api.protocol.encoding import DataParseError, decode_tenths
from inheco_tec_api.protocol.framing import Reply
from inheco_tec_api.protocol.tables import CONTROLLER_TYPES, DEVICE_TYPES
from inheco_tec_api.registry import ControllerRegistry, EffectiveLimits

__all__ = [
    "CONTROLLER_ID_MAX",
    "CONTROLLER_ID_MIN",
    "FIRMWARE_VERSION_NAMES",
    "ActualTemperature",
    "AtTargetResult",
    "DeviceTypeResult",
    "FirmwareVersion",
    "InvalidRequest",
    "NotConfigured",
    "ReadOperations",
    "ReplyDataInvalid",
    "SensorValues",
    "TargetTemperature",
]

CONTROLLER_ID_MIN: Final = 0
CONTROLLER_ID_MAX: Final = 7
"""ControllerID range (FR-6.5); the controller is selected by the USB PID (FR-5.9)."""

RAT_SELECTORS: Final = range(1, 4)
"""``RAT`` selectors 1–3, default 3 (FR-8.7; protocol report §4)."""

RHV_SELECTORS: Final = range(0, 9)
"""``RHV`` selectors 0–8; 9 is an erase action and is refused (FR-8.10)."""

RTD_SLOTS: Final = range(0, 7)
"""``RTD`` slot argument 0–6; 0 = controller type (FR-8.11). Range check only, no configured
check (design P3)."""

FIRMWARE_SELECTORS: Final = range(0, 5)
"""``RFV`` selectors 0–4 (FR-8.12)."""

FIRMWARE_VERSION_NAMES: Final[dict[int, str]] = {
    0: "bootstrap",
    1: "application",
    2: "serial",
    3: "hardware",
    4: "copyright",
}
"""``RFV`` selector 0–4 → name (FR-8.12; protocol report §4, HSL:658, 675)."""

TOLERANCE_MIN_C: Final = 0.1
TOLERANCE_MAX_C: Final = 5.0
"""Allowed at-target tolerance override in °C (FR-8.9)."""

_UNSIGNED: Final = re.compile(r"[0-9]+")


class InvalidRequest(Exception):
    """A request value is invalid (HTTP 422 ``validation_error``; FR-6.5, FR-8.2, FR-17.2).

    ``effective_limits`` is set when a target lies outside the slot's effective window (FR-8.2).
    No HID I/O has happened.
    """

    def __init__(
        self,
        message: str,
        *,
        controller_id: int | None = None,
        slot_id: int | None = None,
        effective_limits: EffectiveLimits | None = None,
    ) -> None:
        super().__init__(message)
        self.controller_id = controller_id
        self.slot_id = slot_id
        self.effective_limits = effective_limits


class NotConfigured(Exception):
    """A ControllerID or slot ID is valid but not configured (HTTP 404 ``not_found``; FR-6.5)."""

    def __init__(self, message: str, *, controller_id: int, slot_id: int | None = None) -> None:
        super().__init__(message)
        self.controller_id = controller_id
        self.slot_id = slot_id


class ReplyDataInvalid(TransactionFailed):
    """The transaction succeeded but its reply data cannot be parsed (FR-9.3, fail closed).

    It is a ``TransactionFailed`` of kind ``parse``; ``attempts`` is 1 because the engine does not
    report the attempt count of a successful transaction.
    """

    def __init__(self) -> None:
        super().__init__("parse", None, 1)


@dataclass(frozen=True)
class ActualTemperature:
    """Result of ``actual`` (FR-8.7)."""

    controller_id: int
    slot_id: int
    temperature_c: float
    selector: int


@dataclass(frozen=True)
class TargetTemperature:
    """Result of ``target`` (FR-8.8)."""

    controller_id: int
    slot_id: int
    temperature_c: float


@dataclass(frozen=True)
class AtTargetResult:
    """Result of ``at_target`` (FR-8.9): one instantaneous reading, selector 3."""

    controller_id: int
    slot_id: int
    at_target: bool
    actual_c: float
    target_c: float
    tolerance_c: float
    selector: int = RAT_COMPENSATED


@dataclass(frozen=True)
class SensorValues:
    """Result of ``sensor_values`` (FR-8.10). ``data`` is the raw reply data (format INFERRED —
    see api/README.md checklist item 6)."""

    controller_id: int
    selector: int
    data: str


@dataclass(frozen=True)
class DeviceTypeResult:
    """Result of ``device_type`` (FR-8.11). ``name`` is ``None`` for a code outside the table."""

    controller_id: int
    slot: int
    code: int
    name: str | None


@dataclass(frozen=True)
class FirmwareVersion:
    """Result of ``firmware_version`` (FR-8.12). ``data`` is the raw reply data."""

    controller_id: int
    selector: int
    name: str
    data: str


def is_int(value: object) -> TypeGuard[int]:
    """``True`` for an ``int`` that is not a ``bool`` (``True`` must never address slot 1)."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: object) -> TypeGuard[int | float]:
    """``True`` for an ``int`` or ``float`` that is not a ``bool``."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _tenths(reply: Reply) -> int:
    """Reply data as signed integer tenths of °C (protocol report §5.1); fail closed (FR-9.3)."""
    try:
        decode_tenths(reply.data)
    except DataParseError as exc:
        raise ReplyDataInvalid() from exc
    return int(reply.data)


def _code(reply: Reply) -> int:
    """Reply data as an unsigned decimal code (``RTD``, protocol report §5.3); fail closed."""
    if not _UNSIGNED.fullmatch(reply.data):
        raise ReplyDataInvalid()
    return int(reply.data)


class ReadOperations:
    """Shared request checks, locked device access and the C11 read operations."""

    def __init__(self, registry: ControllerRegistry) -> None:
        self._registry = registry

    # -- C9 gating step 1: IDs (FR-6.5) ------------------------------------------------------

    @staticmethod
    def _controller_in_range(cid: object) -> int:
        """Return ``cid`` when it is an int 0–7; :class:`InvalidRequest` otherwise (422)."""
        if not is_int(cid) or not CONTROLLER_ID_MIN <= cid <= CONTROLLER_ID_MAX:
            raise InvalidRequest(
                f"controller id must be an integer {CONTROLLER_ID_MIN}-{CONTROLLER_ID_MAX}"
            )
        return cid

    def _check_controller(self, cid: object) -> int:
        """Return ``cid`` when it is a configured ControllerID; 422, then 404 (FR-6.5)."""
        checked = self._controller_in_range(cid)
        if checked not in self._registry.managers:
            raise NotConfigured(f"controller {checked} is not configured", controller_id=checked)
        return checked

    def _check_slot(self, cid: object, sid: object) -> tuple[int, int]:
        """Return ``(cid, sid)`` for a configured slot; both range checks before any 404."""
        checked = self._controller_in_range(cid)
        if not is_int(sid) or not commands.SLOT_MIN <= sid <= commands.SLOT_MAX:
            raise InvalidRequest(
                f"slot id must be an integer {commands.SLOT_MIN}-{commands.SLOT_MAX}",
                controller_id=checked,
            )
        checked = self._check_controller(checked)
        if sid not in self._registry.managers[checked].slots:
            raise NotConfigured(
                f"slot {sid} is not configured on controller {checked}",
                controller_id=checked,
                slot_id=sid,
            )
        return checked, sid

    @staticmethod
    def _check_selector(value: object, allowed: range, what: str, cid: int) -> int:
        """C9 step 6: a selector must be an int inside ``allowed`` (FR-8.7, 8.10, 8.12)."""
        if not is_int(value) or value not in allowed:
            raise InvalidRequest(
                f"{what} must be an integer {allowed.start}-{allowed.stop - 1}",
                controller_id=cid,
            )
        return value

    # -- locked device access (T12-E, T14-E, T14-S4) -----------------------------------------

    @contextmanager
    def _device(self, cid: int) -> Iterator[ControllerManager]:
        """Hold the manager lock for one request; refuse while the service is stopping."""
        try:
            manager = self._registry.device_manager(cid)  # ServiceStopping while stopping
        except KeyError as exc:
            raise NotConfigured(f"controller {cid} is not configured", controller_id=cid) from exc
        with manager.locked():
            self._registry.check_accepting()  # T14-S4: shutdown may have started meanwhile
            yield manager

    # -- read operations (C11) ---------------------------------------------------------------

    def actual(self, cid: int, sid: int, sel: int = RAT_COMPENSATED) -> ActualTemperature:
        """``<sid>RAT<sel>``, selector 1–3, default 3 (FR-8.7)."""
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("read", sid)
            sel = self._check_selector(sel, RAT_SELECTORS, "selector", cid)
            reply = manager.run(commands.rat(sid, sel))
        return ActualTemperature(cid, sid, _tenths(reply) / 10, sel)

    def target(self, cid: int, sid: int) -> TargetTemperature:
        """``<sid>RTT`` (FR-8.8)."""
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("read", sid)
            reply = manager.run(commands.rtt(sid))
        return TargetTemperature(cid, sid, _tenths(reply) / 10)

    def at_target(self, cid: int, sid: int, tol: float | None = None) -> AtTargetResult:
        """``<sid>RAT3`` and ``<sid>RTT``; ``|actual − target| <= tolerance`` (FR-8.9).

        The tolerance defaults to the slot's configured ``at_target_tolerance_c``; an override
        must be a finite number within 0.1–5.0 °C. The comparison is exact in decimal:
        ``Decimal(abs(actual_t - target_t)) / 10 <= Decimal(repr(tol))`` with both temperatures
        in integer tenths, so equality is "at target".
        """
        cid, sid = self._check_slot(cid, sid)
        with self._device(cid) as manager:
            manager.gate("read", sid)
            tolerance = self._tolerance(manager, cid, sid, tol)
            actual_reply = manager.run(commands.rat(sid, RAT_COMPENSATED))
            target_reply = manager.run(commands.rtt(sid))
        actual_t, target_t = _tenths(actual_reply), _tenths(target_reply)
        at_target = Decimal(abs(actual_t - target_t)) / 10 <= Decimal(repr(tolerance))
        return AtTargetResult(cid, sid, at_target, actual_t / 10, target_t / 10, tolerance)

    def sensor_values(self, cid: int, sel: int) -> SensorValues:
        """``0RHV<sel>``, selector 0–8; 9 is refused (FR-8.10)."""
        cid = self._check_controller(cid)
        with self._device(cid) as manager:
            manager.gate("read")
            sel = self._check_selector(sel, RHV_SELECTORS, "selector", cid)
            reply = manager.run(commands.rhv(sel))
        return SensorValues(cid, sel, reply.data)

    def device_type(self, cid: int, slot: int) -> DeviceTypeResult:
        """``0RTD<slot>``, slot 0–6 with a range check only (FR-8.11, P3).

        Slot 0 names the controller type (0 STC, 1 MTC, 255 not set); 1–6 the slot device type of
        the HSL v2.2 enum (protocol report §5.3). An unlisted code has no name.
        """
        cid = self._check_controller(cid)
        if not is_int(slot) or slot not in RTD_SLOTS:
            raise InvalidRequest("slot must be an integer 0-6", controller_id=cid)
        with self._device(cid) as manager:
            manager.gate("read")
            reply = manager.run(commands.rtd(slot))
        code = _code(reply)
        names = CONTROLLER_TYPES if slot == 0 else DEVICE_TYPES
        return DeviceTypeResult(cid, slot, code, names.get(code))

    def firmware_version(self, cid: int, sel: int) -> FirmwareVersion:
        """``0RFV<sel>``, selector 0–4 (FR-8.12)."""
        cid = self._check_controller(cid)
        with self._device(cid) as manager:
            manager.gate("read")
            sel = self._check_selector(sel, FIRMWARE_SELECTORS, "selector", cid)
            reply = manager.run(commands.rfv(sel))
        return FirmwareVersion(cid, sel, FIRMWARE_VERSION_NAMES[sel], reply.data)

    @staticmethod
    def _tolerance(manager: ControllerManager, cid: int, sid: int, tol: object) -> float:
        """The configured default, or a finite override within 0.1–5.0 °C (FR-8.9)."""
        if tol is None:
            return float(manager.slots[sid].config.at_target_tolerance_c)
        if (
            not is_number(tol)
            or not math.isfinite(tol)
            or not TOLERANCE_MIN_C <= tol <= TOLERANCE_MAX_C
        ):
            raise InvalidRequest(
                f"tolerance_c must be a number {TOLERANCE_MIN_C}-{TOLERANCE_MAX_C}",
                controller_id=cid,
                slot_id=sid,
            )
        return float(tol)

"""Status data of the controller registry (design C9 ``status()``; FR-6.6, FR-13.4, FR-17.7).

Raw per-controller and per-slot data for the status operation, built from the managers without any
I/O. The HTTP layer (C12) renders these into the status response. ``registry.py`` re-exports them.
"""

from __future__ import annotations

from dataclasses import dataclass

from inheco_tec_api.controller import ControllerManager, ControllerState, SlotRuntime
from inheco_tec_api.identification import StartupSlotState

__all__ = [
    "ControllerStatus",
    "DetectedDeviceType",
    "EffectiveLimits",
    "SlotStatus",
    "controller_status",
]


@dataclass(frozen=True)
class DetectedDeviceType:
    """The ``RTD`` code of a slot and its HSL v2.2 name (``None`` when not in the enum)."""

    code: int
    name: str | None


@dataclass(frozen=True)
class EffectiveLimits:
    """The effective target window of a permitted slot, in °C (FR-7.4)."""

    min_c: float
    max_c: float


@dataclass(frozen=True)
class SlotStatus:
    """FR-6.6 data of one configured slot. Detected values are ``None`` when unread/unidentified.

    ``detected_article_number`` is raw device data: return it only as a structured field, never
    inside a message string (carry-forward T10-S2).
    """

    slot_id: int
    configured_class: str
    detected_device_type: DetectedDeviceType | None
    detected_article_number: str | None
    variant: str | None
    effective_limits: EffectiveLimits | None
    control_permitted: bool
    not_permitted_reason: str | None
    startup_state: StartupSlotState | None


@dataclass(frozen=True)
class ControllerStatus:
    """FR-6.6 data of one configured controller."""

    controller_id: int
    state: ControllerState
    transport_fake: bool
    emergency_off_active: bool
    slots: list[SlotStatus]


def controller_status(manager: ControllerManager) -> ControllerStatus:
    """FR-6.6 data of one controller, read without the lock (status never waits on a device).

    Each slot's identification is read once, so one slot entry is always self-consistent.
    ``emergency_off_active`` is reported as held, also on a ``lost`` controller (UA-1).
    """
    return ControllerStatus(
        controller_id=manager.cid,
        state=manager.state,
        transport_fake=manager.transport_fake,
        emergency_off_active=manager.emergency_off_active,
        slots=[_slot_status(sid, rt) for sid, rt in manager.slots.items()],
    )


def _slot_status(slot_id: int, runtime: SlotRuntime) -> SlotStatus:
    ident = runtime.identification  # read once: one consistent snapshot of this slot
    config = runtime.config
    if ident is None:
        return SlotStatus(
            slot_id=slot_id,
            configured_class=config.limit_class,
            detected_device_type=None,
            detected_article_number=None,
            variant=None,
            effective_limits=None,
            control_permitted=False,
            not_permitted_reason=runtime.not_permitted_reason,
            startup_state=runtime.startup_state,
        )
    limits = ident.limits
    window = (
        EffectiveLimits(limits.min_c, limits.max_c)
        if limits.permitted and limits.min_c is not None and limits.max_c is not None
        else None
    )
    return SlotStatus(
        slot_id=slot_id,
        configured_class=config.limit_class,
        detected_device_type=(
            DetectedDeviceType(ident.device_type, ident.device_type_name)
            if ident.device_type is not None
            else None
        ),
        detected_article_number=ident.article,
        variant=ident.variant,
        effective_limits=window,
        control_permitted=window is not None,
        not_permitted_reason=None if window is not None else (limits.reason or "unknown"),
        startup_state=runtime.startup_state,
    )

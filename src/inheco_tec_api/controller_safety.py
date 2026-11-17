"""Controller safety paths: stop, emergency-off and the re-initialize steps (design C9, S5, S6).

The functions here take the :class:`~.controller.ControllerManager` and are called by its methods
with the controller lock already held (FR-5.1). Every open and close of a handle, the transient one
included, therefore happens under that lock (carry-forward T7-C3).

- :func:`stop_slots`: ``<slot>ATE0`` per requested slot. On a ``connected`` controller it uses the
  open handle; on a ``released`` controller it opens the device for this request only, sends only
  the ``ATE0`` commands (with their FR-5.5 resyncs), runs no identification, closes the handle and
  leaves the controller ``released`` whatever the outcome (FR-12.6). An open failure raises
  :class:`StopNotDelivered` after an error-level ``tec_stop_not_delivered`` line (FR-12.7).
- :func:`emergency_off`: ``0AEO`` with the same transient-open rule while released (FR-17.4). Only
  a code ``0`` sets ``emergency_off_active`` (FR-17.7); any failure propagates and leaves the flag
  unchanged (FR-17.6).
- :func:`reinitialize_connected` / :func:`reinitialize_released`: the FR-17.8 steps run by connect
  (``connected``) and reacquire (``released``) while emergency-off is active: ``ATE0`` to every
  configured slot, identification, then clear the flag. The flag stays set on every failure.

None of these paths calls the release state store, except the unpersist that ends a reacquire:
stop and emergency-off never change the persisted released set (FR-12.8, carry-forward T11-C2).
A failed ``ATE0`` or ``0AEO`` does not prove the command was not applied: after ``DeviceLost`` or a
resync-raised ``TransactionFailed`` the slot state is unknown (carry-forward T9-C1, T9-C6).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Literal, TypeVar

from inheco_tec_api.controller_startup import (
    ConnectResult,
    ControllerError,
    ControllerLost,
    ControllerNotConnected,
    ReconnectStopFailed,
    SlotStopOutcome,
    identify_slots,
    log_state_change,
    open_transport,
    stop_slots_on,
)
from inheco_tec_api.engine import DeviceLost, TransactionEngine
from inheco_tec_api.logging_setup import EVENT_TEC_STOP_NOT_DELIVERED, get_logger
from inheco_tec_api.protocol import commands

if TYPE_CHECKING:
    from inheco_tec_api.controller import ControllerManager

__all__ = [
    "SafetyOperation",
    "StopNotDelivered",
    "emergency_off",
    "reinitialize_connected",
    "reinitialize_released",
    "stop_slots",
]

SafetyOperation = Literal["stop", "emergency_off"]

_T = TypeVar("_T")

_log = get_logger(__name__)


class StopNotDelivered(ControllerError):
    """A released controller could not be opened for stop or emergency-off (FR-12.7, FR-17.4).

    Nothing was sent; the controller stays ``released`` (HTTP 409 ``stop_not_delivered``).
    ``slots`` names the affected slots (every configured slot for emergency-off) and ``operation``
    says which request was not delivered.
    """

    def __init__(self, controller_id: int, slots: list[int], operation: SafetyOperation) -> None:
        if operation == "emergency_off":
            message = f"Emergency-off was NOT delivered (controller {controller_id})"
        else:
            noun = "slot" if len(slots) == 1 else "slots"
            listed = ", ".join(str(s) for s in slots)
            message = f"Stop was NOT delivered (controller {controller_id}, {noun} {listed})"
        super().__init__(controller_id, message)
        self.slots = slots
        self.operation: SafetyOperation = operation


def stop_slots(manager: ControllerManager, slot_ids: Iterable[int]) -> list[SlotStopOutcome]:
    """Send ``<slot>ATE0`` to the requested slots and return one outcome per slot (FR-8.5).

    ``connected``: every slot is attempted on the open handle; a ``DeviceLost`` then runs the C9
    loss handling (best-effort ``ATE0`` per slot, ``lost``) and the outcomes are still returned.
    ``released``: transient open (FR-12.6); open failure → :class:`StopNotDelivered` (FR-12.7).
    ``disconnected``/``lost`` → :class:`ControllerNotConnected`. An empty or unconfigured slot list
    raises ``ValueError`` before any I/O. The caller holds the controller lock.
    """
    slots = _checked_slots(manager, slot_ids)
    cid = manager.cid
    if manager.state == "released":
        outcomes = _transient(
            manager,
            slots,
            "stop",
            lambda engine: stop_slots_on(engine, cid, slots, "stop_while_released"),
        )
        log_state_change(cid, "released", "released", "stop_while_released")
        return outcomes
    engine = manager._engine
    if manager.state != "connected" or engine is None:
        raise ControllerNotConnected(cid, manager.state)
    outcomes = stop_slots_on(engine, cid, slots, "stop")
    if any(o.failure == "device_lost" for o in outcomes):
        manager._enter_lost(best_effort_stop=True)
    return outcomes


def emergency_off(manager: ControllerManager) -> None:
    """Send ``0AEO`` (FR-17.1); code ``0`` sets ``emergency_off_active`` (FR-17.7).

    ``connected``: through :meth:`~.controller.ControllerManager.run` (a ``DeviceLost`` gives
    ``lost`` and :class:`ControllerLost`). ``released``: transient open with only ``0AEO`` and its
    resync, then ``released`` again (FR-17.4); open failure → :class:`StopNotDelivered` naming
    every configured slot; a ``DeviceLost`` → :class:`ControllerNotConnected` (state ``released``).
    ``TransactionFailed`` propagates (FR-9) and the flag is unchanged (FR-17.6). The caller holds
    the controller lock.
    """
    cid = manager.cid
    state = manager.state
    if state == "released":
        _transient(manager, list(manager.slots), "emergency_off", _send_aeo_transient)
    elif state == "connected":
        manager.run(commands.aeo())
    else:
        raise ControllerNotConnected(cid, state)
    manager.emergency_off_active = True
    log_state_change(cid, state, state, "emergency_off_set")


def reinitialize_connected(manager: ControllerManager) -> ConnectResult:
    """FR-17.8 on a ``connected`` controller: ``ATE0`` every slot → identify → clear the flag.

    The handle is never detached while the state stays ``connected`` (carry-forward T12-C2): an
    ``ATE0`` failure keeps the handle, the state and the flag and raises
    :class:`ReconnectStopFailed` with the per-slot outcome (HTTP 502). A ``DeviceLost`` (in the
    sweep or the identification) runs the C9 loss handling, so the state becomes ``lost``
    explicitly; the flag stays set. The caller holds the controller lock.
    """
    cid = manager.cid
    engine = manager._engine
    if manager.state != "connected" or engine is None:
        raise ControllerNotConnected(cid, manager.state)
    slot_ids = list(manager.slots)
    outcomes = stop_slots_on(engine, cid, slot_ids, "reinitialize_stop")
    if any(o.outcome != "stopped" for o in outcomes):
        if any(o.failure == "device_lost" for o in outcomes):
            manager._enter_lost(best_effort_stop=True)
        raise ReconnectStopFailed(cid, outcomes, reinitialize=True)
    try:
        idents = identify_slots(engine, manager._slot_configs(), manager._manifest.article_numbers)
    except DeviceLost as exc:
        manager._enter_lost(best_effort_stop=True)
        raise ControllerLost(cid) from exc
    manager._apply_identification(idents)
    manager.emergency_off_active = False
    log_state_change(cid, "connected", "connected", "emergency_off_cleared")
    return ConnectResult("connected", stopped_slots=slot_ids, emergency_off_cleared=True)


def reinitialize_released(manager: ControllerManager) -> None:
    """FR-17.8 on a ``released`` controller: open → ``ATE0`` every slot → identify → unpersist.

    Any failure (open failure, an ``ATE0`` failure, a loss, a ``StateStoreError``) closes the
    handle and keeps the controller ``released`` with its persisted entry and the flag set. The
    flag is cleared together with the move to ``connected``, after the store write succeeded, so a
    failed unpersist never leaves a released controller with the flag cleared. The caller holds the
    controller lock.
    """
    cid = manager.cid
    transport = open_transport(manager._factory, cid, "reacquire")
    if transport is None:
        raise ControllerNotConnected(cid, "released", "open failed")
    engine = manager._attach(transport)
    slot_ids = list(manager.slots)
    try:
        with manager._closing_on_error():
            outcomes = stop_slots_on(engine, cid, slot_ids, "reinitialize_stop")
            if any(o.outcome != "stopped" for o in outcomes):
                raise ReconnectStopFailed(cid, outcomes, reinitialize=True)
            idents = identify_slots(
                engine, manager._slot_configs(), manager._manifest.article_numbers
            )
            manager._store.remove(cid)
    except DeviceLost as exc:
        raise ControllerNotConnected(cid, "released", "device lost") from exc
    manager._apply_identification(idents)
    manager.emergency_off_active = False
    manager._set_state("connected", "reacquire")
    log_state_change(cid, "connected", "connected", "emergency_off_cleared")


# -- internals ------------------------------------------------------------------------------------


def _checked_slots(manager: ControllerManager, slot_ids: Iterable[int]) -> list[int]:
    """The requested slots in order without duplicates; every one must be configured."""
    slots = list(dict.fromkeys(slot_ids))
    if not slots:
        raise ValueError("stop needs at least one slot")
    unknown = [s for s in slots if s not in manager.slots]
    if unknown:
        raise ValueError(f"slots {unknown} are not configured on controller {manager.cid}")
    return slots


def _transient(
    manager: ControllerManager,
    slots: list[int],
    operation: SafetyOperation,
    action: Callable[[TransactionEngine], _T],
) -> _T:
    """Open the released controller for one request, run ``action``, always close (FR-12.6).

    The handle is never attached to the manager, so the state stays ``released``. An open failure
    logs ``tec_stop_not_delivered`` at error level and raises :class:`StopNotDelivered` (FR-12.7).
    """
    cid = manager.cid
    purpose = "stop_while_released" if operation == "stop" else "emergency_off_while_released"
    transport = open_transport(manager._factory, cid, purpose)
    if transport is None:
        _log.error(
            EVENT_TEC_STOP_NOT_DELIVERED,
            controller_id=cid,
            operation=operation,
            slots=",".join(str(s) for s in slots),
        )
        raise StopNotDelivered(cid, slots, operation)
    try:
        return action(manager._new_engine(transport))
    finally:
        transport.close()


def _send_aeo_transient(engine: TransactionEngine) -> None:
    """``0AEO`` on a transient handle; a loss is reported without leaving ``released``."""
    try:
        engine.execute(commands.aeo())
    except DeviceLost as exc:
        raise ControllerNotConnected(
            engine.controller_id, "released", "device lost during emergency-off, outcome unknown"
        ) from exc

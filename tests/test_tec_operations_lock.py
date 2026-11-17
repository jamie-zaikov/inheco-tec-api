"""Lock and race tests for C11 ``TecService`` (Task 15; carry-forward T12-E, T12-S1, T14-S4).

- T12-E / T12-S1: the gate, the value checks and the transaction run under ONE
  ``manager.locked()``; an emergency-off from another thread cannot get between the gate and the
  command, and a flag that appears after the gate still refuses ``STT``/``ATE1`` with no write.
- T14-S4: a request that passed ``device_manager`` before a shutdown started and then waited for
  the lock gets ``ServiceStopping`` with no I/O.

Contention uses a transport whose read really blocks on a ``threading.Event`` (carry-forward
T8-C2), never real hardware.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import ControllerBusy, EmergencyOffActive, Operation
from inheco_tec_api.registry import ServiceStopping
from tec_controller_helpers import lock_free_elsewhere, lock_held_by_me
from tec_operations_helpers import OpsRig

DEVICE_OPS: list[tuple[str, Callable[[Any], Any]]] = [
    ("set_target", lambda s: s.set_target(0, 1, 37.0)),
    ("start", lambda s: s.start(0, 1)),
    ("stop", lambda s: s.stop(0, 1)),
    ("actual", lambda s: s.actual(0, 1)),
    ("target", lambda s: s.target(0, 1)),
    ("at_target", lambda s: s.at_target(0, 1)),
    ("sensor_values", lambda s: s.sensor_values(0, 0)),
    ("device_type", lambda s: s.device_type(0, 5)),
    ("firmware_version", lambda s: s.firmware_version(0, 0)),
    ("emergency_off", lambda s: s.emergency_off(0, True)),
]


def wrap_gate(rig: OpsRig, after: Callable[[Operation], None]) -> list[bool]:
    """Replace ``manager.gate`` on the instance: run the real gate, then ``after``.

    Returns a list that records, per gate call, whether the caller held the manager lock.
    """
    manager = rig.manager
    real_gate = manager.gate
    held: list[bool] = []

    def gate(operation: Operation, slot: int | None = None) -> None:
        held.append(lock_held_by_me(manager) and not lock_free_elsewhere(manager))
        real_gate(operation, slot)
        after(operation)

    manager.gate = gate  # type: ignore[method-assign]
    return held


# --- T12-E: the gate runs under the manager lock ------------------------------------------------


@pytest.mark.parametrize(("name", "call"), DEVICE_OPS, ids=[n for n, _ in DEVICE_OPS])
def test_t12e_gate_runs_with_the_manager_lock_held(
    tmp_path: Path, name: str, call: Callable[[Any], Any]
) -> None:
    rig = OpsRig(tmp_path)
    held = wrap_gate(rig, lambda op: None)
    call(rig.service)
    assert held == [True]
    assert rig.commands != []


# --- T12-S1: emergency-off cannot get between gate and command ----------------------------------


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda s: s.set_target(0, 1, 37.0), "1STT370"),
        (lambda s: s.start(0, 1), "1ATE1"),
    ],
    ids=["stt", "ate1"],
)
def test_t12s1_emergency_off_from_another_thread_waits_for_the_command(
    tmp_path: Path, call: Callable[[Any], Any], expected: str
) -> None:
    rig = OpsRig(tmp_path)
    eo_errors: list[BaseException] = []
    eo_thread: list[threading.Thread] = []

    def emergency_off() -> None:
        try:
            rig.service.emergency_off(0, True)
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            eo_errors.append(exc)

    def after(operation: Operation) -> None:
        if eo_thread:
            return  # the emergency-off's own gate call
        worker = threading.Thread(target=emergency_off)
        eo_thread.append(worker)
        worker.start()
        worker.join(0.3)
        # the EO thread is blocked on the lock: nothing reached the device yet
        assert worker.is_alive()
        assert "0AEO" not in rig.commands
        assert rig.manager.emergency_off_active is False

    wrap_gate(rig, after)
    call(rig.service)
    eo_thread[0].join(10)
    assert not eo_thread[0].is_alive() and eo_errors == []
    # serialised: the gated command first, then the emergency-off
    assert rig.commands == [expected, "0RDC1", "0AEO", "0RDC1"]
    assert rig.manager.emergency_off_active is True
    rig.mark()
    with pytest.raises(EmergencyOffActive):
        call(rig.service)
    assert rig.commands == []


@pytest.mark.parametrize(
    "call", [lambda s: s.set_target(0, 2, 37.0), lambda s: s.start(0, 2)], ids=["stt", "ate1"]
)
def test_t12s1_flag_set_after_the_gate_still_refuses_with_zero_writes(
    tmp_path: Path, call: Callable[[Any], Any]
) -> None:
    rig = OpsRig(tmp_path)

    def after(operation: Operation) -> None:
        rig.manager.emergency_off_active = True  # a racing EO lands between gate and run

    wrap_gate(rig, after)
    with pytest.raises(EmergencyOffActive):
        call(rig.service)
    assert rig.commands == []
    assert rig.dev.slots[2].enabled is False and rig.dev.slots[2].target == 0


# --- T14-S4: stopping set while a request waits for the lock ------------------------------------


@pytest.mark.parametrize(
    ("name", "call"),
    DEVICE_OPS
    + [
        ("connect", lambda s: s.connect(0)),
        ("release", lambda s: s.release(0)),
        ("reacquire", lambda s: s.reacquire(0)),
    ],
    ids=[n for n, _ in DEVICE_OPS] + ["connect", "release", "reacquire"],
)
def test_t14s4_stopping_while_waiting_for_the_lock_is_refused_without_io(
    tmp_path: Path, name: str, call: Callable[[Any], Any]
) -> None:
    rig = OpsRig(tmp_path)
    registry = rig.reg.registry
    real_device_manager = registry.device_manager
    passed = threading.Event()

    def device_manager(cid: int) -> Any:
        manager = real_device_manager(cid)
        passed.set()  # the request passed the first stopping check
        return manager

    registry.device_manager = device_manager  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def request() -> None:
        try:
            call(rig.service)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=request)
    with rig.reg.held(0):  # another thread holds the lock inside a stop of slot 1
        worker.start()
        assert passed.wait(5)
        registry._stopping.set()  # shutdown starts while the request waits
        rig.reg.factory.io_log.clear()
        before = list(rig.commands)
    worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ServiceStopping)
    # only the holder's stop finished; the waiting request wrote nothing and opened nothing
    assert rig.commands[len(before) :] in ([], ["0RDC1"])
    assert all(c in ("1ATE0", "0RDC1") for c in rig.commands)
    assert "open" not in [what for _, what in rig.reg.factory.io_log]
    assert rig.reg.store.calls == []


def test_t14s4_after_shutdown_every_device_operation_is_refused(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.reg.registry.shutdown()
    rig.mark()
    for _, call in DEVICE_OPS:
        with pytest.raises(ServiceStopping):
            call(rig.service)
    for lifecycle in (rig.service.connect, rig.service.release, rig.service.reacquire):
        with pytest.raises(ServiceStopping):
            lifecycle(0)
    assert rig.commands == [] and rig.reg.factory.events == []


# --- NFR-3 / R5: a busy controller refuses after lock_wait_s ------------------------------------


def test_nfr3_busy_controller_raises_controller_busy_without_io(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path, lock_wait_s=1)
    with rig.reg.held(0):
        before = list(rig.commands)
        with pytest.raises(ControllerBusy):
            rig.service.set_target(0, 1, 37.0)
        assert rig.commands == before
    assert "1STT370" not in rig.commands

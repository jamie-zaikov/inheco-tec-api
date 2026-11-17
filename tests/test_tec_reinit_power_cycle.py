"""Emergency-off recovery: the power-cycle message on firmware code ``I`` (Tasks 28 and 29).

Requirements: FR-17 acceptance (A7, A7.1), FR-17.8 (a failed re-initialize keeps the flag and
``released``, adds no command; A7.1: Release of a ``connected`` controller keeps the flag),
FR-17.10 (code ``I`` until a power cycle), FR-17.11 (the 502 ``stop_all_failed`` message gains the
power-cycle statement only on a re-initialize ``I``; A7.1, Task 29: the statement directs the
operator to release if connected, power-cycle, then Reacquire, and names no Connect), FR-16.5
(the documented recovery path), and design C12 *Re-initialize power-cycle message*, C16 (A7.1),
P10, P11, DD-16 (the FR-11.4 reconnect after a loss keeps the pre-A7 message byte for byte).

HTTP tests use ``TestClient`` (in process, no port) over the fake transport; every response body
is scanned for leaks by :class:`ScanningClient`. Controller tests use the fake-backed ``Rig``. The
real ``hid`` module is never imported and no device is opened.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import ControllerLost, ReconnectStopFailed, SlotStopOutcome
from inheco_tec_api.http import errors
from inheco_tec_api.http.errors import to_api_error
from inheco_tec_api.protocol import commands, tables
from inheco_tec_api.transport.fake_actions import DeviceLoss, FirmwareCode, Match
from tec_api_helpers import ApiRig, ScanningClient
from tec_controller_helpers import Rig

BASE = (
    "Stop on reconnect failed on controller {cid}: not every slot was confirmed stopped;"
    " the slot state is unknown"
)
"""The pre-A7 ``_reconnect_stop_failed`` message (design C12 table, first row)."""

STATEMENT = (
    ". Firmware code I after emergency-off: release the controller if it is connected,"
    " power-cycle the TEC controller, then call Reacquire; emergency-off stays active."
)
"""The exact C12 ``_POWER_CYCLE_STATEMENT`` text (A7.1, task 29.1; 162 characters)."""

FULL_MESSAGE_LENGTH = 267
"""``len(BASE) + len(STATEMENT)`` for every ``cid`` 0-7 (105 + 162, design C12 *Length* A7.1)."""

NODE_MAX_FIELD_LENGTH = 300
"""``MAX_FIELD_LENGTH`` of ``node/src/nodes/InhecoTec/errors.ts`` (design C12 *Length*)."""

REINIT_WRITES = {"1ATE0", "2ATE0", "0RDC1"}
"""The only commands a failed re-initialize may write: ``ATE0`` per slot plus the resyncs."""


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    """A started app with emergency-off active (confirmed) and the earlier writes forgotten."""
    with rig.client() as started:
        response = _post(rig, started, "/v1/controllers/0/emergency-off", json={"confirm": True})
        assert response.status_code == 200
        assert _status(rig, started)["emergency_off_active"] is True
        rig.mark()
        yield started


def _post(rig: ApiRig, client: ScanningClient, path: str, **kwargs: Any) -> Any:
    return client.post(path, headers=rig.auth, **kwargs)


def _status(rig: ApiRig, client: ScanningClient, cid: int = 0) -> dict[str, Any]:
    body = client.get("/v1/status", headers=rig.auth).json()
    return next(c for c in body["controllers"] if c["controller_id"] == cid)


def _answer_every_ate0(rig: ApiRig, code: str) -> None:
    rig.dev().script(match=Match(mnemonic="ATE"), action=FirmwareCode(code), times=None)


def _release(rig: ApiRig, client: ScanningClient) -> None:
    response = _post(rig, client, "/v1/controllers/0/release")
    assert response.status_code == 200
    rig.mark()


def _slot_rows(error: dict[str, Any]) -> list[tuple[Any, ...]]:
    return [(s["controller_id"], s["slot_id"], s["outcome"], s["failure"]) for s in error["slots"]]


def _reinit_call(rig: ApiRig, client: ScanningClient, path: str) -> Any:
    """Release first for Reacquire; then call ``path`` (``connect`` or ``reacquire``)."""
    if path == "reacquire":
        _release(rig, client)
    return _post(rig, client, f"/v1/controllers/0/{path}")


# -- FR-17.11 / FR-17.8: re-initialize answered with code I ----------------------------------


@pytest.mark.parametrize(
    ("path", "state"),
    [("reacquire", "released"), ("connect", "connected")],
    ids=["reacquire-released", "connect-connected"],
)
def test_fr17_11_reinit_code_i_gives_502_with_power_cycle_message(
    rig: ApiRig, client: ScanningClient, path: str, state: str
) -> None:
    _answer_every_ate0(rig, "I")
    opens = rig.dev().open_count
    response = _reinit_call(rig, client, path)
    assert response.status_code == 502
    error = rig.error(response)
    assert error["kind"] == "stop_all_failed"
    assert error["controller_id"] == 0
    message = error["message"]
    assert message == BASE.format(cid=0) + STATEMENT
    for needed in ("release", "power-cycle", "Reacquire", "emergency-off stays active"):
        assert needed in message, f"message lacks {needed!r}"
    # A7.1: the statement names no Connect (case-sensitive; the base holds "reconnect").
    assert "Connect" not in message[len(BASE.format(cid=0)) :]
    assert len(message) <= NODE_MAX_FIELD_LENGTH
    assert _slot_rows(error) == [(0, 1, "failed", "I"), (0, 2, "failed", "I")]
    status = _status(rig, client)
    assert status["emergency_off_active"] is True
    assert status["state"] == state
    if path == "reacquire":
        assert rig.dev().open_count == opens + 1  # the transient open of the re-initialize
        assert not rig.dev().is_open  # closed again: the controller stays released
    else:
        assert rig.dev().open_count == opens  # the kept handle, no reopen
        assert rig.dev().is_open


@pytest.mark.parametrize("path", ["reacquire", "connect"])
def test_fr17_8_reinit_code_i_writes_only_the_reinitialize_commands(
    rig: ApiRig, client: ScanningClient, path: str
) -> None:
    _answer_every_ate0(rig, "I")
    response = _reinit_call(rig, client, path)
    assert response.status_code == 502
    written = rig.commands()
    assert set(written) <= REINIT_WRITES, written
    assert [c for c in written if c != "0RDC1"] == ["1ATE0", "2ATE0"]  # no retry, no identify


def test_fr17_8_connect_code_i_keeps_control_refused(rig: ApiRig, client: ScanningClient) -> None:
    _answer_every_ate0(rig, "I")
    assert _reinit_call(rig, client, "connect").status_code == 502
    rig.mark()
    response = _post(rig, client, "/v1/controllers/0/slots/1/start")
    assert response.status_code == 409
    assert rig.error(response)["kind"] == "emergency_off_active"
    assert rig.commands() == []


@pytest.mark.parametrize("path", ["reacquire", "connect"])
def test_fr17_11_repeat_after_power_cycle_recovers(
    rig: ApiRig, client: ScanningClient, path: str
) -> None:
    # ``reacquire`` is the FR-16.5 / FR-16.6 documented path (A7.1, P11): Release (inside
    # ``_reinit_call``), Reacquire 502 with code I, power cycle, Reacquire 200.
    # ``connect`` is an FR-17.8 behaviour test only, not the documented path: the fake keeps a
    # live handle and does not model the likely dead handle after a power cycle (T28-C1).
    rig.dev().script(match=Match(mnemonic="ATE"), action=FirmwareCode("I"), times=2)
    first = _reinit_call(rig, client, path)
    assert first.status_code == 502
    first_error = rig.error(first)
    assert first_error["kind"] == "stop_all_failed"
    assert first_error["message"] == BASE.format(cid=0) + STATEMENT  # the A7.1 statement
    # The script is used up: the fake now acts as the power-cycled controller.
    again = _post(rig, client, f"/v1/controllers/0/{path}")
    assert again.status_code == 200
    body = again.json()
    assert body["state"] == "connected"
    assert body["emergency_off_cleared"] is True
    assert body["stopped_slots"] == [1, 2]
    assert _status(rig, client)["emergency_off_active"] is False


@pytest.mark.parametrize("path", ["reacquire", "connect"])
def test_fr17_11_one_slot_code_i_still_gives_statement(
    rig: ApiRig, client: ScanningClient, path: str
) -> None:
    rig.dev().script(match=Match(mnemonic="ATE", address=2), action=FirmwareCode("I"), times=None)
    response = _reinit_call(rig, client, path)
    assert response.status_code == 502
    error = rig.error(response)
    assert error["message"] == BASE.format(cid=0) + STATEMENT
    assert _slot_rows(error) == [(0, 1, "stopped", None), (0, 2, "failed", "I")]


def test_fr16_5_release_connected_with_flag_active_keeps_flag(
    rig: ApiRig, client: ScanningClient
) -> None:
    """A7.1 (FR-16.5 step 1, FR-17.7, FR-17.8): Release is allowed and does not clear the flag."""
    response = _post(rig, client, "/v1/controllers/0/release")
    assert response.status_code == 200
    assert response.json()["state"] == "released"
    status = _status(rig, client)
    assert status["emergency_off_active"] is True
    assert status["state"] == "released"
    rig.mark()
    target = client.put(
        "/v1/controllers/0/slots/1/target", headers=rig.auth, json={"temperature_c": 37.0}
    )
    assert target.status_code == 409
    assert rig.error(target)["kind"] == "controller_released"
    assert rig.commands() == []
    assert _status(rig, client)["emergency_off_active"] is True


# -- FR-17.11: another firmware code keeps the base message ----------------------------------


@pytest.mark.parametrize("code", ["3", "5", "W"])
@pytest.mark.parametrize(
    ("path", "state"),
    [("reacquire", "released"), ("connect", "connected")],
    ids=["reacquire-released", "connect-connected"],
)
def test_fr17_11_reinit_other_code_gives_base_message_only(
    rig: ApiRig, client: ScanningClient, path: str, state: str, code: str
) -> None:
    _answer_every_ate0(rig, code)
    response = _reinit_call(rig, client, path)
    assert response.status_code == 502
    error = rig.error(response)
    assert error["kind"] == "stop_all_failed"
    assert error["message"] == BASE.format(cid=0)
    assert "power-cycle" not in error["message"]
    assert _slot_rows(error) == [(0, 1, "failed", code), (0, 2, "failed", code)]
    status = _status(rig, client)
    assert status["emergency_off_active"] is True
    assert status["state"] == state


# -- P10 / FR-11.4: the reconnect after a loss keeps the pre-A7 message ----------------------


def test_p10_reconnect_after_loss_code_i_keeps_pre_a7_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = ApiRig(tmp_path, monkeypatch)
    with rig.client() as client:
        rig.dev().script(match=Match(mnemonic="RTT"), action=DeviceLoss(), times=1)
        lost = client.get("/v1/controllers/0/slots/1/target", headers=rig.auth)
        assert lost.status_code == 503  # ControllerLost: controller_not_connected
        assert _status(rig, client)["state"] == "lost"
        assert _status(rig, client)["emergency_off_active"] is False
        _answer_every_ate0(rig, "I")
        rig.mark()
        response = _post(rig, client, "/v1/controllers/0/connect")
        assert response.status_code == 502
        error = rig.error(response)
        assert error["kind"] == "stop_all_failed"
        assert error["message"] == BASE.format(cid=0)  # byte-identical to the pre-A7 text
        assert "power-cycle" not in error["message"]
        assert _slot_rows(error) == [(0, 1, "failed", "I"), (0, 2, "failed", "I")]
        assert _status(rig, client)["state"] == "lost"


# -- FR-12.5 / FR-17.8: Reacquire of a connected controller is a no-op -----------------------


def test_fr12_5_reacquire_connected_with_flag_active_is_noop(
    rig: ApiRig, client: ScanningClient
) -> None:
    _answer_every_ate0(rig, "I")  # would fail loudly if any ATE0 were sent
    opens = rig.dev().open_count
    response = _post(rig, client, "/v1/controllers/0/reacquire")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "connected"
    assert body["emergency_off_cleared"] is False
    assert body["stopped_slots"] == []
    assert rig.commands() == []  # zero device writes
    assert rig.dev().open_count == opens
    status = _status(rig, client)
    assert status["emergency_off_active"] is True
    assert status["state"] == "connected"


# -- DD-16: the reinitialize flag origin (controller level) ----------------------------------


def _eo_rig(tmp_path: Path, *, release: bool) -> Rig:
    rig = Rig(tmp_path).connected()
    if release:
        rig.manager.release()
    rig.manager.emergency_off()
    assert rig.manager.emergency_off_active
    rig.mark()
    rig.dev.script(match=Match(mnemonic="ATE"), action=FirmwareCode("I"), times=None)
    return rig


def test_dd16_reinitialize_connected_raises_with_flag_true(tmp_path: Path) -> None:
    rig = _eo_rig(tmp_path, release=False)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.reinitialize is True
    assert info.value.outcomes == [
        SlotStopOutcome(1, "failed", "I"),
        SlotStopOutcome(2, "failed", "I"),
    ]
    assert rig.manager.state == "connected" and rig.manager.emergency_off_active


def test_dd16_reinitialize_released_raises_with_flag_true(tmp_path: Path) -> None:
    rig = _eo_rig(tmp_path, release=True)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.reacquire()
    assert info.value.reinitialize is True
    assert rig.manager.state == "released" and rig.manager.emergency_off_active


def test_dd16_reconnect_after_loss_raises_with_flag_false(tmp_path: Path) -> None:
    rig = Rig(tmp_path).connected()
    rig.dev.script(action=DeviceLoss(), times=1)
    with pytest.raises(ControllerLost):
        rig.manager.run(commands.stt(1, 370))
    assert rig.manager.state == "lost"
    rig.dev.script(match=Match(mnemonic="ATE"), action=FirmwareCode("I"), times=None)
    with pytest.raises(ReconnectStopFailed) as info:
        rig.manager.connect()
    assert info.value.reinitialize is False
    assert info.value.outcomes[0] == SlotStopOutcome(1, "failed", "I")


def test_dd16_constructor_default_is_false() -> None:
    assert ReconnectStopFailed(3, []).reinitialize is False
    assert ReconnectStopFailed(3, [], reinitialize=True).reinitialize is True


# -- C12 / P10: the predicate and the message rule (unit) ------------------------------------

FAILED_I = SlotStopOutcome(1, "failed", "I")
FAILED_3 = SlotStopOutcome(2, "failed", "3")
FAILED_NO_REPLY = SlotStopOutcome(1, "failed", "no_reply")
FAILED_LOST = SlotStopOutcome(1, "failed", "device_lost")
NOT_ATTEMPTED = SlotStopOutcome(2, "not_attempted")
STOPPED = SlotStopOutcome(1, "stopped")

PREDICATE_CASES = [
    ([FAILED_I], True),
    ([FAILED_I, FAILED_I], True),
    ([FAILED_I, FAILED_3], True),
    ([FAILED_3, FAILED_I], True),
    ([STOPPED, FAILED_I], True),
    ([FAILED_LOST, FAILED_I], True),
    ([FAILED_3], False),
    ([FAILED_3, FAILED_3], False),
    ([FAILED_NO_REPLY], False),
    ([FAILED_LOST, NOT_ATTEMPTED], False),
    ([NOT_ATTEMPTED], False),
    ([STOPPED, FAILED_3], False),
    ([SlotStopOutcome(1, "failed", "i")], False),  # exact code match only
    ([SlotStopOutcome(1, "stopped", "I")], False),  # only a failed slot counts
    ([SlotStopOutcome(1, "not_attempted", "I")], False),
    ([], False),
]


@pytest.mark.parametrize(("outcomes", "expected"), PREDICATE_CASES)
@pytest.mark.parametrize("reinitialize", [False, True])
def test_fr17_11_power_cycle_predicate(
    outcomes: list[SlotStopOutcome], expected: bool, reinitialize: bool
) -> None:
    exc = ReconnectStopFailed(0, outcomes, reinitialize=reinitialize)
    assert errors._power_cycle_required(exc) is (reinitialize and expected)


@pytest.mark.parametrize(("outcomes", "expected"), PREDICATE_CASES)
@pytest.mark.parametrize("reinitialize", [False, True])
@pytest.mark.parametrize("cid", [0, 4, 7])
def test_fr17_11_message_is_base_plus_statement_exactly_when_predicate(
    outcomes: list[SlotStopOutcome], expected: bool, reinitialize: bool, cid: int
) -> None:
    exc = ReconnectStopFailed(cid, outcomes, reinitialize=reinitialize)
    err = errors._reconnect_stop_failed(exc)
    assert err.kind == "stop_all_failed"
    assert err.status_code == 502
    want = BASE.format(cid=cid) + (STATEMENT if reinitialize and expected else "")
    assert err.message == want
    assert err.detail().message == want
    assert to_api_error(exc).message == want  # the public mapping uses the same rule


@pytest.mark.parametrize("reinitialize", [False, True])
def test_fr17_11_slots_and_kind_unchanged_by_the_flag(reinitialize: bool) -> None:
    outcomes = [FAILED_I, FAILED_3, NOT_ATTEMPTED]
    err = to_api_error(ReconnectStopFailed(5, outcomes, reinitialize=reinitialize))
    assert err.kind == "stop_all_failed" and err.status_code == 502
    assert err.controller_id == 5
    assert err.slots is not None
    assert [(r.slot_id, r.outcome, r.failure) for r in err.slots] == [
        (1, "failed", "I"),
        (2, "failed", "3"),
        (2, "not_attempted", None),
    ]


def test_c12_power_cycle_statement_constant_exact() -> None:
    assert errors._POWER_CYCLE_STATEMENT == STATEMENT


@pytest.mark.parametrize("cid", range(8))
def test_c12_full_message_fits_node_max_field_length(cid: int) -> None:
    exc = ReconnectStopFailed(cid, [FAILED_I], reinitialize=True)
    message = to_api_error(exc).message
    assert message.endswith(STATEMENT)
    assert len(message) == len(BASE.format(cid=cid)) + len(STATEMENT) == FULL_MESSAGE_LENGTH
    assert len(message) <= NODE_MAX_FIELD_LENGTH


def test_c3_code_cpac_voltage_not_ok_constant() -> None:
    assert tables.CODE_CPAC_VOLTAGE_NOT_OK == "I"
    assert "I" in tables.FIRMWARE_ERRORS
    assert "I" not in tables.TRANSIENT_CODES  # FR-5.7: never retried

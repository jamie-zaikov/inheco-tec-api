"""Tests for the C11 read operations of ``TecService`` (Task 15; ``operations_read.py``).

Requirements: FR-8.7 (``RAT`` selector 1-3, default 3), FR-8.8 (``RTT``), FR-8.9 (at-target with
the exact ``Decimal`` comparison: true at |delta| = tolerance, false just above; default tolerance
from the slot config; override 0.1-5.0), FR-8.10 (``0RHV`` 0-8, 9 refused), FR-8.11 (``0RTD`` slot
0-6, range check only, design P3), FR-8.12 (``0RFV`` 0-4 with names), FR-9.3 (unparseable reply
data fails closed as a ``parse`` failure). Every value check rejects with zero writes. Every test
runs the real engine over the fake transport (Task 8) with virtual time.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.controller import ControllerReleased
from inheco_tec_api.engine import TransactionFailed
from inheco_tec_api.operations import (
    ActualTemperature,
    AtTargetResult,
    DeviceTypeResult,
    FirmwareVersion,
    InvalidRequest,
    ReplyDataInvalid,
    SensorValues,
    TargetTemperature,
)
from inheco_tec_api.transport import fake as fake_module
from tec_operations_helpers import OpsRig

# --- FR-8.7: actual temperature -----------------------------------------------------------------


def test_fr87_actual_defaults_to_selector_3(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].actual = 372
    assert rig.service.actual(0, 1) == ActualTemperature(0, 1, 37.2, 3)
    assert rig.commands == ["1RAT3", "0RDC1"]


@pytest.mark.parametrize("sel", [1, 2, 3])
def test_fr87_actual_sends_the_requested_selector(tmp_path: Path, sel: int) -> None:
    rig = OpsRig(tmp_path)
    result = rig.service.actual(0, 2, sel)
    assert rig.commands == [f"2RAT{sel}", "0RDC1"]
    assert (result.selector, result.temperature_c, result.slot_id) == (sel, 25.0, 2)


def test_fr87_negative_actual_is_decoded(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].actual = -5
    assert rig.service.actual(0, 1).temperature_c == -0.5


@pytest.mark.parametrize("sel", [0, 4, -1, True, "3", 3.0, None])
def test_fr87_actual_selector_outside_1_to_3_is_invalid_with_zero_writes(
    tmp_path: Path, sel: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest):
        rig.service.actual(0, 1, sel)
    assert rig.commands == []


# --- FR-8.8: target temperature -----------------------------------------------------------------


def test_fr88_target_reads_rtt(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[2].target = 455
    assert rig.service.target(0, 2) == TargetTemperature(0, 2, 45.5)
    assert rig.commands == ["2RTT", "0RDC1"]


def test_fr88_reads_refused_while_released(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.release()
    with pytest.raises(ControllerReleased):
        rig.service.target(0, 1)
    assert rig.commands == [] and rig.reg.factory.events == []


# --- FR-8.9: at target --------------------------------------------------------------------------

AT_TARGET_CMDS = ["1RAT3", "0RDC1", "1RTT", "0RDC1"]


@pytest.mark.parametrize(
    ("actual", "target", "tol", "expected"),
    [
        (380, 370, None, True),  # |delta| = default tolerance 1.0
        (360, 370, None, True),  # negative delta, equal to tolerance
        (381, 370, None, False),  # just above
        (359, 370, None, False),
        (375, 370, 0.5, True),
        (376, 370, 0.5, False),
        (371, 370, 0.1, True),  # 0.1 is not exact in binary; the Decimal compare is
        (372, 370, 0.1, False),
        (420, 370, 5.0, True),
        (421, 370, 5.0, False),
        (373, 370, 0.3, True),  # 0.1 + 0.2 style float error must not flip the result
        (370, 370, None, True),
    ],
)
def test_fr89_at_target_compares_exactly_at_the_tolerance(
    tmp_path: Path, actual: int, target: int, tol: float | None, expected: bool
) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].actual, rig.dev.slots[1].target = actual, target
    result = rig.service.at_target(0, 1, tol)
    assert rig.commands == AT_TARGET_CMDS
    assert result == AtTargetResult(
        0, 1, expected, actual / 10, target / 10, 1.0 if tol is None else tol, 3
    )


def test_fr89_default_tolerance_comes_from_the_slot_config(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    tolerance = rig.manager.slots[1].config.at_target_tolerance_c
    assert rig.service.at_target(0, 1).tolerance_c == float(tolerance)


@pytest.mark.parametrize("tol", [0.09, 5.01, 0.0, -1.0, math.nan, math.inf, True, "1.0"])
def test_fr89_tolerance_override_outside_range_is_invalid_with_zero_writes(
    tmp_path: Path, tol: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest) as info:
        rig.service.at_target(0, 1, tol)
    assert (info.value.controller_id, info.value.slot_id) == (0, 1)
    assert rig.commands == []


def test_fr89_integer_tolerance_override_is_accepted(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.at_target(0, 1, 2).tolerance_c == 2.0


# --- FR-8.10: sensor values ---------------------------------------------------------------------


@pytest.mark.parametrize("sel", range(9))
def test_fr810_sensor_values_send_rhv_and_return_raw_data(tmp_path: Path, sel: int) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.sensor_values(0, sel) == SensorValues(0, sel, str(200 + sel))
    assert rig.commands == [f"0RHV{sel}", "0RDC1"]
    assert not any("RFV" in c for c in rig.commands)


@pytest.mark.parametrize("sel", [9, -1, 10, True, "0", None])
def test_fr810_rhv_selector_9_or_outside_0_to_8_is_invalid_with_zero_writes(
    tmp_path: Path, sel: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest):
        rig.service.sensor_values(0, sel)
    assert rig.commands == []


# --- FR-8.11: device type -----------------------------------------------------------------------


def test_fr811_device_type_slot_0_is_the_controller_type(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.device_type(0, 0) == DeviceTypeResult(0, 0, 1, "MTC")
    assert rig.commands == ["0RTD0", "0RDC1"]


@pytest.mark.parametrize(("code", "name"), [(0, "STC"), (255, "not set"), (7, None)])
def test_fr811_controller_type_names(tmp_path: Path, code: int, name: str | None) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.mainboard.controller_type = code
    assert rig.service.device_type(0, 0) == DeviceTypeResult(0, 0, code, name)


def test_fr811_device_type_of_a_configured_slot(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.device_type(0, 2) == DeviceTypeResult(0, 2, 1, "CPAC")
    assert rig.commands == ["0RTD2", "0RDC1"]


def test_fr811_p3_device_type_for_unconfigured_slot_5_succeeds(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    assert rig.service.device_type(0, 5) == DeviceTypeResult(0, 5, 128, "Empty slot")
    assert rig.commands == ["0RTD5", "0RDC1"]


def test_fr811_unlisted_device_type_code_has_no_name(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.slots[1].device_type = 99
    assert rig.service.device_type(0, 1) == DeviceTypeResult(0, 1, 99, None)


@pytest.mark.parametrize("slot", [7, -1, True, "1", None])
def test_fr811_device_type_slot_outside_0_to_6_is_invalid_with_zero_writes(
    tmp_path: Path, slot: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest):
        rig.service.device_type(0, slot)
    assert rig.commands == []


# --- FR-8.12: firmware version ------------------------------------------------------------------

NAMES = ["bootstrap", "application", "serial", "hardware", "copyright"]


@pytest.mark.parametrize("sel", range(5))
def test_fr812_firmware_version_sends_rfv_with_name(tmp_path: Path, sel: int) -> None:
    rig = OpsRig(tmp_path)
    expected = rig.dev.mainboard.firmware[sel]
    assert rig.service.firmware_version(0, sel) == FirmwareVersion(0, sel, NAMES[sel], expected)
    assert rig.commands == [f"0RFV{sel}", "0RDC1"]


@pytest.mark.parametrize("sel", [5, -1, True, "0", None])
def test_fr812_rfv_selector_outside_0_to_4_is_invalid_with_zero_writes(
    tmp_path: Path, sel: Any
) -> None:
    rig = OpsRig(tmp_path)
    with pytest.raises(InvalidRequest):
        rig.service.firmware_version(0, sel)
    assert rig.commands == []


# --- FR-17.7: reads stay allowed while emergency-off is active ----------------------------------


def test_fr177_reads_allowed_while_emergency_off_active(tmp_path: Path) -> None:
    rig = OpsRig(tmp_path)
    rig.service.emergency_off(0, True)
    rig.mark()
    rig.service.actual(0, 1)
    rig.service.target(0, 1)
    rig.service.at_target(0, 1)
    rig.service.sensor_values(0, 0)
    rig.service.device_type(0, 0)
    rig.service.firmware_version(0, 0)
    assert rig.commands[::2] == ["1RAT3", "1RTT", "1RAT3", "1RTT", "0RHV0", "0RTD0", "0RFV0"]


# --- FR-9.3: unparseable reply data fails closed ------------------------------------------------


def _reply_data(monkeypatch: pytest.MonkeyPatch, mnemonic: str, data: str) -> None:
    monkeypatch.setitem(fake_module._SLOT_HANDLERS, mnemonic, lambda slot, param: ("0", data))


@pytest.mark.parametrize("data", ["", "12a", "+-1", "3 7"])
def test_fr93_unparseable_temperature_is_a_parse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    rig = OpsRig(tmp_path)
    _reply_data(monkeypatch, "RTT", data)
    with pytest.raises(ReplyDataInvalid) as info:
        rig.service.target(0, 1)
    assert isinstance(info.value, TransactionFailed)
    assert info.value.kind == "parse"
    with pytest.raises(ReplyDataInvalid):
        rig.service.at_target(0, 1)
    _reply_data(monkeypatch, "RAT", data)
    with pytest.raises(ReplyDataInvalid):
        rig.service.actual(0, 1)


@pytest.mark.parametrize("value", ["X", "-1", "1.5", ""])
def test_fr93_unparseable_device_type_is_a_parse_failure(tmp_path: Path, value: str) -> None:
    rig = OpsRig(tmp_path)
    rig.dev.mainboard.controller_type = value  # type: ignore[assignment]
    with pytest.raises(ReplyDataInvalid) as info:
        rig.service.device_type(0, 0)
    assert info.value.kind == "parse"

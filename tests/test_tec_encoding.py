"""Tests for C3 tenths encoding and firmware tables (Task 3).

Covers FR-5.10 (round-half-away-from-zero encode, ``int(data) / 10`` decode), FR-9.3 (non-integer
data and unknown error characters fail closed), FR-5.6 (exact transient set), FR-9.1 (§8.2
descriptions), FR-7.1 / FR-8.11 (§5.3 device and controller types), NFR-5 (pure stdlib codec) and
NFR-8 (section citations). Expected values are transcribed from
``<private-doc-ref>`` §5.1, §5.3 and §8.2, not from the implementation.
"""

from __future__ import annotations

import ast
import math
import sys
from pathlib import Path

import pytest
from inheco_tec_api.protocol import crc, encoding, tables
from inheco_tec_api.protocol.encoding import DataParseError, decode_tenths, encode_tenths
from inheco_tec_api.protocol.tables import (
    CONTROLLER_TYPES,
    DEVICE_TYPES,
    FIRMWARE_ERRORS,
    TRANSIENT_CODES,
)

# ---------------------------------------------------------------------------------------------
# encode_tenths (FR-5.10)
# ---------------------------------------------------------------------------------------------

ENCODE_TABLE: list[tuple[float, int]] = [
    (36.6, 366),  # FR-5.10 acceptance; binary 36.6 * 10 = 365.99999...
    (37.0, 370),
    (15.0, 150),
    (100.0, 1000),
    (199.9, 1999),
    (4.0, 40),
    (70.0, 700),
    (110.0, 1100),
    (0.0, 0),
    (-0.0, 0),
    (0.1, 1),
    (0.7, 7),
    (12.3, 123),
    (-5.0, -50),
    (-36.6, -366),
    (-0.1, -1),
    (0.04, 0),
    (-0.04, 0),
    (0.06, 1),
    (-0.06, -1),
    (36.64, 366),
    (36.66, 367),
]

# Exact halves round away from zero (not to even, not floor, not binary-float rounding).
HALF_TABLE: list[tuple[float, int]] = [
    (0.05, 1),
    (-0.05, -1),
    (0.15, 2),  # binary 0.15 is 0.1499...; decimal rounding still gives 2
    (-0.15, -2),
    (0.25, 3),  # round-half-even would give 2
    (-0.25, -3),
    (36.65, 367),
    (-36.65, -367),
    (2.675, 27),
    (199.95, 2000),
    (4.45, 45),
]


@pytest.mark.parametrize(("celsius", "tenths"), ENCODE_TABLE)
def test_fr5_10_encode_table(celsius: float, tenths: int) -> None:
    result = encode_tenths(celsius)
    assert result == tenths
    assert type(result) is int


@pytest.mark.parametrize(("celsius", "tenths"), HALF_TABLE)
def test_fr5_10_encode_rounds_halves_away_from_zero(celsius: float, tenths: int) -> None:
    assert encode_tenths(celsius) == tenths


def test_fr5_10_encode_is_not_floor_and_not_round_half_even() -> None:
    assert math.floor(36.69 * 10) == 366  # floor, forbidden by FR-5.10
    assert encode_tenths(36.69) == 367
    assert round(0.25 * 10) == 2  # Python round() is half-to-even
    assert encode_tenths(0.25) == 3
    assert math.floor(-0.05 * 10) == -1 and math.floor(-0.04 * 10) == -1
    assert encode_tenths(-0.04) == 0


def test_fr5_10_encode_accepts_int_input() -> None:
    assert encode_tenths(37) == 370
    assert encode_tenths(-4) == -40
    assert encode_tenths(0) == 0


def test_fr5_10_encode_is_symmetric_around_zero() -> None:
    for value in (0.05, 0.15, 1.25, 36.6, 36.65, 70.0):
        assert encode_tenths(-value) == -encode_tenths(value)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_fr5_10_encode_non_finite_raises_value_error(bad: float) -> None:
    with pytest.raises(ValueError):
        encode_tenths(bad)


# ---------------------------------------------------------------------------------------------
# decode_tenths (FR-5.10, FR-9.3)
# ---------------------------------------------------------------------------------------------

DECODE_TABLE: list[tuple[str, float]] = [
    ("0370", 37.0),  # data of the ``1rat0370`` reply
    ("370", 37.0),
    ("366", 36.6),
    ("1999", 199.9),
    ("40", 4.0),
    ("700", 70.0),
    ("0", 0.0),
    ("1", 0.1),
    ("-50", -5.0),  # RLT may report a negative minimum (FR-7.2)
    ("-1", -0.1),
    ("+12", 1.2),
    ("-0", 0.0),
    ("000000", 0.0),
]


@pytest.mark.parametrize(("data", "celsius"), DECODE_TABLE)
def test_fr5_10_decode_table(data: str, celsius: float) -> None:
    result = decode_tenths(data)
    assert result == celsius
    assert isinstance(result, float)


def test_fr5_10_decode_is_int_div_10() -> None:
    for n in range(-2000, 2001, 7):
        assert decode_tenths(str(n)) == n / 10


@pytest.mark.parametrize("tenths", [-500, -1, 0, 1, 40, 366, 700, 1000, 1999])
def test_fr5_10_encode_decode_round_trip(tenths: int) -> None:
    celsius = decode_tenths(str(tenths))
    assert encode_tenths(celsius) == tenths


@pytest.mark.parametrize(
    "data",
    [
        "",
        " ",
        "+",
        "-",
        "12.5",
        "36.6",
        " 12",
        "12 ",
        "12\n",
        "\t12",
        "1_0",
        "1e3",
        "0x10",
        "abc",
        "12a",
        "--1",
        "+-1",
        "1-",
        "١٢",  # Arabic-Indic digits: int() accepts them, the wire never sends them
        "１２",  # full-width digits
        "nan",
        "inf",
    ],
)
def test_fr9_3_decode_non_integer_data_raises_data_parse_error(data: str) -> None:
    with pytest.raises(DataParseError):
        decode_tenths(data)


def test_fr9_3_data_parse_error_is_a_value_error() -> None:
    assert issubclass(DataParseError, ValueError)
    with pytest.raises(ValueError):
        decode_tenths("12.5")


# ---------------------------------------------------------------------------------------------
# Firmware tables (FR-5.6, FR-7.1, FR-8.11, FR-9.1, FR-9.3)
# ---------------------------------------------------------------------------------------------

# Protocol report §8.2, transcribed: error character -> meaning.
REPORT_8_2: dict[str, str] = {
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

# Protocol report §5.3 HSL v2.2 enum (reply of ``0RTD<D>``, D = 1..6).
REPORT_5_3: dict[int, str] = {
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


def test_fr9_1_every_8_2_code_is_present() -> None:
    expected = set("0123456789") | set("ABCDEFGHIJKLMNO") | {"R", "T", "W"}
    assert expected == set(REPORT_8_2)  # guard on the transcription
    assert set(FIRMWARE_ERRORS) == expected
    assert len(FIRMWARE_ERRORS) == 28


@pytest.mark.parametrize(("code", "meaning"), sorted(REPORT_8_2.items()))
def test_fr9_1_description_matches_report_8_2(code: str, meaning: str) -> None:
    assert FIRMWARE_ERRORS[code] == meaning


def test_fr9_1_every_description_is_non_empty_text() -> None:
    for code, description in FIRMWARE_ERRORS.items():
        assert len(code) == 1
        assert isinstance(description, str)
        assert description.strip()


@pytest.mark.parametrize("char", ["P", "Q", "S", "U", "V", "X", "Y", "Z", "a", "w", "#", "", " "])
def test_fr9_3_unknown_error_character_is_not_in_table(char: str) -> None:
    assert char not in FIRMWARE_ERRORS


def test_fr9_3_table_keys_are_upper_case_only() -> None:
    for code in FIRMWARE_ERRORS:
        assert code == code.upper()


def test_fr5_6_transient_set_is_exact() -> None:
    assert TRANSIENT_CODES == {"1", "2", "6", "9", "A", "D"}
    assert len(TRANSIENT_CODES) == 6


def test_fr5_6_transient_codes_are_firmware_codes_and_exclude_success() -> None:
    assert TRANSIENT_CODES <= set(FIRMWARE_ERRORS)
    assert "0" not in TRANSIENT_CODES


@pytest.mark.parametrize("code", ["3", "4", "5", "7", "8", "B", "C", "E", "R", "T", "W"])
def test_fr5_6_fail_fast_codes_are_not_transient(code: str) -> None:
    assert code not in TRANSIENT_CODES


def test_fr5_6_transient_set_is_immutable() -> None:
    assert isinstance(TRANSIENT_CODES, frozenset)


def test_fr7_1_device_types_match_report_5_3() -> None:
    assert DEVICE_TYPES == REPORT_5_3


@pytest.mark.parametrize(
    ("code", "name"),
    [
        (1, "CPAC"),
        (4, "CPAC 2TEC"),
        (5, "Heat PAC"),
        (64, "No device connected"),
        (128, "Empty slot"),
    ],
)
def test_fr7_1_named_device_type_codes(code: int, name: str) -> None:
    assert DEVICE_TYPES[code] == name


def test_fr7_1_device_types_are_not_the_off_by_one_mapping() -> None:
    """CHM-DE / ``InhecoTECController.hsi`` shift the enum by one (Thermoshake=1, CPAC=2)."""
    assert DEVICE_TYPES[1] != "Thermoshake"
    assert DEVICE_TYPES[2] != "CPAC"
    assert DEVICE_TYPES[0] == "Thermoshake"


@pytest.mark.parametrize("code", [11, 16, 63, 65, 127, 129, 255, -1])
def test_fr7_1_unlisted_device_codes_have_no_name(code: int) -> None:
    assert code not in DEVICE_TYPES


def test_fr8_11_controller_types_exact() -> None:
    assert CONTROLLER_TYPES == {0: "STC", 1: "MTC", 255: "not set"}


# ---------------------------------------------------------------------------------------------
# NFR-5 (platform-independent, pure stdlib) and NFR-8 (provenance)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", [crc, encoding, tables], ids=["crc", "encoding", "tables"])
def test_nfr5_protocol_primitives_import_only_stdlib(module: object) -> None:
    source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".", 1)[0])
    non_stdlib = {name for name in imported if name not in sys.stdlib_module_names}
    assert non_stdlib == set()

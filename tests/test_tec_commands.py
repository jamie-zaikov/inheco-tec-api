"""Tests for the C4 command allowlist (Task 4).

Covers FR-10 / FR-10.1 (only the 13 allowlisted mnemonics, parameters validated per operation,
raise before any HID I/O), FR-10.3 (key-protected, development and shaker mnemonics never encode),
FR-5.9 (controller-level commands only with address ``0``) and NFR-8 (section citations). The
expected address and parameter ranges are transcribed from design C4 and protocol report §2.1/§4,
not from the implementation.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest
from inheco_tec_api.protocol import commands
from inheco_tec_api.protocol.commands import (
    ALLOWED_MNEMONICS,
    Command,
    CommandNotAllowed,
    validate,
)
from inheco_tec_api.protocol.framing import encode_command

FR_10_1_ALLOWLIST = {
    "STT", "ATE", "RAT", "RTT", "RLT", "RMT", "RHV", "RTD", "RFV", "RDC", "RAN", "RHE", "AEO",
}  # fmt: skip

FR_10_3_FORBIDDEN = [
    # key-protected and development commands
    "SMT", "SLT", "SDT", "SAP", "SPO", "SDP", "SPP", "SPI", "SPD",
    # shaker commands
    "ASE", "SSR", "SSS", "RSR", "RSS", "RSE",
]  # fmt: skip

CONTROLLER_LEVEL = ["RTD", "RAN", "RFV", "RHV", "RDC", "AEO"]
SLOT_LEVEL = ["STT", "ATE", "RAT", "RTT", "RLT", "RHE", "RMT"]

# One valid parameter per mnemonic (design C4), used to isolate address checks.
VALID_PARAM = {
    "STT": "370", "ATE": "1", "RAT": "1", "RTT": "", "RLT": "", "RAN": "1", "RHE": "",
    "RMT": "1", "RHV": "0", "RTD": "1", "RFV": "1", "RDC": "1", "AEO": "",
}  # fmt: skip


def _encode_and_capture(cmd: Command) -> list[bytes]:
    """Stand-in for a HID write: collect every report the encoder hands out."""
    writes: list[bytes] = []
    writes.extend(encode_command(cmd))
    return writes


# --- Allowlist (FR-10, FR-10.1) ----------------------------------------------------------------


def test_fr10_1_allowed_mnemonics_are_exactly_the_requirement_list() -> None:
    assert set(ALLOWED_MNEMONICS) == FR_10_1_ALLOWLIST


def test_fr10_3_no_forbidden_mnemonic_is_allowlisted() -> None:
    assert not set(FR_10_3_FORBIDDEN) & set(ALLOWED_MNEMONICS)


@pytest.mark.parametrize("mnemonic", FR_10_3_FORBIDDEN)
@pytest.mark.parametrize(("address", "param"), [(0, ""), (1, ""), (1, "0"), (1, "1"), (6, "1234")])
def test_fr10_3_forbidden_mnemonic_raises_from_encoder_with_no_bytes(
    mnemonic: str, address: int, param: str
) -> None:
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(Command(address, mnemonic, param)))
    assert writes == []


@pytest.mark.parametrize("mnemonic", FR_10_3_FORBIDDEN)
def test_fr10_3_forbidden_mnemonic_raises_from_validate(mnemonic: str) -> None:
    with pytest.raises(CommandNotAllowed):
        validate(Command(1, mnemonic, ""))


@pytest.mark.parametrize("mnemonic", ["XYZ", "RCM", "RSN", "RRD", "RRS", "ADD", "", "STTX", "st"])
def test_fr10_1_other_unknown_mnemonics_raise(mnemonic: str) -> None:
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(Command(1, mnemonic, "")))
    assert writes == []


def test_fr10_1_allowlist_is_case_sensitive() -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, "stt", "370"))
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(0, "aeo", ""))


def test_fr10_3_key_carrying_parameter_raises() -> None:
    """A parameter carrying anything beyond the C4 rule (e.g. a key) is refused."""
    for cmd in (
        Command(1, "STT", "370KEY"),
        Command(0, "RFV", "1 1234"),
        Command(0, "AEO", "1234"),
        Command(1, "ATE", "1;0AEO"),
    ):
        with pytest.raises(CommandNotAllowed):
            encode_command(cmd)


def test_fr10_1_validate_rejects_non_command_and_bad_field_types() -> None:
    with pytest.raises(CommandNotAllowed):
        validate("1STT370")  # type: ignore[arg-type]
    with pytest.raises(CommandNotAllowed):
        validate(Command(True, "STT", "370"))  # bool address
    with pytest.raises(CommandNotAllowed):
        validate(Command("1", "STT", "370"))  # type: ignore[arg-type]
    with pytest.raises(CommandNotAllowed):
        validate(Command(1, "STT", 370))  # type: ignore[arg-type]


def test_fr10_1_command_is_immutable() -> None:
    cmd = commands.stt(1, 370)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cmd.mnemonic = "SMT"  # type: ignore[misc]


def test_fr10_1_command_not_allowed_is_a_value_error() -> None:
    assert issubclass(CommandNotAllowed, ValueError)


# --- Constructors: happy path and text (FR-10.1, design C4) ------------------------------------


@pytest.mark.parametrize(
    ("cmd", "address", "mnemonic", "param", "text"),
    [
        (commands.stt(1, 370), 1, "STT", "370", "1STT370"),
        (commands.ate(2, True), 2, "ATE", "1", "2ATE1"),
        (commands.ate(2, False), 2, "ATE", "0", "2ATE0"),
        (commands.rat(3, 3), 3, "RAT", "3", "3RAT3"),
        (commands.rtt(4), 4, "RTT", "", "4RTT"),
        (commands.rlt(5), 5, "RLT", "", "5RLT"),
        (commands.ran(6), 0, "RAN", "6", "0RAN6"),
        (commands.rhe(1), 1, "RHE", "", "1RHE"),
        (commands.rmt1(2), 2, "RMT", "1", "2RMT1"),
        (commands.rhv(8), 0, "RHV", "8", "0RHV8"),
        (commands.rtd(6), 0, "RTD", "6", "0RTD6"),
        (commands.rfv(4), 0, "RFV", "4", "0RFV4"),
        (commands.rdc1(), 0, "RDC", "1", "0RDC1"),
        (commands.aeo(), 0, "AEO", "", "0AEO"),
    ],
    ids=lambda v: v.text if isinstance(v, Command) else None,
)
def test_fr10_1_constructor_builds_expected_command(
    cmd: Command, address: int, mnemonic: str, param: str, text: str
) -> None:
    assert (cmd.address, cmd.mnemonic, cmd.param) == (address, mnemonic, param)
    assert cmd.text == text
    validate(cmd)
    assert _encode_and_capture(cmd)


# --- Constructors: parameter and address boundaries (FR-10.1) ----------------------------------

SLOT_CONSTRUCTORS: dict[str, Callable[[int], Command]] = {
    "stt": lambda slot: commands.stt(slot, 370),
    "ate": lambda slot: commands.ate(slot, True),
    "rat": lambda slot: commands.rat(slot, 1),
    "rtt": commands.rtt,
    "rlt": commands.rlt,
    "rhe": commands.rhe,
    "rmt1": commands.rmt1,
}


@pytest.mark.parametrize("name", sorted(SLOT_CONSTRUCTORS))
@pytest.mark.parametrize("slot", [1, 6])
def test_fr10_1_slot_constructor_accepts_slot_bounds(name: str, slot: int) -> None:
    cmd = SLOT_CONSTRUCTORS[name](slot)
    assert cmd.address == slot
    assert cmd.text.startswith(str(slot))


@pytest.mark.parametrize("name", sorted(SLOT_CONSTRUCTORS))
@pytest.mark.parametrize("slot", [0, 7, -1, 10])
def test_fr10_1_slot_constructor_rejects_slot_outside_1_to_6(name: str, slot: int) -> None:
    with pytest.raises(CommandNotAllowed):
        SLOT_CONSTRUCTORS[name](slot)


@pytest.mark.parametrize("name", sorted(SLOT_CONSTRUCTORS))
def test_fr10_1_slot_constructor_rejects_bool_slot(name: str) -> None:
    with pytest.raises(CommandNotAllowed):
        SLOT_CONSTRUCTORS[name](True)


@pytest.mark.parametrize("tenths", [0, 1, 370, 1000, 1999])
def test_fr10_1_stt_accepts_tenths_0_to_1999(tenths: int) -> None:
    assert commands.stt(1, tenths).param == str(tenths)


@pytest.mark.parametrize("tenths", [-1, 2000, 10000])
def test_fr10_1_stt_rejects_tenths_outside_0_to_1999(tenths: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.stt(1, tenths)


@pytest.mark.parametrize("tenths", [37.0, "370", True, None])
def test_fr10_1_stt_rejects_non_int_tenths(tenths: object) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.stt(1, tenths)  # type: ignore[arg-type]


@pytest.mark.parametrize("param", ["0370", "+370", "-0", " 370", "37.0", "", "2000", "١٢٣"])
def test_fr10_1_stt_hand_built_non_canonical_parameter_raises(param: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, "STT", param))


@pytest.mark.parametrize("on", [1, 0, "1", None])
def test_fr10_1_ate_requires_bool(on: object) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.ate(1, on)  # type: ignore[arg-type]


@pytest.mark.parametrize("param", ["2", "", "01", "10"])
def test_fr10_1_ate_hand_built_parameter_other_than_0_or_1_raises(param: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, "ATE", param))


@pytest.mark.parametrize("sel", [1, 2, 3])
def test_fr10_1_rat_accepts_selector_1_to_3(sel: int) -> None:
    assert commands.rat(1, sel).param == str(sel)


@pytest.mark.parametrize("sel", [0, 4, -1])
def test_fr10_1_rat_rejects_selector_outside_1_to_3(sel: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.rat(1, sel)


@pytest.mark.parametrize("mnemonic", ["RTT", "RLT", "RHE"])
@pytest.mark.parametrize("param", ["0", "1"])
def test_fr10_1_no_parameter_reads_reject_any_parameter(mnemonic: str, param: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, mnemonic, param))


@pytest.mark.parametrize("param", ["", "0", "2"])
def test_fr10_1_rmt_accepts_only_parameter_1(param: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(1, "RMT", param))


@pytest.mark.parametrize("sel", [0, 8])
def test_fr10_1_rhv_accepts_selector_0_to_8(sel: int) -> None:
    assert commands.rhv(sel).text == f"0RHV{sel}"


@pytest.mark.parametrize("sel", [-1, 9, 10])
def test_fr10_1_rhv_rejects_selector_outside_0_to_8(sel: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.rhv(sel)


@pytest.mark.parametrize("slot", [0, 6])
def test_fr10_1_rtd_accepts_slot_0_to_6(slot: int) -> None:
    assert commands.rtd(slot).text == f"0RTD{slot}"


@pytest.mark.parametrize("slot", [-1, 7])
def test_fr10_1_rtd_rejects_slot_outside_0_to_6(slot: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.rtd(slot)


@pytest.mark.parametrize("sel", [0, 4])
def test_fr10_1_rfv_accepts_selector_0_to_4(sel: int) -> None:
    assert commands.rfv(sel).text == f"0RFV{sel}"


@pytest.mark.parametrize("sel", [-1, 5])
def test_fr10_1_rfv_rejects_selector_outside_0_to_4(sel: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.rfv(sel)


@pytest.mark.parametrize("ctor", [commands.rhv, commands.rtd, commands.rfv])
def test_fr10_1_controller_selector_rejects_bool(ctor: Callable[[int], Command]) -> None:
    with pytest.raises(CommandNotAllowed):
        ctor(True)


@pytest.mark.parametrize("param", ["", "0", "2"])
def test_fr10_1_rdc_accepts_only_parameter_1(param: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(0, "RDC", param))


# --- AEO (FR-10.1) -----------------------------------------------------------------------------


def test_fr10_1_aeo_with_address_0_and_no_parameter_encodes() -> None:
    cmd = commands.aeo()
    assert (cmd.address, cmd.mnemonic, cmd.param) == (0, "AEO", "")
    assert _encode_and_capture(cmd) == _encode_and_capture(Command(0, "AEO", ""))


@pytest.mark.parametrize("address", [1, 2, 3, 4, 5, 6, 7, 9])
def test_fr10_1_aeo_with_nonzero_address_raises(address: int) -> None:
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(Command(address, "AEO", "")))
    assert writes == []


@pytest.mark.parametrize("param", ["0", "1", "00", " "])
def test_fr10_1_aeo_with_parameter_raises(param: str) -> None:
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(Command(0, "AEO", param)))
    assert writes == []


# --- Mainboard address (FR-5.9) ----------------------------------------------------------------


@pytest.mark.parametrize("mnemonic", CONTROLLER_LEVEL)
def test_fr5_9_controller_level_command_accepts_address_0(mnemonic: str) -> None:
    validate(Command(0, mnemonic, VALID_PARAM[mnemonic]))


@pytest.mark.parametrize("mnemonic", CONTROLLER_LEVEL)
@pytest.mark.parametrize("address", [1, 3, 6, 7])
def test_fr5_9_controller_level_command_rejects_nonzero_address(
    mnemonic: str, address: int
) -> None:
    """Never the ControllerID digit: ``3RFV1`` would reach slot 3 (protocol report §2.1)."""
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(Command(address, mnemonic, VALID_PARAM[mnemonic])))
    assert writes == []


def test_fr5_9_controller_constructors_always_use_address_0() -> None:
    for cmd in (
        commands.rhv(0),
        commands.rtd(3),
        commands.ran(2),
        commands.rfv(1),
        commands.rdc1(),
        commands.aeo(),
    ):
        assert cmd.address == 0
        assert cmd.text[0] == "0"


@pytest.mark.parametrize("mnemonic", SLOT_LEVEL)
def test_fr10_1_slot_level_command_rejects_address_0(mnemonic: str) -> None:
    with pytest.raises(CommandNotAllowed):
        encode_command(Command(0, mnemonic, VALID_PARAM[mnemonic]))

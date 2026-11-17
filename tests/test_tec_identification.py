"""Tests for C8 ``identification.py``: slot identification and startup slot state (Task 10).

Requirements: FR-6.7, FR-6.8, FR-7, FR-7.1, FR-7.2, FR-7.7, NFR-8, plus carry-forward T6-S3
(any exception from ``compute_limits`` fails the slot closed).

Every test drives the real engine over the fake transport (Task 8) through the recording wrapper
of ``tec_engine_helpers`` with injected virtual time. A reply with unparseable data is produced
with the fake's ``StaleReply`` action: its text echoes the command, so the engine accepts it as the
reply and the identification parser sees the bad data.
"""

from __future__ import annotations

import inheco_tec_api.identification as identification
import pytest
from inheco_tec_api.engine import DeviceLost
from inheco_tec_api.identification import (
    REASON_LIMITS_ERROR,
    SlotIdentification,
    StartupSlotState,
    identify_slot,
    read_startup_state,
)
from inheco_tec_api.limits import compute_limits
from inheco_tec_api.manifest import ArticleEntry, SlotConfig
from inheco_tec_api.transport.fake import FakeSlot, FakeTecDevice
from inheco_tec_api.transport.fake_actions import (
    Action,
    BadCrc,
    DeviceLoss,
    FirmwareCode,
    Match,
    NoReply,
    StaleReply,
)
from structlog.testing import capture_logs
from tec_engine_helpers import make_engine
from tec_manifest_helpers import EXPECTED_ARTICLES

ARTICLE_MAP: dict[str, ArticleEntry] = {
    key: ArticleEntry.model_validate({"class": cls, "variant": variant})
    for key, (cls, variant) in EXPECTED_ARTICLES.items()
}

IDENTIFY_COMMANDS = ["0RTD1", "0RDC1", "0RAN1", "0RDC1", "1RLT", "0RDC1", "1RMT1", "0RDC1"]
STARTUP_COMMANDS = ["1RHE", "0RDC1", "1RTT", "0RDC1", "1RAT3", "0RDC1"]

# Matchers for each read of slot 1, and the reply echo that StaleReply must carry.
READS: dict[str, tuple[Match, str]] = {
    "RTD": (Match(mnemonic="RTD", address=0, param="1"), "0rtd"),
    "RAN": (Match(mnemonic="RAN", address=0, param="1"), "0ran"),
    "RLT": (Match(mnemonic="RLT", address=1), "1rlt"),
    "RMT1": (Match(mnemonic="RMT", address=1, param="1"), "1rmt"),
    "RHE": (Match(mnemonic="RHE", address=1), "1rhe"),
    "RTT": (Match(mnemonic="RTT", address=1), "1rtt"),
    "RAT3": (Match(mnemonic="RAT", address=1, param="3"), "1rat"),
}

UNREAD_REASON = {
    "RTD": "device_type_unreadable",
    "RAN": "article_unreadable",
    "RLT": "firmware_limits_unreadable",
    "RMT1": "firmware_limits_unreadable",
}

# Transaction-level failures: each makes engine.execute raise TransactionFailed.
TRANSACTION_FAILURES: dict[str, Action] = {
    "firmware": FirmwareCode("3"),  # command not executable: fail fast
    "no_reply": NoReply(),  # retried until attempts run out
    "bad_crc": BadCrc(),  # retried until attempts run out
    "unknown_code": FirmwareCode("X"),  # outside the §8.2 table: parse failure
}


def cpac_slot(slot_id: int = 1) -> SlotConfig:
    return SlotConfig.model_validate({"id": slot_id, "class": "cpac"})


def two_tec_slot(slot_id: int = 1) -> SlotConfig:
    return SlotConfig.model_validate({"id": slot_id, "class": "cpac-2tec"})


def assert_no_state_change(dev: FakeTecDevice) -> None:
    """FR-6.7: no STT and no ATE (nor any other non-read command) was written."""
    for command in dev.commands:
        assert command[1:4] not in ("STT", "ATE", "AEO"), dev.commands


def assert_not_permitted(ident: SlotIdentification, reason: str) -> None:
    assert ident.limits.permitted is False
    assert ident.limits.reason == reason
    assert ident.limits.min_c is None
    assert ident.limits.max_c is None


# --- FR-7 / FR-7.1 / FR-7.2 / FR-7.7: command strings and happy path ----------------------------


def test_fr7_identify_writes_exactly_the_four_reads_each_with_resync() -> None:
    engine, _, dev, _ = make_engine()
    identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert dev.commands == IDENTIFY_COMMANDS


def test_fr7_identify_uses_slot_id_from_config() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[3] = FakeSlot()
    ident = identify_slot(engine, cpac_slot(3), ARTICLE_MAP)
    assert dev.commands == ["0RTD3", "0RDC1", "0RAN3", "0RDC1", "3RLT", "0RDC1", "3RMT1", "0RDC1"]
    assert ident.slot == 3
    assert ident.limits.permitted is True


def test_fr7_cpac_slot_identifies_permitted_with_type_article_variant() -> None:
    engine, _, _, _ = make_engine()
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.slot == 1
    assert ident.device_type == 1
    assert ident.device_type_name == "CPAC"
    assert ident.article == "7000179"
    assert ident.variant == "CPAC Microplate"
    assert ident.limits.permitted is True
    assert (ident.limits.min_c, ident.limits.max_c) == (4.0, 70.0)
    assert ident.limits.limit_class == "cpac"
    assert ident.limits.reason is None


def test_fr7_cpac_2tec_slot_with_article_7000163_and_type_4_is_permitted() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(device_type=4, article_number="7000163", rlt=40, rmt1=1100)
    ident = identify_slot(engine, two_tec_slot(), ARTICLE_MAP)
    assert ident.device_type == 4
    assert ident.device_type_name == "CPAC 2TEC"
    assert ident.article == "7000163"
    assert ident.variant == "CPAC Microplate HT 2TEC"
    assert ident.limits.permitted is True
    assert ident.limits.limit_class == "cpac-2tec"
    assert (ident.limits.min_c, ident.limits.max_c) == (4.0, 100.0)


def test_fr7_2_firmware_limits_read_in_tenths_and_rlt_signed() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(rlt=-50, rmt1=655)
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    # RLT -5.0 does not lower the class min; RMT1 65.5 lowers the max (FR-7.4).
    assert ident.limits.permitted is True
    assert (ident.limits.min_c, ident.limits.max_c) == (4.0, 65.5)


def test_fr7_2_firmware_min_above_class_min_raises_effective_min() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(rlt=120, rmt1=700)
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert (ident.limits.min_c, ident.limits.max_c) == (12.0, 70.0)


# --- FR-7.1: device-type naming with the HSL v2.2 enum ------------------------------------------


@pytest.mark.parametrize(
    ("device_type", "name", "reason"),
    [
        (1, "CPAC", None),
        (4, "CPAC 2TEC", "article_class_mismatch_device_type"),
        (0, "Thermoshake", "device_type_not_controllable"),
        (2, "Teleshake", "device_type_not_controllable"),
        (5, "Heat PAC", "device_type_not_controllable"),
        (64, "No device connected", "device_type_not_controllable"),
        (11, None, "device_type_not_controllable"),  # not in §5.3: no name
    ],
)
def test_fr7_1_device_type_named_with_hsl_enum_not_off_by_one(
    device_type: int, name: str | None, reason: str | None
) -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1].device_type = device_type
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.device_type == device_type
    assert ident.device_type_name == name
    assert ident.limits.reason == reason
    assert ident.limits.permitted is (reason is None)


def test_fr7_1_empty_slot_type_128_is_named_empty_slot_and_not_permitted() -> None:
    engine, _, dev, _ = make_engine()
    del dev.slots[1]  # the fake answers 0RTD1 with 128; slot reads get code 7
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.device_type == 128
    assert ident.device_type_name == "Empty slot"
    assert ident.article is None
    assert_not_permitted(ident, "device_type_not_controllable")
    assert_no_state_change(dev)


# --- FR-7.7: article number ----------------------------------------------------------------------


@pytest.mark.parametrize("article", ["70A0179", "700017", "70001790", " 7000179", "7000179 "])
def test_fr7_7_non_numeric_or_wrong_length_ran_is_article_malformed(article: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1].article_number = article
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.article == article  # raw data, not trimmed
    assert_not_permitted(ident, "article_malformed")


def test_fr7_7_unmapped_article_is_article_unmapped() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1].article_number = "1234567"
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.variant is None
    assert_not_permitted(ident, "article_unmapped")


def test_fr7_7_article_class_differs_from_config_reports_variant() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1].article_number = "7000163"  # cpac-2tec article on a cpac slot
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.variant == "CPAC Microplate HT 2TEC"
    assert_not_permitted(ident, "article_class_mismatch_config")


# --- FR-7 / FR-7.5: each identification read failing individually ------------------------------


def test_fr7_2_scripted_firmware_error_on_rlt_is_firmware_limits_unreadable() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS["RLT"][0], action=FirmwareCode("3"))
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert_not_permitted(ident, "firmware_limits_unreadable")
    assert ident.device_type == 1
    assert ident.article == "7000179"
    assert ident.variant == "CPAC Microplate"
    # The failed read has no resync; every other read still runs.
    assert dev.commands == ["0RTD1", "0RDC1", "0RAN1", "0RDC1", "1RLT", "1RMT1", "0RDC1"]
    assert_no_state_change(dev)


@pytest.mark.parametrize("failure", sorted(TRANSACTION_FAILURES))
@pytest.mark.parametrize("read", sorted(UNREAD_REASON))
def test_fr7_5_transaction_failure_on_one_read_records_it_unread(read: str, failure: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS[read][0], action=TRANSACTION_FAILURES[failure], times=None)
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert_not_permitted(ident, UNREAD_REASON[read])
    assert (ident.device_type is None) is (read == "RTD")
    assert (ident.article is None) is (read == "RAN")
    assert (ident.device_type_name is None) is (read == "RTD")
    # Every one of the four reads was still attempted.
    written = {cmd for cmd in dev.commands if cmd != "0RDC1"}
    assert written == {"0RTD1", "0RAN1", "1RLT", "1RMT1"}
    assert_no_state_change(dev)


@pytest.mark.parametrize(
    ("read", "data"),
    [("RTD", "Z"), ("RTD", "-1"), ("RTD", " 1"), ("RLT", "4.0"), ("RLT", "x40"), ("RMT1", "")],
)
def test_fr7_5_unparseable_reply_data_records_value_unread(read: str, data: str) -> None:
    engine, _, dev, _ = make_engine()
    match, echo = READS[read]
    dev.script(match=match, action=StaleReply(f"{echo}0{data}"))
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert_not_permitted(ident, UNREAD_REASON[read])
    if read == "RTD":
        assert ident.device_type is None
        assert ident.device_type_name is None
    assert_no_state_change(dev)


def test_fr7_2_signed_plus_rlt_is_accepted() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS["RLT"][0], action=StaleReply("1rlt0+50"))
    ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert ident.limits.permitted is True
    assert ident.limits.min_c == 5.0


@pytest.mark.parametrize("read", sorted(UNREAD_REASON))
def test_fr6_4_device_lost_propagates_from_each_identification_read(read: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS[read][0], action=DeviceLoss())
    with pytest.raises(DeviceLost):
        identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert_no_state_change(dev)


# --- T6-S3: compute_limits raising fails the slot closed ----------------------------------------


@pytest.mark.parametrize("exc_type", [RuntimeError, ValueError, KeyError, ZeroDivisionError])
def test_t6s3_compute_limits_exception_gives_not_permitted_and_error_event(
    monkeypatch: pytest.MonkeyPatch, exc_type: type[Exception]
) -> None:
    secret_text = "detail-that-must-not-be-logged"  # pragma: allowlist secret

    def boom(*_args: object, **_kwargs: object) -> None:
        raise exc_type(secret_text)

    monkeypatch.setattr(identification, "compute_limits", boom)
    engine, _, dev, _ = make_engine(controller_id=3)
    dev.slots[1] = FakeSlot()
    with capture_logs() as events:
        ident = identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert_not_permitted(ident, REASON_LIMITS_ERROR)
    assert REASON_LIMITS_ERROR == "limits_computation_failed"
    assert ident.limits.limit_class is None
    assert ident.variant is None
    assert ident.device_type == 1
    assert ident.article == "7000179"
    errors = [e for e in events if e["event"] == "tec_identification_error"]
    assert len(errors) == 1
    event = errors[0]
    assert event["log_level"] == "error"
    assert set(event) - {"event", "log_level"} == {"controller_id", "slot", "error"}
    assert (event["controller_id"], event["slot"], event["error"]) == (3, 1, exc_type.__name__)
    assert secret_text not in repr(events)


def test_t6s3_compute_limits_receives_the_parsed_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, ...]] = []
    real = compute_limits

    def spy(*args: object) -> object:
        seen.append(args[2:])
        return real(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(identification, "compute_limits", spy)
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(rlt=-50, rmt1=655)
    identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    assert seen == [(1, "7000179", -50, 655)]


# --- FR-6.8 / FR-6.7: startup slot state --------------------------------------------------------


def test_fr6_8_startup_writes_exactly_rhe_rtt_rat3_each_with_resync() -> None:
    engine, _, dev, _ = make_engine()
    read_startup_state(engine, 1)
    assert dev.commands == STARTUP_COMMANDS


@pytest.mark.parametrize(
    ("actual", "expected_state"),
    [(250, "cooling"), (40, "heating"), (20, "heating")],
)
def test_fr6_8_slot_preset_on_at_4_c_reports_fake_derived_state(
    actual: int, expected_state: str
) -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(target=40, enabled=True, actual=actual)
    assert dev.slots[1].heater_state == {"heating": 0, "cooling": 1}[expected_state]
    state = read_startup_state(engine, 1)
    assert state == StartupSlotState(
        control_state=expected_state,  # type: ignore[arg-type]
        target_c=4.0,
        actual_c=actual / 10,
    )
    # FR-6.7: leave as found — no STT/ATE, the slot stays on at its target.
    assert_no_state_change(dev)
    assert dev.slots[1].enabled is True
    assert dev.slots[1].target == 40


def test_fr6_8_slot_off_reports_off() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(target=370, enabled=False, actual=251)
    state = read_startup_state(engine, 1)
    assert state == StartupSlotState(control_state="off", target_c=37.0, actual_c=25.1)


@pytest.mark.parametrize(
    ("rhe_data", "expected"),
    [("0", "heating"), ("1", "cooling"), ("2", "off"), ("3", "unknown"), ("9", "unknown")],
)
def test_fr6_8_rhe_value_maps_to_control_state(rhe_data: str, expected: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS["RHE"][0], action=StaleReply(f"1rhe0{rhe_data}"))
    assert read_startup_state(engine, 1).control_state == expected


@pytest.mark.parametrize("data", ["", "X", "-1", "01 "])
def test_fr6_8_rhe_unparseable_is_unknown(data: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS["RHE"][0], action=StaleReply(f"1rhe0{data}"))
    assert read_startup_state(engine, 1).control_state == "unknown"


@pytest.mark.parametrize("failure", sorted(TRANSACTION_FAILURES))
def test_fr6_8_rhe_unread_is_unknown_never_off(failure: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(target=40, enabled=True, actual=250)
    dev.script(match=READS["RHE"][0], action=TRANSACTION_FAILURES[failure], times=None)
    state = read_startup_state(engine, 1)
    assert state == StartupSlotState(control_state="unknown", target_c=4.0, actual_c=25.0)
    assert_no_state_change(dev)


@pytest.mark.parametrize("failure", sorted(TRANSACTION_FAILURES))
@pytest.mark.parametrize("read", ["RTT", "RAT3"])
def test_fr6_8_rtt_or_rat3_unread_is_none(read: str, failure: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(target=40, enabled=True, actual=250)
    dev.script(match=READS[read][0], action=TRANSACTION_FAILURES[failure], times=None)
    state = read_startup_state(engine, 1)
    assert state.control_state == "cooling"
    assert state.target_c == (None if read == "RTT" else 4.0)
    assert state.actual_c == (None if read == "RAT3" else 25.0)


@pytest.mark.parametrize("read", ["RTT", "RAT3"])
def test_fr6_8_rtt_or_rat3_unparseable_is_none(read: str) -> None:
    engine, _, dev, _ = make_engine()
    match, echo = READS[read]
    dev.script(match=match, action=StaleReply(f"{echo}0abc"))
    state = read_startup_state(engine, 1)
    assert (state.target_c if read == "RTT" else state.actual_c) is None


def test_fr6_8_negative_actual_decoded_signed() -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS["RAT3"][0], action=StaleReply("1rat0-015"))
    assert read_startup_state(engine, 1).actual_c == -1.5


@pytest.mark.parametrize("read", ["RHE", "RTT", "RAT3"])
def test_fr6_4_device_lost_propagates_from_each_startup_read(read: str) -> None:
    engine, _, dev, _ = make_engine()
    dev.script(match=READS[read][0], action=DeviceLoss())
    with pytest.raises(DeviceLost):
        read_startup_state(engine, 1)
    assert_no_state_change(dev)


def test_fr6_7_identify_then_startup_on_active_slot_writes_no_state_change() -> None:
    engine, _, dev, _ = make_engine()
    dev.slots[1] = FakeSlot(target=40, enabled=True, actual=250)
    identify_slot(engine, cpac_slot(), ARTICLE_MAP)
    read_startup_state(engine, 1)
    assert dev.commands == IDENTIFY_COMMANDS + STARTUP_COMMANDS
    assert dev.slots[1].enabled is True

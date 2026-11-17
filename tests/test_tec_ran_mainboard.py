"""Tests for amendment A6: ``RAN`` is a mainboard command, ``0RAN<slot>`` (Task 27).

Requirements: FR-10.1 and FR-5.9 (allowlist: ``RAN`` only at address ``0`` with the slot ``1``-``6``
as the parameter), FR-13.1 (the fake answers ``RAN`` as the real controller does, design C6),
FR-7 / FR-7.7 / FR-7.8 (identification sends ``0RAN<slot>`` and never ``<slot>RAN``; the observed
hardware reply ``0ran07000190`` identifies a permitted ``cpac`` slot, design C8, sequence S1) and
NFR-8 (no item-11 INFERRED marker remains). The expected strings are transcribed from the
2026-10-13 read-only probe recorded in api/README.md checklist item 11, not from the
implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from inheco_tec_api.identification import identify_slot
from inheco_tec_api.manifest import SlotConfig, load_manifest
from inheco_tec_api.protocol import commands
from inheco_tec_api.protocol.commands import Command, CommandNotAllowed, validate
from inheco_tec_api.protocol.framing import ReplyAssembler, encode_command, parse_reply
from inheco_tec_api.transport.fake import FakeSlot, FakeTecDevice, encode_reply
from tec_engine_helpers import make_engine
from tec_fake_helpers import opened, send_one, send_raw

OBSERVED_REPLY = "0ran07000190"  # checklist item 11, 2026-10-13
OBSERVED_ARTICLE = "7000190"
SLOT_RAN = re.compile(r"^[1-6]RAN")


# --- (a) Allowlist (FR-10.1, FR-5.9, design C4) ------------------------------------------------


@pytest.mark.parametrize("param", ["1", "2", "3", "4", "5", "6"])
def test_fr10_1_mainboard_ran_with_slot_parameter_encodes(param: str) -> None:
    cmd = Command(0, "RAN", param)
    validate(cmd)
    assert encode_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        Command(0, "RAN", ""),  # 0RAN: observed error 5
        Command(0, "RAN", "0"),  # 0RAN0: observed error 5
        Command(0, "RAN", "7"),
        Command(0, "RAN", "10"),
        Command(0, "RAN", "01"),
        Command(1, "RAN", ""),  # 1RAN: the old slot-level form, observed error 4
        Command(1, "RAN", "1"),
        Command(2, "RAN", ""),
        Command(6, "RAN", "6"),
        Command(7, "RAN", "1"),
    ],
    ids=lambda c: c.text,
)
def test_fr10_1_ran_outside_the_mainboard_rule_raises_with_no_report(cmd: Command) -> None:
    writes: list[bytes] = []
    with pytest.raises(CommandNotAllowed):
        writes.extend(encode_command(cmd))
    assert writes == []
    with pytest.raises(CommandNotAllowed):
        validate(cmd)


@pytest.mark.parametrize("slot", [1, 2, 3, 4, 5, 6])
def test_fr5_9_ran_constructor_builds_mainboard_form(slot: int) -> None:
    cmd = commands.ran(slot)
    assert (cmd.address, cmd.mnemonic, cmd.param) == (0, "RAN", str(slot))
    assert cmd.text == f"0RAN{slot}"


def test_fr10_1_ran_constructor_bounds_text() -> None:
    assert commands.ran(1).text == "0RAN1"
    assert commands.ran(6).text == "0RAN6"


@pytest.mark.parametrize("slot", [0, 7, -1, 10])
def test_fr10_1_ran_constructor_rejects_slot_outside_1_to_6(slot: int) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.ran(slot)


@pytest.mark.parametrize("slot", [True, False, 1.0, "1", None])
def test_fr10_1_ran_constructor_rejects_non_int_slot(slot: object) -> None:
    with pytest.raises(CommandNotAllowed):
        commands.ran(slot)  # type: ignore[arg-type]


# --- (b) Fake answers RAN as the real controller does (FR-13.1, design C6) ---------------------


def _two_slot_fake() -> FakeTecDevice:
    return opened(
        FakeTecDevice(
            slots={
                1: FakeSlot(article_number="7000190"),
                2: FakeSlot(device_type=4, article_number="7000163"),
            }
        )
    )


@pytest.mark.parametrize(("slot", "article"), [(1, "7000190"), (2, "7000163")])
def test_fr13_1_fake_mainboard_ran_returns_that_slots_article(slot: int, article: str) -> None:
    reply = send_one(_two_slot_fake(), commands.ran(slot))
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == (
        "0",
        "ran",
        "0",
        article,
    )


def test_fr13_1_fake_answers_the_observed_reply_text_for_7000190() -> None:
    replies = send_raw(_two_slot_fake(), "0RAN1")
    assert len(replies) == 1
    reply = replies[0]
    assert reply.address + reply.mnemonic + reply.error_char + reply.data == OBSERVED_REPLY


@pytest.mark.parametrize("text", ["0RAN", "0RAN0", "0RAN7", "0RAN9"])
def test_fr13_1_fake_mainboard_ran_bad_parameter_is_error_5(text: str) -> None:
    replies = send_raw(_two_slot_fake(), text)
    assert [(r.address, r.mnemonic, r.error_char, r.data) for r in replies] == [
        ("0", "ran", "5", "")
    ]


@pytest.mark.parametrize("text", ["1RAN", "2RAN", "1RAN1", "3RAN", "6RAN"])
def test_fr13_1_fake_slot_addressed_ran_is_error_4(text: str) -> None:
    # 3RAN and 6RAN address slots that are not in the model: still 4, never 7 (C6).
    replies = send_raw(_two_slot_fake(), text)
    assert len(replies) == 1
    assert replies[0].error_char == "4"
    assert replies[0].data == ""


@pytest.mark.parametrize("slot", [3, 4, 5, 6])
def test_fr13_1_fake_mainboard_ran_for_slot_not_in_model_is_error_7(slot: int) -> None:
    reply = send_one(_two_slot_fake(), commands.ran(slot))
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == ("0", "ran", "7", "")


def test_fr13_1_fake_ran_leaves_slot_state_unchanged() -> None:
    dev = _two_slot_fake()
    before = {sid: (s.target, s.enabled) for sid, s in dev.slots.items()}
    send_one(dev, commands.ran(1))
    send_raw(dev, "1RAN")
    send_raw(dev, "0RAN0")
    assert {sid: (s.target, s.enabled) for sid, s in dev.slots.items()} == before


# --- (c) Identification sends only the mainboard form (FR-7.7, sequence S1) --------------------


def _cpac_slot_config(slot_id: int) -> SlotConfig:
    return SlotConfig.model_validate({"id": slot_id, "class": "cpac"})


@pytest.mark.parametrize("slot", [1, 2])
def test_fr7_7_identify_writes_mainboard_ran_and_never_slot_ran(slot: int) -> None:
    manifest = load_manifest(_shipped_manifest_path())
    engine, _, dev, _ = make_engine()
    ident = identify_slot(engine, _cpac_slot_config(slot), manifest.article_numbers)
    assert f"0RAN{slot}" in dev.commands
    assert [c for c in dev.commands if SLOT_RAN.match(c)] == []
    reads = [c for c in dev.commands if c != "0RDC1"]
    assert reads == [f"0RTD{slot}", f"0RAN{slot}", f"{slot}RLT", f"{slot}RMT1"]
    assert ident.article == "7000179"  # the fake's default article for the slot


def test_fr7_7_identify_on_both_slots_never_sends_slot_ran() -> None:
    manifest = load_manifest(_shipped_manifest_path())
    engine, _, dev, _ = make_engine()
    for slot in (1, 2):
        identify_slot(engine, _cpac_slot_config(slot), manifest.article_numbers)
    assert [c for c in dev.commands if "RAN" in c] == ["0RAN1", "0RAN2"]
    assert not any(SLOT_RAN.match(c) for c in dev.commands)


# --- (d) The observed hardware reply (FR-7.7 acceptance, FR-4.7, FR-7.8) -----------------------


def _shipped_manifest_path() -> Path:
    # instrument.yaml sits at the repository root, the parent of the tests folder.
    return Path(__file__).resolve().parents[1] / "instrument.yaml"


def test_fr4_7_observed_reply_text_frames_and_parses() -> None:
    assembler = ReplyAssembler()
    payloads = encode_reply(OBSERVED_REPLY)
    done = [assembler.feed(p) for p in payloads]
    assert done[-1] is True
    assert not any(done[:-1])
    text = assembler.finish()
    assert text == OBSERVED_REPLY
    reply = parse_reply(text)
    assert (reply.address, reply.mnemonic, reply.error_char, reply.data) == (
        "0",
        "ran",
        "0",
        OBSERVED_ARTICLE,
    )


def test_fr7_7_observed_article_identifies_permitted_cpac_with_shipped_manifest() -> None:
    manifest = load_manifest(_shipped_manifest_path())
    controller = manifest.controllers[0]
    slot_cfg = next(s for s in controller.slots if s.id == 1)
    device = FakeTecDevice(slots={1: FakeSlot(device_type=1, article_number=OBSERVED_ARTICLE)})
    engine, _, dev, _ = make_engine(device=device)
    ident = identify_slot(engine, slot_cfg, manifest.article_numbers)
    assert ident.article == OBSERVED_ARTICLE
    assert ident.limits.permitted is True
    assert ident.limits.limit_class == "cpac"
    assert ident.variant == "CPAC Ultraflat"
    assert ident.limits.reason is None
    # FR-7.8: the window is the cpac class window (fake RLT 4.0, RMT1 70.0, config 4.0-70.0).
    assert (ident.limits.min_c, ident.limits.max_c) == (4.0, 70.0)
    assert "0RAN1" in dev.commands

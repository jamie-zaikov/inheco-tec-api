"""Tests for C8 ``compute_limits`` (Task 6).

Table-driven over the FR-7 acceptance table in ``requirements.md``. Covers FR-7 (identify before
permitting), FR-7.3 (class bounds, hard cap), FR-7.4 (effective window), FR-7.5 (fail closed with
a reason, first failure wins), FR-7.6 (never above ``RMT1`` nor below ``RLT``), FR-7.7 (class and
variant from the article map) and FR-7.8 (article number fail closed). Expected values come from
the spec (``requirements.md`` FR-7, ``design.md`` C8, ``temperature-limits-research.md``), not
from the implementation. ``RLT``/``RMT1`` are integer tenths of °C (FR-7.2).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import pytest
from inheco_tec_api.limits import SlotLimits, compute_limits
from inheco_tec_api.manifest import ArticleEntry, SlotConfig
from tec_manifest_helpers import EXPECTED_ARTICLES

ARTICLE_MAP: dict[str, ArticleEntry] = {
    key: ArticleEntry.model_validate({"class": cls, "variant": variant})
    for key, (cls, variant) in EXPECTED_ARTICLES.items()
}
"""The FR-2.7 article-number map, as the manifest would hold it."""

CPAC_ARTICLE = "7000179"
TWO_TEC_ARTICLE = "7000163"


def slot_cfg(limit_class: str = "cpac", **extra: Any) -> SlotConfig:
    """A validated slot config; ``min_c``/``max_c`` default per DD-12 unless given."""
    return SlotConfig.model_validate({"id": 1, "class": limit_class, **extra})


@dataclass(frozen=True)
class Reads:
    """Identification reads of one slot; ``None`` means unread."""

    rtd: int | None = 1
    ran: str | None = CPAC_ARTICLE
    rlt: int | None = 40
    rmt1: int | None = 700
    cfg: dict[str, Any] = field(default_factory=lambda: {"min_c": 4.0, "max_c": 70.0})
    limit_class: str = "cpac"


def run(reads: Reads) -> SlotLimits:
    cfg = slot_cfg(reads.limit_class, **reads.cfg)
    return compute_limits(cfg, ARTICLE_MAP, reads.rtd, reads.ran, reads.rlt, reads.rmt1)


def assert_blocked(result: SlotLimits, reason: str) -> None:
    assert result.permitted is False
    assert result.reason == reason
    assert result.min_c is None
    assert result.max_c is None


# ---------------------------------------------------------------------------------------------
# FR-7.4 / FR-7.3 / FR-7.7: permitted rows of the FR-7 acceptance table
# ---------------------------------------------------------------------------------------------

TWO_TEC: dict[str, Any] = {
    "rtd": 4,
    "ran": TWO_TEC_ARTICLE,
    "limit_class": "cpac-2tec",
    "rmt1": 1100,
}

PERMITTED_ROWS: list[tuple[str, Reads, float, float, str, str]] = [
    ("type1_rlt40_rmt700_cfg4_70", Reads(), 4.0, 70.0, "cpac", "CPAC Microplate"),
    (
        "cfg10_50",
        Reads(cfg={"min_c": 10.0, "max_c": 50.0}),
        10.0,
        50.0,
        "cpac",
        "CPAC Microplate",
    ),
    ("rmt1_650", Reads(rmt1=650), 4.0, 65.0, "cpac", "CPAC Microplate"),
    (
        "type4_cfg_max110",
        Reads(**TWO_TEC, cfg={"min_c": 4.0, "max_c": 110.0}),
        4.0,
        110.0,
        "cpac-2tec",
        "CPAC Microplate HT 2TEC",
    ),
    ("article_7000179_type1", Reads(ran="7000179"), 4.0, 70.0, "cpac", "CPAC Microplate"),
    ("article_7000190_type1", Reads(ran="7000190"), 4.0, 70.0, "cpac", "CPAC Ultraflat"),
    (
        "article_7000163_type4",
        Reads(**TWO_TEC, cfg={}),
        4.0,
        100.0,  # DD-12 default configured max of cpac-2tec (FR-7.3)
        "cpac-2tec",
        "CPAC Microplate HT 2TEC",
    ),
    ("rlt_above_cfg_min", Reads(rlt=100), 10.0, 70.0, "cpac", "CPAC Microplate"),
]


@pytest.mark.parametrize(
    ("reads", "min_c", "max_c", "limit_class", "variant"),
    [row[1:] for row in PERMITTED_ROWS],
    ids=[row[0] for row in PERMITTED_ROWS],
)
def test_fr7_4_acceptance_table_permitted(
    reads: Reads, min_c: float, max_c: float, limit_class: str, variant: str
) -> None:
    result = run(reads)
    assert result.permitted is True
    assert result.reason is None
    assert result.min_c == min_c
    assert result.max_c == max_c
    assert result.limit_class == limit_class
    assert result.variant == variant


# ---------------------------------------------------------------------------------------------
# FR-7.5 / FR-7.8: not-permitted rows of the FR-7 acceptance table
# ---------------------------------------------------------------------------------------------

BLOCKED_ROWS: list[tuple[str, Reads, str]] = [
    ("type_unread", Reads(rtd=None), "device_type_unreadable"),
    ("type5_heatpac", Reads(rtd=5), "device_type_not_controllable"),
    ("type128_empty_slot", Reads(rtd=128), "device_type_not_controllable"),
    ("type64_no_device", Reads(rtd=64), "device_type_not_controllable"),
    ("type0", Reads(rtd=0), "device_type_not_controllable"),
    ("type2", Reads(rtd=2), "device_type_not_controllable"),
    ("type_negative", Reads(rtd=-1), "device_type_not_controllable"),
    # Type/class mismatch: type 4 reported on a cpac slot holding a cpac article.
    ("type4_on_cpac_slot", Reads(rtd=4), "article_class_mismatch_device_type"),
    # Type 1 reported on a cpac-2tec slot holding a cpac-2tec article.
    (
        "type1_on_2tec_slot_2tec_article",
        Reads(rtd=1, ran=TWO_TEC_ARTICLE, limit_class="cpac-2tec", cfg={}),
        "article_class_mismatch_device_type",
    ),
    # 7000163 (cpac-2tec) with type 1 on the shipped cpac slot: the config check runs first.
    ("article_7000163_type1", Reads(ran=TWO_TEC_ARTICLE), "article_class_mismatch_config"),
    # A cpac article on a cpac-2tec slot whose device type agrees with the slot.
    (
        "cpac_article_on_2tec_slot",
        Reads(rtd=4, ran=CPAC_ARTICLE, limit_class="cpac-2tec", cfg={}),
        "article_class_mismatch_config",
    ),
    ("rlt_unread", Reads(rlt=None), "firmware_limits_unreadable"),
    ("rmt1_unread", Reads(rmt1=None), "firmware_limits_unreadable"),
    ("both_firmware_unread", Reads(rlt=None, rmt1=None), "firmware_limits_unreadable"),
    ("article_unmapped_7000999", Reads(ran="7000999"), "article_unmapped"),
    ("article_unread", Reads(ran=None), "article_unreadable"),
    # Empty window: firmware max 8.0 below the configured min 10.0.
    (
        "empty_window_rmt1_below_cfg_min",
        Reads(rmt1=80, cfg={"min_c": 10.0, "max_c": 50.0}),
        "empty_window",
    ),
    # Empty window: firmware min 60.0 above the configured max 50.0.
    (
        "empty_window_rlt_above_cfg_max",
        Reads(rlt=600, cfg={"min_c": 10.0, "max_c": 50.0}),
        "empty_window",
    ),
    # Empty window: firmware min above firmware max.
    ("empty_window_rlt_above_rmt1", Reads(rlt=500, rmt1=400), "empty_window"),
    # Firmware max below the class minimum 4.0.
    ("empty_window_rmt1_below_class_min", Reads(rmt1=30), "empty_window"),
]


@pytest.mark.parametrize(
    ("reads", "reason"), [row[1:] for row in BLOCKED_ROWS], ids=[row[0] for row in BLOCKED_ROWS]
)
def test_fr7_5_acceptance_table_not_permitted(reads: Reads, reason: str) -> None:
    assert_blocked(run(reads), reason)


@pytest.mark.parametrize(
    "ran",
    [
        "ABCDEFG",
        "700017a",
        "70001.9",
        "-700017",
        "+700017",
        "700017",  # 6 digits
        "70001790",  # 8 digits
        "",
        " 7000179",
        "7000179 ",
        "7000179\r",
        "7 00179",
        "７０００１７９",  # full-width digits, not ASCII
        "٧٠٠٠١٧٩",  # Arabic-Indic digits, not ASCII
    ],
)
def test_fr7_8_non_numeric_or_wrong_length_ran_is_malformed(ran: str) -> None:
    assert_blocked(run(Reads(ran=ran)), "article_malformed")


def test_fr7_8_seven_digit_unmapped_article_is_unmapped_not_malformed() -> None:
    assert_blocked(run(Reads(ran="0000000")), "article_unmapped")


# ---------------------------------------------------------------------------------------------
# FR-7.5: check order, first failure wins
# ---------------------------------------------------------------------------------------------

ORDER_ROWS: list[tuple[str, Reads, str]] = [
    (
        "type_unread_beats_everything",
        Reads(rtd=None, ran=None, rlt=None, rmt1=None),
        "device_type_unreadable",
    ),
    (
        "not_controllable_beats_article",
        Reads(rtd=5, ran="bad", rlt=None),
        "device_type_not_controllable",
    ),
    ("article_unread_beats_firmware", Reads(ran=None, rlt=None), "article_unreadable"),
    ("malformed_beats_firmware", Reads(ran="12ab", rmt1=None), "article_malformed"),
    ("unmapped_beats_firmware", Reads(ran="7000999", rlt=None), "article_unmapped"),
    (
        "unmapped_beats_type_mismatch",
        Reads(rtd=4, ran="7000999"),
        "article_unmapped",
    ),
    (
        "config_mismatch_beats_type_mismatch",
        Reads(rtd=4, ran=TWO_TEC_ARTICLE, limit_class="cpac"),
        "article_class_mismatch_config",
    ),
    (
        "config_mismatch_beats_firmware",
        Reads(ran=TWO_TEC_ARTICLE, rlt=None),
        "article_class_mismatch_config",
    ),
    (
        "type_mismatch_beats_firmware",
        Reads(rtd=4, rlt=None, rmt1=None),
        "article_class_mismatch_device_type",
    ),
    (
        "type_mismatch_beats_empty_window",
        Reads(rtd=4, rmt1=10),
        "article_class_mismatch_device_type",
    ),
    (
        "firmware_unread_beats_empty_window",
        Reads(rlt=None, rmt1=10),
        "firmware_limits_unreadable",
    ),
]


@pytest.mark.parametrize(
    ("reads", "reason"), [row[1:] for row in ORDER_ROWS], ids=[row[0] for row in ORDER_ROWS]
)
def test_fr7_5_check_order_first_failure_wins(reads: Reads, reason: str) -> None:
    assert_blocked(run(reads), reason)


# ---------------------------------------------------------------------------------------------
# FR-7.3: class clamp holds even for a config outside the class bounds
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit_class", "rtd", "ran", "expected_max"),
    [("cpac", 1, CPAC_ARTICLE, 70.0), ("cpac-2tec", 4, TWO_TEC_ARTICLE, 110.0)],
)
def test_fr7_3_class_clamp_with_out_of_bounds_config(
    limit_class: str, rtd: int, ran: str, expected_max: float
) -> None:
    # model_construct skips validation, so the config lies outside the class bounds.
    cfg = SlotConfig.model_construct(
        id=1, limit_class=limit_class, min_c=0.0, max_c=200.0, at_target_tolerance_c=1.0
    )
    result = compute_limits(cfg, ARTICLE_MAP, rtd, ran, -100, 1999)
    assert result.permitted is True
    assert result.min_c == 4.0
    assert result.max_c == expected_max


def test_fr7_3_cpac_2tec_never_above_hard_cap_even_with_high_rmt1() -> None:
    result = run(Reads(**{**TWO_TEC, "rmt1": 1999}, cfg={"min_c": 4.0, "max_c": 110.0}))
    assert result.permitted is True
    assert result.max_c == 110.0


# ---------------------------------------------------------------------------------------------
# FR-7.2 / FR-7.4: negative RLT
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rlt", [-127, -50, -1, 0])
def test_fr7_4_negative_rlt_does_not_lower_class_min(rlt: int) -> None:
    result = run(Reads(rlt=rlt))
    assert result.permitted is True
    assert result.min_c == 4.0
    assert result.max_c == 70.0


# ---------------------------------------------------------------------------------------------
# FR-7.6: never above RMT1, never below RLT (property-style sweep)
# ---------------------------------------------------------------------------------------------

_CONFIGS: list[tuple[str, int, str, float, float]] = [
    ("cpac", 1, CPAC_ARTICLE, 4.0, 70.0),
    ("cpac", 1, CPAC_ARTICLE, 10.0, 50.0),
    ("cpac", 1, CPAC_ARTICLE, 37.0, 37.0),
    ("cpac-2tec", 4, TWO_TEC_ARTICLE, 4.0, 100.0),
    ("cpac-2tec", 4, TWO_TEC_ARTICLE, 4.0, 110.0),
    ("cpac-2tec", 4, TWO_TEC_ARTICLE, 60.0, 95.5),
]
_RLT_VALUES = range(-127, 1200, 37)
_RMT1_VALUES = range(-50, 2000, 41)


@pytest.mark.parametrize(("limit_class", "rtd", "ran", "cfg_min", "cfg_max"), _CONFIGS)
def test_fr7_6_window_never_above_rmt1_nor_below_rlt(
    limit_class: str, rtd: int, ran: str, cfg_min: float, cfg_max: float
) -> None:
    cfg = slot_cfg(limit_class, min_c=cfg_min, max_c=cfg_max)
    class_max = 70.0 if limit_class == "cpac" else 110.0
    permitted = 0
    for rlt, rmt1 in itertools.product(_RLT_VALUES, _RMT1_VALUES):
        result = compute_limits(cfg, ARTICLE_MAP, rtd, ran, rlt, rmt1)
        expected_min = max(cfg_min, 4.0, rlt / 10)
        expected_max = min(cfg_max, class_max, rmt1 / 10)
        if expected_min > expected_max:
            assert_blocked(result, "empty_window")
            continue
        permitted += 1
        assert result.permitted is True, (rlt, rmt1)
        assert result.min_c is not None
        assert result.max_c is not None
        # FR-7.6, compared in tenths so float rounding cannot hide a violation.
        assert round(result.max_c * 10) <= rmt1, (rlt, rmt1)
        assert round(result.min_c * 10) >= rlt, (rlt, rmt1)
        assert result.min_c <= result.max_c
        # FR-7.3: inside the class bounds and the configured window.
        assert 4.0 <= result.min_c
        assert result.max_c <= class_max
        assert cfg_min <= result.min_c
        assert result.max_c <= cfg_max
        # FR-7.4: exact formula.
        assert result.min_c == pytest.approx(expected_min, abs=1e-9)
        assert result.max_c == pytest.approx(expected_max, abs=1e-9)
    assert permitted > 0


# ---------------------------------------------------------------------------------------------
# Purity: the inputs are not changed
# ---------------------------------------------------------------------------------------------


def test_fr7_compute_limits_does_not_mutate_inputs() -> None:
    cfg = slot_cfg("cpac", min_c=4.0, max_c=70.0)
    before_map = dict(ARTICLE_MAP)
    before_cfg = cfg.model_dump()
    compute_limits(cfg, ARTICLE_MAP, 1, CPAC_ARTICLE, 40, 700)
    assert ARTICLE_MAP == before_map
    assert cfg.model_dump() == before_cfg

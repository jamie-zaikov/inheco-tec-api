"""Limit classes of the Inheco TEC slot devices (design C8; FR-7.3).

The class table holds the bounds every configured and effective temperature window must stay
inside. Values are integer tenths of °C, the unit of the wire protocol (protocol report §5.1), so
limit arithmetic never meets binary-float error.

Sources: ``<private-doc-ref>`` §1.1 (CPAC 4–70 °C, CPAC HT 2TEC up to
110 °C) and §2.4 (device types ``1`` = CPAC, ``4`` = CPAC HT 2TEC), and protocol report §5.3.
The 100.0 °C default maximum of ``cpac-2tec`` is FR-7.3 / DD-12.

:func:`compute_limits` turns the class table, the slot configuration and the identification
reads into the effective window of one slot (FR-7.4), failing closed (FR-7.5, FR-7.8).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    # Type-only: manifest.py imports CLASS_TABLE from this module at runtime.
    from inheco_tec_api.manifest import ArticleEntry, SlotConfig


@dataclass(frozen=True)
class LimitClass:
    """One row of the C8 class table; temperatures in integer tenths of °C."""

    name: str
    device_type: int
    """``RTD`` device type that identifies this class (protocol report §5.3)."""
    min_tenths: int
    """Class minimum target temperature."""
    max_tenths: int
    """Class maximum target temperature (hard cap; no configuration may exceed it)."""
    default_max_tenths: int
    """Configured maximum used when the manifest omits ``max_c`` (DD-12)."""


CPAC: Final = "cpac"
CPAC_2TEC: Final = "cpac-2tec"

CLASS_TABLE: Final[dict[str, LimitClass]] = {
    CPAC: LimitClass(
        name=CPAC, device_type=1, min_tenths=40, max_tenths=700, default_max_tenths=700
    ),
    CPAC_2TEC: LimitClass(
        name=CPAC_2TEC, device_type=4, min_tenths=40, max_tenths=1100, default_max_tenths=1000
    ),
}
"""The C8 class table (FR-7.3), keyed by class name."""


def class_of_device_type(rtd: int) -> str | None:
    """Return the limit class name for ``RTD`` device type ``rtd``, or ``None`` if it has none.

    Only types ``1`` (CPAC) and ``4`` (CPAC HT 2TEC) are controllable (FR-7.5); every other type,
    for example ``5`` or ``128``, returns ``None``.
    """
    for limit_class in CLASS_TABLE.values():
        if limit_class.device_type == rtd:
            return limit_class.name
    return None


ARTICLE_DIGITS: Final = 7
"""An article number is exactly this many ASCII decimal digits (FR-7.7, FR-7.8)."""


@dataclass(frozen=True)
class SlotLimits:
    """Result of :func:`compute_limits` for one slot (design C8).

    ``permitted`` is ``True`` only when every fail-closed check passed and the window is not empty;
    then ``min_c``/``max_c`` hold the effective window in °C and ``reason`` is ``None``. When it is
    ``False``, ``min_c``/``max_c`` are ``None`` and ``reason`` names the first failing check
    (FR-7.5); the slot still allows reads and stop. ``limit_class`` and ``variant`` come from the
    article-number map and are set whenever the article number was mapped (FR-7.7), so the status
    can report the detected variant even on a class mismatch or an empty window.
    """

    permitted: bool
    min_c: float | None
    max_c: float | None
    limit_class: str | None
    variant: str | None
    reason: str | None


def _blocked(reason: str, limit_class: str | None = None, variant: str | None = None) -> SlotLimits:
    return SlotLimits(
        permitted=False,
        min_c=None,
        max_c=None,
        limit_class=limit_class,
        variant=variant,
        reason=reason,
    )


def _is_article_number(ran: str) -> bool:
    return len(ran) == ARTICLE_DIGITS and ran.isascii() and ran.isdigit()


def compute_limits(
    slot_cfg: SlotConfig,
    article_map: Mapping[str, ArticleEntry],
    rtd: int | None,
    ran: str | None,
    rlt: int | None,
    rmt1: int | None,
) -> SlotLimits:
    """Compute the effective target window of one slot, failing closed (FR-7, FR-7.4–FR-7.8).

    Pure function: no I/O, no logging. Inputs are the configured slot, the FR-2.7 article map, and
    the identification reads, each ``None`` when it could not be read: ``rtd`` the ``RTD`` device
    type, ``ran`` the raw ``RAN`` reply data, ``rlt``/``rmt1`` the firmware minimum and maximum in
    integer tenths of °C (``RLT`` is signed, FR-7.2).

    Check order; the first failure wins and becomes ``reason`` (FR-7.5, FR-7.8):

    1. ``device_type_unreadable``; ``device_type_not_controllable`` (type not 1 or 4).
    2. ``article_unreadable``; ``article_malformed`` (not exactly 7 ASCII decimal digits);
       ``article_unmapped``.
    3. ``article_class_mismatch_config``; ``article_class_mismatch_device_type``.
    4. ``firmware_limits_unreadable`` (``RLT`` or ``RMT1`` unread).
    5. FR-7.4: ``min = max(cfg_min, class_min, RLT)``, ``max = min(cfg_max, class_max, RMT1)``;
       ``empty_window`` when ``min > max``.

    All arithmetic is in integer tenths. The class bounds are applied here as well, so the result
    never trusts the configuration to lie inside the class (FR-7.3). Because ``RLT`` is a term of
    the ``max`` that gives ``min`` and ``RMT1`` a term of the ``min`` that gives ``max``, and an
    empty window is refused, a permitted window is never below ``RLT`` nor above ``RMT1`` (FR-7.6).

    Sources: ``<private-doc-ref>`` §1.1 and §2.4 (class bounds and
    device types) and §2.5 (``RLT``/``RMT1`` in tenths); protocol report §5.1 and §5.3.
    """
    if rtd is None:
        return _blocked("device_type_unreadable")
    device_class = class_of_device_type(rtd)
    if device_class is None:
        return _blocked("device_type_not_controllable")

    if ran is None:
        return _blocked("article_unreadable")
    if not _is_article_number(ran):
        return _blocked("article_malformed")
    article = article_map.get(ran)
    if article is None:
        return _blocked("article_unmapped")

    limit_class, variant = article.limit_class, article.variant
    if limit_class != slot_cfg.limit_class:
        return _blocked("article_class_mismatch_config", limit_class, variant)
    if limit_class != device_class:
        return _blocked("article_class_mismatch_device_type", limit_class, variant)

    if rlt is None or rmt1 is None:
        return _blocked("firmware_limits_unreadable", limit_class, variant)

    bounds = CLASS_TABLE[limit_class]
    min_tenths = max(slot_cfg.min_tenths, bounds.min_tenths, rlt)
    max_tenths = min(slot_cfg.max_tenths, bounds.max_tenths, rmt1)
    if min_tenths > max_tenths:
        return _blocked("empty_window", limit_class, variant)

    return SlotLimits(
        permitted=True,
        min_c=min_tenths / 10,
        max_c=max_tenths / 10,
        limit_class=limit_class,
        variant=variant,
        reason=None,
    )

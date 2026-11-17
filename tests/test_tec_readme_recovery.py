"""FR-16 (amendments A7, A7.1): checklist results, the 3 of 18 banner and the Emergency Off runbook.

Requirements: FR-16 acceptance (A7, A7.1), FR-16.3, FR-16.3.1 (rows 11, 12 and 16 recorded
2026-10-13 by JZ), FR-16.4 (the banner reads 3 of 18 and equals the count of fully recorded rows),
FR-16.5 (A7.1, Task 29: the section 5.11 Emergency Off recovery runbook with its five points, in
order: Release if connected, power-cycle, then Reacquire; no Connect instruction) and design C16,
C17. The README is read as text only; ``test_tec_readme.py`` keeps the pre-A7 structure checks.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

API_DIR = Path(__file__).resolve().parents[1]
README = API_DIR / "README.md"
# The secret-shape families of the shape scanner, shipped beside the tests (tec_secret_shapes.py).
SCANNER_SCRIPT = Path(__file__).resolve().parent / "tec_secret_shapes.py"

RECOVERY_HEADING = "### 5.11 Emergency Off recovery"
SAFETY_HEADING = "### 6.1 Safety responses the operator must act on"
POINT_1_LEAD = "**Release if connected, power-cycle, then Reacquire.**"


def _lines() -> list[str]:
    return README.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def _block(heading: str, stop: tuple[str, ...]) -> str:
    """The text from ``heading`` up to the next line that starts with any prefix in ``stop``."""
    lines = _lines()
    start = lines.index(heading)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith(stop)), len(lines))
    return "\n".join(lines[start:end])


def _recovery() -> str:
    return _block(RECOVERY_HEADING, ("## ", "### "))


def _safety() -> str:
    return _block(SAFETY_HEADING, ("## ", "### "))


def _points() -> list[str]:
    """The five numbered points of section 5.11, each with its continuation lines."""
    text = _recovery()
    starts = [m.start() for m in re.finditer(r"(?m)^(\d+)\. ", text)]
    assert len(starts) == 5, f"section 5.11 has {len(starts)} numbered points, expected 5"
    numbers = [int(m.group(1)) for m in re.finditer(r"(?m)^(\d+)\. ", text)]
    assert numbers == [1, 2, 3, 4, 5]
    return [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)], strict=True)]


def _load_secret_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdd_secret_scan_for_recovery", SCANNER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- FR-16.5: section 5.11 Emergency Off recovery -------------------------------------------


def test_fr16_5_point_1_release_power_cycle_then_reacquire() -> None:
    point = _points()[0]
    for needed in (
        "call Release",
        "Power-cycle the TEC controller",
        "call Reacquire",
        "emergency_off_cleared: true",
    ):
        assert needed in point, f"point 1 lacks {needed!r}"
    assert point.startswith(f"1. {POINT_1_LEAD}")
    # The order check runs on the text after the bold lead (design C16).
    body = point[point.index(POINT_1_LEAD) + len(POINT_1_LEAD) :]
    release = body.index("call Release")
    power_cycle = body.index("Power-cycle the TEC controller")
    reacquire = body.index("call Reacquire")
    assert release < power_cycle < reacquire
    assert "stopped_slots" in point


def test_fr16_5_point_2_code_i_502_means_power_cycle_missing() -> None:
    point = _points()[1]
    for needed in ("code `I`", "502", "power cycle is missing", "stays `released`"):
        assert needed in point, f"point 2 lacks {needed!r}"
    assert "`stop_all_failed`" in point


def test_fr16_5_point_3_connect_on_connected_is_not_the_documented_path() -> None:
    point = _points()[2]
    for needed in ("Connect", "old device handle", "fails closed"):
        assert needed in point, f"point 3 lacks {needed!r}"
    assert "not the documented path" in point


def test_fr16_5_point_4_reacquire_on_connected_does_not_clear() -> None:
    point = _points()[3]
    assert "Reacquire on a `connected` controller is a no-op" in point
    assert "no-op" in point
    assert "FR-12.5" in point
    assert re.search(r"does not clear\s+emergency-off", point)
    assert "Use Connect" not in point


def test_fr16_5_point_5_service_restart_is_not_a_recovery_step() -> None:
    point = _points()[4]
    assert "FR-17.9" in point
    assert "does not recover the device" in point
    assert "not a recovery step" in point


def test_fr16_5_connect_appears_only_in_point_3() -> None:
    """A7.1: case-sensitive; the lower-case state name ``connected`` is allowed."""
    section = _recovery()
    points = _points()
    point_3_start = section.index(points[2])
    point_3_end = point_3_start + len(points[2])
    hits = [m.start() for m in re.finditer("Connect", section)]
    assert hits, "section 5.11 names Connect nowhere (point 3 must)"
    outside = [i for i in hits if not point_3_start <= i < point_3_end]
    assert outside == [], [section[max(0, i - 30) : i + 30] for i in outside]
    for i, point in enumerate(points):
        if i != 2:
            assert "Connect" not in point, f"point {i + 1} names Connect"
    assert "Connect" not in section[: section.index(points[0])]  # the intro
    assert "Use Connect" not in section
    assert "repeat the same call" not in section


@pytest.mark.parametrize("section", ["5.11", "6.1"])
def test_nfr1_recovery_sections_hold_no_secret_shaped_value(section: str) -> None:
    text = _recovery() if section == "5.11" else _safety()
    findings = _load_secret_scan().scan_content(text, "<api-repo-root>/README.md")
    assert findings == [], [(f.type, f.line) for f in findings]
    assert not re.search(r"\b[0-9a-fA-F]{40,}\b", text)
    assert not re.search(r"(?i)bearer\s+[A-Za-z0-9]", text)
    for banned in ("DNS:", "IT request", "DHCP", "lab-firewall"):
        assert banned not in text, f"section {section} holds {banned!r}"

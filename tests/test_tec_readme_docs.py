"""Task 26 (amendment A5) / FR-16.2, FR-18, NFR-1: runbook section 3.4 (non-editable host install,
upgrade, planned restart) and the new section 5.10 (Swagger UI page); design C17 sections 3 and
5.10, DD-15, R15, R18 (AR-1).

The README is read as text only.
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
H34 = "### 3.4 Check out the release tag and create the venv"
H510 = "### 5.10 API documentation page (Swagger UI)"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The text from ``heading`` to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading)
    rest = text[start + len(heading) :]
    nxt = re.search(rf"(?m)^#{{1,{level}}} ", rest)
    return heading + (rest[: nxt.start()] if nxt else rest)


@pytest.fixture(scope="module")
def s34(readme: str) -> str:
    return _section(readme, H34)


@pytest.fixture(scope="module")
def s510(readme: str) -> str:
    return _section(readme, H510)


# --- section 3.4: non-editable install, upgrade, restart ----------------------------------------


def test_fr16_2_s34_restart_is_planned_per_5_9_with_stop_all(s34: str) -> None:
    restart = s34[s34.index("**Restart after an install or upgrade.**") :]
    assert "section 5.9" in restart
    assert "Stop All from n8n first" in restart
    assert "ATE0" in restart


# --- section 5.10: the documentation page --------------------------------------------------------


def test_fr18_s510_trusts_ca_in_current_user_store_only(s510: str) -> None:
    imports = [line for line in s510.splitlines() if "Import-Certificate" in line]
    assert any("-CertStoreLocation Cert:\\CurrentUser\\Root" in line for line in imports)
    assert not [line for line in imports if "LocalMachine" in line]


def test_fr18_readme_has_no_import_certificate_line_for_local_machine(readme: str) -> None:
    lines = [ln for ln in readme.splitlines() if "Import-Certificate" in ln]
    lines = [ln for ln in lines if "LocalMachine" in ln]
    assert lines == []


# --- NFR-1: no secret-shaped value in the two sections -----------------------------------------


def _load_secret_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdd_secret_scan_for_docs", SCANNER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("heading", [H34, H510])
def test_nfr1_sections_hold_no_secret_shaped_value(readme: str, heading: str) -> None:
    text = _section(readme, heading)
    findings = _load_secret_scan().scan_content(text, "<api-repo-root>/README.md")
    assert findings == [], [(f.type, f.line) for f in findings]  # type and line only
    assert not re.search(r"-----BEGIN [A-Z ]+-----", text)
    assert not re.search(r"(?i)authorization\s*[:=]\s*bearer\s+[^\s<$]", text)
    assert not re.search(r"\b[0-9a-fA-F]{40,}\b", text)

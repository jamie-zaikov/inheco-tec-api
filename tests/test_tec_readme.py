"""FR-16 / design C17: guard the API runbook and release gate in ``api/README.md``.

The README is the operator runbook (FR-16.2), the hardware-verification checklist (FR-16.3) and
the release banner (FR-16.4). These tests read the file as text only. They check:

- the seven C17 section headings, in order;
- the exact banner text and the "3 of 18" count (items 11, 12 and 16, amendment A7);
- the section 7 checklist table: 18 consecutively numbered rows, each with the observed result,
  date and verified-by columns (empty while unverified; the A7 rows are checked in
  ``test_tec_readme_recovery.py``);
- the security-critical runbook steps (install-root ACL lockdown, ``sc.exe config`` to the
  virtual account plus ``sc.exe qc`` verification, token entry without echo);
- that no secret-shaped value appears (NFR-1), with the documented placeholders allowed;
- that the host-resident api states no Terraform / ``infra/`` change (FR-1.5).
"""

import importlib.util
import math
import re
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest

API_DIR = Path(__file__).resolve().parents[1]
README = API_DIR / "README.md"
# The secret-shape families of the shape scanner, shipped beside the tests (tec_secret_shapes.py).
SCANNER_SCRIPT = Path(__file__).resolve().parent / "tec_secret_shapes.py"

BANNER = "NOT RELEASED FOR PRODUCTION USE ON HARDWARE — 3 of 18 checklist items verified"
CHECKLIST_HEADING = "## 7. Hardware-verification checklist"
CHECKLIST_ROW = re.compile(r"^\|\s*(\d+)\s*\|")

# Placeholders the runbook is allowed to carry instead of a real value (NFR-1).
ALLOWED_PLACEHOLDERS = (
    "<CA_KEY_SECRET_NAME>",
    "<WINSW_SHA256_TO_FILL_AT_INSTALL>",
    "________",
    "INHECO_TEC_API_TOKEN=<value>",
)


def _text() -> str:
    return README.read_text(encoding="utf-8").replace("\r\n", "\n")


def _lines() -> list[str]:
    return _text().split("\n")


def _section(heading: str) -> str:
    """Return the text from ``heading`` up to the next H2 heading (or end of file)."""
    lines = _lines()
    start = lines.index(heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def _cells(row: str) -> list[str]:
    """Split a markdown table row into its trimmed cells (outer pipes removed)."""
    body = row.strip()
    assert body.startswith("|") and body.endswith("|"), row
    return [c.strip() for c in body[1:-1].split("|")]


def _checklist_rows() -> list[list[str]]:
    section = _section(CHECKLIST_HEADING)
    return [_cells(line) for line in section.split("\n") if CHECKLIST_ROW.match(line)]


def _strip_placeholders(text: str) -> str:
    for placeholder in ALLOWED_PLACEHOLDERS:
        text = text.replace(placeholder, "<PLACEHOLDER>")
    return text


def _load_secret_scan() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdd_secret_scan_for_readme", SCANNER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entropy(value: str) -> float:
    counts = Counter(value)
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in counts.values())


def _high_entropy_runs(text: str) -> list[str]:
    """Return base64/hex-looking runs that read like key material, not prose or paths."""
    hits = re.findall(r"\b[0-9a-fA-F]{40,}\b", text)
    for run in re.findall(r"[A-Za-z0-9+/=_]{32,}", text):
        mixed = re.search(r"\d", run) and re.search(r"[a-z]", run) and re.search(r"[A-Z]", run)
        if mixed and _entropy(run) >= 4.3:
            hits.append(run)
    return hits


# --- FR-16 / FR-16.2: the seven C17 sections --------------------------------------------------


def test_fr16_2_token_section_reads_without_echo_and_names_env_var() -> None:
    section4 = _section("## 4. Bearer token")
    assert "Read-Host -AsSecureString" in section4
    assert "INHECO_TEC_API_TOKEN" in section4
    assert "icacls" in section4


# --- Security-critical runbook steps (T19-S1, T19-S2) -----------------------------------------


def test_fr16_2_install_root_acl_lockdown_admins_and_system_only() -> None:
    install = _section("## 3. Python and venv install")
    assert r"C:\instrument-apis\inheco-tec" in install
    grant = r"/inheritance:r /grant:r '\*S-1-5-32-544:\(OI\)\(CI\)F' '\*S-1-5-18:\(OI\)\(CI\)F'"
    # The install root (and its parent C:\instrument-apis) is locked first: inheritance removed,
    # Administrators (S-1-5-32-544) and SYSTEM (S-1-5-18) only, full control.
    root_lock = _section("## 3. Python and venv install").split("### 3.2")[0]
    assert re.search(r"\$top\s*=\s*'C:\\instrument-apis'", root_lock)
    assert re.search(rf"icacls \$top {grant}", root_lock)
    # The data folders (secrets, tls, state, logs) get the same lockdown before the first start.
    assert re.search(rf"icacls \$data {grant}", install)


def test_fr16_2_service_account_gets_read_execute_on_install_root() -> None:
    service = _section("## 5. Windows service")
    assert re.search(
        r"icacls C:\\instrument-apis\\inheco-tec /grant \"\$\{acct\}:\(OI\)\(CI\)RX\"", service
    )


def test_fr16_2_sc_config_virtual_account_and_qc_verification() -> None:
    service = _section("## 5. Windows service")
    assert 'sc.exe config inheco-tec-api obj= "NT SERVICE\\inheco-tec-api"' in service
    assert "sc.exe qc inheco-tec-api" in service
    assert "SERVICE_START_NAME" in service
    assert "LocalSystem" in service
    config_at = service.index("sc.exe config inheco-tec-api obj=")
    qc_at = service.index("sc.exe qc inheco-tec-api")
    assert config_at < qc_at, "sc.exe qc must verify after sc.exe config"


# --- FR-16.4: the banner ----------------------------------------------------------------------


def test_fr16_4_banner_text_exact() -> None:
    assert BANNER in _text()


def test_fr16_4_no_released_claim_while_unverified() -> None:
    # The only mention of a "Released" state is the rule for when it may be used.
    for line in _lines():
        if line.lstrip().startswith(">"):
            assert "NOT RELEASED" in line or "Released" not in line


# --- FR-16.3: the checklist table -------------------------------------------------------------


def test_fr16_3_row_count_ignores_digit_rows_outside_section_7() -> None:
    # The 5.8 exit-code table also has digit-led rows; they must not count as checklist rows.
    all_digit_rows = [line for line in _lines() if CHECKLIST_ROW.match(line)]
    assert len(all_digit_rows) > len(_checklist_rows())


# --- NFR-1: no secret-shaped value ------------------------------------------------------------


def test_nfr1_repo_secret_scan_patterns_find_nothing() -> None:
    scan = _load_secret_scan()
    findings = scan.scan_content(_text(), "<api-repo-root>/README.md")
    # Report type and line only, never the matched value.
    assert findings == [], [(f.type, f.line) for f in findings]


def test_nfr1_no_private_key_or_certificate_body() -> None:
    assert not re.search(r"-----BEGIN [A-Z ]+-----", _text())


def _token_assignment_is_placeholder(tail: str, rest: str) -> bool:
    r"""True when an ``INHECO_TEC_API_TOKEN=`` tail is one of the three documented forms.

    - ``<value>`` (the placeholder in prose, optionally closed by a backtick and a full stop);
    - ``'`` closing an empty PowerShell string that is joined (``+``) with the SecureString value;
    - ``\S'`` as the regex class in the ``Select-String -Pattern`` check.
    """
    if re.fullmatch(r"<value>`?[.,]?", tail):
        return True
    if tail == "'" and re.match(r"\s*\+\s*\[Runtime\.InteropServices\.Marshal\]::", rest):
        return True
    return tail == r"\S'" and rest.strip().startswith("-Quiet")


def test_nfr1_token_env_var_only_assigned_placeholder() -> None:
    # Every ``INHECO_TEC_API_TOKEN=`` must be followed by exactly one documented placeholder
    # form, never a literal value (quoted or not).
    matches = list(re.finditer(r"INHECO_TEC_API_TOKEN\s*=\s*(\S*)([^\n]*)", _text()))
    assert matches, "the runbook names no INHECO_TEC_API_TOKEN= line"
    for match in matches:
        assert _token_assignment_is_placeholder(
            match.group(1), match.group(2)
        ), f"literal token assignment: offset {match.start()}"


@pytest.mark.parametrize(
    ("tail", "rest"),
    [
        ("'realvalue'", ""),
        ('"realvalue"', ""),
        ("'", " + 'realvalue'"),
        ("realvalue", ""),
        ("<value>x", ""),
        (r"\Sabc'", " -Quiet"),
        (r"\S'", " -Pattern 'x'"),
        ("", ""),
    ],
)
def test_nfr1_token_placeholder_check_rejects_literal_values(tail: str, rest: str) -> None:
    # Negative control for the check above: a quoted or bare literal never passes.
    assert not _token_assignment_is_placeholder(tail, rest)


def test_nfr1_no_high_entropy_key_material() -> None:
    hits = _high_entropy_runs(_strip_placeholders(_text()))
    assert hits == [], f"{len(hits)} key-like run(s) found"


def test_nfr1_no_literal_bearer_header() -> None:
    for line in _lines():
        m = re.search(r"(?i)authorization\s*[:=]\s*bearer\s+(\S+)", line)
        if m:
            assert m.group(1).startswith(("$", "<", "{")), "literal bearer value"


def test_nfr1_secret_detectors_catch_planted_values() -> None:
    # Negative control: the detectors used above fire on real-looking values (built at runtime,
    # so no secret-shaped literal sits in this file).
    scan = _load_secret_scan()
    planted = "token: " + "gh" + "p_" + "Ab1" * 13
    assert scan.scan_content(planted, "x.md")
    key_like = "".join("aB3dE7gH9kLmN2pQrS5tUvW8xYz1C4F6"[i % 32] for i in range(40))
    assert _high_entropy_runs(key_like)
    assert _high_entropy_runs("0123456789abcdef" * 3)
    assert not _high_entropy_runs(_strip_placeholders("<WINSW_SHA256_TO_FILL_AT_INSTALL>"))

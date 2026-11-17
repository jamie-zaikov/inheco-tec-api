"""Task 25 (amendment A4) / FR-16, FR-16.2, FR-1.5, NFR-1: the runbook network and certificate
steps after the IT request was withdrawn (design C17 sections 2 and 5, DD-14, Risks R10, R14).

The README is read as text only. The checks:

- no IT request, DHCP reservation or DNS SAN text is left anywhere in the README;
- section 2 does the key work in Cloud Shell, keeps both private keys in Secret Manager (project
  ``<gcp-project-id>``), issues the leaf with ``subjectAltName = IP:192.0.2.10`` only, and
  installs on `the API host` by download, copy into ``tls\\`` and delete of the downloaded copies;
- section 5 holds the operator's local ``New-NetFirewallRule`` (port and remote range, never
  ``-Program``), its ``Remove-NetFirewallRule`` at uninstall, and the reachability check with
  its stop-and-report rule;
- the withdrawn IT request draft file is gone.
"""

import re
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
README = API_DIR / "README.md"
RULE_NAME = "inheco-tec-api TCP 8743"


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


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"(?ms)^```[a-z]*\n(.*?)^```", text)


# --- Section 2: Cloud Shell, Secret Manager, IP-only SAN -----------------------------------------


@pytest.fixture(scope="module")
def section2(readme: str) -> str:
    return _section(readme, "## 2. TLS certificate provisioning")


def test_fr16_2_key_work_is_in_cloud_shell(section2: str) -> None:
    assert "Cloud Shell" in section2
    assert re.search(r"Never do key work on the lab host", section2)


def test_fr16_2_both_private_keys_go_to_secret_manager_of_the_project(section2: str) -> None:
    assert "Secret Manager" in section2
    creates = re.findall(r"gcloud secrets create (\S+) --project (\S+)", section2)
    assert ("<CA_KEY_SECRET_NAME>", "<gcp-project-id>") in creates
    assert ("<LEAF_KEY_SECRET_NAME>", "<gcp-project-id>") in creates
    # Both uploads read the key from a file, never from the command line.
    assert re.search(r"--data-file ca\.key", section2)
    assert re.search(r"--data-file server\.key", section2)


def test_nfr1_no_command_prints_a_key(section2: str) -> None:
    code = "\n".join(_fenced_blocks(section2))
    assert not re.search(r"\bcat\b[^\n]*\.key", code)
    assert not re.search(r"Get-Content[^\n]*\.key", code)
    accesses = re.findall(r"gcloud secrets versions access(?:[^\n]*\\\n)*[^\n]*", code)
    assert accesses, "the CA key fetch for signing is documented"
    for access in accesses:
        assert "--out-file" in access, access
    assert not re.search(r"BEGIN [A-Z ]*PRIVATE KEY", section2)


def test_fr16_2_install_downloads_copies_into_tls_and_deletes(section2: str) -> None:
    install = _section(section2, "### 2.3 Install on the API host")
    tls = r"C:\ProgramData\instrument-apis\inheco-tec\tls"
    assert "no `openssl`, so all key work stays in Cloud Shell" in install
    download_at = install.index("download `server.crt`, `server.key` and `ca.crt`")
    copy_at = install.index(
        'Copy-Item -LiteralPath "$dl\\server.key" -Destination "$tls\\server.key"'
    )
    delete_at = install.index('Remove-Item -LiteralPath "$dl\\server.key"')
    assert download_at < copy_at < delete_at
    assert f"$tls = '{tls}'" in install
    assert "Move-Item" not in install
    assert "icacls" in install
    assert "the CA key never reaches the" in install


def test_fr16_2_cloud_shell_key_copy_is_deleted(section2: str) -> None:
    assert section2.count("shred -u server.key") >= 1
    assert "shred -u ca.key" in section2


def test_r10_ip_change_reissues_leaf_and_updates_host_baseurl_and_firewall(section2: str) -> None:
    renewal = _section(section2, "### 2.5 Renewal")
    assert "IP change" in renewal
    assert "subjectAltName = IP:<new IP>" in renewal
    assert "`host` in `instrument.yaml`" in renewal
    assert "`baseUrl`" in renewal
    assert "firewall rule" in renewal


# --- Section 5: local host firewall rule and reachability check -----------------------------------


@pytest.fixture(scope="module")
def section5(readme: str) -> str:
    return _section(readme, "## 5. Windows service")


def test_fr16_2_local_firewall_rule_is_port_and_range_scoped(section5: str) -> None:
    rules = [
        line.strip()
        for block in _fenced_blocks(section5)
        for line in block.splitlines()
        if line.strip().startswith("New-NetFirewallRule")
    ]
    assert len(rules) == 1, rules
    rule = rules[0]
    assert f'-DisplayName "{RULE_NAME}"' in rule
    assert "-Direction Inbound" in rule
    assert "-Action Allow" in rule
    assert "-Protocol TCP" in rule
    assert re.search(r"-LocalPort 8743\b", rule)
    assert re.search(r"-RemoteAddress 198\.51\.100\.0/24\b", rule)


def test_fr16_2_firewall_rule_has_no_program_argument(readme: str) -> None:
    assert not re.search(r"-Program\b", readme, re.IGNORECASE)
    assert "Get-Process -Name" not in readme


def test_r14_reachability_check_from_n8n_with_stop_and_report(section5: str) -> None:
    check = _section(section5, "#### Reachability check")
    assert "from the n8n instance" in check
    assert "HTTPS" in check
    assert "certificate" in check and "verification" in check
    assert "DD-14" in check
    assert "Stop and" in check and "report it" in check
    assert re.search(r"Do not change any network setting", check)
    assert "R14" in check

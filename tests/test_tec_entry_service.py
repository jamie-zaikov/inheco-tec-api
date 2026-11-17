"""Tests for the C13 WinSW service definition ``service/inheco-tec-api.xml`` (Task 19.2).

FR-3.1 (automatic start, restart after an abnormal exit), FR-11.1 / S7 (a graceful stop budget
that covers the uvicorn drain plus the shutdown stop), carry-forward T7-S4 (only the venv on
``sys.path``) and NFR-1 / Risk R13 (no secret in the XML). The file is parsed with
``xml.etree``; no service is installed and WinSW never runs.
"""

from __future__ import annotations

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath

import pytest
import yaml
from inheco_tec_api import entry
from inheco_tec_api.manifest import MANIFEST_ENV_VAR

OPAQUE_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
"""A long opaque run, the shape of a generated token (paths and words are shorter)."""

FORBIDDEN_WORDS = ("token", "secret", "password", "passwd", "bearer", "apikey", "api_key")


@pytest.fixture(scope="module")
def service(api_root: Path) -> ET.Element:
    return ET.parse(api_root / "service" / "inheco-tec-api.xml").getroot()


def _text(root: ET.Element, tag: str) -> str:
    element = root.find(tag)
    assert element is not None, f"<{tag}> is missing"
    return (element.text or "").strip()


def _env(root: ET.Element) -> dict[str, str]:
    pairs = [(e.get("name"), e.get("value")) for e in root.findall("env")]
    names = [name for name, _ in pairs]
    assert len(names) == len(set(names)), f"duplicate <env> names: {names}"
    return {str(name): str(value) for name, value in pairs}


def test_fr3_1_root_is_a_winsw_service_with_an_id(service: ET.Element) -> None:
    assert service.tag == "service"
    assert _text(service, "id") == "inheco-tec-api"
    assert _text(service, "name")


def test_fr3_1_executable_is_the_venv_console_script(service: ET.Element, api_root: Path) -> None:
    scripts = tomllib.loads((api_root / "pyproject.toml").read_text("utf-8"))["project"]["scripts"]
    assert scripts["inheco-tec-api"] == "inheco_tec_api.entry:main"

    executable = PureWindowsPath(_text(service, "executable"))
    assert executable.is_absolute()
    assert executable.name == "inheco-tec-api.exe"
    assert executable.parent.name == "Scripts"
    venv = executable.parent.parent
    assert venv.name == "venv"
    assert PureWindowsPath(_text(service, "workingdirectory")) == venv
    assert service.find("arguments") is None  # no CLI flags: the manifest is the only config


def test_fr3_1_env_names_the_manifest_and_locks_the_import_path(service: ET.Element) -> None:
    env = _env(service)
    assert set(env) == {MANIFEST_ENV_VAR, "PYTHONSAFEPATH", "PYTHONNOUSERSITE", "PYTHONPATH"}
    manifest = PureWindowsPath(env[MANIFEST_ENV_VAR])
    assert manifest.is_absolute() and manifest.suffix == ".yaml"
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONPATH"] == ""


def test_fr3_1_starts_automatically_and_restarts_on_failure(service: ET.Element) -> None:
    assert _text(service, "startmode") == "Automatic"
    failures = service.findall("onfailure")
    assert len(failures) == 3
    for failure in failures:
        assert failure.get("action") == "restart"
        assert failure.get("delay") == "10 sec"
    assert _text(service, "resetfailure") == "1 hour"


def test_fr11_1_stop_budget_covers_drain_and_shutdown_stop(
    service: ET.Element, duo_root: Path
) -> None:
    assert _text(service, "stoptimeout") == "30 sec"
    assert _text(service, "preshutdown") == "true"
    assert _text(service, "preshutdownTimeout") == "30 sec"
    shipped = yaml.safe_load((duo_root / "instrument.yaml").read_text("utf-8"))
    budget = entry.GRACEFUL_SHUTDOWN_S + shipped["service"]["shutdown_timeout_s"]
    assert budget < 30  # S7: drain + shutdown deadline fits the WinSW stop timeout


def test_fr3_1_logs_roll_by_size_under_programdata(service: ET.Element) -> None:
    assert PureWindowsPath(_text(service, "logpath")) == PureWindowsPath(
        r"C:\ProgramData\instrument-apis\inheco-tec\logs"
    )
    log = service.find("log")
    assert log is not None
    assert log.get("mode") == "roll-by-size"
    assert int(_text(log, "sizeThreshold")) > 0
    assert int(_text(log, "keepFiles")) > 0


def test_nfr1_xml_holds_no_secret(service: ET.Element, duo_root: Path) -> None:
    shipped = yaml.safe_load((duo_root / "instrument.yaml").read_text("utf-8"))
    bearer_env = shipped["auth"]["bearer_token_ref"]
    values: list[str] = []
    for element in service.iter():
        values.append(element.tag)
        values.append(element.text or "")
        for name, value in element.attrib.items():
            values += [name, value]
    for value in values:
        assert bearer_env not in value, "the token env var must not be set in the XML (R13)"
        assert not OPAQUE_RUN.search(value), f"secret-shaped value in the XML: {value[:12]}..."
        assert not any(word in value.lower() for word in FORBIDDEN_WORDS), value[:40]
    assert service.find("serviceaccount") is None  # no stored account password

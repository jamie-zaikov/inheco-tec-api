"""The Windows service definition is well-formed XML (FR-9.6, FR-9.7).

``service/inheco-tec-api.xml`` is the WinSW definition of the service. This test parses it with
the standard-library parser and checks the elements that WinSW needs to install and run it. The
element and attribute names are compared with the source definition by the release tooling.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

SERVICE_XML = Path(__file__).resolve().parents[1] / "service" / "inheco-tec-api.xml"


def test_fr9_7_service_definition_parses_as_well_formed_xml() -> None:
    # Rejects: a service file that does not parse (a broken tag or an unescaped character).
    root = ET.parse(SERVICE_XML).getroot()
    assert root.tag == "service"


def test_fr9_6_service_definition_keeps_the_winsw_settings() -> None:
    # Rejects: a generalized file that lost the service id, the executable or the stop settings.
    root = ET.parse(SERVICE_XML).getroot()
    for name in ("id", "name", "executable", "workingdirectory", "startmode", "stoptimeout"):
        element = root.find(name)
        assert element is not None and (element.text or "").strip(), name
    assert (root.findtext("id") or "").strip() == "inheco-tec-api"
    assert len(root.findall("onfailure")) == 3

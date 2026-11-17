"""Controller-query route tests: sensor values, device type, firmware version (Task 18.2;
FR-8.10, FR-8.11, FR-8.12, FR-6.5; design C12 endpoints table, P3).

Split from ``test_tec_api_slot.py`` to keep each file under 500 lines. ``TestClient`` (in
process, no port) over ``create_app`` with an injected fake factory; every response body is
scanned by :class:`ScanningClient` (FR-3.7). Each route writes its exact command string and
returns the decoded body; a bad selector or slot and a bad controller ID get 422 / 404 with zero
writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.protocol.tables import CONTROLLER_TYPES, DEVICE_TYPES
from tec_api_helpers import ApiRig, ScanningClient

RDC = "0RDC1"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    """A started app (lifespan entered) with the startup writes forgotten."""
    with rig.client() as started:
        rig.mark()
        yield started


def _get(rig: ApiRig, client: ScanningClient, path: str) -> Any:
    return client.get(path, headers=rig.auth)


# -- FR-8.10: sensor values ------------------------------------------------------------------


@pytest.mark.parametrize("selector", range(9))
def test_fr_8_10_sensor_values_writes_rhv(
    rig: ApiRig, client: ScanningClient, selector: int
) -> None:
    response = _get(rig, client, f"/v1/controllers/0/sensor-values?selector={selector}")
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "selector": selector,
        "data": str(200 + selector),
    }
    assert rig.commands() == [f"0RHV{selector}", RDC]


@pytest.mark.parametrize(
    "query", ["?selector=9", "?selector=-1", "?selector=10", "", "?selector=a"]
)
def test_fr_8_10_sensor_values_bad_selector_is_422_without_io(
    rig: ApiRig, client: ScanningClient, query: str
) -> None:
    response = _get(rig, client, "/v1/controllers/0/sensor-values" + query)
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"
    assert rig.commands() == []


# -- FR-8.11: device type (P3: range check only) ---------------------------------------------


def test_fr_8_11_device_type_slot_0_is_controller_type(rig: ApiRig, client: ScanningClient) -> None:
    response = _get(rig, client, "/v1/controllers/0/device-type?slot=0")
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "slot": 0,
        "code": 1,
        "name": CONTROLLER_TYPES[1],
    }
    assert rig.commands() == ["0RTD0", RDC]


@pytest.mark.parametrize(("slot", "code"), [(1, 1), (2, 1), (5, 128), (6, 128)])
def test_fr_8_11_device_type_any_slot_0_to_6_range_check_only(
    rig: ApiRig, client: ScanningClient, slot: int, code: int
) -> None:
    """P3: slots 5 and 6 are not configured, yet the read is allowed (range check only)."""
    response = _get(rig, client, f"/v1/controllers/0/device-type?slot={slot}")
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "slot": slot,
        "code": code,
        "name": DEVICE_TYPES.get(code),
    }
    assert rig.commands() == [f"0RTD{slot}", RDC]


@pytest.mark.parametrize("query", ["?slot=7", "?slot=-1", "?slot=99", "", "?slot=x"])
def test_fr_8_11_device_type_slot_out_of_range_is_422_without_io(
    rig: ApiRig, client: ScanningClient, query: str
) -> None:
    response = _get(rig, client, "/v1/controllers/0/device-type" + query)
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"
    assert rig.commands() == []


# -- FR-8.12: firmware version ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("selector", "name"),
    [(0, "bootstrap"), (1, "application"), (2, "serial"), (3, "hardware"), (4, "copyright")],
)
def test_fr_8_12_firmware_version_writes_rfv(
    rig: ApiRig, client: ScanningClient, selector: int, name: str
) -> None:
    response = _get(rig, client, f"/v1/controllers/0/firmware-version?selector={selector}")
    assert response.status_code == 200
    assert response.json() == {
        "controller_id": 0,
        "selector": selector,
        "name": name,
        "data": rig.dev().mainboard.firmware[selector],
    }
    assert rig.commands() == [f"0RFV{selector}", RDC]


@pytest.mark.parametrize("query", ["?selector=5", "?selector=-1", ""])
def test_fr_8_12_firmware_version_bad_selector_is_422_without_io(
    rig: ApiRig, client: ScanningClient, query: str
) -> None:
    response = _get(rig, client, "/v1/controllers/0/firmware-version" + query)
    assert response.status_code == 422
    assert rig.error(response)["kind"] == "validation_error"
    assert rig.commands() == []


# -- FR-6.5: 422 (range) before 404 (not configured) -----------------------------------------


@pytest.mark.parametrize(
    "query", ["sensor-values?selector=0", "device-type?slot=0", "firmware-version?selector=0"]
)
@pytest.mark.parametrize(("cid", "status"), [(8, 422), (-1, 422), (1, 404), (7, 404)])
def test_fr_6_5_query_route_ids_range_before_configured(
    rig: ApiRig, client: ScanningClient, query: str, cid: int, status: int
) -> None:
    response = _get(rig, client, f"/v1/controllers/{cid}/{query}")
    assert response.status_code == status
    assert rig.error(response)["kind"] == ("validation_error" if status == 422 else "not_found")
    assert rig.commands() == []


def test_fr_6_5_unconfigured_controller_404_before_bad_value(
    rig: ApiRig, client: ScanningClient
) -> None:
    response = _get(rig, client, "/v1/controllers/1/sensor-values?selector=9")
    assert response.status_code == 404  # configured check (step 1) before the value check
    response = _get(rig, client, "/v1/controllers/1/device-type?slot=7")
    assert response.status_code == 404
    assert rig.commands() == []

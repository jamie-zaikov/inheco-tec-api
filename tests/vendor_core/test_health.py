"""Health-probe contract suite for ``instrument_api_core.health`` (Task 2).

This suite pins the unauthenticated ``/healthz`` contract: the two constants, the frozen
``HealthStatus`` dataclass, and the exact ``health_payload()`` body. It uses the standard library
plus pytest only and stays fully offline (NFR-7). Every assertion guards the no-host/no-port/no-
secret rule the reference service contract requires (NFR-1): the payload is a status token only.
"""

import dataclasses

import pytest
from inheco_tec_api._vendor.instrument_api_core.health import (
    HEALTH_PATH,
    HEALTH_STATUS_OK,
    HealthStatus,
    health_payload,
)

# --------------------------------------------------------------------------------------------------
# Constants (FR-1, FR-1.1)
# --------------------------------------------------------------------------------------------------


def test_health_path_is_the_unauthenticated_probe_path() -> None:
    """``HEALTH_PATH`` is exactly ``"/healthz"`` (FR-1, FR-1.1)."""
    assert HEALTH_PATH == "/healthz"


def test_health_status_ok_is_the_single_live_token() -> None:
    """``HEALTH_STATUS_OK`` is exactly ``"ok"`` (FR-1, FR-1.1)."""
    assert HEALTH_STATUS_OK == "ok"


# --------------------------------------------------------------------------------------------------
# HealthStatus frozen dataclass (FR-1.2)
# --------------------------------------------------------------------------------------------------


def test_health_status_is_a_dataclass() -> None:
    """``HealthStatus`` is a dataclass (FR-1.2)."""
    assert dataclasses.is_dataclass(HealthStatus)


def test_health_status_default_is_health_status_ok() -> None:
    """A default-constructed ``HealthStatus`` carries ``status == HEALTH_STATUS_OK`` (FR-1.2)."""
    assert HealthStatus().status == HEALTH_STATUS_OK
    assert HealthStatus().status == "ok"


def test_health_status_is_frozen() -> None:
    """Assignment to ``status`` raises ``dataclasses.FrozenInstanceError`` (FR-1.2)."""
    status = HealthStatus()
    with pytest.raises(dataclasses.FrozenInstanceError):
        # A frozen dataclass forbids attribute mutation after construction.
        status.status = "tampered"  # type: ignore[misc]


def test_health_status_reflects_a_supplied_status() -> None:
    """A supplied ``status`` argument is reflected on the instance (FR-1.2)."""
    assert HealthStatus("custom").status == "custom"


def test_health_status_has_exactly_the_status_field() -> None:
    """``HealthStatus`` carries the single ``status`` field: no host, no port (FR-1.2, NFR-1)."""
    field_names = [field.name for field in dataclasses.fields(HealthStatus)]
    assert field_names == ["status"]


# --------------------------------------------------------------------------------------------------
# health_payload() exact body (FR-1.3, NFR-1)
# --------------------------------------------------------------------------------------------------


def test_health_payload_is_exactly_status_ok() -> None:
    """``health_payload()`` equals ``{"status": "ok"}`` exactly (FR-1.3)."""
    assert health_payload() == {"status": "ok"}


def test_health_payload_carries_only_the_status_key() -> None:
    """The body has the single ``status`` key: no host, port, instrument, or secret key (NFR-1)."""
    payload = health_payload()
    assert list(payload.keys()) == ["status"]
    assert payload["status"] == HEALTH_STATUS_OK


def test_health_payload_returns_a_fresh_dict_each_call() -> None:
    """Each call returns an equal but distinct dict; no shared mutable state (FR-1.3)."""
    first = health_payload()
    second = health_payload()
    assert first == second
    assert first is not second

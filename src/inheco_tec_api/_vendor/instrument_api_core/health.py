"""The unauthenticated health-probe contract shared by every instruments/*/api (C11, FR-5.1).

Every owned api exposes a liveness/readiness probe on :data:`HEALTH_PATH`, mounted WITHOUT the
bearer-token dependency, so the Cloud Run probe needs no credential (the unauthenticated
``/healthz`` rule from the reference ``zebra_printer`` service). The probe reports process liveness
only: it opens no instrument socket, so instrument reachability never affects liveness, and its body
carries no host and no port (the no-host/port-in-body rule).

The concrete api mounts a router that returns :func:`health_payload` at :data:`HEALTH_PATH` on an
unauthenticated route. This module holds only the shared constant and the payload shape.
"""

from dataclasses import dataclass

HEALTH_PATH: str = "/healthz"
"""The unauthenticated liveness/readiness probe path every api mounts."""

HEALTH_STATUS_OK: str = "ok"
"""The single body value the probe reports when the process is live."""


@dataclass(frozen=True)
class HealthStatus:
    """The health-probe body shape: a status token only, never a host or port.

    ``status`` defaults to :data:`HEALTH_STATUS_OK`. The body deliberately carries no address,
    instrument detail, or secret, matching the reference service contract.
    """

    status: str = HEALTH_STATUS_OK


def health_payload() -> dict[str, str]:
    """Return the JSON-serializable health body ``{"status": "ok"}`` (FR-5.1).

    The concrete api serves this from an unauthenticated route at :data:`HEALTH_PATH`. The body
    holds a status token only and never a host, port, or secret.
    """
    return {"status": HealthStatus().status}

"""Cross-cutting API conformance tests (Task 18.3; design C12, C16).

``TestClient`` (in process, no port) over ``create_app`` with an injected fake factory; the real
``hid`` module is never imported. Every request goes through :class:`ScanningClient`, so every
response body in this suite (and in the other ``test_tec_api_*`` suites) is scanned for an IPv4
address, ``host:port``, a USB/HID path, a COM port and the sentinel tokens (FR-3.7). The test
manifest carries the real host ``192.0.2.10`` and port ``8743``, so a leak of either would show.

Covered: 401 on every non-health route walked from ``app.routes`` (FR-3.5, T17-T1); every §8.2
code through slot routes → non-2xx with the code (FR-9.1); transport failures → 504 (FR-9.2,
FR-9.3); OpenAPI documents ``ErrorResponse`` on every route and has no free-form command field
(FR-9.4, FR-10.2); two concurrent requests on one controller never interleave reports (FR-5.1);
the FR-15.1 transaction log fields and no token in any log (FR-15.1, FR-15.3); every operation
responds within 100 ms with the fake and zero delays (NFR-2).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS
from inheco_tec_api.transport.base import HidTransport
from inheco_tec_api.transport.fake import FakeTecDevice, FakeTransportFactory
from inheco_tec_api.transport.fake_actions import BadCrc, FirmwareCode, Match, NoReply
from structlog.testing import capture_logs
from tec_api_helpers import PUBLIC_DOC_PATHS, ApiRig, ScanningClient, iter_routes, sentinel

RDC = "0RDC1"
EXPECTED_ROUTE_COUNT = 21  # 17 API routes + the 4 public /docs routes
ERROR_CODES = sorted(set(FIRMWARE_ERRORS) - {"0"})
# Every operation of the API with a request that succeeds on a started fake (NFR-2, FR-3.7).
OPERATIONS: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/v1/status", {}),
    ("POST", "/v1/controllers/0/connect", {}),
    ("PUT", "/v1/controllers/0/slots/1/target", {"json": {"temperature_c": 37.0}}),
    ("GET", "/v1/controllers/0/slots/1/target", {}),
    ("POST", "/v1/controllers/0/slots/1/start", {}),
    ("POST", "/v1/controllers/0/slots/1/stop", {}),
    ("GET", "/v1/controllers/0/slots/1/temperature?selector=3", {}),
    ("GET", "/v1/controllers/0/slots/1/at-target?tolerance_c=1.0", {}),
    ("GET", "/v1/controllers/0/sensor-values?selector=0", {}),
    ("GET", "/v1/controllers/0/device-type?slot=0", {}),
    ("GET", "/v1/controllers/0/firmware-version?selector=1", {}),
    ("POST", "/v1/stop-all", {}),
    ("POST", "/v1/controllers/0/emergency-off", {"json": {"confirm": True}}),
    ("POST", "/v1/controllers/0/release", {}),
    ("POST", "/v1/controllers/0/reacquire", {}),
]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiRig:
    return ApiRig(tmp_path, monkeypatch)


@pytest.fixture
def client(rig: ApiRig) -> Iterator[ScanningClient]:
    with rig.client() as started:
        rig.mark()
        yield started


def _call(rig: ApiRig, client: ScanningClient, method: str, path: str, **kw: Any) -> Any:
    return client.request(method, path, headers=rig.auth, **kw)


# -- FR-3.5 / T17-T1: 401 on every non-health route walked from app.routes ---------------------


def test_fr_3_5_every_route_from_app_routes_requires_token(rig: ApiRig) -> None:
    routes = list(iter_routes(rig.app.routes))
    assert len(routes) == EXPECTED_ROUTE_COUNT  # a flat walk would find none (T17-T1)
    checked = 0
    with rig.client() as client:
        rig.mark()
        for path, route in routes:
            concrete = path.replace("{cid}", "0").replace("{sid}", "1")
            for method in sorted(route.methods - {"HEAD"}):
                if path == "/healthz":
                    assert client.request(method, concrete).status_code == 200
                    continue
                if path in PUBLIC_DOC_PATHS:  # public by design; see test_tec_api_docs.py
                    continue
                bodies: list[dict[str, Any]] = [{}]
                if method in ("PUT", "POST"):
                    bodies += [{"json": {"temperature_c": 37.0}}, {"json": {"confirm": True}}]
                    bodies.append({"json": {"temperature_c": 1e30}})
                for kw in bodies:
                    for headers in ({}, {"Authorization": f"Bearer {rig.wrong_bearer}"}):
                        response = client.request(method, concrete, headers=headers, **kw)
                        assert response.status_code == 401, (method, path, kw)
                        error = rig.error(response)
                        assert error["kind"] == "unauthorized"
                        checked += 1
        assert rig.commands() == []  # no unauthenticated request reached the device
    assert checked > 0
    assert client.scanned == checked + 1


# -- FR-9.1 / FR-9.3: every §8.2 code through a slot route is non-2xx with the code -----------


@pytest.mark.parametrize("code", ERROR_CODES)
def test_fr_9_1_every_firmware_code_through_set_target(
    rig: ApiRig, client: ScanningClient, code: str
) -> None:
    rig.dev().script(match=Match(mnemonic="STT"), action=FirmwareCode(code), times=None)
    response = _call(
        rig, client, "PUT", "/v1/controllers/0/slots/1/target", json={"temperature_c": 37.0}
    )
    assert response.status_code == 502
    error = rig.error(response)
    assert (error["kind"], error["code"]) == ("firmware_error", code)
    assert error["code_description"] == FIRMWARE_ERRORS[code]
    assert (error["controller_id"], error["slot_id"]) == (0, 1)
    assert code in error["message"]


@pytest.mark.parametrize("code", ERROR_CODES)
def test_fr_9_1_every_firmware_code_through_stop_is_never_2xx(
    rig: ApiRig, client: ScanningClient, code: str
) -> None:
    rig.dev().script(match=Match(mnemonic="ATE"), action=FirmwareCode(code), times=None)
    response = _call(rig, client, "POST", "/v1/controllers/0/slots/2/stop")
    assert response.status_code == 502
    error = rig.error(response)
    assert (error["kind"], error["code"]) == ("firmware_error", code)
    assert error["code_description"] == FIRMWARE_ERRORS[code]
    assert [(s["slot_id"], s["outcome"]) for s in error["slots"]] == [(2, "failed")]


# -- FR-9.2 / FR-9.3: transport failures → 504 with the failure kind ---------------------------


@pytest.mark.parametrize(
    ("action", "failure", "attempts"),
    [(NoReply(), "no_reply", 3), (BadCrc(), "crc", 3), (FirmwareCode("Z"), "parse", None)],
    ids=["no-reply", "crc", "unknown-code"],
)
@pytest.mark.parametrize(
    "path",
    ["/v1/controllers/0/slots/1/target", "/v1/controllers/0/slots/1/temperature"],
)
def test_fr_9_2_transport_failures_are_504(
    rig: ApiRig,
    client: ScanningClient,
    path: str,
    action: Any,
    failure: str,
    attempts: int | None,
) -> None:
    rig.dev().script(match=Match(address=1), action=action, times=None)
    response = _call(rig, client, "GET", path)
    assert response.status_code == 504
    error = rig.error(response)
    assert (error["kind"], error["failure"], error["code"]) == ("transport_error", failure, None)
    if attempts is not None:
        assert error["attempts"] == attempts
    assert error["code_description"] is None


# -- FR-9.4 / FR-10.2: OpenAPI -----------------------------------------------------------------

_ALL_DEVICE = {401, 404, 409, 422, 500, 502, 503, 504}
_SLOT_ROUTES = {
    ("/v1/controllers/{cid}/slots/{sid}/target", "put"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/slots/{sid}/target", "get"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/slots/{sid}/start", "post"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/slots/{sid}/stop", "post"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/slots/{sid}/temperature", "get"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/slots/{sid}/at-target", "get"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/sensor-values", "get"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/device-type", "get"): _ALL_DEVICE,
    ("/v1/controllers/{cid}/firmware-version", "get"): _ALL_DEVICE,
}
_KINDS = {
    ("/v1/controllers/{cid}/slots/{sid}/target", "put"): {
        "409": {"controller_released", "emergency_off_active", "slot_not_permitted"},
        "502": {"firmware_error"},
        "504": {"transport_error"},
    },
    ("/v1/controllers/{cid}/slots/{sid}/start", "post"): {
        "409": {"controller_released", "emergency_off_active", "slot_not_permitted"},
    },
    ("/v1/controllers/{cid}/slots/{sid}/stop", "post"): {"409": {"stop_not_delivered"}},
    ("/v1/controllers/{cid}/slots/{sid}/target", "get"): {"409": {"controller_released"}},
}


def _spec(rig: ApiRig) -> dict[str, Any]:
    response = rig.client().get("/openapi.json", headers=rig.auth)
    assert response.status_code == 200
    assert "HTTPValidationError" not in response.text
    spec: dict[str, Any] = response.json()
    return spec


def test_fr_9_4_every_route_documents_errors_with_error_response(rig: ApiRig) -> None:
    spec = _spec(rig)
    assert "ValidationError" not in spec["components"]["schemas"]
    documented = 0
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            statuses = {s for s in op["responses"] if not s.startswith("2")}
            if path == "/healthz":
                continue
            assert {"401", "500"} <= statuses, (path, method)
            for status in statuses:
                body = op["responses"][status]
                ref = body["content"]["application/json"]["schema"]["$ref"]
                assert ref == "#/components/schemas/ErrorResponse", (path, method, status)
                assert body["description"].startswith("error kind: ")
                documented += 1
    assert documented > 0
    for (path, method), statuses in _SLOT_ROUTES.items():
        responses = spec["paths"][path][method]["responses"]
        assert {int(s) for s in responses if not s.startswith("2")} == statuses, (path, method)
    for (path, method), kinds in _KINDS.items():
        responses = spec["paths"][path][method]["responses"]
        for status, names in kinds.items():
            listed = set(responses[status]["description"].removeprefix("error kind: ").split(", "))
            assert names <= listed, (path, method, status, names - listed)
    stop = spec["paths"]["/v1/controllers/{cid}/slots/{sid}/stop"]["post"]["responses"]["409"]
    assert "emergency_off_active" not in stop["description"]  # a stop is never gated by EO


def test_fr_10_2_no_free_form_command_field(rig: ApiRig) -> None:
    spec = _spec(rig)
    params = set()
    for operations in spec["paths"].values():
        for op in operations.values():
            params |= {(p["name"], p["in"]) for p in op.get("parameters", [])}
            for p in op.get("parameters", []):
                assert p["schema"].get("type") in ("integer", None) or p["name"] == "tolerance_c"
    assert params == {
        ("authorization", "header"),  # the bearer token header of the auth dependency
        ("cid", "path"),
        ("sid", "path"),
        ("selector", "query"),
        ("slot", "query"),
        ("tolerance_c", "query"),
    }
    forbidden = {"command", "cmd", "raw", "command_string", "mnemonic", "param", "payload"}
    for name, schema in spec["components"]["schemas"].items():
        assert name.lower() not in forbidden
        assert not set(schema.get("properties", {})) & forbidden, name
        if name.endswith("Request"):
            assert schema.get("additionalProperties") is False, name
            for prop in schema["properties"].values():
                assert prop.get("type") != "string", (name, prop)


# -- FR-3.7: leak scan over every route, success and failure -----------------------------------


def test_fr_3_7_every_operation_and_error_body_is_leak_scanned(
    rig: ApiRig, client: ScanningClient
) -> None:
    before = client.scanned
    for method, path, kw in OPERATIONS:
        assert _call(rig, client, method, path, **kw).status_code == 200, (method, path)
    rig.dev().script(action=NoReply(), times=None)
    failures = [
        ("GET", "/v1/controllers/0/slots/1/target"),
        ("POST", "/v1/controllers/0/slots/1/stop"),
        ("GET", "/v1/controllers/0/firmware-version?selector=2"),
        ("GET", "/v1/controllers/9/sensor-values?selector=0"),
        ("GET", "/v1/controllers/1/device-type?slot=0"),
    ]
    for method, path in failures:
        assert _call(rig, client, method, path).status_code >= 400
    rig.dev().clear_script()
    rig.dev().script(action=FirmwareCode("E"), times=None)
    assert _call(rig, client, "POST", "/v1/controllers/0/slots/1/start").status_code == 502
    assert client.scanned - before == len(OPERATIONS) + len(failures) + 1


def test_fr_3_7_unexpected_error_on_slot_route_is_fixed_500(
    rig: ApiRig, client: ScanningClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = sentinel("boom-203.0.113.23:99")
    rig.markers.append(marker)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(marker)

    monkeypatch.setattr(rig.app.state.service, "target", boom)
    with capture_logs() as events:
        response = _call(rig, client, "GET", "/v1/controllers/0/slots/1/target")
    assert response.status_code == 500
    assert rig.error(response)["kind"] == "internal_error"
    assert any(e["event"] == "http_internal_error" for e in events)
    assert marker not in repr(events)


# -- FR-5.1: two concurrent requests on one controller never interleave ------------------------


class _GatedTransport:
    """Wraps a fake device; while armed, reads block until the gate opens (T8-C2)."""

    is_fake = True

    def __init__(self, device: FakeTecDevice, factory: _GatedFactory) -> None:
        self.device = device
        self.factory = factory

    def open(self) -> None:
        self.device.open()

    def close(self) -> None:
        self.device.close()

    def flush_input(self) -> int:
        return self.device.flush_input()

    def write_report(self, report: bytes) -> None:
        self.factory.log.append((threading.get_ident(), "write"))
        self.device.write_report(report)

    def read_report(self, timeout_ms: int) -> bytes | None:
        gate = self.factory.gate
        if gate is not None:
            self.factory.in_read.set()
            assert gate.wait(10), "read gate never opened"
        self.factory.log.append((threading.get_ident(), "read"))
        return self.device.read_report(timeout_ms)


class _GatedFactory(FakeTransportFactory):
    def __init__(self) -> None:
        super().__init__()
        self.gate: threading.Event | None = None
        self.in_read = threading.Event()
        self.log: list[tuple[int, str]] = []

    def create(self, controller_id: int) -> HidTransport:
        return _GatedTransport(self.device(controller_id), self)


def test_fr_5_1_concurrent_requests_on_one_controller_never_interleave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = _GatedFactory()
    rig = ApiRig(tmp_path, monkeypatch, factory=factory)
    results: dict[str, int] = {}
    client = rig.client()

    def call(name: str, method: str, path: str) -> None:
        results[name] = client.request(method, path, headers=rig.auth).status_code

    with client:
        rig.mark()
        factory.log.clear()
        factory.gate = threading.Event()
        first = threading.Thread(target=call, args=("a", "GET", "/v1/controllers/0/slots/1/target"))
        first.start()
        assert factory.in_read.wait(5)  # A holds the lock and is blocked inside its read
        second = threading.Thread(
            target=call, args=("b", "POST", "/v1/controllers/0/slots/2/start")
        )
        second.start()
        time.sleep(0.3)
        assert rig.commands() == ["1RTT"]  # B has not written while A's transaction runs
        factory.gate.set()
        first.join(10)
        second.join(10)
        assert rig.commands() == ["1RTT", RDC, "2ATE1", RDC]
        owners = [ident for ident, _ in factory.log]  # before the shutdown stop sweep
    assert results == {"a": 200, "b": 200}
    runs = [o for i, o in enumerate(owners) if i == 0 or owners[i - 1] != o]
    assert len(runs) == 2  # each request's reports form one contiguous block


# -- FR-15.1 / FR-15.3: transaction log fields; no token in any log ----------------------------

_TX_FIELDS = {
    "event",
    "log_level",
    "controller_id",
    "slot",
    "mnemonic",
    "param",
    "result",
    "attempts",
    "duration_ms",
}


def test_fr_15_1_transaction_log_fields(rig: ApiRig, client: ScanningClient) -> None:
    with capture_logs() as events:
        _call(rig, client, "GET", "/v1/controllers/0/slots/2/temperature?selector=1")
        rig.dev().script(match=Match(mnemonic="STT"), action=FirmwareCode("5"), times=1)
        _call(rig, client, "PUT", "/v1/controllers/0/slots/1/target", json={"temperature_c": 40})
        rig.dev().script(match=Match(mnemonic="RTT"), action=NoReply(), times=None)
        _call(rig, client, "GET", "/v1/controllers/0/slots/1/target")
    tx = [e for e in events if e["event"] == "tec_transaction"]
    for event in tx:
        assert set(event) == _TX_FIELDS, event
        assert isinstance(event["duration_ms"], int) and event["duration_ms"] >= 0
    rows = [(e["slot"], e["mnemonic"], e["param"], e["result"], e["attempts"]) for e in tx]
    assert rows == [
        (2, "RAT", "1", "ok", 1),  # the 0RDC1 resync is part of the transaction (FR-5.5)
        (1, "STT", "400", "5", 1),
        (1, "RTT", "", "no_reply", 3),
    ]
    assert all(e["controller_id"] == 0 for e in tx)
    assert [e["log_level"] for e in tx] == ["info", "warning", "warning"]


def test_fr_15_3_token_absent_from_every_log(
    rig: ApiRig, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    wrong = {"Authorization": f"Bearer {rig.wrong_bearer}"}
    with rig.client() as client, capture_logs() as events:
        for method, path, kw in OPERATIONS:
            client.request(method, path, headers=rig.auth, **kw)
            client.request(method, path, headers=wrong, **kw)
    assert any(e["event"] == "tec_transaction" for e in events)
    out = capsys.readouterr()
    for text in (repr(events), caplog.text, out.out, out.err):
        assert rig.bearer not in text
        assert rig.wrong_bearer not in text


# -- NFR-2: every operation within 100 ms with the fake and zero delays ------------------------


def test_nfr_2_each_operation_responds_within_100_ms(rig: ApiRig, client: ScanningClient) -> None:
    for method, path, kw in OPERATIONS:  # warm-up: first-call imports and caches
        _call(rig, client, method, path, **kw)
    _call(rig, client, "POST", "/v1/controllers/0/reacquire")
    slow = {}
    for method, path, kw in OPERATIONS:
        best = min(_timed(rig, client, method, path, kw) for _ in range(3))
        if best > 0.1:
            slow[(method, path)] = round(best, 3)
    assert slow == {}


def _timed(
    rig: ApiRig, client: ScanningClient, method: str, path: str, kw: dict[str, Any]
) -> float:
    start = time.perf_counter()
    response = _call(rig, client, method, path, **kw)
    elapsed = time.perf_counter() - start
    assert response.status_code == 200, (method, path, response.text)
    return elapsed

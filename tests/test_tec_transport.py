"""Tests for C5 ``transport/base.py`` and ``transport/hidapi_transport.py`` (Task 7).

Requirements: FR-4, FR-4.1, FR-4.6, FR-5.3, FR-6.4, FR-3.7, NFR-5, NFR-8.

SAFETY: the host that runs this suite may have real Inheco TEC controllers on USB. The autouse
fixture ``stub_hid`` replaces the ``hid`` module in ``sys.modules`` for EVERY test in this file,
so ``HidapiTransport.open()`` always imports the stub and never the native library. The one test
that checks the lazy import runs in a subprocess with an import blocker that refuses ``hid``.
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from inheco_tec_api import logging_setup
from inheco_tec_api.transport import hidapi_transport
from inheco_tec_api.transport.base import (
    DeviceIOError,
    DeviceNotFound,
    DeviceOpenError,
    TransportError,
)
from inheco_tec_api.transport.hidapi_transport import (
    HidapiTransport,
    HidapiTransportFactory,
)

# Distinctive synthetic USB path. No log line and no exception message may contain it (FR-3.7).
SENTINEL_PATH = b"\\\\?\\hid#vid_03eb&pid_2023#SENTINEL-USB-PATH-9c41"
SENTINEL_TEXT = "SENTINEL-USB-PATH"


class StubDevice:
    """Stand-in for ``hid.device`` that records every call."""

    def __init__(self, hid: StubHid) -> None:
        self._hid = hid
        self.opened_path: bytes | None = None
        self.nonblocking: Any = None
        self.writes: list[bytes] = []
        self.reads: list[tuple[int, int]] = []
        self.close_calls = 0
        self.read_queue: list[list[int]] = []
        self.write_result: int | None = None
        self.open_error: OSError | None = None
        self.read_error: OSError | None = None
        self.write_error: OSError | None = None
        self.close_error: Exception | None = None

    def open_path(self, path: bytes) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened_path = path

    def set_nonblocking(self, value: Any) -> int:
        self.nonblocking = value
        return 0

    def write(self, data: bytes) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(bytes(data))
        return len(data) if self.write_result is None else self.write_result

    def read(self, max_length: int, timeout_ms: int = 0) -> list[int]:
        self.reads.append((max_length, timeout_ms))
        if self.read_error is not None:
            raise self.read_error
        if self.read_queue:
            return self.read_queue.pop(0)
        return []

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class StubHid(types.ModuleType):
    """Stand-in for the ``hid`` module of PyPI ``hidapi``."""

    def __init__(self) -> None:
        super().__init__("hid")
        self.devices: list[dict[str, Any]] = [{"path": SENTINEL_PATH}]
        self.enumerate_calls: list[tuple[int, int]] = []
        self.enumerate_error: OSError | None = None
        self.created: list[StubDevice] = []
        self.next_open_error: OSError | None = None

    def enumerate(self, vid: int = 0, pid: int = 0) -> list[dict[str, Any]]:
        self.enumerate_calls.append((vid, pid))
        if self.enumerate_error is not None:
            raise self.enumerate_error
        return list(self.devices)

    def device(self) -> StubDevice:
        dev = StubDevice(self)
        dev.open_error = self.next_open_error
        self.created.append(dev)
        return dev


@pytest.fixture(autouse=True)
def stub_hid(monkeypatch: pytest.MonkeyPatch) -> StubHid:
    """Install the stub ``hid`` module for every test (never the real native library)."""
    stub = StubHid()
    monkeypatch.setitem(sys.modules, "hid", stub)
    return stub


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Save and restore the root logger and the global structlog configuration."""
    import logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_structlog = structlog.get_config()
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        structlog.reset_defaults()
        structlog.configure(**saved_structlog)


def _opened(controller_id: int = 0) -> HidapiTransport:
    transport = HidapiTransport(controller_id)
    transport.open()
    return transport


def _device(stub: StubHid) -> StubDevice:
    assert len(stub.created) == 1
    return stub.created[0]


@pytest.mark.parametrize(("controller_id", "pid"), [(0, 0x2023), (3, 0x2026), (7, 0x202A)])
def test_fr4_1_open_enumerates_vid_and_pid_of_controller(
    stub_hid: StubHid, controller_id: int, pid: int
) -> None:
    transport = _opened(controller_id)

    assert stub_hid.enumerate_calls == [(0x03EB, pid)]
    dev = _device(stub_hid)
    assert dev.opened_path == SENTINEL_PATH
    assert dev.nonblocking in (False, 0)  # blocking mode
    assert transport.is_open


@pytest.mark.parametrize("controller_id", [-1, 8])
def test_fr4_1_controller_id_out_of_range_is_rejected(controller_id: int) -> None:
    with pytest.raises(ValueError):
        HidapiTransport(controller_id)


def test_fr4_1_not_found_raises_device_not_found(stub_hid: StubHid) -> None:
    stub_hid.devices = []
    transport = HidapiTransport(3)

    with pytest.raises(DeviceNotFound) as info:
        transport.open()

    assert isinstance(info.value, TransportError)
    assert stub_hid.created == []
    assert not transport.is_open


def test_fr4_1_open_path_oserror_raises_device_open_error(stub_hid: StubHid) -> None:
    stub_hid.next_open_error = OSError("open failed")
    transport = HidapiTransport(0)

    with pytest.raises(DeviceOpenError):
        transport.open()

    assert not transport.is_open
    assert _device(stub_hid).close_calls == 1  # the half-opened handle is released


def test_fr4_1_enumerate_oserror_raises_device_open_error(stub_hid: StubHid) -> None:
    stub_hid.enumerate_error = OSError("enumeration failed")

    with pytest.raises(DeviceOpenError):
        HidapiTransport(0).open()


def test_fr4_1_handle_stays_open_across_calls(stub_hid: StubHid) -> None:
    transport = _opened(0)
    dev = _device(stub_hid)
    dev.read_queue = [[0x30] * 8, [0x31] * 8]

    transport.write_report(bytes(9))
    transport.read_report(100)
    transport.write_report(bytes(9))
    transport.read_report(100)

    assert len(stub_hid.created) == 1
    assert len(stub_hid.enumerate_calls) == 1
    assert dev.close_calls == 0
    assert transport.is_open


def test_fr4_1_second_open_is_a_no_op(stub_hid: StubHid) -> None:
    transport = _opened(0)
    transport.open()

    assert len(stub_hid.enumerate_calls) == 1
    assert len(stub_hid.created) == 1
    assert transport.is_open


def test_fr4_1_reopen_after_close_creates_new_handle(stub_hid: StubHid) -> None:
    transport = _opened(0)
    transport.close()
    transport.open()

    assert len(stub_hid.created) == 2
    assert transport.is_open


def test_fr4_close_is_idempotent_and_never_raises(stub_hid: StubHid) -> None:
    transport = HidapiTransport(0)
    transport.close()  # never opened

    transport.open()
    dev = _device(stub_hid)
    dev.close_error = OSError("close failed")
    transport.close()
    transport.close()

    assert dev.close_calls == 1
    assert not transport.is_open


def test_fr4_write_report_sends_exactly_nine_bytes(stub_hid: StubHid) -> None:
    transport = _opened(0)
    report = bytes([0x00]) + b"1STT370\x5a"

    transport.write_report(report)

    assert _device(stub_hid).writes == [report]


@pytest.mark.parametrize("length", [0, 8, 10, 65])
def test_fr4_write_report_rejects_wrong_length_before_io(stub_hid: StubHid, length: int) -> None:
    transport = _opened(0)

    with pytest.raises(ValueError):
        transport.write_report(bytes(length))

    assert _device(stub_hid).writes == []


def test_fr4_6_read_report_returns_payload_bytes(stub_hid: StubHid) -> None:
    transport = _opened(0)
    payload = list(b"1sttX0#\x00")
    _device(stub_hid).read_queue = [payload]

    data = transport.read_report(250)

    assert data == bytes(payload)
    assert _device(stub_hid).reads == [(64, 250)]


def test_fr4_6_read_timeout_returns_none(stub_hid: StubHid) -> None:
    transport = _opened(0)

    assert transport.read_report(100) is None


def test_fr4_6_no_read_is_issued_with_timeout_zero(stub_hid: StubHid) -> None:
    transport = _opened(0)
    dev = _device(stub_hid)
    dev.read_queue = [[0x41] * 8, [0x42] * 8]

    transport.read_report(0)
    transport.read_report(-5)
    transport.flush_input()

    assert dev.reads
    assert all(timeout >= 1 for _, timeout in dev.reads)


def test_fr6_4_read_oserror_raises_device_io_error(stub_hid: StubHid) -> None:
    transport = _opened(0)
    _device(stub_hid).read_error = OSError("read error")

    with pytest.raises(DeviceIOError):
        transport.read_report(100)


def test_fr6_4_write_oserror_raises_device_io_error(stub_hid: StubHid) -> None:
    transport = _opened(0)
    _device(stub_hid).write_error = OSError("write error")

    with pytest.raises(DeviceIOError):
        transport.write_report(bytes(9))


def test_fr6_4_write_negative_return_raises_device_io_error(stub_hid: StubHid) -> None:
    transport = _opened(0)
    _device(stub_hid).write_result = -1

    with pytest.raises(DeviceIOError):
        transport.write_report(bytes(9))


def test_fr6_4_io_on_unopened_transport_raises_device_io_error() -> None:
    transport = HidapiTransport(0)

    with pytest.raises(DeviceIOError):
        transport.write_report(bytes(9))
    with pytest.raises(DeviceIOError):
        transport.read_report(10)


def test_fr6_4_flush_oserror_raises_device_io_error(stub_hid: StubHid) -> None:
    transport = _opened(0)
    _device(stub_hid).read_error = OSError("read error")

    with pytest.raises(DeviceIOError):
        transport.flush_input()


def test_fr5_3_flush_discards_queued_reports_and_returns_count(stub_hid: StubHid) -> None:
    transport = _opened(0)
    dev = _device(stub_hid)
    dev.read_queue = [[0x30] * 8 for _ in range(3)]

    assert transport.flush_input() == 3
    assert dev.read_queue == []
    assert len(dev.reads) == 4  # three reports plus the empty read that ends the loop


def test_fr5_3_flush_on_empty_queue_returns_zero(stub_hid: StubHid) -> None:
    transport = _opened(0)

    assert transport.flush_input() == 0
    assert len(_device(stub_hid).reads) == 1


def test_fr5_3_flush_is_bounded_to_64_reports(stub_hid: StubHid) -> None:
    transport = _opened(0)
    dev = _device(stub_hid)
    dev.read_queue = [[0x30] * 8 for _ in range(100)]

    assert transport.flush_input() == 64
    assert len(dev.reads) == 64
    assert len(dev.read_queue) == 36


def test_fr3_7_usb_path_absent_from_logs_and_exception_messages(
    stub_hid: StubHid, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    logging_setup.configure("DEBUG")
    messages: list[str] = []

    # One device; the multi-device warning and DeviceOpenError are in test_tec_transport_open.py.
    transport = _opened(0)
    dev = _device(stub_hid)
    dev.read_error = OSError("io failed")
    with pytest.raises(DeviceIOError) as read_info:
        transport.read_report(5)
    dev.write_error = OSError("io failed")
    with pytest.raises(DeviceIOError) as write_info:
        transport.write_report(bytes(9))
    messages += [str(read_info.value), repr(read_info.value)]
    messages += [str(write_info.value), repr(write_info.value)]
    transport.close()

    stub_hid.next_open_error = OSError("open failed")
    with pytest.raises(DeviceOpenError) as open_info:
        HidapiTransport(0).open()
    messages += [str(open_info.value), repr(open_info.value)]

    stub_hid.devices = []
    with pytest.raises(DeviceNotFound) as nf_info:
        HidapiTransport(0).open()
    messages += [str(nf_info.value), repr(nf_info.value)]

    captured = capsys.readouterr()
    for text in [captured.out, captured.err, *messages]:
        assert SENTINEL_TEXT not in text


def test_fr3_7_usb_path_not_stored_on_transport(stub_hid: StubHid) -> None:
    transport = _opened(0)

    assert SENTINEL_TEXT not in repr(vars(transport))


def test_fr4_factory_creates_unopened_real_transport(stub_hid: StubHid) -> None:
    factory = HidapiTransportFactory()

    transport = factory.create(3)

    assert factory.is_fake is False
    assert isinstance(transport, HidapiTransport)
    assert transport.is_fake is False
    assert transport.controller_id == 3
    assert not transport.is_open
    assert stub_hid.enumerate_calls == []
    assert stub_hid.created == []


_LAZY_IMPORT_PROBE = """
import sys

class _BlockHid:
    def find_spec(self, name, path=None, target=None):
        if name == "hid" or name.startswith("hid."):
            raise ImportError("hid import blocked by test")
        return None

sys.meta_path.insert(0, _BlockHid())
sys.modules.pop("hid", None)
import inheco_tec_api.transport.hidapi_transport as m
import inheco_tec_api.transport.base
print("HID_LOADED_AFTER_IMPORT=" + str("hid" in sys.modules))
t = m.HidapiTransportFactory().create(0)
try:
    t.open()
except ImportError as exc:
    print("OPEN_IMPORT_ERROR=" + str(exc))
"""


def test_nfr5_module_import_does_not_import_hid_until_open() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _LAZY_IMPORT_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "HID_LOADED_AFTER_IMPORT=False" in result.stdout
    assert "OPEN_IMPORT_ERROR=hid import blocked by test" in result.stdout


def test_nfr5_open_imports_hid_lazily_from_sys_modules(stub_hid: StubHid) -> None:
    assert not hasattr(hidapi_transport, "hid")

    _opened(0)

    assert stub_hid.enumerate_calls  # open() resolved ``hid`` to the stub at call time

"""Tests for amendment A3 of C5 ``HidapiTransport.open()`` (Task 7, sub-task 7.6).

Requirements: FR-4.1 (more than one VID/PID match fails closed with ``DeviceOpenError``; no device
is opened), FR-3.7 (the warning and the exception carry no USB path).

SAFETY: the host that runs this suite may have real Inheco TEC controllers on USB. The autouse
fixture ``stub_hid`` replaces the ``hid`` module in ``sys.modules`` for EVERY test in this file,
so ``HidapiTransport.open()`` always imports the stub and never the native library.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from inheco_tec_api import logging_setup
from inheco_tec_api.transport.base import DeviceNotFound, DeviceOpenError, TransportError
from inheco_tec_api.transport.hidapi_transport import (
    EVENT_TEC_HID_MULTIPLE_DEVICES,
    HidapiTransport,
)
from structlog.testing import capture_logs

# Distinctive synthetic USB paths. No log line and no exception text may contain them (FR-3.7).
SENTINEL_TEXT = "SENTINEL-USB-PATH"
SENTINEL_PATHS = [
    b"\\\\?\\hid#vid_03eb&pid_2023#SENTINEL-USB-PATH-A-9c41",
    b"\\\\?\\hid#vid_03eb&pid_2023#SENTINEL-USB-PATH-B-77d2",
    b"\\\\?\\hid#vid_03eb&pid_2023#SENTINEL-USB-PATH-C-1f08",
]


class StubDevice:
    """Stand-in for ``hid.device``; records ``open_path`` calls on the owning stub module."""

    def __init__(self, hid: StubHid) -> None:
        self._hid = hid
        self.nonblocking: Any = None

    def open_path(self, path: bytes) -> None:
        self._hid.open_path_calls.append(path)

    def set_nonblocking(self, value: Any) -> int:
        self.nonblocking = value
        return 0

    def close(self) -> None:
        pass


class StubHid(types.ModuleType):
    """Stand-in for the ``hid`` module of PyPI ``hidapi`` that records every call."""

    def __init__(self) -> None:
        super().__init__("hid")
        self.devices: list[dict[str, Any]] = [{"path": SENTINEL_PATHS[0]}]
        self.enumerate_calls: list[tuple[int, int]] = []
        self.device_calls = 0
        self.open_path_calls: list[bytes] = []

    def enumerate(self, vid: int = 0, pid: int = 0) -> list[dict[str, Any]]:
        self.enumerate_calls.append((vid, pid))
        return list(self.devices)

    def device(self) -> StubDevice:
        self.device_calls += 1
        return StubDevice(self)


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


def _set_matches(stub: StubHid, count: int) -> None:
    stub.devices = [{"path": path} for path in SENTINEL_PATHS[:count]]


@pytest.mark.parametrize("count", [2, 3])
def test_fr4_1_multiple_matches_raise_device_open_error_without_opening(
    stub_hid: StubHid, count: int
) -> None:
    _set_matches(stub_hid, count)
    transport = HidapiTransport(0)

    with pytest.raises(DeviceOpenError) as info:
        transport.open()

    assert isinstance(info.value, TransportError)
    assert not isinstance(info.value, DeviceNotFound)
    assert stub_hid.enumerate_calls == [(0x03EB, 0x2023)]
    assert stub_hid.device_calls == 0  # hid.device() never called
    assert stub_hid.open_path_calls == []  # open_path never called
    assert not transport.is_open


def test_fr4_1_multiple_matches_stay_closed_on_repeated_open(stub_hid: StubHid) -> None:
    _set_matches(stub_hid, 2)
    transport = HidapiTransport(0)

    for _ in range(2):
        with pytest.raises(DeviceOpenError):
            transport.open()

    assert len(stub_hid.enumerate_calls) == 2
    assert stub_hid.device_calls == 0
    assert stub_hid.open_path_calls == []
    assert not transport.is_open


@pytest.mark.parametrize(("controller_id", "count"), [(0, 2), (0, 3), (5, 2)])
def test_fr4_1_multiple_matches_log_one_warning_with_id_and_count_only(
    stub_hid: StubHid, controller_id: int, count: int
) -> None:
    _set_matches(stub_hid, count)

    with capture_logs() as events, pytest.raises(DeviceOpenError):
        HidapiTransport(controller_id).open()

    warnings = [e for e in events if e["event"] == EVENT_TEC_HID_MULTIPLE_DEVICES]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["log_level"] == "warning"
    assert warning["controller_id"] == controller_id
    assert warning["device_count"] == count
    assert set(warning) == {"event", "log_level", "controller_id", "device_count"}
    assert stub_hid.enumerate_calls == [(0x03EB, 0x2023 + controller_id)]


def test_fr3_7_multiple_matches_path_absent_from_logs_and_exception(
    stub_hid: StubHid, restore_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_matches(stub_hid, 3)

    with capture_logs() as events, pytest.raises(DeviceOpenError) as info:
        HidapiTransport(0).open()

    assert SENTINEL_TEXT not in repr(events)
    for text in (str(info.value), repr(info.value)):
        assert SENTINEL_TEXT not in text

    # Rendered output through the real logging configuration (FR-3.7 applies to the log sink too).
    logging_setup.configure("DEBUG")
    with pytest.raises(DeviceOpenError):
        HidapiTransport(0).open()
    captured = capsys.readouterr()
    assert EVENT_TEC_HID_MULTIPLE_DEVICES in captured.out  # logging really was captured
    assert captured.out.count(EVENT_TEC_HID_MULTIPLE_DEVICES) == 1
    for text in (captured.out, captured.err):
        assert SENTINEL_TEXT not in text


def test_fr4_1_zero_matches_still_raise_device_not_found(stub_hid: StubHid) -> None:
    stub_hid.devices = []
    transport = HidapiTransport(0)

    with capture_logs() as events, pytest.raises(DeviceNotFound):
        transport.open()

    assert not [e for e in events if e["event"] == EVENT_TEC_HID_MULTIPLE_DEVICES]
    assert stub_hid.device_calls == 0
    assert stub_hid.open_path_calls == []
    assert not transport.is_open


def test_fr4_1_single_match_still_opens(stub_hid: StubHid) -> None:
    _set_matches(stub_hid, 1)
    transport = HidapiTransport(0)

    with capture_logs() as events:
        transport.open()

    assert not [e for e in events if e["event"] == EVENT_TEC_HID_MULTIPLE_DEVICES]
    assert stub_hid.device_calls == 1
    assert stub_hid.open_path_calls == [SENTINEL_PATHS[0]]
    assert transport.is_open

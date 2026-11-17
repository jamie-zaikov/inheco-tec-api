"""HID transport interface and transport exceptions (design C5; FR-4, FR-4.1, FR-4.6, FR-6.4).

A transport moves raw HID reports for one Inheco TEC controller; it knows nothing about commands,
CRC or replies (that is the protocol codec, C3, and the transaction engine, C7).

- Output reports are exactly 9 bytes: report ID ``0x00`` plus 8 data bytes (protocol report §1.3
  step 5).
- Input reports are returned as the data payload **without** the report-ID byte, which the DLL drops
  in ``ReadOnlyAssync`` (protocol report §1.4 step 2). Whether ``hidapi`` already strips it is
  INFERRED — see api/README.md checklist item 1.

Two implementations exist: :class:`~.hidapi_transport.HidapiTransport` (the real device) and the
fake transport (C6). No exception message carries the USB device path (FR-3.7).
"""

from __future__ import annotations

from typing import Protocol


class TransportError(Exception):
    """Base class of every transport failure. Messages never contain the USB path (FR-3.7)."""


class DeviceNotFound(TransportError):
    """No HID device with the controller's VID/PID is present (protocol report §1.1)."""


class DeviceOpenError(TransportError):
    """A matching HID device exists but the OS refused to open it."""


class DeviceIOError(TransportError):
    """A HID read or write failed at the OS level: device removed or handle invalid (FR-6.4)."""


class HidTransport(Protocol):
    """Report-level access to one controller's HID device (design C5).

    The handle stays open across transactions (FR-4.1); the caller serialises access with the
    controller lock, so implementations need not be thread-safe.
    """

    is_fake: bool

    def open(self) -> None:
        """Open the device. Raises :class:`DeviceNotFound` or :class:`DeviceOpenError`."""
        ...

    def close(self) -> None:
        """Release the handle. Idempotent; never raises."""
        ...

    def write_report(self, report: bytes) -> None:
        """Write one output report of exactly 9 bytes. Raises :class:`DeviceIOError`."""
        ...

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Read one input report payload without the report ID; ``None`` on timeout.

        Raises :class:`DeviceIOError`.
        """
        ...

    def flush_input(self) -> int:
        """Discard queued input reports (FR-5.3); return how many were discarded."""
        ...


class TransportFactory(Protocol):
    """Creates the transport for a ControllerID (design C5)."""

    is_fake: bool

    def create(self, controller_id: int) -> HidTransport:
        """Return an unopened transport for ``controller_id``."""
        ...

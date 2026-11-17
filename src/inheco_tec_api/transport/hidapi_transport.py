"""Real HID transport over the ``hid`` module of PyPI ``hidapi`` (design C5; FR-4, FR-4.1, FR-6.4).

This is the only module of the package that imports ``hid``, and it does so lazily inside
:meth:`HidapiTransport.open`, so importing this module never loads the native library (tests and
Linux CI run without it; NFR-5).

Protocol rules ported here:

- Device identification: VID ``0x03EB``, PID ``0x2023 + ControllerID`` (protocol report §1.1,
  ``GlobCom.FindTheUniversalControl`` IL_0029..IL_0036).
- One handle, opened once and held across transactions (FR-4.1). The DLL re-opens before every
  command and leaks handles (protocol report §1.2); that behaviour is not ported.
- Output reports are 9 bytes: report ID ``0x00`` plus 8 data bytes (protocol report §1.3 step 5).
- Input reports are returned without the report-ID byte, which the DLL drops in ``ReadOnlyAssync``
  (protocol report §1.4 step 2). ``hidapi`` removes report ID 0 itself for devices without numbered
  reports, so the bytes it returns are already the payload. This is INFERRED — see api/README.md
  checklist item 1. If the hardware shows a leading ``0x00``, the fix belongs in
  :meth:`HidapiTransport._payload` and nowhere else.

The USB device path is held only in a local variable during :meth:`HidapiTransport.open`. It is
never stored on the object, never logged, and never put into an exception message (FR-3.7).
"""

from __future__ import annotations

from typing import Any, Final

from inheco_tec_api.logging_setup import get_logger
from inheco_tec_api.protocol.framing import REPORT_SIZE
from inheco_tec_api.transport.base import (
    DeviceIOError,
    DeviceNotFound,
    DeviceOpenError,
    HidTransport,
)

VENDOR_ID: Final = 0x03EB
"""Atmel VID of every Inheco TEC controller (protocol report §1.1)."""

PRODUCT_ID_BASE: Final = 0x2023
"""PID of ControllerID 0; ControllerID *n* has PID ``0x2023 + n`` (protocol report §1.1)."""

MAX_CONTROLLER_ID: Final = 7
"""Highest ControllerID (DIP switch 0-7; PID 0x2023-0x202A, protocol report §1.1)."""

READ_SIZE: Final = 64
"""Maximum bytes requested per ``hid`` read; larger than any input report (protocol report §1.4)."""

FLUSH_BOUND: Final = 64
"""Upper bound on reports discarded by one :meth:`HidapiTransport.flush_input` call (design C5)."""

_MIN_TIMEOUT_MS: Final = 1
"""Smallest timeout passed to ``hid.device.read``.

The ``hid`` binding treats ``timeout_ms=0`` as "no timeout" (a blocking ``hid_read``), not as a
poll. A flush or a zero-timeout read therefore uses 1 ms, so that an empty queue can never block
the controller lock indefinitely.
"""

EVENT_TEC_HID_MULTIPLE_DEVICES: Final = "tec_hid_multiple_devices"
"""Warning event: more than one HID device matched the controller's VID/PID (design C5).

Logged just before :meth:`HidapiTransport.open` raises ``DeviceOpenError``; no device is opened.
Fields: ``controller_id``, ``device_count``. The USB paths are deliberately not logged (FR-3.7).
"""

_log = get_logger(__name__)


def product_id(controller_id: int) -> int:
    """Return the USB PID of ``controller_id`` (protocol report §1.1)."""
    if not 0 <= controller_id <= MAX_CONTROLLER_ID:
        raise ValueError(f"controller_id must be 0-{MAX_CONTROLLER_ID}, got {controller_id}")
    return PRODUCT_ID_BASE + controller_id


class HidapiTransport:
    """:class:`~.base.HidTransport` for one physical controller, selected by USB PID."""

    is_fake: bool = False

    def __init__(self, controller_id: int) -> None:
        self._controller_id = controller_id
        self._product_id = product_id(controller_id)
        self._device: Any | None = None

    @property
    def controller_id(self) -> int:
        """The ControllerID this transport addresses."""
        return self._controller_id

    @property
    def is_open(self) -> bool:
        """Whether the HID handle is currently open."""
        return self._device is not None

    def open(self) -> None:
        """Enumerate VID/PID, open the single match in blocking mode, and hold the handle.

        A second call while the handle is open does nothing (FR-4.1: no reopen per command).
        Raises :class:`DeviceNotFound` when no device matches. When more than one device matches,
        logs one warning and raises :class:`DeviceOpenError` without opening any device (fail
        closed, FR-4.1). Raises :class:`DeviceOpenError` when the OS refuses the open.
        """
        if self._device is not None:
            return
        import hid  # type: ignore[import-not-found]  # lazy: the only import of the native library

        try:
            matches = hid.enumerate(VENDOR_ID, self._product_id)
        except OSError as exc:
            raise DeviceOpenError(
                f"HID enumeration failed for controller {self._controller_id}"
            ) from exc
        if not matches:
            raise DeviceNotFound(f"no HID device found for controller {self._controller_id}")
        if len(matches) > 1:
            # Fail closed: an ambiguous match must never open an arbitrary device (FR-4.1).
            _log.warning(
                EVENT_TEC_HID_MULTIPLE_DEVICES,
                controller_id=self._controller_id,
                device_count=len(matches),
            )
            raise DeviceOpenError(
                f"{len(matches)} HID devices match controller {self._controller_id}; "
                "refusing to open any"
            )
        path = matches[0]["path"]
        device = hid.device()
        try:
            device.open_path(path)
            device.set_nonblocking(False)
        except OSError as exc:
            _close_quietly(device)
            raise DeviceOpenError(
                f"cannot open HID device for controller {self._controller_id}"
            ) from exc
        self._device = device

    def close(self) -> None:
        """Release the handle. Idempotent; never raises."""
        device, self._device = self._device, None
        if device is not None:
            _close_quietly(device)

    def write_report(self, report: bytes) -> None:
        """Write one 9-byte output report (protocol report §1.3 step 5).

        A report of any other length raises ``ValueError`` before any I/O. A negative return or
        an ``OSError`` raises :class:`DeviceIOError` (FR-6.4).
        """
        if len(report) != REPORT_SIZE:
            raise ValueError(f"HID output report must be {REPORT_SIZE} bytes, got {len(report)}")
        device = self._require_open()
        try:
            written = device.write(bytes(report))
        except OSError as exc:
            raise DeviceIOError(f"HID write failed for controller {self._controller_id}") from exc
        if written < 0:
            raise DeviceIOError(f"HID write failed for controller {self._controller_id}")

    def read_report(self, timeout_ms: int) -> bytes | None:
        """Read one input-report payload without the report ID; ``None`` on timeout.

        An ``OSError`` raises :class:`DeviceIOError` (FR-6.4).
        """
        device = self._require_open()
        try:
            data = device.read(READ_SIZE, max(timeout_ms, _MIN_TIMEOUT_MS))
        except OSError as exc:
            raise DeviceIOError(f"HID read failed for controller {self._controller_id}") from exc
        if not data:
            return None
        return self._payload(data)

    def flush_input(self) -> int:
        """Discard queued input reports, at most :data:`FLUSH_BOUND` of them (FR-5.3).

        Mirrors the DLL's ``HidD_FlushQueue`` after open (protocol report §1.2 step 4). Returns the
        number of reports discarded. An ``OSError`` raises :class:`DeviceIOError`.
        """
        discarded = 0
        while discarded < FLUSH_BOUND:
            if self.read_report(_MIN_TIMEOUT_MS) is None:
                break
            discarded += 1
        return discarded

    @staticmethod
    def _payload(data: list[int] | bytes) -> bytes:
        """Normalise one ``hid`` read to the payload without the report ID.

        ``hidapi`` has already removed report ID 0, so this is the identity. INFERRED — see
        api/README.md checklist item 1.
        """
        return bytes(data)

    def _require_open(self) -> Any:
        if self._device is None:
            raise DeviceIOError(f"HID device for controller {self._controller_id} is not open")
        return self._device


class HidapiTransportFactory:
    """:class:`~.base.TransportFactory` that creates :class:`HidapiTransport` instances."""

    is_fake: bool = False

    def create(self, controller_id: int) -> HidTransport:
        """Return an unopened :class:`HidapiTransport` for ``controller_id``."""
        return HidapiTransport(controller_id)


def _close_quietly(device: Any) -> None:
    """Close a ``hid.device`` and swallow every error (``close`` must never raise)."""
    try:
        device.close()
    except Exception:  # noqa: BLE001 - close is best effort by contract (design C5)
        pass

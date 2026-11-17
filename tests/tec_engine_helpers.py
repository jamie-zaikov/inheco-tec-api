"""Shared helpers for the transaction-engine suites (not a test module; Task 9).

- :class:`RecordingTransport` wraps a ``FakeTecDevice`` and records every transport call in order,
  every ``read_report`` timeout (carry-forward T8-C1), the ``close()`` count and the ``flush_input``
  return values. It can inject extra reply payloads after the next command write (more stale
  replies than one fake ``StaleReply`` rule queues) and can run a hook inside ``read_report`` so a
  concurrency test can block or yield inside a transaction (carry-forward T8-C2).
- :class:`VirtualTime` is an injected ``sleep``/``clock`` pair: ``sleep`` records the duration and
  advances the clock, so retry spacing is checked without wall-clock waits.

The tests directory has no ``__init__.py`` (design Risk R8); modules import this file by its unique
top-level name.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from inheco_tec_api.engine import TransactionEngine
from inheco_tec_api.manifest import TransportConfig
from inheco_tec_api.transport.fake import FakeTecDevice, encode_reply


class VirtualTime:
    """Deterministic monotonic time; ``sleep`` advances it and records each duration."""

    def __init__(self, start: float = 500.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingTransport:
    """A ``HidTransport`` wrapper over a ``FakeTecDevice`` that records every call."""

    is_fake: bool = True

    def __init__(self, device: FakeTecDevice) -> None:
        self.device = device
        self.events: list[tuple[str, Any]] = []
        self.read_timeouts: list[Any] = []
        self.flush_results: list[int] = []
        self.close_count = 0
        self.read_hook: Callable[[], None] | None = None
        self.max_reads: int | None = None
        self._inject_next: list[str] = []
        self._injected: deque[bytes] = deque()

    # -- test controls ------------------------------------------------------------------------

    def inject_after_next_command(self, *texts: str) -> None:
        """Queue reply ``texts`` (valid CRC) to be returned after the next complete command."""
        self._inject_next.extend(texts)

    @property
    def read_count(self) -> int:
        return sum(1 for name, _ in self.events if name == "read")

    def names(self) -> list[str]:
        """Event names in order; writes appear as ``write:<command>`` once per command."""
        return [name if value is None else f"{name}:{value}" for name, value in self.events]

    # -- HidTransport -------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.device.is_open

    def open(self) -> None:
        self.events.append(("open", None))
        self.device.open()

    def close(self) -> None:
        self.close_count += 1
        self.events.append(("close", None))
        self.device.close()

    def write_report(self, report: bytes) -> None:
        before = len(self.device.writes)
        self.device.write_report(report)
        if len(self.device.writes) > before:
            self.events.append(("write", self.device.writes[-1].command_str))
            for text in self._inject_next:
                self._injected.extend(encode_reply(text))
            self._inject_next.clear()

    def read_report(self, timeout_ms: int) -> bytes | None:
        self.read_timeouts.append(timeout_ms)
        if self.max_reads is not None and self.read_count >= self.max_reads:
            raise AssertionError("engine kept reading: busy loop suspected")
        if self.read_hook is not None:
            self.read_hook()
        if self._injected:
            payload: bytes | None = self._injected.popleft()
        else:
            payload = self.device.read_report(timeout_ms)
        self.events.append(("read", None if payload is None else "data"))
        return payload

    def flush_input(self) -> int:
        self._injected.clear()
        count = self.device.flush_input()
        self.flush_results.append(count)
        self.events.append(("flush", None))
        return count


def settings(**overrides: Any) -> TransportConfig:
    """A fake-kind ``TransportConfig`` with zero delays unless overridden."""
    values: dict[str, Any] = {"kind": "fake", "pre_send_delay_ms": 0, "retry_delay_ms": 0}
    values.update(overrides)
    return TransportConfig(**values)


def make_engine(
    *,
    device: FakeTecDevice | None = None,
    time_source: VirtualTime | None = None,
    controller_id: int = 0,
    **config: Any,
) -> tuple[TransactionEngine, RecordingTransport, FakeTecDevice, VirtualTime]:
    """Build an engine over a recording wrapper of an opened fake with virtual time.

    The fake records writes on the same virtual clock, so ``WriteRecord.monotonic_s`` shows the
    injected delays.
    """
    vt = time_source if time_source is not None else VirtualTime()
    dev = device if device is not None else FakeTecDevice(controller_id, clock=vt.clock)
    dev.open()
    transport = RecordingTransport(dev)
    engine = TransactionEngine(
        transport, controller_id, settings(**config), sleep=vt.sleep, clock=vt.clock
    )
    return engine, transport, dev, vt

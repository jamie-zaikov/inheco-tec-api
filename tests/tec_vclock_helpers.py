"""A hidapi-like virtual-clock transport for the engine deadline suite (not a test module; Task 9).

:class:`VirtualClockTransport` models the timing of the real blocking ``hid.device.read`` call
(carry-forward T8-C1, corrected): a read blocks until the next scheduled input report arrives, or
it consumes its whole timeout and returns ``None``. Every read advances the shared
:class:`~tec_engine_helpers.VirtualTime` clock, so the engine's per-attempt read bound (FR-4.8,
NFR-3) shows as virtual time. A read timeout that is not an ``int`` in ``1..read_timeout_ms`` is
refused with ``AssertionError`` (the binding takes a C int, and 0 means "block").

Replies are scheduled per written command, in write order, with :meth:`VirtualClockTransport.plan`.
Arrival offsets are seconds after the command's last output report. A command with no plan left
gets no reply. A ``FakeTecDevice`` decodes the writes only; its own emulated replies are dropped.

The tests directory has no ``__init__.py`` (design Risk R8); modules import this file by its unique
top-level name.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from inheco_tec_api.engine import TransactionEngine
from inheco_tec_api.manifest import TransportConfig
from inheco_tec_api.transport.fake import FakeTecDevice, encode_reply
from tec_engine_helpers import VirtualTime

Arrival = tuple[float, bytes]
"""One scheduled input report: (offset in seconds after the command write, payload)."""


def reply_at(offset_s: float, text: str) -> list[Arrival]:
    """Every payload of reply ``text`` (valid CRC) arrives ``offset_s`` after the write."""
    return [(offset_s, payload) for payload in encode_reply(text)]


def partial_at(text: str, first_s: float, rest_s: float | None = None) -> list[Arrival]:
    """The first payload of a multi-report ``text`` at ``first_s``; the rest at ``rest_s``.

    ``rest_s=None`` means the rest never arrives.
    """
    payloads = encode_reply(text)
    if len(payloads) < 2:
        raise ValueError("partial_at needs a reply that spans more than one input report")
    arrivals = [(first_s, payloads[0])]
    if rest_s is not None:
        arrivals += [(rest_s, payload) for payload in payloads[1:]]
    return arrivals


@dataclass(frozen=True, slots=True)
class ReadRecord:
    """One ``read_report`` call: its timeout, the virtual time it took, and whether data came."""

    timeout_ms: int
    elapsed_s: float
    data: bool


class VirtualClockTransport:
    """A ``HidTransport`` with blocking-read timing on a ``VirtualTime`` clock."""

    is_fake: bool = True

    def __init__(self, vt: VirtualTime, read_timeout_ms: int, *, max_reads: int = 200) -> None:
        self.vt = vt
        self.read_timeout_ms = read_timeout_ms
        self.max_reads = max_reads
        self.device = FakeTecDevice(0, clock=vt.clock)
        self.device.open()
        self.commands: list[str] = []
        self.reads: list[ReadRecord] = []
        self.exchanges: list[list[ReadRecord]] = []  # the reads after each command write
        self._plans: deque[list[Arrival]] = deque()
        self._queue: deque[tuple[float, bytes]] = deque()  # (absolute arrival, payload)

    # -- test controls ------------------------------------------------------------------------

    def plan(self, *per_command: list[Arrival]) -> None:
        """Schedule the input reports of the next commands, one list per command write."""
        self._plans.extend(per_command)

    @property
    def read_timeouts(self) -> list[int]:
        return [record.timeout_ms for record in self.reads]

    def read_time_per_exchange(self) -> list[float]:
        """Summed read time after each command write (an attempt or the resync)."""
        return [sum(record.elapsed_s for record in reads) for reads in self.exchanges]

    def timeouts_per_exchange(self) -> list[list[int]]:
        return [[record.timeout_ms for record in reads] for reads in self.exchanges]

    # -- HidTransport -------------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.device.is_open

    def open(self) -> None:
        raise AssertionError("the engine must never open the transport")

    def close(self) -> None:
        raise AssertionError("the engine must never close the transport")

    def write_report(self, report: bytes) -> None:
        before = len(self.device.writes)
        self.device.write_report(report)
        if len(self.device.writes) == before:
            return  # not the last report of a command yet
        self.device.flush_input()  # drop the emulated reply; the plan decides what arrives
        self.commands.append(self.device.writes[-1].command_str)
        self.exchanges.append([])
        written = self.vt.now
        arrivals = self._plans.popleft() if self._plans else []
        for offset_s, payload in sorted(arrivals, key=lambda arrival: arrival[0]):
            self._queue.append((written + offset_s, payload))

    def read_report(self, timeout_ms: int) -> bytes | None:
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= self.read_timeout_ms:
            raise AssertionError(
                f"read timeout must be an int in 1..{self.read_timeout_ms}, got {timeout_ms!r}"
            )
        if len(self.reads) >= self.max_reads:
            raise AssertionError("engine kept reading: busy loop suspected")
        start = self.vt.now
        limit = start + timeout_ms / 1000
        payload: bytes | None = None
        if self._queue and self._queue[0][0] <= limit:
            arrival, payload = self._queue.popleft()
            self.vt.now = max(start, arrival)
        else:
            self.vt.now = limit  # the whole timeout elapses
        record = ReadRecord(timeout_ms, self.vt.now - start, payload is not None)
        self.reads.append(record)
        if self.exchanges:
            self.exchanges[-1].append(record)
        return payload

    def flush_input(self) -> int:
        """Discard the reports that have arrived by now; later arrivals stay scheduled."""
        count = 0
        while self._queue and self._queue[0][0] <= self.vt.now:
            self._queue.popleft()
            count += 1
        return count


def make_vclock_engine(
    **config: object,
) -> tuple[TransactionEngine, VirtualClockTransport, VirtualTime]:
    """An engine with the manifest default timing over a :class:`VirtualClockTransport`.

    Defaults: pre-send 200 ms, read timeout 3000 ms, retry delay 1000 ms, 3 attempts, so
    ``worst_case_budget_s()`` is 15.8 s (NFR-3). ``config`` overrides any field.
    """
    values: dict[str, object] = {"kind": "fake"}
    values.update(config)
    settings = TransportConfig(**values)  # type: ignore[arg-type]
    vt = VirtualTime()
    transport = VirtualClockTransport(vt, int(settings.read_timeout_ms))
    engine = TransactionEngine(transport, 0, settings, sleep=vt.sleep, clock=vt.clock)
    return engine, transport, vt

"""Transaction engine: one command as one atomic HID exchange (design C7; FR-5, FR-9, FR-15.1).

A :class:`TransactionEngine` wraps one open :class:`~.transport.base.HidTransport` and executes one
allowlisted :class:`~.protocol.commands.Command` per :meth:`TransactionEngine.execute` call. The
steps port the HSL transaction engine ``_SendCommandToController`` (protocol report §3) with the
deviations of design C7: firmware errors are surfaced, never treated as success (FR-9), and the
retry set is the FR-5.6 transient set (protocol report §8.2).

Per call (design C7 steps 1–5, sequence S3):

1. Encode first: the allowlist check raises ``CommandNotAllowed`` before any I/O (FR-10.1).
2. Attempt loop: flush queued input (FR-5.3, NFR-7), wait the pre-send delay unconditionally
   (FR-5.2, DD-2; protocol report §3 step 4, §9), flush again to drop any reply that arrived
   during the delay (FR-5.3, amendment A2), write every report, read the reply with the
   echo match and the ``0rdc`` stale rule (FR-5.4; protocol report §3 step 5, §7), classify the
   error character (FR-5.6, FR-5.7, FR-9.3), and retry after the retry delay (protocol report §3
   step 5, §9) until the attempts are used up (FR-5.8).
3. After a ``0`` success for any command except ``RDC``: the checked ``0RDC1`` resync (FR-5.5,
   amendment A2): an echo-matching reply with a non-``0`` code read during the resync fails the
   transaction with that code.
4. A ``DeviceIOError`` raises :class:`DeviceLost` at once, without retry (FR-6.4).
5. One ``tec_transaction`` log event per call (FR-15.1, design C14).

Thread-safety (FR-5.1, DD-1; protocol report §7): the engine holds no lock of its own. The caller
must hold the controller lock around every :meth:`TransactionEngine.execute` call, so the write,
all reads, the retries and the resync of one transaction are never interleaved with another. The
transport is not thread-safe, ``close()`` included, so the engine never opens or closes the
transport; the controller (C9) owns the handle and closes it only while it holds the lock.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Literal

from inheco_tec_api.logging_setup import EVENT_TEC_TRANSACTION, get_logger
from inheco_tec_api.manifest import TransportConfig
from inheco_tec_api.protocol.commands import Command, CommandNotAllowed, rdc1
from inheco_tec_api.protocol.framing import (
    Reply,
    ReplyAssembler,
    ReplyCrcError,
    ReplyFrameError,
    encode_command,
    parse_reply,
)
from inheco_tec_api.protocol.tables import FIRMWARE_ERRORS, TRANSIENT_CODES
from inheco_tec_api.transport.base import DeviceIOError, HidTransport

__all__ = [
    "CommandNotAllowed",
    "DeviceLost",
    "FailureKind",
    "TransactionEngine",
    "TransactionFailed",
]

FailureKind = Literal["firmware", "no_reply", "crc", "parse"]

_STALE_NAME = "0rdc"
"""Echo name of a resync reply; read past for any other command (protocol report §3 step 5)."""

_RESYNC_MNEMONIC = "RDC"

EVENT_TEC_RESYNC_FAILED = "tec_resync_failed"
"""Warning event of a resync that got no valid reply (design C7 step 3). It is not one of the C14
events in ``logging_setup.EVENT_NAMES``; it carries ``controller_id``, ``slot``, ``mnemonic`` and a
``reason`` only."""

_log = get_logger(__name__)


class TransactionFailed(Exception):
    """A transaction completed as failed (FR-5.7, FR-5.8, FR-9.3).

    ``kind`` is ``firmware`` (``code`` is the §8.2 error character), or a transport failure
    kind ``no_reply``, ``crc`` or ``parse`` (``code`` is ``None``). ``attempts`` counts the
    attempts made.
    """

    def __init__(self, kind: FailureKind, code: str | None, attempts: int) -> None:
        self.kind: FailureKind = kind
        self.code = code
        self.attempts = attempts
        detail = f"firmware code {code}" if kind == "firmware" else kind
        super().__init__(f"transaction failed: {detail} after {attempts} attempt(s)")


class DeviceLost(Exception):
    """A HID read or write failed at the OS level during a transaction (FR-6.4).

    Raised at once, without retry. The controller (C9) marks itself ``lost`` and releases the
    handle; the engine never closes the transport itself.
    """


class _AttemptFailed(Exception):
    """Internal: one attempt failed; ``retryable`` selects retry (FR-5.6) or fail fast."""

    def __init__(self, kind: FailureKind, code: str | None = None, *, retryable: bool) -> None:
        super().__init__(kind)
        self.kind: FailureKind = kind
        self.code = code
        self.retryable = retryable


class _NoReply(Exception):
    """Internal: no complete reply arrived within the attempt's one read timeout (FR-4.8)."""


class TransactionEngine:
    """Executes allowlisted commands on one open transport (design C7).

    ``settings`` supplies ``pre_send_delay_ms``, ``read_timeout_ms``, ``retry_delay_ms``,
    ``max_attempts`` and ``max_stale_reads`` (FR-2.3, FR-5). ``sleep`` and ``clock`` are injected
    for tests; ``clock`` is a monotonic time in seconds. The caller holds the controller lock around
    :meth:`execute` (FR-5.1); the engine never closes the transport.
    """

    def __init__(
        self,
        transport: HidTransport,
        controller_id: int,
        settings: TransportConfig,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._controller_id = controller_id
        self._settings = settings
        self._sleep = sleep
        self._clock = clock
        # Integer milliseconds: the real hidapi binding passes the timeout to a C int.
        self._read_timeout_ms = int(settings.read_timeout_ms)
        self._read_timeout_s = settings.read_timeout_ms / 1000
        self._pre_send_s = settings.pre_send_delay_ms / 1000
        self._retry_delay_s = settings.retry_delay_ms / 1000
        self._attempts = 0  # attempts made by the current execute() call (caller holds the lock)

    @property
    def controller_id(self) -> int:
        """The ControllerID this engine serves."""
        return self._controller_id

    def worst_case_budget_s(self, max_attempts: int | None = None) -> float:
        """Worst-case duration of one transaction, in seconds (design C7; NFR-3).

        ``max_attempts × (pre_send + read_timeout + retry_delay) + (pre_send + read_timeout)``: the
        attempts plus the resync exchange. 3 × 4.2 s + 3.2 s = 15.8 s with the defaults.
        """
        attempts = self._settings.max_attempts if max_attempts is None else max_attempts
        per_attempt = self._pre_send_s + self._read_timeout_s + self._retry_delay_s
        return attempts * per_attempt + (self._pre_send_s + self._read_timeout_s)

    def execute(self, cmd: Command, *, max_attempts: int | None = None) -> Reply:
        """Run ``cmd`` as one transaction and return its ``0`` reply (design C7 steps 1–5).

        ``max_attempts`` overrides the configured attempt count for this call (at least 1).
        Raises ``CommandNotAllowed`` before any I/O, :class:`TransactionFailed` when the
        transaction fails, and :class:`DeviceLost` on an OS-level HID failure. The caller must
        hold the controller lock (FR-5.1).
        """
        limit = self._settings.max_attempts if max_attempts is None else max_attempts
        if limit < 1:
            raise ValueError(f"max_attempts must be at least 1, got {limit}")
        start = self._clock()
        self._attempts = 0
        result = "error"
        try:
            # Step 1: the allowlist check raises before any byte exists (FR-10.1).
            try:
                frames = encode_command(cmd)
            except CommandNotAllowed:
                result = "not_allowed"
                raise
            reply = self._attempt_loop(cmd, frames, limit)
            # Step 3: resync after a "0" success, except for RDC itself (FR-5.5).
            if cmd.mnemonic != _RESYNC_MNEMONIC:
                self._resync(cmd)
            result = "ok"
            return reply
        except TransactionFailed as exc:
            result = exc.code if exc.kind == "firmware" and exc.code else exc.kind
            raise
        except DeviceLost:
            result = "device_lost"
            raise
        finally:
            # Step 5: one tec_transaction event per call, FR-15.1 fields only.
            duration_ms = int(round((self._clock() - start) * 1000))
            log = _log.info if result == "ok" else _log.warning
            log(
                EVENT_TEC_TRANSACTION,
                controller_id=self._controller_id,
                slot=cmd.address,
                mnemonic=cmd.mnemonic,
                param=cmd.param,
                result=result,
                attempts=self._attempts,
                duration_ms=duration_ms,
            )

    # -- step 2: attempts --------------------------------------------------------------------

    def _attempt_loop(self, cmd: Command, frames: list[bytes], limit: int) -> Reply:
        """Run up to ``limit`` attempts and return the ``0`` reply.

        A retryable failure (transient code, no reply, CRC; FR-5.6) waits the retry delay and
        resends (protocol report §3 step 5: wait 1.0 s and loop; §9). A non-retryable failure
        (another firmware code, FR-5.7; a parse failure, FR-9.3) raises at once. Exhausted
        attempts raise :class:`TransactionFailed` with the last code or kind (FR-5.8).
        """
        attempt = 1
        while True:
            self._attempts = attempt
            try:
                return self._attempt(cmd, frames)
            except _AttemptFailed as failure:
                if not failure.retryable or attempt >= limit:
                    raise TransactionFailed(failure.kind, failure.code, attempt) from None
            self._sleep(self._retry_delay_s)
            attempt += 1

    def _attempt(self, cmd: Command, frames: list[bytes]) -> Reply:
        """One attempt: flush, delay, flush, write, read, classify (design C7 steps 2.1–2.6)."""
        try:
            # FR-5.3: discard stale input before writing; also NFR-7 after an earlier failure
            # (protocol report §1.2 step 4 / §7: the DLL flushes on controller selection).
            self._transport.flush_input()
            # FR-5.2, DD-2: unconditional pre-send delay (protocol report §3 step 4, §9).
            self._sleep(self._pre_send_s)
            # FR-5.3 (amendment A2): discard any reply that arrived during the delay, immediately
            # before the write, so it is never read as this command's reply.
            self._transport.flush_input()
            for report in frames:
                self._transport.write_report(report)
            reply = self._read_matching(cmd)
        except DeviceIOError as exc:
            raise DeviceLost(f"controller {self._controller_id}: HID I/O failed") from exc
        return self._classify(reply)

    def _classify(self, reply: Reply) -> Reply:
        """Map the error character ``E`` to an outcome (protocol report §8.2; FR-5.6/5.7, FR-9.3).

        ``0`` is success. A transient code is a retryable attempt failure. A character outside the
        §8.2 table fails the transaction closed as ``parse``. Every other code fails fast as
        ``firmware`` with that code; unlike HSL v2.2 (protocol report §3), none is treated as
        success (FR-9).
        """
        code = reply.error_char
        if code == "0":
            return reply
        if code in TRANSIENT_CODES:
            raise _AttemptFailed("firmware", code, retryable=True)
        if code not in FIRMWARE_ERRORS:
            raise _AttemptFailed("parse", retryable=False)
        raise _AttemptFailed("firmware", code, retryable=False)

    def _read_matching(self, cmd: Command) -> Reply:
        """Read until the reply echoing ``cmd`` arrives (design C7 step 2.4; FR-4.8, FR-5.4).

        A ``0rdc`` reply for another command is stale and read past without a resend, and a reply
        whose first four characters differ from the sent address and mnemonic (case-insensitive)
        is discarded (protocol report §3 step 5, §7). All reads of the attempt share ONE read
        timeout (FR-4.8): more than ``max_stale_reads`` such replies, a ``None`` read, or the
        deadline passing on the injected clock is a ``no_reply`` attempt failure. A CRC error is a
        ``crc`` attempt failure; a frame error fails the transaction as ``parse`` with no retry
        (FR-9.3).
        """
        sent_name = f"{cmd.address}{cmd.mnemonic}".lower()
        deadline = self._clock() + self._read_timeout_s
        stale_reads = 0
        while True:
            try:
                reply = parse_reply(self._read_reply(deadline))
            except _NoReply:
                raise _AttemptFailed("no_reply", retryable=True) from None
            except ReplyCrcError:
                raise _AttemptFailed("crc", retryable=True) from None
            except ReplyFrameError:
                raise _AttemptFailed("parse", retryable=False) from None
            name = reply.name.lower()
            if name == sent_name:
                return reply
            # 0rdc for another command, or any non-matching echo: discard and read again.
            stale_reads += 1
            if stale_reads > self._settings.max_stale_reads or self._clock() >= deadline:
                raise _AttemptFailed("no_reply", retryable=True)

    def _read_reply(self, deadline: float) -> str:
        """Assemble one CRC-validated reply text from input reports (protocol report §1.4).

        Every read is bounded by the time left until ``deadline`` (FR-4.8, NFR-3): with nothing
        left, the read is a no-reply and no read is made. ``None`` ends the read at once (no spin
        on the clock). A reply that completes only after the deadline is a no-reply too. Raises
        :class:`_NoReply`, ``ReplyCrcError`` or ``ReplyFrameError``.
        """
        assembler = ReplyAssembler()
        while True:
            payload = self._transport.read_report(self._read_timeout_left_ms(deadline))
            if payload is None:
                raise _NoReply
            if assembler.feed(payload):
                if self._clock() > deadline:
                    raise _NoReply
                return assembler.finish()

    def _read_timeout_left_ms(self, deadline: float) -> int:
        """Return the timeout for the next read: the time left until ``deadline`` (FR-4.8).

        Integer milliseconds in ``1..read_timeout_ms`` (T7-C1: the hidapi binding takes a C int,
        and 0 means "block"); the first read of an attempt gets the configured value. Raises
        :class:`_NoReply` when the deadline has passed.
        """
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _NoReply
        # round() removes float noise, so a whole number of ms left is not rounded up by ceil.
        left_ms = math.ceil(round(remaining * 1000, 6))
        return max(1, min(self._read_timeout_ms, left_ms))

    # -- step 3: resync ----------------------------------------------------------------------

    def _resync(self, cmd: Command) -> None:
        """Send ``0RDC1`` and check the replies until ``0rdc`` (FR-5.5; design C7 step 3).

        HSL sends the dummy ``0RDC1`` after every success (protocol report §3, HSL:2016-2018). The
        meaning of ``RDC`` is INFERRED — see api/README.md checklist item 4. The pre-send delay
        applies to this write too (FR-5.2, DD-2); there is NO flush before it (FR-5.3), so a late
        echo reply of ``cmd`` is still read here. Replies are read past, within one resync read
        timeout and ``max_stale_reads``, until a ``0rdc`` reply arrives. An echo-matching reply
        with a non-``0`` code means the earlier ``0`` was stale: :class:`TransactionFailed`
        (``firmware``, that code) is raised (amendment A2). A no-reply, CRC or frame error, or an
        exhausted stale budget, is logged at warning level and does not fail the command: the next
        transaction flushes the input queue anyway (FR-5.3). A ``DeviceIOError`` raises
        :class:`DeviceLost`.
        """
        sent_name = f"{cmd.address}{cmd.mnemonic}".lower()
        try:
            self._sleep(self._pre_send_s)
            for report in encode_command(rdc1()):
                self._transport.write_report(report)
            # The resync read has its own single read timeout, as the budget counts (NFR-3).
            deadline = self._clock() + self._read_timeout_s
            stale_reads = 0
            while True:
                reply = parse_reply(self._read_reply(deadline))
                name = reply.name.lower()
                if name == _STALE_NAME:
                    return
                if name == sent_name and reply.error_char != "0":
                    raise TransactionFailed("firmware", reply.error_char, self._attempts)
                # Any other reply: read past, bounded like the attempt read (FR-5.4).
                stale_reads += 1
                if stale_reads > self._settings.max_stale_reads or self._clock() >= deadline:
                    raise _NoReply
        except DeviceIOError as exc:
            raise DeviceLost(f"controller {self._controller_id}: HID I/O failed") from exc
        except (_NoReply, ReplyCrcError, ReplyFrameError) as exc:
            reason = "no_reply" if isinstance(exc, _NoReply) else exc.kind
            _log.warning(
                EVENT_TEC_RESYNC_FAILED,
                controller_id=self._controller_id,
                slot=cmd.address,
                mnemonic=cmd.mnemonic,
                reason=reason,
            )

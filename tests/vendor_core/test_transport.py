"""Marquee offline transport suite for ``instrument_api_core.transport`` (Task 7).

This suite pins the full contract of the shared TCP/protobuf transport base:

- the documented constants and the ``TransportError`` hierarchy (FR-6, FR-6.1);
- abstract-method enforcement on ``TcpProtobufTransport`` via a private concrete subclass
  ``_LenPrefixed`` (FR-6.2);
- the 4-byte length-prefix round-trip of ``encode_length`` / ``decode_length`` (FR-6.3);
- the ephemeral-socket lifecycle over a real loopback echo server: a fresh socket per
  ``exchange``, the timeout set BEFORE ``connect``, a framed round-trip, and no connection or
  listening socket held between calls (FR-6.4);
- the full failure-mapping taxonomy, specific-before-generic, driven by monkeypatch so every
  branch is deterministic and offline (FR-6.5);
- the reply cap over the oversized-reply loopback server (FR-6.6);
- error-message hygiene: every raised ``TransportError`` names host/port (and timeout/cap where
  applicable) and never the request payload or any secret (FR-6.7, NFR-1).

The suite uses the standard library plus pytest only and runs fully offline (NFR-7): loopback
(``127.0.0.1``) for the socket lifecycle and the reply cap, and ``monkeypatch`` for the DNS,
timeout, generic, and socket-construction branches. It relies on no real DNS lookup, no external
host, and no real wall-clock timeout.
"""

import socket
from collections.abc import Callable
from typing import Any

import pytest
from inheco_tec_api._vendor.instrument_api_core import transport as transport_module
from inheco_tec_api._vendor.instrument_api_core.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    ConnectionRefused,
    HostNotFound,
    MessageTooLarge,
    TcpProtobufTransport,
    TransportError,
    TransportTimeout,
)

from vendor_core.conftest import LoopbackAddress

# A distinctive, non-secret request body that must never appear in any error message (FR-6.7,
# NFR-1). It is prose (not a credential shape) so it round-trips over loopback and doubles as the
# "leaked payload" probe in the hygiene assertions.
_SECRET_PAYLOAD: bytes = b"top-secret-request-body-should-never-appear"


# --------------------------------------------------------------------------------------------------
# Concrete and incomplete subclasses (FR-6.2)
# --------------------------------------------------------------------------------------------------


class _LenPrefixed(TcpProtobufTransport):
    """A complete concrete transport: 4-byte length-prefix framing, prefix-strip parsing.

    ``frame`` reuses :meth:`TcpProtobufTransport.encode_length` to prepend a big-endian length
    prefix; ``parse`` reuses :meth:`TcpProtobufTransport.decode_length` to strip it. This is the
    test double that exercises the base socket lifecycle end to end over a loopback echo server.
    """

    def frame(self, message: bytes) -> bytes:
        return self.encode_length(len(message)) + message

    def parse(self, raw: bytes) -> bytes:
        length = self.decode_length(raw[:4])
        return raw[4 : 4 + length]


class _MissingFrame(TcpProtobufTransport):
    """An incomplete subclass that implements ``parse`` but not ``frame`` (stays abstract)."""

    def parse(self, raw: bytes) -> bytes:
        return raw


class _MissingParse(TcpProtobufTransport):
    """An incomplete subclass that implements ``frame`` but not ``parse`` (stays abstract)."""

    def frame(self, message: bytes) -> bytes:
        return message


# --------------------------------------------------------------------------------------------------
# Socket doubles for the offline failure/lifecycle branches
# --------------------------------------------------------------------------------------------------


class _FakeSocket:
    """A minimal ``socket.socket`` stand-in whose ``connect`` raises a chosen ``OSError``.

    It drives the monkeypatched failure-mapping branches (DNS, timeout, generic ``OSError``)
    deterministically and offline: ``connect`` raises the configured error, and ``close`` is a
    no-op that only records that the ``finally`` block released the socket.
    """

    def __init__(self, connect_error: OSError) -> None:
        self._connect_error = connect_error
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int]) -> None:
        raise self._connect_error

    def sendall(self, data: bytes) -> None:
        return None

    def shutdown(self, how: int) -> None:
        return None

    def recv(self, bufsize: int) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


class _RecordingSocket:
    """Wrap a real socket and record the order of lifecycle calls (timeout-before-connect proof).

    It delegates every operation to a real loopback socket so the happy-path round-trip is genuine,
    while appending a marker for each call so a test can assert ``settimeout`` precedes ``connect``.
    It intentionally exposes no ``bind`` / ``listen`` / ``accept``, so a successful ``exchange``
    through it proves the transport opens only an outbound client socket (FR-6.4).
    """

    def __init__(self, real: socket.socket, log: list[str]) -> None:
        self.real = real
        self.log = log

    def settimeout(self, timeout: float | None) -> None:
        self.log.append(f"settimeout:{timeout}")
        self.real.settimeout(timeout)

    def connect(self, address: tuple[str, int]) -> None:
        self.log.append("connect")
        self.real.connect(address)

    def sendall(self, data: bytes) -> None:
        self.log.append("sendall")
        self.real.sendall(data)

    def shutdown(self, how: int) -> None:
        self.log.append("shutdown")
        self.real.shutdown(how)

    def recv(self, bufsize: int) -> bytes:
        return self.real.recv(bufsize)

    def close(self) -> None:
        self.log.append("close")
        self.real.close()


class _ScriptedSocket:
    """A socket double whose ``connect`` succeeds and whose receive/close behavior is scripted.

    ``_FakeSocket`` always raises on ``connect``, so it can only exercise the pre-connect failure
    branches. This double instead lets ``connect``/``sendall``/``shutdown`` succeed and then drives
    the post-connect receive path deterministically and offline: a scripted list of ``recv`` chunks
    (for the multi-chunk accumulation, the exact-cap boundary, and the clean-EOF empty reply), an
    optional ``recv`` error (the finally-close-on-error-path branch), and an optional ``close``
    error (the guarded finally that must not mask the original failure). Nothing touches a network.
    """

    def __init__(
        self,
        recv_chunks: list[bytes],
        *,
        recv_error: OSError | None = None,
        close_error: OSError | None = None,
    ) -> None:
        self._recv_chunks = list(recv_chunks)
        self._recv_error = recv_error
        self._close_error = close_error
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, address: tuple[str, int]) -> None:
        return None

    def sendall(self, data: bytes) -> None:
        return None

    def shutdown(self, how: int) -> None:
        return None

    def recv(self, bufsize: int) -> bytes:
        if self._recv_error is not None:
            raise self._recv_error
        if self._recv_chunks:
            return self._recv_chunks.pop(0)
        return b""

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _fake_socket_factory(connect_error: OSError) -> Callable[..., _FakeSocket]:
    """Return a ``socket.socket`` replacement yielding a ``_FakeSocket`` (raises on connect)."""

    def factory(*args: object, **kwargs: object) -> _FakeSocket:
        return _FakeSocket(connect_error)

    return factory


def _raising_socket_factory(construction_error: OSError) -> Callable[..., _FakeSocket]:
    """Return a ``socket.socket`` replacement that raises at construction time (FR-6.5 branch)."""

    def factory(*args: object, **kwargs: object) -> _FakeSocket:
        raise construction_error

    return factory


def _recording_socket_factory(
    original: Callable[..., socket.socket],
    call_log: list[str],
    created: list[_RecordingSocket],
) -> Callable[..., socket.socket | _RecordingSocket]:
    """Return a ``socket.socket`` replacement wrapping the outbound client socket.

    Only the transport's outbound construction — ``socket.socket(AF_INET, SOCK_STREAM)`` with no
    ``fileno`` — is wrapped and recorded. ``socket.socket.accept`` also constructs through this same
    module global, but passes ``fileno=`` for the accepted fd; that call is delegated faithfully to
    the original so the loopback server thread keeps working (wrapping an accepted fd would give a
    fresh, unconnected socket).
    """

    def factory(*args: object, **kwargs: object) -> socket.socket | _RecordingSocket:
        if "fileno" in kwargs or len(args) > 2:
            return original(*args, **kwargs)
        real = original(socket.AF_INET, socket.SOCK_STREAM)
        wrapper = _RecordingSocket(real, call_log)
        created.append(wrapper)
        return wrapper

    return factory


def _scripted_socket_factory(scripted: _ScriptedSocket) -> Callable[..., _ScriptedSocket]:
    """Return a ``socket.socket`` replacement yielding a single, inspectable ``_ScriptedSocket``.

    The same instance is returned on every construction so a test can inspect ``.closed`` after
    ``exchange`` returns or raises (the transport opens exactly one socket per ``exchange``).
    """

    def factory(*args: object, **kwargs: object) -> _ScriptedSocket:
        return scripted

    return factory


def _refused_address() -> LoopbackAddress:
    """Bind then immediately close a ``127.0.0.1`` port so a connect to it is refused (FR-6.5)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return LoopbackAddress(host="127.0.0.1", port=port)


# --------------------------------------------------------------------------------------------------
# 7.1 — surface, constants, and the TransportError hierarchy (FR-6, FR-6.1)
# --------------------------------------------------------------------------------------------------


def test_module_exposes_the_documented_transport_surface() -> None:
    """The module exposes the constants, the ABC, and the full error hierarchy (FR-6)."""
    for name in (
        "DEFAULT_TIMEOUT_SECONDS",
        "MAX_MESSAGE_BYTES",
        "TcpProtobufTransport",
        "TransportError",
        "ConnectionRefused",
        "HostNotFound",
        "TransportTimeout",
        "MessageTooLarge",
    ):
        assert hasattr(transport_module, name)


def test_default_timeout_seconds_constant() -> None:
    """The default connect/send/receive timeout is ``5.0`` seconds (FR-6.1)."""
    assert DEFAULT_TIMEOUT_SECONDS == 5.0
    assert isinstance(DEFAULT_TIMEOUT_SECONDS, float)


def test_max_message_bytes_constant() -> None:
    """The reply buffer cap is exactly 1 MiB, ``1 << 20`` (FR-6.1)."""
    assert MAX_MESSAGE_BYTES == 1 << 20
    assert MAX_MESSAGE_BYTES == 1_048_576


def test_transport_error_hierarchy_subclass_relationships() -> None:
    """Each specific error subclasses ``TransportError``, which subclasses ``Exception`` (6.1)."""
    assert issubclass(ConnectionRefused, TransportError)
    assert issubclass(HostNotFound, TransportError)
    assert issubclass(TransportTimeout, TransportError)
    assert issubclass(MessageTooLarge, TransportError)
    assert issubclass(TransportError, Exception)


def test_transport_errors_are_not_os_error_subclasses() -> None:
    """No mapped transport error is an ``OSError`` (a mapped failure never looks like raw I/O)."""
    assert not issubclass(TransportError, OSError)
    assert not issubclass(ConnectionRefused, OSError)
    assert not issubclass(HostNotFound, OSError)
    assert not issubclass(TransportTimeout, OSError)
    assert not issubclass(MessageTooLarge, OSError)


# --------------------------------------------------------------------------------------------------
# 7.2 — abstract-method enforcement (FR-6.2)
# --------------------------------------------------------------------------------------------------


def test_complete_subclass_instantiates() -> None:
    """A subclass implementing both ``frame`` and ``parse`` instantiates cleanly (FR-6.2)."""
    instance = _LenPrefixed("127.0.0.1", 9000)
    assert isinstance(instance, TcpProtobufTransport)
    assert instance.host == "127.0.0.1"
    assert instance.port == 9000
    assert instance.timeout == DEFAULT_TIMEOUT_SECONDS


def test_missing_frame_subclass_cannot_instantiate() -> None:
    """A subclass that omits ``frame`` raises ``TypeError`` on instantiation (FR-6.2)."""
    # Typed ``type[Any]`` because ``TcpProtobufTransport`` is abstract: mypy forbids assigning an
    # abstract class to ``type[TcpProtobufTransport]``. The runtime ``TypeError`` is the assertion.
    cls: type[Any] = _MissingFrame
    with pytest.raises(TypeError):
        cls("127.0.0.1", 9000)


def test_missing_parse_subclass_cannot_instantiate() -> None:
    """A subclass that omits ``parse`` raises ``TypeError`` on instantiation (FR-6.2)."""
    cls: type[Any] = _MissingParse
    with pytest.raises(TypeError):
        cls("127.0.0.1", 9000)


# --------------------------------------------------------------------------------------------------
# 7.3 — length-prefix round-trip (FR-6.3)
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 255, 65535, 2**32 - 1])
def test_length_prefix_round_trips_across_the_four_byte_range(n: int) -> None:
    """``decode_length(encode_length(n)) == n`` across the whole 4-byte range (FR-6.3)."""
    encoded = TcpProtobufTransport.encode_length(n)
    assert len(encoded) == 4
    assert TcpProtobufTransport.decode_length(encoded) == n


# --------------------------------------------------------------------------------------------------
# 7.4 — happy-path ephemeral-socket lifecycle over loopback (FR-6.4)
# --------------------------------------------------------------------------------------------------


def test_exchange_round_trips_over_loopback(loopback_echo_server: LoopbackAddress) -> None:
    """``exchange`` frames, round-trips over a real loopback socket, returns ``parse`` (FR-6.4)."""
    transport = _LenPrefixed(loopback_echo_server.host, loopback_echo_server.port)
    result = transport.exchange(_SECRET_PAYLOAD)
    assert result == _SECRET_PAYLOAD


def test_exchange_uses_a_fresh_socket_with_timeout_before_connect(
    loopback_echo_server: LoopbackAddress, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ``exchange`` opens one fresh socket, timeout set before ``connect`` (FR-6.4).

    A recording wrapper around a real loopback socket proves the lifecycle order and that exactly
    one outbound client socket is created (the wrapper exposes no ``bind``/``listen``/``accept``, so
    a successful round-trip proves no listening socket is opened).
    """
    original_socket = socket.socket
    call_log: list[str] = []
    created: list[_RecordingSocket] = []
    monkeypatch.setattr(
        socket, "socket", _recording_socket_factory(original_socket, call_log, created)
    )

    transport = _LenPrefixed(loopback_echo_server.host, loopback_echo_server.port, timeout=2.5)
    result = transport.exchange(_SECRET_PAYLOAD)

    assert result == _SECRET_PAYLOAD
    # Exactly one fresh socket for the single exchange (no connection reuse, none held).
    assert len(created) == 1
    # The configured timeout is set BEFORE connect so no blocking call can hang.
    assert "settimeout:2.5" in call_log
    assert call_log.index("settimeout:2.5") < call_log.index("connect")
    # The full client lifecycle ran and the socket was released in the finally block.
    assert call_log == ["settimeout:2.5", "connect", "sendall", "shutdown", "close"]
    assert created[0].real.fileno() == -1


def test_exchange_holds_no_socket_state_between_calls(
    loopback_echo_server: LoopbackAddress,
) -> None:
    """The transport keeps only ``host``/``port``/``timeout`` — no cached socket held (FR-6.4)."""
    transport = _LenPrefixed(loopback_echo_server.host, loopback_echo_server.port)
    transport.exchange(_SECRET_PAYLOAD)
    assert set(vars(transport)) == {"host", "port", "timeout"}


# --------------------------------------------------------------------------------------------------
# 7.5 — failure-mapping taxonomy, specific-before-generic, offline (FR-6.5)
# --------------------------------------------------------------------------------------------------


def test_refused_connection_maps_to_connection_refused() -> None:
    """A refused connect (bound-then-closed loopback port) maps to ``ConnectionRefused`` (6.5)."""
    address = _refused_address()
    transport = _LenPrefixed(address.host, address.port)
    with pytest.raises(ConnectionRefused) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    assert type(exc) is ConnectionRefused
    assert not isinstance(exc, OSError)
    assert exc.host == address.host
    assert exc.port == address.port


def test_gaierror_maps_to_host_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``socket.gaierror`` (unresolved host) maps to ``HostNotFound`` (FR-6.5)."""
    monkeypatch.setattr(
        socket, "socket", _fake_socket_factory(socket.gaierror("name resolution failed"))
    )
    transport = _LenPrefixed("instrument.invalid", 9000)
    with pytest.raises(HostNotFound) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    assert type(exc) is HostNotFound
    assert not isinstance(exc, OSError)
    assert exc.host == "instrument.invalid"


def test_timeout_maps_to_transport_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``TimeoutError`` maps to ``TransportTimeout`` naming host/port/timeout (FR-6.5)."""
    monkeypatch.setattr(socket, "socket", _fake_socket_factory(TimeoutError("timed out")))
    transport = _LenPrefixed("203.0.113.9", 5555, timeout=3.0)
    with pytest.raises(TransportTimeout) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    assert type(exc) is TransportTimeout
    assert not isinstance(exc, OSError)
    assert exc.host == "203.0.113.9"
    assert exc.port == 5555
    assert exc.timeout == 3.0


def test_generic_oserror_maps_to_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generic ``OSError`` maps to the base ``TransportError`` (specific-before-generic) (6.5)."""
    monkeypatch.setattr(socket, "socket", _fake_socket_factory(OSError("generic failure")))
    transport = _LenPrefixed("203.0.113.9", 5555)
    with pytest.raises(TransportError) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    # It is the base class exactly, not one of the specific subclasses.
    assert type(exc) is TransportError
    assert not isinstance(exc, OSError)
    assert "203.0.113.9" in str(exc)
    assert "5555" in str(exc)


def test_socket_construction_oserror_maps_to_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` from ``socket.socket`` construction maps to ``TransportError`` (FR-6.5)."""
    monkeypatch.setattr(socket, "socket", _raising_socket_factory(OSError("cannot create socket")))
    transport = _LenPrefixed("203.0.113.9", 5555)
    with pytest.raises(TransportError) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    assert type(exc) is TransportError
    assert not isinstance(exc, OSError)
    assert "203.0.113.9" in str(exc)
    assert "5555" in str(exc)


# --------------------------------------------------------------------------------------------------
# 7.6 — reply cap over the oversized-reply loopback server (FR-6.6)
# --------------------------------------------------------------------------------------------------


def test_oversized_reply_raises_message_too_large(
    oversized_reply_server: LoopbackAddress,
) -> None:
    """A reply past ``MAX_MESSAGE_BYTES`` raises ``MessageTooLarge`` naming host/port/cap (6.6)."""
    transport = _LenPrefixed(oversized_reply_server.host, oversized_reply_server.port)
    with pytest.raises(MessageTooLarge) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    exc = exc_info.value
    assert type(exc) is MessageTooLarge
    assert not isinstance(exc, OSError)
    assert exc.host == oversized_reply_server.host
    assert exc.port == oversized_reply_server.port
    assert exc.limit == MAX_MESSAGE_BYTES
    text = str(exc)
    assert oversized_reply_server.host in text
    assert str(oversized_reply_server.port) in text
    assert str(MAX_MESSAGE_BYTES) in text


# --------------------------------------------------------------------------------------------------
# 7.6b — receive-path branches: multi-chunk reassembly, the exact-cap boundary, clean EOF, and the
#        finally-close on the post-connect error path (FR-6.4, FR-6.6)
# --------------------------------------------------------------------------------------------------


def test_recv_reassembles_a_reply_split_across_multiple_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``exchange`` reassembles a framed reply delivered across several ``recv`` chunks (FR-6.4).

    The loopback echo test round-trips a single-chunk payload; this pins the ``_recv`` while-loop
    accumulation by scripting the framed reply across three separate ``recv`` returns.
    """
    body = b"multi-chunk-reassembled-reply-body"
    wire = TcpProtobufTransport.encode_length(len(body)) + body
    scripted = _ScriptedSocket([wire[:5], wire[5:17], wire[17:]])
    monkeypatch.setattr(socket, "socket", _scripted_socket_factory(scripted))

    transport = _LenPrefixed("203.0.113.9", 5555)
    result = transport.exchange(_SECRET_PAYLOAD)

    assert result == body
    assert scripted.closed


def test_reply_exactly_at_the_cap_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reply of exactly ``MAX_MESSAGE_BYTES`` is accepted; only past the cap rejects (FR-6.6).

    The oversized-reply fixture proves the ``> MAX`` reject side; this pins the untested boundary
    (``len(buf) == MAX_MESSAGE_BYTES`` must NOT raise), so the cap is inclusive by exactly one byte.
    """
    body = b"\x00" * (MAX_MESSAGE_BYTES - 4)
    wire = TcpProtobufTransport.encode_length(len(body)) + body
    assert len(wire) == MAX_MESSAGE_BYTES
    scripted = _ScriptedSocket([wire])
    monkeypatch.setattr(socket, "socket", _scripted_socket_factory(scripted))

    transport = _LenPrefixed("203.0.113.9", 5555)
    result = transport.exchange(_SECRET_PAYLOAD)

    assert len(result) == MAX_MESSAGE_BYTES - 4
    assert scripted.closed


def test_empty_reply_maps_cleanly_not_a_raw_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean immediate EOF reply returns empty bytes, never a raw ``OSError`` (FR-6.4)."""
    scripted = _ScriptedSocket([])
    monkeypatch.setattr(socket, "socket", _scripted_socket_factory(scripted))

    transport = _LenPrefixed("203.0.113.9", 5555)
    result = transport.exchange(_SECRET_PAYLOAD)

    assert result == b""
    assert scripted.closed


def test_post_connect_error_still_closes_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after a successful ``connect`` still releases the socket in ``finally`` (FR-6.4).

    Every pre-connect double raises on ``connect``; this scripts a ``recv`` error after ``connect``
    succeeds, proving the finally-close runs on the post-connect error path and the ``recv``
    ``OSError`` maps to the generic ``TransportError`` (not a raw ``OSError``).
    """
    scripted = _ScriptedSocket([], recv_error=OSError("receive failed after connect"))
    monkeypatch.setattr(socket, "socket", _scripted_socket_factory(scripted))

    transport = _LenPrefixed("203.0.113.9", 5555)
    with pytest.raises(TransportError) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)

    exc = exc_info.value
    assert type(exc) is TransportError
    assert not isinstance(exc, OSError)
    assert scripted.closed


def test_close_error_does_not_mask_the_original_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``close`` that raises in ``finally`` is swallowed; the mapped failure propagates (FR-6.4).

    Pins the guarded ``try: sock.close() except OSError: pass`` branch: with both ``recv`` and
    ``close`` failing, the caller still sees the mapped ``TransportError`` naming host/port, never
    the teardown ``OSError``.
    """
    scripted = _ScriptedSocket(
        [],
        recv_error=OSError("receive failed after connect"),
        close_error=OSError("close failed during teardown"),
    )
    monkeypatch.setattr(socket, "socket", _scripted_socket_factory(scripted))

    transport = _LenPrefixed("203.0.113.9", 5555)
    with pytest.raises(TransportError) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)

    exc = exc_info.value
    assert type(exc) is TransportError
    assert not isinstance(exc, OSError)
    text = str(exc)
    assert "203.0.113.9" in text
    assert "5555" in text
    assert "close failed during teardown" not in text
    assert scripted.closed


# --------------------------------------------------------------------------------------------------
# 7.7 — error-message hygiene: names host/port/timeout/cap, never the payload or a secret
#       (FR-6.7, NFR-1)
# --------------------------------------------------------------------------------------------------


def _assert_no_payload_leak(text: str) -> None:
    """Assert neither the decoded payload nor its ``bytes`` repr is in ``text`` (6.7, NFR-1)."""
    assert _SECRET_PAYLOAD.decode() not in text
    assert str(_SECRET_PAYLOAD) not in text
    assert repr(_SECRET_PAYLOAD) not in text


def test_connection_refused_message_hygiene() -> None:
    """``ConnectionRefused`` names host and port and never the payload (FR-6.7, NFR-1)."""
    text = str(ConnectionRefused("203.0.113.5", 5555))
    assert "203.0.113.5" in text
    assert "5555" in text
    _assert_no_payload_leak(text)


def test_host_not_found_message_hygiene() -> None:
    """``HostNotFound`` names the host and never the payload (FR-6.7, NFR-1)."""
    text = str(HostNotFound("instrument.invalid"))
    assert "instrument.invalid" in text
    _assert_no_payload_leak(text)


def test_transport_timeout_message_hygiene() -> None:
    """``TransportTimeout`` names host, port, and the timeout, never the payload (FR-6.7, NFR-1)."""
    text = str(TransportTimeout("203.0.113.5", 5555, 4.0))
    assert "203.0.113.5" in text
    assert "5555" in text
    assert "4.0" in text
    _assert_no_payload_leak(text)


def test_message_too_large_message_hygiene() -> None:
    """``MessageTooLarge`` names host, port, and the cap, never the payload (FR-6.7, NFR-1)."""
    text = str(MessageTooLarge("203.0.113.5", 5555, MAX_MESSAGE_BYTES))
    assert "203.0.113.5" in text
    assert "5555" in text
    assert str(MAX_MESSAGE_BYTES) in text
    _assert_no_payload_leak(text)


def test_generic_transport_error_message_hygiene(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base ``TransportError`` from ``exchange`` names host/port, never the payload (FR-6.7)."""
    monkeypatch.setattr(socket, "socket", _fake_socket_factory(OSError("generic failure")))
    transport = _LenPrefixed("203.0.113.5", 5555)
    with pytest.raises(TransportError) as exc_info:
        transport.exchange(_SECRET_PAYLOAD)
    text = str(exc_info.value)
    assert "203.0.113.5" in text
    assert "5555" in text
    _assert_no_payload_leak(text)

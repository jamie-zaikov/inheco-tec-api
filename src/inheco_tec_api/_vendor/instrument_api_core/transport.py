"""The TCP/protobuf transport base shared by every instruments/*/api (C11, FR-5.1, NFR-1).

Many lab instruments speak a length-prefixed protobuf protocol over a raw TCP socket. This module is
the shared transport base for that pattern, modeled on the ``zebra_printer`` TCP transport:

- Ephemeral-socket-per-request: every :meth:`TcpProtobufTransport.exchange` opens a fresh socket,
  sends the framed request, reads the framed reply, and closes the socket in a ``finally`` block.
  No long-lived connection is held between requests. The transport makes outbound client connections
  only and opens no listening socket.
- Protocol-blind at the byte level: :meth:`exchange` moves opaque ``bytes`` and never inspects
  message content, so no instrument-specific protobuf schema leaks into the base.
- The two protobuf seams — :meth:`frame` (prefix an encoded message for the wire) and :meth:`parse`
  (recover one message from the wire bytes) — are abstract. A concrete per-instrument transport
  implements them with its own protobuf message types (the ``transport`` extra installs
  ``protobuf``). The base ships the socket handling and the length-prefix helpers only.

The transport holds no secret and no config: a caller supplies ``host`` / ``port`` / ``timeout`` and
opaque payload bytes. Failures map to the :class:`TransportError` hierarchy; messages name
host/port/timeout only and never a payload or a secret (NFR-1).
"""

import abc
import socket

DEFAULT_TIMEOUT_SECONDS: float = 5.0
"""Default connect/send/receive timeout, in seconds, when a caller supplies none."""

MAX_MESSAGE_BYTES: int = 1 << 20
"""Upper bound (1 MiB) on a single reply the base buffers, guarding against a misbehaving peer."""

_LENGTH_PREFIX_BYTES: int = 4
_RECV_CHUNK_BYTES: int = 4096


class TransportError(Exception):
    """Base for every transport failure."""


class ConnectionRefused(TransportError):
    """The instrument actively refused the TCP connection. Names host and port."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        super().__init__(f"instrument at {host}:{port} refused the connection")


class HostNotFound(TransportError):
    """The host name/address could not be resolved. Names the host."""

    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(f"instrument host {host!r} could not be resolved")


class TransportTimeout(TransportError):
    """A connect, send, or receive exceeded the timeout. Names host, port, and the timeout."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        super().__init__(f"instrument at {host}:{port} timed out after {timeout}s")


class MessageTooLarge(TransportError):
    """A framed message exceeded :data:`MAX_MESSAGE_BYTES`. Names host, port, and the cap."""

    def __init__(self, host: str, port: int, limit: int) -> None:
        self.host = host
        self.port = port
        self.limit = limit
        super().__init__(f"instrument at {host}:{port} message exceeded {limit} bytes")


class TcpProtobufTransport(abc.ABC):
    """Base ephemeral-socket TCP transport for length-prefixed protobuf messages.

    Concrete per-instrument transports subclass this and implement :meth:`frame` and :meth:`parse`
    with their own protobuf message types. The base owns the socket lifecycle, the timeout, the
    length-prefix framing helpers, and the failure mapping (FR-5.1, NFR-1).
    """

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    @abc.abstractmethod
    def frame(self, message: bytes) -> bytes:
        """Frame one encoded protobuf message for the wire (concrete subclass implements this).

        Given the serialized bytes of a protobuf message, return the exact bytes to put on the wire
        (for example a big-endian length prefix followed by the message). :meth:`encode_length` is
        the shared default helper a subclass may reuse.
        """

    @abc.abstractmethod
    def parse(self, raw: bytes) -> bytes:
        """Recover one protobuf message payload from wire bytes (concrete subclass implements this).

        Given the raw bytes read from the wire, return the serialized protobuf message payload (for
        example by stripping the length prefix). :meth:`decode_length` is the shared default helper.
        """

    @staticmethod
    def encode_length(length: int) -> bytes:
        """Encode ``length`` as a 4-byte big-endian length prefix (shared framing helper)."""
        return length.to_bytes(_LENGTH_PREFIX_BYTES, byteorder="big")

    @staticmethod
    def decode_length(prefix: bytes) -> int:
        """Decode a 4-byte big-endian length prefix (shared framing helper)."""
        return int.from_bytes(prefix, byteorder="big")

    def exchange(self, message: bytes) -> bytes:
        """Send one framed message and return the parsed reply payload (FR-5.1, NFR-1).

        Frames ``message`` with :meth:`frame`, opens a fresh socket to ``(host, port)`` with the
        configured timeout set BEFORE ``connect`` so no blocking call can hang, sends the framed
        bytes, reads the reply, and returns :meth:`parse` of the raw reply. The socket is always
        closed in a ``finally`` block, guarded so the close cannot mask the original failure.

        Failures map to the :class:`TransportError` hierarchy in specific-before-generic order (each
        is an ``OSError`` subclass): a refused connection, an unresolved host, a timeout, then any
        other ``OSError``. The socket constructor is inside the mapped ``try``, so a construction
        failure also maps rather than escaping the hierarchy. Error messages name host/port/timeout
        only and never the payload or a secret.
        """
        wire = self.frame(message)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            raise TransportError(f"could not open socket to {self.host}:{self.port}") from exc
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            sock.sendall(wire)
            sock.shutdown(socket.SHUT_WR)
            return self.parse(self._recv(sock))
        except ConnectionRefusedError as exc:
            raise ConnectionRefused(self.host, self.port) from exc
        except socket.gaierror as exc:
            raise HostNotFound(self.host) from exc
        except TimeoutError as exc:
            raise TransportTimeout(self.host, self.port, self.timeout) from exc
        except OSError as exc:
            raise TransportError(f"transport to {self.host}:{self.port} failed") from exc
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _recv(self, sock: socket.socket) -> bytes:
        """Read the reply in chunks until a clean EOF, capped at :data:`MAX_MESSAGE_BYTES`.

        Each ``recv`` is bounded by the socket timeout, so this cannot hang. Reading past the cap
        raises :class:`MessageTooLarge`. Content is never inspected here (protocol-blind, NFR-1).
        """
        buf = bytearray()
        while True:
            chunk = sock.recv(_RECV_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_MESSAGE_BYTES:
                raise MessageTooLarge(self.host, self.port, MAX_MESSAGE_BYTES)
        return bytes(buf)

"""Shared, fully typed fixtures for the instrument_api_core suites (NFR-5, NFR-7).

Every fixture and helper here is strict-typed (``mypy --strict`` runs over ``tests/``) and uses the
standard library only, so the whole member stays dependency-free and lint-clean. The fixtures give
the transport suite (Task 7) a real but offline loopback socket, give the auth/config/port suites an
injectable ``dict[str, str]`` environment (so no suite mutates ``os.environ``), and give the logging
suite (Task 5) a snapshot/restore guard around the process-global root logger.
"""

import logging
import socket
import threading
from collections.abc import Callable, Iterator
from typing import NamedTuple

import pytest
from inheco_tec_api._vendor.instrument_api_core.transport import MAX_MESSAGE_BYTES

_RECV_CHUNK_BYTES: int = 4096


class LoopbackAddress(NamedTuple):
    """The ``(host, port)`` a loopback fixture listens on, yielded to the requesting test."""

    host: str
    port: int


# --------------------------------------------------------------------------------------------------
# Loopback TCP servers (transport suite, FR-6.4 / FR-6.6)
# --------------------------------------------------------------------------------------------------


def _recv_until_eof(conn: socket.socket) -> bytes:
    """Read from ``conn`` in chunks until the peer half-closes (EOF), then return the bytes."""
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(_RECV_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _start_loopback_server(
    build_reply: Callable[[bytes], bytes],
) -> tuple[socket.socket, LoopbackAddress]:
    """Bind ``127.0.0.1:0`` and serve one connection on a daemon thread.

    The daemon thread accepts a single connection, reads the framed request to EOF, writes the reply
    ``build_reply`` computes, half-closes, and closes the connection. The listening socket is
    returned to the caller so the fixture can hard-teardown after the test; a daemon thread never
    outlives the process. Binding port ``0`` gives an ephemeral port, so the servers never collide
    and never touch an external host (NFR-7).
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    sockname = listener.getsockname()
    address = LoopbackAddress(host=str(sockname[0]), port=int(sockname[1]))

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            # The listener was closed during teardown before a client connected.
            return
        try:
            request = _recv_until_eof(conn)
            conn.sendall(build_reply(request))
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            # The client hung up early (e.g. the reply-cap test aborts mid-reply); ignore it.
            pass
        finally:
            conn.close()

    thread = threading.Thread(target=serve, name="loopback-server", daemon=True)
    thread.start()
    return listener, address


@pytest.fixture
def loopback_echo_server() -> Iterator[LoopbackAddress]:
    """A loopback TCP echo server: it writes the framed request straight back as the framed reply.

    Because the reply is byte-identical to the request, a length-prefixed transport round-trips its
    payload unchanged. Yields the ``127.0.0.1`` host/port; the listening socket is hard-closed after
    the test so no server survives it (transport happy path, FR-6.4).
    """
    listener, address = _start_loopback_server(lambda request: request)
    try:
        yield address
    finally:
        listener.close()


@pytest.fixture
def oversized_reply_server() -> Iterator[LoopbackAddress]:
    """A loopback server that replies with ``MAX_MESSAGE_BYTES + 1`` bytes.

    It drives the reply-cap branch: a transport reading this reply must raise ``MessageTooLarge``
    rather than buffer past the cap (FR-6.6). Yields the ``127.0.0.1`` host/port and hard-closes the
    listening socket after the test.
    """
    oversized = b"\x00" * (MAX_MESSAGE_BYTES + 1)
    listener, address = _start_loopback_server(lambda _request: oversized)
    try:
        yield address
    finally:
        listener.close()


# --------------------------------------------------------------------------------------------------
# Injectable environment maps (auth / config / port suites, no os.environ mutation)
# --------------------------------------------------------------------------------------------------

EnvFactory = Callable[..., dict[str, str]]
"""A factory that builds an isolated ``dict[str, str]`` environment from keyword entries."""


def _make_env(**entries: str) -> dict[str, str]:
    """Build a fresh ``dict[str, str]`` environment for injection through an ``env=`` parameter."""
    return dict(entries)


@pytest.fixture
def env_factory() -> EnvFactory:
    """Return a factory that builds injectable ``dict[str, str]`` environments.

    Suites call ``env_factory(PORT="9090")`` and pass the result through the ``env=`` parameter,
    so a test never mutates the process ``os.environ`` and stays hermetic (design Test Strategy).
    """
    return _make_env


# --------------------------------------------------------------------------------------------------
# Root-logger snapshot/restore (logging suite, FR-4.2 global-state guard)
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    """Snapshot the root logger's handlers and level, then restore them after the test.

    ``configure_logging`` rewrites the process-global root logger, so the logging suite requests
    this fixture to stop that mutation from leaking into any other test (design Risks → global-state
    leakage).
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)

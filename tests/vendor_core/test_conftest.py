"""Fixture smoke test for the shared ``tests`` conftest (Task 1, sub-task 1.6).

This suite proves the Task 1 fixtures actually work, so ``pytest <vendored-core>``
collects a non-empty suite. It uses the standard library plus pytest only and stays fully offline on
``127.0.0.1`` (NFR-7). It deliberately does NOT exercise ``TcpProtobufTransport.exchange`` or any
per-module surface: that is the job of the per-module suites (Tasks 2-8). Here we only confirm the
fixtures themselves behave as the design pins them (NFR-5, FR-6.4, FR-6.6).
"""

import logging
import socket
from collections.abc import Callable, Iterator
from typing import cast

import pytest
from inheco_tec_api._vendor.instrument_api_core.transport import MAX_MESSAGE_BYTES

from vendor_core.conftest import EnvFactory, LoopbackAddress, restore_root_logger

_CLIENT_TIMEOUT_SECONDS: float = 5.0
_RECV_CHUNK_BYTES: int = 4096


def _frame(payload: bytes) -> bytes:
    """Build a 4-byte big-endian length-prefixed frame for ``payload`` (stdlib only)."""
    return len(payload).to_bytes(4, byteorder="big") + payload


def _recv_until_eof(sock: socket.socket) -> bytes:
    """Read from ``sock`` until the peer half-closes (EOF), then return the accumulated bytes."""
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(_RECV_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _exchange_raw(address: LoopbackAddress, request: bytes) -> bytes:
    """Open a raw client socket to ``address``, send ``request``, and read the reply to EOF."""
    sock = socket.create_connection((address.host, address.port), timeout=_CLIENT_TIMEOUT_SECONDS)
    try:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        return _recv_until_eof(sock)
    finally:
        sock.close()


# --------------------------------------------------------------------------------------------------
# loopback_echo_server (FR-6.4, NFR-7)
# --------------------------------------------------------------------------------------------------


def test_loopback_echo_server_yields_reachable_loopback_port(
    loopback_echo_server: LoopbackAddress,
) -> None:
    """The echo fixture yields a reachable ``127.0.0.1`` port (collection is non-empty, 1.6)."""
    assert loopback_echo_server.host == "127.0.0.1"
    assert loopback_echo_server.port > 0
    # A successful connect proves the port is reachable and served on the loopback interface.
    sock = socket.create_connection(
        (loopback_echo_server.host, loopback_echo_server.port),
        timeout=_CLIENT_TIMEOUT_SECONDS,
    )
    sock.close()


def test_loopback_echo_server_round_trips_a_framed_payload(
    loopback_echo_server: LoopbackAddress,
) -> None:
    """The echo fixture returns the framed request byte-for-byte, so a payload round-trips."""
    frame = _frame(b"instrument-api-core round-trip")
    reply = _exchange_raw(loopback_echo_server, frame)
    assert reply == frame


# --------------------------------------------------------------------------------------------------
# oversized_reply_server (FR-6.6, NFR-7)
# --------------------------------------------------------------------------------------------------


def test_oversized_reply_server_writes_more_than_the_cap(
    oversized_reply_server: LoopbackAddress,
) -> None:
    """The oversized fixture yields a loopback address whose server replies past the cap."""
    assert oversized_reply_server.host == "127.0.0.1"
    assert oversized_reply_server.port > 0
    reply = _exchange_raw(oversized_reply_server, _frame(b"trigger"))
    assert len(reply) > MAX_MESSAGE_BYTES
    assert len(reply) == MAX_MESSAGE_BYTES + 1


# --------------------------------------------------------------------------------------------------
# env_factory (design Test Strategy: injectable dict[str, str], no os.environ mutation)
# --------------------------------------------------------------------------------------------------


def test_env_factory_builds_the_expected_dict(env_factory: EnvFactory) -> None:
    """The factory builds an isolated ``dict[str, str]`` from its keyword entries."""
    env = env_factory(PORT="9090", API_TOKEN="s3cr3t")
    assert env == {"PORT": "9090", "API_TOKEN": "s3cr3t"}
    assert isinstance(env, dict)


def test_env_factory_empty_call_yields_empty_dict(env_factory: EnvFactory) -> None:
    """A no-argument call yields an empty mapping, and each call is a fresh, isolated dict."""
    first = env_factory()
    second = env_factory()
    assert first == {}
    assert first is not second


# --------------------------------------------------------------------------------------------------
# restore_root_logger (FR-4.2 global-state guard; design Risks -> logging global-state leakage)
# --------------------------------------------------------------------------------------------------


def test_restore_root_logger_fixture_is_usable(restore_root_logger: None) -> None:
    """The fixture is requestable and yields ``None``; mutation inside the test is permitted.

    The fixture cleans up on teardown, so this mutation cannot leak into another test. The
    restoration semantics themselves are asserted in the next test, which drives the fixture's
    generator across its teardown.
    """
    assert restore_root_logger is None
    logging.getLogger().addHandler(logging.NullHandler())


def test_restore_root_logger_restores_handlers_and_level() -> None:
    """Driving the fixture across teardown restores the root logger's handlers and level.

    The fixture snapshots on setup and restores on teardown, so we drive its underlying generator
    directly: ``next`` runs setup, we mutate the root logger, then exhausting the generator runs the
    ``finally`` restore. This proves the logging suite (Task 5) cannot leak global state.
    """
    raw = cast(
        Callable[[], Iterator[None]],
        restore_root_logger.__wrapped__,  # type: ignore[attr-defined]
    )
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    mutated_level = logging.DEBUG if original_level != logging.DEBUG else logging.WARNING

    generator = raw()
    next(generator)  # setup: the fixture takes its snapshot here.

    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    root.setLevel(mutated_level)
    assert sentinel in root.handlers
    assert root.level == mutated_level

    with pytest.raises(StopIteration):
        next(generator)  # teardown: the fixture restores the snapshot here.

    assert root.handlers == original_handlers
    assert sentinel not in root.handlers
    assert root.level == original_level

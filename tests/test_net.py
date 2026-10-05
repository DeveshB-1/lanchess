"""Tests for lanchess.net (loopback only, ephemeral ports)."""

from __future__ import annotations

import errno
import json
import queue
import socket
import struct
import sys
import threading
import time
import unittest
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

from lanchess import net
from lanchess.net import (
    DEFAULT_PORT, MAX_LINE, PROTOCOL_VERSION, Connection, ConnectionClosed, DiscoveryResponder,
    HandshakeError, HostInfo, Server, broadcast_targets, client_handshake, connect, discover_hosts,
    local_ip_addresses, parse_host_port, sanitize_name, server_handshake,
)

try:
    from .support import watch_threads
except ImportError:  # (the tests folder itself is on sys.path: unittest discover -s tests)
    from support import watch_threads

LOCALHOST = "127.0.0.1"
WAIT = 3.0
QUIET = 0.25


def free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("" if kind == socket.SOCK_DGRAM else LOCALHOST, 0))
        return probe.getsockname()[1]


def run_in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Any]:
    """Run ``fn`` in a daemon thread; the returned callable joins it and returns or re-raises."""
    outcome: Dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["value"] = fn(*args, **kwargs)
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    def wait() -> Any:
        thread.join(WAIT * 2)
        if thread.is_alive():
            raise AssertionError(f"{fn.__name__} did not finish")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]

    return wait


class LineReader:
    """Reads JSON lines from a raw socket peer."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = b""

    def next_line(self, timeout: float = WAIT) -> bytes:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("timed out waiting for a line")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("peer closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def next_message(self, timeout: float = WAIT, skip: Tuple[str, ...] = ("ping",)) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            msg = json.loads(self.next_line(max(0.01, deadline - time.monotonic())).decode("utf-8"))
            if msg.get("type") not in skip:
                return msg


class NetTestCase(unittest.TestCase):
    def setUp(self) -> None:
        watch_threads(self, WAIT)

    def get(self, conn: Connection, timeout: float = WAIT) -> Dict[str, Any]:
        try:
            return conn.inbox.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("no message arrived") from None

    def assert_quiet(self, conn: Connection, duration: float = QUIET) -> None:
        with self.assertRaises(queue.Empty):
            msg = conn.inbox.get(timeout=duration)
            self.fail(f"unexpected message {msg!r}")

    def pair(self, **options: Any) -> Tuple[Connection, Connection]:
        """(server-side, client-side) connected Connections over loopback."""
        server = Server(port=0, bind=LOCALHOST, **options)
        self.addCleanup(server.close)
        client = connect(LOCALHOST, server.port, timeout=WAIT, **options)
        self.addCleanup(client.close)
        accepted = server.accept(WAIT)
        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.addCleanup(accepted.close)
        return accepted, client

    def raw_pair(self, small_buffers: bool = False, **options: Any) -> Tuple[Connection, socket.socket]:
        """A started Connection whose peer is a plain socket controlled by the test."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((LOCALHOST, 0))
            listener.listen(1)
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(raw.close)
            if small_buffers:
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            raw.settimeout(WAIT)
            raw.connect(listener.getsockname())
            accepted, _address = listener.accept()
        if small_buffers:
            accepted.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        conn = Connection(accepted, **options).start()
        self.addCleanup(conn.close)
        return conn, raw

    def wrapped_pair(self, socket_class: type, **options: Any) -> Tuple[Connection, socket.socket]:
        """Like raw_pair, but the Connection's socket is a ``socket_class`` (a socket subclass)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((LOCALHOST, 0))
            listener.listen(1)
            raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(raw.close)
            raw.settimeout(WAIT)
            raw.connect(listener.getsockname())
            accepted, _address = listener.accept()
        sock = socket_class(fileno=accepted.detach())
        conn = Connection(sock, **options).start()
        self.addCleanup(conn.close)
        return conn, raw


class DripSocket(socket.socket):
    """A socket that sends at most 1 KB per send() call, 20 ms apart: like a peer that keeps reading,
    slowly, so every call makes progress but a long message takes far longer than its timeout."""

    def send(self, data: Any, flags: int = 0) -> int:
        time.sleep(0.02)
        return super().send(bytes(memoryview(data)[:1024]), flags)


class ConnectionTests(NetTestCase):
    def test_round_trip_both_directions_with_unicode(self) -> None:
        host, guest = self.pair()
        messages = [
            {"type": "chat", "text": "Привет ♚♛ 日本語 🎉 naïve"},
            {"type": "move", "uci": "e2e4", "ply": 1, "clock_ms": None},
            {"type": "nested", "data": {"list": [1, 2.5, True, None], "s": "a\nb\"c\\"}},
        ]
        for msg in messages:
            host.send(msg)
            guest.send(dict(msg, echo=True))
        for msg in messages:
            self.assertEqual(self.get(guest), msg)
            self.assertEqual(self.get(host), dict(msg, echo=True))
        self.assertFalse(host.closed)
        self.assertFalse(guest.closed)

    def test_peer_is_host_and_port(self) -> None:
        host, guest = self.pair()
        self.assertEqual(guest.peer, f"{LOCALHOST}:{host._sock.getsockname()[1]}")
        self.assertTrue(host.peer.startswith(f"{LOCALHOST}:"))

    def test_wire_format_is_compact_utf8_json_line(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        conn.send({"type": "chat", "text": "é ♚"})
        self.assertEqual(LineReader(raw).next_line(), '{"type":"chat","text":"é ♚"}'.encode("utf-8"))

    def test_partial_lines_and_several_messages_per_chunk(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        king = "♚".encode("utf-8")
        chunks = [
            b'{"type":"a",', b'"n":1}', b'\n{"type":"b"}\n{"type":"c","t":"' + king[:1],
            king[1:] + b'"}\n{"ty', b'pe":"d"}', b"\n",
        ]
        for chunk in chunks:
            raw.sendall(chunk)
            time.sleep(0.03)
        self.assertEqual(self.get(conn), {"type": "a", "n": 1})
        self.assertEqual(self.get(conn), {"type": "b"})
        self.assertEqual(self.get(conn), {"type": "c", "t": "♚"})
        self.assertEqual(self.get(conn), {"type": "d"})
        self.assert_quiet(conn)

    def test_many_messages_in_one_send(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        raw.sendall(b"".join(b'{"type":"m","i":%d}\r\n' % i for i in range(200)))
        self.assertEqual([self.get(conn)["i"] for _ in range(200)], list(range(200)))

    def test_malformed_lines_become_protocol_errors(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        bad_lines = [b"not json", b"[1, 2]", b'"text"', b'{"no_type": 1}', b'{"type": 5}',
                     b"\xff\xfe{}", b'{"type":"_disconnected","reason":"fake"}', b"[" * 5000]
        raw.sendall(b"\n".join(bad_lines) + b"\n\n  \r\n" + b'{"type":"ok"}\n')
        for _line in bad_lines:
            msg = self.get(conn)
            self.assertEqual(msg["type"], "_protocol_error")
            self.assertIsInstance(msg["reason"], str)
        self.assertEqual(self.get(conn), {"type": "ok"})
        self.assertFalse(conn.closed)

    def test_line_of_exactly_max_length_is_accepted(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        prefix, suffix = b'{"type":"chat","text":"', b'"}'
        text_len = MAX_LINE - len(prefix) - len(suffix)
        raw.sendall(prefix + b"a" * text_len + suffix + b"\n")
        msg = self.get(conn)
        self.assertEqual(msg["type"], "chat")
        self.assertEqual(len(msg["text"]), text_len)

    def test_oversize_line_without_newline_disconnects(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        try:
            raw.sendall(b"a" * (MAX_LINE + 100))
        except OSError:
            pass
        msg = self.get(conn)
        self.assertEqual(msg["type"], "_disconnected")
        self.assertIn("too long", msg["reason"])
        self.assertTrue(conn.closed)

    def test_oversize_line_with_newline_disconnects(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        try:
            raw.sendall(b'{"type":"chat","text":"' + b"a" * MAX_LINE + b'"}\n{"type":"after"}\n')
        except OSError:
            pass
        msg = self.get(conn)
        self.assertEqual(msg["type"], "_disconnected")
        self.assertIn("too long", msg["reason"])
        self.assert_quiet(conn)

    def test_peer_close_yields_exactly_one_disconnected(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        raw.sendall(b'{"type":"bye"}\n')
        raw.close()
        self.assertEqual(self.get(conn), {"type": "bye"})
        self.assertEqual(self.get(conn), {"type": "_disconnected", "reason": "connection closed by peer"})
        self.assertTrue(conn.closed)
        self.assert_quiet(conn, 0.3)

    def test_single_disconnected_when_reader_and_heartbeat_both_notice(self) -> None:
        conn, raw = self.raw_pair(ping_interval=0.02, idle_timeout=0.2)
        time.sleep(0.1)
        raw.close()
        msg = self.get(conn)
        self.assertEqual(msg["type"], "_disconnected")
        self.assertIsInstance(msg["reason"], str)
        self.assertTrue(conn.closed)
        self.assert_quiet(conn, 0.4)

    def test_send_after_peer_died_raises_promptly(self) -> None:
        host, guest = self.pair()
        guest.close()
        self.assertEqual(self.get(host)["type"], "_disconnected")
        started = time.monotonic()
        for _ in range(3):
            with self.assertRaises(ConnectionClosed):
                host.send({"type": "chat", "text": "anyone there?"})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assert_quiet(host)

    def test_local_close_is_idempotent_and_quiet(self) -> None:
        host, guest = self.pair()
        guest.close()
        guest.close()
        self.assertTrue(guest.closed)
        with self.assertRaises(ConnectionClosed):
            guest.send({"type": "chat", "text": "x"})
        self.assert_quiet(guest)
        self.assertEqual(self.get(host)["type"], "_disconnected")
        host.close()
        self.assertTrue(all(not t.is_alive() for t in guest._threads + host._threads))

    def test_close_before_start_and_start_after_close(self) -> None:
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        conn = Connection(a)
        conn.close()
        conn.start()
        self.assertTrue(conn.closed)
        self.assertEqual(conn._threads, [])

    def test_heartbeat_timeout_against_silent_peer(self) -> None:
        started = time.monotonic()
        conn, raw = self.raw_pair(ping_interval=0.1, idle_timeout=0.5)
        self.assertEqual(LineReader(raw).next_message(skip=()), {"type": "ping"})
        msg = self.get(conn)
        elapsed = time.monotonic() - started
        self.assertEqual(msg, {"type": "_disconnected", "reason": "timed out"})
        self.assertGreaterEqual(elapsed, 0.45)
        self.assertLess(elapsed, 2.5)
        self.assertTrue(conn.closed)
        self.assert_quiet(conn)

    def test_heartbeat_keeps_live_connection_open(self) -> None:
        host, guest = self.pair(ping_interval=0.1, idle_timeout=0.5)
        time.sleep(1.2)
        self.assertFalse(host.closed)
        self.assertFalse(guest.closed)
        self.assertTrue(host.inbox.empty())
        self.assertTrue(guest.inbox.empty())
        guest.send({"type": "chat", "text": "still here"})
        self.assertEqual(self.get(host), {"type": "chat", "text": "still here"})

    def test_ping_answered_with_pong_and_not_surfaced(self) -> None:
        conn, raw = self.raw_pair(ping_interval=60)
        reader = LineReader(raw)
        raw.sendall(b'{"type":"ping"}\n')
        self.assertEqual(reader.next_message(skip=()), {"type": "pong"})
        raw.sendall(b'{"type":"pong"}\n{"type":"ping","x":1}\n{"type":"after"}\n')
        self.assertEqual(reader.next_message(skip=()), {"type": "pong"})
        self.assertEqual(self.get(conn), {"type": "after"})
        self.assert_quiet(conn)

    def test_concurrent_sends_produce_intact_lines(self) -> None:
        host, guest = self.pair()
        senders, per_sender, padding = 6, 120, "x" * 3000
        errors: List[BaseException] = []

        def blast(sender: int) -> None:
            try:
                for seq in range(per_sender):
                    guest.send({"type": "chat", "sender": sender, "seq": seq, "text": padding})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=blast, args=(i,)) for i in range(senders)]
        for thread in threads:
            thread.start()
        received: Dict[int, List[int]] = {i: [] for i in range(senders)}
        for _ in range(senders * per_sender):
            msg = self.get(host)
            self.assertEqual(msg["type"], "chat", msg)
            self.assertEqual(msg["text"], padding)
            received[msg["sender"]].append(msg["seq"])
        for thread in threads:
            thread.join(WAIT)
        self.assertEqual(errors, [])
        self.assertEqual(received, {i: list(range(per_sender)) for i in range(senders)})
        self.assert_quiet(host)

    def test_send_rejects_invalid_messages(self) -> None:
        host, guest = self.pair()
        for bad in ({}, {"type": 3}, ["type"]):
            with self.assertRaises(ValueError):
                guest.send(bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            guest.send({"type": "chat", "text": "a" * MAX_LINE})
        guest.send({"type": "chat", "text": "lone surrogate \ud800 replaced"})
        self.assertEqual(self.get(host), {"type": "chat", "text": "lone surrogate ? replaced"})
        self.assertFalse(guest.closed)

    def test_send_times_out_when_peer_stops_reading(self) -> None:
        conn, _raw = self.raw_pair(small_buffers=True, ping_interval=60, send_timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(ConnectionClosed):
            for _ in range(5000):
                conn.send({"type": "chat", "text": "z" * 60000})
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.get(conn), {"type": "_disconnected", "reason": "send timed out"})
        self.assertTrue(conn.closed)

    def test_a_slow_reader_cannot_stretch_a_send_past_its_timeout(self) -> None:
        conn, _raw = self.wrapped_pair(DripSocket, ping_interval=None, idle_timeout=None, send_timeout=0.3)
        started = time.monotonic()
        with self.assertRaises(ConnectionClosed):
            conn.send({"type": "chat", "text": "z" * 60000})  # (60 sends of 1 KB: 1.2 s)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.get(conn), {"type": "_disconnected", "reason": "send timed out"})

    def test_send_gives_up_at_its_deadline_not_at_the_next_poll(self) -> None:
        # The socket's own timeout is the reader's poll interval (here made long): a send that is
        # stuck must still give up when send_timeout has passed, not when the next poll would end.
        with mock.patch.object(net, "_POLL_INTERVAL", 1.0):
            conn, _raw = self.raw_pair(small_buffers=True, ping_interval=None, idle_timeout=None, send_timeout=1.2)
            with self.assertRaises(ConnectionClosed):
                for _ in range(5000):
                    started = time.monotonic()
                    conn.send({"type": "chat", "text": "z" * 60000})
            elapsed = time.monotonic() - started  # (of the send that timed out)
        self.assertGreaterEqual(elapsed, 1.15)
        self.assertLess(elapsed, 1.7)  # (giving up at the poll after the deadline: 2 s)
        self.assertEqual(self.get(conn), {"type": "_disconnected", "reason": "send timed out"})

    def test_sends_without_poll_as_on_windows(self) -> None:
        with mock.patch.object(net, "select", mock.Mock(spec=["select"], select=net.select.select)):
            host, guest = self.pair()
            guest.send({"type": "chat", "text": "via select"})
            self.assertEqual(self.get(host), {"type": "chat", "text": "via select"})
            conn, _raw = self.raw_pair(small_buffers=True, ping_interval=None, idle_timeout=None, send_timeout=0.3)
            with self.assertRaises(ConnectionClosed):
                for _ in range(5000):
                    started = time.monotonic()
                    conn.send({"type": "chat", "text": "z" * 60000})
            self.assertLess(time.monotonic() - started, 1.0)


class NoWakeSocket(socket.socket):
    """A socket whose shutdown() does nothing. Like a macOS socket, where neither shutdown() nor
    close() from another thread wakes a thread blocked in recv (nor does close() on Linux)."""

    def shutdown(self, how: int) -> None:
        pass


class CloseTests(NetTestCase):
    """close() must end the reader and heartbeat threads promptly on every OS."""

    def no_wake_pair(self, **options: Any) -> Tuple[Connection, socket.socket]:
        """Like raw_pair, but the Connection's socket is a NoWakeSocket."""
        return self.wrapped_pair(NoWakeSocket, **options)

    def assert_closes_promptly(self, conn: Connection) -> None:
        time.sleep(0.1)  # the reader is now blocked waiting for data
        started = time.monotonic()
        conn.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([t.name for t in conn._threads if t.is_alive()], [])
        self.assertEqual(conn._sock.fileno(), -1)  # closed (by the reader, as it stopped)

    def test_close_ends_the_threads_even_when_shutdown_does_not_wake_recv(self) -> None:
        conn, raw = self.no_wake_pair(ping_interval=0.05, idle_timeout=30)
        self.assertEqual(LineReader(raw).next_message(skip=()), {"type": "ping"})
        self.assertEqual(len(conn._threads), 2)  # reader and heartbeat
        self.assert_closes_promptly(conn)
        raw.settimeout(WAIT)
        while raw.recv(4096):  # pings sent before the close, then the end of the stream
            pass

    def test_close_from_another_thread(self) -> None:
        conn, _raw = self.no_wake_pair(ping_interval=None, idle_timeout=None)
        wait = run_in_thread(self.assert_closes_promptly, conn)
        wait()
        self.assert_quiet(conn)

    def test_peer_reset_while_closing(self) -> None:
        conn, raw = self.raw_pair(ping_interval=None, idle_timeout=None)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        raw.close()  # RST: on macOS a later shutdown() fails with ENOTCONN
        self.assertEqual(self.get(conn)["type"], "_disconnected")
        self.assert_closes_promptly(conn)

    def test_failure_noticed_by_the_heartbeat_closes_the_socket(self) -> None:
        conn, raw = self.no_wake_pair(ping_interval=None, idle_timeout=0.3)
        self.assertEqual(self.get(conn), {"type": "_disconnected", "reason": "timed out"})
        deadline = time.monotonic() + WAIT
        while conn._sock.fileno() != -1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(conn._sock.fileno(), -1)  # without close(): the reader stopped and closed it
        self.assertEqual([t.name for t in conn._threads if t.is_alive()], [])

    def test_send_blocked_on_a_full_buffer_ends_when_closed(self) -> None:
        conn, _raw = self.raw_pair(small_buffers=True, ping_interval=None, idle_timeout=None)
        outcome: List[BaseException] = []

        def flood() -> None:
            try:
                while True:
                    conn.send({"type": "chat", "text": "z" * 60000})
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        time.sleep(0.3)  # the peer reads nothing: the send is now waiting for buffer space
        started = time.monotonic()
        conn.close()
        thread.join(WAIT)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], ConnectionClosed)


class ServerTests(NetTestCase):
    def test_accept_returns_none_on_timeout(self) -> None:
        with Server(port=0, bind=LOCALHOST) as server:
            self.assertGreater(server.port, 0)
            started = time.monotonic()
            self.assertIsNone(server.accept(0.2))
            self.assertGreaterEqual(time.monotonic() - started, 0.15)
            self.assertLess(time.monotonic() - started, 2.0)

    def test_accept_returns_started_connection(self) -> None:
        with Server(port=0, bind=LOCALHOST) as server:
            with connect(LOCALHOST, server.port, timeout=WAIT) as guest:
                with server.accept(WAIT) as host:  # type: ignore[union-attr]
                    self.assertIsInstance(host, Connection)
                    guest.send({"type": "hi"})
                    self.assertEqual(self.get(host), {"type": "hi"})

    def fake_listener(self, server: Server) -> mock.Mock:
        """Replace the server's listening socket by a mock wrapping it (to make accept() fail)."""
        real = server._sock
        self.addCleanup(real.close)
        server._sock = fake = mock.Mock(wraps=real)
        return fake

    def test_accept_skips_a_client_that_gave_up(self) -> None:
        aborted = ConnectionAbortedError(errno.ECONNABORTED, "Software caused connection abort")
        reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        # Linux reports a new connection's pending network error from accept(): retried there only.
        network = [OSError(getattr(errno, name), name) for name in ("EPROTO", "ENETDOWN") if hasattr(errno, name)]
        for linux in (True, False):
            with Server(port=0, bind=LOCALHOST) as server, mock.patch.object(net, "_IS_LINUX", linux):
                fake = self.fake_listener(server)
                for error in [aborted, reset] + network:
                    with self.subTest(linux=linux, error=error):
                        fake.accept.side_effect = error
                        if linux or isinstance(error, ConnectionError):
                            self.assertIsNone(server.accept(0.1))
                        else:  # (e.g. Windows' WSAENETDOWN: the network subsystem has failed)
                            with self.assertRaises(OSError):
                                server.accept(0.1)
                fake.accept.side_effect = OSError(errno.EBADF, "Bad file descriptor")
                with self.assertRaises(OSError):
                    server.accept(0.1)

    def test_accept_raises_a_client_error_that_never_stops(self) -> None:
        with Server(port=0, bind=LOCALHOST) as server:
            fake = self.fake_listener(server)
            error = ConnectionAbortedError(errno.ECONNABORTED, "Software caused connection abort")
            fake.accept.side_effect = error
            for _ in range(net._ACCEPT_ERROR_BURST):  # (as many clients as can plausibly give up at once)
                self.assertIsNone(server.accept(0.1))
            fake.accept.side_effect = None  # a client gets through: the count starts again
            with connect(LOCALHOST, server.port, timeout=WAIT) as guest:
                with server.accept(WAIT) as host:  # type: ignore[union-attr]
                    self.assertIsInstance(host, Connection)
            fake.accept.side_effect = error
            for _ in range(net._ACCEPT_ERROR_BURST):
                self.assertIsNone(server.accept(0.1))
            with self.assertRaises(ConnectionAbortedError):  # instead of the caller retrying at full speed
                for _ in range(100 * net._ACCEPT_ERROR_BURST):
                    server.accept(0.1)

    def test_port_in_use_raises_oserror(self) -> None:
        with Server(port=0, bind=LOCALHOST) as server:
            with self.assertRaises(OSError):
                Server(port=server.port, bind=LOCALHOST)

    def test_close_is_idempotent(self) -> None:
        server = Server(port=0, bind=LOCALHOST)
        server.close()
        server.close()

    def test_connect_refused_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            connect(LOCALHOST, free_port(socket.SOCK_STREAM), timeout=WAIT)


class SpoofVersion:
    """Wraps a Connection so the hello it sends advertises another protocol version."""

    def __init__(self, conn: Connection, version: Any) -> None:
        self.conn, self.version, self.inbox = conn, version, conn.inbox

    def send(self, msg: Dict[str, Any]) -> None:
        if msg.get("type") == "hello":
            msg = dict(msg, version=self.version)
        self.conn.send(msg)


class HandshakeTests(NetTestCase):
    TC = {"initial_ms": 300000, "increment_ms": 3000}

    def handshake(self, color: str, time_control: Any, guest_name: str = "Bob",
                  **kwargs: Any) -> Tuple[Dict[str, Any], Tuple[str, str]]:
        host, guest = self.pair()
        wait = run_in_thread(server_handshake, host, "Alice", color, time_control, timeout=WAIT, **kwargs)
        welcome = client_handshake(guest, guest_name, timeout=WAIT)
        return welcome, wait()

    def test_success_returns_complementary_colours_and_settings(self) -> None:
        welcome, (opponent, host_color) = self.handshake("w", dict(self.TC))
        self.assertEqual((opponent, host_color), ("Bob", "w"))
        self.assertEqual(welcome["type"], "welcome")
        self.assertEqual(welcome["app"], "lanchess")
        self.assertEqual(welcome["version"], PROTOCOL_VERSION)
        self.assertEqual(welcome["name"], "Alice")
        self.assertEqual(welcome["your_color"], "b")
        self.assertEqual(welcome["time_control"], self.TC)
        self.assertEqual(welcome["fen"], net.STARTING_FEN)

    def test_black_untimed_custom_fen(self) -> None:
        fen = "4k3/8/8/8/8/8/4P3/4K3 b - - 0 1"
        welcome, (_opponent, host_color) = self.handshake("b", None, fen=fen)
        self.assertEqual(host_color, "b")
        self.assertEqual(welcome["your_color"], "w")
        self.assertIsNone(welcome["time_control"])
        self.assertEqual(welcome["fen"], fen)

    def test_random_colour_is_w_or_b_and_complementary(self) -> None:
        for _ in range(4):
            welcome, (_opponent, host_color) = self.handshake("random", None)
            self.assertIn(host_color, ("w", "b"))
            self.assertEqual({host_color, welcome["your_color"]}, {"w", "b"})

    def test_time_control_object_with_to_dict(self) -> None:
        class TimeControl:
            def to_dict(self) -> Dict[str, int]:
                return {"initial_ms": 60000, "increment_ms": 0}

        welcome, _result = self.handshake("white", TimeControl())
        self.assertEqual(welcome["time_control"], {"initial_ms": 60000, "increment_ms": 0})

    def test_invalid_server_arguments_raise_value_error(self) -> None:
        host, _guest = self.pair()
        with self.assertRaises(ValueError):
            server_handshake(host, "Alice", "green", None, timeout=0.1)
        with self.assertRaises(ValueError):
            server_handshake(host, "Alice", "w", {"initial_ms": "5"}, timeout=0.1)

    def test_version_mismatch_rejected_on_both_sides(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(server_handshake, host, "Alice", "w", None, timeout=WAIT)
        with self.assertRaises(HandshakeError) as client_error:
            client_handshake(SpoofVersion(guest, 99), "Bob", timeout=WAIT)  # type: ignore[arg-type]
        with self.assertRaises(HandshakeError) as server_error:
            wait()
        self.assertIn("version", str(client_error.exception))
        self.assertIn("99", str(server_error.exception))

    def test_boolean_version_is_rejected(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(server_handshake, host, "Alice", "w", None, timeout=WAIT)
        with self.assertRaises(HandshakeError):
            client_handshake(SpoofVersion(guest, True), "Bob", timeout=WAIT)  # type: ignore[arg-type]
        with self.assertRaises(HandshakeError):
            wait()

    def test_wrong_app_is_rejected(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(server_handshake, host, "Alice", "w", None, timeout=WAIT)
        guest.send({"type": "hello", "app": "othergame", "version": PROTOCOL_VERSION, "name": "x"})
        with self.assertRaises(HandshakeError):
            wait()
        self.assertEqual(self.get(guest)["type"], "reject")

    def test_server_ignores_unrelated_messages_before_hello(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(server_handshake, host, "Alice", "b", None, timeout=WAIT)
        guest.send({"type": "chat", "text": "early"})
        guest._sock.sendall(b"garbage\n")
        welcome = client_handshake(guest, "Bob", timeout=WAIT)
        self.assertEqual(wait(), ("Bob", "b"))
        self.assertEqual(welcome["your_color"], "w")

    def test_client_times_out_without_reply(self) -> None:
        _host, guest = self.pair()
        started = time.monotonic()
        with self.assertRaises(HandshakeError):
            client_handshake(guest, "Bob", timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_server_times_out_without_hello(self) -> None:
        host, _guest = self.pair()
        with self.assertRaises(HandshakeError):
            server_handshake(host, "Alice", "w", None, timeout=0.3)

    def test_disconnect_during_handshake(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(client_handshake, guest, "Bob", timeout=WAIT)
        self.assertEqual(self.get(host)["type"], "hello")
        host.close()
        with self.assertRaises(HandshakeError):
            wait()

    def test_client_rejects_invalid_welcome(self) -> None:
        host, guest = self.pair()
        wait = run_in_thread(client_handshake, guest, "Bob", timeout=WAIT)
        self.assertEqual(self.get(host)["type"], "hello")
        host.send({"type": "welcome", "app": "lanchess", "version": PROTOCOL_VERSION,
                   "name": "A", "your_color": "purple", "time_control": None, "fen": net.STARTING_FEN})
        with self.assertRaises(HandshakeError):
            wait()

    def test_names_are_sanitized(self) -> None:
        nasty = "\x1b[31mEve\x07\n" + "x" * 40
        welcome, (opponent, _colour) = self.handshake("w", None, guest_name=nasty)
        self.assertEqual(opponent, "[31mEve " + "x" * 12)
        self.assertEqual(welcome["name"], "Alice")
        _welcome, (opponent, _colour) = self.handshake("w", None, guest_name=" \t\x00 ")
        self.assertEqual(opponent, "Opponent")


class SanitizeNameTests(unittest.TestCase):
    def test_cases(self) -> None:
        cases = [
            ("  Bob  ", "Bob"),
            ("Zoë ♚ 日本", "Zoë ♚ 日本"),
            ("a\tb\n\nc", "a b c"),
            ("\x00\x07\x1b", "Opponent"),
            ("", "Opponent"),
            (None, "Opponent"),
            (42, "Opponent"),
            ("‮evil​", "evil"),
            ("y" * 50, "y" * 20),
            ("nineteen characters x", "nineteen characters"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_name(raw), expected)

    def test_result_has_no_control_characters(self) -> None:
        name = sanitize_name("".join(chr(i) for i in range(0, 0x250)))
        self.assertLessEqual(len(name), 20)
        self.assertFalse(any(unicodedata.category(ch).startswith("C") for ch in name))


class AddressTests(unittest.TestCase):
    def test_parse_host_port_valid(self) -> None:
        cases = [
            ("10.0.0.5", ("10.0.0.5", DEFAULT_PORT)),
            ("10.0.0.5:6000", ("10.0.0.5", 6000)),
            ("myhost:6000", ("myhost", 6000)),
            ("  myhost  ", ("myhost", DEFAULT_PORT)),
            ("myhost: 7000 ", ("myhost", 7000)),
            ("[::1]:7000", ("::1", 7000)),
            ("[fe80::1]", ("fe80::1", DEFAULT_PORT)),
            ("::1", ("::1", DEFAULT_PORT)),
            ("host:65535", ("host", 65535)),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_host_port(text), expected)
        self.assertEqual(parse_host_port("box", default_port=4242), ("box", 4242))

    def test_parse_host_port_invalid(self) -> None:
        for text in ("", "   ", "host:", "host:abc", "host:0", "host:65536", "host:-1", ":5555",
                     "[::1", "[::1]x", "[]:5555", "my host:5555", "host:５"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_host_port(text)

    def test_local_ip_addresses_shape(self) -> None:
        addresses = local_ip_addresses()
        self.assertIsInstance(addresses, list)
        for entry in addresses:
            self.assertIsInstance(entry, tuple)
            name, address = entry
            self.assertIsInstance(name, str)
            socket.inet_aton(address)
            self.assertFalse(address.startswith("127."))
        self.assertEqual(len({a for _n, a in addresses}), len(addresses))

    def test_broadcast_targets_shape(self) -> None:
        targets = broadcast_targets()
        self.assertIsInstance(targets, list)
        self.assertEqual(targets[0], "255.255.255.255")
        self.assertEqual(len(set(targets)), len(targets))
        for target in targets:
            socket.inet_aton(target)


class SlowResolverTests(NetTestCase):
    """Resolving this computer's own name can take half a minute (macOS, when DNS and mDNS do
    not know it): nothing may wait for that."""

    ADDRESS = "192.168.77.5"

    def setUp(self) -> None:
        super().setUp()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.lookups: List[Any] = []

        def slow(answer: Any) -> Callable[..., Any]:
            def lookup(*args: Any, **kwargs: Any) -> Any:
                self.lookups.append(args)
                self.release.wait(WAIT * 2)
                return answer
            return lookup

        self.cache = net._HostnameAddresses()
        for target, name, value in (
                (net, "_interfaces", lambda: []),  # (no interface list from the kernel, as on Windows)
                (net, "_HOSTNAME_ADDRESSES", self.cache),
                (socket, "getaddrinfo", slow([(socket.AF_INET, socket.SOCK_DGRAM, 17, "", (self.ADDRESS, 0))])),
                (socket, "gethostbyname", slow(self.ADDRESS)),
                (socket, "gethostbyname_ex", slow(("box", [], [self.ADDRESS]))),
                (socket, "getfqdn", slow("box.local"))):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait_for_the_lookup(self) -> None:
        self.release.set()
        deadline = time.monotonic() + WAIT
        while self.cache._running is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNone(self.cache._running)

    def test_local_ip_addresses_waits_briefly_then_uses_the_answer_when_it_comes(self) -> None:
        started = time.monotonic()
        first = local_ip_addresses()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotIn(("", self.ADDRESS), first)
        started = time.monotonic()
        for _ in range(5):  # while the lookup runs, nobody waits for it again
            local_ip_addresses()
            broadcast_targets()
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(self.lookups), 1)
        self.wait_for_the_lookup()
        self.assertIn(("", self.ADDRESS), local_ip_addresses())
        self.assertIn("192.168.77.255", broadcast_targets())
        self.assertEqual(len(self.lookups), 1)  # cached

    def test_discovery_does_not_wait_for_the_lookup(self) -> None:
        started = time.monotonic()
        hosts = discover_hosts(timeout=0.3, discovery_port=free_port(socket.SOCK_DGRAM),
                               extra_targets=[LOCALHOST])
        self.assertEqual(hosts, [])
        self.assertLess(time.monotonic() - started, 1.0)
        self.wait_for_the_lookup()

    def test_a_failed_lookup_means_no_addresses_from_the_name(self) -> None:
        def broken() -> List[str]:
            raise OSError(errno.EAGAIN, "Temporary failure in name resolution")

        cache = net._HostnameAddresses(broken)
        self.assertEqual(cache.get(WAIT), [])
        self.assertIsNone(cache._running)


class InterfaceQueryTests(unittest.TestCase):
    """The ioctl interface list, with the kernel replaced by a fake that answers like macOS."""

    BSD = {"flags": 0xC0206911, "addr": 0xC0206921, "broadcast": 0xC0206923}

    def fake_ioctl(self, interfaces: Dict[str, Tuple[int, Optional[str], Optional[str]]]) -> Callable[..., bytes]:
        def sockaddr_in(address: str) -> bytes:  # BSD: sin_len, sin_family, sin_port, sin_addr, zero
            return bytes([16, socket.AF_INET]) + b"\0\0" + socket.inet_aton(address) + b"\0" * 8

        def ioctl(fd: int, request: int, ifreq: bytes) -> bytes:
            name = ifreq[:16].rstrip(b"\0").decode()
            flags, address, broadcast = interfaces[name]
            if request == self.BSD["flags"]:
                value = struct.pack("H", flags)
            elif request == self.BSD["addr"] and address:
                value = sockaddr_in(address)
            elif request == self.BSD["broadcast"] and broadcast:
                value = sockaddr_in(broadcast)
            else:
                raise OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
            return (ifreq[:16] + value).ljust(len(ifreq), b"\0")

        return ioctl

    def test_macos_layout(self) -> None:
        up, broadcast, loopback, p2p = 0x1, 0x2, 0x8, 0x10
        interfaces = {"lo0": (up | loopback, "127.0.0.1", None),
                      "en0": (up | broadcast, "192.168.1.23", "192.168.1.255"),
                      "en1": (broadcast, "10.9.9.9", "10.9.9.255"),          # down
                      "utun0": (up | p2p, None, None),                       # IPv6 only
                      "en5": (up | broadcast, "169.254.10.20", "169.254.255.255")}
        fake_fcntl = mock.Mock(ioctl=self.fake_ioctl(interfaces))
        names = [(index, name) for index, name in enumerate(interfaces, 1)]
        with mock.patch.multiple(net, fcntl=fake_fcntl, _IS_LINUX=False, _SIOCGIFFLAGS=self.BSD["flags"],
                                 _SIOCGIFADDR=self.BSD["addr"], _SIOCGIFBRDADDR=self.BSD["broadcast"]), \
                mock.patch.object(socket, "if_nameindex", return_value=names, create=True):
            self.assertEqual(net._interfaces(), [("en0", "192.168.1.23", "192.168.1.255"),
                                                 ("en5", "169.254.10.20", "169.254.255.255")])
            with mock.patch.object(net, "_primary_ipv4", return_value="192.168.1.23"):
                self.assertEqual(local_ip_addresses(), [("en0", "192.168.1.23"), ("en5", "169.254.10.20")])

    def test_without_ioctl(self) -> None:
        with mock.patch.object(net, "fcntl", None):
            self.assertEqual(net._interfaces(), [])


@unittest.skipUnless(sys.platform.startswith(("linux", "freebsd")) or sys.platform == "darwin",
                     "the kernel is asked for its interfaces on Linux, macOS and FreeBSD")
class RealInterfaceTests(unittest.TestCase):
    """The ioctl interface list against the real kernel. Wrong request numbers or struct offsets
    would leave it empty or wrong, and the program would quietly fall back to the slow host-name
    lookup (the tests above only check the layout against a fake kernel)."""

    def test_the_address_of_the_default_route_is_listed(self) -> None:
        primary = net._primary_ipv4()
        if primary is None:
            self.skipTest("no default route")
        interfaces = net._interfaces()
        self.assertIn(primary, [address for _name, address, _broadcast in interfaces], interfaces)
        for name, address, broadcast in interfaces:
            self.assertTrue(name)
            self.assertTrue(net._is_usable_ipv4(address), address)
            self.assertTrue(broadcast is None or net._is_usable_ipv4(broadcast), broadcast)


class DiscoveryTests(NetTestCase):
    def responder(self, name: str = "Alice's laptop", game_port: int = 6123) -> DiscoveryResponder:
        responder = DiscoveryResponder(name, game_port, discovery_port=free_port(socket.SOCK_DGRAM))
        self.addCleanup(responder.stop)
        self.assertTrue(responder.start())
        self.assertTrue(responder.running)
        return responder

    def test_discovery_finds_responder(self) -> None:
        responder = self.responder()
        hosts = discover_hosts(timeout=1.2, discovery_port=responder.discovery_port,
                               extra_targets=[LOCALHOST])
        self.assertIsInstance(hosts, list)
        self.assertIn(HostInfo("Alice's laptop", LOCALHOST, 6123), hosts)
        self.assertEqual(len(set((h.address, h.port) for h in hosts)), len(hosts))

    def test_responder_answers_only_valid_discover(self) -> None:
        responder = self.responder(name="Bob\x1b[2J", game_port=7000)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(WAIT)
            target = (LOCALHOST, responder.discovery_port)
            for junk in (b"\xff\x00", b"[]", b'{"app":"other","type":"discover","v":1}',
                         b'{"app":"lanchess","type":"announce","v":1}',
                         b'{"app":"lanchess","type":"discover","v":2}'):
                client.sendto(junk, target)
            client.sendto(b'{"app":"lanchess","type":"discover","v":1}', target)
            data, sender = client.recvfrom(4096)
            self.assertEqual(sender[1], responder.discovery_port)
            self.assertEqual(json.loads(data.decode("utf-8")),
                             {"app": "lanchess", "type": "announce", "v": 1, "name": "Bob[2J", "port": 7000})
            client.settimeout(QUIET)
            with self.assertRaises(socket.timeout):
                client.recvfrom(4096)

    def test_responder_start_returns_false_when_port_taken(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
            blocker.bind(("", 0))
            responder = DiscoveryResponder("x", 5555, discovery_port=blocker.getsockname()[1])
            self.assertFalse(responder.start())
            self.assertFalse(responder.running)
            responder.stop()

    def test_responder_stop_is_idempotent(self) -> None:
        responder = self.responder()
        responder.stop()
        responder.stop()
        self.assertFalse(responder.running)

    def test_discover_without_responder_returns_empty_list(self) -> None:
        started = time.monotonic()
        hosts = discover_hosts(timeout=0.3, discovery_port=free_port(socket.SOCK_DGRAM),
                               extra_targets=[LOCALHOST])
        self.assertEqual(hosts, [])
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()

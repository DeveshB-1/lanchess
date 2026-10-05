"""Networking for LAN Chess: JSON-lines over TCP, handshake and UDP LAN discovery."""

from __future__ import annotations

import json
import os
import queue
import random
import socket
import struct
import sys
import threading
import time
import unicodedata
from collections import namedtuple
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "PROTOCOL_VERSION", "DEFAULT_PORT", "DISCOVERY_PORT", "MAX_LINE", "APP_ID", "STARTING_FEN",
    "SEND_TIMEOUT", "ConnectionClosed", "HandshakeError", "Connection", "Server", "connect",
    "parse_host_port", "local_ip_addresses", "broadcast_targets", "HostInfo",
    "DiscoveryResponder", "discover_hosts", "client_handshake", "server_handshake", "sanitize_name",
]

PROTOCOL_VERSION = 1
DEFAULT_PORT = 5555
DISCOVERY_PORT = 5556
MAX_LINE = 65536
APP_ID = "lanchess"
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
SEND_TIMEOUT = 10.0
MAX_NAME_LEN = 20

_IS_WINDOWS = os.name == "nt"
_IS_LINUX = sys.platform.startswith("linux")
_SIOCGIFFLAGS = 0x8913
_SIOCGIFADDR = 0x8915
_SIOCGIFBRDADDR = 0x8919
_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8
_RECV_CHUNK = 65536
_DATAGRAM_MAX = 4096
_DISCOVERY_INTERVAL = 0.5
_RESPONDER_POLL = 0.2
_JOIN_TIMEOUT = 2.0
_CONTROL_CATEGORIES = ("Cc", "Cf", "Cs")

HostInfo = namedtuple("HostInfo", "name address port")


class ConnectionClosed(Exception):
    """Raised when sending on a connection that is closed or broken."""


class HandshakeError(Exception):
    """Raised when the opening handshake fails or is rejected."""


def _encode(msg: Dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")


def _int_value(value: Any) -> Optional[int]:
    """Return ``value`` as an int if it is an integral number (bools excluded), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _describe(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or type(exc).__name__


def _clean_text(text: Any, max_len: int) -> str:
    """Strip control characters, collapse whitespace and truncate to ``max_len`` characters."""
    if not isinstance(text, str):
        return ""
    kept = (" " if ch.isspace() else ch for ch in text
            if ch.isspace() or unicodedata.category(ch) not in _CONTROL_CATEGORIES)
    return " ".join("".join(kept).split())[:max_len].rstrip()


def sanitize_name(name: Any, default: str = "Opponent", max_len: int = MAX_NAME_LEN) -> str:
    """Return a display-safe player name (no control chars, at most ``max_len`` chars)."""
    return _clean_text(name, max_len) or default


def _format_peer(sock: socket.socket) -> str:
    try:
        address = sock.getpeername()
    except (OSError, ValueError):
        return "unknown"
    if isinstance(address, tuple) and len(address) >= 2:
        host, port = str(address[0]), address[1]
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return str(address) or "unknown"


class Connection:
    """A newline-framed JSON message channel with background reader and heartbeat threads."""

    def __init__(self, sock: socket.socket, *, ping_interval: Optional[float] = 5.0,
                 idle_timeout: Optional[float] = 30.0, send_timeout: float = SEND_TIMEOUT) -> None:
        self.inbox: queue.Queue[Dict[str, Any]] = queue.Queue()
        self.peer = _format_peer(sock)
        self._sock = sock
        self._ping_interval = ping_interval if ping_interval and ping_interval > 0 else None
        self._idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._started = False
        self._last_recv = time.monotonic()
        self._threads: List[threading.Thread] = []
        sock.settimeout(send_timeout)

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def start(self) -> Connection:
        """Configure the socket and launch the reader and heartbeat threads (once)."""
        with self._state_lock:
            if self._started or self._closed:
                return self
            self._started = True
        for level, option in ((socket.IPPROTO_TCP, socket.TCP_NODELAY),
                              (socket.SOL_SOCKET, socket.SO_KEEPALIVE)):
            try:
                self._sock.setsockopt(level, option, 1)
            except OSError:
                pass
        self._last_recv = time.monotonic()
        self._spawn(self._read_loop, "reader")
        if self._ping_interval or self._idle_timeout:
            self._spawn(self._heartbeat_loop, "heartbeat")
        return self

    def send(self, msg: Dict[str, Any]) -> None:
        """Send one message; raises ConnectionClosed if closed or broken, ValueError if invalid."""
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise ValueError("message must be a dict with a string 'type'")
        data = _encode(msg)
        if len(data) > MAX_LINE:
            raise ValueError(f"message too long ({len(data)} bytes, limit {MAX_LINE})")
        with self._send_lock:
            if self._closed:
                raise ConnectionClosed("connection is closed")
            try:
                self._sock.sendall(data + b"\n")
            except socket.timeout as exc:
                self._fail("send timed out")
                raise ConnectionClosed("send timed out") from exc
            except (OSError, ValueError) as exc:
                reason = f"connection lost: {_describe(exc)}"
                self._fail(reason)
                raise ConnectionClosed(reason) from exc

    def close(self) -> None:
        """Close the connection and stop its threads. Safe to call repeatedly."""
        with self._state_lock:
            already_closed = self._closed
            self._closed = True
        self._stop.set()
        if not already_closed:
            self._release_socket()
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(_JOIN_TIMEOUT)

    def _spawn(self, target: Any, role: str) -> None:
        thread = threading.Thread(target=target, name=f"lanchess-{role}-{self.peer}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _release_socket(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _fail(self, reason: str) -> None:
        """Mark the connection dead and report it exactly once (unless closed locally)."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self.inbox.put({"type": "_disconnected", "reason": reason})
        self._stop.set()
        self._release_socket()

    def _deliver(self, msg: Dict[str, Any]) -> None:
        with self._state_lock:
            if not self._closed:
                self.inbox.put(msg)

    def _protocol_error(self, reason: str) -> None:
        self._deliver({"type": "_protocol_error", "reason": reason})

    def _read_loop(self) -> None:
        buffer = bytearray()
        while not self._closed:
            try:
                data = self._sock.recv(_RECV_CHUNK)
            except socket.timeout:
                continue
            except (OSError, ValueError) as exc:
                self._fail(f"connection lost: {_describe(exc)}")
                return
            if not data:
                self._fail("connection closed by peer")
                return
            self._last_recv = time.monotonic()
            buffer += data
            start = 0
            while not self._closed:
                end = buffer.find(b"\n", start)
                if end < 0:
                    break
                if end - start > MAX_LINE:
                    self._fail("message too long")
                    return
                self._handle_line(bytes(buffer[start:end]))
                start = end + 1
            del buffer[:start]
            if len(buffer) > MAX_LINE:
                self._fail("message too long")
                return

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._protocol_error("invalid UTF-8")
            return
        try:
            msg = json.loads(text)
        except (ValueError, RecursionError):
            self._protocol_error("invalid JSON")
            return
        if not isinstance(msg, dict):
            self._protocol_error("message is not a JSON object")
            return
        kind = msg.get("type")
        if not isinstance(kind, str):
            self._protocol_error("message has no string 'type'")
        elif kind.startswith("_"):
            self._protocol_error(f"reserved message type {kind[:40]!r}")
        elif kind == "ping":
            try:
                self.send({"type": "pong"})
            except ConnectionClosed:
                pass
        elif kind != "pong":
            self._deliver(msg)

    def _heartbeat_loop(self) -> None:
        interval, idle = self._ping_interval, self._idle_timeout
        next_ping = time.monotonic() + interval if interval is not None else float("inf")
        while not self._stop.is_set():
            now = time.monotonic()
            idle_deadline = self._last_recv + idle if idle is not None else float("inf")
            if now >= idle_deadline:
                self._fail("timed out")
                return
            if interval is not None and now >= next_ping:
                try:
                    self.send({"type": "ping"})
                except ConnectionClosed:
                    return
                next_ping = time.monotonic() + interval
            self._stop.wait(max(0.01, min(next_ping, idle_deadline) - time.monotonic()))


class Server:
    """A listening TCP socket that hands out started Connections."""

    def __init__(self, port: int = DEFAULT_PORT, bind: str = "0.0.0.0", *,
                 ping_interval: Optional[float] = 5.0, idle_timeout: Optional[float] = 30.0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _set_address_reuse(sock)
            sock.bind((bind, port))
            sock.listen(4)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._options = {"ping_interval": ping_interval, "idle_timeout": idle_timeout}
        self.port: int = sock.getsockname()[1]

    def __enter__(self) -> Server:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def accept(self, timeout: Optional[float] = None) -> Optional[Connection]:
        """Wait up to ``timeout`` seconds for a client; return a started Connection or None."""
        self._sock.settimeout(timeout)
        try:
            client, _address = self._sock.accept()
        except socket.timeout:
            return None
        except ConnectionAbortedError:
            return None
        return Connection(client, **self._options).start()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _set_address_reuse(sock: socket.socket) -> None:
    """SO_REUSEADDR on POSIX only: on Windows it would let another socket steal the port."""
    if _IS_WINDOWS:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass


def connect(host: str, port: int = DEFAULT_PORT, timeout: float = 10.0, *,
            ping_interval: Optional[float] = 5.0, idle_timeout: Optional[float] = 30.0) -> Connection:
    """Open a TCP connection to a host and return a started Connection (raises OSError)."""
    sock = socket.create_connection((host, port), timeout=timeout)
    return Connection(sock, ping_interval=ping_interval, idle_timeout=idle_timeout).start()


def _parse_port(text: str) -> int:
    value = text.strip()
    if not (value.isascii() and value.isdigit()) or not 0 < int(value) < 65536:
        raise ValueError(f"Invalid port {text!r} (expected a number from 1 to 65535)")
    return int(value)


def parse_host_port(text: str, default_port: int = DEFAULT_PORT) -> Tuple[str, int]:
    """Split ``"host"``, ``"host:port"`` or ``"[ipv6]:port"`` into (host, port)."""
    value = (text or "").strip()
    port_text: Optional[str] = None
    if value.startswith("["):
        end = value.find("]")
        rest = value[end + 1:] if end >= 0 else ""
        if end < 0 or (rest and not rest.startswith(":")):
            raise ValueError(f"Invalid address {text!r}")
        host = value[1:end]
        port_text = rest[1:] if rest else None
    elif value.count(":") == 1:
        host, port_text = value.split(":")
    else:
        host = value
    host = host.strip()
    if not host or any(ch.isspace() for ch in host):
        raise ValueError(f"Invalid host {text!r}")
    return host, default_port if port_text is None else _parse_port(port_text)


def _is_usable_ipv4(address: Any) -> bool:
    if not isinstance(address, str) or address.count(".") != 3:
        return False
    try:
        packed = socket.inet_aton(address)
    except OSError:
        return False
    return packed[0] != 127 and packed != b"\0\0\0\0"


def _linux_interfaces() -> List[Tuple[str, str, Optional[str]]]:
    """(name, ipv4, broadcast) for each up, non-loopback IPv4 interface. Linux only."""
    if not _IS_LINUX or fcntl is None:
        return []
    try:
        names = [name for _index, name in socket.if_nameindex()]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []

    def query(request: int, ifreq: bytes, offset: int, size: int) -> Optional[bytes]:
        try:
            return fcntl.ioctl(probe.fileno(), request, ifreq)[offset:offset + size]
        except OSError:
            return None

    results = []
    with probe:
        for name in names:
            ifreq = struct.pack("256s", name.encode("utf-8", "replace")[:15])
            raw_flags = query(_SIOCGIFFLAGS, ifreq, 16, 2)
            flags = struct.unpack("H", raw_flags)[0] if raw_flags else _IFF_UP
            raw_address = query(_SIOCGIFADDR, ifreq, 20, 4)
            if not flags & _IFF_UP or flags & _IFF_LOOPBACK or not raw_address:
                continue
            address = socket.inet_ntoa(raw_address)
            if not _is_usable_ipv4(address):
                continue
            raw_broadcast = query(_SIOCGIFBRDADDR, ifreq, 20, 4)
            broadcast = socket.inet_ntoa(raw_broadcast) if raw_broadcast else None
            results.append((name, address, broadcast if _is_usable_ipv4(broadcast) else None))
    return results


def _primary_ipv4() -> Optional[str]:
    """The source address the OS would use for the default route (a UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return address if _is_usable_ipv4(address) else None


def _hostname_ipv4s() -> List[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except (OSError, ValueError):
        return []
    return [str(info[4][0]) for info in infos]


def local_ip_addresses() -> List[Tuple[str, str]]:
    """Non-loopback IPv4 addresses as (interface name or "", address), primary first. Never raises."""
    found: Dict[str, str] = {}
    for name, address, _broadcast in _linux_interfaces():
        found.setdefault(address, name)
    if not found or not _IS_LINUX:
        for address in _hostname_ipv4s():
            if _is_usable_ipv4(address):
                found.setdefault(address, "")
    primary = _primary_ipv4()
    if primary:
        found.setdefault(primary, "")
    ordered = sorted(found.items(), key=lambda item: item[0] != primary)
    return [(name, address) for address, name in ordered]


def _unique(items: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(items))


def broadcast_targets() -> List[str]:
    """Limited broadcast, per-interface broadcasts and x.y.z.255 guesses, deduplicated. Never raises."""
    targets = ["255.255.255.255"]
    targets.extend(broadcast for _n, _a, broadcast in _linux_interfaces() if broadcast)
    targets.extend(address.rsplit(".", 1)[0] + ".255" for _name, address in local_ip_addresses())
    return _unique(targets)


def _decode_datagram(data: bytes) -> Optional[Dict[str, Any]]:
    try:
        msg = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return msg if isinstance(msg, dict) and msg.get("app") == APP_ID else None


def _discover_payload() -> bytes:
    return _encode({"app": APP_ID, "type": "discover", "v": 1})


class DiscoveryResponder:
    """Answers LAN discovery datagrams with this host's name and game port."""

    def __init__(self, name: str, game_port: int, discovery_port: int = DISCOVERY_PORT) -> None:
        self.name = sanitize_name(name, default="Host")
        self.game_port = game_port
        self.discovery_port = discovery_port
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> DiscoveryResponder:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Bind the discovery port and answer in a daemon thread; False if the bind fails."""
        if self._thread is not None:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return False
        try:
            _set_address_reuse(sock)
            sock.bind(("", self.discovery_port))
            sock.settimeout(_RESPONDER_POLL)
        except OSError:
            sock.close()
            return False
        self.discovery_port = sock.getsockname()[1]
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, args=(sock,),
                                        name="lanchess-discovery", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop answering and release the port. Safe to call repeatedly."""
        self._stop.set()
        thread, sock = self._thread, self._sock
        self._thread = self._sock = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(_JOIN_TIMEOUT)
        if sock is not None:
            sock.close()

    def _serve(self, sock: socket.socket) -> None:
        reply = _encode({"app": APP_ID, "type": "announce", "v": 1,
                         "name": self.name, "port": self.game_port})
        while not self._stop.is_set():
            try:
                data, sender = sock.recvfrom(_DATAGRAM_MAX)
            except socket.timeout:
                continue
            except OSError:
                self._stop.wait(0.05)
                continue
            msg = _decode_datagram(data)
            if msg and msg.get("type") == "discover" and _int_value(msg.get("v")) == 1:
                try:
                    sock.sendto(reply, sender)
                except OSError:
                    pass


def _parse_announce(data: bytes, sender: Any) -> Optional[HostInfo]:
    msg = _decode_datagram(data)
    if not msg or msg.get("type") != "announce":
        return None
    port = _int_value(msg.get("port"))
    if port is None or not 0 < port < 65536:
        return None
    return HostInfo(sanitize_name(msg.get("name"), default="Host"), str(sender[0]), port)


def discover_hosts(timeout: float = 2.5, discovery_port: int = DISCOVERY_PORT,
                   extra_targets: Sequence[str] = ()) -> List[HostInfo]:
    """Broadcast discovery requests for ``timeout`` seconds and return the hosts that answered."""
    found: Dict[Tuple[str, int], HostInfo] = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    with sock:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            sock.bind(("", 0))
        except OSError:
            return []
        targets = _unique(list(broadcast_targets()) + [str(t) for t in extra_targets])
        payload = _discover_payload()
        deadline = time.monotonic() + max(0.0, timeout)
        next_send = 0.0
        while True:
            now = time.monotonic()
            if now >= next_send:
                for target in targets:
                    try:
                        sock.sendto(payload, (target, discovery_port))
                    except (OSError, ValueError):
                        pass
                next_send = now + _DISCOVERY_INTERVAL
            if now >= deadline:
                break
            try:
                sock.settimeout(max(0.01, min(deadline, next_send) - now))
                data, sender = sock.recvfrom(_DATAGRAM_MAX)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.01)
                continue
            host = _parse_announce(data, sender)
            if host is not None:
                found.setdefault((host.address, host.port), host)
    return list(found.values())


def _resolve_color(color_pref: str) -> str:
    value = str(color_pref).strip().lower()
    if value in ("w", "white"):
        return "w"
    if value in ("b", "black"):
        return "b"
    if value == "random":
        return random.choice("wb")
    raise ValueError(f"Invalid color preference {color_pref!r} (expected 'w', 'b' or 'random')")


def _normalize_time_control(time_control: Any) -> Optional[Dict[str, int]]:
    """Return ``{"initial_ms", "increment_ms"}`` ints (accepts a dict or an object with to_dict())."""
    if time_control is not None and hasattr(time_control, "to_dict"):
        time_control = time_control.to_dict()
    if time_control is None:
        return None
    if not isinstance(time_control, dict):
        raise ValueError(f"Invalid time control {time_control!r}")
    initial = _int_value(time_control.get("initial_ms"))
    increment = _int_value(time_control.get("increment_ms"))
    if initial is None or increment is None or initial <= 0 or increment < 0:
        raise ValueError(f"Invalid time control {time_control!r}")
    return {"initial_ms": initial, "increment_ms": increment}


def _send_handshake(conn: Connection, msg: Dict[str, Any]) -> None:
    try:
        conn.send(msg)
    except ConnectionClosed as exc:
        raise HandshakeError(f"Connection lost during handshake ({exc})") from exc


def _reject(conn: Connection, reason: str) -> None:
    try:
        conn.send({"type": "reject", "reason": reason})
    except ConnectionClosed:
        pass


def _await_message(conn: Connection, wanted: Sequence[str], deadline: float, waiting_for: str) -> Dict[str, Any]:
    """Return the next inbox message whose type is in ``wanted``, skipping unrelated ones."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HandshakeError(f"Timed out waiting for {waiting_for}")
        try:
            msg = conn.inbox.get(timeout=remaining)
        except queue.Empty:
            continue
        kind = msg.get("type")
        if kind == "_disconnected":
            raise HandshakeError(f"Connection lost during handshake ({msg.get('reason', 'unknown reason')})")
        if kind in wanted:
            return msg


def client_handshake(conn: Connection, my_name: str, timeout: float = 10.0) -> Dict[str, Any]:
    """Say hello to the host and return its (validated) welcome message; raises HandshakeError."""
    deadline = time.monotonic() + timeout
    _send_handshake(conn, {"type": "hello", "app": APP_ID, "version": PROTOCOL_VERSION,
                           "name": sanitize_name(my_name)})
    msg = _await_message(conn, ("welcome", "reject"), deadline, "the host to respond")
    if msg["type"] == "reject":
        raise HandshakeError(_clean_text(msg.get("reason"), 200) or "The host rejected the connection")
    if msg.get("app") != APP_ID or _int_value(msg.get("version")) != PROTOCOL_VERSION:
        raise HandshakeError(f"Protocol version mismatch: host uses version "
                             f"{_clean_text(str(msg.get('version')), 16)}, this client uses {PROTOCOL_VERSION}")
    color = msg.get("your_color")
    if color not in ("w", "b"):
        raise HandshakeError("The host sent an invalid colour assignment")
    try:
        time_control = _normalize_time_control(msg.get("time_control"))
    except ValueError:
        raise HandshakeError("The host sent an invalid time control") from None
    fen = msg.get("fen")
    if fen is None:
        fen = STARTING_FEN
    elif not isinstance(fen, str):
        raise HandshakeError("The host sent an invalid starting position")
    welcome = dict(msg)
    welcome.update(name=sanitize_name(msg.get("name")), your_color=color,
                   time_control=time_control, fen=fen)
    return welcome


def server_handshake(conn: Connection, my_name: str, color_pref: str, time_control: Any,
                     fen: str = STARTING_FEN, timeout: float = 10.0) -> Tuple[str, str]:
    """Wait for the joiner's hello, send welcome; return (opponent_name, host_color)."""
    host_color = _resolve_color(color_pref)
    tc = _normalize_time_control(time_control)
    hello = _await_message(conn, ("hello",), time.monotonic() + timeout, "the joining player")
    if hello.get("app") != APP_ID:
        _reject(conn, "Not a lanchess client")
        raise HandshakeError("The connecting program is not a lanchess client")
    version = hello.get("version")
    if _int_value(version) != PROTOCOL_VERSION:
        shown = _clean_text(str(version), 16) or "?"
        _reject(conn, f"Protocol version mismatch: host uses version {PROTOCOL_VERSION}, you use {shown}")
        raise HandshakeError(f"Protocol version mismatch: opponent uses version {shown}, "
                             f"this host uses {PROTOCOL_VERSION}")
    _send_handshake(conn, {"type": "welcome", "app": APP_ID, "version": PROTOCOL_VERSION,
                           "name": sanitize_name(my_name), "your_color": "b" if host_color == "w" else "w",
                           "time_control": tc, "fen": fen or STARTING_FEN})
    return sanitize_name(hello.get("name")), host_color

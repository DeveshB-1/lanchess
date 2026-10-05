"""Tests for lanchess.menu: every screen driven by scripted keys, with fake network, clock and terminal.

Nothing here needs a terminal or a network: MenuContext takes fakes for the server, the discovery
responder, the LAN search, connect and both handshakes, and runs background work inline
(``threaded=False``) so every test is deterministic.
"""

from __future__ import annotations

import contextlib
import errno
import io
import os
import queue
import tempfile
import threading
import time
import unittest
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from unittest import mock

from lanchess import cli, config, menu, net, term, ui
from lanchess.engine import STARTING_FEN
from lanchess.game import TimeControl

IPS = [("wlan0", "192.168.1.23"), ("eth0", "10.0.0.5")]


# -- fakes ------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeConn:
    def __init__(self, peer: str = "192.168.1.40:51000") -> None:
        self.peer = peer
        self.closed = False
        self.sent: List[Dict[str, Any]] = []
        self.inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    def send(self, msg: Dict[str, Any]) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


class FakeServer:
    def __init__(self, port: int) -> None:
        self.port = port
        self.closed = False
        self.incoming: deque = deque()
        self.accepts = 0

    def accept(self, timeout: float) -> Optional[FakeConn]:
        self.accepts += 1
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        return self.incoming.popleft() if self.incoming else None

    def close(self) -> None:
        self.closed = True


class FakeResponder:
    def __init__(self, name: str, port: int, ok: bool = True) -> None:
        self.name, self.port, self.ok = name, port, ok
        self.started = self.stopped = False

    def start(self) -> bool:
        self.started = True
        return self.ok

    def stop(self) -> None:
        self.stopped = True


class FakeDiscovery:
    def __init__(self, hosts: Sequence[net.HostInfo] = (), rounds: int = 1) -> None:
        self.hosts = list(hosts)
        self.rounds = rounds
        self.error = ""
        self.started = self.stopped = False
        self.rescans = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def rescan(self) -> None:
        self.rescans += 1
        self.rounds = 0

    def snapshot(self) -> Tuple[List[net.HostInfo], int]:
        return list(self.hosts), self.rounds


class FakeKeys:
    """Hands out scripted keys; None means "no key this time" (a timeout); afterwards: end of input."""

    def __init__(self, keys: Iterable[Any] = ()) -> None:
        self.keys: deque = deque(keys)
        self.eof = False
        self.starts = self.restores = 0
        self.timeouts: List[float] = []

    def add(self, *keys: Any) -> None:
        self.keys.extend(keys)

    def start(self) -> None:
        self.starts += 1

    def restore(self) -> None:
        self.restores += 1

    def read_key(self, timeout: float) -> Optional[str]:
        self.timeouts.append(timeout)
        if self.keys:
            key = self.keys.popleft()
            return key() if callable(key) else key
        self.eof = True
        return term.CTRL_D


class FakeDisplay:
    def __init__(self) -> None:
        self.frames: List[Tuple[str, Optional[Tuple[int, int]], Tuple[int, int]]] = []
        self.enters = self.exits = 0

    def enter(self) -> None:
        self.enters += 1

    def exit(self) -> None:
        self.exits += 1

    def draw(self, frame: str, cursor: Optional[Tuple[int, int]], size: Optional[Tuple[int, int]] = None) -> None:
        self.frames.append((frame, cursor, size or (0, 0)))

    @property
    def last(self) -> str:
        return ui.strip_ansi(self.frames[-1][0]) if self.frames else ""


def flat(text: str) -> str:
    """Screen text with line breaks and indentation folded into single spaces (wrapped lines rejoin)."""
    return " ".join(text.split())


def typed(text: str, enter: bool = True) -> List[str]:
    return list(text) + ([term.ENTER] if enter else [])


class MenuTestCase(unittest.TestCase):
    """Builds a MenuContext full of fakes, with a private config file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanchess-menu-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_path = os.path.join(self.tmp, "config.json")
        env = mock.patch.dict(os.environ, {config.ENV_DIR: os.path.join(self.tmp, "envcfg")})
        env.start()
        self.addCleanup(env.stop)
        self.clock = FakeClock()
        self.servers: List[FakeServer] = []
        self.responders: List[FakeResponder] = []
        self.discoveries: List[FakeDiscovery] = []
        self.hosts: List[net.HostInfo] = []
        self.rounds = 1
        self.responder_ok = True
        self.played: List[menu.GameRequest] = []
        self.connects: List[Tuple[str, int]] = []
        self.connect_errors: List[OSError] = []
        self.handshakes: List[Tuple[Any, ...]] = []
        self.server_handshake_result: Any = ("Bob", "w")
        self.welcome: Dict[str, Any] = {"name": "Alice", "your_color": "b",
                                        "time_control": {"initial_ms": 300000, "increment_ms": 3000},
                                        "fen": STARTING_FEN}

    def make_ctx(self, settings: Optional[config.Settings] = None, explicit: Optional[Dict[str, Any]] = None,
                 theme: Optional[ui.Theme] = ui.Theme(unicode=True, color=False), **kwargs: Any) -> menu.MenuContext:
        def server_factory(port: int) -> FakeServer:
            server = FakeServer(port)
            self.servers.append(server)
            return server

        def responder_factory(name: str, port: int) -> FakeResponder:
            responder = FakeResponder(name, port, self.responder_ok)
            self.responders.append(responder)
            return responder

        def discovery_factory() -> FakeDiscovery:
            discovery = FakeDiscovery(self.hosts, self.rounds)
            self.discoveries.append(discovery)
            return discovery

        def connect(host: str, port: int, timeout: float = 10.0) -> FakeConn:
            self.connects.append((host, port))
            if self.connect_errors:
                raise self.connect_errors.pop(0)
            return FakeConn(f"{host}:{port}")

        def server_handshake(conn: Any, name: str, color: str, time_control: Any, fen: str,
                             timeout: float = 10.0) -> Tuple[str, str]:
            self.handshakes.append((conn, name, color, time_control, fen))
            result = self.server_handshake_result
            if isinstance(result, BaseException):
                raise result
            return result

        def client_handshake(conn: Any, name: str, timeout: float = 10.0) -> Dict[str, Any]:
            self.handshakes.append((conn, name))
            return dict(self.welcome)

        def play(request: menu.GameRequest) -> List[menu.Message]:
            self.played.append(request)
            return [("info", "Result: test game over")]

        options: Dict[str, Any] = dict(
            theme=theme, clock=self.clock, size_fn=lambda: (100, 30), threaded=False,
            server_factory=server_factory, responder_factory=responder_factory,
            discovery_factory=discovery_factory, connect=connect, server_handshake=server_handshake,
            client_handshake=client_handshake, local_ips=lambda: list(IPS), firewall=lambda: "firewalld",
            play=play)
        options.update(kwargs)
        if settings is None:
            settings = config.load(self.config_path)
        return menu.MenuContext(settings, explicit or {}, self.config_path, **options)

    def frame(self, screen: menu.Screen, ctx: menu.MenuContext, width: int = 100, height: int = 30) -> str:
        return ui.strip_ansi(menu.compose(screen, ctx.style, width, height)[0])

    def press(self, screen: menu.Screen, *keys: str) -> Optional[menu.Action]:
        action = None
        for key in keys:
            action = screen.handle_key(key)
        return action

    def run_app(self, ctx: menu.MenuContext, keys: Iterable[Any]) -> Tuple[menu.MenuApp, FakeKeys, FakeDisplay]:
        reader, display = FakeKeys(keys), FakeDisplay()
        app = menu.MenuApp(ctx, reader, display)
        self.assertEqual(app.run(), 0)
        return app, reader, display


# -- main menu --------------------------------------------------------------------------------


class MainMenuTests(MenuTestCase):
    def test_navigation_wraps_and_supports_vi_keys(self) -> None:
        screen = menu.MainMenu(self.make_ctx())
        self.assertEqual(screen.selected, "host")
        self.press(screen, term.UP)
        self.assertEqual(screen.selected, "quit")  # wraps to the bottom
        self.press(screen, term.DOWN)
        self.assertEqual(screen.selected, "host")  # and back to the top
        self.press(screen, "j", "j")
        self.assertEqual(screen.selected, "local")
        self.press(screen, "k")
        self.assertEqual(screen.selected, "join")
        self.press(screen, term.END)
        self.assertEqual(screen.selected, "quit")
        self.press(screen, term.HOME)
        self.assertEqual(screen.selected, "host")

    def test_enter_and_number_shortcuts_open_screens(self) -> None:
        ctx = self.make_ctx()
        expected = {"1": menu.HostScreen, "2": menu.JoinScreen, "3": menu.LocalScreen,
                    "4": menu.SettingsScreen, "5": menu.HelpScreen}
        for key, cls in expected.items():
            with self.subTest(key=key):
                screen = menu.MainMenu(ctx)
                action = screen.handle_key(key)
                self.assertEqual(action.kind, "push")
                self.assertIsInstance(action.screen, cls)
                action.screen.on_leave()
        screen = menu.MainMenu(ctx)
        self.assertEqual(screen.handle_key("6").kind, "quit")
        self.assertIsNone(screen.handle_key("7"))  # no such item
        screen = menu.MainMenu(ctx)
        self.press(screen, term.DOWN)
        action = screen.handle_key(term.ENTER)
        self.assertIsInstance(action.screen, menu.JoinScreen)
        action.screen.on_leave()

    def test_escape_q_and_ctrl_c_quit(self) -> None:
        for key in (term.ESCAPE, "q", "Q", term.CTRL_C):
            with self.subTest(key=key):
                self.assertEqual(menu.MainMenu(self.make_ctx()).handle_key(key).kind, "quit")

    def test_main_screen_contents(self) -> None:
        ctx = self.make_ctx()
        text = self.frame(menu.MainMenu(ctx), ctx)
        for item in ("Host a game", "Join a game", "Local game", "Settings", "How to play", "Quit"):
            self.assertIn(item, text)
        self.assertIn("v" + menu.__version__, text)
        self.assertIn("Your IP: 192.168.1.23 (wlan0), 10.0.0.5 (eth0)", text)
        self.assertIn("Enter select", text)
        self.assertIn("▸", text)
        self.assertIn("█", text)  # the big title
        self.assertEqual(len(menu.compose(menu.MainMenu(ctx), ctx.style, 100, 30)[0].split("\n")), 30)

    def test_ascii_and_no_color(self) -> None:
        ctx = self.make_ctx(theme=ui.Theme(unicode=False, color=False))
        for screen in (menu.MainMenu(ctx), menu.HostScreen(ctx), menu.SettingsScreen(ctx), menu.HelpScreen(ctx),
                       menu.WaitingScreen(ctx, "white", None, 5555), menu.JoinScreen(ctx)):
            with self.subTest(screen=type(screen).__name__):
                frame = menu.compose(screen, ctx.style, 100, 30)[0]
                self.assertTrue(frame.isascii(), [ch for ch in frame if not ch.isascii()][:5])
                self.assertNotIn("\x1b", frame)
                screen.on_leave()
        self.assertIn("terminal chess for two | v", self.frame(menu.MainMenu(ctx), ctx))

    def test_colour_highlights_the_selection(self) -> None:
        ctx = self.make_ctx(theme=ui.Theme(unicode=True, color=True))
        frame = menu.compose(menu.MainMenu(ctx), ctx.style, 100, 30)[0]
        self.assertIn(f"48;5;{menu.SELECT_BG}", frame)

    def test_theme_comes_from_settings_and_flags(self) -> None:
        settings = config.Settings(piece_style="ascii", colors=False)
        ctx = self.make_ctx(settings, theme=None)
        self.assertFalse(ctx.theme.unicode)
        self.assertFalse(ctx.theme.color)
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}):
            ctx = self.make_ctx(config.Settings(), {"no_color": True}, theme=None)
            self.assertFalse(ctx.theme.color)
            ctx = self.make_ctx(config.Settings(), {}, theme=None)
            self.assertTrue(ctx.theme.color)
        ctx = self.make_ctx(config.Settings(), {"ascii": True}, theme=None)
        self.assertFalse(ctx.theme.unicode)

    def test_terminal_too_small(self) -> None:
        ctx = self.make_ctx()
        frame, cursor = menu.compose(menu.MainMenu(ctx), ctx.style, 39, 20)
        self.assertIn("Terminal too small", ui.strip_ansi(frame).replace("\n", " "))
        self.assertIsNone(cursor)
        frame, _ = menu.compose(menu.SettingsScreen(ctx), ctx.style, 80, 13)
        self.assertIn("Terminal too small", ui.strip_ansi(frame).replace("\n", " "))
        for width, height in ((40, 14), (60, 20), (80, 24), (200, 60)):
            with self.subTest(size=(width, height)):
                for screen in (menu.MainMenu(ctx), menu.SettingsScreen(ctx), menu.HelpScreen(ctx)):
                    frame, cursor = menu.compose(screen, ctx.style, width, height)
                    lines = frame.split("\n")
                    self.assertEqual(len(lines), height)
                    self.assertTrue(all(ui.visible_len(line) <= width for line in lines))
                    self.assertNotIn("Terminal too small", frame)
                    if cursor is not None:
                        self.assertTrue(0 <= cursor[0] < height and 0 <= cursor[1] < width)
        self.assertIn("Your IP", ui.strip_ansi(menu.compose(menu.MainMenu(ctx), ctx.style, 40, 14)[0]))


# -- the app loop -----------------------------------------------------------------------------


class AppTests(MenuTestCase):
    def test_redraws_only_when_something_changes(self) -> None:
        ctx = self.make_ctx()
        _app, reader, display = self.run_app(ctx, [None, None, None, term.DOWN, None, "q"])
        texts = [frame for frame, _cursor, _size in display.frames]
        self.assertEqual(len(texts), 2)  # the first frame, then the moved selection
        self.assertNotEqual(texts[0], texts[1])

    def test_resize_redraws(self) -> None:
        current = [(100, 30)]

        def resize(width: int, height: int) -> Callable[[], None]:
            def apply() -> None:
                current[0] = (width, height)
            return apply

        ctx = self.make_ctx(size_fn=lambda: current[0])
        _app, _reader, display = self.run_app(ctx, [None, resize(60, 20), None, resize(30, 10), None, "q"])
        drawn_sizes = [frame_size for _frame, _cursor, frame_size in display.frames]
        self.assertEqual(drawn_sizes, [(100, 30), (60, 20), (30, 10)])
        self.assertIn("Terminal too small", display.last.replace("\n", " "))

    def test_screen_stack_and_end_of_input(self) -> None:
        ctx = self.make_ctx()
        app, _reader, display = self.run_app(ctx, ["5", term.ESCAPE, "4", term.CTRL_C, "2"])
        # Help, back, Settings, back, Join, then the input ends: the app stops and leaves every screen.
        self.assertFalse(app.running)
        self.assertEqual(app.stack, [])
        self.assertTrue(self.discoveries[0].started and self.discoveries[0].stopped)
        self.assertIn("Join a game", display.last)

    def test_game_runs_and_returns_to_the_menu_with_its_result(self) -> None:
        ctx = self.make_ctx()
        app, _reader, display = self.run_app(ctx, ["3", term.ENTER, term.ENTER, term.ENTER, None, "q"])
        self.assertEqual(len(self.played), 1)
        request = self.played[0]
        self.assertEqual(request.mode, "local")
        self.assertIsNone(request.time_control)
        self.assertEqual(app.games, 1)
        self.assertIn("Result: test game over", display.last)
        self.assertIn("Host a game", display.last)

    def test_a_crashing_game_returns_to_the_menu(self) -> None:
        def broken(request: menu.GameRequest) -> List[menu.Message]:
            raise ValueError("boom")

        ctx = self.make_ctx(play=broken)
        _app, _reader, display = self.run_app(ctx, ["3", term.DOWN, term.DOWN, term.ENTER, None, "q"])
        self.assertIn("The game stopped because of an error: ValueError: boom", display.last)


# -- host -------------------------------------------------------------------------------------


class HostTests(MenuTestCase):
    def test_form_cycles_choices(self) -> None:
        ctx = self.make_ctx()
        screen = menu.HostScreen(ctx)
        self.assertIs(screen.current(), screen.color)
        self.assertEqual(screen.color.value, "white")
        self.press(screen, term.RIGHT)
        self.assertEqual(screen.color.value, "black")
        self.press(screen, term.RIGHT, " ")
        self.assertEqual(screen.color.value, "white")  # wraps
        self.press(screen, term.LEFT)
        self.assertEqual(screen.color.value, "random")
        self.press(screen, term.DOWN)
        self.assertIs(screen.current(), screen.time)
        self.assertEqual(screen.time.value, "")
        seen = []
        for _ in menu.TIME_PRESETS:
            seen.append(screen.time.value)
            self.press(screen, term.RIGHT)
        self.assertEqual(seen, ["", "1+0", "3+2", "5+3", "10+5", "15+10", "30+0", menu.CUSTOM])
        self.assertEqual(screen.time.value, "")
        self.assertNotIn(screen.custom, screen.visible_fields())  # only for Custom…
        self.press(screen, term.DOWN)
        self.assertIs(screen.current(), screen.port)
        self.press(screen, term.DOWN)
        self.assertIs(screen.current(), screen.start)
        self.press(screen, term.DOWN)
        self.assertIs(screen.current(), screen.color)  # wraps around
        self.assertEqual(self.press(screen, "q").kind, "pop")

    def test_presets_start_hosting_with_that_time_control(self) -> None:
        ctx = self.make_ctx()
        screen = menu.HostScreen(ctx)
        self.press(screen, term.DOWN, term.RIGHT, term.RIGHT, term.RIGHT)  # 5+3
        self.press(screen, term.UP, term.LEFT)  # colour: Left from White wraps to Random
        action = self.press(screen, term.DOWN, term.DOWN, term.DOWN, term.ENTER)
        self.assertEqual(action.kind, "push")
        waiting = action.screen
        self.assertIsInstance(waiting, menu.WaitingScreen)
        self.assertEqual(waiting.color, "random")
        self.assertEqual(waiting.time_control, TimeControl(300000, 3000))
        self.assertEqual(waiting.port, 5555)
        waiting.on_leave()

    def test_custom_time_is_validated(self) -> None:
        ctx = self.make_ctx()
        screen = menu.HostScreen(ctx)
        screen.time.select(menu.CUSTOM)
        self.assertIn(screen.custom, screen.visible_fields())
        self.assertIsNone(screen.submit())  # empty custom value
        self.assertIs(screen.current(), screen.custom)
        self.assertIn("Type a time control", screen.custom.error)
        self.press(screen, *typed("abc", enter=False))
        self.assertEqual(screen.custom.value, "")  # letters cannot be typed in the box
        self.press(screen, *typed("99999", enter=False))
        self.assertIsNone(screen.submit())
        self.assertTrue(screen.custom.error)
        self.assertIn("✗", self.frame(screen, ctx))
        self.press(screen, term.CTRL_U, *typed("7+2", enter=False))
        self.assertEqual(screen.custom.error, "")
        action = screen.submit()
        self.assertEqual(action.screen.time_control, TimeControl(420000, 2000))
        action.screen.on_leave()

    def test_port_is_validated(self) -> None:
        ctx = self.make_ctx()
        screen = menu.HostScreen(ctx)
        screen.focus(screen.port)
        self.press(screen, term.BACKSPACE, term.BACKSPACE, term.BACKSPACE, term.BACKSPACE, "0")
        self.assertIsNone(screen.submit())
        self.assertIn("1 to 65535", screen.port.error)
        self.press(screen, term.BACKSPACE, *typed("6001", enter=False))
        action = screen.submit()
        self.assertEqual(action.screen.port, 6001)
        self.assertEqual(self.servers[-1].port, 6001)
        action.screen.on_leave()

    def test_saved_settings_are_the_defaults(self) -> None:
        ctx = self.make_ctx(config.Settings(host_color="black", time_control="7+2", port=6100))
        screen = menu.HostScreen(ctx)
        self.assertEqual(screen.color.value, "black")
        self.assertEqual(screen.time.value, menu.CUSTOM)
        self.assertEqual(screen.custom.value, "7+2")
        self.assertEqual(screen.port.value, "6100")
        ctx = self.make_ctx(config.Settings(time_control="10+5"))
        self.assertEqual(menu.HostScreen(ctx).time.value, "10+5")


class WaitingTests(MenuTestCase):
    def waiting(self, **kwargs: Any) -> Tuple[menu.MenuContext, menu.WaitingScreen]:
        ctx = self.make_ctx(**kwargs)
        return ctx, menu.WaitingScreen(ctx, "white", TimeControl(300000, 3000), 5555)

    def test_waiting_screen_contents(self) -> None:
        ctx, screen = self.waiting()
        text = self.frame(screen, ctx)
        self.assertIn("Waiting for an opponent", text)
        self.assertIn("192.168.1.23", text)
        self.assertIn("wlan0", text)
        self.assertIn("eth0", text)
        self.assertIn("On the other laptop choose Join, or run:", text)
        self.assertIn(" join 192.168.1.23", text)
        self.assertIn("Network search: on", text)
        self.assertIn("sudo firewall-cmd --add-port=5555/tcp --add-port=5556/udp", text)
        self.assertIn("5 min + 3 s per move", text)
        self.assertTrue(self.responders[0].started)
        before = text.split("\n")[3]
        self.clock.advance(0.1)
        self.assertNotEqual(self.frame(screen, ctx).split("\n")[3], before)  # the spinner turns
        screen.on_leave()

    def test_firewall_hints(self) -> None:
        self.assertEqual(menu.firewall_command("ufw", 5555), "sudo ufw allow 5555/tcp && sudo ufw allow 5556/udp")
        self.assertEqual(menu.firewall_command("firewalld", 6000),
                         "sudo firewall-cmd --add-port=6000/tcp --add-port=5556/udp")
        self.assertEqual(menu.firewall_command(None, 5555), "")
        self.assertIn("TCP 5555 and UDP 5556", menu.firewall_hint(None, 5555)[0])
        ctx, screen = self.waiting(firewall=lambda: "ufw")
        self.assertIn("sudo ufw allow 5555/tcp && sudo ufw allow 5556/udp", self.frame(screen, ctx))
        screen.on_leave()

    def test_detect_firewall(self) -> None:
        def detect(paths: Sequence[str] = (), ufw_conf: str = "", tools: Sequence[str] = (),
                   platform: str = "linux") -> Optional[str]:
            return menu.detect_firewall(platform, exists=lambda path: path in paths,
                                        read_text=lambda path: ufw_conf,
                                        which=lambda tool: f"/usr/bin/{tool}" if tool in tools else None)

        self.assertEqual(detect(paths=["/run/firewalld"]), "firewalld")
        self.assertEqual(detect(ufw_conf="# comment\nENABLED=yes\n"), "ufw")
        self.assertEqual(detect(ufw_conf="ENABLED=no\n", tools=["firewall-cmd"]), "firewalld")
        self.assertEqual(detect(tools=["ufw"]), "ufw")
        self.assertIsNone(detect())
        self.assertIsNone(detect(paths=["/run/firewalld"], platform="darwin"))

    def test_escape_cancels_and_closes_server_and_responder(self) -> None:
        ctx = self.make_ctx()
        app, _reader, display = self.run_app(ctx, ["1", term.DOWN, term.DOWN, term.DOWN, term.ENTER, None,
                                                   term.ESCAPE, None, term.ESCAPE, None, "q"])
        self.assertEqual(len(self.servers), 1)
        self.assertTrue(self.servers[0].closed)
        self.assertTrue(self.responders[0].stopped)
        self.assertGreater(self.servers[0].accepts, 0)
        self.assertEqual(self.played, [])

    def test_connection_handshake_then_game(self) -> None:
        ctx, screen = self.waiting()
        self.assertIsNone(screen.tick())
        conn = FakeConn()
        self.servers[0].incoming.append(conn)
        # With background work inline, the handshake finishes during this tick; the next tick starts the game.
        self.assertIsNone(screen.tick())
        self.assertTrue(screen.connecting)
        self.assertIn("Connecting", self.frame(screen, ctx))
        action = screen.tick()
        self.assertEqual(action.kind, "game")
        request = action.request
        self.assertIs(request.conn, conn)
        self.assertEqual((request.mode, request.my_color, request.opponent_name), ("network", "w", "Bob"))
        self.assertEqual(request.time_control, TimeControl(300000, 3000))
        handshake_conn, name, color, time_control, fen = self.handshakes[0]
        self.assertIs(handshake_conn, conn)
        self.assertEqual((color, time_control, fen), ("white", TimeControl(300000, 3000), STARTING_FEN))
        self.assertTrue(self.servers[0].closed and self.responders[0].stopped)  # no more players accepted
        self.assertFalse(conn.closed)  # the game owns it now
        screen.on_leave()
        self.assertFalse(conn.closed)

    def test_whole_host_flow_in_the_app(self) -> None:
        ctx = self.make_ctx()

        def player_arrives() -> None:
            self.servers[0].incoming.append(FakeConn())
            return None

        _app, _reader, display = self.run_app(ctx, ["1", term.DOWN, term.DOWN, term.DOWN, term.ENTER,
                                                    player_arrives, None, None, None, "q"])
        self.assertEqual(len(self.played), 1)
        self.assertEqual(self.played[0].opponent_name, "Bob")
        self.assertTrue(self.played[0].conn.closed)  # closed by the app after the game
        self.assertIn("Result: test game over", display.last)

    def test_failed_handshake_keeps_waiting(self) -> None:
        self.server_handshake_result = net.HandshakeError("Protocol version mismatch")
        ctx, screen = self.waiting()
        conn = FakeConn()
        self.servers[0].incoming.append(conn)
        screen.tick()
        self.assertIsNone(screen.tick())
        self.assertTrue(conn.closed)
        self.assertFalse(screen.connecting)
        self.assertIn("Protocol version mismatch", flat(self.frame(screen, ctx)))
        self.assertIn("Still waiting", flat(self.frame(screen, ctx)))
        self.assertFalse(self.servers[0].closed)
        screen.on_leave()

    def test_leaving_during_the_handshake_closes_the_connection(self) -> None:
        release = threading.Event()

        def slow_handshake(conn: Any, *args: Any, **kwargs: Any) -> Tuple[str, str]:
            release.wait(5)
            return ("Bob", "w")

        ctx = self.make_ctx(threaded=True, server_handshake=slow_handshake)
        screen = menu.WaitingScreen(ctx, "white", None, 5555)
        conn = FakeConn()
        self.servers[0].incoming.append(conn)
        screen.tick()
        self.assertTrue(screen.connecting)
        screen.on_leave()
        self.assertTrue(conn.closed)
        self.assertTrue(self.servers[0].closed and self.responders[0].stopped)
        release.set()

    def test_port_in_use_and_discovery_unavailable(self) -> None:
        def busy(port: int) -> Any:
            raise OSError(98, "Address already in use")

        ctx = self.make_ctx(server_factory=busy)
        screen = menu.WaitingScreen(ctx, "white", None, 5555)
        text = self.frame(screen, ctx)
        self.assertIn("Could not host the game: port 5555 is already in use", text)
        self.assertIn("pick another port, for example 6000", flat(text))
        self.assertNotIn("--port", text)
        self.assertEqual(screen.handle_key(term.ESCAPE).kind, "pop")
        screen.on_leave()
        self.responder_ok = False
        ctx, screen = self.waiting()
        self.assertIn("Network search: off", self.frame(screen, ctx))
        self.assertIsNone(screen.responder)
        screen.on_leave()

    def test_accept_error_is_reported(self) -> None:
        ctx, screen = self.waiting()
        self.servers[0].closed = True  # accept() now raises OSError
        self.assertIsNone(screen.tick())
        self.assertIn("stopped listening for players", self.frame(screen, ctx))
        screen.on_leave()

    def test_port_needing_administrator_rights_gives_menu_advice(self) -> None:
        def denied(port: int) -> Any:
            raise OSError(errno.EACCES, "Permission denied")

        ctx = self.make_ctx(server_factory=denied)
        text = flat(self.frame(menu.WaitingScreen(ctx, "white", None, 80), ctx))
        self.assertIn("Could not host the game: not allowed to use port 80.", text)
        self.assertIn("Ports below 1024 need administrator rights; go back and pick another port, "
                      "for example 5555.", text)
        self.assertNotIn("--port", text)
        text = flat(self.frame(menu.WaitingScreen(ctx, "white", None, 5555), ctx))  # e.g. a reserved port
        self.assertNotIn("below 1024", text)
        self.assertIn("go back and pick another port, for example 6000", text)
        self.assertNotIn("--port", text)

    def test_long_lines_wrap_and_a_short_window_scrolls(self) -> None:
        ctx = self.make_ctx(config.Settings(name="Maximilian Alexander von X"), local_ips=lambda: [IPS[0]])
        screen = menu.WaitingScreen(ctx, "white", None, 5555)
        status = "Network search: on — they will see “Maximilian Alexander” in the Join list (UDP 5556)."
        for width, height in ((80, 24), (50, 16), (40, 14)):
            with self.subTest(size=(width, height)):
                column = min(width - 4, menu.COLUMN_MAX)
                lines = screen.lines(ctx.style, column)
                self.assertTrue(all(ui.visible_len(line) <= column for line in lines), lines)
                self.assertIn(status, flat("\n".join(ui.strip_ansi(line) for line in lines)))
        text = self.frame(screen, ctx, 80, 24)  # everything fits: no scrolling
        self.assertIn(status, flat(text))
        self.assertNotIn("scroll", text)
        self.assertIn("sudo firewall-cmd --add-port=5555/tcp --add-port=5556/udp", text)
        top = self.frame(screen, ctx, 50, 16)  # too short: the footer says how to see the rest
        self.assertIn("Waiting for an opponent", top)
        self.assertIn("Lines 1–", top)
        self.assertIn("↑↓ scroll · Esc cancel", top)
        self.assertNotIn("firewall-cmd", top)
        self.press(screen, term.END)
        bottom = [line.strip() for line in self.frame(screen, ctx, 50, 16).split("\n")]
        # Each port gets a complete command of its own when the combined one does not fit.
        self.assertIn("sudo firewall-cmd --add-port=5555/tcp", bottom)
        self.assertIn("sudo firewall-cmd --add-port=5556/udp", bottom)
        self.press(screen, term.HOME)
        self.assertEqual(screen.offset, 0)
        self.press(screen, term.DOWN, "j")
        self.assertEqual(screen.offset, 2)
        self.assertIsNone(self.press(screen, term.PGDN))
        self.assertEqual(self.press(screen, term.ESCAPE).kind, "pop")
        screen.on_leave()

    def test_ufw_commands_are_split_when_narrow(self) -> None:
        self.assertEqual(menu.firewall_commands("ufw", 6000), ["sudo ufw allow 6000/tcp", "sudo ufw allow 5556/udp"])
        self.assertEqual(menu.firewall_commands(None, 6000), [])
        ctx, screen = self.waiting(firewall=lambda: "ufw")
        lines = [ui.strip_ansi(line).strip() for line in screen.lines(ctx.style, 46)]
        self.assertIn("sudo ufw allow 5555/tcp", lines)
        self.assertIn("sudo ufw allow 5556/udp", lines)
        ctx, screen = self.waiting(firewall=lambda: None)
        lines = [ui.strip_ansi(line) for line in screen.lines(ctx.style, 46)]
        self.assertTrue(lines[-1].strip())  # no empty command line when the firewall is unknown
        self.assertNotIn("sudo", " ".join(lines))

    def test_leaving_does_not_wait_for_the_search_responder(self) -> None:
        release = threading.Event()
        order: List[str] = []

        class SlowResponder(FakeResponder):
            def stop(self) -> None:  # like DiscoveryResponder.stop(): waits for its thread
                release.wait(5)
                self.stopped = True
                order.append("responder stopped")

        def responder_factory(name: str, port: int) -> FakeResponder:
            responder = SlowResponder(name, port)
            self.responders.append(responder)
            return responder

        def server_factory(port: int) -> FakeServer:
            order.append(f"listen {port}")
            server = FakeServer(port)
            self.servers.append(server)
            return server

        ctx = self.make_ctx(threaded=True, responder_factory=responder_factory, server_factory=server_factory)
        self.addCleanup(ctx.wait_for_stops, 5)
        self.addCleanup(release.set)
        screen = menu.WaitingScreen(ctx, "white", None, 5555)
        started = time.monotonic()
        screen.on_leave()
        self.assertLess(time.monotonic() - started, 2.0)  # (the responder would hold it for 5 s)
        self.assertTrue(self.servers[0].closed)  # the game port is free at once
        self.assertFalse(self.responders[0].stopped)
        threading.Timer(0.2, release.set).start()
        again = menu.WaitingScreen(ctx, "white", None, 5555)  # hosting again waits for the old responder
        self.assertEqual(order, ["listen 5555", "responder stopped", "listen 5555"])
        again.on_leave()


# -- join -------------------------------------------------------------------------------------


class JoinTests(MenuTestCase):
    HOSTS = [net.HostInfo("Alice", "192.168.1.20", 5555), net.HostInfo("devesh", "127.0.0.1", 6000)]

    def join(self, **kwargs: Any) -> Tuple[menu.MenuContext, menu.JoinScreen]:
        ctx = self.make_ctx(**kwargs)
        return ctx, menu.JoinScreen(ctx)

    def test_list_from_discovery(self) -> None:
        self.hosts = list(self.HOSTS)
        ctx, screen = self.join()
        self.assertTrue(self.discoveries[0].started)
        text = self.frame(screen, ctx)
        self.assertIn("Alice", text)
        self.assertIn("192.168.1.20:5555", text)
        self.assertIn("127.0.0.1:6000  (this computer)", text)
        self.assertIn("2 found", text)
        self.assertIn("Enter address manually", text)
        self.assertEqual(screen.selected.target, ("192.168.1.20", 5555))
        self.press(screen, term.DOWN)
        self.assertEqual(screen.selected.target, ("127.0.0.1", 6000))
        self.assertEqual(self.press(screen, term.ESCAPE).kind, "pop")
        screen.on_leave()
        self.assertTrue(self.discoveries[0].stopped)

    def test_hosts_appearing_later_and_loopback_duplicates(self) -> None:
        self.rounds = 0
        ctx, screen = self.join()
        self.assertIn("Looking for games", self.frame(screen, ctx))
        discovery = self.discoveries[0]
        discovery.hosts = [net.HostInfo("Alice", "127.0.0.1", 5555), net.HostInfo("Alice", "192.168.1.20", 5555)]
        discovery.rounds = 1
        screen.tick()
        text = self.frame(screen, ctx)
        self.assertEqual(text.count("Alice"), 1)  # the 127.0.0.1 echo of a LAN answer is hidden
        self.assertEqual(screen.selected.target, ("192.168.1.20", 5555))
        screen.on_leave()

    def test_empty_state_is_helpful(self) -> None:
        ctx, screen = self.join()
        text = self.frame(screen, ctx)
        self.assertIn("No games found yet.", text)
        self.assertIn("UDP 5556", text)
        self.assertIn("same Wi-Fi", text)
        self.assertIn("Enter address manually", text)
        self.assertEqual(screen.selected.kind, "manual")
        self.press(screen, "r")
        self.assertEqual(self.discoveries[0].rescans, 1)
        self.assertIn("Looking for games", self.frame(screen, ctx))
        screen.on_leave()

    def test_join_a_listed_game(self) -> None:
        self.hosts = list(self.HOSTS)
        ctx, screen = self.join()
        self.press(screen, term.ENTER)
        self.assertEqual(self.connects, [("192.168.1.20", 5555)])
        self.assertEqual(screen.mode, "connecting")
        self.assertIn("Connecting to 192.168.1.20:5555", self.frame(screen, ctx))
        action = screen.tick()
        self.assertEqual(action.kind, "game")
        request = action.request
        self.assertEqual((request.mode, request.my_color, request.opponent_name), ("network", "b", "Alice"))
        self.assertEqual(request.time_control, TimeControl(300000, 3000))
        self.assertFalse(request.conn.closed)
        self.assertEqual(config.load(self.config_path).recent_hosts, ["192.168.1.20:5555"])
        self.assertEqual(ctx.settings.recent_hosts, ["192.168.1.20:5555"])
        screen.on_leave()

    def test_manual_address_invalid_then_valid(self) -> None:
        ctx, screen = self.join()
        self.press(screen, "m")
        self.assertEqual(screen.mode, "manual")
        self.press(screen, term.ENTER)
        self.assertIn("Type the IP address", screen.manual.error)
        self.press(screen, *typed("10.0.0.5:99999"))
        self.assertIn("Invalid port", screen.manual.error)
        text = self.frame(screen, ctx)
        self.assertIn("Invalid port", text)
        frame, cursor = menu.compose(screen, ctx.style, 100, 30)
        self.assertIsNotNone(cursor)  # the caret is in the address box
        self.assertEqual(self.connects, [])
        self.press(screen, term.CTRL_U, *typed("q.example:6000"))  # q is typed, not "back"
        self.assertEqual(self.connects, [("q.example", 6000)])
        screen.on_leave()

    def test_manual_address_uses_the_saved_port_and_escape_returns_to_the_list(self) -> None:
        ctx, screen = self.join(settings=config.Settings(port=6200))
        self.press(screen, term.END, term.UP, term.ENTER)  # "Enter address manually…"
        self.assertEqual(screen.mode, "manual")
        self.press(screen, *typed("10.0.0.9", enter=False), term.ESCAPE)
        self.assertEqual(screen.mode, "list")
        self.press(screen, term.ENTER, term.ENTER)
        self.assertEqual(self.connects, [("10.0.0.9", 6200)])
        screen.on_leave()

    def test_connect_error_then_retry(self) -> None:
        self.hosts = list(self.HOSTS)
        self.connect_errors = [ConnectionRefusedError(111, "Connection refused")]
        ctx, screen = self.join()
        self.press(screen, term.ENTER)
        self.assertIsNone(screen.tick())
        self.assertEqual(screen.mode, "error")
        text = self.frame(screen, ctx)
        self.assertIn("Could not join 192.168.1.20:5555: connection refused", text)
        self.assertIn("try again", text)
        self.assertEqual(config.load(self.config_path).recent_hosts, [])
        self.press(screen, term.ENTER)  # retry
        self.assertEqual(self.connects, [("192.168.1.20", 5555)] * 2)
        action = screen.tick()
        self.assertEqual(action.kind, "game")
        screen.on_leave()

    def test_timeout_error_mentions_the_firewall_and_escape_goes_back(self) -> None:
        import socket

        self.hosts = list(self.HOSTS)
        self.connect_errors = [socket.timeout("timed out")]
        ctx, screen = self.join()
        self.press(screen, term.ENTER)
        screen.tick()
        text = self.frame(screen, ctx)
        self.assertIn("timed out", text)
        self.assertIn("firewall", text)
        self.press(screen, term.ESCAPE)
        self.assertEqual(screen.mode, "list")
        screen.on_leave()

    def test_bad_handshake_is_an_error(self) -> None:
        self.hosts = list(self.HOSTS)

        def refuse(conn: Any, name: str, timeout: float = 10.0) -> Dict[str, Any]:
            raise net.HandshakeError("The host refused: game already started")

        ctx, screen = self.join(client_handshake=refuse)
        self.press(screen, term.ENTER)
        screen.tick()
        self.assertIn("game already started", self.frame(screen, ctx))
        screen.on_leave()

    def test_cancel_while_connecting_closes_a_late_connection(self) -> None:
        self.hosts = list(self.HOSTS)
        release = threading.Event()
        conns: List[FakeConn] = []

        def slow_connect(host: str, port: int, timeout: float = 10.0) -> FakeConn:
            release.wait(5)
            conn = FakeConn()
            conns.append(conn)
            return conn

        ctx, screen = self.join(threaded=True, connect=slow_connect)
        self.press(screen, term.ENTER)
        self.assertEqual(screen.mode, "connecting")
        self.press(screen, term.ESCAPE)
        self.assertEqual(screen.mode, "list")
        release.set()
        deadline = time.monotonic() + 5
        while not (conns and conns[0].closed) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(conns and conns[0].closed)
        screen.on_leave()

    def test_recent_hosts_are_listed_and_joinable(self) -> None:
        settings = config.Settings(recent_hosts=["10.0.0.9:7000", "192.168.1.20:5555"])
        self.hosts = [self.HOSTS[0]]
        ctx, screen = self.join(settings=settings)
        text = self.frame(screen, ctx)
        self.assertIn("Recent", text)
        self.assertIn("10.0.0.9:7000", text)
        self.assertEqual(text.count("192.168.1.20:5555"), 1)  # already listed as found
        kinds = [item.kind for item in screen.items]
        self.assertEqual(kinds, ["host", "recent", "manual", "rescan"])
        self.press(screen, term.DOWN, term.ENTER)
        screen.tick()
        self.assertEqual(self.connects, [("10.0.0.9", 7000)])
        self.assertEqual(config.load(self.config_path).recent_hosts, ["10.0.0.9:7000"])
        screen.on_leave()

    def test_discovery_worker_merges_rounds_and_forgets_old_hosts(self) -> None:
        clock = FakeClock()
        rounds = [[net.HostInfo("A", "10.0.0.1", 5555)], [net.HostInfo("B", "10.0.0.2", 5555)]]
        calls: List[Tuple[float, Tuple[str, ...]]] = []
        done = threading.Event()

        def discover(timeout: float, extra_targets: Sequence[str] = ()) -> List[net.HostInfo]:
            calls.append((timeout, tuple(extra_targets)))
            if rounds:
                return rounds.pop(0)
            done.set()
            time.sleep(0.01)
            return []

        worker = menu.DiscoveryWorker(discover, clock=clock, round_time=0.5, keep=4.0)
        worker.start()
        self.addCleanup(worker.stop, 5)
        self.assertTrue(done.wait(5))
        hosts, count = worker.snapshot()
        self.assertEqual([host.name for host in hosts], ["A", "B"])
        self.assertGreaterEqual(count, 2)
        self.assertEqual(calls[0], (0.5, ("127.0.0.1", "127.255.255.255")))
        clock.advance(10)
        self.assertEqual(worker.snapshot()[0], [])  # not seen for too long
        worker.rescan()
        self.assertEqual(worker.snapshot(), ([], 0))
        worker.stop(wait=5)
        self.assertFalse(worker.running)

    def test_discovery_errors_are_shown(self) -> None:
        ctx, screen = self.join()
        self.discoveries[0].error = "Network is down"
        self.assertIn("Network search problem: Network is down", self.frame(screen, ctx))
        screen.on_leave()

    def test_discovery_worker_stop_does_not_wait_for_the_round(self) -> None:
        searching, release = threading.Event(), threading.Event()

        def discover(timeout: float, extra_targets: Sequence[str] = ()) -> List[net.HostInfo]:
            searching.set()
            release.wait(5)
            return []

        class JoinSpy:
            def __init__(self, thread: threading.Thread) -> None:
                self.thread, self.joins = thread, []

            def join(self, timeout: Optional[float] = None) -> None:
                self.joins.append(timeout)
                self.thread.join(timeout)

            def is_alive(self) -> bool:
                return self.thread.is_alive()

        worker = menu.DiscoveryWorker(discover, round_time=0.5)
        worker.start()
        thread = worker._thread
        spy = worker._thread = JoinSpy(thread)
        self.addCleanup(release.set)
        self.assertTrue(searching.wait(5))
        worker.stop()
        self.assertEqual(spy.joins, [])  # leaving the Join screen never waits for the search thread
        self.assertFalse(worker.running)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())  # the round ended and the thread stopped by itself

    def test_games_hosted_on_this_computer_are_probed_with_the_loopback_broadcast(self) -> None:
        self.assertEqual(menu.DiscoveryWorker().extra_targets, ("127.0.0.1", "127.255.255.255"))


# -- local, settings, help ------------------------------------------------------------------


class LocalTests(MenuTestCase):
    def test_time_picker_and_start(self) -> None:
        ctx = self.make_ctx(config.Settings(flip_local=False))
        screen = menu.LocalScreen(ctx)
        self.assertEqual(screen.flip.value, False)
        self.press(screen, term.RIGHT, term.RIGHT)  # 3+2
        action = self.press(screen, term.DOWN, term.LEFT, term.DOWN, term.ENTER)
        self.assertEqual(action.kind, "game")
        self.assertEqual(action.request.mode, "local")
        self.assertEqual(action.request.time_control, TimeControl(180000, 2000))
        self.assertTrue(action.request.flip)

    def test_custom_time_in_local_game(self) -> None:
        ctx = self.make_ctx()
        screen = menu.LocalScreen(ctx)
        self.press(screen, term.LEFT)  # Custom… (wraps backwards)
        self.assertEqual(screen.time.value, menu.CUSTOM)
        self.press(screen, term.DOWN, *typed("0.5+1", enter=False))
        action = screen.submit()
        self.assertEqual(action.request.time_control, TimeControl(30000, 1000))


class SettingsTests(MenuTestCase):
    def test_edit_save_and_reload(self) -> None:
        ctx = self.make_ctx()
        screen = menu.SettingsScreen(ctx)
        self.press(screen, *typed("Alice", enter=False))
        self.press(screen, term.DOWN, term.RIGHT, term.RIGHT, term.RIGHT)  # time 5+3
        self.press(screen, term.DOWN, term.RIGHT)  # host colour black
        self.press(screen, term.DOWN, term.BACKSPACE, "6")  # port 5556
        self.press(screen, term.DOWN, term.RIGHT)  # letters
        self.press(screen, term.DOWN, term.RIGHT)  # colours off
        self.press(screen, term.DOWN, term.RIGHT)  # flip off
        self.press(screen, term.DOWN, term.RIGHT)  # autosave off
        self.press(screen, term.DOWN, *typed("/tmp/games", enter=False))
        action = self.press(screen, term.DOWN, term.ENTER)
        self.assertEqual(action.kind, "pop")
        self.assertEqual(action.message, (("ok", "Settings saved."),))
        saved = config.load(self.config_path)
        self.assertEqual(saved, config.Settings(name="Alice", time_control="5+3", host_color="black", port=5556,
                                                piece_style="ascii", colors=False, flip_local=False,
                                                autosave=False, pgn_dir="/tmp/games"))
        self.assertEqual(ctx.settings, saved)
        options = ctx.options()
        self.assertEqual((options.ascii, options.no_color, options.no_save, options.pgn_dir),
                         (True, True, True, "/tmp/games"))
        # Persisted: a new menu starts from them.
        ctx2 = self.make_ctx(theme=None)
        self.assertFalse(ctx2.theme.unicode)
        host = menu.HostScreen(ctx2)
        self.assertEqual((host.color.value, host.time.value, host.port.value), ("black", "5+3", "5556"))
        self.assertEqual(ctx2.player_name(), "Alice")

    def test_settings_screen_shows_saved_values_and_cancel_discards(self) -> None:
        config.save(config.Settings(name="Zed", time_control="7+2", recent_hosts=["10.0.0.1:5555"]),
                    self.config_path)
        ctx = self.make_ctx()
        screen = menu.SettingsScreen(ctx)
        self.assertEqual(screen.name.value, "Zed")
        self.assertEqual((screen.time.value, screen.custom.value), (menu.CUSTOM, "7+2"))
        self.press(screen, term.CTRL_U, *typed("Other", enter=False))
        self.assertEqual(self.press(screen, term.ESCAPE).kind, "pop")
        self.assertEqual(config.load(self.config_path).name, "Zed")
        screen = menu.SettingsScreen(ctx)
        self.press(screen, term.CTRL_U)
        screen.save()
        saved = config.load(self.config_path)
        self.assertEqual(saved.name, "")  # empty: the login name
        self.assertEqual(saved.recent_hosts, ["10.0.0.1:5555"])  # kept

    def test_invalid_values_are_not_saved(self) -> None:
        ctx = self.make_ctx()
        screen = menu.SettingsScreen(ctx)
        screen.focus(screen.port)
        self.press(screen, term.CTRL_U)
        self.assertIsNone(screen.save())
        self.assertIs(screen.current(), screen.port)
        self.assertFalse(os.path.exists(self.config_path))
        screen.time.select(menu.CUSTOM)
        screen.custom.value = "abc"
        self.assertIsNone(screen.save())
        self.assertIs(screen.current(), screen.custom)

    def test_save_failure_is_reported(self) -> None:
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as handle:
            handle.write("x")
        ctx = self.make_ctx()
        ctx.config_path = os.path.join(blocker, "config.json")
        screen = menu.SettingsScreen(ctx)
        self.assertIsNone(screen.save())
        self.assertIn("Could not save the settings", self.frame(screen, ctx))

    def test_command_line_overrides_are_shown(self) -> None:
        ctx = self.make_ctx(explicit={"ascii": True, "no_save": True})
        self.assertIn("--ascii --no-save", self.frame(menu.SettingsScreen(ctx), ctx))


class HelpTests(MenuTestCase):
    def test_scrolls(self) -> None:
        ctx = self.make_ctx()
        screen = menu.HelpScreen(ctx)
        first = self.frame(screen, ctx, 80, 24)
        self.assertIn("Starting a game", first)
        self.assertIn("Lines 1", first)
        self.press(screen, term.PGDN)
        self.assertGreater(screen.offset, 0)
        self.press(screen, term.END)
        last = self.frame(screen, ctx, 80, 24)
        self.assertIn("Settings are stored in", last)
        self.press(screen, term.HOME)
        self.assertEqual(screen.offset, 0)
        self.press(screen, "j")
        self.assertEqual(screen.offset, 1)
        self.assertEqual(self.press(screen, term.ESCAPE).kind, "pop")

    def test_help_mentions_firewall_commands(self) -> None:
        ctx = self.make_ctx()
        text = " ".join(line for _heading, lines in menu.help_sections(ctx) for line in lines)
        self.assertIn("sudo firewall-cmd --add-port=5555/tcp --add-port=5556/udp", text)
        self.assertIn("sudo ufw allow 5555/tcp && sudo ufw allow 5556/udp", text)
        self.assertIn("/quit", text)


# -- tasks and context --------------------------------------------------------------------------


class TaskTests(unittest.TestCase):
    def test_inline_task(self) -> None:
        task = menu.Task(lambda: 42, threaded=False)
        self.assertTrue(task.done)
        self.assertEqual(task.result, 42)
        failing = menu.Task(lambda: 1 / 0, threaded=False)
        self.assertIsInstance(failing.error, ZeroDivisionError)

    def test_cancel_cleans_up_finished_or_late_results(self) -> None:
        cleaned: List[Any] = []
        task = menu.Task(lambda: "conn", threaded=False, on_abandon=cleaned.append)
        task.cancel()
        task.cancel()
        self.assertEqual(cleaned, ["conn"])
        release = threading.Event()
        task = menu.Task(lambda: release.wait(5) and "late", on_abandon=cleaned.append)
        task.cancel()
        release.set()
        deadline = time.monotonic() + 5
        while len(cleaned) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(cleaned, ["conn", "late"])


class ContextTests(MenuTestCase):
    def test_ip_lookup_is_cached_and_refreshed_in_the_background(self) -> None:
        calls: List[int] = []
        release = threading.Event()

        def lookup() -> List[Tuple[str, str]]:
            calls.append(1)
            if len(calls) > 1:
                release.wait(5)
                return [("eth1", "10.1.1.1")]
            return list(IPS)

        ctx = self.make_ctx(local_ips=lookup, threaded=True)
        self.assertEqual(ctx.local_ips(), IPS)
        self.assertEqual(ctx.local_ips(), IPS)
        self.assertEqual(len(calls), 1)
        self.clock.advance(11)
        self.assertEqual(ctx.local_ips(), IPS)  # the slow refresh does not block
        release.set()
        deadline = time.monotonic() + 5
        while ctx.local_ips() == IPS and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(ctx.local_ips(), [("eth1", "10.1.1.1")])

    def test_ip_lookup_failure(self) -> None:
        def broken() -> List[Tuple[str, str]]:
            raise OSError("no network")

        ctx = self.make_ctx(local_ips=broken)
        self.assertEqual(ctx.local_ips(), [])
        self.assertIn("Your IP: not found", self.frame(menu.MainMenu(ctx), ctx))

    def test_game_finished_messages(self) -> None:
        ctx = self.make_ctx(explicit={"no_save": True})
        session = ctx.make_session(menu.GameRequest(mode="local"))
        self.assertFalse(session.autosave)
        for move in ("f3", "e5", "g4", "Qh4#"):
            session.handle_input(move)
        messages = ctx.game_finished(session)
        self.assertEqual(messages, [("info", "Result: Checkmate — Black wins (0-1)")])
        self.assertEqual(ctx.history, ["Result: Checkmate — Black wins (0-1)"])
        empty = ctx.make_session(menu.GameRequest(mode="local"))
        self.assertEqual(ctx.game_finished(empty), [("info", "You left the game.")])


# -- a real game inside the menu --------------------------------------------------------------


class InTerminalGameTests(MenuTestCase):
    def test_local_game_in_the_menu_then_back(self) -> None:
        reader, display = FakeKeys(), FakeDisplay()
        ctx = self.make_ctx(explicit={"no_save": True})
        ctx.play = lambda request: menu.play_in_terminal(ctx, request, reader, display)
        reader.add("3", term.ENTER, term.ENTER, term.ENTER)  # Local game, Start
        for move in ("f3", "e5", "g4", "Qh4#", "/quit"):
            reader.add(*typed(move))
        reader.add(None, None, "q")
        app = menu.MenuApp(ctx, reader, display, size_fn=lambda: (100, 30))
        self.assertEqual(app.run(), 0)
        texts = [ui.strip_ansi(frame) for frame, _cursor, _size in display.frames]
        self.assertTrue(any("Checkmate" in text for text in texts))
        self.assertIn("Result: Checkmate — Black wins (0-1)", texts[-1])
        self.assertIn("Host a game", texts[-1])
        # The game borrowed the terminal: it never switched raw mode or the alternate screen.
        self.assertEqual((reader.starts, reader.restores, display.enters, display.exits), (0, 0, 0, 0))

    def test_fullscreen_menu_restores_the_terminal(self) -> None:
        reader, display = FakeKeys(["q"]), FakeDisplay()
        ctx = self.make_ctx()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menu.run_fullscreen_menu(ctx, reader, display), 0)
        self.assertEqual((reader.starts, reader.restores, display.enters, display.exits), (1, 1, 1, 1))

        def interrupt() -> str:
            raise KeyboardInterrupt

        reader, display = FakeKeys([interrupt]), FakeDisplay()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menu.run_fullscreen_menu(self.make_ctx(), reader, display), 0)
        self.assertEqual((reader.restores, display.exits), (1, 1))

        def crash() -> str:
            raise RuntimeError("bug")

        reader, display = FakeKeys([crash]), FakeDisplay()
        with self.assertRaises(RuntimeError):
            menu.run_fullscreen_menu(self.make_ctx(), reader, display)
        self.assertEqual((reader.restores, display.exits), (1, 1))

    def test_fullscreen_prints_the_history_after_leaving(self) -> None:
        reader, display = FakeKeys(["3", term.ENTER, term.ENTER, term.ENTER, None, "q"]), FakeDisplay()
        ctx = self.make_ctx()
        ctx.play = lambda request: ctx.history.append("Result: x") or [("info", "Result: x")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            menu.run_fullscreen_menu(ctx, reader, display)
        self.assertEqual(out.getvalue(), "Result: x\n")

    def test_not_a_terminal_falls_back_to_the_text_menu(self) -> None:
        class NoTerminal(FakeKeys):
            def start(self) -> None:
                raise OSError("stdin is not a terminal")

        ctx = self.make_ctx()
        with mock.patch.object(menu, "run_plain_menu", return_value=0) as plain:
            self.assertEqual(menu.run_fullscreen_menu(ctx, NoTerminal(), FakeDisplay()), 0)
        plain.assert_called_once_with(ctx)


# -- keys typed quickly -----------------------------------------------------------------------


class QuickKeyTests(MenuTestCase):
    """An Esc and the next key can arrive in one read; the menu must keep both."""

    def test_parser_reports_esc_and_a_character_only_when_asked(self) -> None:
        self.assertEqual(term.KeyParser().feed("\x1b2"), [])  # the default (games) is unchanged
        parser = term.KeyParser(alt_keys=True)
        self.assertEqual(parser.feed("\x1b2"), ["ALT+2"])
        self.assertEqual(parser.feed("\x1b\x1bq"), ["ESC", "ALT+q"])
        self.assertEqual(parser.feed("\x1b[A\x1bOB"), ["UP", "DOWN"])
        self.assertEqual(parser.feed("\x1b\ufffdx"), ["x"])
        self.assertEqual(parser.feed("\x1b"), [])
        self.assertEqual(parser.feed("j"), ["ALT+j"])  # (split across reads)
        editor = term.LineEditor(None)
        editor.handle_key("ALT+x")  # a game started from the menu ignores them, as before
        self.assertEqual(editor.buffer, "")

    def test_alt_key_is_escape_then_the_key(self) -> None:
        ctx = self.make_ctx()
        # The Waiting screen, then Esc, then "Esc 2" in one read: back to the form, to the main menu, Join.
        _app, _reader, display = self.run_app(ctx, ["1", term.UP, term.ENTER, None, term.ESCAPE, "ALT+2", None])
        self.assertTrue(self.servers[0].closed and self.responders[0].stopped)
        self.assertEqual(len(self.discoveries), 1)
        self.assertIn("LAN Chess › Join a game", display.last)

    def test_fullscreen_menu_reads_alt_keys(self) -> None:
        with mock.patch.object(menu.term, "KeyReader", return_value=FakeKeys(["q"])) as reader_class, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menu.run_fullscreen_menu(self.make_ctx(), display=FakeDisplay()), 0)
        reader_class.assert_called_once_with(alt_keys=True)

    @unittest.skipUnless(hasattr(os, "openpty") and term.termios is not None, "needs a POSIX terminal")
    def test_escape_escape_digit_from_the_waiting_screen_on_a_real_terminal(self) -> None:
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        reader = term.KeyReader(slave, alt_keys=True)
        reader.start()
        self.addCleanup(reader.restore)
        app = menu.MenuApp(self.make_ctx(), reader, FakeDisplay())
        self.addCleanup(app.close)
        deadline = time.monotonic() + 5

        def step_until(kind: type) -> None:
            while not isinstance(app.screen, kind) and time.monotonic() < deadline:
                app.step()
            self.assertIsInstance(app.screen, kind)

        os.write(master, b"1\x1b[A\r")  # Host a game, Up to "Start hosting", Enter
        step_until(menu.WaitingScreen)
        os.write(master, b"\x1b\x1b2")  # Esc, Esc, 2 arriving together (the menu was busy)
        step_until(menu.JoinScreen)


# -- the text menu ----------------------------------------------------------------------------


class PlainMenuTests(MenuTestCase):
    def run_plain(self, script: str, explicit: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Tuple[int, str]:
        ctx = self.make_ctx(explicit=explicit or {"plain": True, "no_save": True}, **kwargs)
        source = term.PlainLineInput(io.StringIO(script))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = menu.run_plain_menu(ctx, source)
        self.ctx = ctx
        return code, out.getvalue()

    def test_quit_and_end_of_input(self) -> None:
        code, out = self.run_plain("6\n")
        self.assertEqual(code, 0)
        for number, label in enumerate(("Host a game", "Join a game", "Local game", "Settings",
                                        "How to play", "Quit"), 1):
            self.assertIn(f"  {number}) {label}", out)
        self.assertIn("Your IP: 192.168.1.23 (wlan0)", out)
        self.assertEqual(self.run_plain("")[0], 0)  # EOF
        code, out = self.run_plain("9\nnonsense\nq\n")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("Please type a number from 1 to 6."), 2)

    def test_help_and_settings(self) -> None:
        code, out = self.run_plain("5\n4\n1\nAlice\n3\nb\n4\n6001\n5\n\n4\n\n6\n")
        self.assertEqual(code, 0)
        self.assertIn("Commands during a game", out)
        self.assertIn("Saved.", out)
        saved = config.load(self.config_path)
        self.assertEqual((saved.name, saved.host_color, saved.port, saved.piece_style),
                         ("Alice", "black", 6001, "ascii"))
        self.assertIn("Your name: Alice", out)
        self.assertIn("Port: 6001", out)

    def test_local_game_returns_to_the_menu(self) -> None:
        code, out = self.run_plain("3\n\nf3\ne5\ng4\nQh4#\n/quit\n6\n")
        self.assertEqual(code, 0)
        self.assertRegex(out, "Checkmate (—|-) Black wins \\(0-1\\)")
        self.assertGreaterEqual(out.count("1) Host a game"), 2)  # the menu came back after the game
        self.assertNotIn("\x1b", out)

    def test_end_of_input_inside_a_game_leaves_the_menu(self) -> None:
        code, out = self.run_plain("l\n5+3\ne4\n")
        self.assertEqual(code, 0)
        self.assertIn("Time control 5+3", out)
        self.assertEqual(out.count("1) Host a game"), 1)

    def test_bad_time_is_asked_again(self) -> None:
        code, out = self.run_plain("3\nabc\n10\n/quit\n6\n")
        self.assertEqual(code, 0)
        self.assertIn("Invalid time control", out)
        self.assertIn("Time control 10+0", out)

    def test_join_shares_the_input(self) -> None:
        prompts: List[str] = []

        def choose_host(theme: Any, scan_time: float, port: int, ask: Callable[[str], Optional[str]]) -> Any:
            prompts.append(ask("Pick: "))
            return None

        with mock.patch.object(cli, "choose_host", side_effect=choose_host):
            code, out = self.run_plain("2\nsomething\n6\n")
        self.assertEqual(code, 0)
        self.assertEqual(prompts, ["something"])
        self.assertIn("No game selected.", out)
        self.assertGreaterEqual(out.count("1) Host a game"), 2)

    def test_ctrl_c_goes_back(self) -> None:
        class Interrupting:
            eof = False

            def __init__(self) -> None:
                self.events = deque([("line", "4"), ("interrupt",), ("interrupt",)])

            def poll(self, timeout: float) -> Optional[tuple]:
                return self.events.popleft() if self.events else ("eof",)

        ctx = self.make_ctx()
        out = io.StringIO()
        self.assertEqual(menu.PlainMenu(ctx, Interrupting(), out).run(), 0)
        self.assertIn("Settings (saved in", out.getvalue())

    def test_ctrl_c_while_searching_or_connecting_goes_back_to_the_menu(self) -> None:
        def interrupt(*args: Any, **kwargs: Any) -> Any:
            raise KeyboardInterrupt

        with mock.patch.object(cli, "choose_host", side_effect=interrupt):
            code, out = self.run_plain("2\n6\n")
        self.assertEqual(code, 0)
        self.assertIn("Stopped searching. No game was played.", out)
        self.assertEqual(out.count("1) Host a game"), 2)  # the menu came back, then 6 quit
        with mock.patch.object(cli, "choose_host", return_value=("10.0.0.9", 5555)), \
                mock.patch.object(cli, "join_host", side_effect=interrupt):
            code, out = self.run_plain("2\n6\n")
        self.assertEqual(code, 0)
        self.assertIn("Stopped connecting. No game was played.", out)
        self.assertEqual(out.count("1) Host a game"), 2)
        with mock.patch.object(cli, "run_local", side_effect=interrupt):  # anywhere else in a choice
            code, out = self.run_plain("3\n\n6\n")
        self.assertEqual(code, 0)
        self.assertIn("Stopped.", out)
        self.assertEqual(out.count("1) Host a game"), 2)

    def test_host_port_errors_point_to_settings(self) -> None:
        for error, expected in ((OSError(errno.EADDRINUSE, "Address already in use"),
                                 "Change the port in Settings, for example to 6000"),
                                (OSError(errno.EACCES, "Permission denied"),
                                 "change the port in Settings, for example to 5555")):
            err = io.StringIO()
            with self.subTest(error=error), mock.patch.object(cli.net, "Server", side_effect=error), \
                    contextlib.redirect_stderr(err):
                code, out = self.run_plain("1\n\n\n6\n", settings=config.Settings(port=80))
            self.assertEqual(code, 0)
            self.assertIn(expected, err.getvalue())
            self.assertNotIn("--port", err.getvalue())
            self.assertEqual(out.count("1) Host a game"), 2)

    def test_ascii_output(self) -> None:
        code, out = self.run_plain("6\n", theme=ui.Theme(unicode=False, color=False))
        self.assertTrue(out.isascii())


# -- entry point ------------------------------------------------------------------------------


class RunMenuTests(MenuTestCase):
    def test_non_terminal_uses_the_text_menu(self) -> None:
        out = io.StringIO()
        with mock.patch.object(menu.sys, "stdin", io.StringIO("6\n")), contextlib.redirect_stdout(out):
            self.assertEqual(menu.run_menu({}, config.Settings(), self.config_path), 0)
        self.assertIn("1) Host a game", out.getvalue())

    def test_plain_flag_or_terminal_choice(self) -> None:
        with mock.patch.object(menu, "run_plain_menu", return_value=0) as plain, \
                mock.patch.object(menu, "run_fullscreen_menu", return_value=0) as full, \
                mock.patch.object(menu.cli, "_isatty", return_value=True), \
                mock.patch.object(menu.term, "enable_ansi", return_value=True):
            menu.run_menu({}, config.Settings(), self.config_path)
            self.assertEqual((plain.call_count, full.call_count), (0, 1))
            menu.run_menu({"plain": True}, config.Settings(), self.config_path)
            self.assertEqual((plain.call_count, full.call_count), (1, 1))
            ctx = full.call_args[0][0]
            self.assertEqual(ctx.config_path, self.config_path)


if __name__ == "__main__":
    unittest.main()

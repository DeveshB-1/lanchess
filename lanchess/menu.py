"""The start menu: what ``lanchess`` shows when it is run without a sub-command.

On a terminal the menu is full-screen: arrow keys and number shortcuts, forms for hosting and local
games, a live list of the games on the network, settings and a help page. With ``--plain``, or when
stdin is not a terminal, it is a numbered text menu instead.

Everything outside the terminal is injectable through ``MenuContext`` (clock, network factories,
discovery, the game runner, the config file), and ``MenuApp`` takes any key source and screen, so
every screen can be tested without a terminal or a network. A game started from the menu returns
to the menu when the players leave it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import textwrap
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import __version__, cli, config, game, net, term, ui
from .engine import STARTING_FEN

__all__ = [
    "MIN_WIDTH", "MIN_HEIGHT", "TIME_PRESETS", "CUSTOM", "Style", "Body", "Action", "GameRequest",
    "Task", "DiscoveryWorker", "MenuContext", "MenuApp", "Screen", "MainMenu", "HostScreen",
    "WaitingScreen", "JoinScreen", "LocalScreen", "SettingsScreen", "HelpScreen", "PlainMenu",
    "compose", "banner_lines", "detect_firewall", "firewall_command", "firewall_hint", "run_menu",
    "run_fullscreen_menu", "run_plain_menu",
]

MIN_WIDTH = 40
MIN_HEIGHT = 14
COLUMN_MAX = 78
LOOPBACK = cli.LOOPBACK
LOOPBACK_TARGETS = cli.LOOPBACK_TARGETS

SELECT_FG, SELECT_BG = 231, 25
ACCENT_FG = 75
KNIGHT_FG = 222
TITLE_FG = 231
NOTICE_FG = 214
OK_FG = ui.TURN_FG
ERROR_FG = ui.ERROR_FG
DIM_FG = ui.DIM_FG

CUSTOM = "custom"
TIME_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("", "Untimed"), ("1+0", "1+0 bullet"), ("3+2", "3+2 blitz"), ("5+3", "5+3 blitz"),
    ("10+5", "10+5 rapid"), ("15+10", "15+10 rapid"), ("30+0", "30+0 classical"), (CUSTOM, "Custom…"),
)
COLOR_CHOICES: Tuple[Tuple[str, str], ...] = (("white", "White"), ("black", "Black"), ("random", "Random"))
ON_OFF: Tuple[Tuple[bool, str], ...] = ((True, "On"), (False, "Off"))
BOARD_SIZE_CHOICES: Tuple[Tuple[str, str], ...] = (
    ("auto", "Auto (biggest that fits)"), ("small", "Small"), ("medium", "Medium"),
    ("large", "Large (drawn pieces)"), ("xl", "Extra large (drawn pieces)"),
)
TIME_CHARS = "0123456789.+| "

Message = Tuple[str, str]  # (kind, text); kind is info, ok, notice or error

# ---------------------------------------------------------------------------------------------
# Text and style helpers
# ---------------------------------------------------------------------------------------------

_ASCII_TABLE = str.maketrans({
    "—": "-", "–": "-", "…": "...", "·": "|", "‹": "<", "›": ">", "◂": "<", "▸": ">", "•": "*",
    "✗": "x", "✓": "+", "─": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "×": "x",
})


class Style:
    """Painting helpers for the menu: ASCII fallbacks for every symbol, colour only if enabled."""

    def __init__(self, theme: ui.Theme) -> None:
        self.theme = theme
        self.unicode = theme.unicode
        self.color = theme.color
        self.updown = "↑↓" if theme.unicode else "Up/Down"
        self.leftright = "←→" if theme.unicode else "Left/Right"
        self.marker = "▸ " if theme.unicode else "> "

    def t(self, text: str) -> str:
        """``text`` with Unicode punctuation replaced in ASCII mode."""
        return text if self.unicode else text.translate(_ASCII_TABLE)

    def p(self, text: str, **style: Any) -> str:
        return self.theme.paint(self.t(text), **style)

    def dim(self, text: str) -> str:
        return self.p(text, fg=DIM_FG)

    def bold(self, text: str) -> str:
        return self.p(text, bold=True)

    def accent(self, text: str) -> str:
        return self.p(text, fg=ACCENT_FG, bold=True)

    def error(self, text: str) -> str:
        return self.p(text, fg=ERROR_FG, bold=True)

    def ok(self, text: str) -> str:
        return self.p(text, fg=OK_FG, bold=True)

    def notice(self, text: str) -> str:
        return self.p(text, fg=NOTICE_FG, bold=True)

    def selected(self, text: str) -> str:
        """The highlighted menu row or button (reverse-style bar when colour is on)."""
        return self.p(text, fg=SELECT_FG, bg=SELECT_BG, bold=True) if self.color else self.t(text)

    def message(self, kind: str, text: str) -> str:
        painter = {"ok": self.ok, "error": self.error, "notice": self.notice}.get(kind)
        return painter(text) if painter else self.t(text)

    def spinner(self, now: float) -> str:
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if self.unicode else "|/-\\"
        return frames[int(now * 10) % len(frames)]


def _wrap(text: str, width: int, indent: str = "") -> List[str]:
    """Word-wrap plain text (no escape codes) to ``width`` columns."""
    return textwrap.wrap(text, max(8, width), subsequent_indent=indent, break_on_hyphens=False) or [""]


def _labelled(s: Style, label: str, text: str, width: int, paint: Callable[[str], str],
              indent: str = "  ") -> List[str]:
    """``label`` (painted with ``paint``) followed by ``text``, word-wrapped to ``width`` columns."""
    label, text = s.t(label), s.t(text)
    parts = _wrap(label + text, width, indent)
    head = label.rstrip()
    if parts[0].startswith(head):
        parts[0] = paint(head) + parts[0][len(head):]
    return parts


def ip_summary(ips: Sequence[Tuple[str, str]], pending: bool = False) -> str:
    """``Your IP: 192.168.1.23 (wlan0), …`` for the footer (``pending``: still being looked up)."""
    if not ips:
        if pending:
            return "Your IP: looking it up…"
        return "Your IP: not found (is this computer connected to a network?)"
    return "Your IP: " + ", ".join(f"{address} ({iface})" if iface else address for iface, address in ips)


# ---------------------------------------------------------------------------------------------
# The banner
# ---------------------------------------------------------------------------------------------

_KNIGHT_PIXELS = (
    "....X.X....",
    "...XXXXX...",
    "..XXXXXXX..",
    ".XX.XXXXXX.",
    "XXXXXXXXXX.",
    "XXXXXXXXXXX",
    ".....XXXXXX",
    "....XXXXXXX",
    "...XXXXXXX.",
    "...XXXXXX..",
    "..XXXXXXXX.",
    ".XXXXXXXXXX",
)


def _half_blocks(pixels: Sequence[str]) -> Tuple[str, ...]:
    """Two pixel rows per text row using half-block characters."""
    rows = []
    for top, bottom in zip(pixels[0::2], pixels[1::2]):
        rows.append("".join("█" if a == "X" and b == "X" else "▀" if a == "X" else "▄" if b == "X" else " "
                            for a, b in zip(top, bottom)))
    return tuple(rows)


_KNIGHT_UNICODE = _half_blocks(_KNIGHT_PIXELS)
_TITLE_UNICODE = ("█   ▄▀█ █▄ █   █▀▀ █ █ █▀▀ █▀ █▀",
                  "█▄▄ █▀█ █ ▀█   █▄▄ █▀█ ██▄ ▄█ ▄█")
_KNIGHT_ASCII = (r"    __/\ ",
                 r"   /  o \ ",
                 r"  (__   | ",
                 r"     |  | ",
                 r"    /____\ ")
_TITLE_ASCII = (r" _      _   _  _    ___ _  _ ___ ___ ___",
                r"| |    /_\ | \| |  / __| || | __/ __/ __|",
                r"| |__ / _ \| .` | | (__| __ | _|\__ \__ \ ",
                r"|____/_/ \_\_|\_|  \___|_||_|___|___/___/")
TAGLINE = "terminal chess for two"


def banner_lines(s: Style, width: int, rows: int) -> List[str]:
    """The title: a knight next to big "LAN CHESS" letters if it fits in ``width`` x ``rows``, else one line."""
    version = f"v{__version__}"
    knight, title, title_row = ((_KNIGHT_UNICODE, _TITLE_UNICODE, 2) if s.unicode
                                else (_KNIGHT_ASCII, _TITLE_ASCII, 0))
    knight_w = max(len(row) for row in knight)
    tagline = f"{TAGLINE} · {version}"
    text_w = max(max(len(row) for row in title), len(tagline))
    if width >= knight_w + 3 + text_w and rows >= len(knight):
        lines = []
        for index, row in enumerate(knight):
            line = s.p(row.ljust(knight_w), fg=KNIGHT_FG, bold=True) + "   "
            offset = index - title_row
            if 0 <= offset < len(title):
                line += s.p(title[offset], fg=TITLE_FG, bold=True)
            elif offset == len(title):
                line += s.dim(tagline)
            lines.append(line)
        return lines
    if s.unicode:
        return [s.p("♞ ", fg=KNIGHT_FG, bold=True) + s.p("LAN CHESS", fg=TITLE_FG, bold=True) + "  " + s.dim(version)]
    return [s.p("LAN CHESS", fg=TITLE_FG, bold=True) + "  " + s.dim(version)]


# ---------------------------------------------------------------------------------------------
# Actions, game requests, background tasks
# ---------------------------------------------------------------------------------------------


@dataclass
class GameRequest:
    """Everything needed to start a game chosen in the menu."""

    mode: str                                  # "network" or "local"
    conn: Any = None
    my_color: Optional[str] = None
    my_name: str = "White"
    opponent_name: str = "Black"
    time_control: Optional[game.TimeControl] = None
    fen: str = STARTING_FEN
    flip: bool = True


@dataclass
class Action:
    """What a screen asks the app to do: push, pop, quit or game."""

    kind: str
    screen: Optional["Screen"] = None
    message: Tuple[Message, ...] = ()
    request: Optional[GameRequest] = None


def push(screen: "Screen") -> Action:
    return Action("push", screen=screen)


def pop(*message: Message) -> Action:
    return Action("pop", message=tuple(message))


def quit_menu() -> Action:
    return Action("quit")


def start_game(request: GameRequest) -> Action:
    return Action("game", request=request)


class Task:
    """Runs ``fn`` in a daemon thread (or at once when ``threaded`` is False); poll ``done``.

    ``on_abandon(result)`` runs if the task is cancelled before its result was used, so a
    connection opened after the user gave up is closed instead of leaking.
    """

    def __init__(self, fn: Callable[[], Any], threaded: bool = True,
                 on_abandon: Optional[Callable[[Any], None]] = None) -> None:
        self._fn = fn
        self._on_abandon = on_abandon
        self._lock = threading.Lock()
        self._cancelled = False
        self._finished = threading.Event()
        self.done = False
        self.result: Any = None
        self.error: Optional[BaseException] = None
        if threaded:
            threading.Thread(target=self._run, name="lanchess-menu-task", daemon=True).start()
        else:
            self._run()

    def _run(self) -> None:
        result: Any = None
        error: Optional[BaseException] = None
        try:
            result = self._fn()
        except Exception as exc:  # reported through .error
            error = exc
        with self._lock:
            self.result, self.error, self.done = result, error, True
            abandoned = self._cancelled
        self._finished.set()
        if abandoned and error is None:
            self._abandon(result)

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the task to finish; True if it has."""
        return self._finished.wait(max(0.0, timeout))

    def cancel(self) -> None:
        """Give up on the result (cleaning it up now or when it arrives)."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            finished = self.done and self.error is None
        if finished:
            self._abandon(self.result)

    def _abandon(self, result: Any) -> None:
        if self._on_abandon is not None and result is not None:
            try:
                self._on_abandon(result)
            except Exception:
                pass


class DiscoveryWorker:
    """Searches the network continuously in a thread: short ``discover_hosts`` rounds, merged.

    A host stays listed while it answered within the last ``keep`` seconds. The loopback addresses
    are probed too, so games hosted on this computer are found even when a firewall drops broadcast
    replies (see ``cli.LOOPBACK_TARGETS``).
    """

    def __init__(self, discover: Callable[..., List[net.HostInfo]] = net.discover_hosts,
                 clock: Callable[[], float] = time.monotonic, round_time: float = 0.6,
                 keep: float = 4.0, extra_targets: Sequence[str] = LOOPBACK_TARGETS) -> None:
        self._discover = discover
        self._clock = clock
        self.round_time = round_time
        self.keep = max(keep, 3 * round_time)
        self.extra_targets = tuple(extra_targets)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._seen: Dict[Tuple[str, int], Tuple[net.HostInfo, float]] = {}
        self._thread: Optional[threading.Thread] = None
        self.rounds = 0
        self.error = ""

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lanchess-menu-discovery", daemon=True)
            self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        """Stop searching. The current round (at most ``round_time``) ends and closes its socket on
        its own; by default this does not wait for that, so leaving the Join screen never stalls
        the keyboard (``wait``: seconds to wait anyway)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if wait > 0 and thread is not None and thread is not threading.current_thread():
            thread.join(wait)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def rescan(self) -> None:
        """Forget what was found and start counting rounds again."""
        with self._lock:
            self._seen.clear()
            self.rounds = 0

    def snapshot(self) -> Tuple[List[net.HostInfo], int]:
        """The hosts seen recently (in the order they were first found) and the finished rounds."""
        now = self._clock()
        with self._lock:
            hosts = [host for host, seen in self._seen.values() if now - seen <= self.keep]
            return hosts, self.rounds

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                found = self._discover(timeout=self.round_time, extra_targets=self.extra_targets)
                self.error = ""
            except Exception as exc:  # never let the search thread die silently
                found = []
                self.error = str(exc) or type(exc).__name__
                self._stop.wait(1.0)
            now = self._clock()
            with self._lock:
                for host in found:
                    self._seen[(host.address, host.port)] = (host, now)
                self.rounds += 1
            self._stop.wait(0.05)


# ---------------------------------------------------------------------------------------------
# Firewall hints
# ---------------------------------------------------------------------------------------------


def _read_text(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def detect_firewall(platform: Optional[str] = None, exists: Callable[[str], bool] = os.path.exists,
                    read_text: Callable[[str], str] = _read_text,
                    which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """``"firewalld"``, ``"ufw"`` or None: a cheap guess (no root needed) at the Linux firewall in use."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return None
    if exists("/run/firewalld") or exists("/var/run/firewalld"):
        return "firewalld"
    if re.search(r"^\s*ENABLED\s*=\s*yes", read_text("/etc/ufw/ufw.conf"), re.M | re.I):
        return "ufw"
    if which("firewall-cmd"):
        return "firewalld"
    if which("ufw") or exists("/usr/sbin/ufw") or exists("/sbin/ufw"):
        return "ufw"
    return None


def firewall_command(kind: Optional[str], port: int) -> str:
    """The command that opens the game ports for ``kind`` (``"firewalld"`` or ``"ufw"``), else ""."""
    udp = net.DISCOVERY_PORT
    if kind == "firewalld":
        return f"sudo firewall-cmd --add-port={port}/tcp --add-port={udp}/udp"
    if kind == "ufw":
        return " && ".join(firewall_commands(kind, port))
    return ""


def firewall_commands(kind: Optional[str], port: int) -> List[str]:
    """The same as ``firewall_command``, one shorter command per port (for narrow screens)."""
    udp = net.DISCOVERY_PORT
    if kind == "firewalld":
        return [f"sudo firewall-cmd --add-port={port}/tcp", f"sudo firewall-cmd --add-port={udp}/udp"]
    if kind == "ufw":
        return [f"sudo ufw allow {port}/tcp", f"sudo ufw allow {udp}/udp"]
    return []


def firewall_hint(kind: Optional[str], port: int) -> List[str]:
    """What to do if the other player cannot connect: an explanation, then the command (if known)."""
    command = firewall_command(kind, port)
    if command:
        return [f"If they can't connect, open the ports in this computer's firewall ({kind}):", command]
    return [f"If they can't connect, allow incoming TCP {port} and UDP {net.DISCOVERY_PORT} "
            "in this computer's firewall."]


# ---------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------


class MenuContext:
    """Settings plus every outside dependency of the menu (all replaceable for tests)."""

    FIRST_IPS_WAIT = 0.25  # seconds the first local_ips() call waits before showing "looking it up"

    def __init__(self, settings: Optional[config.Settings] = None, explicit: Optional[Dict[str, Any]] = None,
                 config_path: Optional[str] = None, *, theme: Optional[ui.Theme] = None,
                 color_ok: bool = True, clock: Callable[[], float] = time.monotonic,
                 size_fn: Optional[Callable[[], Tuple[int, int]]] = None, threaded: bool = True,
                 server_factory: Optional[Callable[[int], Any]] = None,
                 responder_factory: Optional[Callable[[str, int], Any]] = None,
                 discovery_factory: Optional[Callable[[], Any]] = None,
                 connect: Optional[Callable[..., Any]] = None,
                 server_handshake: Optional[Callable[..., Tuple[str, str]]] = None,
                 client_handshake: Optional[Callable[..., Dict[str, Any]]] = None,
                 local_ips: Optional[Callable[[], List[Tuple[str, str]]]] = None,
                 firewall: Optional[Callable[[], Optional[str]]] = None,
                 play: Optional[Callable[[GameRequest], List[Message]]] = None,
                 accept_timeout: float = 0.05) -> None:
        self.config_path = config_path
        self.settings = settings if settings is not None else config.load(config_path)
        self.explicit = dict(explicit or {})
        self.color_ok = color_ok
        self._fixed_theme = theme
        self.clock = clock
        self.size_fn = size_fn or game._live_terminal_size
        self.threaded = threaded
        self.server_factory = server_factory or (lambda port: net.Server(port=port))
        self.responder_factory = responder_factory or net.DiscoveryResponder
        self.discovery_factory = discovery_factory or (lambda: DiscoveryWorker(net.discover_hosts, clock=self.clock))
        self.connect = connect or net.connect
        self.server_handshake = server_handshake or net.server_handshake
        self.client_handshake = client_handshake or net.client_handshake
        self._local_ips = local_ips or net.local_ip_addresses
        self._ips: Optional[List[Tuple[str, str]]] = None
        self._ips_at = 0.0
        self._ips_task: Optional[Task] = None
        self._firewall = firewall or detect_firewall
        self._firewall_kind: Optional[Tuple[Optional[str]]] = None
        self.play = play or self._no_player
        self.accept_timeout = accept_timeout
        self.history: List[str] = []
        self._stopping: List[Task] = []
        self.refresh_theme()

    @staticmethod
    def _no_player(request: GameRequest) -> List[Message]:
        raise RuntimeError("no game runner configured")

    # -- settings ------------------------------------------------------------------------------

    def options(self) -> cli.Options:
        """Saved settings, overridden by the options typed on the command line."""
        return cli.Options.resolve(self.explicit, self.settings)

    def refresh_theme(self) -> None:
        self.theme = self._fixed_theme or cli.make_theme(self.options(), color_ok=self.color_ok)
        self.style = Style(self.theme)

    def save_settings(self, settings: config.Settings) -> None:
        """Save (raises OSError) and adopt the new settings."""
        config.save(settings, self.config_path)
        self.settings = settings.copy()
        self.refresh_theme()

    def remember_host(self, host: str, port: int) -> None:
        """Add a joined host to the recent hosts, in memory and in the config file."""
        self.settings.remember_host(host, port)
        config.remember_host(host, port, self.config_path)

    def reload_recent_hosts(self) -> None:
        self.settings.recent_hosts = config.load(self.config_path).recent_hosts

    def player_name(self) -> str:
        return cli._player_name(None, self.settings.name)

    def config_file(self) -> str:
        return self.config_path or config.config_path()

    # -- environment ---------------------------------------------------------------------------

    def _fetch_ips(self) -> List[Tuple[str, str]]:
        try:
            return list(self._local_ips())
        except Exception:
            return []

    def local_ips(self) -> List[Tuple[str, str]]:
        """This computer's LAN addresses, looked up in the background and refreshed every 10 s (a
        lookup can be slow on some systems, and the menu must never freeze). The first call waits
        ``FIRST_IPS_WAIT`` seconds at most; until the first answer the list is empty and
        ``ips_pending`` is True."""
        now = self.clock()
        if self._ips_task is None and (self._ips is None or now - self._ips_at > 10.0):
            self._ips_at = now
            self._ips_task = self.run_task(self._fetch_ips)
            if self._ips is None:
                self._ips_task.wait(self.FIRST_IPS_WAIT)
        task = self._ips_task
        if task is not None and task.done:
            self._ips_task = None
            if task.error is None and task.result is not None:
                self._ips = task.result
        return self._ips if self._ips is not None else []

    @property
    def ips_pending(self) -> bool:
        """True while the first lookup of this computer's addresses has not answered yet."""
        return self._ips is None and self._ips_task is not None

    def firewall_kind(self) -> Optional[str]:
        if self._firewall_kind is None:
            try:
                self._firewall_kind = (self._firewall(),)
            except Exception:
                self._firewall_kind = (None,)
        return self._firewall_kind[0]

    def run_task(self, fn: Callable[[], Any], on_abandon: Optional[Callable[[Any], None]] = None) -> Task:
        return Task(fn, threaded=self.threaded, on_abandon=on_abandon)

    def stop_later(self, stop: Callable[[], Any]) -> None:
        """Run ``stop`` in the background: stopping the network-search responder waits for its
        thread (up to 0.2 s), and the menu must keep reading keys meanwhile."""
        self._stopping = [task for task in self._stopping if not task.done]
        self._stopping.append(self.run_task(stop))

    def wait_for_stops(self, timeout: float = 2.5) -> None:
        """Wait (at most ``timeout`` seconds in all) until earlier ``stop_later`` calls are done."""
        deadline = time.monotonic() + timeout
        for task in self._stopping:
            task.wait(deadline - time.monotonic())
        self._stopping = [task for task in self._stopping if not task.done]

    # -- games ---------------------------------------------------------------------------------

    def make_session(self, request: GameRequest) -> game.GameSession:
        options = self.options()
        return game.GameSession(mode=request.mode, my_color=request.my_color, my_name=request.my_name,
                                opponent_name=request.opponent_name, conn=request.conn,
                                time_control=request.time_control, fen=request.fen,
                                pgn_dir=options.pgn_dir, autosave=not options.no_save,
                                flip=request.flip, unicode=self.theme.unicode,
                                board_size=self.settings.board_size)

    def game_finished(self, session: game.GameSession) -> List[Message]:
        """The messages the main menu shows after a game (also kept for printing at exit)."""
        messages: List[Message] = []
        for line in session.summary_lines():
            if not line.startswith("Game saved to "):
                messages.append(("ok" if "You won" in line else "info", line))
        for path in session.saved_paths:
            messages.append(("info", f"Game saved to {game._display_path(path)}"))
        self.history.extend(text for _kind, text in messages)
        return messages or [("info", "You left the game.")]


# ---------------------------------------------------------------------------------------------
# Screens and form fields
# ---------------------------------------------------------------------------------------------


@dataclass
class Body:
    """A screen's content: lines, the caret (row, col) and the row to keep visible when scrolling."""

    lines: List[str]
    cursor: Optional[Tuple[int, int]] = None
    focus: int = 0


class Screen:
    """One page of the menu. ``tick`` runs every loop, ``handle_key`` for each key."""

    title = ""
    center = False
    poll_timeout = 0.1

    def __init__(self, ctx: MenuContext) -> None:
        self.ctx = ctx

    def body(self, s: Style, width: int, height: int) -> Body:
        return Body([])

    def hints(self, s: Style) -> str:
        return "Esc back"

    def info(self, s: Style) -> str:
        ips = self.ctx.local_ips()
        return ip_summary(ips, self.ctx.ips_pending)

    def handle_key(self, key: str) -> Optional[Action]:
        return None

    def tick(self) -> Optional[Action]:
        return None

    def on_leave(self) -> None:
        """Called once when the screen is closed: release sockets and threads here."""


class ScrollingScreen(Screen):
    """A screen whose text can be taller than the room: ↑/↓ (j/k), PgUp/PgDn, Home/End scroll it."""

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        self.offset = 0
        self.page = 10
        self.total = 0

    def scrolled(self, lines: List[str], height: int) -> Body:
        """The part of ``lines`` that fits in ``height`` rows at the current scroll position."""
        self.total, self.page = len(lines), max(1, height)
        self.offset = max(0, min(self.offset, self.total - self.page))
        return Body(lines[self.offset:self.offset + self.page])

    @property
    def scrollable(self) -> bool:
        return self.total > self.page

    def scroll_key(self, key: str) -> bool:
        """Scroll for a scrolling key; True if ``key`` was one."""
        steps = {term.UP: -1, "k": -1, term.DOWN: 1, "j": 1, term.PGUP: -self.page, term.PGDN: self.page,
                 " ": self.page, term.HOME: -10 ** 6, term.END: 10 ** 6}
        if key not in steps:
            return False
        self.offset = max(0, min(self.offset + steps[key], max(0, self.total - self.page)))
        return True

    def position(self) -> str:
        """``Lines 1–12 of 30`` while not everything fits, else ""."""
        if not self.scrollable:
            return ""
        last = min(self.total, self.offset + self.page)
        return f"Lines {self.offset + 1}–{last} of {self.total}"


_BACK_KEYS = (term.ESCAPE, term.CTRL_C)
_EDIT_KEYS = frozenset((term.BACKSPACE, term.DELETE, term.LEFT, term.RIGHT, term.HOME, term.END,
                        term.CTRL_A, term.CTRL_E, term.CTRL_U, term.CTRL_W, term.CTRL_D))


class Field:
    """A row of a form: a label and a value."""

    text_input = False

    def __init__(self, label: str, help: str = "") -> None:
        self.label = label
        self.help = help
        self.error = ""
        self.visible_if: Optional[Callable[[], bool]] = None

    def visible(self) -> bool:
        return self.visible_if is None or self.visible_if()

    def handle(self, key: str) -> bool:
        """Apply a key; True if the field used it."""
        return False

    def render(self, s: Style, label_w: int, width: int, focused: bool) -> Tuple[str, Optional[int]]:
        marker = s.accent(s.marker) if focused else "  "
        label = s.t(self.label).ljust(label_w)
        prefix = marker + (s.bold(label) if focused else label) + "  "
        prefix_w = ui.visible_len(prefix)
        value, caret = self.render_value(s, max(4, width - prefix_w), focused)
        return prefix + value, None if caret is None else prefix_w + caret

    def render_value(self, s: Style, width: int, focused: bool) -> Tuple[str, Optional[int]]:
        return "", None


class ChoiceField(Field):
    """Cycles through fixed choices with Left/Right (or Space)."""

    def __init__(self, label: str, choices: Sequence[Tuple[Any, str]], value: Any = None, help: str = "") -> None:
        super().__init__(label, help)
        self.choices = list(choices)
        self.index = 0
        self.select(value)

    @property
    def value(self) -> Any:
        return self.choices[self.index][0]

    @property
    def text(self) -> str:
        return self.choices[self.index][1]

    def select(self, value: Any) -> None:
        for index, (choice, _text) in enumerate(self.choices):
            if choice == value:
                self.index = index
                return

    def cycle(self, step: int) -> None:
        self.index = (self.index + step) % len(self.choices)
        self.error = ""

    def handle(self, key: str) -> bool:
        if key == term.LEFT:
            self.cycle(-1)
            return True
        if key in (term.RIGHT, " "):
            self.cycle(1)
            return True
        return False

    def render_value(self, s: Style, width: int, focused: bool) -> Tuple[str, Optional[int]]:
        if focused:
            return s.selected(f"◂ {self.text} ▸"), None
        return "  " + s.t(self.text), None


class TextField(Field):
    """A one-line text box (``allowed``: the characters that may be typed)."""

    text_input = True

    def __init__(self, label: str, value: str = "", placeholder: str = "", max_len: int = 40,
                 allowed: Optional[str] = None, box: int = 16, help: str = "") -> None:
        super().__init__(label, help)
        self.placeholder = placeholder
        self.allowed = allowed
        self.box = box
        self.editor = term.LineEditor(None, max_len=max_len)
        self.editor.set_buffer(value)

    @property
    def value(self) -> str:
        return self.editor.buffer

    @value.setter
    def value(self, text: str) -> None:
        self.editor.set_buffer(text)

    def handle(self, key: str) -> bool:
        if len(key) == 1:
            if key.isprintable() and (self.allowed is None or key in self.allowed):
                self.editor.handle_key(key)
                self.error = ""
            return True
        if key in _EDIT_KEYS:
            self.editor.handle_key(key)
            self.error = ""
            return True
        return False

    def render_value(self, s: Style, width: int, focused: bool) -> Tuple[str, Optional[int]]:
        box = max(4, min(self.box, width - 2))
        caret: Optional[int] = None
        if focused:
            shown, column = ui._scroll_input(self.value, self.editor.cursor, box, s.theme)
            inner = ui.pad(shown, box)
            caret = 1 + column
        elif self.value:
            inner = ui.pad(ui.truncate(self.value, box, s.t("…")), box)
        else:
            inner = ui.pad(s.dim(ui.truncate(s.t(self.placeholder), box)), box)
        left, right = (s.accent("["), s.accent("]")) if focused else ("[", "]")
        return left + inner + right, caret


class Button(Field):
    def __init__(self, label: str, action: Callable[[], Optional[Action]], help: str = "") -> None:
        super().__init__(label, help)
        self.action = action

    def render(self, s: Style, label_w: int, width: int, focused: bool) -> Tuple[str, Optional[int]]:
        text = f"[ {self.label} ]"
        if focused:
            return s.accent(s.marker) + s.selected(text), None
        return "  " + s.bold(text), None


def time_fields(initial: str, label: str = "Time control") -> Tuple[ChoiceField, TextField]:
    """A preset picker plus the text box shown when "Custom…" is picked."""
    values = [value for value, _text in TIME_PRESETS]
    preset = initial if initial in values else CUSTOM
    choice = ChoiceField(label, TIME_PRESETS, preset,
                         help="Minutes for each player + seconds added after every move.")
    custom = TextField("  Minutes+sec", "" if preset != CUSTOM else initial, placeholder="e.g. 7+2",
                       max_len=12, allowed=TIME_CHARS, box=10,
                       help="MINUTES+SECONDS, for example 7+2, 10 or 0.5+0 (0 = untimed).")
    custom.visible_if = lambda: choice.value == CUSTOM
    return choice, custom


def resolve_time(choice: ChoiceField, custom: TextField) -> Optional[game.TimeControl]:
    """The picked time control; raises ValueError (and sets ``custom.error``) for a bad custom value."""
    if choice.value != CUSTOM:
        return game.parse_time_control(choice.value)
    text = custom.value.strip()
    try:
        if not text:
            raise ValueError("Type a time control, for example 7+2.")
        return game.parse_time_control(text)
    except ValueError as exc:
        custom.error = str(exc)
        raise


def port_value(field: TextField) -> int:
    """The port typed in ``field``; raises ValueError (and sets ``field.error``)."""
    text = field.value.strip()
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        field.error = "Use a port number from 1 to 65535."
        raise ValueError(field.error)
    return int(text)


class FormScreen(Screen):
    """A list of fields: Up/Down (Tab) move, Left/Right change choices, Enter presses buttons."""

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        self.fields: List[Field] = []
        self.focused: Optional[Field] = None
        self.message: Optional[Message] = None

    def visible_fields(self) -> List[Field]:
        return [f for f in self.fields if f.visible()]

    def current(self) -> Field:
        fields = self.visible_fields()
        if self.focused not in fields:
            self.focused = fields[0]
        assert self.focused is not None
        return self.focused

    def focus(self, field: Field) -> None:
        self.focused = field

    def move(self, step: int) -> None:
        fields = self.visible_fields()
        index = fields.index(self.current())
        self.focused = fields[(index + step) % len(fields)]

    def cancel(self) -> Optional[Action]:
        return pop()

    def handle_key(self, key: str) -> Optional[Action]:
        field = self.current()
        if key in _BACK_KEYS:
            return self.cancel()
        if key == term.UP:
            self.move(-1)
        elif key in (term.DOWN, term.TAB):
            self.move(1)
        elif key == term.ENTER:
            if isinstance(field, Button):
                return field.action()
            self.move(1)
        elif field.handle(key):
            self.message = None
        elif not field.text_input:
            if key == "k":
                self.move(-1)
            elif key == "j":
                self.move(1)
            elif key == "q":
                return self.cancel()
        return None

    def intro(self, s: Style, width: int) -> List[str]:
        return []

    def outro(self, s: Style, width: int) -> List[str]:
        return []

    def body(self, s: Style, width: int, height: int) -> Body:
        lines = self.intro(s, width)
        fields = self.visible_fields()
        focused = self.current()
        label_w = max([len(s.t(f.label)) for f in fields if not isinstance(f, Button)] or [0])
        cursor: Optional[Tuple[int, int]] = None
        focus_row = 0
        in_buttons = False
        for field in fields:
            if isinstance(field, Button) and not in_buttons:
                lines.append("")
                in_buttons = True
            line, caret = field.render(s, label_w, width, field is focused)
            if field is focused:
                focus_row = len(lines)
                if caret is not None:
                    cursor = (focus_row, caret)
            lines.append(line)
            if field.error:
                indent = " " * (label_w + 6)
                lines.extend(indent + s.error(part) for part in _wrap("✗ " + field.error, width - len(indent)))
        if self.message is not None:
            lines.append("")
            lines.extend(s.message(self.message[0], part) for part in _wrap(self.message[1], width))
        if focused.help:
            lines.append("")
            lines.extend(s.dim(part) for part in _wrap(focused.help, width))
        lines.extend(self.outro(s, width))
        return Body(lines, cursor, focus_row)

    def hints(self, s: Style) -> str:
        field = self.current()
        if isinstance(field, ChoiceField):
            return f"{s.updown} move · {s.leftright} change · Enter next · Esc back"
        if isinstance(field, Button):
            return f"{s.updown} move · Enter {field.label.lower()} · Esc back"
        return f"{s.updown} move · type to edit · Enter next · Esc back"


# -- main menu --------------------------------------------------------------------------------


class MainMenu(Screen):
    center = True
    ITEMS: Tuple[Tuple[str, str, str], ...] = (
        ("host", "Host a game", "wait for a player on this network"),
        ("join", "Join a game", "find a game on this network"),
        ("local", "Local game (same keyboard)", "two players, one computer"),
        ("settings", "Settings", "name, defaults, display"),
        ("help", "How to play", "moves, commands, firewall"),
        ("quit", "Quit", ""),
    )

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        self.index = 0
        self.message: List[Message] = []

    @property
    def selected(self) -> str:
        return self.ITEMS[self.index][0]

    def handle_key(self, key: str) -> Optional[Action]:
        count = len(self.ITEMS)
        if key in (term.UP, "k"):
            self.index = (self.index - 1) % count
        elif key in (term.DOWN, "j", term.TAB):
            self.index = (self.index + 1) % count
        elif key in (term.HOME, term.PGUP):
            self.index = 0
        elif key in (term.END, term.PGDN):
            self.index = count - 1
        elif len(key) == 1 and key.isdigit() and 1 <= int(key) <= count:
            self.index = int(key) - 1
            return self.activate()
        elif key == term.ENTER:
            return self.activate()
        elif key in _BACK_KEYS or key in ("q", "Q"):
            return quit_menu()
        return None

    def activate(self) -> Optional[Action]:
        self.message = []
        item = self.selected
        screens: Dict[str, Callable[[MenuContext], Screen]] = {
            "host": HostScreen, "join": JoinScreen, "local": LocalScreen,
            "settings": SettingsScreen, "help": HelpScreen,
        }
        if item in screens:
            return push(screens[item](self.ctx))
        return quit_menu()

    def _item_lines(self, s: Style, width: int) -> List[str]:
        label_w = max(len(s.t(label)) for _key, label, _desc in self.ITEMS)
        desc_w = max(len(desc) for _key, _label, desc in self.ITEMS)
        show_desc = width >= 2 + 5 + label_w + 3 + desc_w + 1
        lines = []
        for index, (_key, label, desc) in enumerate(self.ITEMS):
            number = str(index + 1)
            label_text = s.t(label).ljust(label_w)
            desc_text = ("   " + desc.ljust(desc_w)) if show_desc else ""
            if index == self.index:
                lines.append(s.accent(s.marker) + s.selected(f" {number}  {label_text}{desc_text} "))
            else:
                lines.append("  " + " " + s.dim(number) + "  " + label_text + s.dim(desc_text))
        return lines

    def body(self, s: Style, width: int, height: int) -> Body:
        items = self._item_lines(s, width)
        message: List[str] = []
        for kind, text in self.message:
            message.extend(s.message(kind, part) for part in _wrap(text, width))
        reserved = len(items) + (len(message) + 1 if message else 0)
        banner = banner_lines(s, width, height - reserved - 2)
        lines = banner + [""] + items + ([""] + message if message else [])
        return Body(lines, None, len(banner) + 1 + self.index)

    def hints(self, s: Style) -> str:
        return f"{s.updown} move · Enter select · 1-{len(self.ITEMS)} shortcut · Esc quit"


# -- host -------------------------------------------------------------------------------------


class HostScreen(FormScreen):
    title = "Host a game"

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        saved = ctx.settings
        self.color = ChoiceField("Your colour", COLOR_CHOICES, saved.host_color,
                                 help="The colour you play; Random lets the computer decide.")
        self.time, self.custom = time_fields(saved.time_control)
        self.port = TextField("Port", str(saved.port), max_len=5, allowed="0123456789", box=6,
                              help=f"TCP port to listen on. The default is {net.DEFAULT_PORT}; with another "
                                   "port the other player types IP:PORT.")
        self.start = Button("Start hosting", self.submit, help="Wait for the other player to join.")
        self.fields = [self.color, self.time, self.custom, self.port, self.start]

    def submit(self) -> Optional[Action]:
        try:
            time_control = resolve_time(self.time, self.custom)
        except ValueError:
            self.focus(self.custom)
            return None
        try:
            port = port_value(self.port)
        except ValueError:
            self.focus(self.port)
            return None
        return push(WaitingScreen(self.ctx, self.color.value, time_control, port))


def _describe_time(time_control: Optional[game.TimeControl]) -> str:
    return f"{time_control} ({time_control.describe()})" if time_control else "untimed"


class WaitingScreen(ScrollingScreen):
    """Listens for the other player, answers network searches, then hands the connection to a game."""

    title = "Host a game"
    poll_timeout = 0.05

    def __init__(self, ctx: MenuContext, color: str, time_control: Optional[game.TimeControl], port: int) -> None:
        super().__init__(ctx)
        self.color = color
        self.time_control = time_control
        self.port = port
        self.name = ctx.player_name()
        self.server: Any = None
        self.responder: Any = None
        self.discovery = False
        self.failure: Optional[Tuple[str, List[str]]] = None
        self.notice = ""
        self.pending: Optional[Tuple[Any, Task]] = None
        self.started = ctx.clock()
        ctx.wait_for_stops()  # the last host's search responder may still be letting go of its port
        try:
            self.server = ctx.server_factory(port)
        except OSError as exc:
            self.failure = cli.server_error(exc, port, advice="form")
            return
        self.port = getattr(self.server, "port", port) or port
        try:
            responder = ctx.responder_factory(self.name, self.port)
            self.discovery = bool(responder.start())
        except OSError:
            responder, self.discovery = None, False
        self.responder = responder if self.discovery else None

    @property
    def connecting(self) -> bool:
        return self.pending is not None

    def _close_listeners(self) -> None:
        responder, self.responder = self.responder, None
        server, self.server = self.server, None
        if responder is not None:
            self.ctx.stop_later(responder.stop)  # never stall the keyboard (see MenuContext.stop_later)
        if server is not None:
            try:
                server.close()
            except Exception:
                pass

    def on_leave(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None:
            conn, task = pending
            task.cancel()
            _close_quietly(conn)
        self._close_listeners()

    def handle_key(self, key: str) -> Optional[Action]:
        if key in _BACK_KEYS or key in ("q", "Q"):
            return pop()
        self.scroll_key(key)
        return None

    def tick(self) -> Optional[Action]:
        if self.pending is not None:
            conn, task = self.pending
            if not task.done:
                return None
            self.pending = None
            if task.error is None:
                opponent, host_color = task.result
                self._close_listeners()
                return start_game(GameRequest(mode="network", conn=conn, my_color=host_color, my_name=self.name,
                                              opponent_name=opponent, time_control=self.time_control))
            _close_quietly(conn)
            reason = str(task.error) or type(task.error).__name__
            self.notice = f"A connection from {getattr(conn, 'peer', 'someone')} failed ({reason}). Still waiting…"
            return None
        if self.server is None:
            return None
        try:
            conn = self.server.accept(self.ctx.accept_timeout)
        except OSError as exc:
            self._close_listeners()
            self.failure = (f"stopped listening for players: {exc.strerror or exc}", [])
            return None
        if conn is None:
            return None
        self.notice = ""
        ctx, name, color, time_control = self.ctx, self.name, self.color, self.time_control

        def handshake() -> Tuple[str, str]:
            return ctx.server_handshake(conn, name, color, time_control, STARTING_FEN, timeout=10.0)

        self.pending = (conn, ctx.run_task(handshake, on_abandon=lambda _result: _close_quietly(conn)))
        return None

    def body(self, s: Style, width: int, height: int) -> Body:
        return self.scrolled(self.lines(s, width), height)

    def lines(self, s: Style, width: int) -> List[str]:
        """Everything this screen shows, wrapped to ``width`` (the body scrolls if it is too tall)."""
        if self.failure is not None:
            message, hints = self.failure
            lines = [s.error(part) for part in _wrap(f"✗ Could not host the game: {message}", width, "  ")]
            for hint in hints:
                lines.extend("  " + part for part in _wrap(hint, width - 2))
            lines += [""] + [s.dim(part) for part in _wrap("Press Esc to go back (and choose another port).", width)]
            return lines
        now = self.ctx.clock()
        spin = s.accent(s.spinner(now))
        if self.pending is not None:
            peer = getattr(self.pending[0], "peer", "")
            lines = [spin + " " + s.bold(f"Connecting… ({peer})" if peer else "Connecting…")]
        else:
            lines = [spin + " " + s.bold("Waiting for an opponent…")]
        colour = {"white": "You play White", "black": "You play Black"}.get(self.color, "Colours are picked at random")
        lines.extend(s.dim(part) for part in
                     _wrap(f"{colour} · {_describe_time(self.time_control)} · TCP port {self.port}", width, "  "))
        if self.notice:
            lines.extend(s.notice(part) for part in _wrap(self.notice, width))
        lines.append("")
        ips = self.ctx.local_ips()
        suffix = "" if self.port == net.DEFAULT_PORT else f":{self.port}"
        if ips:
            lines.append(s.bold("This computer's addresses:"))
            address_w = max(len(address) for _iface, address in ips)
            for iface, address in ips:
                lines.append("    " + s.accent(address.ljust(address_w)) + ("   " + s.dim(iface) if iface else ""))
        elif self.ctx.ips_pending:
            lines.append(s.dim("Looking up this computer's IP address…"))
        else:
            lines.append(s.notice("Could not find this computer's IP address (see 'ip addr' or 'ipconfig')."))
        lines.append("")
        command = cli.launch_command()
        target = (ips[0][1] if ips else "<this computer's IP>") + suffix
        lines.extend(_wrap("On the other laptop choose Join, or run:", width))
        lines.extend("    " + s.bold(part) for part in _wrap(f"{command} join {target}", width - 4, "  "))
        lines.append("")
        udp = net.DISCOVERY_PORT
        if self.discovery:
            lines.extend(_labelled(s, "Network search: ", f"on — they will see “{self.name}” in the Join "
                                   f"list (UDP {udp}).", width, s.ok))
        else:
            lines.extend(_labelled(s, "Network search: off ", f"(UDP {udp} is busy) — the other player must "
                                   "type one of the addresses.", width, s.notice))
        kind = self.ctx.firewall_kind()
        lines.extend(s.dim(part) for part in _wrap(firewall_hint(kind, self.port)[0], width, "  "))
        # One complete command per line where possible, so it can be copied as it is.
        combined = firewall_command(kind, self.port)
        commands = [combined] if len(combined) <= width - 4 else firewall_commands(kind, self.port)
        for fw_command in filter(None, commands):
            lines.extend("    " + s.bold(part) for part in _wrap(fw_command, width - 4, "  "))
        return lines

    def info(self, s: Style) -> str:
        return self.position() if self.scrollable else super().info(s)

    def hints(self, s: Style) -> str:
        scroll = f"{s.updown} scroll · " if self.scrollable else ""
        return scroll + ("Esc cancel" if self.failure is None else "Esc back")


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


# -- join -------------------------------------------------------------------------------------


@dataclass
class JoinItem:
    key: str
    kind: str                      # host, recent, manual or rescan
    name: str = ""
    address: str = ""
    this_computer: bool = False
    target: Optional[Tuple[str, int]] = None


class JoinScreen(Screen):
    """A live list of the games on the network, recent hosts and manual address entry."""

    title = "Join a game"

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        self.worker = ctx.discovery_factory()
        self.worker.start()
        self.mode = "list"             # list, manual, connecting or error
        self.selected_key: Optional[str] = None
        self.user_moved = False
        self.manual = TextField("Address", "", placeholder="e.g. 192.168.1.23", max_len=80, box=28)
        self.target: Optional[Tuple[str, int]] = None
        self.task: Optional[Task] = None
        self.name = ctx.player_name()
        self.failure: Optional[Tuple[str, List[str]]] = None
        self.rounds = 0
        self.items: List[JoinItem] = []
        self.refresh()

    # -- the list -----------------------------------------------------------------------------

    def refresh(self) -> None:
        old_index = self.index if self.items else 0
        hosts, self.rounds = self.worker.snapshot()
        hosts = cli._without_loopback_duplicates(list(hosts))
        mine = {address for _iface, address in self.ctx.local_ips()} | {LOOPBACK}
        items = [JoinItem(f"host {host.address}:{host.port}", "host", host.name,
                          config.format_host(host.address, host.port), host.address in mine,
                          (host.address, host.port)) for host in hosts]
        found = {item.target for item in items}
        for text in self.ctx.settings.recent_hosts:
            try:
                target = net.parse_host_port(text, self.ctx.settings.port)
            except ValueError:
                continue
            if target not in found:
                items.append(JoinItem(f"recent {text}", "recent", address=text, target=target))
        items.append(JoinItem("manual", "manual"))
        items.append(JoinItem("rescan", "rescan"))
        self.items = items
        keys = [item.key for item in items]
        if not self.user_moved:
            self.selected_key = keys[0]  # until the user moves, the first game found is selected
        elif self.selected_key not in keys:
            self.selected_key = keys[min(old_index, len(keys) - 1)]

    @property
    def index(self) -> int:
        keys = [item.key for item in self.items]
        return keys.index(self.selected_key) if self.selected_key in keys else 0

    @property
    def selected(self) -> JoinItem:
        return self.items[self.index]

    def select(self, kind: str) -> None:
        """Select the first item of ``kind`` (used by tests and shortcuts)."""
        for item in self.items:
            if item.kind == kind:
                self.selected_key = item.key
                self.user_moved = True
                return

    def _move(self, step: int) -> None:
        self.selected_key = self.items[(self.index + step) % len(self.items)].key
        self.user_moved = True

    # -- keys ---------------------------------------------------------------------------------

    def handle_key(self, key: str) -> Optional[Action]:
        handler = {"list": self._list_key, "manual": self._manual_key,
                   "connecting": self._connecting_key, "error": self._error_key}[self.mode]
        return handler(key)

    def _list_key(self, key: str) -> Optional[Action]:
        if key in _BACK_KEYS or key in ("q", "Q"):
            return pop()
        if key in (term.UP, "k"):
            self._move(-1)
        elif key in (term.DOWN, "j", term.TAB):
            self._move(1)
        elif key in (term.HOME, term.PGUP):
            self.selected_key, self.user_moved = self.items[0].key, True
        elif key in (term.END, term.PGDN):
            self.selected_key, self.user_moved = self.items[-1].key, True
        elif key in ("r", "R"):
            self.rescan()
        elif key in ("m", "M", "a", "A"):
            self.select("manual")
            self.mode = "manual"
        elif key == term.ENTER:
            item = self.selected
            if item.kind in ("host", "recent") and item.target is not None:
                self.connect(item.target)
            elif item.kind == "manual":
                self.mode = "manual"
                self.manual.error = ""
            elif item.kind == "rescan":
                self.rescan()
        return None

    def _manual_key(self, key: str) -> Optional[Action]:
        if key in _BACK_KEYS:
            self.mode = "list"
            self.manual.error = ""
        elif key == term.ENTER:
            text = self.manual.value.strip()
            if not text:
                self.manual.error = "Type the IP address shown on the host's screen, for example 192.168.1.23."
                return None
            try:
                target = net.parse_host_port(text, self.ctx.settings.port)
            except ValueError as exc:
                self.manual.error = str(exc)
                return None
            self.connect(target)
        elif key in (term.UP, term.DOWN, term.TAB):
            self.mode = "list"
            return self._list_key(key)
        else:
            self.manual.handle(key)
        return None

    def _connecting_key(self, key: str) -> Optional[Action]:
        if key in _BACK_KEYS or key in ("q", "Q"):
            self._cancel_task()
            self.mode = "list"
        return None

    def _error_key(self, key: str) -> Optional[Action]:
        if key in (term.ENTER, "r", "R") and self.target is not None:
            self.connect(self.target)
        elif key in _BACK_KEYS or key in ("q", "Q"):
            self.mode = "list"
            self.failure = None
        elif key in ("m", "M"):
            self.failure = None
            self.select("manual")
            self.mode = "manual"
        return None

    # -- actions ------------------------------------------------------------------------------

    def rescan(self) -> None:
        self.worker.rescan()
        self.refresh()

    def connect(self, target: Tuple[str, int]) -> None:
        self._cancel_task()
        self.target = target
        self.mode = "connecting"
        self.failure = None
        host, port = target
        ctx, name = self.ctx, self.name

        def work() -> cli.Joined:
            return cli.join_host(host, port, name, connect=ctx.connect, handshake=ctx.client_handshake)

        self.task = ctx.run_task(work, on_abandon=lambda joined: _close_quietly(joined.conn))

    def _cancel_task(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()

    def tick(self) -> Optional[Action]:
        self.refresh()
        task = self.task
        if self.mode != "connecting" or task is None or not task.done:
            return None
        self.task = None
        assert self.target is not None
        if task.error is None:
            joined = task.result
            host, port = self.target
            self.ctx.remember_host(host, port)
            return start_game(GameRequest(mode="network", conn=joined.conn, my_color=joined.my_color,
                                          my_name=self.name, opponent_name=joined.opponent,
                                          time_control=joined.time_control, fen=joined.fen))
        error = task.error
        if isinstance(error, cli.JoinError):
            self.failure = (error.message, error.hints)
        else:
            self.failure = (f"could not join the game: {error}", [])
        self.mode = "error"
        return None

    def on_leave(self) -> None:
        self._cancel_task()
        try:
            self.worker.stop()
        except Exception:
            pass

    # -- drawing ------------------------------------------------------------------------------

    def body(self, s: Style, width: int, height: int) -> Body:
        spin = s.accent(s.spinner(self.ctx.clock()))
        if self.mode == "connecting" and self.target is not None:
            where = config.format_host(*self.target)
            return Body([spin + " " + s.bold(f"Connecting to {where}…"), "", s.dim("Press Esc to cancel.")])
        if self.mode == "error" and self.failure is not None:
            message, hints = self.failure
            where = config.format_host(*self.target) if self.target else ""
            lines = [s.error(part) for part in _wrap(f"✗ Could not join {where}: {message}", width, "  ")]
            for hint in hints:
                lines.extend("  " + part for part in _wrap(hint, width - 2))
            lines += ["", s.t("Enter: try again   Esc: back to the list   m: type another address")]
            return Body(lines)
        return self._list_body(s, width, spin)

    def _list_body(self, s: Style, width: int, spin: str) -> Body:
        lines: List[str] = []
        cursor: Optional[Tuple[int, int]] = None
        focus = 0
        hosts = [item for item in self.items if item.kind == "host"]
        recent = [item for item in self.items if item.kind == "recent"]
        status = "searching…" if self.rounds == 0 else f"searching · {len(hosts)} found"
        title = s.bold("Games on this network")
        right = spin + " " + s.dim(status)
        gap = width - ui.visible_len(title) - ui.visible_len(right)
        lines.append(title + " " * max(2, gap) + right if gap >= 2 else title)
        name_w = min(20, max([len(item.name) for item in hosts] + [4]))

        def row(item: JoinItem, text: str) -> None:
            nonlocal focus
            selected = item.key == self.selected_key and self.mode == "list"
            if item.key == self.selected_key:
                focus = len(lines)
            if selected:
                lines.append(s.accent(s.marker) + s.selected(" " + ui.truncate(text, width - 4) + " "))
            else:
                lines.append("   " + text)

        if hosts:
            for item in hosts:
                name = ui.pad(ui.truncate(item.name, name_w, s.t("…")), name_w)
                text = f"{name}  {item.address}" + ("  (this computer)" if item.this_computer else "")
                row(item, text)
        elif self.rounds == 0:
            lines.append("   " + s.dim("Looking for games…"))
        else:
            lines.append("   " + s.notice("No games found yet."))
            tips = [
                "Is the other player on the Host screen, waiting?",
                "Both computers must be on the same Wi-Fi or network (not a guest network).",
                f"The host's firewall must allow UDP {net.DISCOVERY_PORT} (search) and "
                f"TCP {self.ctx.settings.port} (the game).",
                "Turn off any VPN on both computers.",
                "Or choose “Enter address manually…” and type the IP shown on the host's screen.",
            ]
            for tip in tips:
                lines.extend("   " + s.dim(part) for part in _wrap(s.t("• " + tip), width - 3, "  "))
        if recent:
            lines += ["", s.bold("Recent")]
            for item in recent:
                row(item, item.address)
        lines.append("")
        for item in self.items:
            if item.kind == "manual":
                if self.mode == "manual":
                    focus = len(lines)
                    field_line, caret = self.manual.render(s, 7, width, True)
                    cursor = (len(lines), caret) if caret is not None else None
                    lines.append(field_line)
                    if self.manual.error:
                        lines.extend("           " + s.error(part)
                                     for part in _wrap("✗ " + self.manual.error, width - 11))
                    lines.append("           " + s.dim(f"IP or IP:PORT, e.g. 192.168.1.23 or 192.168.1.23:{net.DEFAULT_PORT}"))
                else:
                    row(item, s.t("Enter address manually…"))
            elif item.kind == "rescan":
                row(item, "Search again (r)")
        if self.worker_error():
            lines += ["", s.notice(ui.truncate(f"Network search problem: {self.worker_error()}", width))]
        return Body(lines, cursor, focus)

    def worker_error(self) -> str:
        return str(getattr(self.worker, "error", "") or "")

    def hints(self, s: Style) -> str:
        if self.mode == "manual":
            return "type the address · Enter connect · Esc cancel"
        if self.mode == "connecting":
            return "Esc cancel"
        if self.mode == "error":
            return "Enter try again · Esc back"
        return f"{s.updown} move · Enter join · r search again · Esc back"


# -- local ------------------------------------------------------------------------------------


class LocalScreen(FormScreen):
    title = "Local game"

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        saved = ctx.settings
        self.time, self.custom = time_fields(saved.time_control)
        self.flip = ChoiceField("Board", ((True, "turns to the side to move"), (False, "White at the bottom")),
                                saved.flip_local, help="Whether the board turns around after every move.")
        self.start = Button("Start game", self.submit, help="Both players share this keyboard.")
        self.fields = [self.time, self.custom, self.flip, self.start]

    def submit(self) -> Optional[Action]:
        try:
            time_control = resolve_time(self.time, self.custom)
        except ValueError:
            self.focus(self.custom)
            return None
        return start_game(GameRequest(mode="local", time_control=time_control, flip=bool(self.flip.value)))


# -- settings ---------------------------------------------------------------------------------


class SettingsScreen(FormScreen):
    title = "Settings"

    def __init__(self, ctx: MenuContext) -> None:
        super().__init__(ctx)
        saved = ctx.settings
        login = cli._default_name()
        self.name = TextField("Your name", saved.name, placeholder=login, max_len=net.MAX_NAME_LEN, box=22,
                              help=f"The name your opponent sees (empty: your login name, {login}).")
        self.time, self.custom = time_fields(saved.time_control, "Time control")
        self.time.help = "The time control the Host and Local screens start with."
        self.color = ChoiceField("Host colour", COLOR_CHOICES, saved.host_color,
                                 help="The colour you play when you host.")
        self.port = TextField("Port", str(saved.port), max_len=5, allowed="0123456789", box=6,
                              help=f"The TCP port for hosting and joining (default {net.DEFAULT_PORT}).")
        self.pieces = ChoiceField("Pieces", (("unicode", "Chess symbols"), ("ascii", "Letters (N, Q, K)")),
                                  saved.piece_style, help="Letters work on any terminal and font.")
        self.board = ChoiceField("Board size", BOARD_SIZE_CHOICES, saved.board_size,
                                 help="How big the board and pieces are in a game. Auto: the biggest that fits "
                                      "the window. Large and extra large draw big pieces (they need chess "
                                      "symbols; a smaller size is used while the window is too small). "
                                      "/size changes it during a game.")
        self.colors = ChoiceField("Colours", ON_OFF, saved.colors, help="Turn colours off for plain terminals.")
        self.flip = ChoiceField("Flip local board", ON_OFF, saved.flip_local,
                                help="In local games, turn the board to the side to move.")
        self.autosave = ChoiceField("Save games", ON_OFF, saved.autosave,
                                    help="Save every finished game as a PGN file.")
        self.folder = TextField("Games folder", saved.pgn_dir, placeholder=game.DEFAULT_PGN_DIR, max_len=300, box=28,
                                help="Where finished games are saved (empty: ~/lanchess_games).")
        self.save_button = Button("Save", self.save, help=f"Saved in {_short_path(ctx.config_file())}.")
        self.cancel_button = Button("Cancel", self.cancel, help="Leave without saving.")
        self.fields = [self.name, self.time, self.custom, self.color, self.port, self.pieces, self.board,
                       self.colors, self.flip, self.autosave, self.folder, self.save_button, self.cancel_button]

    def collect(self) -> Optional[config.Settings]:
        """The edited settings, or None (with the bad field focused) if something is invalid."""
        try:
            time_control = resolve_time(self.time, self.custom)
        except ValueError:
            self.focus(self.custom)
            return None
        try:
            port = port_value(self.port)
        except ValueError:
            self.focus(self.port)
            return None
        saved = self.ctx.settings
        return config.Settings(
            name=net.sanitize_name(self.name.value, default=""),
            time_control=str(time_control) if time_control else "",
            host_color=self.color.value, port=port, piece_style=self.pieces.value,
            board_size=self.board.value,
            colors=bool(self.colors.value), flip_local=bool(self.flip.value),
            autosave=bool(self.autosave.value), pgn_dir=self.folder.value.strip(),
            recent_hosts=list(saved.recent_hosts))

    def save(self) -> Optional[Action]:
        settings = self.collect()
        if settings is None:
            return None
        try:
            self.ctx.save_settings(settings)
        except OSError as exc:
            self.message = ("error", f"Could not save the settings: {exc.strerror or exc}")
            return None
        return pop(("ok", "Settings saved."))

    def info(self, s: Style) -> str:
        given = [f"--{name.replace('_', '-')}" for name in cli.COMMON_FLAGS if name in self.ctx.explicit]
        if given:
            return "For this run, the command line overrides: " + " ".join(given)
        return f"Settings file: {_short_path(self.ctx.config_file())}"


def _short_path(path: str) -> str:
    return game._display_path(os.path.abspath(path))


# -- help -------------------------------------------------------------------------------------


def help_sections(ctx: MenuContext) -> List[Tuple[str, List[str]]]:
    port = ctx.settings.port
    udp = net.DISCOVERY_PORT
    command = cli.launch_command()
    commands = [line for line in ui.help_lines() if line.startswith("/")]
    return [
        ("Starting a game", [
            "Host a game: this computer waits for the other player. Pick your colour and the time control; "
            "the waiting screen shows the address the other player needs.",
            "Join a game: games on the same network appear in a list. If yours is missing, choose "
            "“Enter address manually…” and type the IP shown on the host's screen.",
            "Local game: two players share one keyboard; the board turns to the side to move.",
            f"From the command line: {command} host, {command} join [IP], {command} local ({command} --help).",
        ]),
        ("Moves", [
            "Type a move and press Enter: e4, Nf3, exd5, O-O, O-O-O, e8=Q (standard notation).",
            "Coordinates work too: e2e4, g1f3, e7e8q. Check marks are optional: Qh4 and Qh4# both work.",
            "Clocks start after the first move. Increments are added after each move.",
        ]),
        ("Commands during a game", commands),
        ("Keys", [
            "Menus: ↑/↓ or j/k move, Enter selects, 1-6 jump on the main menu, Esc or q goes back.",
            "Forms: ←/→ change a choice, type into text boxes, Tab or ↓ goes to the next row.",
            "In a game: ↑/↓ recall earlier input, PgUp/PgDn scroll the messages, Ctrl-L redraws.",
            "/quit leaves a game and returns here; in a running network game, /quit twice resigns.",
        ]),
        ("Network and firewall", [
            "Both computers must be on the same network, such as the same Wi-Fi. Guest networks and VPNs "
            "often block games.",
            f"The game uses TCP port {port} (change it in Settings) and UDP port {udp} to find games.",
            "If the other player cannot connect, open the ports on the host computer:",
            f"  firewalld: sudo firewall-cmd --add-port={port}/tcp --add-port={udp}/udp",
            f"  ufw: sudo ufw allow {port}/tcp && sudo ufw allow {udp}/udp",
            "  Windows and macOS: allow Python (or lanchess) when the firewall asks.",
            "Network search not working? Joining by IP address always works.",
        ]),
        ("Saved games and settings", [
            "Finished games are saved as PGN files in ~/lanchess_games (change it in Settings).",
            f"Settings are stored in {_short_path(ctx.config_file())}.",
        ]),
    ]


class HelpScreen(ScrollingScreen):
    title = "How to play"

    def lines(self, s: Style, width: int) -> List[str]:
        out: List[str] = []
        for heading, paragraphs in help_sections(self.ctx):
            if out:
                out.append("")
            out.append(s.accent(heading))
            for text in paragraphs:
                indent = "  " + " " * (len(text) - len(text.lstrip(" ")))
                out.extend(indent + part for part in _wrap(s.t(text.strip()), width - len(indent), "  "))
        return out

    def body(self, s: Style, width: int, height: int) -> Body:
        return self.scrolled(self.lines(s, width), height)

    def handle_key(self, key: str) -> Optional[Action]:
        if key in _BACK_KEYS or key in ("q", "Q"):
            return pop()
        self.scroll_key(key)
        return None

    def info(self, s: Style) -> str:
        return self.position()

    def hints(self, s: Style) -> str:
        return f"{s.updown} scroll · PgUp/PgDn page · Esc back"


# ---------------------------------------------------------------------------------------------
# Composing frames and the app loop
# ---------------------------------------------------------------------------------------------


def _too_small(s: Style, width: int, height: int) -> str:
    text = [s.notice(line) for line in _wrap(s.t("Terminal too small — enlarge the window"), width - 2)]
    text.append(s.dim(f"(need {MIN_WIDTH}x{MIN_HEIGHT}, now {width}x{height})"))
    top = max(0, (height - len(text)) // 2)
    lines = [""] * top + [ui.pad(line, width, "center").rstrip() for line in text]
    return "\n".join(ui.truncate(line, width) for line in lines[:height])


def _header(s: Style, title: str, width: int) -> List[str]:
    knight = s.p("♞ ", fg=KNIGHT_FG, bold=True) if s.unicode else ""
    left = " " + knight + s.bold("LAN Chess") + s.dim(" › ") + s.accent(title)
    right = s.dim(f"v{__version__} ")
    gap = width - ui.visible_len(left) - ui.visible_len(right)
    line = left + " " * gap + right if gap >= 1 else left
    rule = s.dim(("─" if s.unicode else "-") * width)
    return [ui.truncate(line, width), rule]


def _hints_bar(s: Style, hints: str, width: int) -> str:
    text = ui.pad(" " + ui.truncate(s.t(hints), width - 2, s.t("…")), width)
    if s.color:
        return s.theme.paint(text, fg=ui.STATUS_FG, bg=ui.STATUS_BG)
    return text.rstrip()


def compose(screen: Screen, s: Style, width: int, height: int) -> Tuple[str, Optional[Tuple[int, int]]]:
    """The full frame for ``screen`` (exactly ``height`` lines) and the caret position."""
    width, height = max(1, int(width)), max(1, int(height))
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return _too_small(s, width, height), None
    out = _header(s, screen.title, width) if screen.title else []
    room = height - len(out) - 2  # the footer is an info line and the key hints
    column = min(width - 4, COLUMN_MAX)
    body = screen.body(s, column, room)
    info = screen.info(s)
    footer = [(" " + s.dim(ui.truncate(s.t(info), width - 2, s.t("…")))) if info else "",
              _hints_bar(s, screen.hints(s), width)]
    lines, cursor = list(body.lines), body.cursor
    if len(lines) > room:
        start = max(0, min(body.focus - room // 2, len(lines) - room))
        lines = lines[start:start + room]
        if cursor is not None:
            cursor = (cursor[0] - start, cursor[1]) if 0 <= cursor[0] - start < room else None
    if screen.center:
        block = max([ui.visible_len(line) for line in lines] + [0])
        left = max(0, (width - block) // 2)
        top = max(0, (room - len(lines)) // 2)
    else:
        left = max(0, (width - column) // 2)
        top = 1 if len(lines) < room else 0
    margin = " " * left
    rows = [""] * top + [margin + line if line else "" for line in lines]
    rows = (rows + [""] * room)[:room]
    if cursor is not None:
        cursor = (len(out) + top + cursor[0], left + cursor[1])
    frame = [ui.truncate(line, width) for line in out + rows + footer]
    return "\n".join(frame), cursor


class MenuApp:
    """Runs the screen stack: reads keys, ticks the top screen, applies actions, redraws on change."""

    def __init__(self, ctx: MenuContext, keys: Any, display: Any,
                 size_fn: Optional[Callable[[], Tuple[int, int]]] = None) -> None:
        self.ctx = ctx
        self.keys = keys
        self.display = display
        self.size_fn = size_fn or ctx.size_fn
        self.main = MainMenu(ctx)
        self.stack: List[Screen] = [self.main]
        self.running = True
        self.games = 0
        self._drawn: Any = None
        self._queued: "deque[str]" = deque()

    @property
    def screen(self) -> Screen:
        return self.stack[-1]

    def run(self) -> int:
        try:
            while self.running and self.stack:
                self.step()
        finally:
            self.close()
        return 0

    def step(self) -> None:
        """One loop iteration: tick, draw, wait for one key (up to the screen's poll time)."""
        screen = self.screen
        action = screen.tick()
        if action is None:
            self.render()
            key = self.read_key(screen.poll_timeout)
            if key is None:
                return
            if key == term.CTRL_D and getattr(self.keys, "eof", False):
                self.running = False  # the terminal went away
                return
            action = screen.handle_key(key)
        if action is not None:
            self.apply(action)
        if self.running and self.stack:
            self.render()

    def read_key(self, timeout: float) -> Optional[str]:
        """The next key. ``ALT+x`` counts as Esc and then x: an Esc and the key typed after it can
        reach the menu together (typed quickly, or while a screen was busy), and nothing in the
        menu uses Alt, so this keeps both keys instead of losing them."""
        if self._queued:
            return self._queued.popleft()
        key = self.keys.read_key(timeout)
        if key is not None and key.startswith(term.ALT_PREFIX) and len(key) > len(term.ALT_PREFIX):
            self._queued.append(key[len(term.ALT_PREFIX):])
            return term.ESCAPE
        return key

    def apply(self, action: Action) -> None:
        if action.kind == "push" and action.screen is not None:
            self.stack.append(action.screen)
        elif action.kind == "pop":
            self._leave(self.stack.pop())
            if not self.stack:
                self.running = False
            elif action.message:
                self.main.message = list(action.message)
        elif action.kind == "quit":
            self.running = False
        elif action.kind == "game" and action.request is not None:
            self._unwind()
            self.games += 1
            try:
                self.main.message = self.ctx.play(action.request)
            except Exception as exc:  # a broken game must not take the menu (and the terminal) down
                self.main.message = [("error", f"The game stopped because of an error: "
                                               f"{type(exc).__name__}: {exc}")]
            finally:
                if action.request.conn is not None:
                    _close_quietly(action.request.conn)
                self._drawn = None  # the game drew over the screen

    def _unwind(self) -> None:
        while len(self.stack) > 1:
            self._leave(self.stack.pop())

    @staticmethod
    def _leave(screen: Screen) -> None:
        try:
            screen.on_leave()
        except Exception:
            pass

    def close(self) -> None:
        while self.stack:
            self._leave(self.stack.pop())

    def frame(self) -> Tuple[str, Optional[Tuple[int, int]], Tuple[int, int]]:
        size = self.size_fn()
        frame, cursor = compose(self.screen, self.ctx.style, size[0], size[1])
        return frame, cursor, size

    def render(self) -> None:
        frame, cursor, size = self.frame()
        drawn = (frame, cursor, size)
        if drawn != self._drawn:
            self.display.draw(frame, cursor, size)
            self._drawn = drawn


class _Borrowed:
    """Lends the menu's key reader or screen to a game without letting the game switch it off."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def start(self) -> None:
        pass

    def restore(self) -> None:
        pass

    close = restore

    def enter(self) -> None:
        pass

    def exit(self) -> None:
        pass


def play_in_terminal(ctx: MenuContext, request: GameRequest, reader: Any, display: Any) -> List[Message]:
    """Run a full-screen game on the menu's terminal; the menu takes over again afterwards."""
    session = ctx.make_session(request)
    try:
        game.run_interactive(session, request.conn, ctx.theme, plain=False, key_reader=_Borrowed(reader),
                             screen=_Borrowed(display), size_fn=ctx.size_fn)
    except KeyboardInterrupt:
        pass
    return ctx.game_finished(session)


def run_fullscreen_menu(ctx: MenuContext, reader: Any = None, display: Any = None) -> int:
    """The full-screen menu on this terminal; the terminal is restored however it ends."""
    reader = reader if reader is not None else term.KeyReader(alt_keys=True)  # see MenuApp.read_key
    try:
        reader.start()
    except Exception:  # not a terminal after all (OSError, termios.error)
        return run_plain_menu(ctx)
    display = display if display is not None else term.Screen()
    if ctx.play is MenuContext._no_player:
        ctx.play = lambda request: play_in_terminal(ctx, request, reader, display)
    try:
        with game._exit_on_signals():
            display.enter()
            MenuApp(ctx, reader, display).run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            display.exit()
        finally:
            reader.restore()
    ctx.wait_for_stops(1.0)  # let a search responder release its port before the program ends
    for line in ctx.history:
        print(line)
    return 0


# ---------------------------------------------------------------------------------------------
# The numbered text menu (--plain, or stdin is not a terminal)
# ---------------------------------------------------------------------------------------------


class _EndOfInput(Exception):
    """stdin reached its end: leave the menu."""


class PlainMenu:
    """A line-by-line menu that shares one stdin reader with the games it starts."""

    def __init__(self, ctx: MenuContext, source: Any = None, out: Optional[TextIO] = None) -> None:
        self.ctx = ctx
        self.out = out if out is not None else sys.stdout
        self.source = source if source is not None else term.PlainLineInput(sys.stdin)
        self.unicode = ctx.theme.unicode

    def say(self, *lines: str) -> None:
        for line in lines:
            self._write(line + "\n")

    def _write(self, text: str) -> None:
        if not self.unicode:
            text = text.translate(_ASCII_TABLE)
        try:
            self.out.write(text)
            self.out.flush()
        except UnicodeEncodeError:
            encoding = getattr(self.out, "encoding", None) or "ascii"
            self.out.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            self.out.flush()

    def ask(self, prompt: str) -> Optional[str]:
        """One line from stdin; None after Ctrl-C (go back). Raises _EndOfInput at the end of input."""
        self._write(prompt)
        while True:
            if getattr(self.source, "eof", False):  # a game already read the end of input
                self._write("\n")
                raise _EndOfInput()
            try:
                event = self.source.poll(0.25)
            except KeyboardInterrupt:
                self._write("\n")
                return None
            if event is None:
                continue
            if event[0] == "eof":
                self._write("\n")
                raise _EndOfInput()
            if event[0] == "interrupt":
                self._write("\n")
                return None
            if event[0] == "line":
                return event[1]

    def run(self) -> int:
        try:
            while not getattr(self.source, "eof", False) and self.main_menu():
                pass  # (a game that read the end of input ends the menu too)
        except (_EndOfInput, KeyboardInterrupt):
            pass
        return 0

    def main_menu(self) -> bool:
        """Show the menu and run one choice; False means quit."""
        items = MainMenu.ITEMS
        self.say("", f"LAN Chess {__version__} — {TAGLINE}", "")
        for number, (_key, label, _desc) in enumerate(items, 1):
            self.say(f"  {number}) {label}")
        ips = self.ctx.local_ips()
        self.say("", ip_summary(ips, self.ctx.ips_pending))
        answer = self.ask(f"Choose 1-{len(items)}: ")
        if answer is None:
            return False
        choice = answer.strip().lower()
        aliases = {"h": "host", "j": "join", "l": "local", "s": "settings", "?": "help", "q": "quit", "exit": "quit"}
        keys = [key for key, _label, _desc in items]
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            choice = keys[int(choice) - 1]
        choice = aliases.get(choice, choice)
        if choice == "quit":
            return False
        handler = {"host": self.host, "join": self.join, "local": self.local,
                   "settings": self.settings, "help": self.help}.get(choice)
        if handler is None:
            if choice:
                self.say(f"Please type a number from 1 to {len(items)}.")
            return True
        try:
            handler()
        except KeyboardInterrupt:  # Ctrl-C goes back to the menu here, as it does at every question
            self.say("", "Stopped.")
        return True

    # -- questions ----------------------------------------------------------------------------

    def ask_time(self, default: str) -> Tuple[bool, Optional[game.TimeControl]]:
        shown = default or "untimed"
        while True:
            answer = self.ask(f"Time control, e.g. 5+3 or none (Enter = {shown}): ")
            if answer is None:
                return False, None
            try:
                return True, game.parse_time_control(answer.strip() or default)
            except ValueError as exc:
                self.say(str(exc))

    def ask_choice(self, question: str, choices: Sequence[str], default: str) -> Optional[str]:
        while True:
            answer = self.ask(f"{question} ({'/'.join(choices)}, Enter = {default}): ")
            if answer is None:
                return None
            text = answer.strip().lower() or default
            matches = [choice for choice in choices if choice.startswith(text)]
            if len(matches) == 1:
                return matches[0]
            self.say(f"Please answer {', '.join(choices[:-1])} or {choices[-1]}.")

    def ask_port(self, default: int) -> Optional[int]:
        while True:
            answer = self.ask(f"Port (Enter = {default}): ")
            if answer is None:
                return None
            text = answer.strip() or str(default)
            if text.isdigit() and 1 <= int(text) <= 65535:
                return int(text)
            self.say("Use a port number from 1 to 65535.")

    # -- the choices --------------------------------------------------------------------------

    def host(self) -> None:
        saved = self.ctx.settings
        self.say("", "Host a game")
        ok, time_control = self.ask_time(saved.time_control)
        if not ok:
            return
        color = self.ask_choice("Your colour", ("white", "black", "random"), saved.host_color)
        if color is None:
            return
        args = argparse.Namespace(port=saved.port, name=None, color=color, time=time_control, fen=None,
                                  no_discovery=False)
        cli.run_host(args, self.ctx.options(), saved, input_source=self.source, port_advice="settings")

    def join(self) -> None:
        saved = self.ctx.settings
        self.say("", "Join a game")
        args = argparse.Namespace(host=None, port=saved.port, name=None, scan_time=2.5)
        cli.run_join(args, self.ctx.options(), saved, input_source=self.source, ask=self.ask,
                     config_path=self.ctx.config_path)
        self.ctx.reload_recent_hosts()

    def local(self) -> None:
        saved = self.ctx.settings
        self.say("", "Local game (same keyboard)")
        ok, time_control = self.ask_time(saved.time_control)
        if not ok:
            return
        args = argparse.Namespace(time=time_control, fen=None, no_flip=not saved.flip_local)
        cli.run_local(args, self.ctx.options(), saved, input_source=self.source)

    def help(self) -> None:
        for heading, paragraphs in help_sections(self.ctx):
            self.say("", heading)
            for text in paragraphs:
                indent = "  " + " " * (len(text) - len(text.lstrip(" ")))
                self.say(*(indent + line for line in _wrap(text.strip(), 76 - len(indent), "  ")))

    def _rows(self) -> List[Tuple[str, str]]:
        saved = self.ctx.settings
        on = {True: "on", False: "off"}
        return [
            ("name", f"Your name: {saved.name or cli._default_name() + ' (login name)'}"),
            ("time", f"Time control: {saved.time_control or 'untimed'}"),
            ("color", f"Host colour: {saved.host_color}"),
            ("port", f"Port: {saved.port}"),
            ("pieces", f"Pieces: {'chess symbols' if saved.piece_style == 'unicode' else 'letters'}"),
            ("colors", f"Colours: {on[saved.colors]}"),
            ("flip", f"Flip the board in local games: {on[saved.flip_local]}"),
            ("autosave", f"Save finished games: {on[saved.autosave]}"),
            ("folder", f"Games folder: {saved.pgn_dir or game.DEFAULT_PGN_DIR}"),
        ]

    def settings(self) -> None:
        while True:
            rows = self._rows()
            self.say("", f"Settings (saved in {_short_path(self.ctx.config_file())})")
            for number, (_key, text) in enumerate(rows, 1):
                self.say(f"  {number}) {text}")
            answer = self.ask("Number to change (Enter = back): ")
            if answer is None or not answer.strip():
                return
            text = answer.strip()
            if not (text.isdigit() and 1 <= int(text) <= len(rows)):
                self.say(f"Please type a number from 1 to {len(rows)}.")
                continue
            updated = self._edit(rows[int(text) - 1][0], self.ctx.settings.copy())
            if updated is None:
                continue
            try:
                self.ctx.save_settings(updated)
            except OSError as exc:
                self.say(f"Could not save the settings: {exc.strerror or exc}")
            else:
                self.unicode = self.ctx.theme.unicode
                self.say("Saved.")

    def _edit(self, key: str, settings: config.Settings) -> Optional[config.Settings]:
        if key == "name":
            answer = self.ask("Your name (Enter = keep, - = your login name): ")
            if answer is None or not answer.strip():
                return None
            settings.name = "" if answer.strip() == "-" else net.sanitize_name(answer, default="")
        elif key == "time":
            ok, time_control = self.ask_time(settings.time_control)
            if not ok:
                return None
            settings.time_control = str(time_control) if time_control else ""
        elif key == "color":
            color = self.ask_choice("Host colour", ("white", "black", "random"), settings.host_color)
            if color is None:
                return None
            settings.host_color = color
        elif key == "port":
            port = self.ask_port(settings.port)
            if port is None:
                return None
            settings.port = port
        elif key == "pieces":
            settings.piece_style = "ascii" if settings.piece_style == "unicode" else "unicode"
        elif key == "colors":
            settings.colors = not settings.colors
        elif key == "flip":
            settings.flip_local = not settings.flip_local
        elif key == "autosave":
            settings.autosave = not settings.autosave
        elif key == "folder":
            answer = self.ask("Games folder (Enter = keep, - = the default): ")
            if answer is None or not answer.strip():
                return None
            settings.pgn_dir = "" if answer.strip() == "-" else answer.strip()
        return settings


def run_plain_menu(ctx: MenuContext, source: Any = None, out: Optional[TextIO] = None) -> int:
    return PlainMenu(ctx, source, out).run()


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------


def run_menu(explicit: Optional[Dict[str, Any]] = None, settings: Optional[config.Settings] = None,
             config_path: Optional[str] = None) -> int:
    """``lanchess`` without a sub-command: the full-screen menu on a terminal, else the text menu.

    ``explicit``: the display/saving options given on the command line (they override the saved
    settings for this run).
    """
    explicit = dict(explicit or {})
    settings = settings if settings is not None else config.load(config_path)
    options = cli.Options.resolve(explicit, settings)
    ansi = term.enable_ansi()
    interactive = cli._isatty(sys.stdin) and cli._isatty(sys.stdout)
    if options.plain or not interactive or not ansi:
        if interactive and not ansi and not options.plain:
            print("This terminal does not support the full-screen menu; using the text menu.")
        return run_plain_menu(MenuContext(settings, explicit, config_path, color_ok=False))
    return run_fullscreen_menu(MenuContext(settings, explicit, config_path, color_ok=True))

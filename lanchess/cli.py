"""Command-line entry point for LAN Chess: argument parsing and the host / join / local flows."""

from __future__ import annotations

import argparse
import errno
import getpass
import os
import socket
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__, config, game, net, term, ui
from .engine import STARTING_FEN, Board, color_name


def _codes(*names: str, windows: Sequence[int] = ()) -> frozenset:
    """The values of the errno ``names`` that this platform defines, plus the Windows socket error
    numbers: a socket error on Windows carries its WSAE* number (as errno and as winerror)."""
    return frozenset([getattr(errno, name) for name in names if hasattr(errno, name)] + list(windows))


_ADDRESS_IN_USE = _codes("EADDRINUSE", windows=(10048,))              # WSAEADDRINUSE
_ACCESS_DENIED = _codes("EACCES", "EPERM", windows=(10013,))          # WSAEACCES
_UNREACHABLE = _codes("ENETUNREACH", "EHOSTUNREACH", "EHOSTDOWN", "ENETDOWN",
                      windows=(10051, 10065, 10064, 10050))           # WSAENETUNREACH, WSAEHOSTUNREACH, ...
_TIMED_OUT = _codes("ETIMEDOUT", windows=(10060,))                    # WSAETIMEDOUT

_ASCII_TABLE = str.maketrans({"—": "-", "…": "...", "·": "|"})

EXAMPLES = """\
examples:
  lanchess                            open the menu (host, join or local game, settings)
  lanchess host                       host a game (you play White, untimed)
  lanchess host --time 5+3 --color random
  lanchess join                       search the network for a game
  lanchess join 192.168.1.23          join a host by IP (or HOST:PORT)
  lanchess local --time 10            two players on one keyboard
"""


COMMON_FLAGS = ("ascii", "no_color", "plain", "pgn_dir", "no_save")


@dataclass
class Options:
    """Options shared by every mode."""

    ascii: bool = False
    no_color: bool = False
    plain: bool = False
    pgn_dir: Optional[str] = None
    no_save: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace, settings: Optional[config.Settings] = None) -> "Options":
        """Options from the command line; saved ``settings`` fill in the flags that were not given."""
        return cls.resolve(explicit_flags(args), settings)

    @classmethod
    def resolve(cls, explicit: Dict[str, Any], settings: Optional[config.Settings] = None) -> "Options":
        """Combine explicitly given flags (see ``explicit_flags``) with saved settings."""
        saved = settings if settings is not None else config.Settings()
        return cls(ascii=explicit.get("ascii", saved.piece_style == "ascii"),
                   no_color=explicit.get("no_color", not saved.colors),
                   plain=explicit.get("plain", False),
                   pgn_dir=explicit.get("pgn_dir", saved.pgn_dir or None),
                   no_save=explicit.get("no_save", not saved.autosave))


def explicit_flags(args: argparse.Namespace) -> Dict[str, Any]:
    """The display and saving flags actually typed (their defaults are SUPPRESS, so absent means not given)."""
    return {name: getattr(args, name) for name in COMMON_FLAGS if hasattr(args, name)}


# -- argument parsing -------------------------------------------------------------------------


def _port_arg(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {text!r} (use a number from 1 to 65535)") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"invalid port {text!r} (use a number from 1 to 65535)")
    return value


def _time_arg(text: str) -> Optional[game.TimeControl]:
    try:
        return game.parse_time_control(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _fen_arg(text: str) -> str:
    try:
        return Board(text).fen()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _color_arg(text: str) -> str:
    value = text.strip().lower()
    aliases = {"w": "white", "white": "white", "b": "black", "black": "black", "r": "random", "random": "random"}
    if value not in aliases:
        raise argparse.ArgumentTypeError(f"invalid colour {text!r} (choose white, black or random)")
    return aliases[value]


def _scan_time_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number of seconds {text!r}") from None
    if not 0 < value <= 60:
        raise argparse.ArgumentTypeError("the scan time must be between 0 and 60 seconds")
    return value


def _common_parser() -> argparse.ArgumentParser:
    # Defaults are SUPPRESS so the options work both before and after the sub-command.
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("display and saving")
    group.add_argument("--ascii", action="store_true", default=argparse.SUPPRESS,
                       help="draw pieces as letters instead of chess symbols")
    group.add_argument("--no-color", "--no-colour", dest="no_color", action="store_true",
                       default=argparse.SUPPRESS, help="no colours or other terminal styling")
    group.add_argument("--plain", action="store_true", default=argparse.SUPPRESS,
                       help="simple line-by-line output instead of the full-screen view")
    group.add_argument("--pgn-dir", metavar="DIR", default=argparse.SUPPRESS,
                       help="folder for saved games (default: ~/lanchess_games)")
    group.add_argument("--no-save", action="store_true", default=argparse.SUPPRESS,
                       help="do not save finished games automatically")
    group.add_argument("--version", action="version", version=f"lanchess {__version__}")
    return common


def build_parser(settings: Optional[config.Settings] = None) -> argparse.ArgumentParser:
    """The argument parser; saved ``settings`` (from the menu) supply the defaults of absent options."""
    saved = settings if settings is not None else config.Settings()
    saved_time = saved.time
    time_default = f"default {saved_time}" if saved_time else "default: untimed"
    name_default = f"default {saved.name}" if saved.name else "default: your login name"
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="lanchess", parents=[common], epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Terminal chess for two players on the same network, or two players at one keyboard. "
                    "Run it without a command to open the menu.")
    sub = parser.add_subparsers(dest="command", metavar="{host,join,local}")

    host = sub.add_parser("host", parents=[common], help="host a game and wait for the other player",
                          description="Host a game: print this computer's address and wait for the other player.")
    host.add_argument("--port", type=_port_arg, default=saved.port,
                      help=f"TCP port to listen on (default {saved.port})")
    host.add_argument("--name", help=f"your name as the opponent sees it ({name_default})")
    host.add_argument("--color", "--colour", dest="color", type=_color_arg, default=saved.host_color,
                      metavar="{white,black,random}", help=f"the colour you play (default {saved.host_color})")
    host.add_argument("--time", type=_time_arg, default=saved_time, metavar="TC",
                      help=f"time control MINUTES+SECONDS, e.g. 5+3, 10 or 0.5+0 ({time_default})")
    host.add_argument("--fen", type=_fen_arg, default=None, help="start from this position (FEN)")
    host.add_argument("--no-discovery", action="store_true",
                      help="do not answer network searches (the other player must type your IP)")

    join = sub.add_parser("join", parents=[common], help="join a game on the network",
                          description="Join a game. Without HOST, search the local network for hosts.")
    join.add_argument("host", nargs="?", metavar="HOST[:PORT]", help="the host's IP address or name")
    join.add_argument("--port", type=_port_arg, default=saved.port,
                      help=f"port to use when HOST has no :PORT (default {saved.port})")
    join.add_argument("--name", help=f"your name as the opponent sees it ({name_default})")
    join.add_argument("--scan-time", type=_scan_time_arg, default=2.5, metavar="SECONDS",
                      help="how long to search the network (default 2.5)")

    local = sub.add_parser("local", parents=[common], help="two players at one keyboard (hot seat)",
                           description="Play a local game: both players share this keyboard.")
    local.add_argument("--time", type=_time_arg, default=saved_time, metavar="TC",
                       help=f"time control MINUTES+SECONDS, e.g. 5+3 ({time_default})")
    local.add_argument("--fen", type=_fen_arg, default=None, help="start from this position (FEN)")
    local.add_argument("--no-flip", action="store_true", default=not saved.flip_local,
                       help="keep White at the bottom instead of turning the board to the side to move")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None,
               settings: Optional[config.Settings] = None) -> argparse.Namespace:
    """Parse the command line (raises SystemExit on --help, --version or errors)."""
    return build_parser(settings).parse_args(argv)


# -- helpers ----------------------------------------------------------------------------------


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _setup_display(options: Options) -> Tuple[ui.Theme, bool]:
    """Pick the theme and decide between full-screen and plain output."""
    ansi = term.enable_ansi()
    interactive = _isatty(sys.stdin) and _isatty(sys.stdout)
    plain = options.plain or not interactive or not ansi
    if interactive and not ansi and not options.plain:
        print("This terminal does not support the full-screen view; using plain mode.")
    return make_theme(options, color_ok=ansi and not plain), plain


def make_theme(options: Options, color_ok: bool) -> ui.Theme:
    """The theme for ``options`` (``color_ok``: the output is a terminal that understands colours)."""
    color = color_ok and not options.no_color and not os.environ.get("NO_COLOR")
    unicode = not options.ascii and term.supports_unicode()
    return ui.Theme(unicode=unicode, color=color)


def _text(theme: ui.Theme, text: str) -> str:
    return text if theme.unicode else text.translate(_ASCII_TABLE)


def _say(theme: ui.Theme, *lines: str) -> None:
    for line in lines:
        print(_text(theme, line))
    sys.stdout.flush()


def _fail(message: str, *hints: str) -> int:
    print(f"Error: {message}", file=sys.stderr)
    for hint in hints:
        print(hint, file=sys.stderr)
    return 1


def _default_name() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    return net.sanitize_name(name, default="Player")


def _player_name(value: Optional[str], saved: str = "") -> str:
    """The name to play under: ``--name``, else the saved name, else the login name."""
    if value is not None:
        return net.sanitize_name(value, default=_default_name())
    return net.sanitize_name(saved, default=_default_name())


def launch_command() -> str:
    """How the user started this program, for the join instructions shown by the host."""
    python = "py" if os.name == "nt" else "python3"
    base = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if base.endswith(".pyz"):
        return f"{python} {base}"
    if base == "play.py":
        return f"{python} play.py"
    if base in ("lanchess", "lanchess.exe", "lanchess-script.py"):
        return "lanchess"
    return f"{python} -m lanchess"


def firewall_hint(port: int) -> str:
    return (f"If the host is running, its firewall may be blocking the game: allow incoming TCP {port} "
            f"(and UDP {net.DISCOVERY_PORT} for network search) on the host computer.")


_OTHER_PORT = {  # how to host on another port: with --port, on the menu's Host form, or in Settings
    "flag": ("Host on a different port, for example: --port {port}, and join with HOST:{port}.",
             "try --port {port}"),
    "form": ("Go back and pick another port, for example {port}; the other player then joins IP:{port}.",
             "go back and pick another port, for example {port}"),
    "settings": ("Change the port in Settings, for example to {port}; the other player then joins IP:{port}.",
                 "change the port in Settings, for example to {port}"),
}


def _error_is(exc: OSError, codes: frozenset) -> bool:
    """Whether ``exc`` is one of ``codes``, by its errno or (on Windows) its winerror."""
    return any(code in codes for code in (getattr(exc, "errno", None), getattr(exc, "winerror", None))
               if isinstance(code, int))


def server_error(exc: OSError, port: int, advice: str = "flag") -> Tuple[str, List[str]]:
    """The message and hints for a port that cannot be listened on.

    ``advice`` is how the user picks another port here: ``"flag"`` (the --port option),
    ``"form"`` (the Host screen of the menu) or ``"settings"`` (Settings in the text menu).
    """
    in_use, try_port = _OTHER_PORT.get(advice, _OTHER_PORT["flag"])
    other = 6000 if port != 6000 else 6001
    if _error_is(exc, _ADDRESS_IN_USE):
        return (f"port {port} is already in use (another LAN Chess game or another program is using it).",
                [in_use.format(port=other)])
    if isinstance(exc, PermissionError) or _error_is(exc, _ACCESS_DENIED):
        if port < 1024:
            reason, example = "Ports below 1024 need administrator rights", net.DEFAULT_PORT
        else:  # e.g. a port Windows reserves, or a security policy
            reason, example = "This computer does not let programs use that port", other
        return f"not allowed to use port {port}.", [f"{reason}; {try_port.format(port=example)}."]
    return f"could not listen on port {port}: {exc.strerror or exc}", []


def connect_error(exc: OSError, host: str, port: int) -> Tuple[str, List[str]]:
    target = f"{host}:{port}"
    if isinstance(exc, socket.gaierror):
        return (f"cannot find the computer {host!r}.",
                ["Check the address. The host prints its IP address, for example 192.168.1.23."])
    if isinstance(exc, ConnectionRefusedError):
        return (f"connection refused by {target}.",
                ["Nobody is hosting a game there. Start hosting on the other computer first "
                 "(Host a game in the menu, or the 'host' command), and check the IP address and port."])
    if isinstance(exc, socket.timeout) or _error_is(exc, _TIMED_OUT):
        return (f"timed out connecting to {target}.",
                [firewall_hint(port), "Also check that both computers are on the same network."])
    if _error_is(exc, _UNREACHABLE):
        return (f"{target} is unreachable.",
                ["Check that both computers are on the same network (and not on a guest Wi-Fi).",
                 firewall_hint(port)])
    return f"could not connect to {target}: {exc.strerror or exc}", [firewall_hint(port)]


def _play(session: game.GameSession, conn: Any, theme: ui.Theme, plain: bool, input_source: Any = None) -> int:
    try:
        game.run_interactive(session, conn, theme, plain=plain, input_source=input_source)
    except KeyboardInterrupt:
        pass
    for line in session.summary_lines():
        print(line)
    return 0


# -- host -------------------------------------------------------------------------------------


def _host_banner(theme: ui.Theme, name: str, args: argparse.Namespace, port: int, discovery: bool) -> None:
    colour = {"white": "you play White", "black": "you play Black", "random": "colours are picked at random"}
    timing = f"time control {args.time}" if args.time else "untimed"
    command = launch_command()
    suffix = "" if port == net.DEFAULT_PORT else f":{port}"
    lines = [f"LAN Chess {__version__} — hosting as {name} · {colour[args.color]} · {timing}",
             f"Waiting for an opponent on TCP port {port}.", "",
             "On the other computer, run:"]
    addresses = net.local_ip_addresses()
    if addresses:
        width = max(len(f"{command} join {address}{suffix}") for _iface, address in addresses)
        for iface, address in addresses:
            join = f"{command} join {address}{suffix}"
            lines.append(f"    {join.ljust(width)}" + (f"   ({iface})" if iface else ""))
    else:
        lines.append(f"    {command} join <this computer's IP address>{suffix}")
        lines.append("    (could not detect the IP address; see 'ip addr' / 'ipconfig')")
    if discovery:
        lines.append(f"or simply:  {command} join      (searches the network)")
    lines += ["", f"Firewall: allow incoming TCP {port} and UDP {net.DISCOVERY_PORT} on this computer "
                  "if the other player cannot connect.",
              "Press Ctrl-C to stop waiting."]
    _say(theme, *lines)


def _wait_for_opponent(server: net.Server, theme: ui.Theme, name: str,
                       args: argparse.Namespace, fen: str) -> Tuple[net.Connection, str, str]:
    while True:
        conn = server.accept(0.5)
        if conn is None:
            continue
        _say(theme, f"Connection from {conn.peer}…")
        try:
            opponent, host_color = net.server_handshake(conn, name, args.color, args.time, fen, timeout=10.0)
        except net.HandshakeError as exc:
            conn.close()
            _say(theme, f"That connection failed: {exc}. Still waiting…")
            continue
        except BaseException:
            conn.close()
            raise
        return conn, opponent, host_color


def run_host(args: argparse.Namespace, options: Options, settings: Optional[config.Settings] = None,
             input_source: Any = None, port_advice: str = "flag") -> int:
    """Host a game from the command line.

    ``input_source``: the plain-mode line reader to reuse; ``port_advice``: how the user picks
    another port if this one cannot be used (see ``server_error``).
    """
    theme, plain = _setup_display(options)
    name = _player_name(args.name, settings.name if settings else "")
    fen = args.fen or STARTING_FEN
    try:
        server = net.Server(port=args.port)
    except OSError as exc:
        message, hints = server_error(exc, args.port, port_advice)
        return _fail(message, *hints)
    responder: Optional[net.DiscoveryResponder] = None
    try:
        discovery = not args.no_discovery
        if discovery:
            responder = net.DiscoveryResponder(name, server.port)
            if not responder.start():
                responder = None
                discovery = False
        _host_banner(theme, name, args, server.port, discovery)
        if not args.no_discovery and responder is None:
            _say(theme, f"Note: network search is unavailable (UDP port {net.DISCOVERY_PORT} is busy), "
                        "so the other player must type one of the addresses above.")
        try:
            conn, opponent, host_color = _wait_for_opponent(server, theme, name, args, fen)
        except KeyboardInterrupt:
            _say(theme, "", "Stopped waiting. No game was played.")
            return 130
        except OSError as exc:
            return _fail(f"stopped listening for players: {exc.strerror or exc}")
    finally:
        if responder is not None:
            responder.stop()
        server.close()
    _say(theme, f"{opponent} joined from {conn.peer}. You play {color_name(host_color)}. Starting the game…")
    session = game.GameSession(mode="network", my_color=host_color, my_name=name, opponent_name=opponent,
                               conn=conn, time_control=args.time, fen=fen, pgn_dir=options.pgn_dir,
                               autosave=not options.no_save, unicode=theme.unicode)
    return _play(session, conn, theme, plain, input_source)


# -- join -------------------------------------------------------------------------------------


def _ask(prompt: str) -> Optional[str]:
    try:
        return input(prompt)
    except EOFError:
        print()
        return None


LOOPBACK = "127.0.0.1"
LOOPBACK_BROADCAST = "127.255.255.255"
# Probed besides the LAN broadcasts, so games hosted on this computer are found even when a firewall
# drops broadcast replies. The loopback broadcast reaches every game hosted here (they share the
# discovery port); 127.0.0.1 alone reaches only one of them, and is kept for systems without it.
LOOPBACK_TARGETS = (LOOPBACK, LOOPBACK_BROADCAST)


def _without_loopback_duplicates(hosts: List[net.HostInfo]) -> List[net.HostInfo]:
    """Drop 127.0.0.1 answers from a host that also answered on a LAN address (same name and port).

    127.0.0.1 is probed so that a game hosted on this computer is found even when a firewall drops
    broadcasts; when broadcasts work, the same host would otherwise be listed twice.
    """
    seen = {(host.name, host.port) for host in hosts if host.address != LOOPBACK}
    return [host for host in hosts if host.address != LOOPBACK or (host.name, host.port) not in seen]


def choose_host(theme: ui.Theme, scan_time: float, default_port: int,
                discover: Callable[..., List[net.HostInfo]] = net.discover_hosts,
                ask: Callable[[str], Optional[str]] = _ask) -> Optional[Tuple[str, int]]:
    """Search the LAN and let the user pick a host or type an address. None means give up."""
    while True:
        _say(theme, f"Searching the local network for LAN Chess games ({scan_time:g} s)…")
        hosts = _without_loopback_duplicates(discover(timeout=scan_time, extra_targets=LOOPBACK_TARGETS))
        if hosts:
            _say(theme, "Games found:")
            for number, host in enumerate(hosts, 1):
                where = "   (this computer)" if host.address == LOOPBACK else ""
                _say(theme, f"  {number}) {host.name:<20}  {host.address}:{host.port}{where}")
            prompt = "Pick a number (Enter = 1), type an address, or r to search again: "
        else:
            _say(theme, "No games found. Make sure the other computer is running 'host' on the same network.",
                 f"Network search needs UDP port {net.DISCOVERY_PORT} open on the host, and some networks "
                 "block it; joining by IP address works anyway.")
            prompt = "Type the host's IP address (it is shown on the host's screen), or press Enter to search again: "
        answer = ask(_text(theme, prompt))
        if answer is None:
            return None
        answer = answer.strip()
        lowered = answer.lower()
        if lowered in ("q", "quit", "exit"):
            return None
        if hosts and not answer:
            return hosts[0].address, hosts[0].port
        if not answer or lowered in ("r", "rescan", "search"):
            continue
        if answer.isdigit() and len(answer) <= 3:
            index = int(answer)
            if 1 <= index <= len(hosts):
                return hosts[index - 1].address, hosts[index - 1].port
            _say(theme, f"There is no game number {index}.")
            continue
        try:
            return net.parse_host_port(answer, default_port)
        except ValueError as exc:
            _say(theme, str(exc))


class JoinError(Exception):
    """Joining failed; ``message`` and ``hints`` are ready to show to the user."""

    def __init__(self, message: str, hints: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.hints = list(hints)


@dataclass
class Joined:
    """A connection that completed the handshake, with the game settings the host sent."""

    conn: Any
    opponent: str
    my_color: str
    time_control: Optional[game.TimeControl]
    fen: str


def join_host(host: str, port: int, name: str, *, connect: Optional[Callable[..., Any]] = None,
              handshake: Optional[Callable[..., Dict[str, Any]]] = None, timeout: float = 10.0) -> Joined:
    """Connect to a host and do the handshake; raises JoinError with friendly text on failure.

    ``connect`` and ``handshake`` default to net.connect and net.client_handshake.
    """
    connect = connect or net.connect
    handshake = handshake or net.client_handshake
    try:
        conn = connect(host, port, timeout=timeout)
    except OSError as exc:
        message, hints = connect_error(exc, host, port)
        raise JoinError(message, hints) from exc
    try:
        welcome = handshake(conn, name, timeout=timeout)
        time_control = game.TimeControl.from_dict(welcome.get("time_control"))
        fen = Board(welcome.get("fen") or STARTING_FEN).fen()
    except net.HandshakeError as exc:
        conn.close()
        raise JoinError(f"could not join the game: {exc}") from exc
    except ValueError as exc:
        conn.close()
        raise JoinError(f"the host sent invalid game settings ({exc}).") from exc
    except BaseException:
        conn.close()
        raise
    return Joined(conn, welcome["name"], welcome["your_color"], time_control, fen)


def run_join(args: argparse.Namespace, options: Options, settings: Optional[config.Settings] = None,
             input_source: Any = None, ask: Optional[Callable[[str], Optional[str]]] = None,
             config_path: Optional[str] = None) -> int:
    """Join a game from the command line.

    ``input_source`` and ``ask`` let the text menu share its stdin reader; with ``settings`` the
    host is remembered in the recent hosts (in ``config_path``, default: the usual config file).
    """
    theme, plain = _setup_display(options)
    name = _player_name(args.name, settings.name if settings else "")
    if args.host:
        try:
            host, port = net.parse_host_port(args.host, args.port)
        except ValueError as exc:
            return _fail(str(exc))
    else:
        try:
            picked = choose_host(theme, args.scan_time, args.port, ask=ask or _ask)
        except KeyboardInterrupt:  # (like Ctrl-C while hosting: say so, and let a menu carry on)
            _say(theme, "", "Stopped searching. No game was played.")
            return 130
        if picked is None:
            print("No game selected.")
            return 1
        host, port = picked
    _say(theme, f"Connecting to {host}:{port}…")
    try:
        joined = join_host(host, port, name)
    except JoinError as exc:
        return _fail(exc.message, *exc.hints)
    except KeyboardInterrupt:
        _say(theme, "", "Stopped connecting. No game was played.")
        return 130
    if settings is not None:
        config.remember_host(host, port, config_path)
    conn, opponent, my_color = joined.conn, joined.opponent, joined.my_color
    time_control = joined.time_control
    timing = f", time control {time_control}" if time_control else ", untimed"
    _say(theme, f"Joined {opponent}'s game. You play {color_name(my_color)}{timing}. Starting…")
    session = game.GameSession(mode="network", my_color=my_color, my_name=name, opponent_name=opponent,
                               conn=conn, time_control=time_control, fen=joined.fen, pgn_dir=options.pgn_dir,
                               autosave=not options.no_save, unicode=theme.unicode)
    return _play(session, conn, theme, plain, input_source)


# -- local ------------------------------------------------------------------------------------


def run_local(args: argparse.Namespace, options: Options, settings: Optional[config.Settings] = None,
              input_source: Any = None) -> int:
    """Play a hot-seat game (``input_source``: the plain-mode line reader to reuse)."""
    theme, plain = _setup_display(options)
    session = game.GameSession(mode="local", my_color=None, my_name="White", opponent_name="Black",
                               time_control=args.time, fen=args.fen or STARTING_FEN, pgn_dir=options.pgn_dir,
                               autosave=not options.no_save, flip=not args.no_flip, unicode=theme.unicode)
    return _play(session, None, theme, plain, input_source)


_RUNNERS: Dict[str, Callable[..., int]] = {
    "host": run_host, "join": run_join, "local": run_local,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point; returns the process exit code.

    Without a sub-command the interactive menu opens (full-screen on a terminal, numbered text
    otherwise). Saved settings provide the defaults; options on the command line win.
    """
    settings = config.load()
    parser = build_parser(settings)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    command = getattr(args, "command", None)
    try:
        if not command:
            from . import menu  # imported here: the menu builds on this module

            return menu.run_menu(explicit_flags(args), settings)
        return _RUNNERS[command](args, Options.from_args(args, settings), settings)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

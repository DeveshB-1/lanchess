"""Game logic for LAN Chess: time controls, chess clocks, the GameSession controller and run loops.

GameSession is independent of the terminal: it takes input lines (``handle_input``) and network
messages (``handle_message``), keeps the log and state, and produces a ``ui.ViewState``. The run loops
(``run_interactive``) connect it to the keyboard, the screen and the connection.
"""

from __future__ import annotations

import math
import os
import queue
import re
import signal
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, TextIO, Tuple

from . import __version__, term, ui
from .engine import BLACK, STARTING_FEN, WHITE, Board, IllegalMoveError, Move, Outcome, color_name, opposite
from .net import ConnectionClosed

__all__ = [
    "TimeControl", "parse_time_control", "ChessClock", "GameSession", "run_interactive",
    "NOT_A_MOVE", "CHAT_MAX_LEN", "QUIT_CONFIRM_SECONDS", "DEFAULT_PGN_DIR",
]

CHAT_MAX_LEN = 500
QUIT_CONFIRM_SECONDS = 10.0
LOG_LIMIT = 1000
DEFAULT_PGN_DIR = os.path.join("~", "lanchess_games")
NOT_A_MOVE = "Not a move. Type /help for commands, /c <msg> to chat."
MAX_INITIAL_MINUTES = 24 * 60
MAX_INCREMENT_SECONDS = 10 * 60

# ---------------------------------------------------------------------------------------------
# Time controls and clocks
# ---------------------------------------------------------------------------------------------

_NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
_TC_RE = re.compile(r"^(%s)\s*(?:[+|]\s*(%s))?$" % (_NUMBER, _NUMBER))
_UNTIMED_WORDS = frozenset(("", "none", "0", "off", "no", "untimed", "unlimited", "-"))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return ("%.3f" % value).rstrip("0").rstrip(".")


def _coerce_ms(value: Any, name: str) -> int:
    if _is_int(value):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"Invalid time control: {name} must be a whole number of milliseconds")


@dataclass(frozen=True)
class TimeControl:
    """Starting time per player plus the increment added after each move (both in ms)."""

    initial_ms: int
    increment_ms: int = 0

    def __post_init__(self) -> None:
        if not _is_int(self.initial_ms) or not _is_int(self.increment_ms):
            raise ValueError("Invalid time control: times must be whole milliseconds")
        if self.initial_ms <= 0:
            raise ValueError("Invalid time control: the starting time must be more than zero")
        if self.increment_ms < 0:
            raise ValueError("Invalid time control: the increment cannot be negative")

    def to_dict(self) -> Dict[str, int]:
        return {"initial_ms": self.initial_ms, "increment_ms": self.increment_ms}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["TimeControl"]:
        """Build from ``{"initial_ms", "increment_ms"}``; None stays None. Raises ValueError."""
        if data is None:
            return None
        if isinstance(data, TimeControl):
            return data
        if not isinstance(data, dict):
            raise ValueError(f"Invalid time control: {data!r}")
        increment = data.get("increment_ms", 0)
        return cls(_coerce_ms(data.get("initial_ms"), "initial_ms"),
                   _coerce_ms(0 if increment is None else increment, "increment_ms"))

    def __str__(self) -> str:
        return "%s+%s" % (_format_number(self.initial_ms / 60000.0), _format_number(self.increment_ms / 1000.0))

    def pgn_tag(self) -> str:
        """PGN TimeControl tag value: seconds + increment seconds, e.g. ``300+3``."""
        return "%s+%s" % (_format_number(self.initial_ms / 1000.0), _format_number(self.increment_ms / 1000.0))

    def describe(self) -> str:
        """Human text such as ``5 min + 3 s per move``."""
        if self.initial_ms >= 60000:
            base = "%s min" % _format_number(self.initial_ms / 60000.0)
        else:
            base = "%s s" % _format_number(self.initial_ms / 1000.0)
        if self.increment_ms:
            return "%s + %s s per move" % (base, _format_number(self.increment_ms / 1000.0))
        return base + " each"


def parse_time_control(text: Any) -> Optional[TimeControl]:
    """Parse ``"5+3"`` (minutes + increment seconds), ``"10"``, ``"0.5+0"``; ``none``/``0``/``""`` -> None.

    Raises ValueError with a user-friendly message for anything else.
    """
    if text is None:
        return None
    if isinstance(text, TimeControl):
        return text
    raw = str(text).strip()
    value = raw.lower()
    if value in _UNTIMED_WORDS:
        return None
    match = _TC_RE.match(value)
    if not match:
        raise ValueError(f"Invalid time control {raw!r}: use MINUTES+SECONDS, for example 5+3, 10 or 0.5+0")
    minutes = float(match.group(1))
    seconds = float(match.group(2) or 0)
    if minutes > MAX_INITIAL_MINUTES:
        raise ValueError(f"Invalid time control {raw!r}: at most {MAX_INITIAL_MINUTES} minutes per player")
    if seconds > MAX_INCREMENT_SECONDS:
        raise ValueError(f"Invalid time control {raw!r}: the increment can be at most {MAX_INCREMENT_SECONDS} s")
    initial_ms = int(round(minutes * 60000))
    increment_ms = int(round(seconds * 1000))
    if initial_ms == 0 and increment_ms == 0:
        return None
    if initial_ms < 1000:
        raise ValueError(f"Invalid time control {raw!r}: the starting time must be at least 1 second")
    return TimeControl(initial_ms, increment_ms)


class ChessClock:
    """Two-player chess clock. Nothing runs until the first ``press`` (the first move of the game)."""

    def __init__(self, tc: TimeControl, now_fn: Callable[[], float] = time.monotonic) -> None:
        self.tc = tc
        self._now = now_fn
        self._remaining: Dict[str, float] = {WHITE: float(tc.initial_ms), BLACK: float(tc.initial_ms)}
        self.running: Optional[str] = None
        self._since = 0.0

    def _elapsed_ms(self) -> float:
        return (self._now() - self._since) * 1000.0

    def remaining(self, color: str) -> int:
        """Live remaining time in ms (may be negative once flagged)."""
        ms = self._remaining[color]
        if self.running == color:
            ms -= self._elapsed_ms()
        return int(math.floor(ms + 1e-6))  # tolerate float noise from the time source

    def start(self, color: str) -> None:
        """Start ``color``'s clock (stopping the other one)."""
        self.stop()
        self.running = color
        self._since = self._now()

    def stop(self) -> None:
        """Stop whichever clock runs, keeping the time used."""
        if self.running is not None:
            self._remaining[self.running] -= self._elapsed_ms()
            self.running = None

    def press(self, color_moved: str) -> int:
        """The mover finished a move: stop their clock, add the increment, start the opponent's.

        Returns the mover's remaining ms after the increment.
        """
        self.stop()
        left = self.remaining(color_moved) + self.tc.increment_ms
        self._remaining[color_moved] = float(left)  # whole ms, exactly what is sent to the opponent
        self.start(opposite(color_moved))
        return left

    def set_remaining(self, color: str, ms: float) -> None:
        """Overwrite a clock (used to adopt the opponent's authoritative time)."""
        self._remaining[color] = float(ms)
        if self.running == color:
            self._since = self._now()

    def flagged(self) -> Optional[str]:
        """The running color if its time is used up, else None."""
        if self.running is not None and self.remaining(self.running) <= 0:
            return self.running
        return None

    def snapshot(self) -> Dict[str, int]:
        return {WHITE: self.remaining(WHITE), BLACK: self.remaining(BLACK)}


# ---------------------------------------------------------------------------------------------
# Game session
# ---------------------------------------------------------------------------------------------

_TERMINATION_TAGS = {"timeout": "time forfeit", "timeout_insufficient": "time forfeit", "abandoned": "abandoned"}
_OFFER_LABELS = {"draw": "draw offer", "takeback": "takeback request", "rematch": "rematch offer"}
_INCOMING_TEXT = {
    "draw": "{} offers a draw — /accept or /decline",
    "takeback": "{} asks to take back a move — /accept or /decline",
    "rematch": "{} wants a rematch — /accept or /decline",
}
_OUTGOING_TEXT = {
    "draw": "Draw offered — waiting for {}",
    "takeback": "Takeback requested — waiting for {}",
    "rematch": "Rematch offered — waiting for {}",
}
_ASCII_TABLE = str.maketrans({"—": "-", "–": "-", "…": "...", "·": "|", "’": "'", "‘": "'",
                              "“": '"', "”": '"'})
_FILENAME_UNSAFE = re.compile(r"[^\w.-]+")


def _one_line(text: Any, limit: int) -> str:
    """Untrusted text as a single line: control/format characters removed, whitespace collapsed."""
    if not isinstance(text, str):
        return ""
    kept = "".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    return " ".join(kept.split())[:limit]


def _display_path(path: str) -> str:
    """``path`` for display, with the home folder shortened to ``~``."""
    home = os.path.expanduser("~")
    if home and home not in ("~", os.sep) and path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + path[len(home.rstrip(os.sep)):]
    return path


def _filename_part(name: str) -> str:
    cleaned = _FILENAME_UNSAFE.sub("_", name).strip("._-")
    cleaned = re.sub(r"_+", "_", cleaned)[:30].strip("._-")
    return cleaned or "Player"


class GameSession:
    """The game controller for one connection (or one local hot-seat board), across rematches."""

    def __init__(self, *, mode: str, my_color: Optional[str] = None, my_name: str = "Player",
                 opponent_name: str = "Opponent", conn: Any = None, time_control: Any = None,
                 fen: Optional[str] = STARTING_FEN, now_fn: Callable[[], float] = time.monotonic,
                 pgn_dir: Optional[str] = None, autosave: bool = True, flip: bool = True,
                 unicode: bool = True, board_size: str = "auto") -> None:
        if mode not in ("network", "local"):
            raise ValueError(f"mode must be 'network' or 'local', not {mode!r}")
        if mode == "network" and my_color not in (WHITE, BLACK):
            raise ValueError("a network game needs my_color 'w' or 'b'")
        self.mode = mode
        self.local = mode == "local"
        self.my_color: Optional[str] = None if self.local else my_color
        self.my_name = my_name or ("White" if self.local else "Player")
        self.opponent_name = opponent_name or ("Black" if self.local else "Opponent")
        self.conn = None if self.local else conn
        self.connected = self.conn is not None
        self.peer = str(getattr(self.conn, "peer", "") or "")
        if time_control is not None and not isinstance(time_control, TimeControl):
            time_control = TimeControl.from_dict(time_control)
        self.time_control: Optional[TimeControl] = time_control
        self.start_fen = fen or STARTING_FEN
        self.now_fn = now_fn
        self.pgn_dir = pgn_dir
        self.autosave = autosave
        self.auto_flip = bool(flip) and self.local
        self.unicode = unicode
        self.board_size = ui.normalize_board_size(board_size)  # auto, small, medium, large or xl (/size)
        self.quit_requested = False
        self.log: List[Tuple[str, str]] = []
        self.log_total = 0
        self.log_scroll = 0
        self.saved_paths: List[str] = []
        self.game_number = 0
        self.dirty = True
        self._start_game(self.start_fen)
        self._welcome()
        self._check_start_position()

    # -- state --------------------------------------------------------------------------------

    def _start_game(self, fen: str) -> None:
        self.board = Board(fen)
        self.clock: Optional[ChessClock] = (ChessClock(self.time_control, self.now_fn)
                                            if self.time_control is not None else None)
        self.over: Optional[Outcome] = None
        self.incoming: Dict[str, Dict[str, Any]] = {}
        self.outgoing: Dict[str, Dict[str, Any]] = {}
        self._draw_offered_at: Optional[int] = None
        self._quit_deadline: Optional[float] = None
        self.flipped = False
        self.started_at = time.time()
        self.game_number += 1
        self.dirty = True

    def _check_start_position(self) -> None:
        """A custom start position can already be decided (mate, stalemate, bare kings): end at once.

        Both sides run the same check on the same FEN, so they stay in agreement.
        """
        outcome = self.board.outcome()
        if outcome is not None:
            self._log("system", "The starting position is already decided, so there is nothing to play.")
            self._end(outcome)

    @property
    def opponent_color(self) -> Optional[str]:
        return None if self.my_color is None else opposite(self.my_color)

    @property
    def white_name(self) -> str:
        if self.local or self.my_color == WHITE:
            return self.my_name
        return self.opponent_name

    @property
    def black_name(self) -> str:
        if self.local or self.my_color == WHITE:
            return self.opponent_name
        return self.my_name

    def player_name(self, color: str) -> str:
        return self.white_name if color == WHITE else self.black_name

    @property
    def perspective(self) -> str:
        if self.local:
            base = self.board.turn if self.auto_flip else WHITE
        else:
            base = self.my_color or WHITE
        return opposite(base) if self.flipped else base

    @property
    def my_turn(self) -> bool:
        return self.local or self.board.turn == self.my_color

    def _t(self, text: str) -> str:
        return text if self.unicode else text.translate(_ASCII_TABLE)

    def _log(self, kind: str, text: str) -> None:
        self.log.append((kind, self._t(text)))
        self.log_total += 1
        if len(self.log) > LOG_LIMIT:
            del self.log[: len(self.log) - LOG_LIMIT]
        self.dirty = True

    def _error(self, text: str) -> None:
        self._log("error", text)

    def _welcome(self) -> None:
        self._log("system", f"LAN Chess {__version__}")
        timing = (f"Time control {self.time_control} ({self.time_control.describe()}); "
                  "clocks start after the first move." if self.time_control else "Untimed game.")
        if self.local:
            self._log("info", "Local game: two players share this keyboard. " + timing)
        else:
            where = f" ({self.peer})" if self.peer else ""
            self._log("info", f"You play {color_name(self.my_color)} against {self.opponent_name}{where}. "
                              + timing)
        self._log("info", "Type a move like e4, Nf3, exd5, O-O or e2e4 and press Enter.")
        if self.local:
            self._log("info", "/takeback undoes a move, /draw ends in a draw, /resign resigns for the side "
                              "to move. /help lists all commands.")
        else:
            self._log("info", "Chat with /c <message>. /help lists all commands.")
            self._log("game", "Your move." if self.my_turn else f"{self.opponent_name} moves first.")

    # -- input --------------------------------------------------------------------------------

    def handle_input(self, line: str) -> None:
        """Process one line typed by the user (a move, a command or ``?``)."""
        text = (line or "").strip()
        if not text:
            return
        if text == "?":
            self._cmd_help("")
        elif text.startswith("/"):
            name, _, arg = text[1:].partition(" ")
            self._run_command(name.strip().lower(), arg.strip())
        else:
            self._try_move(text)

    def _run_command(self, name: str, arg: str) -> None:
        handler = _COMMANDS.get(name)
        if handler is None:
            if name:
                self._error(f"Unknown command: /{name}. Type /help for the list of commands.")
            else:
                self._error("Type a command after the slash, for example /help.")
            return
        handler(self, arg)

    def _try_move(self, text: str) -> None:
        board = self.board
        parse_error: Optional[IllegalMoveError] = None
        move: Optional[Move] = None
        try:
            move = board.parse_move(text)
        except IllegalMoveError as exc:
            if exc.kind == "unrecognized":
                self._not_a_move(text)
                return
            parse_error = exc
        except ValueError:
            self._not_a_move(text)
            return
        if self.over is not None:
            self._error("The game is over. " + self._after_game_hint())
            return
        mover = board.turn
        if not self.local:
            if mover != self.my_color:
                self._error(f"It's not your turn — waiting for {self.opponent_name} to move.")
                return
            if "takeback" in self.outgoing:
                self._error(f"Waiting for {self.opponent_name} to answer your takeback request.")
                return
        if self.clock is not None and self.clock.running == mover and self.clock.remaining(mover) <= 0:
            self._flag(mover)
            return
        if parse_error is not None:
            self._error(self._illegal_text(parse_error))
            return
        assert move is not None
        self._apply_move(move, mover, send=not self.local)

    def _not_a_move(self, text: str) -> None:
        word = text.split()[0].lower() if text.split() else ""
        if word in _COMMANDS and len(word) > 1:
            self._error(f"Not a move. Did you mean /{text.strip()}? Commands start with a slash; /help lists them.")
        else:
            self._error(NOT_A_MOVE)

    def _illegal_text(self, exc: IllegalMoveError) -> str:
        text = str(exc)
        if exc.kind != "illegal":
            return text
        if self.board.is_check():
            who = "you are" if not self.local else f"{color_name(self.board.turn)} is"
            text += f" ({who} in check)"
        return text + ". Type /moves to list the legal moves."

    def _apply_move(self, move: Move, color: str, *, send: bool = False,
                    remote_clock: Optional[int] = None) -> None:
        board = self.board
        board.push(move)
        mover_left: Optional[int] = None
        if self.clock is not None:
            mover_left = self.clock.press(color)
            if remote_clock is not None:
                self.clock.set_remaining(color, remote_clock)
        had_offers = bool(self.incoming or self.outgoing)
        self.incoming.clear()
        self.outgoing.clear()
        if not self.local and color != self.my_color:
            self._draw_offered_at = None
        if had_offers:
            self._log("system", "Pending offers were cancelled by the move.")
        self.dirty = True
        if send:
            self._send({"type": "move", "uci": move.uci(), "ply": len(board.move_stack), "clock_ms": mover_left})
        if self.over is None:
            outcome = board.outcome()
            if outcome is not None:
                self._end(outcome)

    # -- commands -----------------------------------------------------------------------------

    def _cmd_help(self, arg: str) -> None:
        for line in ui.help_lines():
            self._log("info", line)
        if self.local:
            self._log("info", "Local game: /draw, /resign and /takeback take effect at once.")

    def _cmd_chat(self, arg: str) -> None:
        text = _one_line(arg, 100000)
        if not text:
            self._error("Type a message after /c, for example: /c good luck!")
            return
        if self.local:
            self._error("Chat is for network games. In a local game you can just talk!")
            return
        if not self.connected:
            self._error(f"{self.opponent_name} is not connected any more.")
            return
        if len(text) > CHAT_MAX_LEN:
            text = text[:CHAT_MAX_LEN]
            self._log("system", f"Message shortened to {CHAT_MAX_LEN} characters.")
        if self._send({"type": "chat", "text": text}):
            self._log("chat_me", f"{self.my_name}: {text}")

    def _cmd_resign(self, arg: str) -> None:
        if self.over is not None:
            self._error("The game is already over.")
            return
        if self.local:
            loser = self.board.turn
            self._log("game", f"{color_name(loser)} resigns.")
            self._end(Outcome("resignation", opposite(loser)))
            return
        self._log("game", "You resigned.")
        self._end(Outcome("resignation", self.opponent_color))
        self._send({"type": "resign"})

    def _cmd_draw(self, arg: str) -> None:
        if self.over is not None:
            self._error("The game is already over.")
            return
        if self.local:
            self._log("game", "Draw agreed.")
            self._end(Outcome("agreement"))
            return
        if not self.connected:
            self._error(f"{self.opponent_name} is not connected any more.")
            return
        if "draw" in self.incoming:
            self._accept("draw")
            return
        if "draw" in self.outgoing:
            self._error(f"You already offered a draw. Waiting for {self.opponent_name}.")
            return
        if self._send({"type": "draw_offer", "ply": len(self.board.move_stack)}):
            self.outgoing["draw"] = {}
            self._draw_offered_at = len(self.board.move_stack)
            self._log("game", f"You offered a draw. Waiting for {self.opponent_name}…")

    def _latest_offer(self, arg: str) -> Optional[str]:
        wanted = arg.strip().lower()
        if wanted:
            return wanted if wanted in self.incoming else None
        return list(self.incoming)[-1] if self.incoming else None

    def _cmd_accept(self, arg: str) -> None:
        if self.local:
            self._error("There are no offers in a local game. /draw ends the game in a draw at once.")
            return
        kind = self._latest_offer(arg)
        if kind is None:
            self._error("There is no offer to accept.")
            return
        self._accept(kind)

    def _cmd_decline(self, arg: str) -> None:
        if self.local:
            self._error("There are no offers in a local game.")
            return
        kind = self._latest_offer(arg)
        if kind is None:
            self._error("There is no offer to decline.")
            return
        data = self.incoming.pop(kind)
        msg: Dict[str, Any] = {"type": f"{kind}_decline"}
        if kind == "takeback":
            msg["ply"] = data["ply"]
        self._send(msg)
        self._log("game", f"You declined {self.opponent_name}'s {_OFFER_LABELS[kind]}.")

    def _accept(self, kind: str) -> None:
        data = self.incoming.pop(kind, {})
        if kind == "draw":
            self._log("game", f"You accepted {self.opponent_name}'s draw offer.")
            self._end(Outcome("agreement"))
            self._send({"type": "draw_accept", "ply": len(self.board.move_stack)})
        elif kind == "takeback":
            plies = self._takeback_plies(self.opponent_color)
            ply = len(self.board.move_stack)
            if data.get("ply") != ply or ply < plies:
                self._error("That takeback request is out of date.")
                return
            if self._send({"type": "takeback_accept", "ply": ply}):
                undone = self._undo(plies)
                self._log("game", f"You accepted the takeback ({' '.join(undone)} undone).")
        elif kind == "rematch":
            if self._send({"type": "rematch_accept"}):
                self._start_rematch()

    def _takeback_plies(self, requester: Optional[str]) -> int:
        """Plies to undo so that ``requester``'s last move is taken back (2 if they are to move)."""
        return 2 if self.board.turn == requester else 1

    def _undo(self, plies: int) -> List[str]:
        undone = self.board.san_stack[-plies:] if plies else []
        for _ in range(plies):
            self.board.pop()
        self.incoming.clear()
        self.outgoing.clear()
        self._draw_offered_at = None
        if self.clock is not None:
            if self.board.move_stack:
                self.clock.start(self.board.turn)
            else:
                self.clock.stop()
        self.dirty = True
        return list(undone)

    def _cmd_takeback(self, arg: str) -> None:
        if self.over is not None:
            self._error("The game is over. " + self._after_game_hint())
            return
        if self.local:
            if not self.board.move_stack:
                self._error("There is no move to take back.")
                return
            undone = self._undo(1)
            self._log("game", f"Took back {undone[0]}. {color_name(self.board.turn)} to move.")
            return
        if not self.connected:
            self._error(f"{self.opponent_name} is not connected any more.")
            return
        if "takeback" in self.outgoing:
            self._error(f"You already asked for a takeback. Waiting for {self.opponent_name}.")
            return
        if "takeback" in self.incoming:
            self._error(f"{self.opponent_name} asked for a takeback first. Answer it with /accept or /decline.")
            return
        plies = self._takeback_plies(self.my_color)
        ply = len(self.board.move_stack)
        if ply < plies:
            self._error("You have no move to take back yet.")
            return
        if self._send({"type": "takeback_request", "ply": ply}):
            self.outgoing["takeback"] = {"ply": ply}
            self._log("game", f"You asked to take back your last move. Waiting for {self.opponent_name}…")

    def _cmd_flip(self, arg: str) -> None:
        current = self.perspective
        if self.auto_flip:
            self.auto_flip = False
            self._log("system", "Board flipped. It no longer turns by itself; /flip turns it again.")
        target = opposite(current)
        base = (WHITE if self.local else self.my_color) or WHITE
        self.flipped = target != base
        self.dirty = True

    def _cmd_size(self, arg: str) -> None:
        """/size [auto|small|medium|large|xl] (a unique prefix will do); no argument: the next size.

        Only this session changes: the saved default is set in the menu's Settings.
        """
        word = arg.strip().lower()
        if not word:
            size = ui.next_board_size(self.board_size)
        else:
            matches = [name for name in ui.BOARD_SIZES if name.startswith(word)]
            if len(matches) != 1:
                self._error(f"Unknown board size: {_one_line(arg, 20)}. "
                            "Use /size auto, small, medium, large or xl (or just /size for the next one).")
                return
            size = matches[0]
        self.board_size = size
        self.dirty = True
        if size == "auto":
            text = "Board size: auto (the biggest board that fits the window)."
        elif size == "small":
            text = "Board size: small."
        elif size in ("large", "xl") and not self.unicode:
            text = f"Board size: {size}. Drawn pieces need chess symbols, so the letter board is shown."
        else:
            drawn = ", with drawn pieces" if size != "medium" else ""
            text = f"Board size: {size}{drawn} (smaller while the window is too small for it)."
        self._log("system", text)

    def _cmd_moves(self, arg: str) -> None:
        if self.over is not None:
            self._error("The game is over, so there are no legal moves.")
            return
        sans = self.board.legal_moves_san()
        side = color_name(self.board.turn)
        self._log("info", f"Legal moves for {side} ({len(sans)}): " + " ".join(sans))

    def _cmd_fen(self, arg: str) -> None:
        self._log("info", "FEN: " + self.board.fen())

    def _cmd_pgn(self, arg: str) -> None:
        self._log("info", self.pgn().rstrip("\n"))

    def _cmd_save(self, arg: str) -> None:
        try:
            path = self.save_pgn(arg or None)
        except OSError as exc:
            self._error(f"Could not save the game: {exc.strerror or exc}")
            return
        self._log("system", f"Game saved to {_display_path(path)}")

    def _cmd_rematch(self, arg: str) -> None:
        if self.over is None:
            self._error("The game is still in progress. You can ask for a rematch when it ends.")
            return
        if self.local:
            self._start_rematch()
            return
        if not self.connected:
            self._error(f"{self.opponent_name} has left, so a rematch is not possible.")
            return
        if "rematch" in self.incoming:
            self._accept("rematch")
            return
        if "rematch" in self.outgoing:
            self._error(f"You already offered a rematch. Waiting for {self.opponent_name}.")
            return
        if self._send({"type": "rematch_offer"}):
            self.outgoing["rematch"] = {}
            self._log("game", f"You offered a rematch (colours swap). Waiting for {self.opponent_name}…")

    def _cmd_quit(self, arg: str) -> None:
        self.request_quit()

    def _cmd_clear(self, arg: str) -> None:
        self.log.clear()
        self.log_scroll = 0
        self.dirty = True

    def request_quit(self) -> None:
        """/quit, Ctrl-C: leave at once, except in a live network game where a second request
        within QUIT_CONFIRM_SECONDS resigns and leaves."""
        live = not self.local and self.connected and self.over is None
        if not live:
            self.quit_requested = True
            return
        now = self.now_fn()
        if self._quit_deadline is not None and now <= self._quit_deadline:
            self._log("game", "You resigned and left the game.")
            self._end(Outcome("resignation", self.opponent_color))
            self._send({"type": "resign"})
            self.quit_requested = True
            return
        self._quit_deadline = now + QUIT_CONFIRM_SECONDS
        self._log("system", "The game is still in progress. Type /quit (or press Ctrl-C) again within "
                            f"{int(QUIT_CONFIRM_SECONDS)} seconds to resign and leave.")

    def leave(self) -> None:
        """Leave immediately without resigning (input closed). The opponent sees the game abandoned."""
        self.quit_requested = True

    # -- game end, timeouts, rematch ----------------------------------------------------------

    def _personal_result(self, outcome: Outcome) -> str:
        if self.local or outcome.result() in ("*", "1/2-1/2"):
            return ""
        return "You won!" if outcome.winner == self.my_color else "You lost."

    def _after_game_hint(self) -> str:
        if self.local:
            return "Type /rematch for a new game, or /quit to leave."
        if self.connected:
            return "Type /rematch to play again, or /quit to leave."
        return "Type /quit to leave."

    def _end(self, outcome: Outcome) -> None:
        if self.over is not None:
            return
        self.over = outcome
        if self.clock is not None:
            self.clock.stop()
        self.incoming.clear()
        self.outgoing.clear()
        self._draw_offered_at = None
        self._quit_deadline = None
        text = outcome.describe()
        if outcome.result() != "*":
            text += f" ({outcome.result()})"
        personal = self._personal_result(outcome)
        self._log("game", text + (f". {personal}" if personal else ""))
        self._log("info", self._after_game_hint())
        if self.autosave and self.board.move_stack:
            try:
                path = self.save_pgn()
            except OSError as exc:
                self._error(f"Could not save the game: {exc.strerror or exc}")
            else:
                self._log("system", f"Game saved to {_display_path(path)}")
        self.dirty = True

    def _timeout_outcome(self, loser: str) -> Outcome:
        winner = opposite(loser)
        if self.board.has_insufficient_material(winner):
            return Outcome("timeout_insufficient")
        return Outcome("timeout", winner)

    def _flag(self, loser: str) -> None:
        if self.over is not None:
            return
        if self.clock is not None:
            self.clock.stop()
            self.clock.set_remaining(loser, 0)
        if self.local or loser != self.my_color:
            self._log("game", f"{self.player_name(loser)} ran out of time.")
        else:
            self._log("game", "Your time ran out.")
        self._end(self._timeout_outcome(loser))
        if not self.local:
            self._send({"type": "timeout", "loser": loser})

    def tick(self) -> None:
        """Periodic housekeeping: expire the quit confirmation and detect a flag on our own clock."""
        if self._quit_deadline is not None and self.now_fn() > self._quit_deadline:
            self._quit_deadline = None
            self.dirty = True
        if self.over is not None or self.clock is None:
            return
        flagged = self.clock.flagged()
        if flagged is None:
            return
        if not self.local and flagged != self.my_color:
            return  # only the side whose own clock ran out reports it
        self._flag(flagged)

    def _start_rematch(self) -> None:
        if not self.local and self.my_color is not None:
            self.my_color = opposite(self.my_color)
        self._start_game(STARTING_FEN)
        if self.local:
            self._log("game", "New game started. White to move.")
            return
        first = "You move first." if self.my_turn else f"{self.opponent_name} moves first."
        timing = f" Time control {self.time_control}." if self.time_control else ""
        self._log("game", f"Rematch! You play {color_name(self.my_color)} this time.{timing} {first}")

    # -- network ------------------------------------------------------------------------------

    def _send(self, msg: Dict[str, Any]) -> bool:
        if self.local or self.conn is None or not self.connected:
            return False
        try:
            self.conn.send(msg)
        except (ConnectionClosed, OSError) as exc:
            self._connection_lost(str(exc) or "connection lost")
            return False
        return True

    def _connection_lost(self, reason: str, left: bool = False) -> None:
        if not self.connected:
            return
        self.connected = False
        self.incoming.clear()
        self.outgoing.clear()
        self._quit_deadline = None
        opp = self.opponent_name
        if self.over is None:
            self._error(f"{opp} left the game." if left else f"Connection to {opp} lost ({reason}).")
            self._end(Outcome("abandoned"))
        else:
            self._log("system", f"{opp} left." if left else f"Connection to {opp} closed ({reason}).")
        self.dirty = True

    def handle_message(self, msg: Any) -> None:
        """Process one message from the opponent (``conn.inbox`` item)."""
        if self.local or not isinstance(msg, dict):
            return
        kind = msg.get("type")
        handler = _HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is not None:
            handler(self, msg)

    def _desync(self, reason: str) -> None:
        self._send({"type": "error", "reason": reason})
        self._error(f"Out of sync with {self.opponent_name}: {reason}. The game has been abandoned.")
        self._end(Outcome("abandoned"))

    def _on_move(self, msg: Dict[str, Any]) -> None:
        if self.over is not None:
            return  # crossed with our resignation / timeout / agreement
        board = self.board
        expected = len(board.move_stack) + 1
        ply = msg.get("ply")
        uci = msg.get("uci")
        if not _is_int(ply) or ply != expected:
            self._desync(f"expected move {expected} but received move {_one_line(str(ply), 12)}")
            return
        if board.turn != self.opponent_color:
            self._desync("received a move when it was not their turn")
            return
        try:
            move = Move.from_uci(uci)
        except (ValueError, TypeError):
            self._desync(f"received an unreadable move {_one_line(str(uci), 12)!r}")
            return
        if not board.is_legal(move):
            self._desync(f"received an illegal move {move.uci()}")
            return
        clock_ms = msg.get("clock_ms")
        self._apply_move(move, board.turn, remote_clock=clock_ms if _is_int(clock_ms) else None)

    def _on_chat(self, msg: Dict[str, Any]) -> None:
        text = _one_line(msg.get("text"), CHAT_MAX_LEN)
        if text:
            self._log("chat_them", f"{self.opponent_name}: {text}")

    def _on_resign(self, msg: Dict[str, Any]) -> None:
        if self.over is not None:
            return
        self._log("game", f"{self.opponent_name} resigned.")
        self._end(Outcome("resignation", self.my_color))

    def _on_draw_offer(self, msg: Dict[str, Any]) -> None:
        if self.over is not None:
            return
        ply = msg.get("ply")
        if _is_int(ply) and ply != len(self.board.move_stack):
            # Made before a move (or takeback) that the sender had not seen yet: that change already
            # cancelled the offer on their side, so showing it here would let us accept a dead offer.
            return
        if "draw" in self.outgoing:  # offers crossed: that is an agreement
            self._log("game", "You both offered a draw.")
            self._end(Outcome("agreement"))
            self._send({"type": "draw_accept", "ply": len(self.board.move_stack)})
            return
        self.incoming.pop("draw", None)
        self.incoming["draw"] = {}
        self._log("game", f"{self.opponent_name} offers a draw. Type /accept or /decline.")

    def _on_draw_accept(self, msg: Dict[str, Any]) -> None:
        if self.over is not None or self._draw_offered_at is None:
            return
        ply = msg.get("ply")
        if _is_int(ply) and ply != self._draw_offered_at:
            return  # an answer to some other (cancelled) offer
        # The opponent may have accepted just before our next move reached them: in that case
        # the game ended where the offer was made, so withdraw our later move to stay in sync.
        withdrawn = []
        while len(self.board.move_stack) > self._draw_offered_at:
            withdrawn.append(self.board.san_stack[-1])
            self.board.pop()
        self._log("game", f"{self.opponent_name} accepted your draw offer.")
        if withdrawn:
            self._log("system", f"They accepted before your move {' '.join(reversed(withdrawn))} "
                                "arrived, so it was withdrawn.")
        self._end(Outcome("agreement"))

    def _on_draw_decline(self, msg: Dict[str, Any]) -> None:
        if self.over is not None or self._draw_offered_at is None:
            return
        self.outgoing.pop("draw", None)
        self._draw_offered_at = None
        self._log("game", f"{self.opponent_name} declined your draw offer.")

    def _on_takeback_request(self, msg: Dict[str, Any]) -> None:
        ply = msg.get("ply")
        current = len(self.board.move_stack)
        plies = self._takeback_plies(self.opponent_color)
        if self.over is not None or not _is_int(ply) or ply != current or current < plies:
            self._send({"type": "takeback_decline", "ply": ply if _is_int(ply) else current})
            if self.over is None:
                self._log("system", f"Declined an out-of-date takeback request from {self.opponent_name}.")
            return
        if "takeback" in self.outgoing:
            # Both asked at the same moment. Accepting both would undo different numbers of plies on
            # each side, so cancel both requests (each side declines the other's) and let them retry.
            self.outgoing.pop("takeback", None)
            self._send({"type": "takeback_decline", "ply": ply})
            self._log("system", f"You and {self.opponent_name} asked for a takeback at the same time, so both "
                                "requests were cancelled. Ask again if you still want one.")
            return
        self.incoming.pop("takeback", None)
        self.incoming["takeback"] = {"ply": ply}
        extra = " (and your reply)" if plies == 2 else ""
        self._log("game", f"{self.opponent_name} asks to take back their last move{extra}. "
                          "Type /accept or /decline.")

    def _on_takeback_accept(self, msg: Dict[str, Any]) -> None:
        data = self.outgoing.get("takeback")
        if data is None or self.over is not None:
            return
        ply = msg.get("ply")
        if ply != data["ply"] or ply != len(self.board.move_stack):
            self._desync("the takeback answer does not match the game")
            return
        self.outgoing.pop("takeback", None)
        undone = self._undo(self._takeback_plies(self.my_color))
        self._log("game", f"{self.opponent_name} accepted your takeback ({' '.join(undone)} undone). Your move.")

    def _on_takeback_decline(self, msg: Dict[str, Any]) -> None:
        if self.outgoing.pop("takeback", None) is not None:
            self._log("game", f"{self.opponent_name} declined your takeback request.")

    def _on_timeout(self, msg: Dict[str, Any]) -> None:
        if self.over is not None:
            return
        loser = msg.get("loser")
        if self.clock is None or loser != self.opponent_color:
            self._log("system", "Ignored an invalid timeout message.")
            return
        self.clock.stop()
        self.clock.set_remaining(loser, 0)
        self._log("game", f"{self.opponent_name} ran out of time.")
        self._end(self._timeout_outcome(loser))

    def _on_rematch_offer(self, msg: Dict[str, Any]) -> None:
        if self.over is None:
            self._send({"type": "rematch_decline"})
            return
        if "rematch" in self.outgoing:  # offers crossed: start right away
            self._send({"type": "rematch_accept"})
            self._start_rematch()
            return
        self.incoming.pop("rematch", None)
        self.incoming["rematch"] = {}
        self._log("game", f"{self.opponent_name} wants a rematch (colours swap). Type /accept or /decline.")

    def _on_rematch_accept(self, msg: Dict[str, Any]) -> None:
        if self.over is not None and "rematch" in self.outgoing:
            self._start_rematch()

    def _on_rematch_decline(self, msg: Dict[str, Any]) -> None:
        if self.outgoing.pop("rematch", None) is not None:
            self._log("game", f"{self.opponent_name} declined the rematch.")

    def _on_error(self, msg: Dict[str, Any]) -> None:
        reason = _one_line(msg.get("reason"), 200) or "no reason given"
        self._error(f"{self.opponent_name} reported a problem: {reason}.")
        if self.over is None:
            self._end(Outcome("abandoned"))

    def _on_bye(self, msg: Dict[str, Any]) -> None:
        self._connection_lost("left", left=True)

    def _on_disconnected(self, msg: Dict[str, Any]) -> None:
        self._connection_lost(_one_line(msg.get("reason"), 200) or "connection closed")

    def _on_protocol_error(self, msg: Dict[str, Any]) -> None:
        reason = _one_line(msg.get("reason"), 100)
        self._log("system", f"Ignored a malformed message from {self.opponent_name} ({reason}).")

    # -- PGN ----------------------------------------------------------------------------------

    def pgn(self) -> str:
        started = time.localtime(self.started_at)
        headers: Dict[str, str] = {
            "Event": "LAN Chess local game" if self.local else "LAN Chess game",
            "Site": "Local" if self.local else "LAN",
            "Date": time.strftime("%Y.%m.%d", started),
            "Round": str(self.game_number),
            "White": self.white_name,
            "Black": self.black_name,
            "Time": time.strftime("%H:%M:%S", started),
            "TimeControl": self.time_control.pgn_tag() if self.time_control else "-",
        }
        if self.over is not None:
            headers["Termination"] = _TERMINATION_TAGS.get(self.over.termination, "normal")
        return self.board.pgn(headers, self.over.result() if self.over is not None else "*")

    def default_filename(self) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}_{_filename_part(self.white_name)}-vs-{_filename_part(self.black_name)}.pgn"

    def save_pgn(self, path: Optional[str] = None) -> str:
        """Write the PGN; default location ``pgn_dir`` (or ~/lanchess_games). Returns the path."""
        if path:
            target = os.path.expanduser(path)
            if os.path.isdir(target) or target.endswith(("/", os.sep)):
                target = os.path.join(target, self.default_filename())
        else:
            directory = os.path.expanduser(self.pgn_dir or DEFAULT_PGN_DIR)
            target = os.path.join(directory, self.default_filename())
            stem, ext = os.path.splitext(target)
            counter = 2
            while os.path.exists(target):
                target = f"{stem}-{counter}{ext}"
                counter += 1
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.pgn())
        target = os.path.abspath(target)
        self.saved_paths.append(target)
        return target

    # -- view ---------------------------------------------------------------------------------

    def status_text(self) -> str:
        if self.over is not None:
            personal = self._personal_result(self.over)
            if self.local or self.connected:
                hint = "/rematch to play again · /quit to leave"
            else:
                hint = "/quit to leave"
            return f"{personal}  {hint}" if personal else hint
        board = self.board
        check = board.is_check()
        if self.local:
            return f"{color_name(board.turn)} to move" + (" — check!" if check else "")
        if board.turn == self.my_color:
            if "takeback" in self.outgoing:
                return f"Waiting for {self.opponent_name} to answer your takeback request…"
            text = "Your move" + (" — you are in check!" if check else "")
            if self.clock is not None and self.clock.running is None:
                text += " (the clocks start after the first move)"
            return text
        return f"Waiting for {self.opponent_name} to move…"

    def pending_text(self) -> str:
        parts = []
        if self._quit_deadline is not None:
            left = max(1, int(math.ceil(self._quit_deadline - self.now_fn())))
            parts.append(f"/quit or Ctrl-C again within {left} s resigns and leaves")
        parts.extend(_INCOMING_TEXT[kind].format(self.opponent_name) for kind in self.incoming)
        parts.extend(_OUTGOING_TEXT[kind].format(self.opponent_name) for kind in self.outgoing)
        return "  ·  ".join(parts)

    def connection_text(self) -> str:
        if self.local:
            parts = ["Local game"]
        elif self.connected:
            parts = [f"Connected to {self.peer}" if self.peer else "Connected"]
        else:
            parts = ["Disconnected"]
        if self.time_control is not None:
            parts.append(str(self.time_control))
        return " · ".join(parts)

    def prompt_text(self) -> str:
        if self.local and self.over is None:
            return f"{color_name(self.board.turn)}> "
        return "> "

    def view_state(self) -> ui.ViewState:
        over = self.over
        clock = self.clock
        return ui.ViewState(
            board=self.board,
            perspective=self.perspective,
            my_color=self.my_color,
            white_name=self.white_name,
            black_name=self.black_name,
            clock_ms=clock.snapshot() if clock is not None else None,
            clock_running=clock.running if clock is not None and over is None else None,
            log=self.log,
            status=self._t(self.status_text()),
            prompt=self.prompt_text(),
            game_over=self._t(over.describe()) if over is not None else None,
            connection=self._t(self.connection_text()),
            pending=self._t(self.pending_text()),
            log_scroll=self.log_scroll,
            board_size=self.board_size,
        )

    def display_key(self) -> Tuple[Any, ...]:
        """Changes whenever the time-dependent parts of the screen change (clocks, quit countdown)."""
        clocks = None
        if self.clock is not None:
            clocks = (ui.format_clock(self.clock.remaining(WHITE)), ui.format_clock(self.clock.remaining(BLACK)))
        quit_left = None
        if self._quit_deadline is not None:
            quit_left = int(math.ceil(self._quit_deadline - self.now_fn()))
        return clocks, quit_left

    def summary_lines(self) -> List[str]:
        """What to print on the normal screen after leaving."""
        lines = []
        if self.over is not None:
            result = self.over.result()
            personal = self._personal_result(self.over)
            lines.append("Result: " + self.over.describe() + (f" ({result})" if result != "*" else "")
                         + (f". {personal}" if personal else ""))
        elif self.board.move_stack:
            lines.append("The game was left unfinished.")
        lines.extend(f"Game saved to {path}" for path in self.saved_paths)
        return [self._t(line) for line in lines]


_COMMANDS: Dict[str, Callable[[GameSession, str], None]] = {
    "help": GameSession._cmd_help, "h": GameSession._cmd_help, "?": GameSession._cmd_help,
    "c": GameSession._cmd_chat, "chat": GameSession._cmd_chat, "say": GameSession._cmd_chat,
    "resign": GameSession._cmd_resign,
    "draw": GameSession._cmd_draw,
    "accept": GameSession._cmd_accept, "decline": GameSession._cmd_decline,
    "takeback": GameSession._cmd_takeback, "undo": GameSession._cmd_takeback,
    "flip": GameSession._cmd_flip,
    "size": GameSession._cmd_size,
    "moves": GameSession._cmd_moves,
    "fen": GameSession._cmd_fen,
    "pgn": GameSession._cmd_pgn,
    "save": GameSession._cmd_save,
    "rematch": GameSession._cmd_rematch,
    "quit": GameSession._cmd_quit, "exit": GameSession._cmd_quit, "q": GameSession._cmd_quit,
    "clear": GameSession._cmd_clear,
}

_HANDLERS: Dict[str, Callable[[GameSession, Dict[str, Any]], None]] = {
    "move": GameSession._on_move,
    "chat": GameSession._on_chat,
    "resign": GameSession._on_resign,
    "draw_offer": GameSession._on_draw_offer,
    "draw_accept": GameSession._on_draw_accept,
    "draw_decline": GameSession._on_draw_decline,
    "takeback_request": GameSession._on_takeback_request,
    "takeback_accept": GameSession._on_takeback_accept,
    "takeback_decline": GameSession._on_takeback_decline,
    "timeout": GameSession._on_timeout,
    "rematch_offer": GameSession._on_rematch_offer,
    "rematch_accept": GameSession._on_rematch_accept,
    "rematch_decline": GameSession._on_rematch_decline,
    "error": GameSession._on_error,
    "bye": GameSession._on_bye,
    "_disconnected": GameSession._on_disconnected,
    "_protocol_error": GameSession._on_protocol_error,
}

# ---------------------------------------------------------------------------------------------
# Run loops
# ---------------------------------------------------------------------------------------------


class _NoTerminal(Exception):
    """Full-screen mode is impossible (stdin is not a terminal)."""


def _raise_exit(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


@contextmanager
def _exit_on_signals() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit so ``finally`` blocks restore the terminal."""
    installed = []
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.signal(signum, _raise_exit)
            except (ValueError, OSError):
                continue
            installed.append((signum, previous if previous is not None else signal.SIG_DFL))
    try:
        yield
    finally:
        for signum, previous in installed:
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, TypeError):
                pass


def _drain_inbox(session: GameSession, conn: Any, limit: int = 500) -> None:
    inbox = getattr(conn, "inbox", None)
    if inbox is None:
        return
    for _ in range(limit):
        try:
            msg = inbox.get_nowait()
        except queue.Empty:
            return
        session.handle_message(msg)


def _close_connection(session: GameSession, conn: Any) -> None:
    if conn is None:
        return
    try:
        if not getattr(conn, "closed", False):
            conn.send({"type": "bye"})
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass
    session.connected = False


def _live_terminal_size() -> Tuple[int, int]:
    """The terminal's current (columns, rows), asked from the tty itself on every call.

    ``shutil.get_terminal_size`` prefers exported COLUMNS/LINES variables, which never change when the
    window is resized, so the real terminal is queried first.
    """
    for stream in (sys.__stdout__, sys.__stderr__, sys.__stdin__):
        try:
            size = os.get_terminal_size(stream.fileno())  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            continue
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    return term.terminal_size()


def _log_line_count(log: List[Tuple[str, str]], width: int) -> int:
    width = max(1, width)
    total = 0
    for entry in log:
        for segment in str(entry[1]).split("\n"):
            total += max(1, -(-len(segment) // width))
    return total


def run_interactive(session: GameSession, conn: Any = None, theme: Optional[ui.Theme] = None,
                    plain: bool = False, *, key_reader: Any = None, screen: Any = None,
                    input_source: Any = None, output: Optional[TextIO] = None,
                    size_fn: Optional[Callable[[], Tuple[int, int]]] = None,
                    poll_interval: float = 0.05) -> None:
    """Run the game until the user quits; sends ``bye`` and closes ``conn`` on the way out.

    Full-screen mode (default) uses the alternate screen, a raw-mode KeyReader and a LineEditor;
    the terminal is restored even if something fails. Plain mode reads lines (PlainLineInput) and
    prints the board after every position change plus new log lines. ``key_reader``, ``screen``,
    ``input_source`` (a stream or an object with ``poll``), ``output`` and ``size_fn`` are for tests.
    """
    theme = theme if theme is not None else ui.Theme()
    if conn is None:
        conn = session.conn
    try:
        with _exit_on_signals():
            use_plain = plain
            if not use_plain:
                try:
                    _run_fullscreen(session, conn, theme, key_reader, screen,
                                    size_fn or _live_terminal_size, poll_interval)
                except _NoTerminal:
                    use_plain = True
            if use_plain:
                _run_plain(session, conn, theme, input_source, output, poll_interval)
    finally:
        _close_connection(session, conn)


def _run_fullscreen(session: GameSession, conn: Any, theme: ui.Theme, key_reader: Any, screen: Any,
                    size_fn: Callable[[], Tuple[int, int]], poll_interval: float) -> None:
    reader = key_reader if key_reader is not None else term.KeyReader()
    try:
        reader.start()
    except Exception as exc:
        raise _NoTerminal(str(exc)) from exc
    display = screen if screen is not None else term.Screen()
    editor = term.LineEditor(reader, max_len=CHAT_MAX_LEN + 8)
    try:
        display.enter()
        last_size: Optional[Tuple[int, int]] = None
        last_key: Any = None
        last_render = 0.0
        force = True
        while not session.quit_requested:
            size = size_fn()
            changed = False
            event = editor.poll(poll_interval)
            handled = 0
            while event is not None:
                changed = _editor_event(session, event, reader, size) or changed
                handled += 1
                if session.quit_requested or handled >= 256:
                    break
                event = editor.poll(0)
            if session.quit_requested:
                break
            _drain_inbox(session, conn)
            session.tick()
            if session.quit_requested:
                break
            key = session.display_key()
            now = time.monotonic()
            if force or changed or session.dirty or size != last_size or key != last_key or now - last_render >= 1.0:
                view = session.view_state()
                view.input_buffer = editor.buffer
                view.input_cursor = editor.cursor
                frame, cursor = ui.render_screen(view, size[0], size[1], theme)
                display.draw(frame, cursor, size)
                session.dirty = False
                force = False
                last_size, last_key, last_render = size, key, now
    finally:
        try:
            display.exit()
        finally:
            reader.restore()


def _editor_event(session: GameSession, event: tuple, reader: Any, size: Tuple[int, int]) -> bool:
    """Apply a LineEditor event; returns True if the screen needs a redraw."""
    kind = event[0]
    if kind == "line":
        session.log_scroll = 0
        session.handle_input(event[1])
    elif kind == "interrupt":
        session.request_quit()
    elif kind == "eof":
        if getattr(reader, "eof", False):
            session.leave()
        else:
            session.request_quit()
    elif kind == "scroll":
        page = max(1, size[1] // 2)
        limit = _log_line_count(session.log, size[0])
        session.log_scroll = max(0, min(limit, session.log_scroll - int(event[1]) * page))
    return True


def _plain_text(text: Any) -> str:
    """Untrusted text made safe for a plain terminal: no escapes or control characters (newlines kept)."""
    stripped = ui.strip_ansi(str(text))
    return "".join(ch for ch in stripped
                   if ch == "\n" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))


class _PlainPrinter:
    """Prints what changed since the last call: the board after position changes, new log lines, status."""

    _PREFIXES = {"error": "! ", "game": "* "}

    def __init__(self, session: GameSession, out: TextIO, theme: ui.Theme) -> None:
        self.session = session
        self.out = out
        self.theme = theme
        self.printed = 0
        self.position: Any = None
        self.status: Optional[str] = None
        self.broken = False

    def _new_logs(self) -> List[str]:
        session = self.session
        count = session.log_total - self.printed
        self.printed = session.log_total
        if count <= 0:
            return []
        entries = session.log[-count:] if count <= len(session.log) else list(session.log)
        return [self._PREFIXES.get(kind, "") + _plain_text(text) for kind, text in entries]

    def _clock_line(self) -> str:
        session = self.session
        clock = session.clock
        if clock is None:
            return ""
        parts = []
        for color in (WHITE, BLACK):
            mark = " (running)" if clock.running == color and session.over is None else ""
            parts.append(f"{session.player_name(color)} {ui.format_clock(clock.remaining(color))}{mark}")
        return "  Clocks: " + "  |  ".join(parts)

    def _status(self) -> str:
        session = self.session
        if session.over is not None:
            return ""
        text = "-- " + session._t(session.status_text()) + " --"
        pending = session._t(session.pending_text())
        return _plain_text(text + (f"\n   {pending}" if pending else ""))

    def update(self, initial: bool = False) -> None:
        session = self.session
        chunks: List[str] = []
        logs = self._new_logs()
        if initial:
            chunks.extend(logs)
            logs = []
        position = (session.game_number, len(session.board.move_stack), session.board.fen(), session.perspective)
        if position != self.position:
            self.position = position
            chunks.append("")
            chunks.append(ui.render_plain_board(session.board, session.perspective, self.theme))
            clocks = self._clock_line()
            if clocks:
                chunks.append(clocks)
            self.status = None
        chunks.extend(logs)
        status = self._status()
        if status != self.status:
            self.status = status
            if status:
                chunks.append(status)
        if chunks:
            self._write("\n".join(chunks) + "\n")

    def _write(self, text: str) -> None:
        if self.broken:
            return
        if not self.theme.unicode:
            text = text.translate(_ASCII_TABLE)
        try:
            try:
                self.out.write(text)
            except UnicodeEncodeError:
                encoding = getattr(self.out, "encoding", None) or "ascii"
                self.out.write(text.encode(encoding, "replace").decode(encoding, "replace"))
            self.out.flush()
        except (OSError, ValueError):
            self.broken = True


def _run_plain(session: GameSession, conn: Any, theme: ui.Theme, input_source: Any,
               output: Optional[TextIO], poll_interval: float) -> None:
    out = output if output is not None else sys.stdout
    source = input_source if hasattr(input_source, "poll") else term.PlainLineInput(input_source)
    printer = _PlainPrinter(session, out, ui.Theme(unicode=theme.unicode, color=False))
    interrupts: List[bool] = []
    with _sigint_as_flag(interrupts):
        printer.update(initial=True)
        while not session.quit_requested:
            try:
                event = source.poll(max(poll_interval, 0.05))
                if event is not None:
                    if event[0] == "line":
                        session.handle_input(event[1])
                    elif event[0] == "eof":
                        session.leave()
                    elif event[0] == "interrupt":
                        session.request_quit()
                _drain_inbox(session, conn)
                session.tick()
            except KeyboardInterrupt:  # only if the SIGINT handler could not be installed
                interrupts.append(True)
            while interrupts and not session.quit_requested:
                interrupts.pop()
                session.request_quit()  # Ctrl-C acts like /quit (a live network game asks first)
            printer.update()
        printer.update()


@contextmanager
def _sigint_as_flag(flag: List[bool]) -> Iterator[None]:
    """Plain mode: make Ctrl-C (SIGINT) append to ``flag`` instead of raising KeyboardInterrupt.

    The loop polls the flag, so an interrupt never lands half-way through handling a message or
    printing. (Full-screen mode needs no handler: the raw terminal delivers Ctrl-C as a key.)
    """
    installed = False
    previous: Any = None
    if threading.current_thread() is threading.main_thread():
        try:
            previous = signal.signal(signal.SIGINT, lambda signum, frame: flag.append(True))
            installed = True
        except (ValueError, OSError, AttributeError):
            pass
    try:
        yield
    finally:
        if installed:
            try:
                signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)
            except (ValueError, OSError, TypeError):
                pass

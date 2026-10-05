"""Pure rendering for the terminal UI: every function returns strings and never prints."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

WHITE = "w"
BLACK = "b"

RESET = "\x1b[0m"

# xterm-256 palette indices. Square backgrounds are mid-tones (relative luminance 0.13-0.28) so that
# pure-white (231) and pure-black (16) pieces both keep a contrast ratio of at least ~3:1:
#   137 #af875f L=.27 (white 3.3:1, black 6.5:1)   94 #875f00 L=.13 (white 5.7:1, black 3.7:1)
#   100 #878700 L=.22 (white 3.8:1, black 5.5:1)   64 #5f8700 L=.20 (white 4.2:1, black 5.0:1)
#   160 #d70000 L=.14 (white 5.4:1, black 3.9:1)
LIGHT_SQUARE = 137
DARK_SQUARE = 94
LIGHT_HIGHLIGHT = 100
DARK_HIGHLIGHT = 64
CHECK_SQUARE = 160
WHITE_PIECE = 231
BLACK_PIECE = 16

COORD_FG = 244
DIM_FG = 244
TURN_FG = 34
ERROR_FG = 196
LOW_TIME_FG = 196
LOW_TIME_BG = 160
RUNNING_BG = 150
CHAT_ME_FG = 33
CHAT_THEM_FG = 170
PENDING_FG = 172
STATUS_BG = 236
STATUS_FG = 252
GAME_OVER_FG = 222

LOW_TIME_MS = 20000
WIDE_MIN_WIDTH = 70
PANEL_GAP = 2
PANEL_MAX_WIDTH = 44
LARGE_PANEL_MIN_WIDTH = 32
MIN_LOG_ROWS = 3
SAN_WIDTH = 7
SQUARE_SIZES = ((3, 1), (5, 2), (7, 3))

_SOLID_GLYPHS = {"k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟"}
_OUTLINE_GLYPHS = {"k": "♔", "q": "♕", "r": "♖", "b": "♗", "n": "♘", "p": "♙"}

_ANSI_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
_ANSI_RE = re.compile(_ANSI_PATTERN)
_ANSI_SPLIT_RE = re.compile("(" + _ANSI_PATTERN + ")")
_UNSAFE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
_ZERO_WIDTH_CATEGORIES = ("Mn", "Me", "Cf", "Cc")
_LOG_STYLES: Dict[str, Dict[str, Any]] = {
    "info": {},
    "system": {"fg": DIM_FG},
    "game": {"bold": True},
    "error": {"fg": ERROR_FG},
    "chat_me": {"fg": CHAT_ME_FG},
    "chat_them": {"fg": CHAT_THEM_FG},
}


def sgr(*codes: object) -> str:
    """Build an SGR escape sequence, e.g. ``sgr(1, 38, 5, 196)``."""
    return "\x1b[" + ";".join(str(code) for code in codes) + "m"


@dataclass
class Theme:
    unicode: bool = True
    color: bool = True

    def paint(
        self,
        text: str,
        fg: Optional[int] = None,
        bg: Optional[int] = None,
        bold: bool = False,
        dim: bool = False,
        reverse: bool = False,
    ) -> str:
        """Wrap text in 256-colour SGR codes (fg/bg are palette indices); identity when colour is off."""
        if not self.color or not text:
            return text
        codes: List[object] = []
        if bold:
            codes.append(1)
        if dim:
            codes.append(2)
        if reverse:
            codes.append(7)
        if fg is not None:
            codes += [38, 5, fg]
        if bg is not None:
            codes += [48, 5, bg]
        return sgr(*codes) + text + RESET if codes else text


@dataclass(frozen=True)
class _Symbols:
    to_move: str
    ellipsis: str
    more_left: str
    more_right: str
    flag: str
    rule: str
    down: str


_UNICODE_SYMBOLS = _Symbols("●", "…", "‹", "›", "⚑", "─", "▼")
_ASCII_SYMBOLS = _Symbols("*", "...", "<", ">", "!", "-", "v")


def _symbols(theme: Theme) -> _Symbols:
    return _UNICODE_SYMBOLS if theme.unicode else _ASCII_SYMBOLS


def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences."""
    return _ANSI_RE.sub("", s)


def _char_width(ch: str) -> int:
    if " " <= ch <= "~":
        return 1
    if unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_len(s: str) -> int:
    """Display width in terminal columns, ignoring ANSI escape sequences."""
    text = _ANSI_RE.sub("", s)
    if text.isascii():
        return len(text)
    return sum(_char_width(ch) for ch in text)


def truncate(s: str, width: int, ellipsis: str = "") -> str:
    """Cut s to at most ``width`` visible columns, keeping escape codes and resetting style if cut."""
    if visible_len(s) <= width:
        return s
    if width <= 0:
        return ""
    ellipsis_width = visible_len(ellipsis)
    if ellipsis_width >= width:
        ellipsis, ellipsis_width = "", 0
    limit = width - ellipsis_width
    out: List[str] = []
    used = 0
    styled = False
    for index, part in enumerate(_ANSI_SPLIT_RE.split(s)):
        if index % 2:
            out.append(part)
            styled = True
            continue
        for ch in part:
            w = _char_width(ch)
            if used + w > limit:
                out.append(ellipsis)
                return "".join(out) + (RESET if styled else "")
            out.append(ch)
            used += w
    return "".join(out) + ellipsis + (RESET if styled else "")


def pad(s: str, width: int, align: str = "left") -> str:
    """Truncate or pad s with spaces to exactly ``width`` columns (align: left, right or center)."""
    s = truncate(s, width)
    gap = max(0, width - visible_len(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        return " " * (gap // 2) + s + " " * (gap - gap // 2)
    return s + " " * gap


def _clean(text: object) -> str:
    """Make untrusted text safe to print on one line: no escape sequences or control characters."""
    return _UNSAFE_RE.sub("", strip_ansi(str(text)).replace("\t", " ").replace("\n", " "))


def format_clock(ms: Optional[float]) -> str:
    """Clock text: ``m:ss`` (or ``h:mm:ss``) from 20 s up, ``s.d`` below 20 s, never below ``0:00``."""
    if ms is None:
        return "--:--"
    ms = int(ms)
    if ms <= 0:
        return "0:00"
    if ms < LOW_TIME_MS:
        return "%d.%d" % (ms // 1000, ms % 1000 // 100)
    hours, rest = divmod(ms // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)


def _opposite(color: str) -> str:
    return BLACK if color == WHITE else WHITE


def _piece_at(board: Any, sq: int) -> Optional[str]:
    return board.piece_at(sq) if board is not None else None


def _turn(board: Any) -> Optional[str]:
    return getattr(board, "turn", None)


def _check_square(board: Any) -> Optional[int]:
    return board.check_square() if board is not None else None


def _last_move(board: Any) -> Any:
    return board.last_move() if board is not None else None


def _captured(board: Any, color: str) -> List[str]:
    return list(board.captured_pieces(color)) if board is not None else []


def _advantage(board: Any, color: str) -> int:
    if board is None:
        return 0
    balance = board.material_balance()
    return balance if color == WHITE else -balance


def piece_glyph(piece: str, theme: Theme) -> str:
    """Text glyph for a piece char: outline symbols for White, filled for Black, or the letter in ascii mode."""
    if not theme.unicode:
        return piece
    table = _SOLID_GLYPHS if piece.islower() else _OUTLINE_GLYPHS
    return table[piece.lower()]


def _square_text(content: str, width: int) -> str:
    left = (width - 1) // 2
    return " " * left + content + " " * (width - 1 - left)


def render_board(
    board: Any,
    perspective: str = WHITE,
    last_move: Any = None,
    theme: Optional[Theme] = None,
    scale: int = 1,
) -> List[str]:
    """Board lines: 8 ranks with labels on the left, file labels below, flipped for Black.

    ``last_move`` defaults to ``board.last_move()``. ``scale`` picks the square size from SQUARE_SIZES
    (1: 3x1, 2: 5x2, 3: 7x3 columns x rows); without colour it is always 1. All lines have equal width.
    """
    theme = theme or Theme()
    scale = max(1, int(scale)) if theme.color else 1
    scale = min(scale, len(SQUARE_SIZES))
    sq_w, sq_h = SQUARE_SIZES[scale - 1]
    mid = sq_h // 2
    flipped = perspective == BLACK
    files = list(range(7, -1, -1)) if flipped else list(range(8))
    ranks = list(range(8)) if flipped else list(range(7, -1, -1))
    if last_move is None:
        last_move = _last_move(board)
    marked = {last_move.from_sq, last_move.to_sq} if last_move is not None else set()
    check = _check_square(board)
    lines: List[str] = []
    for rank in ranks:
        for row in range(sq_h):
            label = " " + theme.paint(str(rank + 1), fg=COORD_FG) + " " if row == mid else "   "
            cells = [label]
            for file in files:
                sq = rank * 8 + file
                piece = _piece_at(board, sq) if row == mid else None
                if theme.color:
                    cells.append(_color_square(piece, (rank + file) % 2 == 1, sq in marked, sq == check, sq_w, theme))
                else:
                    cells.append(_plain_square(_piece_at(board, sq), sq in marked, theme))
            if theme.color:
                cells.append(RESET)
            lines.append("".join(cells))
    labels = "".join(_square_text("abcdefgh"[f], sq_w) for f in files)
    lines.append("   " + theme.paint(labels, fg=COORD_FG))
    return lines


def _color_square(piece: Optional[str], light: bool, marked: bool, check: bool, width: int, theme: Theme) -> str:
    if check:
        bg = CHECK_SQUARE
    elif marked:
        bg = LIGHT_HIGHLIGHT if light else DARK_HIGHLIGHT
    else:
        bg = LIGHT_SQUARE if light else DARK_SQUARE
    if piece is None:
        return sgr(0, 48, 5, bg) + " " * width
    fg = WHITE_PIECE if piece.isupper() else BLACK_PIECE
    glyph = _SOLID_GLYPHS[piece.lower()] if theme.unicode else piece.upper()
    return sgr(0, 1, 38, 5, fg, 48, 5, bg) + _square_text(glyph, width)


def _plain_square(piece: Optional[str], marked: bool, theme: Theme) -> str:
    content = piece_glyph(piece, theme) if piece else "."
    return "[" + content + "]" if marked else " " + content + " "


def _move_pairs(board: Any) -> List[Tuple[int, Optional[str], Optional[str]]]:
    """Group the SAN history into numbered (number, white, black) pairs, honouring the root FEN."""
    sans = list(getattr(board, "san_stack", None) or [])
    number, black_first = 1, False
    fields = str(getattr(board, "root_fen", "") or "").split()
    if len(fields) >= 6:
        black_first = fields[1] == BLACK
        number = int(fields[5]) if fields[5].isdigit() else 1
    pairs: List[Tuple[int, Optional[str], Optional[str]]] = []
    index = 0
    if black_first and sans:
        pairs.append((number, None, sans[0]))
        index, number = 1, number + 1
    while index < len(sans):
        pairs.append((number, sans[index], sans[index + 1] if index + 1 < len(sans) else None))
        index += 2
        number += 1
    return pairs


def _last_move_label(board: Any) -> str:
    pairs = _move_pairs(board)
    if not pairs:
        return ""
    number, white, black = pairs[-1]
    if black is not None:
        return "%d...%s" % (number, black)
    return "%d.%s" % (number, white)


def render_plain_board(board: Any, perspective: str = WHITE, theme: Optional[Theme] = None) -> str:
    """Board plus captured material and the last move, for plain (line-oriented) mode."""
    theme = theme or Theme(color=False)
    lines = render_board(board, perspective, None, theme)
    for color, name in ((WHITE, "White"), (BLACK, "Black")):
        pieces = _captured(board, _opposite(color))
        advantage = _advantage(board, color)
        if pieces or advantage > 0:
            text = "".join(piece_glyph(p, theme) for p in pieces)
            if advantage > 0:
                text += " +%d" % advantage
            lines.append("  %s captured: %s" % (name, text.strip()))
    last = _last_move_label(board)
    if last:
        lines.append("  Last move: " + last)
    return "\n".join(lines)


def help_lines() -> List[str]:
    """Command reference shown by /help."""
    return [
        "Moves: type SAN (e4, Nf3, exd5, O-O, e8=Q) or coordinates (e2e4, e7e8q).",
        "/c <msg>        chat with your opponent (also /chat, /say)",
        "/draw           offer a draw, or accept the opponent's offer",
        "/accept         accept the pending draw / takeback / rematch offer",
        "/decline        decline the pending offer",
        "/takeback       ask to take back your last move (also /undo)",
        "/resign         resign the game",
        "/rematch        offer a rematch after the game ends",
        "/flip           flip the board",
        "/moves          list the legal moves",
        "/fen            show the position as FEN",
        "/pgn            show the game as PGN",
        "/save [path]    save the game as a PGN file",
        "/clear          clear the message log",
        "/quit           leave the game (also /exit, /q)",
        "/help           show this help (also ?)",
        "Keys: Up/Down input history, PgUp/PgDn scroll messages, Ctrl-L redraw.",
    ]


@dataclass
class ViewState:
    board: Any = None
    perspective: str = WHITE
    my_color: Optional[str] = None
    white_name: str = "White"
    black_name: str = "Black"
    clock_ms: Optional[Dict[str, int]] = None
    clock_running: Optional[str] = None
    log: List[Tuple[str, str]] = field(default_factory=list)
    status: str = ""
    prompt: str = "> "
    input_buffer: str = ""
    input_cursor: int = 0
    game_over: Optional[str] = None
    connection: str = ""
    pending: str = ""
    log_scroll: int = 0


def _player_name(view: ViewState, color: str) -> str:
    name = _clean(view.white_name if color == WHITE else view.black_name).strip()
    return name or ("White" if color == WHITE else "Black")


def _clock_chip(view: ViewState, color: str, theme: Theme) -> str:
    if view.clock_ms is None or color not in view.clock_ms:
        return ""
    ms = view.clock_ms[color]
    text = format_clock(ms)
    running = view.clock_running == color
    low = ms < LOW_TIME_MS
    if not theme.color:
        return "[%s]" % text if running else " %s " % text
    chip = " %s " % text
    if running and low:
        return theme.paint(chip, fg=WHITE_PIECE, bg=LOW_TIME_BG, bold=True)
    if running:
        return theme.paint(chip, fg=BLACK_PIECE, bg=RUNNING_BG, bold=True)
    if low:
        return theme.paint(chip, fg=LOW_TIME_FG, bold=True)
    return chip


def _captured_text(view: ViewState, color: str, theme: Theme) -> str:
    """Pieces captured by ``color`` plus its material lead, e.g. ``♟♟♞ +5``."""
    pieces = _captured(view.board, _opposite(color))
    text = "".join(piece_glyph(p, theme) for p in pieces)
    advantage = _advantage(view.board, color)
    if advantage > 0:
        text += (" " if text else "") + theme.paint("+%d" % advantage, bold=True)
    return text


def _player_line(view: ViewState, color: str, width: int, theme: Theme, inline_captured: bool = False) -> str:
    sym = _symbols(theme)
    to_move = view.game_over is None and _turn(view.board) == color
    marker = theme.paint(sym.to_move, fg=TURN_FG, bold=True) if to_move else " " * visible_len(sym.to_move)
    name = theme.paint(_player_name(view, color), bold=to_move)
    if view.my_color == color:
        name += theme.paint(" (you)", fg=DIM_FG)
    left = marker + " " + name
    if inline_captured:
        captured = _captured_text(view, color, theme)
        if captured:
            left += "  " + captured
    clock = _clock_chip(view, color, theme)
    room = max(0, width - visible_len(clock) - (1 if clock else 0))
    return pad(truncate(left, room, sym.ellipsis), room) + (" " if clock else "") + clock


def _san_text(san: str, latest: bool, theme: Theme) -> str:
    return theme.paint(san, fg=WHITE_PIECE, bg=LIGHT_HIGHLIGHT, bold=True) if latest else san


def _move_rows(view: ViewState, width: int, rows: int, theme: Theme) -> List[str]:
    """Most recent numbered move pairs laid out row-major in ``rows`` lines (padded with blanks)."""
    if rows <= 0:
        return []
    pairs = _move_pairs(view.board)
    if not pairs:
        return [theme.paint("  No moves yet", fg=DIM_FG)] + [""] * (rows - 1)
    sym = _symbols(theme)
    num_w = max(3, len(str(pairs[-1][0])) + 1)
    cell_w = num_w + 2 + 2 * SAN_WIDTH
    columns = max(1, min(2, (width + 2) // (cell_w + 2))) if rows < 8 else 1
    capacity = rows * columns
    start = 0
    if len(pairs) > capacity:
        start = -(-(len(pairs) - capacity) // columns) * columns
    cells: List[str] = []
    for index in range(start, len(pairs)):
        number, white, black = pairs[index]
        last_pair = index == len(pairs) - 1
        white_text = _san_text(white, last_pair and black is None, theme) if white else sym.ellipsis
        black_text = _san_text(black, last_pair, theme) if black else ""
        number_text = theme.paint(("%d." % number).rjust(num_w), fg=DIM_FG)
        cells.append(number_text + " " + pad(white_text, SAN_WIDTH) + " " + pad(black_text, SAN_WIDTH))
    lines = ["  ".join(cells[i:i + columns]).rstrip() for i in range(0, len(cells), columns)]
    return lines + [""] * (rows - len(lines))


def _inline_moves(view: ViewState, width: int, rows: int, theme: Theme) -> List[str]:
    """Most recent moves as flowing text ("12. O-O Be7  13. c3 d6") in at most ``rows`` lines."""
    if rows <= 0 or width <= 0:
        return []
    pairs = _move_pairs(view.board)
    if not pairs:
        return []
    sym = _symbols(theme)
    tokens: List[str] = []
    for index, (number, white, black) in enumerate(pairs):
        last_pair = index == len(pairs) - 1
        parts = [theme.paint("%d." % number, fg=DIM_FG)]
        parts.append(_san_text(white, last_pair and black is None, theme) if white else sym.ellipsis)
        if black:
            parts.append(_san_text(black, last_pair, theme))
        tokens.append(" ".join(parts))
    lines: List[str] = []
    current: List[str] = []
    current_w = 0
    for token in reversed(tokens):
        token_w = visible_len(token)
        if current and current_w + 2 + token_w > width:
            lines.append("  ".join(reversed(current)))
            if len(lines) == rows:
                break
            current, current_w = [], 0
        if not current:
            current, current_w = [truncate(token, width)], min(token_w, width)
        else:
            current.append(token)
            current_w += 2 + token_w
    else:
        if current:
            lines.append("  ".join(reversed(current)))
    return list(reversed(lines[:rows]))


def _wide_panel(view: ViewState, width: int, rows: int, theme: Theme) -> List[str]:
    """Right-hand panel aligned with the board ranks: top player, captures, moves, captures, bottom player."""
    top = BLACK if view.perspective != BLACK else WHITE
    bottom = _opposite(top)
    lines = [""] * rows
    if rows < 2:
        return [_player_line(view, bottom, width, theme)][:rows]
    lines[0] = _player_line(view, top, width, theme)
    lines[-1] = _player_line(view, bottom, width, theme)
    if rows >= 4:
        lines[1] = "  " + truncate(_captured_text(view, top, theme), width - 2)
        lines[-2] = "  " + truncate(_captured_text(view, bottom, theme), width - 2)
    first, last = (2, rows - 2) if rows >= 4 else (1, rows - 1)
    if rows >= 16:
        rule = theme.paint(_symbols(theme).rule * width, fg=DIM_FG)
        lines[first], lines[last - 1] = rule, rule
        first, last = first + 1, last - 1
    lines[first:last] = _move_rows(view, width, last - first, theme)
    return [truncate(line, width) for line in lines]


def _board_dims(scale: int) -> Tuple[int, int]:
    sq_w, sq_h = SQUARE_SIZES[scale - 1]
    return 3 + 8 * sq_w, 8 * sq_h + 1


def _choose_scale(width: int, rows: int, theme: Theme, side_width: int, extra_rows: int) -> int:
    """Largest board scale leaving ``side_width`` columns and ``extra_rows`` rows (plus a minimal log) free."""
    if not theme.color:
        return 1
    for scale in range(len(SQUARE_SIZES), 1, -1):
        board_w, board_h = _board_dims(scale)
        if board_w + side_width <= width and board_h + extra_rows + 1 + MIN_LOG_ROWS <= rows:
            return scale
    return 1


def _wide_top(view: ViewState, width: int, rows: int, theme: Theme) -> List[str]:
    scale = _choose_scale(width, rows, theme, PANEL_GAP + LARGE_PANEL_MIN_WIDTH, 0)
    board_lines = render_board(view.board, view.perspective, None, theme, scale)
    board_w = visible_len(board_lines[0])
    panel_w = min(PANEL_MAX_WIDTH, width - board_w - PANEL_GAP)
    panel = _wide_panel(view, panel_w, len(board_lines) - 1, theme)
    gap = " " * PANEL_GAP
    return [line + gap + panel[i] if i < len(panel) and panel[i] else line for i, line in enumerate(board_lines)]


def _narrow_top(view: ViewState, width: int, rows: int, room: int, theme: Theme) -> List[str]:
    """Board with the panel stacked below it: both players (with captures), then recent moves."""
    scale = _choose_scale(width, rows, theme, 0, 3)
    lines = render_board(view.board, view.perspective, None, theme, scale)
    board_w = visible_len(lines[0])
    panel_w = min(width, max(board_w, 40))
    top = BLACK if view.perspective != BLACK else WHITE
    lines.append(_player_line(view, top, panel_w, theme, inline_captured=True))
    lines.append(_player_line(view, _opposite(top), panel_w, theme, inline_captured=True))
    move_rows = max(0, min(2, room - len(lines) - 1 - MIN_LOG_ROWS))
    lines.extend(" " + line for line in _inline_moves(view, panel_w - 1, move_rows, theme))
    return lines


def _wrap(text: str, width: int) -> List[str]:
    """Word-wrap plain text to ``width`` columns; continuation lines are indented by two spaces."""
    if width <= 0:
        return []
    if visible_len(text) <= width:
        return [text]
    indent = "  " if width > 12 else ""
    lines: List[str] = []
    current = ""
    for word in text.split(" "):
        limit = width - (len(indent) if lines else 0)
        candidate = word if not current else current + " " + word
        if visible_len(candidate) <= limit:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
            limit = width - len(indent)
        while visible_len(word) > limit:
            head = truncate(word, limit)
            if not head:
                break
            lines.append(head)
            word = word[len(head):]
            limit = width - len(indent)
        current = word
    if current or not lines:
        lines.append(current)
    return [line if i == 0 else indent + line for i, line in enumerate(lines)]


def _log_lines(view: ViewState, width: int, rows: int, theme: Theme) -> List[str]:
    """The log as wrapped, styled display lines: the most recent ``rows`` lines, scrolled back by ``log_scroll``."""
    if rows <= 0:
        return []
    offset = abs(int(view.log_scroll or 0))
    needed = rows + offset
    collected: List[str] = []
    entries = list(view.log)
    for entry in reversed(entries):
        kind, text = (entry[0], entry[1]) if len(entry) >= 2 else ("info", entry[0] if entry else "")
        style = _LOG_STYLES.get(str(kind), {})
        wrapped = [
            theme.paint(line, **style) if line else line
            for segment in str(text).replace("\r\n", "\n").split("\n")
            for line in _wrap(_clean(segment), width)
        ]
        collected[:0] = wrapped
        if len(collected) >= needed:
            break
    offset = min(offset, max(0, len(collected) - rows))
    end = len(collected) - offset
    visible = collected[max(0, end - rows):end]
    if offset and visible:
        sym = _symbols(theme)
        hint = "%s %d newer line%s below (PgDn)" % (sym.down, offset, "" if offset == 1 else "s")
        visible[-1] = theme.paint(truncate(hint, width), fg=DIM_FG)
    return visible


def _status_line(view: ViewState, width: int, theme: Theme) -> str:
    sym = _symbols(theme)
    left = _clean(view.status).strip()
    if view.game_over:
        over = theme.paint(_clean(view.game_over), fg=GAME_OVER_FG if theme.color else None, bold=True)
        left = over + ("  " + left if left else "")
    right = _clean(view.connection).strip()
    inner = width - 2
    if right and visible_len(left) + 3 + visible_len(right) > inner:
        right = truncate(right, max(0, inner - visible_len(left) - 3), sym.ellipsis)
        if visible_len(right) < 6:
            right = ""
    if right:
        left = truncate(left, inner - visible_len(right) - 2, sym.ellipsis)
        body = " " + pad(left, inner - visible_len(right)) + right + " "
    else:
        body = " " + pad(truncate(left, inner, sym.ellipsis), inner) + " "
    if not theme.color:
        return truncate(body.rstrip(), width)
    painted = body.replace(RESET, RESET + sgr(48, 5, STATUS_BG, 38, 5, STATUS_FG))
    return truncate(sgr(48, 5, STATUS_BG, 38, 5, STATUS_FG) + painted + RESET, width)


def _pending_line(view: ViewState, width: int, theme: Theme) -> str:
    sym = _symbols(theme)
    flag = theme.paint(sym.flag, fg=PENDING_FG, bold=True)
    text = theme.paint(_clean(view.pending).strip(), bold=True)
    return truncate(" " + flag + " " + text, width, sym.ellipsis)


def _scroll_input(text: str, cursor: int, avail: int, theme: Theme) -> Tuple[str, int]:
    """Horizontal window of the input buffer that keeps the caret visible; returns (text, caret column)."""
    sym = _symbols(theme)
    cursor = max(0, min(cursor, len(text)))
    if avail <= 0:
        return "", 0
    widths = [_char_width(ch) for ch in text]
    if sum(widths) < avail:
        return text, sum(widths[:cursor])
    more_left, more_right = theme.paint(sym.more_left, fg=DIM_FG), theme.paint(sym.more_right, fg=DIM_FG)
    left_w, right_w = visible_len(sym.more_left), visible_len(sym.more_right)
    caret_w = widths[cursor] if cursor < len(text) else 1
    target = avail - caret_w - (right_w if cursor + 1 < len(text) else 0)
    start = 0
    if sum(widths[:cursor]) > target:
        target -= left_w
        start, used = cursor, 0
        while start > 0 and used + widths[start - 1] <= target:
            used += widths[start - 1]
            start -= 1
    room = avail - (left_w if start else 0)
    end, used = start, 0
    while end < len(text) and used + widths[end] <= room:
        used += widths[end]
        end += 1
    if end < len(text):
        room -= right_w
        while end > start and used > room:
            end -= 1
            used -= widths[end]
    caret = (left_w if start else 0) + sum(widths[start:cursor])
    shown = (more_left if start else "") + text[start:end] + (more_right if end < len(text) else "")
    return shown, min(caret, avail - 1)


def _prompt_line(view: ViewState, width: int, theme: Theme) -> Tuple[str, int]:
    prompt = _clean(view.prompt)
    prompt = truncate(prompt, max(0, width - 2)) if width > 2 else ""
    my_turn = view.game_over is None and (view.my_color is None or _turn(view.board) == view.my_color)
    painted = theme.paint(prompt, fg=TURN_FG if my_turn else None, bold=True)
    prompt_w = visible_len(prompt)
    buffer = _UNSAFE_RE.sub("?", str(view.input_buffer).replace("\t", " "))
    shown, caret = _scroll_input(buffer, view.input_cursor, width - prompt_w, theme)
    return painted + shown, min(prompt_w + caret, max(0, width - 1))


def render_screen(view: ViewState, width: int, height: int, theme: Optional[Theme] = None) -> Tuple[str, Tuple[int, int]]:
    """Compose the full frame (exactly ``height`` lines, each at most ``width`` columns) and the caret position."""
    theme = theme or Theme()
    width, height = max(1, int(width)), max(1, int(height))
    prompt, caret_col = _prompt_line(view, width, theme)
    footer = [_status_line(view, width, theme)]
    if view.pending:
        footer.insert(0, _pending_line(view, width, theme))
    footer = footer[max(0, len(footer) - (height - 1)):]
    room = height - 1 - len(footer)
    stable_rows = height - 3
    if width >= WIDE_MIN_WIDTH:
        top = _wide_top(view, width, stable_rows, theme)
    else:
        top = _narrow_top(view, width, stable_rows, room, theme)
    top = top[max(0, len(top) - room):] if room > 0 else []
    spare = room - len(top)
    separator = 1 if spare >= 2 else 0
    log = _log_lines(view, width, spare - separator, theme)
    filler = [""] * (spare - len(log))
    lines = [truncate(line, width) for line in top + filler + log + footer + [prompt]]
    return "\n".join(lines), (len(lines) - 1, caret_col)

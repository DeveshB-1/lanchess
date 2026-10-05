"""Pure rendering for the terminal UI: every function returns strings and never prints."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import sprites

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

# Sprite pieces (the large and xl board sizes): every pixel is the square colour, the fill or the
# outline/detail colour. White is a white fill with a near-black outline; Black a black fill with a
# light grey outline, so both stand out on light, dark, last-move and check squares alike.
SPRITE_WHITE_FILL = 231
SPRITE_WHITE_LINE = 234
SPRITE_BLACK_FILL = 16
SPRITE_BLACK_LINE = 248
MONO_MIN_SPRITE = 14  # without colour only the outlined sprites (14 and 16 pixels) are used

LOW_TIME_MS = 20000
WIDE_MIN_WIDTH = 70
PANEL_GAP = 2
PANEL_MAX_WIDTH = 44
LARGE_PANEL_MIN_WIDTH = 32
SPRITE_PANEL_MIN_WIDTH = 30
SPRITE_PANEL_MAX_WIDTH = 60
MIN_LOG_ROWS = 3
LOG_BELOW_MIN_ROWS = 8  # sprite boards show the log below the board when this many rows are free there
SAN_WIDTH = 7
FOOTER_ROWS = 3  # offer line, status bar and prompt: kept free so the board never jumps when an offer comes

# The board sizes a player can choose ("auto" picks the biggest that fits the window).
BOARD_SIZES = ("auto", "small", "medium", "large", "xl")
_TIER_RANK = {"small": 1, "medium": 2, "large": 3, "xl": 4}


@dataclass(frozen=True)
class BoardLevel:
    """One board geometry: its size tier, the square size in columns x rows and the sprite size.

    ``sprite`` is 0 for text squares (one glyph per square); otherwise the pieces are drawn as
    ``sprite`` x ``sprite`` pixel block art, two pixels per cell (so ``sq_w == sprite`` and
    ``sq_h == sprite // 2``: terminal cells are about twice as tall as wide, so pixels are square).
    """

    tier: str
    sq_w: int
    sq_h: int
    sprite: int = 0


# Smallest first; ``render_board``'s ``scale`` is a 1-based index into this table.
BOARD_LEVELS = (
    BoardLevel("small", 3, 1),
    BoardLevel("medium", 5, 2),
    BoardLevel("medium", 7, 3),
    BoardLevel("large", 8, 4, 8),
    BoardLevel("large", 10, 5, 10),
    BoardLevel("xl", 12, 6, 12),
    BoardLevel("xl", 14, 7, 14),
    BoardLevel("xl", 16, 8, 16),
)

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


def normalize_board_size(value: Any) -> str:
    """A board size name from BOARD_SIZES (case and spaces ignored); anything else means "auto"."""
    if isinstance(value, str) and value.strip().lower() in BOARD_SIZES:
        return value.strip().lower()
    return "auto"


def next_board_size(current: Any) -> str:
    """The board size after ``current`` in BOARD_SIZES order (after "xl" comes "auto" again)."""
    index = BOARD_SIZES.index(normalize_board_size(current))
    return BOARD_SIZES[(index + 1) % len(BOARD_SIZES)]


def _allowed_levels(theme: Theme, board_size: str = "auto") -> List[int]:
    """The 1-based BOARD_LEVELS a theme can draw, up to the tier of ``board_size`` (smallest first).

    Sprites need Unicode block characters, so --ascii keeps to the letter boards. Without colour
    the bigger text squares and the silhouette sprites would not show the squares or the sides,
    so there are only the compact board and the outlined sprites (drawn in black and white like a
    printed diagram, see ``_mono_square``).
    """
    size = normalize_board_size(board_size)
    cap = _TIER_RANK.get(size, len(_TIER_RANK))
    levels = []
    for index, level in enumerate(BOARD_LEVELS, 1):
        if _TIER_RANK[level.tier] > cap:
            continue
        if level.sprite and not theme.unicode:
            continue
        if not theme.color and (index > 1 and level.sprite < MONO_MIN_SPRITE):
            continue
        levels.append(index)
    return levels


def _board_dims(scale: int) -> Tuple[int, int]:
    """Columns and rows of the board at a level, including the rank labels and the file-label row."""
    level = BOARD_LEVELS[scale - 1]
    return 3 + 8 * level.sq_w, 8 * level.sq_h + 1


def board_layout(width: int, height: int, theme: Optional[Theme] = None, board_size: str = "auto") -> Tuple[int, str]:
    """The board level (1-based index into BOARD_LEVELS) and layout for a ``width`` x ``height`` frame.

    The biggest level the theme allows (capped at the tier of ``board_size``; "auto" allows all)
    whose board fits is used, so the pieces grow and shrink with the window. Layouts:

    * ``wide``: the board with the side panel to its right (players, clocks, captures, moves; for
      sprite boards also the message log, so the board can use nearly the whole height);
    * ``stacked``: the panel below the board (narrow windows, or tall windows too narrow for a
      big board plus a side panel).

    Three rows stay free for the offer line, status bar and prompt; text boards also keep a
    small log below the board. A board that would not fit is never chosen, except the compact
    board when nothing fits at all.
    """
    theme = theme or Theme()
    width, height = max(1, int(width)), max(1, int(height))
    rows = height - FOOTER_ROWS
    allowed = _allowed_levels(theme, board_size)
    wide_ok = width >= WIDE_MIN_WIDTH
    for scale in reversed(allowed):
        board_w, board_h = _board_dims(scale)
        if wide_ok:
            if BOARD_LEVELS[scale - 1].sprite:
                if board_w + PANEL_GAP + SPRITE_PANEL_MIN_WIDTH <= width and board_h <= rows:
                    return scale, "wide"
            elif board_w + PANEL_GAP + LARGE_PANEL_MIN_WIDTH <= width and board_h + 1 + MIN_LOG_ROWS <= rows:
                return scale, "wide"
        if board_w <= width and board_h + 3 + 1 + MIN_LOG_ROWS <= rows:
            return scale, "stacked"
    return allowed[0], "wide" if wide_ok else "stacked"


def board_tier(width: int, height: int, theme: Optional[Theme] = None, board_size: str = "auto") -> str:
    """The size tier (small, medium, large or xl) actually shown for a frame of this size."""
    return BOARD_LEVELS[board_layout(width, height, theme, board_size)[0] - 1].tier


def _usable_level(scale: Any, theme: Theme) -> int:
    """The biggest level the theme can draw that is not bigger than ``scale``."""
    try:
        wanted = int(scale)
    except (TypeError, ValueError):
        wanted = 1
    wanted = max(1, min(wanted, len(BOARD_LEVELS)))
    return max(level for level in _allowed_levels(theme) if level <= wanted)


def render_board(
    board: Any,
    perspective: str = WHITE,
    last_move: Any = None,
    theme: Optional[Theme] = None,
    scale: int = 1,
) -> List[str]:
    """Board lines: 8 ranks with labels on the left, file labels below, flipped for Black.

    ``last_move`` defaults to ``board.last_move()``. ``scale`` is a 1-based index into BOARD_LEVELS
    (1: 3x1, 2: 5x2, 3: 7x3 text squares; 4-8: sprite pieces on 8x4 up to 16x8 squares), lowered
    to the biggest level the theme can draw (``_allowed_levels``). All lines have equal width.
    """
    theme = theme or Theme()
    scale = _usable_level(scale, theme)
    level = BOARD_LEVELS[scale - 1]
    sq_w, sq_h = level.sq_w, level.sq_h
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
        squares = []
        for file in files:
            sq = rank * 8 + file
            piece = _piece_at(board, sq)
            light = (rank + file) % 2 == 1
            if level.sprite and theme.color:
                squares.append(_sprite_square(piece, _square_bg(light, sq in marked, sq == check), scale))
            elif level.sprite:
                mark = "check" if sq == check else "last" if sq in marked else ""
                squares.append(_mono_square(piece, light, mark, scale))
            elif theme.color:
                cells = [_color_square(piece if row == mid else None, light, sq in marked, sq == check, sq_w, theme)
                         for row in range(sq_h)]
                squares.append(cells)
            else:
                squares.append([_plain_square(piece, sq in marked, theme)])
        for row in range(sq_h):
            label = " " + theme.paint(str(rank + 1), fg=COORD_FG) + " " if row == mid else "   "
            line = label + "".join(cells[row] for cells in squares)
            lines.append(line + RESET if theme.color else line)
    labels = "".join(_square_text("abcdefgh"[f], sq_w) for f in files)
    lines.append("   " + theme.paint(labels, fg=COORD_FG))
    return lines


def _square_bg(light: bool, marked: bool, check: bool) -> int:
    if check:
        return CHECK_SQUARE
    if marked:
        return LIGHT_HIGHLIGHT if light else DARK_HIGHLIGHT
    return LIGHT_SQUARE if light else DARK_SQUARE


def _color_square(piece: Optional[str], light: bool, marked: bool, check: bool, width: int, theme: Theme) -> str:
    bg = _square_bg(light, marked, check)
    if piece is None:
        return sgr(0, 48, 5, bg) + " " * width
    fg = WHITE_PIECE if piece.isupper() else BLACK_PIECE
    glyph = _SOLID_GLYPHS[piece.lower()] if theme.unicode else piece.upper()
    return sgr(0, 1, 38, 5, fg, 48, 5, bg) + _square_text(glyph, width)


def _plain_square(piece: Optional[str], marked: bool, theme: Theme) -> str:
    content = piece_glyph(piece, theme) if piece else "."
    return "[" + content + "]" if marked else " " + content + " "


# Rendered square rows, keyed by (piece or None, square colour or mark, level): a board is at most
# 13 pieces x 5 square colours per level, and a frame then only joins cached strings.
_SQUARE_CACHE: Dict[Tuple[Any, ...], List[str]] = {}
_UPPER_HALF = "▀"


def _sprite_colors(piece: str) -> Tuple[Optional[int], int, int, int]:
    """Colour per sprite pixel kind (EMPTY, FILL, LINE, DETAIL); None is the square colour."""
    if piece.isupper():
        return None, SPRITE_WHITE_FILL, SPRITE_WHITE_LINE, SPRITE_WHITE_LINE
    return None, SPRITE_BLACK_FILL, SPRITE_BLACK_LINE, SPRITE_BLACK_LINE


def _sprite_square(piece: Optional[str], bg: int, scale: int) -> List[str]:
    """The rows of one square with a block-art piece: each cell is an upper half block whose
    foreground is the top pixel and background the bottom pixel (a space where both match)."""
    key = (piece, bg, scale)
    cached = _SQUARE_CACHE.get(key)
    if cached is not None:
        return cached
    level = BOARD_LEVELS[scale - 1]
    if piece is None or piece.upper() not in "KQRBNP":
        rows = [sgr(0, 48, 5, bg) + " " * level.sq_w] * level.sq_h
    else:
        grid = sprites.pixels(level.sprite, piece)
        palette = _sprite_colors(piece)
        rows = []
        for row in range(level.sq_h):
            top_row, bottom_row = grid[2 * row], grid[2 * row + 1]
            out: List[str] = []
            codes: List[object] = [0]
            fg: Optional[int] = None
            cur_bg: Optional[int] = None
            for col in range(level.sq_w):
                top = palette[top_row[col]]
                bottom = palette[bottom_row[col]]
                top = bg if top is None else top
                bottom = bg if bottom is None else bottom
                if bottom != cur_bg:
                    codes += [48, 5, bottom]
                    cur_bg = bottom
                if top != bottom and top != fg:
                    codes += [38, 5, top]
                    fg = top
                if codes:
                    out.append(sgr(*codes))
                    codes = []
                out.append(" " if top == bottom else _UPPER_HALF)
            rows.append("".join(out))
    _SQUARE_CACHE[key] = rows
    return rows


_MONO_CELLS = {(False, False): " ", (True, False): "▀", (False, True): "▄", (True, True): "█"}
_MONO_SHADE = "░"
_MONO_CORNERS = {"last": "┌┐└┘", "check": "╔╗╚╝"}


def _mono_square(piece: Optional[str], light: bool, mark: str, scale: int) -> List[str]:
    """A sprite square without colour, drawn like a printed diagram: dark squares are shaded,
    White pieces are outlines (the outline and detail pixels are ink, the fill is blank) and Black
    pieces are solid ink with their detail lines left blank. Corner marks show the last move
    (light corners) and the king in check (double corners)."""
    key = (piece, light, mark, scale)
    cached = _SQUARE_CACHE.get(key)
    if cached is not None:
        return cached
    level = BOARD_LEVELS[scale - 1]
    size, sq_w, sq_h = level.sprite, level.sq_w, level.sq_h
    ink = [[False] * size for _ in range(size)]
    covered = [[False] * size for _ in range(size)]
    if piece is not None and piece.upper() in "KQRBNP":
        inked = (sprites.LINE, sprites.DETAIL) if piece.isupper() else (sprites.FILL, sprites.LINE)
        for r, pixel_row in enumerate(sprites.pixels(size, piece)):
            for c, kind in enumerate(pixel_row):
                covered[r][c] = kind != sprites.EMPTY
                ink[r][c] = kind in inked
    corners = _MONO_CORNERS.get(mark, "")
    corner_cells = {(0, 0): 0, (0, sq_w - 1): 1, (sq_h - 1, 0): 2, (sq_h - 1, sq_w - 1): 3}
    rows = []
    for row in range(sq_h):
        out = []
        for col in range(sq_w):
            top, bottom = 2 * row, 2 * row + 1
            if covered[top][col] or covered[bottom][col]:
                out.append(_MONO_CELLS[ink[top][col], ink[bottom][col]])
            elif corners and (row, col) in corner_cells:
                out.append(corners[corner_cells[row, col]])
            else:
                out.append(" " if light else _MONO_SHADE)
        rows.append("".join(out))
    _SQUARE_CACHE[key] = rows
    return rows


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
        "/size [size]    board size: auto, small, medium, large, xl (alone: next)",
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
    board_size: str = "auto"


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


def _wide_panel(view: ViewState, width: int, rows: int, theme: Theme,
                with_log: bool = False) -> Tuple[List[str], bool]:
    """Right-hand panel aligned with the board ranks: top player, captures, moves, captures, bottom player.

    ``with_log`` (sprite boards, which take nearly the whole height) also puts the message log in
    the panel, below the moves, newest line at the bottom, if the panel is tall enough. Returns
    the lines and whether the log is in them.
    """
    top = BLACK if view.perspective != BLACK else WHITE
    bottom = _opposite(top)
    lines = [""] * rows
    if rows < 2:
        return [_player_line(view, bottom, width, theme)][:rows], False
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
        with_log = with_log and last - first >= 12
        if with_log:
            move_rows = max(4, min(16, (last - first) // 3))
            log_rows = last - first - move_rows - 1
            log = _log_lines(view, width, log_rows, theme)
            lines[last - log_rows:last] = [""] * (log_rows - len(log)) + log
            last -= log_rows + 1
            lines[last] = rule
    else:
        with_log = False
    lines[first:last] = _move_rows(view, width, last - first, theme)
    return [truncate(line, width) for line in lines], with_log


def _wide_top(view: ViewState, width: int, height: int, scale: int, theme: Theme) -> Tuple[List[str], bool]:
    """The board with the side panel; also says whether the panel holds the message log.

    Sprite boards take nearly the whole height, so their log goes into the panel unless the
    window leaves at least LOG_BELOW_MIN_ROWS rows below the board anyway.
    """
    board_lines = render_board(view.board, view.perspective, None, theme, scale)
    board_w = visible_len(board_lines[0])
    below = height - FOOTER_ROWS - len(board_lines)
    with_log = bool(BOARD_LEVELS[scale - 1].sprite) and below < LOG_BELOW_MIN_ROWS
    panel_w = min(SPRITE_PANEL_MAX_WIDTH if with_log else PANEL_MAX_WIDTH, width - board_w - PANEL_GAP)
    panel, log_in_panel = _wide_panel(view, panel_w, len(board_lines) - 1, theme, with_log)
    gap = " " * PANEL_GAP
    lines = [line + gap + panel[i] if i < len(panel) and panel[i] else line for i, line in enumerate(board_lines)]
    return lines, log_in_panel


def _narrow_top(view: ViewState, width: int, room: int, scale: int, theme: Theme) -> List[str]:
    """Board with the panel stacked below it: both players (with captures), then recent moves."""
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
    """Compose the full frame (exactly ``height`` lines, each at most ``width`` columns) and the caret position.

    The board size follows ``view.board_size`` and the frame size (see ``board_layout``), so it is
    recomputed on every call and a resized window gets bigger or smaller pieces at once.
    """
    theme = theme or Theme()
    width, height = max(1, int(width)), max(1, int(height))
    prompt, caret_col = _prompt_line(view, width, theme)
    footer = [_status_line(view, width, theme)]
    if view.pending:
        footer.insert(0, _pending_line(view, width, theme))
    footer = footer[max(0, len(footer) - (height - 1)):]
    room = height - 1 - len(footer)
    scale, layout = board_layout(width, height, theme, view.board_size)
    log_in_panel = False
    if layout == "wide":
        top, log_in_panel = _wide_top(view, width, height, scale, theme)
    else:
        top = _narrow_top(view, width, room, scale, theme)
    top = top[max(0, len(top) - room):] if room > 0 else []
    spare = room - len(top)
    separator = 1 if spare >= 2 else 0
    log = [] if log_in_panel else _log_lines(view, width, spare - separator, theme)
    filler = [""] * (spare - len(log))
    lines = [truncate(line, width) for line in top + filler + log + footer + [prompt]]
    return "\n".join(lines), (len(lines) - 1, caret_col)

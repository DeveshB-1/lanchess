"""Tests for the board sizes: the sprite art, board levels and layout, /size and the board_size setting."""

from __future__ import annotations

import itertools
import os
import re
import tempfile
import time
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from lanchess import config, game, menu, sprites, term, ui
from lanchess.engine import Board
from lanchess.ui import BOARD_LEVELS, BOARD_SIZES, Theme, ViewState, board_layout, board_tier, render_board
from lanchess.ui import render_screen, visible_len

PLAIN = Theme(unicode=False, color=False)
MONO = Theme(unicode=True, color=False)
COLOR = Theme(unicode=True, color=True)
COLOR_ASCII = Theme(unicode=False, color=True)
THEMES = (PLAIN, MONO, COLOR, COLOR_ASCII)
SIZES = ((40, 14), (80, 24), (100, 30), (120, 40), (160, 50), (200, 60), (250, 70))
CORNERS_FEN = "q3k2b/8/8/8/8/8/8/R3K2N w - - 0 1"  # a1 R, h1 N, a8 q, h8 b
TOKEN = re.compile(r"\x1b\[([0-9;]*)m|(.)", re.S)


def sprite_levels() -> List[int]:
    return [index for index, level in enumerate(BOARD_LEVELS, 1) if level.sprite]


def cells(line: str) -> List[Tuple[str, Optional[int], Optional[int]]]:
    """Decode a rendered line into (character, fg, bg) per column (256-colour SGR only)."""
    out: List[Tuple[str, Optional[int], Optional[int]]] = []
    fg: Optional[int] = None
    bg: Optional[int] = None
    for match in TOKEN.finditer(line):
        if match.group(2) is not None:
            out.append((match.group(2), fg, bg))
            continue
        codes = [int(code) if code else 0 for code in match.group(1).split(";")]
        i = 0
        while i < len(codes):
            if codes[i] == 0:
                fg = bg = None
            elif codes[i] == 38:
                fg, i = codes[i + 2], i + 2
            elif codes[i] == 48:
                bg, i = codes[i + 2], i + 2
            i += 1
    return out


def square_pixels(lines: List[str], row0: int, col0: int, level: ui.BoardLevel) -> List[List[Optional[int]]]:
    """The pixel colours of one rendered sprite square (two pixels per cell: upper half block or space)."""
    grid: List[List[Optional[int]]] = []
    for row in range(level.sq_h):
        decoded = cells(lines[row0 + row])
        top: List[Optional[int]] = []
        bottom: List[Optional[int]] = []
        for col in range(level.sq_w):
            char, fg, bg = decoded[col0 + col]
            if char == "▀":
                top.append(fg)
                bottom.append(bg)
            else:
                assert char == " ", repr(char)
                top.append(bg)
                bottom.append(bg)
        grid += [top, bottom]
    return grid


def expected_pixels(piece: str, size: int, square: int) -> List[List[int]]:
    white = piece.isupper()
    fill = ui.SPRITE_WHITE_FILL if white else ui.SPRITE_BLACK_FILL
    line = ui.SPRITE_WHITE_LINE if white else ui.SPRITE_BLACK_LINE
    colours = {sprites.EMPTY: square, sprites.FILL: fill, sprites.LINE: line, sprites.DETAIL: line}
    return [[colours[kind] for kind in row] for row in sprites.pixels(size, piece)]


def busy_view(**overrides: Any) -> ViewState:
    board = Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "Ng5", "d5", "exd5", "Nxd5", "Nxf7", "Kxf7", "Qf3+"]:
        board.push_san(san)
    log = [("info", "line %03d" % i) for i in range(120)]
    log.append(("chat_them", "Bob: " + "a long chat message " * 12 + "THE-END"))
    view = ViewState(board=board, my_color="w", white_name="Alice the Magnificent", black_name="Bob",
                     clock_ms={"w": 183400, "b": 15300}, clock_running="b", log=log, status="Waiting for Bob",
                     connection="Connected to 192.168.100.200:5555",
                     pending="Draw offered by opponent - /accept or /decline", input_buffer="Nf3", input_cursor=3)
    for key, value in overrides.items():
        setattr(view, key, value)
    return view


class SpriteArtTest(unittest.TestCase):
    def test_every_size_has_all_pieces_with_the_same_dimensions(self) -> None:
        self.assertEqual(set(sprites.SIZES), {level.sprite for level in BOARD_LEVELS if level.sprite})
        for size in sprites.SIZES:
            self.assertEqual(sorted(sprites.ART[size]), sorted("KQRBNP"), size)
            for piece, art in sprites.ART[size].items():
                label = "%s%d" % (piece, size)
                self.assertEqual(len(art), size, label)
                self.assertTrue(all(len(row) == size for row in art), label)
                self.assertTrue(set("".join(art)) <= set(".o#x"), label)
                self.assertGreaterEqual(sum(ch == "o" for row in art for ch in row), size, label)

    def test_margins_baseline_and_symmetry(self) -> None:
        for size in sprites.SIZES:
            tops = {}
            for piece, art in sprites.ART[size].items():
                label = "%s%d" % (piece, size)
                mask = [[ch != "." for ch in row] for row in art]
                self.assertFalse(any(mask[-1]), label + ": the bottom row keeps vertical neighbours apart")
                self.assertFalse(any(row[0] or row[-1] for row in mask), label + ": side margins")
                used = [index for index, row in enumerate(mask) if any(row)]
                self.assertEqual(used[-1], size - 2, label + ": every piece stands on the same baseline")
                self.assertEqual(used, list(range(used[0], used[-1] + 1)), label + ": one connected shape")
                tops[piece] = used[0]
                if piece != "N":
                    self.assertTrue(all(row == row[::-1] for row in mask), label + ": symmetric outline")
            self.assertEqual(min(tops.values()), tops["K"], "the king is the tallest piece (%d)" % size)
            self.assertEqual(max(tops.values()), tops["P"], "the pawn is the smallest piece (%d)" % size)

    def test_pieces_have_their_own_heads(self) -> None:
        """No two pieces share the top three rows of their art (the king once had the pawn's head
        at 8 px and the bishop the king's cross at 12 px), and the 8 px queen's body is not the rook's."""
        for size in sprites.SIZES:
            heads = {}
            for piece, art in sprites.ART[size].items():
                used = [row for row in art if row.strip(".")]
                heads.setdefault(tuple(used[:3]), []).append(piece)
            self.assertEqual([pieces for pieces in heads.values() if len(pieces) > 1], [], size)
        queen, rook = sprites.ART[8]["Q"], sprites.ART[8]["R"]
        self.assertNotEqual(queen[3:6], rook[3:6])
        self.assertIn("...oo...", queen[3:5], "the 8 px queen has a narrow waist")

    def test_levels_have_square_pixels(self) -> None:
        for level in BOARD_LEVELS:
            if level.sprite:
                self.assertEqual((level.sq_w, level.sq_h * 2), (level.sprite, level.sprite))
        self.assertEqual([level.tier for level in BOARD_LEVELS],
                         ["small", "medium", "medium", "large", "large", "xl", "xl", "xl"])


class SpriteBoardTest(unittest.TestCase):
    def test_board_dimensions_at_every_level(self) -> None:
        for scale, level in enumerate(BOARD_LEVELS, 1):
            lines = render_board(Board(), "w", theme=COLOR, scale=scale)
            self.assertEqual(len(lines), 8 * level.sq_h + 1, scale)
            self.assertEqual({visible_len(line) for line in lines}, {3 + 8 * level.sq_w}, scale)

    def test_pixels_match_the_art_and_orientation(self) -> None:
        board = Board(CORNERS_FEN)
        for scale in sprite_levels():
            level = BOARD_LEVELS[scale - 1]
            for perspective in ("w", "b"):
                lines = render_board(board, perspective, theme=COLOR, scale=scale)
                bottom, right = 7 * level.sq_h, 3 + 7 * level.sq_w
                a1 = (bottom, 3) if perspective == "w" else (0, right)
                h8 = (0, right) if perspective == "w" else (bottom, 3)
                a8 = (0, 3) if perspective == "w" else (bottom, right)
                label = "%d %s" % (level.sprite, perspective)
                self.assertEqual(square_pixels(lines, a1[0], a1[1], level),
                                 expected_pixels("R", level.sprite, ui.DARK_SQUARE), label)
                self.assertEqual(square_pixels(lines, h8[0], h8[1], level),
                                 expected_pixels("b", level.sprite, ui.DARK_SQUARE), label)
                self.assertEqual(square_pixels(lines, a8[0], a8[1], level),
                                 expected_pixels("q", level.sprite, ui.LIGHT_SQUARE), label)
                label_row = a1[0] + level.sq_h // 2
                self.assertTrue(ui.strip_ansi(lines[label_row]).startswith(" 1 "), label)
                files = ui.strip_ansi(lines[-1])
                order = "abcdefgh" if perspective == "w" else "hgfedcba"
                self.assertEqual(files.split(), list(order), label)
                for index, letter in enumerate(order):
                    self.assertEqual(files[3 + index * level.sq_w + (level.sq_w - 1) // 2], letter, label)

    def test_highlight_and_check_squares(self) -> None:
        board = Board()
        for san in ["e4", "e5", "Qh5", "Nc6", "Qxf7+"]:
            board.push_san(san)
        scale = sprite_levels()[0]
        level = BOARD_LEVELS[scale - 1]
        lines = render_board(board, "w", theme=COLOR, scale=scale)
        king = square_pixels(lines, 0, 3 + 4 * level.sq_w, level)  # e8, in check
        self.assertEqual(king, expected_pixels("k", level.sprite, ui.CHECK_SQUARE))
        queen = square_pixels(lines, 1 * level.sq_h, 3 + 5 * level.sq_w, level)  # f7, last move to
        self.assertEqual(queen, expected_pixels("Q", level.sprite, ui.LIGHT_HIGHLIGHT))
        origin = square_pixels(lines, 3 * level.sq_h, 3 + 7 * level.sq_w, level)  # h5, last move from
        self.assertEqual({pixel for row in origin for pixel in row}, {ui.LIGHT_HIGHLIGHT})

    def test_squares_are_cached(self) -> None:
        scale = sprite_levels()[-1]
        first = ui._sprite_square("K", ui.LIGHT_SQUARE, scale)
        self.assertIs(ui._sprite_square("K", ui.LIGHT_SQUARE, scale), first)
        render_board(Board(), "w", theme=COLOR, scale=scale)
        self.assertIs(ui._sprite_square("K", ui.LIGHT_SQUARE, scale), first)

    def test_rendering_stays_fast(self) -> None:
        view = busy_view()
        render_screen(view, 250, 70, COLOR)
        start = time.perf_counter()
        for _ in range(20):
            render_screen(view, 250, 70, COLOR)
        self.assertLess(time.perf_counter() - start, 2.0)

    def test_no_colour_draws_outlined_white_and_solid_black(self) -> None:
        for scale in sprite_levels():
            lines = render_board(Board(), "w", theme=MONO, scale=scale)
            text = "\n".join(lines)
            self.assertNotIn("\x1b", text)
            level = BOARD_LEVELS[ui._usable_level(scale, MONO) - 1]
            if level.sprite < ui.MONO_MIN_SPRITE:
                self.assertEqual(len(lines), 9, "small sprites fall back to the compact board without colour")
                continue
            self.assertEqual(len(lines), 8 * level.sq_h + 1)
            white_rook = [line[3:3 + level.sq_w] for line in lines[7 * level.sq_h:8 * level.sq_h]]
            black_rook = [line[3:3 + level.sq_w] for line in lines[:level.sq_h]]
            ink = lambda rows: sum(row.count("█") for row in rows)  # noqa: E731
            self.assertGreater(ink(black_rook), 2 * ink(white_rook), "Black is solid, White an outline")
            self.assertIn("░", "".join(lines[:8 * level.sq_h]), "dark squares are shaded")

    def test_no_colour_marks_last_move_and_check(self) -> None:
        board = Board()
        for san in ["e4", "e5", "Qh5", "Nc6", "Qxf7+"]:
            board.push_san(san)
        lines = render_board(board, "w", theme=MONO, scale=len(BOARD_LEVELS))
        level = BOARD_LEVELS[-1]
        e8_top = lines[0][3 + 4 * level.sq_w:3 + 5 * level.sq_w]
        h5_top = lines[3 * level.sq_h][3 + 7 * level.sq_w:3 + 8 * level.sq_w]
        self.assertEqual((e8_top[0], e8_top[-1]), ("╔", "╗"))
        self.assertEqual((h5_top[0], h5_top[-1]), ("┌", "┐"))
        # Check is a double frame all around the square, the last move only light corners.
        e8_sides = [lines[row][3 + 4 * level.sq_w] + lines[row][2 + 5 * level.sq_w] for row in range(1, level.sq_h - 1)]
        h5_sides = [lines[3 * level.sq_h + row][3 + 7 * level.sq_w] for row in range(1, level.sq_h - 1)]
        self.assertEqual(set(e8_sides), {"║║"})
        self.assertNotIn("║", "".join(h5_sides))
        self.assertIn("═", e8_top)

    def test_no_colour_black_pieces_keep_their_inner_lines(self) -> None:
        cell_pixels = {" ": (False, False), "▀": (True, False), "▄": (False, True), "█": (True, True)}
        for scale in sprite_levels():
            size = BOARD_LEVELS[scale - 1].sprite
            if size < ui.MONO_MIN_SPRITE:
                continue
            for piece in "kqrbnp":
                rows = ui._mono_square(piece, True, "", scale)
                ink = [[False] * size for _ in range(size)]
                for row, text in enumerate(rows):
                    for col, char in enumerate(text):
                        ink[2 * row][col], ink[2 * row + 1][col] = cell_pixels[char]
                grid = sprites.pixels(size, piece)
                label = "%s%d" % (piece, size)
                inside = [(r, c) for r in range(size) for c in range(size)
                          if grid[r][c] != sprites.EMPTY and not ui._on_silhouette_edge(grid, r, c)]
                self.assertTrue(any(not ink[r][c] for r, c in inside), label + ": no blank line inside")
                for r in range(size):
                    for c in range(size):
                        sides = [grid[rr][cc] if 0 <= rr < size and 0 <= cc < size else sprites.EMPTY
                                 for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1))]
                        if grid[r][c] != sprites.EMPTY and sprites.EMPTY in sides:
                            self.assertTrue(ink[r][c], label + ": the silhouette is drawn whole")
                        if grid[r][c] == sprites.EMPTY:
                            self.assertFalse(ink[r][c], label)
        # The 14 px king's cross is cut off from its body by blank pixels (below the cross bar).
        king = sprites.pixels(14, "k")
        self.assertEqual([king[4][c] for c in range(3, 6)], [sprites.DETAIL] * 3)

    def test_ascii_themes_keep_to_letter_boards(self) -> None:
        for theme in (PLAIN, COLOR_ASCII):
            for scale in range(1, len(BOARD_LEVELS) + 1):
                text = "\n".join(render_board(Board(), "b", theme=theme, scale=scale))
                self.assertTrue(text.isascii(), (theme, scale))


class BoardLayoutTest(unittest.TestCase):
    def check_frame(self, view: ViewState, width: int, height: int, theme: Theme) -> str:
        frame, (row, col) = render_screen(view, width, height, theme)
        lines = frame.split("\n")
        label = "%dx%d %s %s" % (width, height, theme, view.board_size)
        self.assertEqual(len(lines), height, label)
        for line in lines:
            self.assertLessEqual(visible_len(line), width, label + " " + repr(ui.strip_ansi(line)))
        self.assertEqual(row, height - 1, label)
        self.assertTrue(0 <= col < width, label)
        if not theme.color:
            self.assertNotIn("\x1b", frame, label)
        if not theme.unicode:
            self.assertTrue(frame.isascii(), label)
        return frame

    def test_every_size_fits_every_terminal(self) -> None:
        for (width, height), size, theme in itertools.product(SIZES, BOARD_SIZES, THEMES):
            for view in (busy_view(board_size=size), busy_view(board_size=size, perspective="b", my_color="b"),
                         ViewState(board_size=size), busy_view(board_size=size, pending="", log=[])):
                self.check_frame(view, width, height, theme)

    def test_odd_sizes_fit(self) -> None:
        for width, height in ((1, 1), (5, 3), (66, 40), (67, 43), (70, 36), (99, 36), (101, 36), (132, 52),
                              (148, 60), (300, 100)):
            for theme in (COLOR, MONO):
                self.check_frame(busy_view(), width, height, theme)

    def test_auto_grows_with_the_terminal(self) -> None:
        tiers = [board_tier(width, height, COLOR) for width, height in SIZES]
        self.assertEqual(tiers, ["small", "medium", "medium", "large", "large", "xl", "xl"])
        levels = [board_layout(width, height, COLOR)[0] for width, height in SIZES]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(levels[-1], len(BOARD_LEVELS))
        self.assertIn("▀", render_screen(busy_view(), 120, 40, COLOR)[0])
        self.assertNotIn("▀", render_screen(busy_view(), 80, 24, COLOR)[0])

    def test_auto_is_monotonic(self) -> None:
        for theme in (COLOR, MONO, COLOR_ASCII):
            grid = {(w, h): board_layout(w, h, theme)[0] for w in range(30, 270, 7) for h in range(10, 80, 3)}
            for (w, h), level in grid.items():
                if (w + 7, h) in grid:
                    self.assertLessEqual(level, grid[w + 7, h], (theme, w, h))
                if (w, h + 3) in grid:
                    self.assertLessEqual(level, grid[w, h + 3], (theme, w, h))

    def test_chosen_board_fits(self) -> None:
        for w, h in itertools.product(range(20, 270, 9), range(8, 80, 4)):
            for theme in (COLOR, MONO):
                scale, layout = board_layout(w, h, theme)
                if scale == 1:
                    continue
                board_w, board_h = ui._board_dims(scale)
                self.assertLessEqual(board_w, w)
                self.assertLessEqual(board_h + ui.FOOTER_ROWS, h)
                if layout == "wide":
                    self.assertGreaterEqual(w, ui.WIDE_MIN_WIDTH)

    def test_forced_sizes_cap_and_degrade(self) -> None:
        for width, height in SIZES:
            self.assertEqual(board_tier(width, height, COLOR, "small"), "small")
            self.assertIn(board_tier(width, height, COLOR, "medium"), ("small", "medium"))
            self.assertEqual(board_layout(width, height, COLOR, "xl"), board_layout(width, height, COLOR))
        self.assertEqual(board_tier(250, 70, COLOR, "medium"), "medium")
        self.assertEqual(board_tier(250, 70, COLOR, "large"), "large")
        self.assertEqual(board_tier(80, 24, COLOR, "xl"), "medium")
        self.assertEqual(board_tier(120, 40, COLOR, "xl"), "large")
        self.assertEqual(board_layout(250, 70, COLOR, "nonsense"), board_layout(250, 70, COLOR))

    def test_themes_limit_the_levels(self) -> None:
        for width, height in SIZES:
            self.assertEqual(board_layout(width, height, PLAIN)[0], 1)
            self.assertLessEqual(board_layout(width, height, COLOR_ASCII)[0], 3)
            self.assertIn(board_layout(width, height, MONO)[0], (1, 7, 8))
        self.assertEqual(board_tier(200, 60, MONO), "xl")

    def test_size_follows_the_view(self) -> None:
        frames = {size: render_screen(busy_view(board_size=size), 200, 60, COLOR)[0] for size in BOARD_SIZES}
        self.assertEqual(frames["auto"], frames["xl"])
        self.assertNotIn("▀", frames["small"])
        self.assertNotIn("▀", frames["medium"])
        self.assertIn("▀", frames["large"])
        self.assertNotEqual(frames["large"], frames["xl"])

    def test_big_boards_keep_the_essentials_visible(self) -> None:
        for width, height in ((120, 40), (160, 50), (200, 60), (250, 70), (100, 50)):
            frame = ui.strip_ansi(render_screen(busy_view(), width, height, COLOR)[0])
            for text in ("Alice the Magnificent", "Bob", "15.3", "3:03", "Qf3+", "THE-END", "Waiting for Bob",
                         "Draw offered by opponent", "> Nf3"):
                self.assertIn(text, frame, (width, height, text))

    def test_log_moves_into_the_panel_beside_big_boards(self) -> None:
        lines = ui.strip_ansi(render_screen(busy_view(pending=""), 200, 60, COLOR)[0]).split("\n")
        board_w = ui._board_dims(board_layout(200, 60, COLOR)[0])[0]
        chat = next(i for i, line in enumerate(lines) if "THE-END" in line)
        self.assertGreater(lines[chat].index("THE-END"), board_w)
        self.assertLess(chat, 57)

    def test_normalize_and_cycle(self) -> None:
        self.assertEqual(ui.normalize_board_size(" XL "), "xl")
        for bad in ("huge", "", None, 3, "auto-ish"):
            self.assertEqual(ui.normalize_board_size(bad), "auto")
        order = ["auto"]
        for _ in BOARD_SIZES:
            order.append(ui.next_board_size(order[-1]))
        self.assertEqual(order, ["auto", "small", "medium", "large", "xl", "auto"])
        self.assertEqual(ui.next_board_size("bogus"), "small")


LONG_GAME = ("e4 e5 Nf3 Nc6 Bb5 a6 Ba4 Nf6 O-O Be7 Re1 b5 Bb3 d6 c3 O-O h3 Na5 Bc2 c5 d4 Qc7 Nbd2 cxd4 cxd4 Nc6 "
             "Nb3 a5 Be3 a4 Nbd2 Bd7 Rc1 Qb7 Qe2 Rfe8 Bd3 Bd8 Nf1 Bb6 Ng3 h6 a3 Rac8 Bb1 Ne7 Qd2 Ng6 dxe5 dxe5 "
             "Bxb6 Qxb6 Rxc8 Rxc8 Rd1 Be6 Qe3 Qxe3 fxe3 Rc4 Nf5 Bxf5 exf5 Ne7 Bd3 Rc7").split()  # 33 moves


def unique_log(entries: int) -> List[Tuple[str, str]]:
    """Log entries of about 95 characters (two or three lines beside a board) made of unique words."""
    return [("info", " ".join("w%03d_%02d" % (i, k) for k in range(11))) for i in range(entries)]


class PanelLogTest(unittest.TestCase):
    """Beside a drawn board the moves and the log share the side panel."""

    def view(self, moves: Sequence[str], log: List[Tuple[str, str]]) -> ViewState:
        board = Board()
        for san in moves:
            board.push_san(san)
        return ViewState(board=board, my_color="w", white_name="Alice", black_name="Bob", log=log)

    def move_numbers(self, view: ViewState, width: int, height: int) -> List[int]:
        text = ui.strip_ansi(render_screen(view, width, height, COLOR)[0])
        return sorted({int(n) for n in re.findall(r"(?<![\d:])(\d{1,2})\. ", text)})

    def test_moves_use_the_rows_a_short_log_leaves(self) -> None:
        log = [("info", "Welcome"), ("chat_them", "Bob: hi"), ("chat_me", "Alice: hi")]
        view = self.view(LONG_GAME, log)
        self.assertTrue(BOARD_LEVELS[board_layout(120, 40, COLOR)[0] - 1].sprite)
        numbers = self.move_numbers(view, 120, 40)
        self.assertGreaterEqual(len(numbers), 20, numbers)
        self.assertEqual(numbers[-1], 33)
        self.assertEqual(ui.log_geometry(view, 120, 40, COLOR)[1], ui.MIN_LOG_ROWS)
        frame = ui.strip_ansi(render_screen(view, 120, 40, COLOR)[0])
        for text in ("Welcome", "Bob: hi", "Alice: hi"):
            self.assertIn(text, frame)
        self.assertEqual(self.move_numbers(view, 160, 50), list(range(4, 34)))
        for width, height in ((200, 60), (250, 70)):
            self.assertEqual(self.move_numbers(view, width, height), list(range(1, 34)), (width, height))

    def test_a_long_log_and_a_long_game_share_the_panel(self) -> None:
        view = self.view(LONG_GAME, unique_log(80))
        columns, rows = ui.log_geometry(view, 120, 40, COLOR)
        moves = self.move_numbers(view, 120, 40)
        self.assertGreaterEqual(rows, 10)
        self.assertGreaterEqual(len(moves), 10)
        self.assertLessEqual(abs(rows - len(moves)), 2, (rows, moves))
        self.assertLess(columns, 120 - 67, "the log is in the side panel")
        # Before the first move the log may take nearly the whole panel.
        opening = self.view([], unique_log(80))
        self.assertGreater(ui.log_geometry(opening, 120, 40, COLOR)[1], 2 * rows - 6)

    def test_paging_reaches_every_log_line(self) -> None:
        for width, height in ((120, 40), (200, 60), (160, 50), (100, 30), (80, 24)):
            session = game.GameSession(mode="local", autosave=False)
            session.set_screen((width, height), COLOR)
            session.log = unique_log(60)
            words = {word for _kind, text in session.log for word in text.split()}
            seen = set()
            limit, page = ui.log_scroll_limit(session.view_state(), width, height, COLOR)
            columns, rows = ui.log_geometry(session.view_state(), width, height, COLOR)
            self.assertEqual(page, max(1, rows - 2))
            for _ in range(limit // page + 3):
                seen.update(re.findall(r"w\d{3}_\d{2}", ui.strip_ansi(render_screen(session.view_state(), width,
                                                                                     height, COLOR)[0])))
                game._editor_event(session, ("scroll", -1), None, (width, height))
            self.assertEqual(session.log_scroll, limit, (width, height))
            self.assertEqual(words - seen, set(), (width, height, "every line is shown on some page"))
            # At the limit the oldest line is in sight; one line less and it is not (no dead PgUp).
            frame = lambda: ui.strip_ansi(render_screen(session.view_state(), width, height, COLOR)[0])  # noqa: E731
            self.assertIn("w000_00", frame())
            session.log_scroll = limit - 1
            self.assertNotIn("w000_00", frame())
            for _ in range(limit // page + 2):
                game._editor_event(session, ("scroll", 1), None, (width, height))
            self.assertEqual(session.log_scroll, 0)

    def test_log_line_count_matches_the_wrapping(self) -> None:
        log = unique_log(5) + [("error", "two\nlines"), ("info", "")]
        view = ViewState(log=log)
        for width in (20, 33, 51, 80):
            lines = ui._log_lines(view, width, 1000, COLOR)
            self.assertEqual(ui.log_line_count(log, width), len(lines), width)
            self.assertEqual(ui.log_line_count(log, width, cap=4), 4)


class SizeHintTest(unittest.TestCase):
    """/size and the start of a game say what window a board size (or drawn pieces) needs."""

    def session(self, size: Tuple[int, int], theme: Theme = COLOR, **kwargs: Any) -> game.GameSession:
        session = game.GameSession(mode="local", autosave=False, unicode=theme.unicode, **kwargs)
        session.set_screen(size, theme)
        return session

    def test_min_window_matches_the_layout(self) -> None:
        for theme in (COLOR, MONO, COLOR_ASCII, PLAIN):
            drawable = {BOARD_LEVELS[scale - 1].tier for scale in ui._allowed_levels(theme)}
            for tier in ("medium", "large", "xl"):
                needed = ui.min_window(tier, theme)
                if needed is None:
                    self.assertNotIn(tier, drawable, (theme, tier))
                    continue
                width, height = needed
                self.assertEqual(board_tier(width, height, theme, tier), tier, (theme, tier))
                self.assertNotEqual(board_tier(width - 1, height, theme, tier), tier, (theme, tier))
                self.assertNotEqual(board_tier(width, height - 1, theme, tier), tier, (theme, tier))
        self.assertEqual([ui.min_window(tier) for tier in ("small", "medium", "large", "xl")],
                         [(1, 1), (77, 24), (99, 36), (131, 52)])  # as in the README
        self.assertEqual(ui.drawn_pieces_window(COLOR), (99, 36))
        self.assertEqual(ui.drawn_pieces_window(MONO), (147, 60))
        self.assertIsNone(ui.drawn_pieces_window(COLOR_ASCII))

    def test_size_says_what_window_it_needs(self) -> None:
        session = self.session((90, 30))
        session.handle_input("/size large")
        text = session.log[-1][1]
        self.assertEqual(text, "Board size: large, but it needs a window of at least 99x36 (this one is 90x30): "
                               "medium is shown until you make the window bigger or the font smaller.")
        for part in ("99x36", "90x30", "medium is shown", "bigger or the font smaller"):
            self.assertIn(part, text)
        session.set_screen((120, 40), COLOR)
        session.handle_input("/size xl")
        self.assertIn("131x52", session.log[-1][1])
        self.assertIn("large is shown until", session.log[-1][1])
        session.set_screen((200, 60), COLOR)
        session.handle_input("/size large")
        self.assertEqual(session.log[-1][1], "Board size: large, with drawn pieces.")
        session.handle_input("/size auto")
        self.assertEqual(session.log[-1][1], "Board size: auto (the biggest board that fits the window).")
        session.set_screen((80, 24), COLOR)
        session.handle_input("/size auto")
        self.assertIn("Drawn pieces need a window of at least 99x36 (this one is 80x24)", session.log[-1][1])
        session.handle_input("/size medium")
        self.assertEqual(session.log[-1][1], "Board size: medium.")

    def test_without_colours_medium_and_large_say_the_small_board_is_shown(self) -> None:
        session = self.session((250, 70), MONO)
        for size in ("medium", "large"):
            session.handle_input("/size " + size)
            text = session.log[-1][1]
            self.assertIn("Without colours", text, size)
            self.assertIn("small board is shown", text, size)
            self.assertIn("147x60", text, size)
            self.assertNotIn("drawn pieces", text, size)
            self.assertEqual(board_tier(250, 70, MONO, size), "small")
        session.handle_input("/size xl")
        self.assertEqual(session.log[-1][1], "Board size: xl, with drawn pieces.")

    def test_one_hint_when_the_window_is_too_small_for_drawn_pieces(self) -> None:
        session = game.GameSession(mode="local", autosave=False)
        before = len(session.log)
        for size in ((80, 24), (80, 24), (70, 20), (120, 40), (80, 24)):
            session.set_screen(size, COLOR)
        self.assertEqual(session.screen_size, (80, 24))
        new = [text for _kind, text in session.log[before:]]
        self.assertEqual(new, ["Drawn pieces need a window of at least 99x36 (this one is 80x24): "
                               "make the window bigger or the font smaller."])
        for size, theme, kwargs in (((120, 40), COLOR, {}), ((80, 24), COLOR, {"board_size": "medium"}),
                                    ((80, 24), COLOR_ASCII, {}), ((200, 60), MONO, {})):
            other = game.GameSession(mode="local", autosave=False, unicode=theme.unicode, **kwargs)
            count = len(other.log)
            other.set_screen(size, theme)
            self.assertEqual(other.log[count:], [], (size, theme, kwargs))
        mono = self.session((120, 40), MONO)
        self.assertIn("147x60", mono.log[-1][1])


class SizeCommandTest(unittest.TestCase):
    class Conn:
        peer = "10.0.0.2:5555"
        closed = False

        def __init__(self) -> None:
            self.sent: List[Dict[str, Any]] = []

        def send(self, msg: Dict[str, Any]) -> None:
            self.sent.append(msg)

        def close(self) -> None:
            self.closed = True

    def sessions(self) -> List[game.GameSession]:
        local = game.GameSession(mode="local", autosave=False)
        network = game.GameSession(mode="network", my_color="b", my_name="Bob", opponent_name="Alice",
                                   conn=self.Conn(), autosave=False)
        return [local, network]

    def test_cycles_without_argument(self) -> None:
        for session in self.sessions():
            self.assertEqual(session.board_size, "auto")
            seen = []
            for _ in BOARD_SIZES:
                session.dirty = False
                session.handle_input("/size")
                self.assertTrue(session.dirty)
                seen.append(session.view_state().board_size)
            self.assertEqual(seen, ["small", "medium", "large", "xl", "auto"])
            if session.conn is not None:
                self.assertEqual(session.conn.sent, [], "/size is local to this screen")

    def test_names_and_prefixes(self) -> None:
        session = self.sessions()[0]
        for typed, expected in (("/size large", "large"), ("/size S", "small"), ("/size x", "xl"),
                                ("/SIZE medium", "medium"), ("/size  auto ", "auto"), ("/size l", "large")):
            session.handle_input(typed)
            self.assertEqual(session.board_size, expected, typed)
        self.assertIn("Board size: large", session.log[-1][1])

    def test_bad_size_is_an_error(self) -> None:
        session = self.sessions()[0]
        session.handle_input("/size m")
        session.handle_input("/size huge")
        self.assertEqual(session.board_size, "medium")
        self.assertEqual(session.log[-1][0], "error")
        self.assertIn("Unknown board size: huge", session.log[-1][1])

    def test_constructor_and_ascii_note(self) -> None:
        self.assertEqual(game.GameSession(mode="local", autosave=False, board_size="LARGE").board_size, "large")
        self.assertEqual(game.GameSession(mode="local", autosave=False, board_size="huge").board_size, "auto")
        ascii_session = game.GameSession(mode="local", autosave=False, unicode=False)
        ascii_session.handle_input("/size xl")
        self.assertIn("letter board", ascii_session.log[-1][1])

    def test_help_lists_size(self) -> None:
        self.assertTrue(any(line.startswith("/size") for line in ui.help_lines()))
        session = self.sessions()[0]
        session.handle_input("/help")
        self.assertTrue(any(text.startswith("/size") for _kind, text in session.log))

    def test_frame_changes_at_once(self) -> None:
        session = self.sessions()[0]
        frame = lambda: render_screen(session.view_state(), 200, 60, COLOR)[0]  # noqa: E731
        self.assertIn("▀", frame())
        session.handle_input("/size small")
        self.assertNotIn("▀", frame())
        session.handle_input("/size large")
        self.assertIn("▀", frame())


class LiveResizeTest(unittest.TestCase):
    class Reader:
        """Fake KeyReader: plays a script of keys; callables in it run (e.g. resize) and read as a pause."""

        def __init__(self, script: Sequence[Any]) -> None:
            self.script = list(script)
            self.eof = False

        def start(self) -> None:
            pass

        def restore(self) -> None:
            pass

        def read_key(self, timeout: float) -> Optional[str]:
            if not self.script:
                return term.CTRL_C
            item = self.script.pop(0)
            if callable(item):
                item()
                return None
            return item

    class Screen:
        def __init__(self) -> None:
            self.frames: List[Tuple[str, Tuple[int, int]]] = []

        def enter(self) -> None:
            pass

        def exit(self) -> None:
            pass

        def draw(self, frame: str, cursor: Any, size: Optional[Tuple[int, int]] = None) -> None:
            assert size is not None
            self.frames.append((frame, size))

    def test_resizing_changes_the_piece_size(self) -> None:
        size = [(80, 24)]
        resize: Callable[[int, int], Callable[[], None]] = lambda w, h: lambda: size.__setitem__(0, (w, h))  # noqa: E731
        script: List[Any] = [None, resize(120, 40), None, None, resize(200, 60), None, None]
        script += list("/size small") + [term.ENTER, None, None, resize(100, 30), None, None]
        session = game.GameSession(mode="local", autosave=False)
        screen = self.Screen()
        game.run_interactive(session, None, COLOR, key_reader=self.Reader(script), screen=screen,
                             size_fn=lambda: size[0], poll_interval=0.001)
        by_size: Dict[Tuple[int, int], List[str]] = {}
        for frame, frame_size in screen.frames:
            self.assertEqual(len(frame.split("\n")), frame_size[1])
            by_size.setdefault(frame_size, []).append(frame)
        self.assertEqual(set(by_size), {(80, 24), (120, 40), (200, 60), (100, 30)})
        self.assertNotIn("▀", by_size[(80, 24)][-1])
        self.assertIn("▀", by_size[(120, 40)][-1])
        self.assertIn("▀", by_size[(200, 60)][0])
        self.assertNotIn("▀", by_size[(200, 60)][-1], "/size small applies at once")
        self.assertEqual(session.board_size, "small")
        hints = [text for _kind, text in session.log if text.startswith("Drawn pieces need")]
        self.assertEqual(hints, ["Drawn pieces need a window of at least 99x36 (this one is 80x24): "
                                 "make the window bigger or the font smaller."], "said once, at the start")
        self.assertIn("Drawn pieces need", ui.strip_ansi(by_size[(80, 24)][-1]))
        self.assertEqual(session.screen_size, (100, 30))


class BoardSizeSettingTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanchess-test-")
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.json")

    def test_round_trip_and_validation(self) -> None:
        self.assertEqual(config.Settings().board_size, "auto")
        for size in BOARD_SIZES:
            config.save(config.Settings(board_size=size), self.path)
            self.assertEqual(config.load(self.path).board_size, size)
        for value, expected in (("Large", "large"), (" xl ", "xl"), ("huge", "auto"), (2, "auto"), (None, "auto")):
            self.assertEqual(config.Settings.from_dict({"board_size": value}).board_size, expected, value)
        config.save(config.Settings(board_size="bogus"), self.path)
        self.assertEqual(config.load(self.path).board_size, "auto")
        self.assertIn("board_size", config.Settings().to_dict())

    def make_ctx(self, settings: Optional[config.Settings] = None) -> menu.MenuContext:
        return menu.MenuContext(settings or config.load(self.path), {}, self.path, theme=MONO,
                                size_fn=lambda: (100, 30), threaded=False, local_ips=lambda: [],
                                firewall=lambda: None)

    def test_settings_screen_cycles_and_saves(self) -> None:
        ctx = self.make_ctx()
        screen = menu.SettingsScreen(ctx)
        self.assertEqual(screen.board.value, "auto")
        screen.focus(screen.board)
        seen = []
        for _ in BOARD_SIZES:
            screen.handle_key(term.RIGHT)
            seen.append(screen.board.value)
        self.assertEqual(seen, ["small", "medium", "large", "xl", "auto"])
        screen.handle_key(term.LEFT)
        self.assertEqual(screen.board.value, "xl")
        frame = ui.strip_ansi(menu.compose(screen, ctx.style, 100, 30)[0])
        self.assertIn("Board size", frame)
        self.assertIn("Extra large", frame)
        self.assertEqual(screen.save().kind, "pop")
        self.assertEqual(config.load(self.path).board_size, "xl")
        self.assertEqual(menu.SettingsScreen(self.make_ctx()).board.value, "xl")

    def test_games_from_the_menu_use_the_saved_size(self) -> None:
        ctx = self.make_ctx(config.Settings(board_size="medium", autosave=False))
        session = ctx.make_session(menu.GameRequest(mode="local"))
        self.assertEqual(session.board_size, "medium")
        self.assertEqual(session.view_state().board_size, "medium")


if __name__ == "__main__":
    unittest.main()

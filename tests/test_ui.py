"""Tests for lanchess.ui using a small stand-in board (no dependency on the engine)."""

from __future__ import annotations

import itertools
import re
import unittest
from typing import Dict, List, NamedTuple, Optional, Sequence

from lanchess import ui
from lanchess.ui import Theme, ViewState, format_clock, pad, render_board, render_plain_board, render_screen
from lanchess.ui import truncate, visible_len

START_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
START_FEN = START_PLACEMENT + " w KQkq - 0 1"
CORNERS = "r6k/8/8/8/8/8/8/K6R"

PLAIN = Theme(unicode=False, color=False)
PLAIN_UNICODE = Theme(unicode=True, color=False)
COLOR = Theme(unicode=True, color=True)
COLOR_ASCII = Theme(unicode=False, color=True)
ALL_THEMES = (PLAIN, PLAIN_UNICODE, COLOR, COLOR_ASCII)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def sq(name: str) -> int:
    return "abcdefgh".index(name[0]) + 8 * (int(name[1]) - 1)


class FakeMove(NamedTuple):
    from_sq: int
    to_sq: int
    promotion: Optional[str] = None


class FakeBoard:
    """Implements only the Board members ui.py relies on."""

    def __init__(
        self,
        placement: str = START_PLACEMENT,
        turn: str = "w",
        sans: Sequence[str] = (),
        root_fen: str = START_FEN,
        last: Optional[FakeMove] = None,
        check: Optional[int] = None,
        captured: Optional[Dict[str, List[str]]] = None,
        balance: int = 0,
    ) -> None:
        self.squares: Dict[int, str] = {}
        for row, rank_text in enumerate(placement.split("/")):
            file = 0
            for char in rank_text:
                if char.isdigit():
                    file += int(char)
                else:
                    self.squares[(7 - row) * 8 + file] = char
                    file += 1
        self.turn = turn
        self.san_stack = list(sans)
        self.root_fen = root_fen
        self.fullmove_number = int(root_fen.split()[5]) + len(self.san_stack) // 2
        self._last = last
        self._check = check
        self._captured = captured or {"w": [], "b": []}
        self._balance = balance

    def piece_at(self, square: int) -> Optional[str]:
        return self.squares.get(square)

    def check_square(self) -> Optional[int]:
        return self._check

    def captured_pieces(self, color: str) -> List[str]:
        return list(self._captured[color])

    def material_balance(self) -> int:
        return self._balance

    def last_move(self) -> Optional[FakeMove]:
        return self._last


def plain(text: str) -> str:
    return ANSI.sub("", text)


def squares_of(line: str) -> List[str]:
    """Piece characters of a 3-column-per-square board line (rank label stripped)."""
    body = plain(line)[3:]
    return [body[i + 1] for i in range(0, 24, 3)]


def busy_view(**overrides: object) -> ViewState:
    sans = ["Q%03d" % i for i in range(200)]
    board = FakeBoard(
        sans=sans,
        turn="b",
        last=FakeMove(sq("e2"), sq("e4")),
        check=sq("e8"),
        captured={"w": ["Q", "N", "P", "P"], "b": ["r", "b", "p"]},
        balance=-1,
    )
    log = [("info", "line %03d" % i) for i in range(300)]
    log.append(("chat_them", "Bob: " + "a very long chat message " * 20 + "THE-END"))
    view = ViewState(
        board=board,
        my_color="w",
        white_name="Alice the Magnificent",
        black_name="Bob",
        clock_ms={"w": 183400, "b": 15300},
        clock_running="b",
        log=log,
        status="Waiting for Bob",
        connection="Connected to 192.168.100.200:5555",
        pending="Draw offered by opponent - /accept or /decline",
        input_buffer="Nf3",
        input_cursor=3,
    )
    for key, value in overrides.items():
        setattr(view, key, value)
    return view


class TextHelpersTest(unittest.TestCase):
    def test_visible_len(self) -> None:
        self.assertEqual(visible_len(""), 0)
        self.assertEqual(visible_len("abc"), 3)
        self.assertEqual(visible_len("\x1b[1;38;5;196mab\x1b[0mc"), 3)
        self.assertEqual(visible_len("♚♛ é"), 4)
        self.assertEqual(visible_len("日本"), 4)
        self.assertEqual(visible_len("e\u0301"), 1)

    def test_truncate_plain(self) -> None:
        self.assertEqual(truncate("hello", 10), "hello")
        self.assertEqual(truncate("hello", 5), "hello")
        self.assertEqual(truncate("hello", 3), "hel")
        self.assertEqual(truncate("hello", 0), "")
        self.assertEqual(truncate("hello", -2), "")
        self.assertEqual(truncate("hello world", 8, "..."), "hello...")
        self.assertEqual(truncate("hello", 2, "..."), "he")

    def test_truncate_keeps_escapes_and_resets(self) -> None:
        styled = "\x1b[1mbold\x1b[0m plain"
        self.assertEqual(truncate(styled, 20), styled)
        cut = truncate(styled, 2)
        self.assertEqual(cut, "\x1b[1mbo" + ui.RESET)
        self.assertEqual(visible_len(cut), 2)
        cut = truncate("\x1b[31mabcdef\x1b[0m", 4, "…")
        self.assertEqual(plain(cut), "abc…")
        self.assertTrue(cut.endswith(ui.RESET))

    def test_truncate_wide_characters(self) -> None:
        self.assertEqual(truncate("日本語", 3), "日")
        self.assertEqual(truncate("日本語", 4), "日本")

    def test_pad(self) -> None:
        self.assertEqual(pad("ab", 5), "ab   ")
        self.assertEqual(pad("ab", 5, "right"), "   ab")
        self.assertEqual(pad("ab", 5, "center"), " ab  ")
        self.assertEqual(pad("abcdef", 3), "abc")
        styled = pad("\x1b[1mab\x1b[0m", 4)
        self.assertEqual(visible_len(styled), 4)
        self.assertTrue(styled.startswith("\x1b[1mab"))

    def test_paint_without_color_is_identity(self) -> None:
        self.assertEqual(PLAIN.paint("x", fg=1, bg=2, bold=True), "x")
        self.assertEqual(COLOR.paint("x"), "x")
        self.assertEqual(COLOR.paint("x", fg=196, bold=True), "\x1b[1;38;5;196mx\x1b[0m")


class FormatClockTest(unittest.TestCase):
    def test_edge_cases(self) -> None:
        cases = {
            0: "0:00",
            -1: "0:00",
            -5000: "0:00",
            99: "0.0",
            100: "0.1",
            5000: "5.0",
            19950: "19.9",
            19999: "19.9",
            20000: "0:20",
            59999: "0:59",
            60000: "1:00",
            300000: "5:00",
            3599000: "59:59",
            3599999: "59:59",
            3600000: "1:00:00",
            3723000: "1:02:03",
            36000000: "10:00:00",
        }
        for ms, text in cases.items():
            self.assertEqual(format_clock(ms), text, ms)

    def test_float_input(self) -> None:
        self.assertEqual(format_clock(1599.9), "1.5")
        self.assertEqual(format_clock(61000.0), "1:01")


class RenderBoardTest(unittest.TestCase):
    def test_white_orientation(self) -> None:
        lines = render_board(FakeBoard(CORNERS), "w", theme=PLAIN)
        self.assertEqual(len(lines), 9)
        self.assertEqual(lines[0], " 8  r  .  .  .  .  .  .  k ")
        self.assertTrue(lines[7].startswith(" 1  K"))
        self.assertEqual(squares_of(lines[7])[0], "K")
        self.assertEqual(lines[8].split(), list("abcdefgh"))

    def test_black_orientation(self) -> None:
        lines = render_board(FakeBoard(CORNERS), "b", theme=PLAIN)
        self.assertTrue(lines[0].startswith(" 1 "))
        self.assertEqual(squares_of(lines[0]), ["R", ".", ".", ".", ".", ".", ".", "K"])
        self.assertTrue(lines[7].startswith(" 8 "))
        self.assertEqual(squares_of(lines[7])[0], "k")
        self.assertEqual(squares_of(lines[7])[-1], "r")
        self.assertEqual(lines[8].split(), list("hgfedcba"))

    def test_start_position_pieces(self) -> None:
        lines = render_board(FakeBoard(), "w", theme=PLAIN)
        self.assertEqual("".join(squares_of(lines[0])), "rnbqkbnr")
        self.assertEqual("".join(squares_of(lines[1])), "pppppppp")
        self.assertEqual("".join(squares_of(lines[6])), "PPPPPPPP")
        self.assertEqual("".join(squares_of(lines[7])), "RNBQKBNR")

    def test_unicode_without_color_uses_outline_for_white(self) -> None:
        lines = render_board(FakeBoard(), "w", theme=PLAIN_UNICODE)
        self.assertEqual("".join(squares_of(lines[0])), "♜♞♝♛♚♝♞♜")
        self.assertEqual("".join(squares_of(lines[7])), "♖♘♗♕♔♗♘♖")
        self.assertEqual(squares_of(lines[4]), ["."] * 8)

    def test_last_move_marked_without_color(self) -> None:
        board = FakeBoard("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR", last=FakeMove(sq("e2"), sq("e4")))
        lines = render_board(board, "w", theme=PLAIN)
        self.assertEqual(lines[6], " 2  P  P  P  P [.] P  P  P ")
        self.assertEqual(lines[4], " 4  .  .  .  . [P] .  .  . ")
        explicit = render_board(board, "w", FakeMove(sq("a7"), sq("a6")), PLAIN)
        self.assertIn("[p]", explicit[1])
        self.assertNotIn("[", explicit[6])

    def test_equal_line_widths(self) -> None:
        for theme in ALL_THEMES:
            for perspective in "wb":
                lines = render_board(FakeBoard(), perspective, theme=theme)
                self.assertEqual({visible_len(line) for line in lines}, {27})

    def test_no_escape_codes_without_color(self) -> None:
        board = FakeBoard(last=FakeMove(0, 8), check=sq("e1"))
        for theme in (PLAIN, PLAIN_UNICODE):
            self.assertNotIn("\x1b", "".join(render_board(board, "w", theme=theme, scale=3)))

    def test_color_uses_palette(self) -> None:
        board = FakeBoard(last=FakeMove(sq("g1"), sq("f3")), check=sq("e1"))
        text = "\n".join(render_board(board, "w", theme=COLOR))
        self.assertIn("48;5;%d" % ui.LIGHT_SQUARE, text)
        self.assertIn("48;5;%d" % ui.DARK_SQUARE, text)
        self.assertIn("48;5;%d" % ui.CHECK_SQUARE, text)
        self.assertIn("38;5;%d" % ui.WHITE_PIECE, text)
        self.assertIn("38;5;%d" % ui.BLACK_PIECE, text)
        self.assertIn("48;5;%d" % ui.LIGHT_HIGHLIGHT, text)
        self.assertIn("48;5;%d" % ui.DARK_HIGHLIGHT, text)
        for glyph in "♔♕♖♗♘♙":
            self.assertNotIn(glyph, text)
        self.assertIn("♚", text)

    def test_highlight_and_check_on_the_right_squares(self) -> None:
        board = FakeBoard(last=FakeMove(sq("e2"), sq("e4")), check=sq("e1"))
        lines = render_board(board, "w", theme=COLOR)

        def background(line: str, file: int) -> int:
            cells = re.findall(r"\x1b\[0;(?:1;38;5;\d+;)?48;5;(\d+)m", line)
            return int(cells[file])

        self.assertEqual(background(lines[6], 4), ui.LIGHT_HIGHLIGHT)
        self.assertEqual(background(lines[4], 4), ui.LIGHT_HIGHLIGHT)
        self.assertEqual(background(lines[7], 4), ui.CHECK_SQUARE)
        self.assertEqual(background(lines[7], 0), ui.DARK_SQUARE)
        self.assertEqual(background(lines[7], 7), ui.LIGHT_SQUARE)
        flipped = render_board(board, "b", theme=COLOR)
        self.assertEqual(background(flipped[0], 3), ui.CHECK_SQUARE)

    def test_ascii_color_letters(self) -> None:
        text = plain("\n".join(render_board(FakeBoard(), "w", theme=COLOR_ASCII)))
        self.assertIn("R  N  B  Q  K  B  N  R", text)
        self.assertEqual("".join(squares_of(text.splitlines()[0])), "RNBQKBNR")

    def test_scaled_boards(self) -> None:
        for scale, (rows, width) in {1: (9, 27), 2: (17, 43), 3: (25, 59)}.items():
            lines = render_board(FakeBoard(), "w", theme=COLOR, scale=scale)
            self.assertEqual(len(lines), rows)
            self.assertEqual({visible_len(line) for line in lines}, {width})
        self.assertEqual(len(render_board(FakeBoard(), "w", theme=PLAIN, scale=3)), 9)

    def test_none_board_renders_empty(self) -> None:
        lines = render_board(None, "w", theme=PLAIN)
        self.assertEqual(squares_of(lines[0]), ["."] * 8)


class PlainBoardAndHelpTest(unittest.TestCase):
    def test_plain_board_summary(self) -> None:
        board = FakeBoard(
            sans=["e4", "d5", "exd5", "Nf6"],
            captured={"w": [], "b": ["p"]},
            balance=1,
            last=FakeMove(sq("g8"), sq("f6")),
        )
        text = render_plain_board(board, "w", PLAIN)
        self.assertIn("White captured: p +1", text)
        self.assertNotIn("Black captured", text)
        self.assertIn("Last move: 2...Nf6", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("Last move: 1.e4", render_plain_board(FakeBoard(sans=["e4"]), "b", PLAIN))
        self.assertIn("♟", render_plain_board(board, "w", PLAIN_UNICODE))

    def test_help_lines(self) -> None:
        text = "\n".join(ui.help_lines())
        for command in ("/c", "/resign", "/draw", "/accept", "/decline", "/takeback", "/flip", "/moves",
                        "/fen", "/pgn", "/save", "/rematch", "/quit", "/clear", "/help"):
            self.assertIn(command, text)
        self.assertNotIn("\x1b", text)

    def test_move_pairs_respect_root_fen(self) -> None:
        board = FakeBoard(sans=["e5", "Nf3", "Nc6"], root_fen=START_PLACEMENT + " b KQkq - 0 7")
        self.assertEqual(ui._move_pairs(board), [(7, None, "e5"), (8, "Nf3", "Nc6")])
        self.assertEqual(ui._move_pairs(FakeBoard(sans=["e4"])), [(1, "e4", None)])


class RenderScreenGeometryTest(unittest.TestCase):
    def check_frame(self, view: ViewState, width: int, height: int, theme: Theme) -> List[str]:
        frame, (row, col) = render_screen(view, width, height, theme)
        lines = frame.split("\n")
        label = "%dx%d %s" % (width, height, theme)
        self.assertEqual(len(lines), height, label)
        for line in lines:
            self.assertLessEqual(visible_len(line), width, label + " " + repr(plain(line)))
        self.assertEqual(row, height - 1, label)
        self.assertTrue(0 <= col < width, label)
        if not theme.color:
            self.assertNotIn("\x1b", frame, label)
        return lines

    def test_sizes_and_themes(self) -> None:
        views = [busy_view(), busy_view(pending="", log=[], clock_ms=None), ViewState(), busy_view(perspective="b")]
        for width, height, theme, view in itertools.product((40, 60, 80, 120), (20, 30, 50), ALL_THEMES, views):
            self.check_frame(view, width, height, theme)

    def test_extreme_sizes(self) -> None:
        for width, height in [(1, 1), (2, 2), (5, 3), (10, 4), (27, 6), (69, 12), (70, 12), (200, 60), (300, 100)]:
            for theme in (PLAIN, COLOR):
                self.check_frame(busy_view(), width, height, theme)

    def test_never_empty_terminal_rows_beyond_height(self) -> None:
        frame, _ = render_screen(busy_view(), 80, 24, COLOR)
        self.assertEqual(frame.count("\n"), 23)


class RenderScreenContentTest(unittest.TestCase):
    def frame(self, view: ViewState, width: int = 80, height: int = 30, theme: Theme = PLAIN) -> List[str]:
        return render_screen(view, width, height, theme)[0].split("\n")

    def test_wide_layout_puts_panel_beside_board(self) -> None:
        lines = self.frame(busy_view(), 80, 24)
        top = next(i for i, line in enumerate(lines) if "Bob" in line)
        bottom = next(i for i, line in enumerate(lines) if "Alice" in line)
        self.assertEqual(top, 0)
        self.assertTrue(lines[0].startswith(" 8 "))
        self.assertTrue(lines[bottom].startswith(" 1 "))

    def test_wide_layout_flipped_for_black(self) -> None:
        lines = self.frame(busy_view(perspective="b", my_color="b"), 80, 24)
        self.assertIn("Alice", lines[0])
        self.assertTrue(lines[0].startswith(" 1 "))
        self.assertIn("Bob (you)", lines[7])

    def test_narrow_layout_stacks_panel_below_board(self) -> None:
        lines = self.frame(busy_view(), 60, 30)
        self.assertNotIn("Bob", lines[0])
        files_row = next(i for i, line in enumerate(lines) if line.split() == list("abcdefgh"))
        bob = next(i for i, line in enumerate(lines) if "Bob" in line)
        alice = next(i for i, line in enumerate(lines) if "Alice" in line)
        self.assertEqual((bob, alice), (files_row + 1, files_row + 2))

    def test_side_to_move_marker(self) -> None:
        text = "\n".join(self.frame(busy_view()))
        self.assertIn("* Bob", text)
        self.assertNotIn("* Alice", text)
        over = "\n".join(self.frame(busy_view(game_over="Black wins by resignation")))
        self.assertNotIn("* Bob", over)
        self.assertIn("Black wins by resignation", over)

    def test_clocks(self) -> None:
        text = "\n".join(self.frame(busy_view()))
        self.assertIn("[15.3]", text)
        self.assertIn(" 3:03 ", text)
        untimed = "\n".join(self.frame(busy_view(clock_ms=None)))
        self.assertNotIn("15.3", untimed)
        self.assertNotIn("3:03", untimed)

    def test_clock_colors(self) -> None:
        running_low = render_screen(busy_view(), 80, 30, COLOR)[0]
        self.assertIn("48;5;%dm 15.3 " % ui.LOW_TIME_BG, running_low)
        running = render_screen(busy_view(clock_running="w"), 80, 30, COLOR)[0]
        self.assertIn("48;5;%dm 3:03 " % ui.RUNNING_BG, running)
        self.assertIn("38;5;%dm 15.3 " % ui.LOW_TIME_FG, running)

    def test_captured_pieces_and_material(self) -> None:
        text = "\n".join(self.frame(busy_view()))
        self.assertIn("rbp", text)
        self.assertIn("QNPP +1", text)
        unicode_text = "\n".join(self.frame(busy_view(), theme=PLAIN_UNICODE))
        self.assertIn("♜♝♟", unicode_text)
        self.assertIn("♕♘♙♙ +1", unicode_text)

    def test_move_list_shows_most_recent_pairs(self) -> None:
        for width, height in [(80, 24), (120, 40), (60, 30), (40, 20)]:
            for theme in (PLAIN, COLOR):
                text = plain(render_screen(busy_view(), width, height, theme)[0])
                self.assertIn("Q199", text, (width, height, theme))
                self.assertNotIn("Q000", text, (width, height, theme))
        text = "\n".join(self.frame(busy_view(board=FakeBoard(sans=["Q000", "Q001", "Q002"])), 120, 40))
        self.assertIn("1. Q000    Q001", text)
        self.assertIn("2. Q002", text)

    def test_latest_move_highlighted(self) -> None:
        frame = render_screen(busy_view(), 80, 30, COLOR)[0]
        self.assertIn("48;5;%dmQ199" % ui.LIGHT_HIGHLIGHT, frame)

    def test_no_moves_placeholder(self) -> None:
        self.assertIn("No moves yet", "\n".join(self.frame(ViewState(board=FakeBoard()))))

    def test_log_shows_latest_lines_and_wraps(self) -> None:
        lines = self.frame(busy_view(), 80, 30)
        text = "\n".join(lines)
        self.assertIn("THE-END", text)
        self.assertNotIn("line 000", text)
        self.assertTrue(any(line.startswith("  ") and "chat message" in line for line in lines))

    def test_log_scroll(self) -> None:
        text = "\n".join(self.frame(busy_view(log_scroll=20), 80, 30))
        self.assertNotIn("THE-END", text)
        self.assertIn("newer lines below", text)
        self.assertIn("line 285", text)
        self.assertNotIn("line 299", text)
        clamped = "\n".join(self.frame(busy_view(log_scroll=10000), 80, 30))
        self.assertIn("line 000", clamped)
        negative = "\n".join(self.frame(busy_view(log_scroll=-20), 80, 30))
        self.assertEqual(negative, text)

    def test_log_colors(self) -> None:
        log = [("chat_me", "me"), ("chat_them", "them"), ("error", "oops"), ("system", "sys"), ("game", "gg")]
        frame = render_screen(busy_view(log=log), 80, 30, COLOR)[0]
        self.assertIn("38;5;%dmme" % ui.CHAT_ME_FG, frame)
        self.assertIn("38;5;%dmthem" % ui.CHAT_THEM_FG, frame)
        self.assertIn("38;5;%dmoops" % ui.ERROR_FG, frame)
        self.assertIn("38;5;%dmsys" % ui.DIM_FG, frame)
        self.assertIn("\x1b[1mgg", frame)

    def test_untrusted_text_is_sanitized(self) -> None:
        hostile = "evil\x1b[2J\x1b]0;title\x07\x07\rname\x00‮"
        view = busy_view(log=[("chat_them", hostile)], black_name=hostile, status=hostile, pending=hostile, input_buffer=hostile)
        frame = render_screen(view, 80, 30, PLAIN)[0]
        self.assertNotIn("\x1b", frame)
        self.assertNotIn("\x07", frame)
        self.assertNotIn("\r", frame)
        self.assertNotIn("\u202e", frame)
        colored = render_screen(view, 80, 30, COLOR)[0]
        self.assertNotIn("\x1b[2J", colored)
        self.assertNotIn("\x1b]0;", colored)
        self.assertIn("evilname", frame)

    def test_pending_and_status_lines(self) -> None:
        lines = self.frame(busy_view())
        self.assertIn("Draw offered by opponent", lines[-3])
        self.assertIn("Waiting for Bob", lines[-2])
        self.assertTrue(lines[-2].rstrip().endswith("Connected to 192.168.100.200:5555"))
        lines = self.frame(busy_view(pending=""))
        self.assertNotIn("Draw offered", "\n".join(lines))
        self.assertIn("Waiting for Bob", lines[-2])

    def test_status_bar_spans_width_in_color(self) -> None:
        for width in (40, 80, 120):
            lines = render_screen(busy_view(), width, 30, COLOR)[0].split("\n")
            self.assertEqual(visible_len(lines[-2]), width)

    def test_status_drops_connection_before_status(self) -> None:
        lines = self.frame(busy_view(status="A fairly long status message here"), 40, 20)
        self.assertIn("A fairly long status", lines[-2])

    def test_frame_without_board(self) -> None:
        lines = self.frame(ViewState(status="Waiting for opponent"))
        self.assertIn("Waiting for opponent", lines[-2])
        self.assertIn("White", "\n".join(lines))


class PromptTest(unittest.TestCase):
    def caret_char(self, view: ViewState, width: int, theme: Theme = PLAIN) -> str:
        frame, (row, col) = render_screen(view, width, 20, theme)
        line = plain(frame.split("\n")[row])
        self.assertLessEqual(visible_len(line), width)
        cells: List[str] = []
        for char in line:
            cells.append(char)
            if ui._char_width(char) == 2:
                cells.append("")
        return cells[col] if col < len(cells) else " "

    def test_cursor_matches_prompt_and_buffer(self) -> None:
        view = busy_view(input_buffer="hello", input_cursor=2)
        frame, cursor = render_screen(view, 80, 24, PLAIN)
        self.assertEqual(frame.split("\n")[-1], "> hello")
        self.assertEqual(cursor, (23, 4))
        _, cursor = render_screen(busy_view(input_buffer="hello", input_cursor=5, prompt="move> "), 80, 24, COLOR)
        self.assertEqual(cursor, (23, 11))
        _, cursor = render_screen(busy_view(input_buffer="", input_cursor=0), 80, 24, COLOR)
        self.assertEqual(cursor, (23, 2))

    def test_cursor_is_clamped_to_buffer(self) -> None:
        _, cursor = render_screen(busy_view(input_buffer="abc", input_cursor=99), 80, 24, PLAIN)
        self.assertEqual(cursor, (23, 5))
        _, cursor = render_screen(busy_view(input_buffer="abc", input_cursor=-4), 80, 24, PLAIN)
        self.assertEqual(cursor, (23, 2))

    def test_long_buffer_scrolls_horizontally(self) -> None:
        buffer = "".join(chr(ord("a") + i % 26) for i in range(300))
        for width in (20, 40, 80):
            for cursor in (0, 1, 37, 150, 298, 299, 300):
                view = busy_view(input_buffer=buffer, input_cursor=cursor)
                expected = buffer[cursor] if cursor < len(buffer) else " "
                for theme in (PLAIN, COLOR):
                    self.assertEqual(self.caret_char(view, width, theme), expected, (width, cursor, theme))
        frame, _ = render_screen(busy_view(input_buffer=buffer, input_cursor=300), 40, 20, PLAIN)
        prompt_line = frame.split("\n")[-1]
        self.assertTrue(prompt_line.startswith("> <"))
        self.assertTrue(prompt_line.endswith(buffer[-10:]))
        frame, _ = render_screen(busy_view(input_buffer=buffer, input_cursor=0), 40, 20, PLAIN)
        self.assertTrue(frame.split("\n")[-1].startswith("> abc"))
        self.assertTrue(frame.split("\n")[-1].endswith(">"))

    def test_wide_characters_in_buffer(self) -> None:
        buffer = "日本語チェス" * 20
        for width in (21, 40):
            for cursor in (0, 5, 60, 119, 120):
                view = busy_view(input_buffer=buffer, input_cursor=cursor)
                expected = buffer[cursor] if cursor < len(buffer) else " "
                self.assertEqual(self.caret_char(view, width), expected, (width, cursor))

    def test_tiny_widths(self) -> None:
        for width in (1, 2, 3, 4):
            frame, (row, col) = render_screen(busy_view(input_buffer="abcdef", input_cursor=3), width, 5, COLOR)
            self.assertLess(col, width)
            self.assertLessEqual(visible_len(frame.split("\n")[row]), width)


class RealEngineTest(unittest.TestCase):
    def test_render_real_board(self) -> None:
        try:
            from lanchess.engine import Board
        except ImportError:
            self.skipTest("engine not available")
        board = Board()
        for san in ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]:
            board.push_san(san)
        view = ViewState(board=board, white_name="Alice", black_name="Bob", game_over="Checkmate")
        for width, height in [(80, 24), (60, 30), (120, 40)]:
            for theme in ALL_THEMES:
                frame, _ = render_screen(view, width, height, theme)
                self.assertTrue(all(visible_len(line) <= width for line in frame.split("\n")))
                self.assertIn("Qxf7#", plain(frame))
        colored = "\n".join(render_board(board, "w", theme=COLOR))
        self.assertIn("48;5;%d" % ui.CHECK_SQUARE, colored)
        text = render_plain_board(board, "w", PLAIN)
        self.assertIn("White captured: p +1", text)
        self.assertIn("Last move: 4.Qxf7#", text)


if __name__ == "__main__":
    unittest.main()

"""Tests for lanchess.term: key parsing, line editing, plain input and screen output."""

from __future__ import annotations

import io
import os
import sys
import time
import unittest
from typing import Any, List, Optional
from unittest import mock

from lanchess import term
from lanchess.term import KeyParser, KeyReader, LineEditor, PlainLineInput, Screen, WinKeyParser


class FakeReader:
    """Scripted key source with the KeyReader interface."""

    def __init__(self, keys: List[str]) -> None:
        self.keys = list(keys)
        self.eof = False

    def read_key(self, timeout: float) -> Optional[str]:
        return self.keys.pop(0) if self.keys else None


def editor_with(keys: List[str], max_len: int = 500) -> LineEditor:
    return LineEditor(FakeReader(keys), max_len=max_len)


def drain(editor: LineEditor) -> list:
    events = []
    while editor._reader.keys:
        events.append(editor.poll(0))
    return events


class KeyParserEscapeTest(unittest.TestCase):
    def assertKeys(self, text: str, expected: List[str]) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed(text), expected, repr(text))
        self.assertFalse(parser.pending, repr(text))

    def test_csi_letter_sequences(self) -> None:
        for seq, key in [("A", "UP"), ("B", "DOWN"), ("C", "RIGHT"), ("D", "LEFT"), ("H", "HOME"), ("F", "END")]:
            self.assertKeys("\x1b[" + seq, [key])

    def test_csi_tilde_sequences(self) -> None:
        expected = {"1": "HOME", "3": "DELETE", "4": "END", "5": "PGUP", "6": "PGDN", "7": "HOME", "8": "END"}
        for number, key in expected.items():
            self.assertKeys("\x1b[%s~" % number, [key])

    def test_ss3_sequences(self) -> None:
        for seq, key in [("H", "HOME"), ("F", "END"), ("A", "UP"), ("B", "DOWN"), ("C", "RIGHT"), ("D", "LEFT"), ("M", "ENTER")]:
            self.assertKeys("\x1bO" + seq, [key])

    def test_modifier_parameters_are_ignored(self) -> None:
        self.assertKeys("\x1b[1;5C", ["RIGHT"])
        self.assertKeys("\x1b[1;2D", ["LEFT"])
        self.assertKeys("\x1b[3;5~", ["DELETE"])

    def test_unknown_sequences_are_ignored(self) -> None:
        for seq in ["\x1b[2~", "\x1b[15~", "\x1b[200~", "\x1b[Z", "\x1bOP", "\x1b[[A", "\x1b[<0;3;4M", "\x1b[I"]:
            self.assertKeys(seq + "x", ["x"])

    def test_alt_letter_is_ignored(self) -> None:
        self.assertKeys("\x1bxy", ["y"])

    def test_lone_escape_waits_then_flushes(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b"), [])
        self.assertTrue(parser.pending)
        self.assertEqual(parser.flush(), ["ESC"])
        self.assertFalse(parser.pending)
        self.assertEqual(parser.flush(), [])

    def test_double_escape_and_escape_before_control(self) -> None:
        self.assertKeys("\x1b\x1b[A", ["ESC", "UP"])
        self.assertKeys("\x1b\x03", ["ESC", "CTRL_C"])

    def test_sequence_split_across_feeds(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("a\x1b"), ["a"])
        self.assertEqual(parser.feed("["), [])
        self.assertEqual(parser.feed("5"), [])
        self.assertEqual(parser.feed("~b"), ["PGUP", "b"])
        self.assertEqual(parser.feed("\x1bO"), [])
        self.assertEqual(parser.feed("H"), ["HOME"])

    def test_partial_sequence_is_dropped_on_flush(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("\x1b[1;"), [])
        self.assertEqual(parser.flush(), [])
        self.assertEqual(parser.feed("a"), ["a"])

    def test_sequence_interrupted_by_control_character(self) -> None:
        self.assertKeys("\x1b[1\x03", ["CTRL_C"])

    def test_overlong_sequence_terminates(self) -> None:
        parser = KeyParser()
        keys = parser.feed("\x1b[" + "1" * 100 + "A")
        self.assertNotIn("UP", keys)
        self.assertFalse(parser.pending)


class KeyParserCharacterTest(unittest.TestCase):
    def test_enter_variants(self) -> None:
        self.assertEqual(KeyParser().feed("\r"), ["ENTER"])
        self.assertEqual(KeyParser().feed("\n"), ["ENTER"])
        self.assertEqual(KeyParser().feed("\r\n"), ["ENTER"])
        self.assertEqual(KeyParser().feed("\r\r"), ["ENTER", "ENTER"])
        self.assertEqual(KeyParser().feed("\n\n"), ["ENTER", "ENTER"])
        self.assertEqual(KeyParser().feed("\n\r"), ["ENTER", "ENTER"])
        self.assertEqual(KeyParser().feed("\r\n\r\n"), ["ENTER", "ENTER"])
        self.assertEqual(KeyParser().feed("a\r\nb"), ["a", "ENTER", "b"])

    def test_crlf_split_across_feeds_is_one_enter(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed("x\r"), ["x", "ENTER"])
        self.assertEqual(parser.feed("\ny"), ["y"])
        self.assertEqual(parser.feed("\n"), ["ENTER"])

    def test_backspace_variants(self) -> None:
        self.assertEqual(KeyParser().feed("\x7f\x08"), ["BACKSPACE", "BACKSPACE"])

    def test_control_keys(self) -> None:
        expected = {
            "\x01": "CTRL_A", "\x03": "CTRL_C", "\x04": "CTRL_D", "\x05": "CTRL_E", "\t": "TAB",
            "\x0c": "CTRL_L", "\x15": "CTRL_U", "\x17": "CTRL_W",
        }
        for char, key in expected.items():
            self.assertEqual(KeyParser().feed(char), [key], repr(char))

    def test_unmapped_control_characters_are_ignored(self) -> None:
        self.assertEqual(KeyParser().feed("\x00\x1a\x1c\x1f\x85a"), ["a"])

    def test_printable_unicode_passes_through(self) -> None:
        self.assertEqual(KeyParser().feed("é♞日 "), ["é", "♞", "日", " "])

    def test_utf8_multibyte_split_across_feeds(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed_bytes(b"\xc3"), [])
        self.assertEqual(parser.feed_bytes(b"\xa9"), ["é"])
        knight = "♞".encode("utf-8")
        self.assertEqual(parser.feed_bytes(b"a" + knight[:1]), ["a"])
        self.assertEqual(parser.feed_bytes(knight[1:2]), [])
        self.assertEqual(parser.feed_bytes(knight[2:] + b"b"), ["♞", "b"])
        emoji = "😀".encode("utf-8")
        keys: List[str] = []
        for byte in emoji:
            keys += parser.feed_bytes(bytes([byte]))
        self.assertEqual(keys, ["😀"])

    def test_invalid_utf8_is_dropped(self) -> None:
        self.assertEqual(KeyParser().feed_bytes(b"\xffa\xfe"), ["a"])

    def test_bytes_with_escape_sequences(self) -> None:
        parser = KeyParser()
        self.assertEqual(parser.feed_bytes(b"\x1b[A\xc3\xa9\x1b"), ["UP", "é"])
        self.assertEqual(parser.feed_bytes(b"[B"), ["DOWN"])

    def test_pasted_input(self) -> None:
        keys = KeyParser().feed("e2e4\r/c hi\r\n\x1b[A")
        self.assertEqual(keys, ["e", "2", "e", "4", "ENTER", "/", "c", " ", "h", "i", "ENTER", "UP"])


class WinKeyParserTest(unittest.TestCase):
    def test_prefixed_special_keys(self) -> None:
        expected = {
            "H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "G": "HOME",
            "O": "END", "S": "DELETE", "I": "PGUP", "Q": "PGDN",
        }
        for code, key in expected.items():
            for prefix in ("\x00", "\xe0"):
                self.assertEqual(WinKeyParser().feed(prefix + code), [key], repr(prefix + code))

    def test_unknown_special_keys_are_ignored(self) -> None:
        self.assertEqual(WinKeyParser().feed("\x00;\x00Da"), ["a"])
        self.assertEqual(WinKeyParser().feed("\xe0R\xe0\x86b"), ["b"])

    def test_plain_characters(self) -> None:
        keys = WinKeyParser().feed("a\r\x08\x1b\x03\t\x17")
        self.assertEqual(keys, ["a", "ENTER", "BACKSPACE", "ESC", "CTRL_C", "TAB", "CTRL_W"])

    def test_a_grave_is_a_character_not_a_prefix(self) -> None:
        parser = WinKeyParser()
        self.assertEqual(parser.feed("\xe0"), [])
        self.assertTrue(parser.pending)
        self.assertEqual(parser.flush(), ["\xe0"])
        self.assertEqual(WinKeyParser().feed("\xe0x"), ["\xe0", "x"])

    def test_surrogate_pairs_are_combined(self) -> None:
        parser = WinKeyParser()
        self.assertEqual(parser.feed("a\ud83d"), ["a"])
        self.assertEqual(parser.feed("\ude00b"), ["\U0001f600", "b"])
        self.assertEqual(WinKeyParser().feed("\ude00c"), ["c"])

    def test_prefix_split_across_feeds(self) -> None:
        parser = WinKeyParser()
        self.assertEqual(parser.feed("\x00"), [])
        self.assertEqual(parser.feed("H"), ["UP"])
        self.assertEqual(parser.flush(), [])


@unittest.skipUnless(hasattr(os, "openpty") and term.termios is not None, "requires a POSIX pty")
class KeyReaderPtyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.master, self.slave = os.openpty()

    def tearDown(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def settable(attrs: List[Any]) -> List[Any]:
        """termios attributes without the local-mode bits that the kernel itself manages: macOS and
        the BSDs set PENDIN when a terminal goes back to canonical mode (to retype pending input)."""
        termios = term.termios
        kernel_bits = getattr(termios, "PENDIN", 0) | getattr(termios, "FLUSHO", 0)
        attrs = list(attrs)
        attrs[3] &= ~kernel_bits
        return attrs

    def test_reads_keys_in_raw_mode_and_restores(self) -> None:
        termios = term.termios
        before = termios.tcgetattr(self.slave)
        with KeyReader(fd=self.slave) as reader:
            lflag = termios.tcgetattr(self.slave)[3]
            self.assertFalse(lflag & termios.ICANON)
            self.assertFalse(lflag & termios.ECHO)
            self.assertFalse(lflag & termios.ISIG)
            os.write(self.master, "ab\x1b[A\x03é".encode("utf-8"))
            keys = [reader.read_key(1.0) for _ in range(5)]
            self.assertEqual(keys, ["a", "b", "UP", "CTRL_C", "é"])
            self.assertIsNone(reader.read_key(0.02))
            os.write(self.master, b"\x1b")
            started = time.monotonic()
            self.assertEqual(reader.read_key(1.0), "ESC")
            self.assertLess(time.monotonic() - started, 0.5)
        after = termios.tcgetattr(self.slave)
        modes = termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN
        self.assertEqual(after[3] & modes, before[3] & modes)  # (the flags raw mode turned off)
        self.assertEqual(self.settable(after), self.settable(before))

    def test_kernel_managed_bits_are_ignored_but_nothing_else(self) -> None:
        termios = term.termios
        attrs = termios.tcgetattr(self.slave)
        pendin = getattr(termios, "PENDIN", 0)
        if pendin:
            changed = list(attrs)
            changed[3] |= pendin
            self.assertEqual(self.settable(changed), self.settable(attrs))
        changed = list(attrs)
        changed[3] ^= termios.ECHO
        self.assertNotEqual(self.settable(changed), self.settable(attrs))

    def test_pasted_crlf_is_one_enter(self) -> None:
        with KeyReader(fd=self.slave) as reader:
            os.write(self.master, b"a\r\nb\rc\n")
            keys = [reader.read_key(1.0) for _ in range(6)]
        self.assertEqual(keys, ["a", "ENTER", "b", "ENTER", "c", "ENTER"])

    def test_restore_is_idempotent(self) -> None:
        reader = KeyReader(fd=self.slave)
        reader.start()
        reader.restore()
        reader.restore()

    def test_eof_reports_ctrl_d_and_editor_eof(self) -> None:
        reader = KeyReader(fd=self.slave)
        with reader:
            os.close(self.master)
            self.assertEqual(reader.read_key(1.0), "CTRL_D")
            self.assertTrue(reader.eof)
            editor = LineEditor(reader)
            editor.buffer, editor.cursor = "abc", 1
            self.assertEqual(editor.poll(0.1), ("eof",))

    def test_rejects_non_terminal(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            with self.assertRaises(OSError):
                KeyReader(fd=read_fd).start()
        finally:
            os.close(read_fd)
            os.close(write_fd)


class LineEditorTest(unittest.TestCase):
    def test_typing_reports_changes(self) -> None:
        editor = editor_with(["a", "b", "c"])
        self.assertEqual(drain(editor), [("changed",)] * 3)
        self.assertEqual((editor.buffer, editor.cursor), ("abc", 3))

    def test_insert_in_middle(self) -> None:
        editor = editor_with(["a", "c", "LEFT", "b"])
        drain(editor)
        self.assertEqual((editor.buffer, editor.cursor), ("abc", 2))

    def test_backspace_and_delete_at_edges(self) -> None:
        editor = editor_with(["a", "b", "c", "BACKSPACE", "HOME", "BACKSPACE", "DELETE", "END", "DELETE"])
        events = drain(editor)
        self.assertEqual(events[3], ("changed",))
        self.assertEqual(events[5], None)
        self.assertEqual(events[6], ("changed",))
        self.assertEqual(events[8], None)
        self.assertEqual((editor.buffer, editor.cursor), ("b", 1))

    def test_backspace_and_delete_in_middle(self) -> None:
        editor = editor_with(list("abcd") + ["LEFT", "LEFT", "BACKSPACE", "DELETE"])
        drain(editor)
        self.assertEqual((editor.buffer, editor.cursor), ("ad", 1))

    def test_home_end_and_ctrl_a_ctrl_e(self) -> None:
        editor = editor_with(list("xyz") + ["HOME", "HOME", "LEFT", "CTRL_E", "END", "RIGHT", "CTRL_A"])
        events = drain(editor)
        self.assertEqual(events[3:], [("changed",), None, None, ("changed",), None, None, ("changed",)])
        self.assertEqual(editor.cursor, 0)

    def test_ctrl_u_clears(self) -> None:
        editor = editor_with(list("hello") + ["LEFT", "CTRL_U", "CTRL_U"])
        events = drain(editor)
        self.assertEqual(events[-2:], [("changed",), None])
        self.assertEqual((editor.buffer, editor.cursor), ("", 0))

    def test_ctrl_w_deletes_previous_word(self) -> None:
        editor = editor_with(list("hello big  world") + ["CTRL_W"])
        drain(editor)
        self.assertEqual((editor.buffer, editor.cursor), ("hello big  ", 11))
        editor._reader.keys = ["CTRL_W"]
        drain(editor)
        self.assertEqual(editor.buffer, "hello ")
        editor.buffer, editor.cursor = "one two three", 7
        self.assertEqual(editor.handle_key("CTRL_W"), ("changed",))
        self.assertEqual((editor.buffer, editor.cursor), ("one  three", 4))
        editor.cursor = 0
        self.assertIsNone(editor.handle_key("CTRL_W"))

    def test_enter_returns_line_and_clears(self) -> None:
        editor = editor_with(list("e4") + ["ENTER", "ENTER"])
        events = drain(editor)
        self.assertEqual(events[2:], [("line", "e4"), ("line", "")])
        self.assertEqual((editor.buffer, editor.cursor), ("", 0))

    def test_history_up_down(self) -> None:
        editor = editor_with(list("e4") + ["ENTER"] + list("Nf3") + ["ENTER", "ENTER"] + list("Nf3") + ["ENTER"])
        drain(editor)
        self.assertEqual(editor.history, ["e4", "Nf3"])
        editor.set_buffer("dra")
        self.assertEqual(editor.handle_key("UP"), ("changed",))
        self.assertEqual((editor.buffer, editor.cursor), ("Nf3", 3))
        self.assertEqual(editor.handle_key("UP"), ("changed",))
        self.assertEqual(editor.buffer, "e4")
        self.assertIsNone(editor.handle_key("UP"))
        self.assertEqual(editor.handle_key("DOWN"), ("changed",))
        self.assertEqual(editor.buffer, "Nf3")
        self.assertEqual(editor.handle_key("DOWN"), ("changed",))
        self.assertEqual((editor.buffer, editor.cursor), ("dra", 3))
        self.assertIsNone(editor.handle_key("DOWN"))

    def test_history_empty_is_noop(self) -> None:
        editor = editor_with(["UP", "DOWN"])
        self.assertEqual(drain(editor), [None, None])

    def test_ctrl_d(self) -> None:
        editor = editor_with(["CTRL_D"])
        self.assertEqual(editor.poll(0), ("eof",))
        editor = editor_with(list("ab") + ["HOME", "CTRL_D", "END", "CTRL_D"])
        events = drain(editor)
        self.assertEqual(events[3:], [("changed",), ("changed",), None])
        self.assertEqual(editor.buffer, "b")

    def test_ctrl_d_from_closed_reader_is_eof(self) -> None:
        editor = editor_with(list("ab") + ["CTRL_D"])
        drain(editor)
        editor._reader.keys = ["CTRL_D"]
        editor._reader.eof = True
        self.assertEqual(editor.poll(0), ("eof",))

    def test_signals_and_scrolling(self) -> None:
        editor = editor_with(["x", "CTRL_C", "CTRL_L", "PGUP", "PGDN", "TAB", "ESC"])
        self.assertEqual(drain(editor), [("changed",), ("interrupt",), ("redraw",), ("scroll", -1), ("scroll", 1), None, None])
        self.assertEqual(editor.buffer, "x")

    def test_max_len(self) -> None:
        editor = editor_with(list("abcd"), max_len=3)
        self.assertEqual(drain(editor), [("changed",)] * 3 + [None])
        self.assertEqual(editor.buffer, "abc")
        editor.set_buffer("123456")
        self.assertEqual(editor.buffer, "123")

    def test_timeout_returns_none(self) -> None:
        self.assertIsNone(editor_with([]).poll(0.01))

    def test_unicode_insert(self) -> None:
        editor = editor_with(["é", "♞"])
        drain(editor)
        self.assertEqual((editor.buffer, editor.cursor), ("é♞", 2))


class PlainLineInputTest(unittest.TestCase):
    def test_lines_then_single_eof(self) -> None:
        inp = PlainLineInput(io.StringIO("e4\nNf3\r\n\nlast"))
        self.assertEqual((inp.buffer, inp.cursor), ("", 0))
        events = [inp.poll(1.0) for _ in range(5)]
        self.assertEqual(events, [("line", "e4"), ("line", "Nf3"), ("line", ""), ("line", "last"), ("eof",)])
        self.assertIsNone(inp.poll(0))
        self.assertIsNone(inp.poll(0.01))
        inp._thread.join(1.0)
        self.assertFalse(inp._thread.is_alive())

    def test_blocking_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "r", encoding="utf-8")
        try:
            inp = PlainLineInput(stream)
            self.assertIsNone(inp.poll(0.05))
            os.write(write_fd, "hello ♞\n".encode("utf-8"))
            self.assertEqual(inp.poll(2.0), ("line", "hello ♞"))
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(inp.poll(2.0), ("eof",))
            inp._thread.join(1.0)
            self.assertFalse(inp._thread.is_alive())
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            stream.close()

    def test_stream_errors_become_eof(self) -> None:
        class Broken:
            def readline(self) -> str:
                raise OSError("gone")

        inp = PlainLineInput(Broken())  # type: ignore[arg-type]
        self.assertEqual(inp.poll(1.0), ("eof",))


class CountingStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes = 0

    def write(self, data: str) -> int:
        self.writes += 1
        return super().write(data)


class ScreenTest(unittest.TestCase):
    def test_enter_and_exit(self) -> None:
        stream = io.StringIO()
        screen = Screen(stream)
        screen.enter()
        self.assertTrue(stream.getvalue().startswith("\x1b[?1049h"))
        self.assertIn("\x1b[2J", stream.getvalue())
        stream.seek(0)
        stream.truncate()
        screen.exit()
        out = stream.getvalue()
        self.assertIn("\x1b[?25h", out)
        self.assertTrue(out.endswith("\x1b[?1049l"))
        stream.seek(0)
        stream.truncate()
        screen.exit()
        self.assertEqual(stream.getvalue(), "")

    def test_exit_without_enter_writes_nothing(self) -> None:
        stream = io.StringIO()
        Screen(stream).exit()
        self.assertEqual(stream.getvalue(), "")

    def test_context_manager(self) -> None:
        stream = io.StringIO()
        with Screen(stream) as screen:
            self.assertTrue(screen.active)
        self.assertFalse(screen.active)
        self.assertTrue(stream.getvalue().endswith("\x1b[?1049l"))

    def test_draw_is_one_write(self) -> None:
        stream = CountingStream()
        Screen(stream).draw("ab\ncd", (1, 2), size=(10, 5))
        out = stream.getvalue()
        self.assertEqual(stream.writes, 1)
        self.assertEqual(
            out,
            "\x1b[?25l\x1b[H" "ab\x1b[0m\x1b[K\r\ncd\x1b[0m\x1b[K" "\r\n\x1b[J" "\x1b[2;3H\x1b[?25h",
        )

    def test_full_width_and_full_height_frame(self) -> None:
        stream = io.StringIO()
        Screen(stream).draw("abcd\nxy", (0, 0), size=(4, 2))
        out = stream.getvalue()
        self.assertIn("abcd\x1b[0m\r\n", out)
        self.assertIn("xy\x1b[0m\x1b[K", out)
        self.assertNotIn("\x1b[J", out)

    def test_frame_clipped_and_cursor_clamped(self) -> None:
        stream = io.StringIO()
        Screen(stream).draw("1\n2\n3\n4", (9, 99), size=(5, 2))
        out = stream.getvalue()
        self.assertNotIn("3", out)
        self.assertNotIn("4", out)
        self.assertTrue(out.endswith("\x1b[2;5H\x1b[?25h"))

    def test_ansi_does_not_count_toward_width(self) -> None:
        stream = io.StringIO()
        Screen(stream).draw("\x1b[1mabcd\x1b[0m", (0, 0), size=(4, 1))
        self.assertNotIn("\x1b[K", stream.getvalue())

    def test_write_errors_are_swallowed(self) -> None:
        stream = io.StringIO()
        screen = Screen(stream)
        screen.enter()
        stream.close()
        screen.draw("x", (0, 0), size=(4, 4))
        screen.exit()


class EnvironmentTest(unittest.TestCase):
    def test_supports_unicode(self) -> None:
        utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        latin = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        self.assertTrue(term.supports_unicode(utf8))
        self.assertFalse(term.supports_unicode(ascii_stream))
        self.assertFalse(term.supports_unicode(latin))
        self.assertFalse(term.supports_unicode(io.StringIO()))

    def test_terminal_size(self) -> None:
        cols, rows = term.terminal_size()
        self.assertIsInstance(cols, int)
        self.assertGreater(cols, 0)
        self.assertGreater(rows, 0)

    def test_enable_ansi_never_raises(self) -> None:
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertFalse(term.enable_ansi())
        with mock.patch.object(sys, "stdout", None):
            self.assertFalse(term.enable_ansi())
        wrapper = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with mock.patch.object(sys, "stdout", wrapper):
            self.assertFalse(term.enable_ansi())
        self.assertEqual(wrapper.errors, "replace")

    def test_display_width(self) -> None:
        self.assertEqual(term.display_width("abc"), 3)
        self.assertEqual(term.display_width("\x1b[1;38;5;196mab\x1b[0m"), 2)
        self.assertEqual(term.display_width("日本"), 4)
        self.assertEqual(term.display_width("e\u0301♞"), 2)


if __name__ == "__main__":
    unittest.main()

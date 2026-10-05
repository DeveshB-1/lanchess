"""Cross-platform terminal plumbing: ANSI setup, raw key input, line editing, screen output."""

from __future__ import annotations

import atexit
import codecs
import os
import queue
import re
import select
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from typing import Any, Deque, Dict, List, Optional, TextIO, Tuple

try:
    import termios
except ImportError:  # pragma: no cover - Windows
    termios = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

ESC = "\x1b"
CSI = ESC + "["

ENTER = "ENTER"
BACKSPACE = "BACKSPACE"
DELETE = "DELETE"
LEFT = "LEFT"
RIGHT = "RIGHT"
UP = "UP"
DOWN = "DOWN"
HOME = "HOME"
END = "END"
PGUP = "PGUP"
PGDN = "PGDN"
TAB = "TAB"
ESCAPE = "ESC"
CTRL_C = "CTRL_C"
CTRL_D = "CTRL_D"
CTRL_L = "CTRL_L"
CTRL_U = "CTRL_U"
CTRL_W = "CTRL_W"
CTRL_A = "CTRL_A"
CTRL_E = "CTRL_E"
CTRL_Z = "CTRL_Z"
ALT_PREFIX = "ALT+"  # ALT+x: ESC followed by x (only reported when a parser is made with alt_keys=True)

CONTROL_KEYS: Dict[str, str] = {
    "\x01": CTRL_A,
    "\x02": LEFT,
    "\x03": CTRL_C,
    "\x04": CTRL_D,
    "\x05": CTRL_E,
    "\x06": RIGHT,
    "\x08": BACKSPACE,
    "\t": TAB,
    "\n": ENTER,
    "\x0c": CTRL_L,
    "\r": ENTER,
    "\x0e": DOWN,
    "\x10": UP,
    "\x15": CTRL_U,
    "\x17": CTRL_W,
    "\x1a": CTRL_Z,
    "\x7f": BACKSPACE,
}

_CSI_FINAL_KEYS = {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT, "H": HOME, "F": END}
_CSI_TILDE_KEYS = {"1": HOME, "3": DELETE, "4": END, "5": PGUP, "6": PGDN, "7": HOME, "8": END}
_SS3_KEYS = {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT, "H": HOME, "F": END, "M": ENTER}
_MAX_SEQUENCE = 32

_WIN_SPECIAL_KEYS = {
    "H": UP, "P": DOWN, "K": LEFT, "M": RIGHT, "G": HOME, "O": END, "S": DELETE, "I": PGUP, "Q": PGDN,
    "s": LEFT, "t": RIGHT, "w": HOME, "u": END, "\x8d": UP, "\x91": DOWN, "\x93": DELETE,
}
_WIN_PREFIXES = ("\x00", "\xe0")


def _is_ignored_windows_code(ch: str) -> bool:
    """Extended scan codes (Insert, F11/F12, Ctrl/Alt combos) that carry no key we use."""
    return ch in "Rv\x84" or "\x85" <= ch <= "\xa5"


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _translate_char(ch: str) -> Optional[str]:
    """Map one non-escape character to a key name, or None if it should be ignored."""
    key = CONTROL_KEYS.get(ch)
    if key is not None:
        return key
    if unicodedata.category(ch) == "Cc" or ch == "\ufffd":
        return None
    return ch


class KeyParser:
    """Pure, incremental terminal input parser: text or bytes in, key names out.

    ESC followed by a printable character is an Alt chord, or an Esc and a key that reached the
    program together (typed quickly, or read late). By default both are dropped; with
    ``alt_keys=True`` they come back as one ``ALT+x`` key so the caller can decide.
    """

    def __init__(self, alt_keys: bool = False) -> None:
        self.alt_keys = alt_keys
        self._buf = ""
        self._after_cr = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def pending(self) -> bool:
        """True while an escape sequence is incomplete (caller should flush after a short wait)."""
        return bool(self._buf)

    def feed_bytes(self, data: bytes) -> List[str]:
        """Decode UTF-8 incrementally (multibyte characters may be split across calls) and parse."""
        return self.feed(self._decoder.decode(data))

    def feed(self, text: str) -> List[str]:
        """Parse text and return the complete keys; an unfinished escape sequence is kept."""
        self._buf += text
        keys: List[str] = []
        i = 0
        buf = self._buf
        while i < len(buf):
            ch = buf[i]
            if ch == ESC:
                consumed, key = self._parse_escape(buf, i)
                if consumed == 0:
                    break
                i += consumed
                self._after_cr = False
                if key is not None and (self.alt_keys or not key.startswith(ALT_PREFIX)):
                    keys.append(key)
                continue
            i += 1
            if ch == "\n" and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = ch == "\r"
            key = _translate_char(ch)
            if key is not None:
                keys.append(key)
        self._buf = buf[i:]
        return keys

    def flush(self) -> List[str]:
        """Resolve a pending sequence after an input pause: a lone ESC is the ESC key, partial sequences are dropped."""
        pending, self._buf = self._buf, ""
        return [ESCAPE] if pending == ESC else []

    @staticmethod
    def _parse_escape(buf: str, i: int) -> Tuple[int, Optional[str]]:
        """Parse the escape sequence at buf[i]; returns (chars consumed, key). 0 consumed means incomplete."""
        if i + 1 >= len(buf):
            return 0, None
        nxt = buf[i + 1]
        if nxt == "[":
            return KeyParser._parse_csi(buf, i)
        if nxt == "O":
            if i + 2 >= len(buf):
                return 0, None
            return 3, _SS3_KEYS.get(buf[i + 2])
        if nxt == ESC or unicodedata.category(nxt) == "Cc":
            return 1, ESCAPE
        key = _translate_char(nxt)
        return 2, (ALT_PREFIX + key if key is not None and len(key) == 1 else None)

    @staticmethod
    def _parse_csi(buf: str, i: int) -> Tuple[int, Optional[str]]:
        j = i + 2
        if j < len(buf) and buf[j] == "[":
            return (0, None) if j + 1 >= len(buf) else (j + 2 - i, None)
        while j < len(buf):
            ch = buf[j]
            if "@" <= ch <= "~":
                params = buf[i + 2:j]
                if ch == "~":
                    return j + 1 - i, _CSI_TILDE_KEYS.get(params.split(";")[0])
                return j + 1 - i, _CSI_FINAL_KEYS.get(ch)
            if not " " <= ch <= "?" or j - i >= _MAX_SEQUENCE:
                return j - i, None
            j += 1
        return 0, None


class WinKeyParser:
    """Pure parser for ``msvcrt.getwch()`` output, where ``\\x00``/``\\xe0`` prefix special keys.

    Characters outside the BMP arrive as UTF-16 surrogate pairs and are recombined.
    """

    def __init__(self) -> None:
        self._prefix = ""
        self._high_surrogate = ""
        self._after_cr = False

    @property
    def pending(self) -> bool:
        """True while a ``\\xe0`` prefix awaits its scan code (flush if nothing follows)."""
        return bool(self._prefix)

    def feed(self, chars: str) -> List[str]:
        """Translate a batch of ``getwch()`` characters into key names."""
        keys: List[str] = []
        for ch in chars:
            if self._prefix:
                prefix, self._prefix = self._prefix, ""
                special = _WIN_SPECIAL_KEYS.get(ch)
                if special is not None or prefix == "\x00" or _is_ignored_windows_code(ch):
                    if special is not None:
                        keys.append(special)
                    continue
                keys.append(prefix)
            if ch in _WIN_PREFIXES:
                self._prefix = ch
                continue
            if "\ud800" <= ch <= "\udbff":
                self._high_surrogate = ch
                continue
            if "\udc00" <= ch <= "\udfff":
                high, self._high_surrogate = self._high_surrogate, ""
                if not high:
                    continue
                ch = (high + ch).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
            if ch == "\n" and self._after_cr:
                self._after_cr = False
                continue
            self._after_cr = ch == "\r"
            key = ESCAPE if ch == ESC else _translate_char(ch)
            if key is not None:
                keys.append(key)
        return keys

    def flush(self) -> List[str]:
        """A ``\\xe0`` with nothing after it is the literal character 'à'."""
        prefix, self._prefix = self._prefix, ""
        return [prefix] if prefix == "\xe0" else []


def _windows_kernel32() -> Any:
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _windows_console_mode(handle_id: int, set_bits: int = 0, clear_bits: int = 0) -> Optional[int]:
    """Adjust a Windows console handle's mode; returns the previous mode or None on failure."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_kernel32()
    handle = kernel32.GetStdHandle(handle_id)
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return None
    old = mode.value
    if not kernel32.SetConsoleMode(handle, (old | set_bits) & ~clear_bits):
        return None
    return old


def _restore_windows_console_mode(handle_id: int, mode: int) -> None:
    kernel32 = _windows_kernel32()
    kernel32.SetConsoleMode(kernel32.GetStdHandle(handle_id), mode)


_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


_original_code_pages: List[Tuple[int, int]] = []


def _set_windows_code_pages(kernel32: Any, output_cp: int, input_cp: int) -> None:
    """Switch console code pages, restoring the originals at exit."""
    if not _original_code_pages:
        _original_code_pages.append((kernel32.GetConsoleOutputCP(), kernel32.GetConsoleCP()))
        old_out, old_in = _original_code_pages[0]
        atexit.register(lambda: (kernel32.SetConsoleOutputCP(old_out), kernel32.SetConsoleCP(old_in)))
    kernel32.SetConsoleOutputCP(output_cp)
    kernel32.SetConsoleCP(input_cp)


def _is_utf8(encoding: Optional[str]) -> bool:
    try:
        return codecs.lookup(encoding or "").name == "utf-8"
    except LookupError:
        return False


def enable_ansi() -> bool:
    """Prepare stdout for ANSI output (VT mode + UTF-8 on Windows). Returns whether ANSI is believed supported."""
    try:
        stdout = sys.stdout
        if stdout is None:
            return False
        is_windows = os.name == "nt"
        supported = True
        if is_windows:
            try:
                kernel32 = _windows_kernel32()
                _set_windows_code_pages(kernel32, 65001, 65001)
                supported = _windows_console_mode(_STD_OUTPUT_HANDLE, set_bits=_ENABLE_VIRTUAL_TERMINAL_PROCESSING) is not None
            except Exception:
                supported = False
        reconfigure = getattr(stdout, "reconfigure", None)
        if reconfigure is not None:
            try:
                if is_windows or _is_utf8(getattr(stdout, "encoding", None)):
                    reconfigure(encoding="utf-8", errors="replace")
                else:
                    reconfigure(errors="replace")
            except Exception:
                pass
        isatty = getattr(stdout, "isatty", None)
        if not (isatty and isatty()):
            return False
        return supported and os.environ.get("TERM", "") != "dumb"
    except Exception:
        return False


def supports_unicode(stream: Optional[TextIO] = None) -> bool:
    """Whether the stream (default stdout) can encode the chess glyphs."""
    stream = stream if stream is not None else sys.stdout
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        "♚♛♜♝♞♟".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def terminal_size() -> Tuple[int, int]:
    """Terminal (columns, rows), falling back to 80x24."""
    size = shutil.get_terminal_size((80, 24))
    return max(1, size.columns), max(1, size.lines)


class KeyReader:
    """Raw keyboard reader (context manager). Raises OSError on enter if stdin is not a terminal.

    POSIX: echo, canonical mode, signals (Ctrl-C/Ctrl-Z), flow control and CR translation are off while active.
    """

    ESC_TIMEOUT = 0.1
    WINDOWS_POLL = 0.01

    def __init__(self, fd: Optional[int] = None, alt_keys: bool = False) -> None:
        """``alt_keys``: report ESC + character as ``ALT+x`` instead of dropping it (see KeyParser)."""
        self._fd = fd
        self._keys: Deque[str] = deque()
        self._parser: Any = WinKeyParser() if msvcrt is not None else KeyParser(alt_keys=alt_keys)
        self._saved_attrs: Optional[list] = None
        self._saved_console_mode: Optional[int] = None
        self._active = False
        self.eof = False

    def __enter__(self) -> "KeyReader":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.restore()

    def start(self) -> None:
        """Switch the terminal to raw-ish input mode."""
        if self._active:
            return
        if msvcrt is not None:
            try:
                self._saved_console_mode = _windows_console_mode(_STD_INPUT_HANDLE, clear_bits=_ENABLE_PROCESSED_INPUT)
            except Exception:
                self._saved_console_mode = None
        else:
            if self._fd is None:
                if sys.stdin is None:
                    raise OSError("stdin is not available")
                self._fd = sys.stdin.fileno()
            if not os.isatty(self._fd):
                raise OSError("stdin is not a terminal")
            self._saved_attrs = termios.tcgetattr(self._fd)
            attrs = termios.tcgetattr(self._fd)
            attrs[0] &= ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.IGNCR)
            attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN)
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSANOW, attrs)
        self._active = True
        atexit.register(self.restore)

    def restore(self) -> None:
        """Restore the terminal mode saved by start(). Idempotent."""
        if not self._active:
            return
        self._active = False
        try:
            atexit.unregister(self.restore)
        except Exception:
            pass
        try:
            if self._saved_attrs is not None and self._fd is not None:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attrs)
            if self._saved_console_mode is not None:
                _restore_windows_console_mode(_STD_INPUT_HANDLE, self._saved_console_mode)
        except Exception:
            pass

    close = restore

    def read_key(self, timeout: float) -> Optional[str]:
        """Return the next key name, or None if nothing arrives within ``timeout`` seconds."""
        if self._keys:
            return self._keys.popleft()
        if self.eof:
            return CTRL_D
        if msvcrt is not None:
            return self._read_key_windows(timeout)
        return self._read_key_posix(timeout)

    def _read_key_posix(self, timeout: float) -> Optional[str]:
        fd = self._fd if self._fd is not None else sys.stdin.fileno()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            wait = self.ESC_TIMEOUT if self._parser.pending else max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([fd], [], [], wait)
                data = os.read(fd, 4096) if ready else None
            except (OSError, ValueError):
                data = b""
            if data == b"":
                self.eof = True
                self._keys.extend(self._parser.flush())
                self._keys.append(CTRL_D)
            elif data:
                self._keys.extend(self._parser.feed_bytes(data))
            elif self._parser.pending:
                self._keys.extend(self._parser.flush())
            if self._keys:
                return self._keys.popleft()
            if not self._parser.pending and time.monotonic() >= deadline:
                return None

    def _read_key_windows(self, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + max(0.0, timeout)
        try:
            while True:
                chars = []
                while msvcrt.kbhit():
                    chars.append(msvcrt.getwch())
                if chars:
                    self._keys.extend(self._parser.feed("".join(chars)))
                    if self._parser.pending and not msvcrt.kbhit():
                        self._keys.extend(self._parser.flush())
                if self._keys:
                    return self._keys.popleft()
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.WINDOWS_POLL)
        except KeyboardInterrupt:
            return CTRL_C


class LineEditor:
    """Single-line editor with history, driven by any object with ``read_key(timeout)``."""

    HISTORY_SIZE = 200

    def __init__(self, key_reader: Any, max_len: int = 500) -> None:
        self._reader = key_reader
        self.max_len = max_len
        self.buffer = ""
        self.cursor = 0
        self.history: List[str] = []
        self._history_pos: Optional[int] = None
        self._draft = ""

    def poll(self, timeout: float) -> Optional[tuple]:
        """Read at most one key and apply it; returns an event tuple or None."""
        key = self._reader.read_key(timeout)
        if key is None:
            return None
        if key == CTRL_D and getattr(self._reader, "eof", False):
            return ("eof",)
        return self.handle_key(key)

    def handle_key(self, key: str) -> Optional[tuple]:
        """Apply one key name to the buffer; returns the resulting event, if any."""
        if key == ENTER:
            return self._submit()
        if key == CTRL_C:
            return ("interrupt",)
        if key == CTRL_L:
            return ("redraw",)
        if key == CTRL_Z:
            return ("undo",)
        if key == PGUP:
            return ("scroll", -1)
        if key == PGDN:
            return ("scroll", 1)
        if key == CTRL_D and not self.buffer:
            return ("eof",)
        before = (self.buffer, self.cursor)
        self._edit(key)
        return ("changed",) if (self.buffer, self.cursor) != before else None

    def set_buffer(self, text: str) -> None:
        """Replace the buffer and move the cursor to its end."""
        self.buffer = text[: self.max_len]
        self.cursor = len(self.buffer)

    def _submit(self) -> tuple:
        line = self.buffer
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[: -self.HISTORY_SIZE]
        self.buffer = ""
        self.cursor = 0
        self._history_pos = None
        self._draft = ""
        return ("line", line)

    def _edit(self, key: str) -> None:
        buf, pos = self.buffer, self.cursor
        if len(key) == 1:
            if len(buf) < self.max_len and key.isprintable():
                self.buffer = buf[:pos] + key + buf[pos:]
                self.cursor = pos + 1
        elif key == BACKSPACE:
            if pos > 0:
                self.buffer = buf[: pos - 1] + buf[pos:]
                self.cursor = pos - 1
        elif key in (DELETE, CTRL_D):
            self.buffer = buf[:pos] + buf[pos + 1:]
        elif key == LEFT:
            self.cursor = max(0, pos - 1)
        elif key == RIGHT:
            self.cursor = min(len(buf), pos + 1)
        elif key in (HOME, CTRL_A):
            self.cursor = 0
        elif key in (END, CTRL_E):
            self.cursor = len(buf)
        elif key == CTRL_U:
            self.buffer = ""
            self.cursor = 0
        elif key == CTRL_W:
            start = pos
            while start > 0 and buf[start - 1].isspace():
                start -= 1
            while start > 0 and not buf[start - 1].isspace():
                start -= 1
            self.buffer = buf[:start] + buf[pos:]
            self.cursor = start
        elif key == UP:
            self._recall(-1)
        elif key == DOWN:
            self._recall(1)

    def _recall(self, step: int) -> None:
        if not self.history:
            return
        if self._history_pos is None:
            if step > 0:
                return
            self._draft = self.buffer
            pos = len(self.history) - 1
        else:
            pos = self._history_pos + step
        if pos < 0:
            return
        if pos >= len(self.history):
            self._history_pos = None
            self.set_buffer(self._draft)
            return
        self._history_pos = pos
        self.set_buffer(self.history[pos])


class PlainLineInput:
    """Line input for non-TTY / plain mode: a daemon thread feeds ``readline()`` results into a queue."""

    _EOF = object()

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self.buffer = ""
        self.cursor = 0
        self._stream = stream
        self._queue: "queue.Queue[object]" = queue.Queue()
        self._eof_reported = False
        self._thread = threading.Thread(target=self._run, name="lanchess-stdin", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        stream = self._stream if self._stream is not None else sys.stdin
        while True:
            try:
                line = stream.readline() if stream is not None else ""
            except UnicodeDecodeError:
                continue
            except Exception:
                line = ""
            if not line:
                self._queue.put(self._EOF)
                return
            self._queue.put(line.rstrip("\r\n"))

    @property
    def eof(self) -> bool:
        """True once ``('eof',)`` has been returned (later polls only time out)."""
        return self._eof_reported

    def poll(self, timeout: float) -> Optional[tuple]:
        """Return ``('line', text)``, ``('eof',)`` once at end of input, or None on timeout."""
        if self._eof_reported:
            if timeout > 0:
                time.sleep(timeout)
            return None
        try:
            item = self._queue.get(timeout=timeout) if timeout > 0 else self._queue.get_nowait()
        except queue.Empty:
            return None
        if item is self._EOF:
            self._eof_reported = True
            return ("eof",)
        return ("line", item)


def display_width(text: str) -> int:
    """Terminal columns occupied by text, ignoring ANSI escape sequences."""
    text = _ANSI_RE.sub("", text)
    if text.isascii():
        return len(text)
    width = 0
    for ch in text:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf", "Cc"):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


class Screen:
    """Full-screen output on the alternate screen buffer with flicker-free redraws."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self._stream = stream
        self.active = False

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    def __enter__(self) -> "Screen":
        self.enter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.exit()

    def enter(self) -> None:
        """Switch to the alternate screen buffer and clear it."""
        if self.active:
            return
        self.active = True
        atexit.register(self.exit)
        self._write(CSI + "?1049h" + CSI + "0m" + CSI + "H" + CSI + "2J")

    def exit(self) -> None:
        """Reset attributes, show the cursor and leave the alternate screen. Idempotent, never raises."""
        if not self.active:
            return
        self.active = False
        try:
            atexit.unregister(self.exit)
        except Exception:
            pass
        self._write(CSI + "0m" + CSI + "?25h" + CSI + "?1049l")

    def draw(self, frame: str, cursor: Optional[Tuple[int, int]],
             size: Optional[Tuple[int, int]] = None) -> None:
        """Repaint the screen with ``frame`` in one write and place the caret at the 0-based (row, col).

        ``cursor`` None keeps the caret hidden (menus). Lines that fill the full width skip
        clear-to-EOL, and nothing is written after the bottom row, so terminals never scroll or
        erase the last column.
        """
        cols, rows = size if size is not None else terminal_size()
        lines = frame.split("\n")[:rows]
        out = [CSI + "?25l" + CSI + "H"]
        for index, line in enumerate(lines):
            if index:
                out.append("\r\n")
            out.append(line)
            out.append(CSI + "0m")
            if display_width(line) < cols:
                out.append(CSI + "K")
        if len(lines) < rows:
            out.append("\r\n" + CSI + "J")
        if cursor is None:
            self._write("".join(out))
            return
        row = min(max(0, cursor[0]), rows - 1)
        col = min(max(0, cursor[1]), cols - 1)
        out.append("%s%d;%dH%s?25h" % (CSI, row + 1, col + 1, CSI))
        self._write("".join(out))

    def _write(self, data: str) -> None:
        try:
            self.stream.write(data)
            self.stream.flush()
        except (OSError, ValueError, AttributeError):
            pass

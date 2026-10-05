> This is the original design spec that LAN Chess was built from. The code is the source of truth where they differ.

# LAN Chess — implementation spec (shared contract for all modules)

Terminal chess for two players on two computers on the same LAN (plus a local
hot-seat mode). **Pure Python standard library only** (no pip packages), must run
on **Python 3.8+** on Linux, macOS and Windows 10+. Do not use `match`, PEP 604
`X | Y` types at runtime, or other 3.9+ only syntax/APIs (`str.removeprefix`,
`list[int]` at runtime, etc.). `from __future__ import annotations` is fine.

Run tests from the project root with: `python3 -m unittest discover -s tests -v`

## Layout

```
lanchess/                 project root (/home/devesh/lanchess)
  lanchess/__init__.py    __version__ = "1.0.0"; APP_NAME = "lanchess"
  lanchess/__main__.py    from .cli import main; raise SystemExit(main())
  lanchess/engine.py      chess rules (no I/O)
  lanchess/net.py         TCP connection, protocol framing, handshake, LAN discovery
  lanchess/term.py        cross-platform raw keyboard input + ANSI setup + line editor
  lanchess/ui.py          pure rendering functions -> strings (no I/O)
  lanchess/game.py        TimeControl, ChessClock, GameSession controller, run loops
  lanchess/cli.py         argparse entry point, host/join/local flows
  tests/test_engine.py  tests/test_net.py  tests/test_ui.py  tests/test_term.py  tests/test_game.py
  play.py                 `python3 play.py host` convenience launcher
  build.py                builds dist/lanchess.pyz (single-file zipapp)
  README.md
```

## engine.py

Constants: `WHITE = 'w'`, `BLACK = 'b'`, `STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"`,
`PIECE_VALUES = {'p':1,'n':3,'b':3,'r':5,'q':9,'k':0}`.

Squares are ints 0..63: a1=0, b1=1, …, h1=7, a2=8, …, h8=63 (`file = sq % 8`, `rank = sq // 8`).
Pieces are single chars: `'PNBRQK'` white, `'pnbrqk'` black; empty = `None`.

- `square_name(sq) -> str` (`0 -> 'a1'`), `parse_square(name) -> int` (raises `ValueError`).
- `opposite(color) -> str`.
- `class IllegalMoveError(ValueError)` — message must be short and user friendly, e.g.
  `"Illegal move: Nf6"`, `"Ambiguous move: Nd2 (could be Nbd2, Nfd2)"`, `"Unrecognized move: xyz"`.
- `@dataclass(frozen=True) class Move: from_sq: int; to_sq: int; promotion: Optional[str] = None`
  (promotion is lowercase `'q','r','b','n'`). `.uci() -> str` (`"e7e8q"`), `Move.from_uci(s) -> Move`
  (syntax only; raises `ValueError`). Hashable, comparable by value.
- `@dataclass(frozen=True) class Outcome: termination: str; winner: Optional[str]` (`'w'`, `'b'` or `None`=draw).
  `.result() -> str` returns `'1-0'`, `'0-1'`, `'1/2-1/2'`, or `'*'` for termination `'abandoned'`.
  `.describe() -> str` human text like `"Checkmate — White wins"`, `"Draw by stalemate"`,
  `"Draw by threefold repetition"`, `"Black wins by resignation"`, `"White wins on time"`,
  `"Draw — timeout vs insufficient material"`, `"Draw by agreement"`, `"Game abandoned"`.
  Engine terminations: `'checkmate','stalemate','insufficient_material','threefold_repetition','fifty_moves'`.
  Game-level terminations (created by game.py): `'resignation','agreement','timeout','timeout_insufficient','abandoned'`.
- `class Board`:
  - `__init__(fen=STARTING_FEN)` — raises `ValueError` on invalid FEN (bad rows, missing kings,
    more than one king per side, pawns on rank 1/8, side not to move in check is OK to reject).
  - attributes: `turn`, `castling` (str subset of `"KQkq"` in that order, `""` if none),
    `ep_square: Optional[int]`, `halfmove_clock: int`, `fullmove_number: int`,
    `root_fen: str` (FEN at construction), `move_stack: List[Move]`, `san_stack: List[str]`.
  - `piece_at(sq) -> Optional[str]`, `copy() -> Board` (deep, independent), `fen() -> str`.
    FEN ep field: write the ep square whenever the last move was a double pawn push (standard).
  - `legal_moves() -> List[Move]` (fully legal; each promotion yields 4 moves), `is_legal(move) -> bool`.
  - `push(move)` — validates legality (raises `IllegalMoveError`), applies it, appends to
    `move_stack` and its SAN (with `+`/`#`) to `san_stack`. `pop() -> Move` undoes the last move exactly
    (all state incl. castling rights, ep, clocks, captured pieces, repetition history). Raises `IndexError` if empty.
  - `san(move) -> str` — SAN for a legal move in the current position: piece letter, minimal
    disambiguation (file, then rank, then both), `x` for captures (pawn captures as `exd5`), `=Q` promotion,
    `O-O` / `O-O-O`, suffix `+` or `#`.
  - `parse_san(text) -> Move` — accepts standard SAN plus leniencies: `0-0`, `0-0-0`, `o-o`, `OO`; trailing
    `+ # ! ?` ignored; promotion as `e8=Q`, `e8Q`, `e8=q`, `e8q`; lowercase piece letters `n r q k`
    allowed (lowercase `b` is a pawn file first; if no pawn move matches, try bishop);
    over-disambiguation allowed (`Ngf3`, `Ng1f3`); missing `x` allowed (`ed5`, `Nd5` for capture).
    A pawn move to the last rank without promotion piece defaults to queen.
    Raises `IllegalMoveError` (unrecognized / illegal / ambiguous).
  - `parse_move(text) -> Move` — strips whitespace; tries coordinate forms first
    (`e2e4`, `e7e8q`, `e2-e4`, `e2 e4`, `e2xe4`, `E2E4`; castling as king two squares `e1g1`), then SAN.
    Coordinate pawn move to last rank without piece defaults to queen. Raises `IllegalMoveError`.
  - `push_san(text) -> Move` (= `push(parse_move(text))`, returns the move).
  - `is_check()`, `is_checkmate()`, `is_stalemate()`, `is_insufficient_material()` (K v K, K+N v K,
    K+B v K, K+B v K+B with all bishops on same square colour — in general: no pawns/rooks/queens and
    either at most one minor total or all minors are bishops on one square colour),
    `has_insufficient_material(color) -> bool` (that color alone cannot mate: only K, or K + exactly one N or B,
    used for timeout rulings), `is_repetition(count=3) -> bool` (current position occurred >= count times,
    position key = placement + turn + castling + ep square **only if an en-passant capture is legal**),
    `is_fifty_moves()` (halfmove_clock >= 100).
  - `outcome() -> Optional[Outcome]` — checked in order: checkmate, stalemate, insufficient material,
    threefold repetition, fifty moves. Draws are automatic (no claiming).
  - `king_square(color) -> int`, `is_attacked(sq, by_color) -> bool`, `checkers_square() -> Optional[int]`
    is not required; instead `check_square() -> Optional[int]` = king square of side to move if in check.
  - `captured_pieces(color) -> List[str]` — pieces OF `color` captured so far (in that color's case),
    sorted by value descending (q, r, b, n, p). En-passant captures included. (A promoted piece that is
    later captured counts as that piece.)
  - `material_balance() -> int` — white minus black using PIECE_VALUES.
  - `legal_moves_san() -> List[str]` sorted.
  - `pgn(headers: Dict[str,str], result: str) -> str` — Seven Tag Roster order first (Event, Site, Date,
    Round, White, Black, Result; defaults `"?"`/`"????.??.??"`), then any extra headers; adds
    `SetUp "1"` and `FEN` headers if `root_fen != STARTING_FEN`. Movetext from `san_stack` with move
    numbers (`1. e4 e5 2. Nf3`; if root has black to move start with `N...`), wrapped at 80 columns, ending in result.
  - `last_move() -> Optional[Move]`.
- `perft(board, depth) -> int` (module function). Must be correct and reasonably fast:
  start position depth 4 (197281) in < ~30 s on CPython. Use internal make/unmake that skip SAN.

## net.py

Constants: `PROTOCOL_VERSION = 1`, `DEFAULT_PORT = 5555` (TCP game), `DISCOVERY_PORT = 5556` (UDP),
`MAX_LINE = 65536`.

Wire format: TCP, each message one JSON object (UTF-8, `ensure_ascii=False`, compact) terminated by `"\n"`.
Every message has a string field `"type"`.

- `class ConnectionClosed(Exception)`, `class HandshakeError(Exception)`.
- `class Connection(sock, *, ping_interval=5.0, idle_timeout=30.0)`:
  - `.start()` launches a daemon reader thread and a daemon heartbeat thread (sends `{"type":"ping"}`
    every `ping_interval`; if nothing at all received for `idle_timeout` → disconnect with reason
    `"timed out"`). Sets TCP_NODELAY and SO_KEEPALIVE.
  - `.inbox: queue.Queue` of received message dicts. `ping` is answered with `pong` internally; `ping`/`pong`
    are never put in the inbox.
  - Invalid JSON / non-object / missing string `type` → put `{"type":"_protocol_error","reason":...}` and
    continue. Line longer than MAX_LINE → disconnect.
  - On EOF / socket error / timeout put exactly one `{"type":"_disconnected","reason":"..."}` then stop.
    After a local `close()` no `_disconnected` event is required.
  - `.send(msg: dict)` thread-safe (lock), raises `ConnectionClosed` if closed or on socket error;
    a whole message may take at most `send_timeout` (10 s) seconds.
  - `.close()` idempotent, waits for the threads to stop; `.closed` bool; `.peer: str` like `"10.0.0.5:5555"`.
  - Portability: the socket timeout is a short poll interval (0.25 s), so the reader re-checks for
    `close()` at least that often; on macOS and the BSDs neither `shutdown()` nor `close()` from another
    thread reliably wakes a thread blocked in `recv`. `close()` shuts the socket down and the reader
    closes the descriptor as it exits (never under a thread still using it). As that timeout is the
    reader's, `send` waits for buffer space itself (`poll`, or `select` on Windows), each wait bounded by
    what is left of `send_timeout`, so a peer that reads slowly cannot stretch a message past it.
- `class Server(port=DEFAULT_PORT, bind="0.0.0.0")`: listening socket with SO_REUSEADDR (not on Windows
  — use SO_EXCLUSIVEADDRUSE semantics there or just skip), `.port` (actual bound port; port 0 allowed for
  tests), `.accept(timeout) -> Optional[Connection]` (returns a *started* Connection, or None on timeout —
  lets callers poll for Ctrl-C — or when the client gave up before it was accepted: a `ConnectionError`
  such as ECONNABORTED, and on Linux also the pending network errors that accept(2) says to retry;
  more than 50 of those within a second are raised instead, as the error is then not about clients),
  `.close()`. Raises `OSError` from constructor if port busy.
- `connect(host, port=DEFAULT_PORT, timeout=10.0) -> Connection` (started).
- `parse_host_port(text, default_port=DEFAULT_PORT) -> Tuple[str,int]` (`"10.0.0.5"`, `"10.0.0.5:6000"`,
  `"myhost:6000"`; raises `ValueError` on bad port).
- `local_ip_addresses(wait=0.5) -> List[Tuple[str,str]]` — `(interface_name_or_"", ipv4)` for non-loopback
  IPv4 addresses, best effort, never raises, never blocks on name resolution. Linux, macOS and FreeBSD:
  enumerate interfaces via `socket.if_nameindex()` + `fcntl.ioctl` SIOCGIFFLAGS/SIOCGIFADDR (same `struct
  ifreq` offsets, different request numbers). Plus the UDP-connect trick to 8.8.8.8 (no packets sent).
  Elsewhere (Windows) or if that finds nothing: `getaddrinfo(gethostname())` in a background thread, cached
  for 60 s; a caller waits at most `wait` seconds for its first answer (resolving the own host name can
  take half a minute on macOS when DNS/mDNS do not know it).
- `broadcast_targets(wait=0.0) -> List[str]` — `"255.255.255.255"`, real per-interface broadcast addresses
  (ioctl SIOCGIFBRDADDR), and `x.y.z.255` guesses for each local IP; deduplicated; never raises; by default
  never waits for a host-name lookup.
- `HostInfo = namedtuple("HostInfo", "name address port")`.
- `class DiscoveryResponder(name, game_port, discovery_port=DISCOVERY_PORT)`: `.start() -> bool` binds UDP
  `("", discovery_port)` (SO_REUSEADDR) in a daemon thread; returns False (no exception) if bind fails.
  On datagram `{"app":"lanchess","type":"discover","v":1}` replies unicast to the sender address with
  `{"app":"lanchess","type":"announce","v":1,"name":name,"port":game_port}`. Ignores anything else. `.stop()`.
- `discover_hosts(timeout=2.5, discovery_port=DISCOVERY_PORT, extra_targets=()) -> List[HostInfo]` — UDP socket
  with SO_BROADCAST; sends the discover datagram to every `broadcast_targets()` + `extra_targets` address
  (ignore per-target send errors; the targets are recomputed for each burst) every 0.5 s until timeout; collects announces, dedup by (address, port),
  address = sender IP. Never raises on network errors (returns what it found).
- Handshake (blocking helpers, read from `conn.inbox`, ignore unrelated messages, `timeout` seconds):
  - `client_handshake(conn, my_name, timeout=10.0) -> dict` sends
    `{"type":"hello","app":"lanchess","version":PROTOCOL_VERSION,"name":my_name}`; returns the `welcome`
    message dict; on `reject` raise `HandshakeError(reason)`; on `_disconnected`/timeout raise `HandshakeError`.
  - `server_handshake(conn, my_name, color_pref, time_control, fen=STARTING_FEN, timeout=10.0) -> Tuple[str,str]`
    waits for `hello`; if `app != "lanchess"` or `version != PROTOCOL_VERSION` → send
    `{"type":"reject","reason":"..."}` and raise `HandshakeError`. `color_pref` is `'w'`, `'b'` or `'random'`
    (host's own color). Sends
    `{"type":"welcome","app":"lanchess","version":1,"name":my_name,"your_color":<joiner color>,"time_control":<dict or null>,"fen":fen}`
    where time_control dict is `{"initial_ms":int,"increment_ms":int}`. Returns `(opponent_name, host_color)`.
    Names are sanitized: stripped, control chars removed, max 20 chars, default `"Opponent"`.

### Game messages (sent/handled by game.py)

| type | fields | meaning |
|---|---|---|
| `move` | `uci`, `ply` (int, 1-based index of this move in the game), `clock_ms` (mover's remaining after increment, or null) | a move |
| `chat` | `text` (≤ 500 chars) | chat line |
| `resign` | — | sender resigns |
| `draw_offer` / `draw_accept` / `draw_decline` | — | draw offer protocol |
| `takeback_request` / `takeback_accept` / `takeback_decline` | `ply` (current ply count when requested) | requester undoes their own last move (2 plies if requester is to move, else 1) |
| `timeout` | `loser` (`'w'`/`'b'`) | sender's own clock reached zero (only the flagging side reports) |
| `rematch_offer` / `rematch_accept` / `rematch_decline` | — | after game over; new game, colors swapped, same time control, standard start position |
| `error` | `reason` | peer detected desync/illegal message |
| `bye` | — | clean disconnect |

## term.py

- `enable_ansi() -> bool` — on Windows enable VT processing via ctypes (`SetConsoleMode` with
  `ENABLE_VIRTUAL_TERMINAL_PROCESSING`) and set console output CP to UTF-8; reconfigure `sys.stdout` to
  UTF-8 with `errors="replace"` where possible. Returns whether ANSI is believed supported. Never raises.
- `supports_unicode() -> bool` — stdout encoding can encode `"♚♛♜♝♞♟"`.
- `terminal_size() -> Tuple[int,int]` (cols, rows) via `shutil.get_terminal_size((80, 24))`.
- Key names returned by readers: single printable characters (any unicode), and the strings
  `'ENTER','BACKSPACE','DELETE','LEFT','RIGHT','UP','DOWN','HOME','END','PGUP','PGDN','TAB','ESC',
  'CTRL_C','CTRL_D','CTRL_L','CTRL_U','CTRL_W','CTRL_A','CTRL_E'`.
- `class KeyReader` — context manager. POSIX: on enter save termios attrs and switch stdin to cbreak-like
  mode (ICANON and ECHO off, **ISIG off** so Ctrl-C arrives as `'CTRL_C'`), restore on exit (also via
  `atexit` safety). `.read_key(timeout: float) -> Optional[str]` uses `select` + `os.read`, incremental UTF-8
  decoding, buffers multiple keys from one read (paste), parses CSI/SS3 escape sequences
  (`ESC [ A/B/C/D/H/F`, `ESC [ 1~ 3~ 4~ 5~ 6~ 7~ 8~`, `ESC O H/F`), lone ESC → `'ESC'`, `\r`/`\n` → `'ENTER'`,
  `\x7f`/`\x08` → `'BACKSPACE'`, `\t` → `'TAB'`, `\x03` `'CTRL_C'`, `\x04` `'CTRL_D'`, etc. Unknown sequences ignored.
  Windows: `msvcrt.kbhit()`/`getwch()` polling (sleep ~10 ms) with `'\x00'`/`'\xe0'` prefixes mapped
  (H up, P down, K left, M right, G home, O end, S delete, I pgup, Q pgdn). The parsing logic must be
  factored into a pure, unit-testable function/class (e.g. `KeyParser.feed(text) -> List[str]`).
- `class LineEditor(key_reader, max_len=500)`: `.buffer: str`, `.cursor: int`. `.poll(timeout) -> Optional[tuple]`:
  returns `('line', text)` on ENTER (adds non-empty lines to history, clears buffer), `('changed',)` when
  buffer/cursor changed, `('interrupt',)` on CTRL_C, `('eof',)` on CTRL_D with empty buffer, `('redraw',)`
  on CTRL_L, `('scroll', -1|+1)` on PGUP/PGDN, `None` on timeout/no-op. Editing: insert at cursor, BACKSPACE,
  DELETE, LEFT/RIGHT, HOME/END, CTRL_A/CTRL_E, CTRL_U (clear), CTRL_W (delete word), UP/DOWN history.
  Must be unit-testable with a fake reader that returns scripted keys.
- `class PlainLineInput` — for non-TTY / `--plain`: daemon thread reading `sys.stdin.readline()` into a
  queue. `.poll(timeout)` returns `('line', text)` (newline stripped), `('eof',)` once at EOF, or `None`.
  `.buffer = ""`, `.cursor = 0` for interface compatibility.

## ui.py (pure: returns strings, never prints)

- `@dataclass class Theme: unicode: bool = True; color: bool = True` and ANSI helpers.
- `render_board(board, perspective='w', last_move=None, theme=Theme()) -> List[str]` — 8 ranks with
  rank labels left, file labels below (flipped when perspective is `'b'`). Each square 3 columns wide.
  Color mode: 256-colour backgrounds for light/dark squares, distinct highlight for last-move from/to squares,
  red-ish background for the king in check (`board.check_square()`); pieces drawn with **filled** glyphs
  `♚♛♜♝♞♟` for both sides, foreground bright white for White and black for Black (unicode mode) or letters
  `KQRBNP` (ascii+color mode). No-color mode: uppercase = White, lowercase = Black, `.` empty square,
  ascii letters even if unicode is available is acceptable but unicode glyphs `♔♕♖♗♘♙` (white) / `♚♛♜♝♞♟`
  (black) are preferred when `theme.unicode`. Last move marked in no-color mode is optional.
- `visible_len(s) -> int` — length ignoring ANSI escape sequences. `pad(s, width)`, `truncate(s, width)`
  ANSI-aware helpers.
- `@dataclass class ViewState` fields (all with defaults):
  `board`, `perspective='w'`, `my_color: Optional[str]` (None in local mode), `white_name`, `black_name`,
  `clock_ms: Optional[Dict[str,int]]` (None = untimed), `clock_running: Optional[str]`,
  `log: List[Tuple[str,str]]` (kind in `'info','chat_me','chat_them','error','system','game'`, text),
  `status: str`, `prompt: str = "> "`, `input_buffer: str`, `input_cursor: int`, `game_over: Optional[str]`,
  `connection: str` (e.g. `"Connected to 10.0.0.5"` / `"Local game"`), `pending: str` (e.g. `"Draw offered by opponent — /accept or /decline"`),
  `log_scroll: int = 0`.
- `format_clock(ms) -> str` — `m:ss` (≥ 20 s) or `s.d` tenths (< 20 s), `0:00` floor; hours as `h:mm:ss`.
- `render_screen(view, width, height, theme) -> Tuple[str, Tuple[int,int]]` — returns the full frame text
  (lines joined by `"\n"`, every line ≤ width visible cols) and the (row, col) 0-based cursor position of the
  input caret. Wide layout (width ≥ 70): board left, right panel with player names + clocks (side to move
  marked, running clock highlighted, low time < 20 s red), captured pieces + material diff (`+3`), move list in
  pairs (`1. e4 e5`), showing the most recent moves that fit. Narrow layout: panel stacked under board.
  Below: message log (most recent lines that fit, honoring `log_scroll`), `pending` line, status line, then the
  prompt line with the input buffer (horizontally scrolled if longer than width). Must not exceed `height`
  lines (drop log lines first). Works with `theme.color=False` (no escape codes at all in output).
- `render_plain_board(board, perspective, theme) -> str` — for plain mode (board + captured + last move).
- `help_lines() -> List[str]` — command reference (see game.py commands).

## game.py

- `@dataclass(frozen=True) class TimeControl: initial_ms: int; increment_ms: int`; `.to_dict()`,
  `TimeControl.from_dict(d)` (None-safe), `parse_time_control(text) -> Optional[TimeControl]`: `"5+3"` = 5 min
  + 3 s increment, `"10"` = 10 min no increment, `"0.5+0"` = 30 s, `"none"`/`"0"`/`""` → None; raises `ValueError`.
  `str()` like `"5+3"`.
- `class ChessClock(tc, now_fn=time.monotonic)`: per-color remaining ms; `.running: Optional[str]`;
  `.remaining(color) -> int` (live, subtract elapsed for the running color, may go negative);
  `.start(color)`, `.stop()`, `.press(color_moved) -> int` (stop mover, add increment, return mover remaining,
  start the other color), `.set_remaining(color, ms)`, `.flagged() -> Optional[str]` (running color with
  remaining <= 0). Clocks start running only after White's first move (Black's clock starts then).
- `class GameSession` — the controller, independent of the terminal so it's unit-testable:
  `GameSession(*, mode, my_color, my_name, opponent_name, conn=None, time_control=None, fen=STARTING_FEN,
  now_fn=time.monotonic, pgn_dir=None, autosave=True)`; `mode` is `'network'` or `'local'`.
  `.board`, `.over: Optional[Outcome]`, `.quit_requested: bool`, `.log` (list of (kind,text)),
  `.handle_input(line)`, `.handle_message(msg)`, `.tick()` (clock flag detection), `.view_state() -> ViewState`,
  `.dirty` flag (render needed), `.pgn() -> str`, `.save_pgn(path=None) -> str` (returns path).
  Rules:
  - Input that starts with `/` is a command; anything else is parsed as a move (`board.parse_move`). If it's
    not a legal move and doesn't look like a move, error: `Not a move. Type /help for commands, /c <msg> to chat.`
  - Commands: `/help` `?`; `/c <msg>`, `/chat <msg>`, `/say <msg>`; `/resign`; `/draw` (offer, or accept if
    the opponent offered); `/accept`, `/decline` (respond to the pending draw/takeback/rematch offer);
    `/takeback` `/undo`; `/flip`; `/moves` (legal moves in SAN); `/fen`; `/pgn` (print PGN to log);
    `/save [path]`; `/rematch` (after game over); `/quit` `/exit` `/q` (during a live network game, first asks
    for confirmation: a second `/quit` within 10 s resigns and leaves); `/clear` (clear log).
  - A move is rejected if the game is over, not your turn, or your clock has flagged.
  - Every move (sent or received) clears all pending draw/takeback offers on both sides.
  - Received `move` is validated: correct ply, opponent's turn, legal. Otherwise send `error` and end the game
    as `abandoned` with a clear log message (desync).
  - After every move check `board.outcome()`. On game end, log the result and autosave PGN (if enabled) to
    `pgn_dir` (default `~/lanchess_games/`), filename `YYYYMMDD-HHMMSS_White-vs-Black.pgn` (sanitized).
  - Timeouts: only the side whose own clock hits zero reports `timeout`. Ruling: if the opponent of the flagged
    player `has_insufficient_material` → draw `timeout_insufficient`; else opponent wins `timeout`.
  - `_disconnected` / `bye` while playing → over with `abandoned`; log "Opponent disconnected/left".
  - Local mode: side to move is always "me"; `/draw` = immediate draw by agreement, `/resign` = side to move
    resigns, `/takeback` = undo one ply immediately, perspective follows side to move unless flip disabled.
- `run_interactive(session, conn, theme, plain=False)` — main loop: poll input (LineEditor or PlainLineInput,
  ~50–100 ms), drain `conn.inbox`, `session.tick()`, re-render when dirty / input changed / once per 100 ms while a
  clock runs below 20 s or once per second otherwise. Full-screen mode uses alternate screen buffer
  (`ESC[?1049h` / `ESC[?1049l`), redraws with cursor-home + per-line clear-to-EOL (no flicker), restores the
  terminal on exit even on exceptions. Plain mode prints board after position changes and new log lines.
  Sends `bye` and closes the connection when quitting.

## cli.py

```
python3 -m lanchess host [--port 5555] [--name NAME] [--color white|black|random] [--time 5+3] [--fen FEN] [--no-discovery]
python3 -m lanchess join [HOST[:PORT]] [--port 5555] [--name NAME] [--scan-time 2.5]
python3 -m lanchess local [--time 5+3] [--fen FEN] [--no-flip]
common: --ascii  --no-color  --plain  --pgn-dir DIR  --no-save  --version
```
- `main(argv=None) -> int`. Default name: `getpass.getuser()` (fallback "Player"). Default color: white for host.
- host: start `Server`; print all `local_ip_addresses()` with the exact join command to type on the other laptop,
  start `DiscoveryResponder` (warn if it fails); wait with `accept(0.5)` loop (Ctrl-C cancels cleanly);
  `server_handshake`; then GameSession + run_interactive.
- join without HOST: `discover_hosts()`; list numbered results; prompt to pick a number or type an address
  (plain `input()`); with no results, explain and ask for an IP. Then `connect` + `client_handshake`.
- Friendly errors (no tracebacks) for: port in use, connection refused, timeout, unreachable, handshake rejection,
  with firewall hints (TCP 5555, UDP 5556).

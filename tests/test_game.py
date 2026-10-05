"""Tests for lanchess.game: time controls, clocks, the GameSession controller and the run loops."""

from __future__ import annotations

import io
import json
import os
import queue
import signal
import tempfile
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple

from lanchess import game, net, ui
from lanchess.engine import BLACK, STARTING_FEN, WHITE, Outcome
from lanchess.game import NOT_A_MOVE, ChessClock, GameSession, TimeControl, parse_time_control
from lanchess.net import ConnectionClosed

FOOLS_MATE = ("f3", "e5", "g4", "Qh4#")
WAIT = 5.0


class FakeTime:
    """A controllable monotonic clock (seconds)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeConn:
    """In-memory stand-in for net.Connection: messages go through a JSON round trip to the peer's inbox."""

    def __init__(self, peer: str) -> None:
        self.inbox: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.peer = peer
        self.other: Optional["FakeConn"] = None
        self.closed = False
        self.sent: List[Dict[str, Any]] = []

    def send(self, msg: Dict[str, Any]) -> None:
        if self.closed:
            raise ConnectionClosed("connection is closed")
        data = json.loads(json.dumps(msg, ensure_ascii=False))
        self.sent.append(data)
        if self.other is not None and not self.other.closed:
            self.other.inbox.put(data)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.other is not None and not self.other.closed:
            self.other.inbox.put({"type": "_disconnected", "reason": "connection closed by peer"})


def fake_pair() -> Tuple[FakeConn, FakeConn]:
    first, second = FakeConn("10.0.0.2:40000"), FakeConn("10.0.0.1:5555")
    first.other, second.other = second, first
    return first, second


def pump(*sessions: GameSession) -> int:
    """Deliver queued messages until every inbox is empty; returns how many were handled."""
    handled = 0
    progress = True
    while progress:
        progress = False
        for session in sessions:
            inbox = session.conn.inbox
            while True:
                try:
                    msg = inbox.get_nowait()
                except queue.Empty:
                    break
                session.handle_message(msg)
                handled += 1
                progress = True
    return handled


def texts(session: GameSession, kind: Optional[str] = None) -> List[str]:
    return [text for k, text in session.log if kind is None or k == kind]


class SessionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanchess-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.now = FakeTime()

    def make_pair(self, tc: Optional[TimeControl] = None, fen: str = STARTING_FEN,
                  autosave: bool = False) -> Tuple[GameSession, GameSession]:
        conn_a, conn_b = fake_pair()
        alice = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob", conn=conn_a,
                            time_control=tc, fen=fen, now_fn=self.now,
                            pgn_dir=os.path.join(self.tmp, "alice"), autosave=autosave)
        bob = GameSession(mode="network", my_color=BLACK, my_name="Bob", opponent_name="Alice", conn=conn_b,
                          time_control=tc, fen=fen, now_fn=self.now,
                          pgn_dir=os.path.join(self.tmp, "bob"), autosave=autosave)
        return alice, bob

    def play(self, alice: GameSession, bob: GameSession, *moves: str) -> None:
        for move in moves:
            mover = alice if alice.board.turn == alice.my_color else bob
            errors_before = len(texts(mover, "error"))
            mover.handle_input(move)
            self.assertEqual(texts(mover, "error")[errors_before:], [], f"move {move!r} was rejected")
            pump(alice, bob)
        self.assertEqual(alice.board.fen(), bob.board.fen())

    def last_error(self, session: GameSession) -> str:
        errors = texts(session, "error")
        self.assertTrue(errors, "expected an error message")
        return errors[-1]

    def local(self, **kwargs: Any) -> GameSession:
        options = dict(mode="local", my_name="White", opponent_name="Black", now_fn=self.now,
                       pgn_dir=os.path.join(self.tmp, "local"), autosave=False)
        options.update(kwargs)
        return GameSession(**options)


# ---------------------------------------------------------------------------------------------


class TimeControlTests(unittest.TestCase):
    def test_parse_valid(self) -> None:
        cases = {
            "5+3": (300000, 3000), "10": (600000, 0), "0.5+0": (30000, 0), " 3 + 2 ": (180000, 2000),
            "1|0": (60000, 0), "15+10": (900000, 10000), "2.5+0.5": (150000, 500), ".5": (30000, 0),
        }
        for text, (initial, increment) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_time_control(text), TimeControl(initial, increment))

    def test_parse_untimed(self) -> None:
        for text in ("none", "None", "0", "", "  ", "off", "0+0", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_time_control(text))

    def test_parse_invalid(self) -> None:
        for text in ("abc", "-1", "5+-3", "5+3+1", "5:3", "0+3", "1e3", "5+", "+3", "99999", "5+9999", "0.001"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_time_control(text)

    def test_str_and_dict_round_trip(self) -> None:
        self.assertEqual(str(TimeControl(300000, 3000)), "5+3")
        self.assertEqual(str(TimeControl(30000, 0)), "0.5+0")
        self.assertEqual(str(TimeControl(600000, 0)), "10+0")
        self.assertEqual(TimeControl(300000, 3000).pgn_tag(), "300+3")
        self.assertEqual(TimeControl(300000, 3000).describe(), "5 min + 3 s per move")
        tc = TimeControl(150000, 500)
        self.assertEqual(tc.to_dict(), {"initial_ms": 150000, "increment_ms": 500})
        self.assertEqual(TimeControl.from_dict(tc.to_dict()), tc)
        self.assertEqual(parse_time_control(str(tc)), tc)
        self.assertIsNone(TimeControl.from_dict(None))
        for bad in ({"initial_ms": 0, "increment_ms": 0}, {"initial_ms": "5"}, {"increment_ms": 1}, [1, 2],
                    {"initial_ms": 1000, "increment_ms": -1}, {"initial_ms": True, "increment_ms": 0}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    TimeControl.from_dict(bad)


class ChessClockTests(unittest.TestCase):
    def test_clock_starts_after_whites_first_move_with_increments(self) -> None:
        now = FakeTime()
        clock = ChessClock(TimeControl(60000, 2000), now)
        self.assertIsNone(clock.running)
        now.advance(5)
        self.assertEqual(clock.snapshot(), {WHITE: 60000, BLACK: 60000})
        self.assertIsNone(clock.flagged())
        self.assertEqual(clock.press(WHITE), 62000)
        self.assertEqual(clock.running, BLACK)
        now.advance(3.5)
        self.assertEqual(clock.remaining(BLACK), 56500)
        self.assertEqual(clock.remaining(WHITE), 62000)
        self.assertEqual(clock.press(BLACK), 58500)
        self.assertEqual(clock.running, WHITE)
        now.advance(10)
        self.assertEqual(clock.remaining(WHITE), 52000)
        self.assertEqual(clock.remaining(BLACK), 58500)

    def test_flag_and_negative_time(self) -> None:
        now = FakeTime()
        clock = ChessClock(TimeControl(1000, 0), now)
        clock.press(WHITE)
        now.advance(0.999)
        self.assertIsNone(clock.flagged())
        now.advance(0.501)
        self.assertEqual(clock.flagged(), BLACK)
        self.assertEqual(clock.remaining(BLACK), -500)
        clock.stop()
        self.assertIsNone(clock.running)
        self.assertIsNone(clock.flagged())
        now.advance(10)
        self.assertEqual(clock.remaining(BLACK), -500)

    def test_set_remaining_and_start(self) -> None:
        now = FakeTime()
        clock = ChessClock(TimeControl(60000, 0), now)
        clock.start(BLACK)
        now.advance(2)
        clock.set_remaining(BLACK, 12345)
        self.assertEqual(clock.remaining(BLACK), 12345)
        now.advance(1)
        self.assertEqual(clock.remaining(BLACK), 11345)
        clock.set_remaining(WHITE, 500)
        self.assertEqual(clock.remaining(WHITE), 500)
        clock.start(WHITE)
        now.advance(1)
        self.assertEqual(clock.remaining(BLACK), 11345)
        self.assertEqual(clock.flagged(), WHITE)


# ---------------------------------------------------------------------------------------------


class NetworkPlayTests(SessionTestCase):
    def test_fools_mate_checkmate_and_autosaved_pgn(self) -> None:
        alice, bob = self.make_pair(autosave=True)
        self.play(alice, bob, *FOOLS_MATE)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("checkmate", BLACK))
            self.assertIn("Checkmate — Black wins (0-1)", " ".join(texts(session, "game")))
            self.assertEqual(len(session.saved_paths), 1)
            path = session.saved_paths[0]
            self.assertRegex(os.path.basename(path), r"^\d{8}-\d{6}_Alice-vs-Bob\.pgn$")
            with open(path, encoding="utf-8") as handle:
                pgn = handle.read()
            self.assertIn('[White "Alice"]', pgn)
            self.assertIn('[Black "Bob"]', pgn)
            self.assertIn('[Result "0-1"]', pgn)
            self.assertIn("1. f3 e5 2. g4 Qh4# 0-1", pgn)
        self.assertIn("You won!", texts(bob, "game")[-1])
        self.assertIn("You lost.", texts(alice, "game")[-1])
        self.assertEqual(alice.view_state().game_over, "Checkmate — Black wins")
        self.assertIsNone(alice.view_state().clock_running)

    def test_move_message_format(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 2000))
        alice.handle_input("e4")
        self.assertEqual(alice.conn.sent[-1], {"type": "move", "uci": "e2e4", "ply": 1, "clock_ms": 62000})
        pump(alice, bob)
        self.assertEqual(bob.board.san_stack, ["e4"])
        self.assertEqual(bob.clock.running, BLACK)
        self.assertEqual(bob.clock.remaining(WHITE), 62000)

    def test_remote_clock_is_adopted(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 0))
        self.play(alice, bob, "e4")
        alice.handle_message({"type": "move", "uci": "e7e5", "ply": 2, "clock_ms": 12345})
        self.assertIsNone(alice.over)
        self.assertEqual(alice.clock.remaining(BLACK), 12345)
        self.assertEqual(alice.clock.running, WHITE)

    def test_chat_delivery(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_input("/c hello there")
        bob.handle_input("/chat  good   luck ")
        alice.handle_input("/say gg")
        pump(alice, bob)
        self.assertIn(("chat_them", "Alice: hello there"), bob.log)
        self.assertIn(("chat_them", "Alice: gg"), bob.log)
        self.assertIn(("chat_me", "Alice: hello there"), alice.log)
        self.assertIn(("chat_them", "Bob: good luck"), alice.log)
        bob.handle_message({"type": "chat", "text": "x" * 900 + "\nfake line"})
        self.assertEqual(len(texts(bob, "chat_them")[-1]), len("Alice: ") + game.CHAT_MAX_LEN)
        bob.handle_message({"type": "chat", "text": 42})
        self.assertEqual(len(texts(bob, "chat_them")), 3)

    def test_moves_rejected_when_not_your_turn_or_game_over(self) -> None:
        alice, bob = self.make_pair()
        bob.handle_input("e5")
        self.assertIn("not your turn", self.last_error(bob))
        self.assertEqual(bob.board.move_stack, [])
        self.assertEqual(bob.conn.sent, [])
        bob.handle_input("/resign")
        pump(alice, bob)
        alice.handle_input("e4")
        self.assertIn("The game is over", self.last_error(alice))
        self.assertEqual(alice.board.move_stack, [])

    def test_resign(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        bob.handle_input("/resign")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("resignation", WHITE))
        self.assertIn("Bob resigned.", texts(alice, "game"))
        self.assertIn("White wins by resignation (1-0). You won!", texts(alice, "game"))
        bob.handle_input("/resign")
        self.assertIn("already over", self.last_error(bob))

    def test_status_and_pending_text(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(300000, 3000))
        self.assertTrue(alice.view_state().status.startswith("Your move"))
        self.assertIn("clocks start", alice.view_state().status)
        self.assertEqual(bob.view_state().status, "Waiting for Alice to move…")
        self.assertEqual(alice.view_state().connection, "Connected to 10.0.0.2:40000 · 5+3")
        self.assertEqual(alice.view_state().my_color, WHITE)
        self.assertEqual(bob.view_state().perspective, BLACK)
        self.assertEqual(bob.view_state().white_name, "Alice")
        self.assertIn("Your move.", texts(alice, "game"))
        self.assertIn("Alice moves first.", texts(bob, "game"))
        self.play(alice, bob, "e4", "f5", "Qh5+")
        self.assertEqual(bob.view_state().status, "Your move — you are in check!")
        self.assertEqual(bob.view_state().clock_running, BLACK)

    def test_ascii_session_text(self) -> None:
        conn_a, _conn_b = fake_pair()
        alice = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob", conn=conn_a,
                            autosave=False, unicode=False, now_fn=self.now)
        self.assertEqual(alice.view_state().status, "Your move")
        alice.handle_input("e5")
        self.assertTrue(all(ch.isascii() for ch in self.last_error(alice)))


class OfferTests(SessionTestCase):
    def test_draw_offer_accept(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/draw")
        pump(alice, bob)
        self.assertIn("draw", bob.incoming)
        self.assertEqual(bob.view_state().pending, "Alice offers a draw — /accept or /decline")
        self.assertEqual(alice.view_state().pending, "Draw offered — waiting for Bob")
        bob.handle_input("/accept")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("agreement"))
            self.assertEqual(session.view_state().pending, "")
        self.assertIn("Bob accepted your draw offer.", texts(alice, "game"))

    def test_draw_command_accepts_pending_offer(self) -> None:
        alice, bob = self.make_pair()
        bob.handle_input("/draw")
        pump(alice, bob)
        alice.handle_input("/draw")
        pump(alice, bob)
        self.assertEqual(alice.over, Outcome("agreement"))
        self.assertEqual(bob.over, Outcome("agreement"))

    def test_draw_offer_decline(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/draw")
        alice.handle_input("/draw")
        self.assertIn("already offered", self.last_error(alice))
        pump(alice, bob)
        bob.handle_input("/decline")
        pump(alice, bob)
        self.assertIsNone(alice.over)
        self.assertIsNone(bob.over)
        self.assertEqual(alice.outgoing, {})
        self.assertEqual(bob.incoming, {})
        self.assertIn("Bob declined your draw offer.", texts(alice, "game"))
        bob.handle_input("/decline")
        self.assertIn("no offer", self.last_error(bob))

    def test_crossing_draw_offers_agree(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_input("/draw")
        bob.handle_input("/draw")
        pump(alice, bob)
        self.assertEqual(alice.over, Outcome("agreement"))
        self.assertEqual(bob.over, Outcome("agreement"))

    def test_offers_cleared_by_a_move(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/draw")
        pump(alice, bob)
        self.assertIn("draw", bob.incoming)
        self.play(alice, bob, "e5")
        for session in (alice, bob):
            self.assertEqual(session.incoming, {})
            self.assertEqual(session.outgoing, {})
            self.assertEqual(session.view_state().pending, "")
        bob.handle_input("/accept")
        self.assertIn("no offer to accept", self.last_error(bob))
        # A late answer to the cleared offer changes nothing.
        alice.handle_message({"type": "draw_accept"})
        self.assertIsNone(alice.over)

    def test_draw_accepted_just_before_our_move_arrived(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_input("/draw")       # offered at ply 0 ...
        alice.handle_input("e4")          # ... then moved before the answer came
        bob.handle_message(bob.conn.inbox.get_nowait())  # only the offer reaches Bob
        bob.handle_input("/accept")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("agreement"))
            self.assertEqual(session.board.move_stack, [])

    def test_draw_offer_crossing_a_move_is_dropped_on_both_sides(self) -> None:
        alice, bob = self.make_pair()
        bob.handle_input("/draw")   # Bob offers on Alice's turn ...
        alice.handle_input("e4")    # ... while Alice's move is already on its way
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.incoming, {})
            self.assertEqual(session.outgoing, {})
        alice.handle_input("/accept")
        self.assertIn("no offer to accept", self.last_error(alice))
        pump(alice, bob)
        self.assertIsNone(alice.over)
        self.assertIsNone(bob.over)
        self.play(alice, bob, "e5")

    def test_draw_offer_crossing_a_takeback_accept_is_dropped(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/takeback")
        pump(alice, bob)
        bob.handle_input("/accept")   # the accept is on its way to Alice ...
        alice.handle_input("/draw")   # ... when she offers a draw at the old ply
        pump(alice, bob)
        self.assertEqual(bob.incoming, {})
        self.assertEqual(alice.outgoing, {})
        bob.handle_input("/accept")
        pump(alice, bob)
        self.assertIsNone(alice.over)
        self.assertIsNone(bob.over)
        self.assertEqual(alice.board.fen(), bob.board.fen())

    def test_stale_draw_accept_is_ignored(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/draw")
        pump(alice, bob)
        alice.handle_message({"type": "draw_accept", "ply": 0})
        self.assertIsNone(alice.over)
        alice.handle_message({"type": "draw_accept", "ply": 1})
        self.assertEqual(alice.over, Outcome("agreement"))

    def test_crossing_takeback_requests_cancel_each_other(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4", "e5")
        alice.handle_input("/takeback")
        bob.handle_input("/takeback")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.incoming, {})
            self.assertEqual(session.outgoing, {})
            self.assertIn("at the same time", texts(session, "system")[-1])
        alice.handle_input("/accept")
        bob.handle_input("/accept")
        pump(alice, bob)
        self.assertEqual(alice.board.san_stack, ["e4", "e5"])
        self.assertEqual(bob.board.san_stack, ["e4", "e5"])
        bob.handle_input("/takeback")
        pump(alice, bob)
        alice.handle_input("/takeback")
        self.assertIn("asked for a takeback first", self.last_error(alice))
        alice.handle_input("/accept")
        pump(alice, bob)
        self.assertEqual(alice.board.san_stack, ["e4"])
        self.assertEqual(bob.board.san_stack, ["e4"])

    def test_takeback_when_requester_is_to_move_undoes_two_plies(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4", "e5")
        alice.handle_input("/takeback")
        self.assertEqual(alice.conn.sent[-1], {"type": "takeback_request", "ply": 2})
        pump(alice, bob)
        self.assertIn("takeback", bob.incoming)
        self.assertIn("asks to take back their last move (and your reply)", texts(bob, "game")[-1])
        alice.handle_input("Nf3")
        self.assertIn("answer your takeback", self.last_error(alice))
        bob.handle_input("/accept")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.board.move_stack, [])
            self.assertEqual(session.board.fen(), STARTING_FEN)
            self.assertEqual(session.incoming, {})
            self.assertEqual(session.outgoing, {})
        self.play(alice, bob, "d4")

    def test_takeback_when_requester_is_not_to_move_undoes_one_ply(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4", "e5")
        bob.handle_input("/undo")
        pump(alice, bob)
        alice.handle_input("/accept")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.board.san_stack, ["e4"])
            self.assertEqual(session.board.turn, BLACK)
        self.play(alice, bob, "c5")

    def test_takeback_after_own_move_white(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 0))
        self.play(alice, bob, "e4")
        alice.handle_input("/takeback")
        pump(alice, bob)
        bob.handle_input("/accept")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.board.move_stack, [])
            self.assertIsNone(session.clock.running)

    def test_takeback_decline(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/takeback")
        alice.handle_input("/takeback")
        self.assertIn("already asked", self.last_error(alice))
        pump(alice, bob)
        bob.handle_input("/decline")
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.board.san_stack, ["e4"])
        self.assertEqual(alice.outgoing, {})
        self.assertIn("Bob declined your takeback request.", texts(alice, "game"))
        self.play(alice, bob, "e5")

    def test_takeback_without_a_move(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_input("/takeback")
        self.assertIn("no move to take back", self.last_error(alice))
        bob.handle_input("/takeback")
        self.assertIn("no move to take back", self.last_error(bob))
        self.assertEqual(alice.conn.sent, [])

    def test_opponent_moves_instead_of_answering_takeback(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/takeback")
        pump(alice, bob)
        self.play(alice, bob, "e5")
        for session in (alice, bob):
            self.assertEqual(session.board.san_stack, ["e4", "e5"])
            self.assertIsNone(session.over)
            self.assertEqual(session.outgoing, {})

    def test_outdated_takeback_request_is_declined(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4", "e5")
        bob.handle_message({"type": "takeback_request", "ply": 1})
        self.assertEqual(bob.conn.sent[-1], {"type": "takeback_decline", "ply": 1})
        self.assertEqual(bob.incoming, {})


class TimeoutTests(SessionTestCase):
    def test_timeout_reported_by_flagging_side_only(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 0))
        self.play(alice, bob, "e4")
        self.now.advance(61)
        alice.tick()
        self.assertIsNone(alice.over, "only the side whose own clock ran out reports it")
        self.assertEqual(alice.view_state().clock_ms[BLACK], -1000)
        bob.tick()
        self.assertEqual(bob.conn.sent[-1], {"type": "timeout", "loser": BLACK})
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("timeout", WHITE))
            self.assertEqual(session.clock.remaining(BLACK), 0)
        self.assertEqual(alice.over.describe(), "White wins on time")
        self.assertIn("Your time ran out.", texts(bob, "game"))
        self.assertIn("Bob ran out of time.", texts(alice, "game"))

    def test_timeout_against_insufficient_material_is_a_draw(self) -> None:
        fen = "3qk3/8/8/8/8/8/8/1N2K3 w - - 0 1"
        alice, bob = self.make_pair(tc=TimeControl(60000, 0), fen=fen)
        self.play(alice, bob, "Nc3")
        self.now.advance(60)
        bob.tick()
        pump(alice, bob)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("timeout_insufficient"))
            self.assertEqual(session.over.result(), "1/2-1/2")
        self.assertEqual(alice.view_state().game_over, "Draw — timeout vs insufficient material")

    def test_move_after_own_flag_is_rejected_and_loses(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 0))
        self.play(alice, bob, "e4", "e5")
        self.now.advance(61)
        alice.handle_input("Nf3")
        self.assertEqual(alice.board.san_stack, ["e4", "e5"])
        self.assertEqual(alice.over, Outcome("timeout", BLACK))
        pump(alice, bob)
        self.assertEqual(bob.over, Outcome("timeout", BLACK))

    def test_invalid_timeout_message_is_ignored(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 0))
        alice.handle_message({"type": "timeout", "loser": WHITE})
        self.assertIsNone(alice.over)
        untimed_a, _untimed_b = self.make_pair()
        untimed_a.handle_message({"type": "timeout", "loser": BLACK})
        self.assertIsNone(untimed_a.over)


class DesyncAndDisconnectTests(SessionTestCase):
    def assert_desync(self, alice: GameSession, bob: GameSession, receiver: GameSession, msg: Dict[str, Any]) -> None:
        receiver.handle_message(msg)
        self.assertEqual(receiver.over, Outcome("abandoned"))
        self.assertEqual(receiver.conn.sent[-1]["type"], "error")
        self.assertIn("Out of sync", self.last_error(receiver))
        pump(alice, bob)
        other = alice if receiver is bob else bob
        self.assertEqual(other.over, Outcome("abandoned"))
        self.assertIn("reported a problem", self.last_error(other))

    def test_wrong_ply(self) -> None:
        alice, bob = self.make_pair()
        self.assert_desync(alice, bob, bob, {"type": "move", "uci": "e2e4", "ply": 3, "clock_ms": None})

    def test_illegal_move(self) -> None:
        alice, bob = self.make_pair()
        self.assert_desync(alice, bob, bob, {"type": "move", "uci": "e2e5", "ply": 1, "clock_ms": None})

    def test_unreadable_move(self) -> None:
        alice, bob = self.make_pair()
        self.assert_desync(alice, bob, bob, {"type": "move", "uci": "zz", "ply": 1})

    def test_move_when_not_their_turn(self) -> None:
        alice, bob = self.make_pair()
        self.assert_desync(alice, bob, alice, {"type": "move", "uci": "e7e5", "ply": 1})

    def test_rematch_after_desync_resyncs(self) -> None:
        alice, bob = self.make_pair()
        self.assert_desync(alice, bob, bob, {"type": "move", "uci": "e2e5", "ply": 1})
        alice.handle_input("/rematch")
        pump(alice, bob)
        bob.handle_input("/accept")
        pump(alice, bob)
        self.play(alice, bob, "e4", "e5")
        self.assertIsNone(alice.over)

    def test_disconnect_while_playing(self) -> None:
        alice, bob = self.make_pair(autosave=True)
        self.play(alice, bob, "e4")
        alice.conn.close()
        pump(bob)
        self.assertEqual(bob.over, Outcome("abandoned"))
        self.assertFalse(bob.connected)
        self.assertIn("Connection to Alice lost", self.last_error(bob))
        self.assertEqual(bob.view_state().connection, "Disconnected")
        self.assertEqual(bob.view_state().game_over, "Game abandoned")
        self.assertEqual(len(bob.saved_paths), 1)
        bob.handle_input("/rematch")
        self.assertIn("rematch is not possible", self.last_error(bob))
        bob.handle_input("/quit")
        self.assertTrue(bob.quit_requested)

    def test_bye_while_playing(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        game._close_connection(alice, alice.conn)
        pump(bob)
        self.assertEqual(bob.over, Outcome("abandoned"))
        self.assertIn("Alice left the game.", texts(bob, "error"))
        self.assertEqual(alice.conn.sent[-1], {"type": "bye"})

    def test_bye_after_game_over_keeps_result(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, *FOOLS_MATE)
        game._close_connection(bob, bob.conn)
        pump(alice)
        self.assertEqual(alice.over, Outcome("checkmate", BLACK))
        self.assertIn("Bob left.", texts(alice, "system"))

    def test_send_failure_abandons(self) -> None:
        alice, bob = self.make_pair()
        alice.conn.closed = True
        alice.handle_input("e4")
        self.assertEqual(alice.over, Outcome("abandoned"))
        self.assertFalse(alice.connected)

    def test_protocol_error_and_unknown_messages_are_harmless(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_message({"type": "_protocol_error", "reason": "invalid JSON"})
        alice.handle_message({"type": "future_feature", "x": 1})
        alice.handle_message("not a dict")
        alice.handle_message({"type": 5})
        self.assertIsNone(alice.over)
        self.assertIn("malformed", texts(alice, "system")[-1])


class RematchAndQuitTests(SessionTestCase):
    def test_rematch_swaps_colours_and_resets(self) -> None:
        alice, bob = self.make_pair(tc=TimeControl(60000, 1000))
        self.play(alice, bob, "e4", "e5")
        self.now.advance(5)
        alice.handle_input("/rematch")
        self.assertIn("still in progress", self.last_error(alice))
        bob.handle_input("/resign")
        pump(alice, bob)
        alice.handle_input("/rematch")
        pump(alice, bob)
        self.assertIn("rematch", bob.incoming)
        self.assertEqual(bob.view_state().pending, "Alice wants a rematch — /accept or /decline")
        bob.handle_input("/accept")
        pump(alice, bob)
        self.assertEqual(alice.my_color, BLACK)
        self.assertEqual(bob.my_color, WHITE)
        for session in (alice, bob):
            self.assertIsNone(session.over)
            self.assertEqual(session.board.fen(), STARTING_FEN)
            self.assertEqual(session.game_number, 2)
            self.assertIsNone(session.clock.running)
            self.assertEqual(session.clock.snapshot(), {WHITE: 60000, BLACK: 60000})
            self.assertEqual(session.white_name, "Bob")
            self.assertEqual(session.black_name, "Alice")
        self.assertEqual(alice.perspective, BLACK)
        self.assertIn("Rematch! You play Black", texts(alice, "game")[-1])
        alice.handle_input("e4")
        self.assertIn("not your turn", self.last_error(alice))
        self.play(alice, bob, "e4", "c5")
        self.assertIn('[Round "2"]', alice.pgn())
        self.assertIn('[White "Bob"]', alice.pgn())

    def test_rematch_decline_and_crossing_offers(self) -> None:
        alice, bob = self.make_pair()
        alice.handle_input("/resign")
        pump(alice, bob)
        alice.handle_input("/rematch")
        pump(alice, bob)
        bob.handle_input("/decline")
        pump(alice, bob)
        self.assertEqual(alice.my_color, WHITE)
        self.assertIn("Bob declined the rematch.", texts(alice, "game"))
        alice.handle_input("/rematch")
        bob.handle_input("/rematch")
        pump(alice, bob)
        self.assertEqual((alice.my_color, bob.my_color), (BLACK, WHITE))
        self.assertEqual(alice.game_number, 2)
        self.assertEqual(bob.game_number, 2)

    def test_quit_confirmation_during_live_game(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, "e4")
        alice.handle_input("/quit")
        self.assertFalse(alice.quit_requested)
        self.assertIn("again within 10 seconds", texts(alice, "system")[-1])
        self.assertEqual(alice.view_state().pending, "/quit or Ctrl-C again within 10 s resigns and leaves")
        self.now.advance(11)
        alice.tick()
        self.assertEqual(alice.view_state().pending, "")
        alice.handle_input("/exit")
        self.assertFalse(alice.quit_requested, "the confirmation window expired")
        self.now.advance(3)
        alice.handle_input("/q")
        self.assertTrue(alice.quit_requested)
        self.assertEqual(alice.over, Outcome("resignation", BLACK))
        pump(alice, bob)
        self.assertEqual(bob.over, Outcome("resignation", BLACK))

    def test_interrupt_behaves_like_quit(self) -> None:
        alice, bob = self.make_pair()
        alice.request_quit()
        self.assertFalse(alice.quit_requested)
        alice.request_quit()
        self.assertTrue(alice.quit_requested)

    def test_quit_after_game_over_is_immediate(self) -> None:
        alice, bob = self.make_pair()
        self.play(alice, bob, *FOOLS_MATE)
        alice.handle_input("/quit")
        self.assertTrue(alice.quit_requested)

    def test_summary_lines_after_leaving(self) -> None:
        alice, bob = self.make_pair(autosave=True)
        self.assertEqual(alice.summary_lines(), [])
        self.play(alice, bob, *FOOLS_MATE)
        self.assertEqual(alice.summary_lines()[0], "Result: Checkmate — Black wins (0-1). You lost.")
        self.assertEqual(bob.summary_lines()[0], "Result: Checkmate — Black wins (0-1). You won!")
        self.assertEqual(alice.summary_lines()[1], "Game saved to " + alice.saved_paths[0])
        unfinished, _other = self.make_pair()
        self.play(unfinished, _other, "e4")
        self.assertEqual(unfinished.summary_lines(), ["The game was left unfinished."])

    def test_start_position_already_decided_ends_at_once(self) -> None:
        alice, bob = self.make_pair(fen="4k3/8/8/8/8/8/8/4K3 w - - 0 1", autosave=True)
        for session in (alice, bob):
            self.assertEqual(session.over, Outcome("insufficient_material"))
            self.assertEqual(session.saved_paths, [], "a game without moves is not saved")
        alice.handle_input("Kd2")
        self.assertIn("The game is over", self.last_error(alice))
        self.assertEqual(alice.conn.sent, [])
        alice.handle_input("/rematch")
        pump(alice, bob)
        bob.handle_input("/accept")
        pump(alice, bob)
        self.assertEqual(alice.board.fen(), STARTING_FEN)
        self.assertIsNone(alice.over)
        stalemate = self.local(fen="7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertEqual(stalemate.over, Outcome("stalemate"))
        self.assertEqual(stalemate.view_state().game_over, "Draw by stalemate")


class LocalModeTests(SessionTestCase):
    def test_perspective_follows_side_to_move_and_takeback(self) -> None:
        s = self.local()
        self.assertIsNone(s.view_state().my_color)
        self.assertEqual(s.perspective, WHITE)
        self.assertEqual(s.view_state().prompt, "White> ")
        self.assertEqual(s.view_state().status, "White to move")
        s.handle_input("e4")
        self.assertEqual(s.perspective, BLACK)
        self.assertEqual(s.view_state().prompt, "Black> ")
        s.handle_input("/takeback")
        self.assertEqual(s.board.move_stack, [])
        self.assertEqual(s.perspective, WHITE)
        s.handle_input("/takeback")
        self.assertIn("no move to take back", self.last_error(s))

    def test_no_flip_and_manual_flip(self) -> None:
        s = self.local(flip=False)
        s.handle_input("e4")
        self.assertEqual(s.perspective, WHITE)
        s.handle_input("/flip")
        self.assertEqual(s.perspective, BLACK)
        auto = self.local()
        auto.handle_input("e4")
        auto.handle_input("/flip")
        self.assertEqual(auto.perspective, WHITE)
        auto.handle_input("e5")
        self.assertEqual(auto.perspective, WHITE, "a manual flip stops the automatic turning")

    def test_draw_and_resign(self) -> None:
        s = self.local()
        s.handle_input("e4")
        s.handle_input("/draw")
        self.assertEqual(s.over, Outcome("agreement"))
        r = self.local()
        r.handle_input("e4")
        r.handle_input("/resign")
        self.assertEqual(r.over, Outcome("resignation", WHITE))
        self.assertIn("Black resigns.", texts(r, "game"))
        r.handle_input("/rematch")
        self.assertIsNone(r.over)
        self.assertEqual(r.game_number, 2)
        self.assertEqual(r.board.fen(), STARTING_FEN)

    def test_local_checkmate_autosaves(self) -> None:
        s = self.local(autosave=True)
        for move in FOOLS_MATE:
            s.handle_input(move)
        self.assertEqual(s.over, Outcome("checkmate", BLACK))
        self.assertEqual(len(s.saved_paths), 1)
        self.assertRegex(os.path.basename(s.saved_paths[0]), r"_White-vs-Black\.pgn$")

    def test_local_timeout_and_offers(self) -> None:
        s = self.local(time_control=TimeControl(60000, 0))
        s.handle_input("e4")
        self.now.advance(61)
        s.tick()
        self.assertEqual(s.over, Outcome("timeout", WHITE))
        t = self.local()
        t.handle_input("/accept")
        self.assertIn("no offers", self.last_error(t))
        t.handle_input("/c hi")
        self.assertIn("network games", self.last_error(t))
        t.handle_input("/quit")
        self.assertTrue(t.quit_requested)


class CommandTests(SessionTestCase):
    def test_parsing_and_errors(self) -> None:
        alice, _bob = self.make_pair()
        alice.handle_input("/foo bar")
        self.assertEqual(self.last_error(alice), "Unknown command: /foo. Type /help for the list of commands.")
        alice.handle_input("/")
        self.assertIn("after the slash", self.last_error(alice))
        alice.handle_input("/c")
        self.assertIn("Type a message after /c", self.last_error(alice))
        alice.handle_input("/c    ")
        self.assertIn("Type a message after /c", self.last_error(alice))
        alice.handle_input("hello there")
        self.assertEqual(self.last_error(alice), NOT_A_MOVE)
        alice.handle_input("resign")
        self.assertIn("Did you mean /resign?", self.last_error(alice))
        alice.handle_input("Ke2")
        self.assertEqual(self.last_error(alice), "Illegal move: Ke2. Type /moves to list the legal moves.")
        alice.handle_input("   ")
        self.assertEqual(alice.conn.sent, [])

    def test_ambiguous_move_message(self) -> None:
        s = self.local(fen="4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
        s.handle_input("Nd2")
        self.assertEqual(self.last_error(s), "Ambiguous move: Nd2 (could be Nbd2, Nfd2)")
        s.handle_input("Nbd2")
        self.assertEqual(s.board.san_stack, ["Nbd2"])

    def test_info_commands(self) -> None:
        alice, _bob = self.make_pair()
        alice.handle_input("?")
        self.assertIn(ui.help_lines()[0], texts(alice, "info"))
        alice.handle_input("/HELP")
        self.assertEqual(texts(alice, "info").count(ui.help_lines()[0]), 2)
        alice.handle_input("/moves")
        self.assertTrue(texts(alice, "info")[-1].startswith("Legal moves for White (20): "))
        alice.handle_input("/fen")
        self.assertEqual(texts(alice, "info")[-1], "FEN: " + STARTING_FEN)
        alice.handle_input("/pgn")
        self.assertIn('[White "Alice"]', texts(alice, "info")[-1])
        target = os.path.join(self.tmp, "saved", "game.pgn")
        alice.handle_input("/save " + target)
        self.assertTrue(os.path.isfile(target))
        self.assertIn("Game saved to", texts(alice, "system")[-1])
        alice.handle_input("/save")
        self.assertTrue(os.path.isfile(alice.saved_paths[-1]))
        self.assertEqual(os.path.dirname(alice.saved_paths[-1]), os.path.join(self.tmp, "alice"))
        alice.handle_input("/flip")
        self.assertEqual(alice.perspective, BLACK)
        alice.handle_input("/clear")
        self.assertEqual(alice.log, [])

    def test_save_failure_is_reported(self) -> None:
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as handle:
            handle.write("x")
        s = self.local(pgn_dir=os.path.join(blocker, "sub"))
        s.handle_input("/save")
        self.assertIn("Could not save the game", self.last_error(s))


# ---------------------------------------------------------------------------------------------


class LoopbackTests(SessionTestCase):
    """Two sessions over a real TCP connection on 127.0.0.1."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self.assert_no_leaked_threads)

    def assert_no_leaked_threads(self) -> None:
        deadline = time.monotonic() + WAIT
        while True:
            leftovers = [t.name for t in threading.enumerate() if t.name.startswith("lanchess-") and t.name != "lanchess-stdin"]
            if not leftovers or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        self.assertEqual(leftovers, [])

    def connect_pair(self, tc: Optional[TimeControl] = None) -> Tuple[GameSession, GameSession]:
        server = net.Server(port=0, bind="127.0.0.1")
        self.addCleanup(server.close)
        client = net.connect("127.0.0.1", server.port, timeout=WAIT)
        self.addCleanup(client.close)
        host = server.accept(WAIT)
        self.assertIsNotNone(host)
        self.addCleanup(host.close)
        result: Dict[str, Any] = {}

        def host_side() -> None:
            result["host"] = net.server_handshake(host, "Alice", "white", tc, timeout=WAIT)

        thread = threading.Thread(target=host_side, daemon=True)
        thread.start()
        welcome = net.client_handshake(client, "Bob", timeout=WAIT)
        thread.join(WAIT)
        opponent, host_color = result["host"]
        self.assertEqual((opponent, host_color, welcome["your_color"]), ("Bob", WHITE, BLACK))
        alice = GameSession(mode="network", my_color=host_color, my_name="Alice", opponent_name=opponent,
                            conn=host, time_control=tc, pgn_dir=os.path.join(self.tmp, "a"))
        bob = GameSession(mode="network", my_color=welcome["your_color"], my_name="Bob",
                          opponent_name=welcome["name"], conn=client,
                          time_control=TimeControl.from_dict(welcome["time_control"]),
                          fen=welcome["fen"], pgn_dir=os.path.join(self.tmp, "b"))
        return alice, bob

    def wait_until(self, sessions: Tuple[GameSession, ...], condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + WAIT
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached over loopback")
            for session in sessions:
                game._drain_inbox(session, session.conn)
            time.sleep(0.005)

    def test_fools_mate_over_real_connection(self) -> None:
        alice, bob = self.connect_pair(TimeControl(60000, 2000))
        self.assertTrue(alice.peer.startswith("127.0.0.1:"))
        pair = (alice, bob)
        for index, move in enumerate(FOOLS_MATE, 1):
            mover = alice if alice.board.turn == alice.my_color else bob
            mover.handle_input(move)
            self.wait_until(pair, lambda n=index: len(alice.board.move_stack) == n == len(bob.board.move_stack))
        for session in pair:
            self.assertEqual(session.over, Outcome("checkmate", BLACK))
            self.assertEqual(len(session.saved_paths), 1)
            with open(session.saved_paths[0], encoding="utf-8") as handle:
                self.assertIn("1. f3 e5 2. g4 Qh4# 0-1", handle.read())
        self.assertEqual(bob.clock.remaining(WHITE), alice.clock.remaining(WHITE))
        self.assertEqual(bob.clock.remaining(BLACK), alice.clock.remaining(BLACK))
        alice.handle_input("/c good game")
        self.wait_until(pair, lambda: ("chat_them", "Alice: good game") in bob.log)
        game._close_connection(alice, alice.conn)
        self.wait_until(pair, lambda: not bob.connected)
        self.assertIn("Alice left.", texts(bob, "system"))
        game._close_connection(bob, bob.conn)


# ---------------------------------------------------------------------------------------------


class ScriptedReader:
    """Fake KeyReader: returns scripted keys, then None."""

    def __init__(self, keys: List[Optional[str]], fail_after: Optional[int] = None) -> None:
        self.keys = list(keys)
        self.eof = False
        self.started = False
        self.restored = False
        self.fail_after = fail_after
        self.reads = 0

    def start(self) -> None:
        self.started = True

    def restore(self) -> None:
        self.restored = True

    def read_key(self, timeout: float) -> Optional[str]:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            raise RuntimeError("boom")
        if self.keys:
            return self.keys.pop(0)  # None in the script is a pause (nothing typed)
        return "CTRL_C"  # script exhausted: ask to quit so a broken test cannot hang


class FakeScreen:
    def __init__(self) -> None:
        self.frames: List[Tuple[str, Tuple[int, int]]] = []
        self.entered = 0
        self.exited = 0

    def enter(self) -> None:
        self.entered += 1

    def exit(self) -> None:
        self.exited += 1

    def draw(self, frame: str, cursor: Tuple[int, int], size: Optional[Tuple[int, int]] = None) -> None:
        self.frames.append((frame, cursor))


def typed(*lines: str) -> List[Optional[str]]:
    """Keys for each line plus ENTER, with a pause after each line."""
    keys: List[Optional[str]] = []
    for line in lines:
        keys.extend(line)
        keys.extend(["ENTER", None])
    return keys


class RunLoopTests(SessionTestCase):
    def test_fullscreen_loop_plays_moves_and_restores_terminal(self) -> None:
        session = self.local()
        reader = ScriptedReader(typed("e4", "e5") + ["PGUP", None, "PGDN"] + list("Nf") + ["BACKSPACE", None, "CTRL_U"]
                                + typed("/quit"))
        screen = FakeScreen()
        game.run_interactive(session, None, ui.Theme(unicode=True, color=False), key_reader=reader,
                             screen=screen, size_fn=lambda: (100, 30), poll_interval=0.001)
        self.assertEqual(session.board.san_stack, ["e4", "e5"])
        self.assertTrue(session.quit_requested)
        self.assertTrue(reader.started and reader.restored)
        self.assertEqual((screen.entered, screen.exited), (1, 1))
        self.assertTrue(screen.frames)
        frame, (row, col) = screen.frames[-1]
        lines = frame.split("\n")
        self.assertEqual(len(lines), 30)
        self.assertIn("1. e4", frame)
        self.assertEqual(row, 29)
        self.assertNotIn("\x1b", frame)

    def test_fullscreen_restores_terminal_on_error(self) -> None:
        conn_a, conn_b = fake_pair()
        session = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob",
                              conn=conn_a, autosave=False, now_fn=self.now)
        reader = ScriptedReader(typed("e4"), fail_after=3)
        screen = FakeScreen()
        with self.assertRaises(RuntimeError):
            game.run_interactive(session, conn_a, ui.Theme(), key_reader=reader, screen=screen,
                                 size_fn=lambda: (80, 24), poll_interval=0.001)
        self.assertTrue(reader.restored)
        self.assertEqual(screen.exited, 1)
        self.assertTrue(conn_a.closed)
        self.assertEqual(conn_a.sent[-1], {"type": "bye"})

    def test_fullscreen_ctrl_c_twice_resigns_in_network_game(self) -> None:
        conn_a, conn_b = fake_pair()
        session = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob",
                              conn=conn_a, autosave=False, now_fn=self.now)
        reader = ScriptedReader(["CTRL_C", "CTRL_C"])
        game.run_interactive(session, conn_a, ui.Theme(), key_reader=reader, screen=FakeScreen(),
                             size_fn=lambda: (80, 24), poll_interval=0.001)
        self.assertEqual(session.over, Outcome("resignation", BLACK))
        self.assertEqual([m["type"] for m in conn_a.sent], ["resign", "bye"])

    def test_falls_back_to_plain_without_terminal(self) -> None:
        class NoTTY(ScriptedReader):
            def start(self) -> None:
                raise OSError("stdin is not a terminal")

        session = self.local()
        out = io.StringIO()
        game.run_interactive(session, None, ui.Theme(), key_reader=NoTTY([]),
                             input_source=io.StringIO("e4\n/quit\n"), output=out, poll_interval=0.01)
        self.assertEqual(session.board.san_stack, ["e4"])
        self.assertIn("Last move: 1.e4", out.getvalue())

    def test_plain_mode_prints_board_log_and_result(self) -> None:
        session = self.local(autosave=True)
        out = io.StringIO()
        game.run_interactive(session, None, ui.Theme(unicode=False, color=False), plain=True,
                             input_source=io.StringIO("f3\nxyz\ne5\ng4\nQh4#\n/quit\n"), output=out,
                             poll_interval=0.01)
        text = out.getvalue()
        self.assertIn("Type a move like e4", text)
        self.assertIn("! " + NOT_A_MOVE, text)
        self.assertIn("Last move: 2...Qh4#", text)
        self.assertIn("* Checkmate - Black wins (0-1)", text)
        self.assertIn("-- White to move --", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(len(session.saved_paths), 1)

    def test_plain_mode_ctrl_c_acts_like_quit(self) -> None:
        class InterruptingSource:
            """Scripted plain input; "SIGINT" sends a real Ctrl-C signal to this process."""

            def __init__(self, script: List[Any]) -> None:
                self.script = list(script)

            def poll(self, timeout: float) -> Optional[tuple]:
                if not self.script:
                    return ("eof",)
                step = self.script.pop(0)
                if step == "SIGINT":
                    signal.raise_signal(signal.SIGINT)
                    return None
                return step

        before = signal.getsignal(signal.SIGINT)
        conn_a, _conn_b = fake_pair()
        session = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob",
                              conn=conn_a, autosave=False, now_fn=self.now)
        out = io.StringIO()
        script: List[Any] = [("line", "e4"), "SIGINT", None]
        game.run_interactive(session, conn_a, ui.Theme(), plain=True, output=out, poll_interval=0.01,
                             input_source=InterruptingSource(script + ["SIGINT"]))
        self.assertEqual(signal.getsignal(signal.SIGINT), before, "the SIGINT handler is restored")
        self.assertIn("again within 10 seconds to resign", out.getvalue())
        self.assertEqual(session.over, Outcome("resignation", BLACK))
        self.assertEqual([m["type"] for m in conn_a.sent], ["move", "resign", "bye"])

    def test_plain_mode_eof_leaves(self) -> None:
        conn_a, conn_b = fake_pair()
        session = GameSession(mode="network", my_color=WHITE, my_name="Alice", opponent_name="Bob",
                              conn=conn_a, autosave=False, time_control=TimeControl(60000, 0), now_fn=self.now)
        out = io.StringIO()
        game.run_interactive(session, conn_a, ui.Theme(), plain=True, input_source=io.StringIO("e4\n"),
                             output=out, poll_interval=0.01)
        self.assertTrue(session.quit_requested)
        self.assertEqual([m["type"] for m in conn_a.sent], ["move", "bye"])
        self.assertIn("Clocks: Alice 1:00  |  Bob 1:00 (running)", out.getvalue())


if __name__ == "__main__":
    unittest.main()

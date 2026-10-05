"""Tests for lanchess.engine."""

from __future__ import annotations

import unittest
from typing import List

from lanchess.engine import (
    BLACK, PIECE_VALUES, STARTING_FEN, WHITE, Board, IllegalMoveError, Move, Outcome,
    opposite, parse_square, perft, square_name,
)

KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
POSITION_3 = "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1"
POSITION_4 = "r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1"
POSITION_5 = "rnbq1k1r/pp1Pbppp/2p5/8/2B5/8/PPP1NnPP/RNBQK2R w KQ - 1 8"
POSITION_6 = "r4rk1/1pp1qppp/p1np1n2/2b1p1B1/2B1P1b1/P1NP1N2/1PP1QPPP/R4RK1 w - - 0 10"


def sq(name: str) -> int:
    return parse_square(name)


def mv(uci: str) -> Move:
    return Move.from_uci(uci)


def play(board: Board, moves: str) -> Board:
    for text in moves.split():
        board.push_san(text)
    return board


class TestPerft(unittest.TestCase):
    def check(self, fen: str, expected: List[int]) -> None:
        board = Board(fen)
        for depth, nodes in enumerate(expected, 1):
            with self.subTest(fen=fen, depth=depth):
                self.assertEqual(perft(board, depth), nodes)
        self.assertEqual(board.fen(), Board(fen).fen())

    def test_start(self) -> None:
        self.check(STARTING_FEN, [20, 400, 8902, 197281])

    def test_kiwipete(self) -> None:
        self.check(KIWIPETE, [48, 2039, 97862])

    def test_position_3(self) -> None:
        self.check(POSITION_3, [14, 191, 2812, 43238])

    def test_position_4(self) -> None:
        self.check(POSITION_4, [6, 264, 9467])

    def test_position_5(self) -> None:
        self.check(POSITION_5, [44, 1486, 62379])

    def test_position_6(self) -> None:
        self.check(POSITION_6, [46, 2079, 89890])

    def test_depth_zero(self) -> None:
        self.assertEqual(perft(Board(), 0), 1)


class TestPushPop(unittest.TestCase):
    @staticmethod
    def snapshot(board: Board) -> tuple:
        return (board.fen(), list(board._history), list(board.san_stack), list(board.move_stack),
                board.captured_pieces(WHITE), board.captured_pieces(BLACK), board.turn,
                board.castling, board.ep_square, board.halfmove_clock, board.fullmove_number,
                board.king_square(WHITE), board.king_square(BLACK))

    def test_kiwipete_depth_two_restores_everything(self) -> None:
        board = Board(KIWIPETE)
        root = self.snapshot(board)
        for m1 in board.legal_moves():
            board.push(m1)
            after_m1 = self.snapshot(board)
            for m2 in board.legal_moves():
                board.push(m2)
                self.assertEqual(board.pop(), m2)
                self.assertEqual(self.snapshot(board), after_m1)
            self.assertEqual(board.pop(), m1)
            self.assertEqual(self.snapshot(board), root)

    def test_pop_empty_raises_index_error(self) -> None:
        with self.assertRaises(IndexError):
            Board().pop()

    def test_pop_restores_promotion_and_castling(self) -> None:
        board = Board(POSITION_5)
        board.push(mv("d7c8q"))
        self.assertEqual(board.piece_at(sq("c8")), 'Q')
        self.assertEqual(board.san_stack, ["dxc8=Q"])
        board.pop()
        self.assertEqual(board.fen(), POSITION_5)
        board = Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 3 10")
        board.push(mv("e1c1"))
        self.assertEqual(board.piece_at(sq("d1")), 'R')
        self.assertEqual(board.castling, "kq")
        board.pop()
        self.assertEqual(board.fen(), "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 3 10")

    def test_copy_is_independent(self) -> None:
        board = play(Board(), "e4 e5")
        clone = board.copy()
        clone.push_san("Nf3")
        clone.pop()
        clone.pop()
        self.assertEqual(len(board.move_stack), 2)
        self.assertEqual(board.san_stack, ["e4", "e5"])
        self.assertEqual(clone.san_stack, ["e4"])
        self.assertNotEqual(board.fen(), clone.fen())
        self.assertEqual(clone.root_fen, board.root_fen)

    def test_last_move_and_stacks(self) -> None:
        board = Board()
        self.assertIsNone(board.last_move())
        board.push_san("e4")
        self.assertEqual(board.last_move(), mv("e2e4"))
        self.assertEqual(board.move_stack, [mv("e2e4")])
        self.assertEqual(board.san_stack, ["e4"])
        self.assertEqual(board.root_fen, STARTING_FEN)


class TestFen(unittest.TestCase):
    def test_round_trip(self) -> None:
        fens = [
            STARTING_FEN, KIWIPETE, POSITION_3, POSITION_4, POSITION_5, POSITION_6,
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
            "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2",
            "4k3/8/8/8/8/8/8/4K3 w - - 57 123",
            "r3k2r/8/8/8/8/8/8/R3K2R b Kq - 0 1",
        ]
        for fen in fens:
            with self.subTest(fen=fen):
                self.assertEqual(Board(fen).fen(), fen)

    def test_normalization(self) -> None:
        self.assertEqual(
            Board("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w qkQK -").fen(), STARTING_FEN)
        self.assertEqual(Board("  " + STARTING_FEN + "\n").fen(), STARTING_FEN)
        self.assertEqual(Board(None).fen(), STARTING_FEN)

    def test_ep_written_after_any_double_push(self) -> None:
        board = play(Board(), "e4")
        self.assertEqual(board.fen(), "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
        board.push_san("Nf6")
        self.assertIn(" w KQkq - 1 2", board.fen())

    def test_invalid_fens_rejected(self) -> None:
        bad = [
            "",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP w KQkq - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR/8 w KQkq - 0 1",
            "rnbqkbnr/ppppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbqkbnr/pppppppp/9/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbqkbn/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbqkbnx/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "rnbq1bnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQ - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQ1BNR w kq - 0 1",
            "rnbqkknr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1",
            "Pnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQk - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/pNBQKBNR w Kkq - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR x KQkq - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkqK - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KX - 0 1",
            "4k3/8/8/8/8/8/8/4K3 w K - 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq e6 0 1",
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e6 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq z9 0 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - -1 1",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 x",
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1 extra",
            "4k3/8/8/8/8/8/8/4R1K1 w - - 0 1",
        ]
        for fen in bad:
            with self.subTest(fen=fen):
                with self.assertRaises(ValueError):
                    Board(fen)
        with self.assertRaises(ValueError):
            Board(123)  # type: ignore[arg-type]

    def test_side_to_move_in_check_is_fine(self) -> None:
        board = Board("4k3/8/8/8/8/8/8/4R1K1 b - - 0 1")
        self.assertTrue(board.is_check())
        self.assertEqual(board.check_square(), sq("e8"))


class TestHelpers(unittest.TestCase):
    def test_squares(self) -> None:
        self.assertEqual(square_name(0), "a1")
        self.assertEqual(square_name(63), "h8")
        self.assertEqual(square_name(12), "e2")
        self.assertEqual(parse_square("e4"), 28)
        self.assertEqual(parse_square("h8"), 63)
        for bad in ("", "e9", "i1", "e", "e44"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_square(bad)
        with self.assertRaises(ValueError):
            square_name(64)

    def test_opposite(self) -> None:
        self.assertEqual(opposite(WHITE), BLACK)
        self.assertEqual(opposite(BLACK), WHITE)
        with self.assertRaises(ValueError):
            opposite('x')

    def test_move_uci(self) -> None:
        self.assertEqual(Move(sq("e7"), sq("e8"), 'q').uci(), "e7e8q")
        self.assertEqual(mv("e2e4"), Move(12, 28))
        self.assertEqual(mv("e7e8q"), Move(52, 60, 'q'))
        self.assertEqual(len({mv("e2e4"), Move(12, 28)}), 1)
        for bad in ("e2", "e2e9", "e7e8k", "e2e4qq", "e2e2", "zz"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    Move.from_uci(bad)

    def test_illegal_move_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(IllegalMoveError, ValueError))
        self.assertEqual(IllegalMoveError("x").kind, 'illegal')

    def test_outcome(self) -> None:
        cases = [
            (Outcome('checkmate', WHITE), '1-0', "Checkmate — White wins"),
            (Outcome('checkmate', BLACK), '0-1', "Checkmate — Black wins"),
            (Outcome('stalemate', None), '1/2-1/2', "Draw by stalemate"),
            (Outcome('insufficient_material', None), '1/2-1/2', "Draw by insufficient material"),
            (Outcome('threefold_repetition', None), '1/2-1/2', "Draw by threefold repetition"),
            (Outcome('fifty_moves', None), '1/2-1/2', "Draw by fifty-move rule"),
            (Outcome('resignation', BLACK), '0-1', "Black wins by resignation"),
            (Outcome('timeout', WHITE), '1-0', "White wins on time"),
            (Outcome('timeout_insufficient', None), '1/2-1/2',
             "Draw — timeout vs insufficient material"),
            (Outcome('agreement', None), '1/2-1/2', "Draw by agreement"),
            (Outcome('abandoned', None), '*', "Game abandoned"),
        ]
        for outcome, result, text in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(outcome.result(), result)
                self.assertEqual(outcome.describe(), text)


class TestSan(unittest.TestCase):
    def test_file_disambiguation(self) -> None:
        board = Board("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
        self.assertEqual(board.san(mv("b1d2")), "Nbd2")
        self.assertEqual(board.san(mv("f1d2")), "Nfd2")
        self.assertEqual(board.san(mv("b1c3")), "Nc3")

    def test_rank_disambiguation(self) -> None:
        board = Board("4k3/8/8/R7/8/8/8/R3K3 w - - 0 1")
        self.assertEqual(board.san(mv("a5a3")), "R5a3")
        self.assertEqual(board.san(mv("a1a3")), "R1a3")
        self.assertEqual(board.san(mv("a1b1")), "Rb1")

    def test_file_and_rank_disambiguation(self) -> None:
        board = Board("4k3/8/8/8/8/Q7/8/Q1Q1K3 w - - 0 1")
        self.assertEqual(board.san(mv("a1b2")), "Qa1b2")
        self.assertEqual(board.san(mv("a3b2")), "Q3b2")
        self.assertEqual(board.san(mv("c1b2")), "Qcb2")

    def test_no_disambiguation_when_rival_is_pinned(self) -> None:
        board = Board("4k3/8/8/b7/8/2N5/8/4K1N1 w - - 0 1")
        self.assertEqual(board.san(mv("g1e2")), "Ne2")
        self.assertEqual(board.parse_san("Ne2"), mv("g1e2"))

    def test_pawn_moves(self) -> None:
        board = Board("4k3/8/8/3p4/4P3/8/8/4K3 w - - 0 1")
        self.assertEqual(board.san(mv("e4d5")), "exd5")
        self.assertEqual(board.san(mv("e4e5")), "e5")
        board = Board("4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1")
        self.assertEqual(board.san(mv("e5d6")), "exd6")

    def test_promotion_with_capture_and_check(self) -> None:
        board = Board("3r3k/4P3/8/8/8/8/8/4K3 w - - 0 1")
        self.assertEqual(board.san(mv("e7d8q")), "exd8=Q+")
        self.assertEqual(board.san(mv("e7d8n")), "exd8=N")
        self.assertEqual(board.san(mv("e7e8r")), "e8=R+")
        board.push(mv("e7d8q"))
        self.assertEqual(board.san_stack, ["exd8=Q+"])
        self.assertEqual(board.piece_at(sq("d8")), 'Q')

    def test_castling(self) -> None:
        board = Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertEqual(board.san(mv("e1g1")), "O-O")
        self.assertEqual(board.san(mv("e1c1")), "O-O-O")
        board = Board("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1")
        self.assertEqual(board.san(mv("e8g8")), "O-O")
        self.assertEqual(board.san(mv("e8c8")), "O-O-O")
        self.assertEqual(Board("5k2/8/8/8/8/8/8/4K2R w K - 0 1").san(mv("e1g1")), "O-O+")

    def test_check_and_mate_suffixes(self) -> None:
        board = play(Board(), "e4 f6")
        self.assertEqual(board.san(mv("d1h5")), "Qh5+")
        board = play(Board(), "f3 e5 g4 Qh4")
        self.assertEqual(board.san_stack, ["f3", "e5", "g4", "Qh4#"])
        self.assertEqual(Board("7k/8/6K1/8/8/8/8/R7 w - - 0 1").san(mv("a1a8")), "Ra8#")

    def test_captures(self) -> None:
        board = play(Board(), "e4 d5 Nc3 Nf6")
        self.assertEqual(board.san(mv("e4d5")), "exd5")
        self.assertEqual(board.san(mv("c3d5")), "Nxd5")
        board.push_san("exd5")
        self.assertEqual(board.san(mv("f6d5")), "Nxd5")
        self.assertEqual(board.san(mv("d8d5")), "Qxd5")

    def test_san_of_illegal_move_raises(self) -> None:
        with self.assertRaises(IllegalMoveError):
            Board().san(mv("e2e5"))

    def test_legal_moves_san_sorted(self) -> None:
        sans = Board().legal_moves_san()
        self.assertEqual(len(sans), 20)
        self.assertEqual(sans, sorted(sans))
        self.assertIn("Nf3", sans)
        self.assertIn("e4", sans)


class TestParsing(unittest.TestCase):
    def test_standard_san(self) -> None:
        board = Board()
        self.assertEqual(board.parse_san("e4"), mv("e2e4"))
        self.assertEqual(board.parse_san("Nf3"), mv("g1f3"))
        self.assertEqual(board.parse_san("Nf3+!?"), mv("g1f3"))

    def test_castling_variants(self) -> None:
        board = Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        for text in ("O-O", "0-0", "o-o", "OO", "O-O+", "oo"):
            with self.subTest(text=text):
                self.assertEqual(board.parse_san(text), mv("e1g1"))
        for text in ("O-O-O", "0-0-0", "o-o-o", "OOO"):
            with self.subTest(text=text):
                self.assertEqual(board.parse_san(text), mv("e1c1"))
        self.assertEqual(board.parse_move("e1g1"), mv("e1g1"))
        self.assertEqual(board.parse_move("e1c1"), mv("e1c1"))
        with self.assertRaises(IllegalMoveError) as ctx:
            Board().parse_san("O-O")
        self.assertEqual(str(ctx.exception), "Illegal move: O-O")

    def test_promotion_variants(self) -> None:
        board = Board("8/4P3/8/8/8/k7/8/4K3 w - - 0 1")
        for text in ("e8=Q", "e8Q", "e8=q", "e8q", "e8", "e8=Q+"):
            with self.subTest(text=text):
                self.assertEqual(board.parse_san(text), mv("e7e8q"))
        self.assertEqual(board.parse_san("e8N"), mv("e7e8n"))
        self.assertEqual(board.parse_san("e8=n"), mv("e7e8n"))
        self.assertEqual(board.parse_san("e8=R"), mv("e7e8r"))
        self.assertEqual(board.parse_move("e7e8"), mv("e7e8q"))
        self.assertEqual(board.parse_move("e7e8n"), mv("e7e8n"))
        self.assertEqual(board.parse_move("e7-e8=B"), mv("e7e8b"))
        black = Board("4k3/8/8/8/8/K7/3p4/8 b - - 0 1")
        self.assertEqual(black.parse_san("d1"), mv("d2d1q"))
        self.assertEqual(black.parse_move("d2d1"), mv("d2d1q"))
        with self.assertRaises(IllegalMoveError):
            Board().parse_san("e4=Q")

    def test_lowercase_pieces(self) -> None:
        board = Board()
        self.assertEqual(board.parse_san("nf3"), mv("g1f3"))
        self.assertEqual(board.parse_san("NF3"), mv("g1f3"))
        board = play(Board(), "e4 e5")
        self.assertEqual(board.parse_san("qh5"), mv("d1h5"))
        self.assertEqual(board.parse_san("ke2"), mv("e1e2"))
        self.assertEqual(board.parse_san("bc4"), mv("f1c4"))
        board = Board("4k3/8/8/8/8/8/8/R3K3 w - - 0 1")
        self.assertEqual(board.parse_san("ra7"), mv("a1a7"))

    def test_lowercase_b_pawn_before_bishop(self) -> None:
        board = Board("4k3/8/8/8/8/2n5/1P6/4B2K w - - 0 1")
        self.assertEqual(board.parse_san("bxc3"), mv("b2c3"))
        self.assertEqual(board.parse_san("bc3"), mv("b2c3"))
        self.assertEqual(board.parse_san("Bxc3"), mv("e1c3"))
        self.assertEqual(board.parse_san("Bc3"), mv("e1c3"))
        self.assertEqual(board.parse_san("bd2"), mv("e1d2"))
        self.assertEqual(board.parse_san("bf2"), mv("e1f2"))
        self.assertEqual(board.parse_san("b3"), mv("b2b3"))
        self.assertEqual(board.parse_san("b4"), mv("b2b4"))

    def test_over_disambiguation(self) -> None:
        board = Board()
        self.assertEqual(board.parse_san("Ngf3"), mv("g1f3"))
        self.assertEqual(board.parse_san("N1f3"), mv("g1f3"))
        self.assertEqual(board.parse_san("Ng1f3"), mv("g1f3"))
        self.assertEqual(board.parse_san("Ng1xf3"), mv("g1f3"))

    def test_missing_capture_marker(self) -> None:
        board = play(Board(), "e4 d5")
        self.assertEqual(board.parse_san("ed5"), mv("e4d5"))
        play(board, "exd5 Nf6 Nc3")
        self.assertEqual(board.parse_san("Nd5"), mv("f6d5"))
        self.assertEqual(board.parse_san("Nxd5"), mv("f6d5"))
        self.assertEqual(board.parse_san("Qd5"), mv("d8d5"))

    def test_pawn_without_file_is_a_push(self) -> None:
        board = Board("4k3/8/8/3p4/2P1P3/8/8/4K3 w - - 0 1")
        with self.assertRaises(IllegalMoveError):
            board.parse_san("d5")
        self.assertEqual(board.parse_san("cd5"), mv("c4d5"))
        self.assertEqual(board.parse_san("exd5"), mv("e4d5"))

    def test_coordinate_forms(self) -> None:
        board = Board()
        for text in ("e2e4", "e2-e4", "e2 e4", "e2xe4", "E2E4", "  e2e4  ", "E2-E4"):
            with self.subTest(text=text):
                self.assertEqual(board.parse_move(text), mv("e2e4"))
        self.assertEqual(board.parse_move("g1f3"), mv("g1f3"))
        self.assertEqual(board.parse_move("Nf3"), mv("g1f3"))
        self.assertEqual(board.push_san("e4"), mv("e2e4"))
        self.assertEqual(board.turn, BLACK)

    def test_bishop_san_versus_coordinates(self) -> None:
        board = Board("4k3/8/8/8/8/4B3/8/4B2K w - - 0 1")
        self.assertEqual(board.san(mv("e1f2")), "B1f2")
        self.assertEqual(board.parse_move("B1f2"), mv("e1f2"))
        self.assertEqual(board.parse_move("B3f2"), mv("e3f2"))
        self.assertEqual(board.parse_move("B1F2"), mv("e1f2"))
        with self.assertRaises(IllegalMoveError) as ctx:
            board.parse_move("b1f2")
        self.assertEqual(str(ctx.exception), "Illegal move: b1f2")
        board = Board("4k3/8/8/8/8/8/8/1N2B2K w - - 0 1")
        self.assertEqual(board.parse_move("b1c3"), mv("b1c3"))
        self.assertEqual(board.parse_move("B1C3"), mv("b1c3"))
        self.assertEqual(board.parse_move("B1c3"), mv("e1c3"))

    def test_error_messages(self) -> None:
        board = Board()
        with self.assertRaises(IllegalMoveError) as ctx:
            board.parse_move("Nf6")
        self.assertEqual(str(ctx.exception), "Illegal move: Nf6")
        self.assertEqual(ctx.exception.kind, 'illegal')
        with self.assertRaises(IllegalMoveError) as ctx:
            board.parse_move("xyz")
        self.assertEqual(str(ctx.exception), "Unrecognized move: xyz")
        self.assertEqual(ctx.exception.kind, 'unrecognized')
        with self.assertRaises(IllegalMoveError) as ctx:
            board.parse_move("e2e5")
        self.assertEqual(str(ctx.exception), "Illegal move: e2e5")
        with self.assertRaises(IllegalMoveError):
            board.parse_move("   ")
        ambiguous = Board("4k3/8/8/8/8/8/8/1N2KN2 w - - 0 1")
        with self.assertRaises(IllegalMoveError) as ctx:
            ambiguous.parse_move("Nd2")
        self.assertEqual(str(ctx.exception), "Ambiguous move: Nd2 (could be Nbd2, Nfd2)")
        self.assertEqual(ctx.exception.kind, 'ambiguous')
        self.assertEqual(ambiguous.parse_move("Nbd2"), mv("b1d2"))
        self.assertEqual(ambiguous.parse_move("Nfd2"), mv("f1d2"))

    def test_push_rejects_illegal_moves(self) -> None:
        board = Board()
        with self.assertRaises(IllegalMoveError):
            board.push(mv("e2e5"))
        with self.assertRaises(IllegalMoveError):
            board.push(mv("e7e5"))
        with self.assertRaises(TypeError):
            board.push("e2e4")  # type: ignore[arg-type]
        self.assertTrue(board.is_legal(mv("e2e4")))
        self.assertFalse(board.is_legal(mv("e2e5")))
        self.assertEqual(board.fen(), STARTING_FEN)


class TestCastling(unittest.TestCase):
    @staticmethod
    def legal(fen: str) -> List[Move]:
        return Board(fen).legal_moves()

    def test_both_sides_available(self) -> None:
        moves = self.legal("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
        self.assertIn(mv("e1g1"), moves)
        self.assertIn(mv("e1c1"), moves)

    def test_not_through_check(self) -> None:
        moves = self.legal("4k3/8/8/8/2b5/8/8/R3K2R w KQ - 0 1")
        self.assertNotIn(mv("e1g1"), moves)
        self.assertIn(mv("e1c1"), moves)
        moves = self.legal("3rk3/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        self.assertNotIn(mv("e1c1"), moves)
        self.assertIn(mv("e1g1"), moves)

    def test_not_into_check(self) -> None:
        moves = self.legal("4k3/8/8/2b5/8/8/8/R3K2R w KQ - 0 1")
        self.assertNotIn(mv("e1g1"), moves)
        moves = self.legal("2r1k3/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        self.assertNotIn(mv("e1c1"), moves)

    def test_not_out_of_check(self) -> None:
        moves = self.legal("4k3/4r3/8/8/8/8/8/R3K2R w KQ - 0 1")
        self.assertNotIn(mv("e1g1"), moves)
        self.assertNotIn(mv("e1c1"), moves)

    def test_b_file_attack_does_not_prevent_queenside(self) -> None:
        moves = self.legal("1r2k3/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        self.assertIn(mv("e1c1"), moves)

    def test_blocked(self) -> None:
        moves = self.legal("4k3/8/8/8/8/8/8/RN2K1NR w KQ - 0 1")
        self.assertNotIn(mv("e1g1"), moves)
        self.assertNotIn(mv("e1c1"), moves)

    def test_rights_lost_when_king_moves(self) -> None:
        board = play(Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"), "Kf1 Kf8 Ke1 Ke8")
        self.assertEqual(board.castling, "")
        self.assertNotIn(mv("e1g1"), board.legal_moves())
        self.assertIn(" w - - ", board.fen())

    def test_rights_lost_when_rook_moves(self) -> None:
        board = play(Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"), "Rg1 Rb8 Rh1 Ra8")
        self.assertEqual(board.castling, "Qk")
        self.assertNotIn(mv("e1g1"), board.legal_moves())
        self.assertIn(mv("e1c1"), board.legal_moves())

    def test_rights_lost_when_rook_captured_on_home_square(self) -> None:
        board = play(Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"), "Rxh8+")
        self.assertEqual(board.castling, "Qq")
        board = play(Board("r3k2r/8/8/8/8/8/1b6/R3K2R b KQkq - 0 1"), "Bxa1")
        self.assertEqual(board.castling, "Kkq")
        board.pop()
        self.assertEqual(board.castling, "KQkq")

    def test_castling_moves_rook(self) -> None:
        board = play(Board("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"), "O-O O-O-O")
        self.assertEqual(board.piece_at(sq("g1")), 'K')
        self.assertEqual(board.piece_at(sq("f1")), 'R')
        self.assertIsNone(board.piece_at(sq("h1")))
        self.assertEqual(board.piece_at(sq("c8")), 'k')
        self.assertEqual(board.piece_at(sq("d8")), 'r')
        self.assertIsNone(board.piece_at(sq("a8")))
        self.assertEqual(board.king_square(WHITE), sq("g1"))
        self.assertEqual(board.king_square(BLACK), sq("c8"))


class TestEnPassant(unittest.TestCase):
    def test_capture_from_play(self) -> None:
        board = play(Board(), "e4 a6 e5 d5")
        self.assertEqual(board.ep_square, sq("d6"))
        self.assertIn(mv("e5d6"), board.legal_moves())
        board.push_san("exd6")
        self.assertIsNone(board.piece_at(sq("d5")))
        self.assertEqual(board.piece_at(sq("d6")), 'P')
        self.assertEqual(board.captured_pieces(BLACK), ['p'])
        board.pop()
        self.assertEqual(board.piece_at(sq("d5")), 'p')
        self.assertIsNone(board.piece_at(sq("d6")))

    def test_only_immediately(self) -> None:
        board = play(Board(), "e4 a6 e5 d5 Nf3 Nf6")
        self.assertNotIn(mv("e5d6"), board.legal_moves())

    def test_pinned_ep_capture_is_illegal(self) -> None:
        board = Board("8/8/8/KPp4r/8/8/8/7k w - c6 0 1")
        self.assertNotIn(mv("b5c6"), board.legal_moves())
        self.assertIn(mv("b5b6"), board.legal_moves())

    def test_ep_capture_resolves_check(self) -> None:
        board = Board("8/8/8/3pP3/4K3/8/8/7k w - d6 0 1")
        self.assertTrue(board.is_check())
        self.assertIn(mv("e5d6"), board.legal_moves())

    def test_black_ep(self) -> None:
        board = Board("4k3/8/8/8/3p4/8/4P3/4K3 w - - 0 1")
        play(board, "e4")
        self.assertEqual(board.san(mv("d4e3")), "dxe3")
        board.push_san("dxe3")
        self.assertIsNone(board.piece_at(sq("e4")))
        self.assertEqual(board.captured_pieces(WHITE), ['P'])


class TestGameEnd(unittest.TestCase):
    def test_checkmate(self) -> None:
        board = play(Board(), "f3 e5 g4 Qh4")
        self.assertTrue(board.is_check())
        self.assertTrue(board.is_checkmate())
        self.assertFalse(board.is_stalemate())
        self.assertEqual(board.legal_moves(), [])
        self.assertEqual(board.check_square(), sq("e1"))
        self.assertEqual(board.outcome(), Outcome('checkmate', BLACK))
        self.assertTrue(board.is_game_over())

    def test_stalemate(self) -> None:
        board = Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
        self.assertFalse(board.is_check())
        self.assertTrue(board.is_stalemate())
        self.assertFalse(board.is_checkmate())
        self.assertIsNone(board.check_square())
        self.assertEqual(board.outcome(), Outcome('stalemate', None))

    def test_game_continues(self) -> None:
        board = Board()
        self.assertIsNone(board.outcome())
        self.assertFalse(board.is_game_over())

    def test_insufficient_material(self) -> None:
        cases = {
            "8/8/8/8/8/8/8/K6k w - - 0 1": True,
            "8/8/8/8/8/8/8/KN5k w - - 0 1": True,
            "8/8/8/8/8/8/8/KB5k w - - 0 1": True,
            "7b/8/8/8/8/8/8/K6k w - - 0 1": True,
            "5b2/8/8/8/8/8/8/K1B4k w - - 0 1": True,
            "2b5/8/8/8/8/8/8/K1B4k w - - 0 1": False,
            "8/8/8/8/8/8/1B6/K1B4k w - - 0 1": True,
            "8/8/8/8/8/8/8/KNN4k w - - 0 1": False,
            "n7/8/8/8/8/8/8/KN5k w - - 0 1": False,
            "n7/8/8/8/8/8/8/KB5k w - - 0 1": False,
            "8/8/8/8/8/8/P7/K6k w - - 0 1": False,
            "8/8/8/8/8/8/R7/K6k w - - 0 1": False,
            "8/8/8/8/8/8/Q7/K6k w - - 0 1": False,
        }
        for fen, expected in cases.items():
            with self.subTest(fen=fen):
                self.assertEqual(Board(fen).is_insufficient_material(), expected)
        self.assertEqual(Board("8/8/8/8/8/8/8/K6k w - - 0 1").outcome(),
                         Outcome('insufficient_material', None))

    def test_has_insufficient_material_per_color(self) -> None:
        board = Board("q7/8/8/8/8/8/8/KN5k w - - 0 1")
        self.assertTrue(board.has_insufficient_material(WHITE))
        self.assertFalse(board.has_insufficient_material(BLACK))
        cases = {
            "8/8/8/8/8/8/8/K6k w - - 0 1": True,
            "8/8/8/8/8/8/8/KB5k w - - 0 1": True,
            "8/8/8/8/8/8/8/KNN4k w - - 0 1": False,
            "8/8/8/8/8/8/1B6/K1B4k w - - 0 1": False,
            "8/8/8/8/8/8/R7/K6k w - - 0 1": False,
            "8/8/8/8/8/8/P7/K6k w - - 0 1": False,
        }
        for fen, expected in cases.items():
            with self.subTest(fen=fen):
                self.assertEqual(Board(fen).has_insufficient_material(WHITE), expected)
                self.assertTrue(Board(fen).has_insufficient_material(BLACK))

    def test_threefold_repetition(self) -> None:
        board = Board()
        play(board, "Nf3 Nf6 Ng1 Ng8")
        self.assertTrue(board.is_repetition(2))
        self.assertFalse(board.is_repetition())
        self.assertIsNone(board.outcome())
        play(board, "Nf3 Nf6 Ng1")
        self.assertFalse(board.is_repetition())
        board.push_san("Ng8")
        self.assertTrue(board.is_repetition())
        self.assertEqual(board.outcome(), Outcome('threefold_repetition', None))
        board.pop()
        self.assertIsNone(board.outcome())

    def test_repetition_ignores_ep_square_without_legal_capture(self) -> None:
        board = play(Board(), "e4")
        self.assertEqual(board.ep_square, sq("e3"))
        play(board, "Nf6 Nf3 Ng8 Ng1 Nf6 Nf3 Ng8")
        self.assertFalse(board.is_repetition())
        board.push_san("Ng1")
        self.assertTrue(board.is_repetition())

    def test_repetition_counts_legal_ep_square(self) -> None:
        board = play(Board("4k3/3p4/8/4P3/8/8/8/4K1N1 b - - 0 1"), "d5")
        self.assertIn(mv("e5d6"), board.legal_moves())
        cycle = "Nf3 Kd7 Ng1 Ke8"
        play(board, cycle + " " + cycle)
        self.assertTrue(board.is_repetition(2))
        self.assertFalse(board.is_repetition(3))
        play(board, cycle)
        self.assertTrue(board.is_repetition(3))

    def test_repetition_ignores_pinned_ep_square(self) -> None:
        board = play(Board("8/2p5/8/KP5r/8/8/8/7k b - - 0 1"), "c5")
        self.assertEqual(board.ep_square, sq("c6"))
        play(board, "Ka4 Kg1 Ka5 Kh1")
        self.assertTrue(board.is_repetition(2))

    def test_fifty_moves(self) -> None:
        board = Board("7k/8/6K1/8/8/8/8/R7 w - - 99 80")
        self.assertFalse(board.is_fifty_moves())
        board.push_san("Rb1")
        self.assertTrue(board.is_fifty_moves())
        self.assertEqual(board.outcome(), Outcome('fifty_moves', None))

    def test_checkmate_beats_fifty_moves(self) -> None:
        board = Board("7k/8/6K1/8/8/8/8/R7 w - - 99 80")
        board.push_san("Ra8")
        self.assertTrue(board.is_fifty_moves())
        self.assertEqual(board.outcome(), Outcome('checkmate', WHITE))

    def test_halfmove_clock_resets(self) -> None:
        board = play(Board(), "Nf3 Nf6")
        self.assertEqual(board.halfmove_clock, 2)
        board.push_san("e4")
        self.assertEqual(board.halfmove_clock, 0)
        play(board, "Nxe4")
        self.assertEqual(board.halfmove_clock, 0)
        self.assertEqual(board.fullmove_number, 3)


class TestMaterial(unittest.TestCase):
    def test_captured_pieces_sorted(self) -> None:
        board = play(Board("q3k3/8/8/n7/8/b7/p7/R3K3 w - - 0 1"),
                     "Rxa2 Ke7 Rxa3 Ke8 Rxa5 Ke7 Rxa8")
        self.assertEqual(board.captured_pieces(BLACK), ['q', 'b', 'n', 'p'])
        self.assertEqual(board.captured_pieces(WHITE), [])
        self.assertEqual(board.material_balance(), 5)

    def test_captured_promoted_piece_counts_as_promoted(self) -> None:
        board = play(Board("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1"), "a8=Q Rxa8")
        self.assertEqual(board.captured_pieces(WHITE), ['Q'])
        self.assertEqual(board.material_balance(), -5)
        board = play(Board("1r2k3/P7/8/8/8/8/8/4K3 w - - 0 1"), "axb8=N")
        self.assertEqual(board.captured_pieces(BLACK), ['r'])
        self.assertEqual(board.material_balance(), 3)

    def test_material_balance(self) -> None:
        self.assertEqual(Board().material_balance(), 0)
        self.assertEqual(play(Board(), "e4 d5 exd5").material_balance(), PIECE_VALUES['p'])
        self.assertEqual(Board("4k3/8/8/8/8/8/8/Q3K3 w - - 0 1").material_balance(), 9)
        self.assertEqual(Board("4k3/8/8/8/8/8/8/r3K3 w - - 0 1").material_balance(), -5)


class TestPgn(unittest.TestCase):
    def test_basic_format(self) -> None:
        board = play(Board(), "e4 e5 Nf3 Nc6")
        text = board.pgn({"White": "Alice", "Black": "Bob", "Event": "Casual",
                          "TimeControl": "300+3", "Annotator": "me"}, "*")
        expected = (
            '[Event "Casual"]\n'
            '[Site "?"]\n'
            '[Date "????.??.??"]\n'
            '[Round "?"]\n'
            '[White "Alice"]\n'
            '[Black "Bob"]\n'
            '[Result "*"]\n'
            '[TimeControl "300+3"]\n'
            '[Annotator "me"]\n'
            '\n'
            '1. e4 e5 2. Nf3 Nc6 *\n'
        )
        self.assertEqual(text, expected)

    def test_result_and_defaults(self) -> None:
        board = play(Board(), "f3 e5 g4 Qh4")
        text = board.pgn({}, "0-1")
        self.assertIn('[Result "0-1"]', text)
        self.assertIn('[Event "?"]', text)
        self.assertTrue(text.endswith("1. f3 e5 2. g4 Qh4# 0-1\n"))
        self.assertNotIn("SetUp", text)
        self.assertNotIn("FEN", text)

    def test_empty_game(self) -> None:
        self.assertTrue(Board().pgn({}, "*").endswith('[Result "*"]\n\n*\n'))

    def test_custom_start_black_to_move(self) -> None:
        fen = "4k3/8/8/8/8/8/4P3/4K3 b - - 0 7"
        board = play(Board(fen), "Kd7 e4 Ke6")
        text = board.pgn({"White": "A"}, "*")
        lines = text.splitlines()
        self.assertEqual(lines[7], '[SetUp "1"]')
        self.assertEqual(lines[8], f'[FEN "{fen}"]')
        self.assertEqual(lines[-1], "7... Kd7 8. e4 Ke6 *")

    def test_custom_start_white_to_move(self) -> None:
        board = play(Board("4k3/8/8/8/8/8/4P3/4K3 w - - 0 1"), "e4")
        self.assertTrue(board.pgn({}, "*").endswith("\n1. e4 *\n"))
        self.assertIn('[SetUp "1"]', board.pgn({}, "*"))

    def test_real_game_san_and_wrapping(self) -> None:
        opera = ("e4 e5 Nf3 d6 d4 Bg4 dxe5 Bxf3 Qxf3 dxe5 Bc4 Nf6 Qb3 Qe7 Nc3 c6 Bg5 b5 "
                 "Nxb5 cxb5 Bxb5+ Nbd7 O-O-O Rd8 Rxd7 Rxd7 Rd1 Qe6 Bxd7+ Nxd7 Qb8+ Nxb8 Rd8#")
        board = Board()
        for text in opera.split():
            board.push_san(text.rstrip("+#"))
        self.assertEqual(board.san_stack, opera.split())
        self.assertEqual(board.outcome(), Outcome('checkmate', WHITE))
        text = board.pgn({"White": "Morphy"}, "1-0")
        movetext = text.split("\n\n", 1)[1].rstrip("\n")
        lines = movetext.split("\n")
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 80 for line in lines))
        self.assertTrue(any(len(line) > 70 for line in lines))
        self.assertTrue(movetext.startswith("1. e4 e5 2. Nf3 d6 3. d4 Bg4"))
        self.assertTrue(movetext.endswith("17. Rd8# 1-0"))
        tokens = " ".join(lines).split()
        self.assertEqual([t for t in tokens if not t[0].isdigit()], board.san_stack)
        self.assertEqual(" ".join(lines), " ".join(tokens))

    def test_header_escaping(self) -> None:
        text = Board().pgn({"White": 'Al "the" \\ Kid', "Event": "a\nb"}, "*")
        self.assertIn('[White "Al \\"the\\" \\\\ Kid"]', text)
        self.assertIn('[Event "a b"]', text)


if __name__ == "__main__":
    unittest.main()

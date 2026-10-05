"""Chess rules engine: board state, legal moves, FEN, SAN and PGN (no I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

WHITE = 'w'
BLACK = 'b'
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
PIECE_VALUES = {'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9, 'k': 0}
COLOR_NAMES = {WHITE: "White", BLACK: "Black"}

FILE_NAMES = "abcdefgh"
RANK_NAMES = "12345678"

_MoveTuple = Tuple[int, int, Optional[str]]

_PROMOTIONS = ('q', 'r', 'b', 'n')
_CAPTURE_ORDER = {'q': 0, 'r': 1, 'b': 2, 'n': 3, 'p': 4, 'k': 5}
_KIND = {p: p.lower() for p in "PNBRQKpnbrqk"}
_COLOR_OF = {p: (WHITE if p.isupper() else BLACK) for p in "PNBRQKpnbrqk"}
_OPP = {WHITE: BLACK, BLACK: WHITE}
_OWN = {WHITE: frozenset("PNBRQK"), BLACK: frozenset("pnbrqk")}
_PIECES = {WHITE: tuple("PNBRQK"), BLACK: tuple("pnbrqk")}
_CASTLE_LOSS = {4: "KQ", 7: "K", 0: "Q", 60: "kq", 63: "k", 56: "q"}
_CASTLE_HOME = {'K': (4, 7, 'K', 'R'), 'Q': (4, 0, 'K', 'R'),
                'k': (60, 63, 'k', 'r'), 'q': (60, 56, 'k', 'r')}
_SEVEN_TAG_ROSTER = (("Event", "?"), ("Site", "?"), ("Date", "????.??.??"), ("Round", "?"),
                     ("White", "?"), ("Black", "?"), ("Result", "*"))


def _build_tables() -> tuple:
    def targets(f: int, r: int, deltas: Sequence[Tuple[int, int]]) -> Tuple[int, ...]:
        return tuple((r + dr) * 8 + f + df for df, dr in deltas
                     if 0 <= f + df < 8 and 0 <= r + dr < 8)

    knight_deltas = ((1, 2), (2, 1), (2, -1), (1, -2), (-1, -2), (-2, -1), (-2, 1), (-1, 2))
    king_deltas = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
    directions = ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1))
    knight, king, rays, white_pawn, black_pawn = [], [], [], [], []
    for sq in range(64):
        f, r = sq % 8, sq // 8
        knight.append(targets(f, r, knight_deltas))
        king.append(targets(f, r, king_deltas))
        white_pawn.append(targets(f, r, ((-1, 1), (1, 1))))
        black_pawn.append(targets(f, r, ((-1, -1), (1, -1))))
        sq_rays = []
        for df, dr in directions:
            ray = []
            nf, nr = f + df, r + dr
            while 0 <= nf < 8 and 0 <= nr < 8:
                ray.append(nr * 8 + nf)
                nf += df
                nr += dr
            sq_rays.append(tuple(ray))
        rays.append(tuple(sq_rays))
    return (tuple(knight), tuple(king), tuple(rays),
            {WHITE: tuple(white_pawn), BLACK: tuple(black_pawn)})


_KNIGHT, _KING, _RAYS, _PAWN_ATTACKS = _build_tables()
_ROOK_RAYS = tuple(r[:4] for r in _RAYS)
_BISHOP_RAYS = tuple(r[4:] for r in _RAYS)
_SLIDER_RAYS = {'r': _ROOK_RAYS, 'b': _BISHOP_RAYS, 'q': _RAYS}

_SAN_RE = re.compile(r"^([NBRQKPnbrqkp])?([a-h])?([1-8])?[x:]?([a-h][1-8])(?:=?([NBRQnbrq]))?$")
_COORD_RE = re.compile(r"^([a-h][1-8])\s*[-x:]?\s*([a-h][1-8])\s*(?:=?\s*([qrbn]))?$")
_DIGITS_RE = re.compile(r"^[0-9]+$")


def square_name(sq: int) -> str:
    """Return the algebraic name of a square index (0 -> 'a1')."""
    if not isinstance(sq, int) or not 0 <= sq < 64:
        raise ValueError(f"Invalid square index: {sq!r}")
    return FILE_NAMES[sq % 8] + RANK_NAMES[sq // 8]


def parse_square(name: str) -> int:
    """Return the index of an algebraic square name ('e4' -> 28)."""
    if (not isinstance(name, str) or len(name) != 2
            or name[0].lower() not in FILE_NAMES or name[1] not in RANK_NAMES):
        raise ValueError(f"Invalid square: {name!r}")
    return RANK_NAMES.index(name[1]) * 8 + FILE_NAMES.index(name[0].lower())


def opposite(color: str) -> str:
    """Return the other color."""
    try:
        return _OPP[color]
    except KeyError:
        raise ValueError(f"Invalid color: {color!r}") from None


def color_name(color: str) -> str:
    """Return 'White' or 'Black'."""
    return COLOR_NAMES[color]


class IllegalMoveError(ValueError):
    """Unrecognized, illegal or ambiguous move input.

    `kind` is one of 'unrecognized', 'illegal' or 'ambiguous'.
    """

    def __init__(self, message: str, kind: str = 'illegal') -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Move:
    """A move from one square to another, with optional lowercase promotion piece."""

    from_sq: int
    to_sq: int
    promotion: Optional[str] = None

    def uci(self) -> str:
        """Return the move in UCI notation, e.g. 'e7e8q'."""
        return square_name(self.from_sq) + square_name(self.to_sq) + (self.promotion or '')

    @classmethod
    def from_uci(cls, text: str) -> Move:
        """Parse UCI notation (syntax only, legality is not checked)."""
        if not isinstance(text, str):
            raise ValueError(f"Invalid UCI move: {text!r}")
        s = text.strip().lower()
        if len(s) not in (4, 5) or (len(s) == 5 and s[4] not in _PROMOTIONS):
            raise ValueError(f"Invalid UCI move: {text!r}")
        frm, to = parse_square(s[:2]), parse_square(s[2:4])
        if frm == to:
            raise ValueError(f"Invalid UCI move: {text!r}")
        return cls(frm, to, s[4] if len(s) == 5 else None)

    def __str__(self) -> str:
        return self.uci()


@dataclass(frozen=True)
class Outcome:
    """How a game ended; `winner` is 'w', 'b' or None for a draw."""

    termination: str
    winner: Optional[str] = None

    def result(self) -> str:
        """Return the PGN result string."""
        if self.termination == 'abandoned':
            return '*'
        if self.winner == WHITE:
            return '1-0'
        if self.winner == BLACK:
            return '0-1'
        return '1/2-1/2'

    def describe(self) -> str:
        """Return a human readable description of the outcome."""
        if self.termination in _FIXED_DESCRIPTIONS:
            return _FIXED_DESCRIPTIONS[self.termination]
        who = COLOR_NAMES.get(self.winner or '')
        if who is None:
            return "Draw"
        return _WIN_DESCRIPTIONS.get(self.termination, "{} wins").format(who)


_FIXED_DESCRIPTIONS = {
    'stalemate': "Draw by stalemate",
    'insufficient_material': "Draw by insufficient material",
    'threefold_repetition': "Draw by threefold repetition",
    'fifty_moves': "Draw by fifty-move rule",
    'agreement': "Draw by agreement",
    'timeout_insufficient': "Draw — timeout vs insufficient material",
    'abandoned': "Game abandoned",
}
_WIN_DESCRIPTIONS = {
    'checkmate': "Checkmate — {} wins",
    'resignation': "{} wins by resignation",
    'timeout': "{} wins on time",
}


def _fen_error(reason: str) -> ValueError:
    return ValueError(f"Invalid FEN: {reason}")


def _pgn_escape(value: str) -> str:
    text = " ".join(str(value).split())
    return text.replace("\\", "\\\\").replace('"', '\\"')


class Board:
    """A chess position plus the history of moves played from `root_fen`."""

    def __init__(self, fen: Optional[str] = STARTING_FEN) -> None:
        self._set_fen(STARTING_FEN if fen is None else fen)
        self.root_fen: str = self.fen()
        self._root_turn = self.turn
        self._root_fullmove = self.fullmove_number
        self.move_stack: List[Move] = []
        self.san_stack: List[str] = []
        self._undo: List[tuple] = []
        self._history: List[tuple] = [self._position_key()]

    def __repr__(self) -> str:
        return f"Board({self.fen()!r})"

    def _set_fen(self, fen: str) -> None:
        if not isinstance(fen, str):
            raise _fen_error("not a string")
        parts = fen.split()
        if not parts:
            raise _fen_error("empty")
        if len(parts) > 6:
            raise _fen_error("too many fields")
        placement, turn, castling, ep, half, full = parts + ['w', '-', '-', '0', '1'][len(parts) - 1:]

        rows = placement.split('/')
        if len(rows) != 8:
            raise _fen_error("expected 8 ranks")
        board: List[Optional[str]] = [None] * 64
        for i, row in enumerate(rows):
            rank, file = 7 - i, 0
            for ch in row:
                if ch in "12345678":
                    file += int(ch)
                elif ch in _KIND:
                    if file < 8:
                        board[rank * 8 + file] = ch
                    file += 1
                else:
                    raise _fen_error(f"unexpected character {ch!r}")
                if file > 8:
                    raise _fen_error(f"rank {rank + 1} has too many squares")
            if file != 8:
                raise _fen_error(f"rank {rank + 1} has too few squares")
        if board.count('K') != 1 or board.count('k') != 1:
            raise _fen_error("each side needs exactly one king")
        if any(board[sq] in ('P', 'p') for sq in list(range(8)) + list(range(56, 64))):
            raise _fen_error("pawns on the first or last rank")

        turn = turn.lower()
        if turn not in (WHITE, BLACK):
            raise _fen_error("side to move must be 'w' or 'b'")

        if castling == '-':
            rights = ''
        else:
            if any(c not in "KQkq" for c in castling) or len(set(castling)) != len(castling):
                raise _fen_error("bad castling field")
            for c in castling:
                king_sq, rook_sq, king, rook = _CASTLE_HOME[c]
                if board[king_sq] != king or board[rook_sq] != rook:
                    raise _fen_error(f"castling right {c!r} does not match the position")
            rights = ''.join(c for c in "KQkq" if c in castling)

        if ep == '-':
            ep_square = None
        else:
            try:
                ep_square = parse_square(ep)
            except ValueError:
                raise _fen_error("bad en passant square") from None
            step = 8 if turn == WHITE else -8
            expected_rank = 5 if turn == WHITE else 2
            pawn = 'p' if turn == WHITE else 'P'
            if (ep_square // 8 != expected_rank or board[ep_square] is not None
                    or board[ep_square + step] is not None or board[ep_square - step] != pawn):
                raise _fen_error("en passant square does not match the position")

        if not _DIGITS_RE.match(half) or not _DIGITS_RE.match(full):
            raise _fen_error("move counters must be non-negative integers")

        self._board = board
        self.turn: str = turn
        self.castling: str = rights
        self.ep_square: Optional[int] = ep_square
        self.halfmove_clock: int = int(half)
        self.fullmove_number: int = max(1, int(full))
        self._king: Dict[str, int] = {WHITE: board.index('K'), BLACK: board.index('k')}
        if self.is_attacked(self._king[_OPP[turn]], turn):
            raise _fen_error("the side not to move is in check")

    def piece_at(self, sq: int) -> Optional[str]:
        """Return the piece on a square or None."""
        return self._board[sq]

    def copy(self) -> Board:
        """Return an independent deep copy, including move history."""
        other = Board.__new__(Board)
        other.__dict__.update(self.__dict__)
        other._board = self._board[:]
        other._king = dict(self._king)
        other.move_stack = self.move_stack[:]
        other.san_stack = self.san_stack[:]
        other._undo = self._undo[:]
        other._history = self._history[:]
        return other

    def fen(self) -> str:
        """Return the FEN of the current position."""
        rows = []
        for rank in range(7, -1, -1):
            row, empty = '', 0
            for sq in range(rank * 8, rank * 8 + 8):
                p = self._board[sq]
                if p is None:
                    empty += 1
                else:
                    if empty:
                        row += str(empty)
                        empty = 0
                    row += p
            rows.append(row + (str(empty) if empty else ''))
        ep = square_name(self.ep_square) if self.ep_square is not None else '-'
        return (f"{'/'.join(rows)} {self.turn} {self.castling or '-'} {ep} "
                f"{self.halfmove_clock} {self.fullmove_number}")

    def king_square(self, color: str) -> int:
        """Return the square of `color`'s king."""
        return self._king[color]

    def is_attacked(self, sq: int, by_color: str) -> bool:
        """Return True if any piece of `by_color` attacks `sq`."""
        board = self._board
        pawn, knight, bishop, rook, queen, king = _PIECES[by_color]
        for t in _KNIGHT[sq]:
            if board[t] == knight:
                return True
        for t in _PAWN_ATTACKS[_OPP[by_color]][sq]:
            if board[t] == pawn:
                return True
        for t in _KING[sq]:
            if board[t] == king:
                return True
        for ray in _ROOK_RAYS[sq]:
            for t in ray:
                p = board[t]
                if p is not None:
                    if p == rook or p == queen:
                        return True
                    break
        for ray in _BISHOP_RAYS[sq]:
            for t in ray:
                p = board[t]
                if p is not None:
                    if p == bishop or p == queen:
                        return True
                    break
        return False

    def is_check(self) -> bool:
        """Return True if the side to move is in check."""
        return self.is_attacked(self._king[self.turn], _OPP[self.turn])

    def check_square(self) -> Optional[int]:
        """Return the king square of the side to move if it is in check."""
        return self._king[self.turn] if self.is_check() else None

    def _make(self, move: _MoveTuple) -> tuple:
        frm, to, promo = move
        board = self._board
        piece = board[frm]
        captured = board[to]
        cap_sq = to
        us = self.turn
        board[frm] = None
        kind = _KIND[piece]
        if kind == 'p':
            if to == self.ep_square:
                cap_sq = to - 8 if us == WHITE else to + 8
                captured = board[cap_sq]
                board[cap_sq] = None
            board[to] = (promo.upper() if us == WHITE else promo) if promo else piece
            ep = (frm + to) // 2 if to - frm in (16, -16) else None
            half = 0
        else:
            board[to] = piece
            ep = None
            half = 0 if captured is not None else self.halfmove_clock + 1
            if kind == 'k':
                self._king[us] = to
                if to - frm == 2:
                    board[frm + 1] = board[frm + 3]
                    board[frm + 3] = None
                elif frm - to == 2:
                    board[frm - 1] = board[frm - 4]
                    board[frm - 4] = None
        rights = self.castling
        if rights and (frm in _CASTLE_LOSS or to in _CASTLE_LOSS):
            lost = _CASTLE_LOSS.get(frm, '') + _CASTLE_LOSS.get(to, '')
            self.castling = ''.join(c for c in rights if c not in lost)
        undo = (frm, to, piece, captured, cap_sq, rights,
                self.ep_square, self.halfmove_clock, self.fullmove_number)
        self.ep_square = ep
        self.halfmove_clock = half
        if us == BLACK:
            self.fullmove_number += 1
        self.turn = _OPP[us]
        return undo

    def _unmake(self, undo: tuple) -> None:
        frm, to, piece, captured, cap_sq, rights, ep, half, full = undo
        board = self._board
        us = _OPP[self.turn]
        board[frm] = piece
        board[to] = None
        if captured is not None:
            board[cap_sq] = captured
        if piece == 'K' or piece == 'k':
            self._king[us] = frm
            if to - frm == 2:
                board[frm + 3] = board[frm + 1]
                board[frm + 1] = None
            elif frm - to == 2:
                board[frm - 4] = board[frm - 1]
                board[frm - 1] = None
        self.turn = us
        self.castling = rights
        self.ep_square = ep
        self.halfmove_clock = half
        self.fullmove_number = full

    def _is_safe(self, move: _MoveTuple) -> bool:
        us = self.turn
        undo = self._make(move)
        safe = not self.is_attacked(self._king[us], _OPP[us])
        self._unmake(undo)
        return safe

    def _pinned(self, ksq: int, us: str) -> Dict[int, FrozenSet[int]]:
        board = self._board
        own = _OWN[us]
        _, _, bishop, rook, queen, _ = _PIECES[_OPP[us]]
        pinned = {}
        for d, ray in enumerate(_RAYS[ksq]):
            slider = rook if d < 4 else bishop
            blocker = -1
            for i, sq in enumerate(ray):
                p = board[sq]
                if p is None:
                    continue
                if blocker < 0:
                    if p in own:
                        blocker = sq
                        continue
                    break
                if p == slider or p == queen:
                    pinned[blocker] = frozenset(ray[:i + 1])
                break
        return pinned

    def _generate(self) -> List[_MoveTuple]:
        board = self._board
        us = self.turn
        them = _OPP[us]
        own = _OWN[us]
        enemy = _OWN[them]
        ksq = self._king[us]
        in_check = self.is_attacked(ksq, them)
        ep = self.ep_square
        if us == WHITE:
            push, start_rank, last_rank = 8, 1, 7
        else:
            push, start_rank, last_rank = -8, 6, 0
        pawn_caps = _PAWN_ATTACKS[us]
        moves: List[_MoveTuple] = []
        add = moves.append

        for sq in range(64):
            p = board[sq]
            if p not in own:
                continue
            kind = _KIND[p]
            if kind == 'p':
                to = sq + push
                if board[to] is None:
                    if to // 8 == last_rank:
                        for pr in _PROMOTIONS:
                            add((sq, to, pr))
                    else:
                        add((sq, to, None))
                        if sq // 8 == start_rank and board[to + push] is None:
                            add((sq, to + push, None))
                for to in pawn_caps[sq]:
                    if board[to] in enemy:
                        if to // 8 == last_rank:
                            for pr in _PROMOTIONS:
                                add((sq, to, pr))
                        else:
                            add((sq, to, None))
                    elif to == ep:
                        add((sq, to, None))
            elif kind == 'n':
                for to in _KNIGHT[sq]:
                    q = board[to]
                    if q is None or q in enemy:
                        add((sq, to, None))
            elif kind == 'k':
                for to in _KING[sq]:
                    q = board[to]
                    if q is None or q in enemy:
                        add((sq, to, None))
            else:
                for ray in _SLIDER_RAYS[kind][sq]:
                    for to in ray:
                        q = board[to]
                        if q is None:
                            add((sq, to, None))
                        else:
                            if q in enemy:
                                add((sq, to, None))
                            break

        if in_check:
            return [m for m in moves if self._is_safe(m)]

        rights = self.castling
        if rights:
            attacked = self.is_attacked
            if us == WHITE:
                if ('K' in rights and board[5] is None and board[6] is None
                        and not attacked(5, them) and not attacked(6, them)):
                    add((4, 6, None))
                if ('Q' in rights and board[3] is None and board[2] is None and board[1] is None
                        and not attacked(3, them) and not attacked(2, them)):
                    add((4, 2, None))
            else:
                if ('k' in rights and board[61] is None and board[62] is None
                        and not attacked(61, them) and not attacked(62, them)):
                    add((60, 62, None))
                if ('q' in rights and board[59] is None and board[58] is None and board[57] is None
                        and not attacked(59, them) and not attacked(58, them)):
                    add((60, 58, None))

        pinned = self._pinned(ksq, us)
        legal: List[_MoveTuple] = []
        keep = legal.append
        for m in moves:
            frm, to, _ = m
            if frm == ksq:
                if to - frm in (2, -2) or not self.is_attacked(to, them):
                    keep(m)
            elif to == ep and _KIND[board[frm]] == 'p':
                if self._is_safe(m):
                    keep(m)
            elif frm in pinned:
                if to in pinned[frm]:
                    keep(m)
            else:
                keep(m)
        return legal

    def _perft(self, depth: int) -> int:
        moves = self._generate()
        if depth == 1:
            return len(moves)
        total = 0
        for m in moves:
            undo = self._make(m)
            total += self._perft(depth - 1)
            self._unmake(undo)
        return total

    def _legal_ep_square(self) -> Optional[int]:
        ep = self.ep_square
        if ep is None:
            return None
        pawn = 'P' if self.turn == WHITE else 'p'
        for frm in _PAWN_ATTACKS[_OPP[self.turn]][ep]:
            if self._board[frm] == pawn and self._is_safe((frm, ep, None)):
                return ep
        return None

    def _position_key(self) -> tuple:
        return (tuple(self._board), self.turn, self.castling, self._legal_ep_square())

    def legal_moves(self) -> List[Move]:
        """Return all fully legal moves (each promotion yields four moves)."""
        return [Move(*m) for m in self._generate()]

    def is_legal(self, move: Move) -> bool:
        """Return True if `move` is legal in the current position."""
        return _as_tuple(move) in self._generate()

    def _san(self, move: _MoveTuple, legal: List[_MoveTuple]) -> str:
        frm, to, promo = move
        board = self._board
        piece = board[frm]
        kind = _KIND[piece]
        if kind == 'k' and to - frm in (2, -2):
            san = "O-O" if to > frm else "O-O-O"
        elif kind == 'p':
            if frm % 8 != to % 8:
                san = FILE_NAMES[frm % 8] + 'x' + square_name(to)
            else:
                san = square_name(to)
            if promo:
                san += '=' + promo.upper()
        else:
            san = kind.upper()
            rivals = [f for f, t, _ in legal if t == to and f != frm and board[f] == piece]
            if rivals:
                if all(f % 8 != frm % 8 for f in rivals):
                    san += FILE_NAMES[frm % 8]
                elif all(f // 8 != frm // 8 for f in rivals):
                    san += RANK_NAMES[frm // 8]
                else:
                    san += square_name(frm)
            if board[to] is not None:
                san += 'x'
            san += square_name(to)
        undo = self._make(move)
        if self.is_check():
            san += '+' if self._generate() else '#'
        self._unmake(undo)
        return san

    def san(self, move: Move) -> str:
        """Return the SAN of a legal move in the current position."""
        m = _as_tuple(move)
        legal = self._generate()
        if m not in legal:
            raise IllegalMoveError(f"Illegal move: {_uci_text(move)}")
        return self._san(m, legal)

    def legal_moves_san(self) -> List[str]:
        """Return the SAN of every legal move, sorted."""
        legal = self._generate()
        return sorted(self._san(m, legal) for m in legal)

    def push(self, move: Move) -> None:
        """Play a legal move; raises IllegalMoveError otherwise."""
        if not isinstance(move, Move):
            raise TypeError("push() expects a Move; use push_san() for text")
        m = _as_tuple(move)
        legal = self._generate()
        if m not in legal:
            raise IllegalMoveError(f"Illegal move: {_uci_text(move)}")
        san = self._san(m, legal)
        self._undo.append(self._make(m))
        self.move_stack.append(Move(*m))
        self.san_stack.append(san)
        self._history.append(self._position_key())

    def pop(self) -> Move:
        """Undo the last move and return it; raises IndexError if there is none."""
        if not self._undo:
            raise IndexError("pop from empty move stack")
        self._unmake(self._undo.pop())
        self.san_stack.pop()
        self._history.pop()
        return self.move_stack.pop()

    def last_move(self) -> Optional[Move]:
        """Return the most recent move or None."""
        return self.move_stack[-1] if self.move_stack else None

    def parse_san(self, text: str) -> Move:
        """Parse (lenient) SAN into a legal move."""
        s = text.strip() if isinstance(text, str) else ''
        if not s:
            raise IllegalMoveError("Unrecognized move", kind='unrecognized')
        core = s.rstrip("+#!?")
        legal = self._generate()

        castle = core.replace('0', 'O').replace('o', 'O').replace('-', '')
        if castle in ("OO", "OOO"):
            frm = self._king[self.turn]
            to = frm + 2 if castle == "OO" else frm - 2
            m = (frm, to, None)
            if m in legal:
                return Move(*m)
            raise IllegalMoveError(f"Illegal move: {s}")

        match = _SAN_RE.match(core) or _SAN_RE.match(core.lower())
        if not match:
            raise IllegalMoveError(f"Unrecognized move: {s}", kind='unrecognized')
        piece_ch, file_ch, rank_ch, dest, promo = match.groups()
        to_sq = parse_square(dest)
        promo = promo.lower() if promo else None

        readings: List[Tuple[str, Optional[str], Optional[str]]] = []
        if piece_ch is None or piece_ch in "Pp":
            readings.append(('p', file_ch or dest[0], rank_ch))
        elif piece_ch == 'b' and file_ch is None:
            readings.append(('p', 'b', rank_ch))
            readings.append(('b', None, rank_ch))
        else:
            readings.append((piece_ch.lower(), file_ch, rank_ch))

        board = self._board
        candidates: List[_MoveTuple] = []
        for kind, f, r in readings:
            candidates = [
                m for m in legal
                if m[1] == to_sq and _KIND[board[m[0]]] == kind
                and (f is None or FILE_NAMES[m[0] % 8] == f)
                and (r is None or RANK_NAMES[m[0] // 8] == r)
                and (m[2] == (promo or 'q') if m[2] else promo is None)
            ]
            if candidates:
                break
        if not candidates:
            raise IllegalMoveError(f"Illegal move: {s}")
        if len(candidates) > 1:
            options = ", ".join(sorted(self._san(m, legal) for m in candidates))
            raise IllegalMoveError(f"Ambiguous move: {s} (could be {options})", kind='ambiguous')
        return Move(*candidates[0])

    def parse_move(self, text: str) -> Move:
        """Parse coordinate notation (e2e4, e2-e4, e7e8q, ...) or SAN into a legal move."""
        s = text.strip() if isinstance(text, str) else ''
        if not s:
            raise IllegalMoveError("Unrecognized move", kind='unrecognized')
        match = _COORD_RE.match(s.lower())
        if not match:
            return self.parse_san(s)
        frm, to = parse_square(match.group(1)), parse_square(match.group(2))
        promo = match.group(3)
        if promo is None and self._board[frm] in ('P', 'p') and to // 8 in (0, 7):
            promo = 'q'
        coord: Optional[Move] = Move(frm, to, promo) if (frm, to, promo) in self._generate() else None
        if s[0] != 'B':
            if coord is None:
                raise IllegalMoveError(f"Illegal move: {s}")
            return coord
        try:
            san_move: Optional[Move] = self.parse_san(s)
        except IllegalMoveError:
            san_move = None
        preferred, other = (san_move, coord) if s != s.upper() else (coord, san_move)
        move = preferred if preferred is not None else other
        if move is None:
            raise IllegalMoveError(f"Illegal move: {s}")
        return move

    def push_san(self, text: str) -> Move:
        """Parse `text` with parse_move, play it and return the move."""
        move = self.parse_move(text)
        self.push(move)
        return move

    def is_checkmate(self) -> bool:
        """Return True if the side to move is checkmated."""
        return self.is_check() and not self._generate()

    def is_stalemate(self) -> bool:
        """Return True if the side to move has no legal move and is not in check."""
        return not self.is_check() and not self._generate()

    def is_insufficient_material(self) -> bool:
        """Return True if neither side can possibly checkmate."""
        minors: List[Tuple[str, int]] = []
        for sq, p in enumerate(self._board):
            if p is None:
                continue
            kind = _KIND[p]
            if kind in ('p', 'r', 'q'):
                return False
            if kind != 'k':
                minors.append((kind, sq))
        if len(minors) <= 1:
            return True
        return (all(kind == 'b' for kind, _ in minors)
                and len({(sq % 8 + sq // 8) % 2 for _, sq in minors}) == 1)

    def has_insufficient_material(self, color: str) -> bool:
        """Return True if `color` has only a king, or a king and one knight or bishop."""
        own = [_KIND[p] for p in self._board
               if p is not None and _COLOR_OF[p] == color and _KIND[p] != 'k']
        return not own or (len(own) == 1 and own[0] in ('n', 'b'))

    def is_repetition(self, count: int = 3) -> bool:
        """Return True if the current position has occurred at least `count` times."""
        key = self._history[-1]
        window = self._history[-(self.halfmove_clock + 1):]
        return window.count(key) >= count

    def is_fifty_moves(self) -> bool:
        """Return True if 50 moves passed without a capture or pawn move."""
        return self.halfmove_clock >= 100

    def outcome(self) -> Optional[Outcome]:
        """Return the automatic game result, or None if the game continues."""
        if not self._generate():
            if self.is_check():
                return Outcome('checkmate', _OPP[self.turn])
            return Outcome('stalemate', None)
        if self.is_insufficient_material():
            return Outcome('insufficient_material', None)
        if self.is_repetition(3):
            return Outcome('threefold_repetition', None)
        if self.is_fifty_moves():
            return Outcome('fifty_moves', None)
        return None

    def is_game_over(self) -> bool:
        """Return True if outcome() is not None."""
        return self.outcome() is not None

    def captured_pieces(self, color: str) -> List[str]:
        """Return pieces of `color` captured so far, most valuable first."""
        taken = [u[3] for u in self._undo if u[3] is not None and _COLOR_OF[u[3]] == color]
        return sorted(taken, key=lambda p: _CAPTURE_ORDER[_KIND[p]])

    def material_balance(self) -> int:
        """Return White's material minus Black's."""
        total = 0
        for p in self._board:
            if p is not None:
                value = PIECE_VALUES[_KIND[p]]
                total += value if p.isupper() else -value
        return total

    def pgn(self, headers: Optional[Dict[str, str]] = None, result: str = '*') -> str:
        """Return the game as PGN text (ends with a newline)."""
        headers = dict(headers or {})
        tags: List[Tuple[str, str]] = []
        for name, default in _SEVEN_TAG_ROSTER:
            value = result if name == "Result" else headers.get(name, default)
            tags.append((name, str(value) if value not in (None, '') else default))
        special = {name for name, _ in _SEVEN_TAG_ROSTER}
        if self.root_fen != STARTING_FEN:
            tags.append(("SetUp", "1"))
            tags.append(("FEN", self.root_fen))
            special.update(("SetUp", "FEN"))
        tags.extend((k, str(v)) for k, v in headers.items() if k not in special)
        header_text = "\n".join(f'[{k} "{_pgn_escape(v)}"]' for k, v in tags)

        tokens: List[str] = []
        color, number = self._root_turn, self._root_fullmove
        for i, san in enumerate(self.san_stack):
            if color == WHITE:
                tokens.append(f"{number}.")
            elif i == 0:
                tokens.append(f"{number}...")
            tokens.append(san)
            if color == BLACK:
                number += 1
            color = _OPP[color]
        tokens.append(result)

        lines: List[str] = []
        line = ''
        for tok in tokens:
            if not line:
                line = tok
            elif len(line) + 1 + len(tok) <= 80:
                line += ' ' + tok
            else:
                lines.append(line)
                line = tok
        lines.append(line)
        return header_text + "\n\n" + "\n".join(lines) + "\n"


def _as_tuple(move: Move) -> _MoveTuple:
    promo = move.promotion.lower() if move.promotion else None
    return (move.from_sq, move.to_sq, promo)


def _uci_text(move: Move) -> str:
    try:
        return move.uci()
    except (ValueError, TypeError):
        return repr(move)


def perft(board: Board, depth: int) -> int:
    """Count the leaf nodes of the legal move tree `depth` plies deep."""
    if depth <= 0:
        return 1
    return board.copy()._perft(depth)

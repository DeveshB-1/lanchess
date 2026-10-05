"""Ctrl+Z undo shortcut, its status-bar hint, and the saved board size reaching CLI games."""

import unittest
from unittest import mock

from lanchess import cli, config, game, term, ui


class FakeReader:
    def __init__(self, keys):
        self.keys = list(keys)

    def read_key(self, timeout):
        return self.keys.pop(0) if self.keys else None


def local_session(**extra):
    return game.GameSession(mode="local", my_color=None, my_name="White", opponent_name="Black",
                            autosave=False, **extra)


class FakeConn:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class CtrlZKeyTests(unittest.TestCase):
    def test_parser_maps_ctrl_z(self):
        self.assertEqual(term.KeyParser().feed("\x1a"), [term.CTRL_Z])
        self.assertEqual(term.KeyParser().feed_bytes(b"e4\x1a"), ["e", "4", term.CTRL_Z])
        self.assertEqual(term.WinKeyParser().feed("\x1a"), [term.CTRL_Z])  # msvcrt.getwch()

    def test_line_editor_reports_undo_and_keeps_the_buffer(self):
        editor = term.LineEditor(FakeReader(["N", "f", term.CTRL_Z]))
        editor.poll(0)
        editor.poll(0)
        self.assertEqual(editor.poll(0), ("undo",))
        self.assertEqual(editor.buffer, "Nf")


class UndoEventTests(unittest.TestCase):
    def test_ctrl_z_undoes_a_local_move(self):
        session = local_session()
        session.handle_input("e4")
        session.handle_input("e5")
        self.assertTrue(game._editor_event(session, ("undo",), FakeReader([]), (120, 40)))
        self.assertEqual(session.board.san_stack, ["e4"])
        game._editor_event(session, ("undo",), FakeReader([]), (120, 40))
        self.assertEqual(session.board.san_stack, [])

    def test_ctrl_z_asks_the_opponent_in_a_network_game(self):
        conn = FakeConn()
        session = game.GameSession(mode="network", my_color="w", my_name="Al", opponent_name="Bo",
                                   conn=conn, autosave=False)
        session.handle_input("e4")
        game._editor_event(session, ("undo",), FakeReader([]), (120, 40))
        self.assertEqual(conn.sent[-1]["type"], "takeback_request")


class UndoHintTests(unittest.TestCase):
    def test_hint_only_in_full_screen_and_after_a_move(self):
        session = local_session()
        session.handle_input("e4")
        self.assertNotIn("Ctrl+Z", session.status_text())  # plain mode: Ctrl+Z is not a key there
        session.set_screen((120, 40), ui.Theme())
        self.assertIn("Ctrl+Z undo", session.status_text())
        session.handle_input("/undo")
        self.assertNotIn("Ctrl+Z", session.status_text())  # nothing left to undo

    def test_network_hint_needs_a_move_of_mine(self):
        session = game.GameSession(mode="network", my_color="b", my_name="Bo", opponent_name="Al",
                                   conn=FakeConn(), autosave=False)
        session.set_screen((120, 40), ui.Theme())
        session.handle_message({"type": "move", "uci": "e2e4", "ply": 1, "clock_ms": None})
        self.assertNotIn("Ctrl+Z", session.status_text())  # Black has not moved yet
        session.handle_input("e5")
        self.assertIn("Ctrl+Z undo", session.status_text())

    def test_help_mentions_ctrl_z(self):
        self.assertTrue(any("Ctrl+Z" in line for line in ui.help_lines()))


class SavedBoardSizeTests(unittest.TestCase):
    def test_subcommand_games_use_the_saved_board_size(self):
        settings = config.Settings(board_size="large")
        args = cli.parse_args(["local", "--plain", "--no-save"], settings)
        options = cli.Options.from_args(args, settings)
        with mock.patch.object(cli.game, "GameSession", wraps=game.GameSession) as made, \
                mock.patch.object(cli, "_play", return_value=0):
            self.assertEqual(cli.run_local(args, options, settings), 0)
        self.assertEqual(made.call_args.kwargs["board_size"], "large")

    def test_no_settings_means_auto(self):
        self.assertEqual(cli._board_size(None), "auto")


if __name__ == "__main__":
    unittest.main()

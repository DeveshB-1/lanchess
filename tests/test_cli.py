"""Tests for lanchess.cli: argument parsing, friendly errors, and end-to-end runs in subprocesses."""

from __future__ import annotations

import contextlib
import errno
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Iterator, List, Optional, Tuple
from unittest import mock

from lanchess import __version__, cli, config, net
from lanchess.game import TimeControl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 30

_CONFIG_DIR: Optional[tempfile.TemporaryDirectory] = None
_ENV_PATCH: Any = None


def setUpModule() -> None:
    """Never read or write the real settings: point LANCHESS_CONFIG_DIR at an empty folder."""
    global _CONFIG_DIR, _ENV_PATCH
    _CONFIG_DIR = tempfile.TemporaryDirectory(prefix="lanchess-cli-config-")
    _ENV_PATCH = mock.patch.dict(os.environ, {config.ENV_DIR: _CONFIG_DIR.name})
    _ENV_PATCH.start()


def tearDownModule() -> None:
    if _ENV_PATCH is not None:
        _ENV_PATCH.stop()
    if _CONFIG_DIR is not None:
        _CONFIG_DIR.cleanup()


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def child_env(home: str) -> dict:
    env = dict(os.environ)
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HOME=home, USERPROFILE=home, NO_COLOR="1")
    env[config.ENV_DIR] = os.path.join(home, "config")
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


@contextlib.contextmanager
def captured() -> Iterator[Tuple[io.StringIO, io.StringIO]]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class ParserTests(unittest.TestCase):
    def test_host_defaults(self) -> None:
        args = cli.parse_args(["host"])
        self.assertEqual(args.command, "host")
        self.assertEqual(args.port, net.DEFAULT_PORT)
        self.assertEqual(args.color, "white")
        self.assertIsNone(args.time)
        self.assertIsNone(args.fen)
        self.assertIsNone(args.name)
        self.assertFalse(args.no_discovery)
        self.assertEqual(cli.Options.from_args(args), cli.Options())

    def test_host_options(self) -> None:
        args = cli.parse_args(["host", "--port", "6000", "--name", "Al", "--color", "random", "--time", "5+3",
                               "--fen", "4k3/8/8/8/8/8/8/4K3 w - - 0 1", "--no-discovery"])
        self.assertEqual(args.port, 6000)
        self.assertEqual(args.name, "Al")
        self.assertEqual(args.color, "random")
        self.assertEqual(args.time, TimeControl(300000, 3000))
        self.assertEqual(args.fen, "4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        self.assertTrue(args.no_discovery)
        self.assertEqual(cli.parse_args(["host", "--color", "b"]).color, "black")
        self.assertIsNone(cli.parse_args(["host", "--time", "none"]).time)

    def test_join_and_local(self) -> None:
        args = cli.parse_args(["join"])
        self.assertIsNone(args.host)
        self.assertEqual(args.scan_time, 2.5)
        args = cli.parse_args(["join", "10.0.0.5:6000", "--name", "Bob", "--scan-time", "1"])
        self.assertEqual((args.host, args.name, args.scan_time), ("10.0.0.5:6000", "Bob", 1.0))
        args = cli.parse_args(["local", "--time", "10", "--no-flip"])
        self.assertEqual(args.time, TimeControl(600000, 0))
        self.assertTrue(args.no_flip)

    def test_common_options_before_or_after_the_command(self) -> None:
        for argv in (["--plain", "--ascii", "local", "--no-color", "--pgn-dir", "games", "--no-save"],
                     ["local", "--plain", "--ascii", "--no-color", "--pgn-dir", "games", "--no-save"],
                     ["--plain", "--ascii", "--no-color", "--pgn-dir", "games", "--no-save", "local"]):
            with self.subTest(argv=argv):
                options = cli.Options.from_args(cli.parse_args(argv))
                self.assertEqual(options, cli.Options(ascii=True, no_color=True, plain=True,
                                                      pgn_dir="games", no_save=True))

    def test_invalid_arguments_exit_2_with_message(self) -> None:
        cases = [
            (["host", "--time", "abc"], "Invalid time control"),
            (["host", "--port", "70000"], "invalid port"),
            (["host", "--port", "x"], "invalid port"),
            (["host", "--color", "green"], "invalid colour"),
            (["local", "--fen", "not a fen"], "Invalid FEN"),
            (["join", "--scan-time", "-1"], "scan time"),
            (["fly"], "invalid choice"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                with captured() as (out, err):
                    self.assertEqual(cli.main(argv), 2)
                self.assertIn(message, err.getvalue())

    def test_version_and_help(self) -> None:
        with captured() as (out, _err):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), f"lanchess {__version__}")
        with captured() as (out, _err):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("{host,join,local}", out.getvalue())
        self.assertIn("examples:", out.getvalue())
        self.assertIn("open the menu", out.getvalue())

    def test_no_command_without_a_terminal_opens_the_text_menu(self) -> None:
        for argv in ([], ["--plain"], ["--ascii", "--no-color"]):
            with self.subTest(argv=argv):
                with mock.patch.object(sys, "stdin", io.StringIO("")), captured() as (out, err):
                    self.assertEqual(cli.main(argv), 0)  # end of input: leave quietly
                self.assertIn("1) Host a game", out.getvalue())
                self.assertIn("6) Quit", out.getvalue())
                self.assertNotIn("Traceback", err.getvalue())
        with mock.patch.object(sys, "stdin", io.StringIO("6\n")), captured() as (out, _err):
            self.assertEqual(cli.main([]), 0)
        self.assertEqual(out.getvalue().count("1) Host a game"), 1)

    def test_no_command_passes_the_typed_flags_to_the_menu(self) -> None:
        with mock.patch("lanchess.menu.run_menu", return_value=0) as run_menu:
            self.assertEqual(cli.main(["--ascii", "--no-save"]), 0)
        explicit, settings = run_menu.call_args[0]
        self.assertEqual(explicit, {"ascii": True, "no_save": True})
        self.assertIsInstance(settings, config.Settings)


class SavedSettingsTests(unittest.TestCase):
    SAVED = config.Settings(name="Saved", time_control="5+3", host_color="black", port=6100,
                            piece_style="ascii", colors=False, flip_local=False, autosave=False,
                            pgn_dir="/saved/games")

    def test_subcommands_use_saved_defaults(self) -> None:
        args = cli.parse_args(["host"], self.SAVED)
        self.assertEqual((args.port, args.color, args.time), (6100, "black", TimeControl(300000, 3000)))
        self.assertEqual(cli.parse_args(["join"], self.SAVED).port, 6100)
        local = cli.parse_args(["local"], self.SAVED)
        self.assertEqual((local.time, local.no_flip), (TimeControl(300000, 3000), True))
        options = cli.Options.from_args(args, self.SAVED)
        self.assertEqual(options, cli.Options(ascii=True, no_color=True, plain=False, pgn_dir="/saved/games",
                                              no_save=True))
        self.assertEqual(cli._player_name(None, self.SAVED.name), "Saved")

    def test_command_line_flags_win(self) -> None:
        args = cli.parse_args(["host", "--port", "7000", "--color", "white", "--time", "none",
                               "--name", "Typed", "--pgn-dir", "/typed"], self.SAVED)
        self.assertEqual((args.port, args.color, args.time), (7000, "white", None))
        self.assertEqual(cli._player_name(args.name, self.SAVED.name), "Typed")
        self.assertEqual(cli.Options.from_args(args, self.SAVED).pgn_dir, "/typed")
        self.assertEqual(cli.Options.resolve({"ascii": True}, config.Settings()).ascii, True)
        self.assertEqual(cli.Options.resolve({}, config.Settings()), cli.Options())

    def test_main_reads_the_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {config.ENV_DIR: folder}):
            config.save(self.SAVED, None)
            run_local = mock.Mock(return_value=0)
            with mock.patch.dict(cli._RUNNERS, {"local": run_local}):
                self.assertEqual(cli.main(["local"]), 0)
            args, options, settings = run_local.call_args[0]
            self.assertEqual(args.time, TimeControl(300000, 3000))
            self.assertTrue(options.no_save and options.ascii)
            self.assertEqual(settings.name, "Saved")
            with tempfile.TemporaryDirectory() as other, mock.patch.dict(os.environ, {config.ENV_DIR: other}):
                run_local.reset_mock()
                with mock.patch.dict(cli._RUNNERS, {"local": run_local}):
                    cli.main(["local"])
                self.assertIsNone(run_local.call_args[0][0].time)  # another folder: defaults


class FriendlyErrorTests(unittest.TestCase):
    def test_connection_refused(self) -> None:
        port = free_tcp_port()
        with captured() as (out, err):
            code = cli.main(["join", f"127.0.0.1:{port}", "--plain"])
        self.assertEqual(code, 1)
        self.assertIn("connection refused", err.getvalue())
        self.assertIn("Start hosting on the other computer", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_port_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.bind(("0.0.0.0", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            with captured() as (out, err):
                code = cli.main(["host", "--port", str(port), "--plain", "--no-discovery"])
        self.assertEqual(code, 1)
        self.assertIn(f"port {port} is already in use", err.getvalue())
        self.assertIn("--port", err.getvalue())

    def test_bad_join_address(self) -> None:
        with captured() as (out, err):
            self.assertEqual(cli.main(["join", "10.0.0.5:99999", "--plain"]), 1)
        self.assertIn("Invalid port", err.getvalue())

    def test_error_messages(self) -> None:
        message, hints = cli.connect_error(socket.timeout("timed out"), "10.0.0.5", 5555)
        self.assertIn("timed out", message)
        self.assertIn("TCP 5555", " ".join(hints))
        self.assertIn("UDP 5556", " ".join(hints))
        message, _ = cli.connect_error(socket.gaierror(-2, "Name or service not known"), "nohost", 5555)
        self.assertIn("cannot find", message)
        message, _ = cli.connect_error(OSError(errno.EHOSTUNREACH, "No route to host"), "10.0.0.5", 5555)
        self.assertIn("unreachable", message)
        message, hints = cli.server_error(OSError(errno.EADDRINUSE, "Address already in use"), 5555)
        self.assertIn("already in use", message)
        message, _ = cli.server_error(OSError(errno.EACCES, "Permission denied"), 80)
        self.assertIn("not allowed", message)

    def test_port_advice_fits_where_the_port_is_chosen(self) -> None:
        in_use, denied = OSError(errno.EADDRINUSE, "Address already in use"), OSError(errno.EACCES, "Permission denied")
        self.assertEqual(cli.server_error(in_use, 5555)[1],
                         ["Host on a different port, for example: --port 6000, and join with HOST:6000."])
        self.assertEqual(cli.server_error(denied, 80)[1],
                         ["Ports below 1024 need administrator rights; try --port 5555."])
        self.assertEqual(cli.server_error(in_use, 6000, advice="form")[1],
                         ["Go back and pick another port, for example 6001; the other player then joins IP:6001."])
        self.assertEqual(cli.server_error(denied, 80, advice="form")[1],
                         ["Ports below 1024 need administrator rights; go back and pick another port, "
                          "for example 5555."])
        self.assertEqual(cli.server_error(in_use, 5555, advice="settings")[1],
                         ["Change the port in Settings, for example to 6000; the other player then joins IP:6000."])
        self.assertEqual(cli.server_error(denied, 80, advice="settings")[1],
                         ["Ports below 1024 need administrator rights; change the port in Settings, "
                          "for example to 5555."])
        for advice in ("flag", "form", "settings"):  # a refused port of 1024 or more is not about admin rights
            hints = " ".join(cli.server_error(denied, 5555, advice=advice)[1])
            self.assertNotIn("below 1024", hints)
            self.assertIn("6000", hints)
        message, hints = cli.server_error(OSError(errno.EINVAL, "Invalid argument"), 5555, advice="form")
        self.assertEqual((message, hints), ("could not listen on port 5555: Invalid argument", []))

    def test_ctrl_c_while_joining_stops_cleanly(self) -> None:
        searching = cli.parse_args(["join", "--plain"])
        with mock.patch.object(cli, "choose_host", side_effect=KeyboardInterrupt), captured() as (out, err):
            self.assertEqual(cli.run_join(searching, cli.Options(plain=True)), 130)
        self.assertIn("Stopped searching. No game was played.", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        direct = cli.parse_args(["join", "10.0.0.9", "--plain"])
        with mock.patch.object(cli, "join_host", side_effect=KeyboardInterrupt), captured() as (out, err):
            self.assertEqual(cli.run_join(direct, cli.Options(plain=True)), 130)
        self.assertIn("Connecting to 10.0.0.9:5555", out.getvalue())
        self.assertIn("Stopped connecting. No game was played.", out.getvalue())

    def test_launch_command(self) -> None:
        python = "py" if os.name == "nt" else "python3"
        cases = {"/x/dist/lanchess.pyz": f"{python} lanchess.pyz", "/x/lanchess/__main__.py": f"{python} -m lanchess",
                 "play.py": f"{python} play.py", "/usr/bin/lanchess": "lanchess"}
        for argv0, expected in cases.items():
            with self.subTest(argv0=argv0), mock.patch.object(sys, "argv", [argv0]):
                self.assertEqual(cli.launch_command(), expected)


class ChooseHostTests(unittest.TestCase):
    HOSTS = [net.HostInfo("Alice", "192.168.1.20", 5555), net.HostInfo("Carol", "192.168.1.30", 6000)]

    def choose(self, answers: List[Optional[str]], results: List[List[net.HostInfo]]) -> Any:
        answers, results = list(answers), list(results)

        def discover(timeout: float, extra_targets: Any = ()) -> List[net.HostInfo]:
            self.assertEqual(tuple(extra_targets), ("127.0.0.1", "127.255.255.255"))
            return results.pop(0) if results else []

        with captured() as (out, _err):
            choice = cli.choose_host(cli.ui.Theme(), 0.1, 5555, discover=discover, ask=lambda prompt: answers.pop(0))
        self.output = out.getvalue()
        return choice

    def test_pick_by_number_default_and_address(self) -> None:
        self.assertEqual(self.choose([""], [self.HOSTS]), ("192.168.1.20", 5555))
        self.assertIn("1) Alice", self.output)
        self.assertIn("2) Carol", self.output)
        self.assertEqual(self.choose(["2"], [self.HOSTS]), ("192.168.1.30", 6000))
        self.assertEqual(self.choose(["7", "1"], [self.HOSTS, self.HOSTS]), ("192.168.1.20", 5555))
        self.assertIn("no game number 7", self.output)
        self.assertEqual(self.choose(["10.0.0.9:7000"], [self.HOSTS]), ("10.0.0.9", 7000))

    def test_no_hosts_then_type_ip(self) -> None:
        self.assertEqual(self.choose(["", "192.168.1.5"], [[], []]), ("192.168.1.5", 5555))
        self.assertIn("No games found", self.output)
        self.assertEqual(self.output.count("Searching"), 2)

    def test_loopback_duplicates_are_dropped(self) -> None:
        same_host = [net.HostInfo("Alice", "127.0.0.1", 5555), net.HostInfo("Alice", "192.168.1.20", 5555),
                     net.HostInfo("Dave", "127.0.0.1", 7000)]
        self.assertEqual(self.choose(["2"], [same_host]), ("127.0.0.1", 7000))
        self.assertIn("1) Alice", self.output)
        self.assertIn("2) Dave", self.output)
        self.assertIn("(this computer)", self.output)
        self.assertNotIn("3)", self.output)

    def test_give_up(self) -> None:
        self.assertIsNone(self.choose([None], [[]]))
        self.assertIsNone(self.choose(["q"], [self.HOSTS]))


class LoopbackDiscoveryTests(unittest.TestCase):
    """Several games hosted on one computer share the discovery port; the search must find them all."""

    @unittest.skipUnless(sys.platform.startswith("linux"), "the loopback broadcast address is Linux behaviour")
    def test_every_game_hosted_on_this_computer_is_found(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("", 0))
            udp = probe.getsockname()[1]
        alice, carol = net.DiscoveryResponder("Alice", 6001, udp), net.DiscoveryResponder("Carol", 5555, udp)
        self.assertTrue(alice.start())
        self.addCleanup(alice.stop)
        self.assertTrue(carol.start())  # (SO_REUSEADDR: both bind the same UDP port)
        self.addCleanup(carol.stop)
        found = net.discover_hosts(0.6, udp, extra_targets=cli.LOOPBACK_TARGETS)
        here = {(host.name, host.port) for host in found if host.address == cli.LOOPBACK}
        if not here:
            self.skipTest("this system does not deliver the loopback broadcast")
        self.assertEqual(here, {("Alice", 6001), ("Carol", 5555)})  # 127.0.0.1 alone reaches only one


class Child:
    """A subprocess whose stdout is collected by a thread, so tests can wait for text."""

    def __init__(self, args: List[str], env: dict, cwd: str) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "lanchess"] + args, cwd=cwd, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.chunks: List[str] = []
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            with self.lock:
                self.chunks.append(raw.decode("utf-8", "replace"))

    @property
    def output(self) -> str:
        with self.lock:
            return "".join(self.chunks)

    def wait_for(self, text: str, timeout: float = TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while text not in self.output:
            if time.monotonic() > deadline or (self.proc.poll() is not None and text not in self.output):
                time.sleep(0.1)
                if text in self.output:
                    return
                raise AssertionError(f"{text!r} did not appear; output so far:\n{self.output}")
            time.sleep(0.02)

    def send(self, line: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def finish(self, timeout: float = TIMEOUT) -> int:
        try:
            code = self.proc.wait(timeout)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream is not None:
                    stream.close()
            self.thread.join(5)
        return code


class SubprocessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanchess-cli-")
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.env = child_env(self.tmp)

    def run_cli(self, args: List[str], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "lanchess"] + args, input=stdin.encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT, env=self.env,
                              timeout=TIMEOUT)

    def test_version(self) -> None:
        result = self.run_cli(["--version"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode().strip(), f"lanchess {__version__}")

    def test_menu_without_a_terminal_and_settings_persist(self) -> None:
        result = self.run_cli([], "")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("1) Host a game", result.stdout.decode("utf-8"))
        result = self.run_cli([], "4\n1\nAlice\n\n6\n")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("Saved.", result.stdout.decode("utf-8"))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "config", "config.json")))
        result = self.run_cli(["--plain"], "4\n\n6\n")
        self.assertIn("Your name: Alice", result.stdout.decode("utf-8"))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.decode(), "")

    def test_local_game_from_the_text_menu(self) -> None:
        result = self.run_cli(["--no-save"], "3\n\nf3\ne5\ng4\nQh4#\n/quit\n6\n")
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("Result: Checkmate — Black wins (0-1)", out)
        self.assertGreaterEqual(out.count("1) Host a game"), 2)
        self.assertNotIn("\x1b", out)

    def test_local_plain_fools_mate_from_piped_stdin(self) -> None:
        games = os.path.join(self.tmp, "games")
        result = self.run_cli(["local", "--plain", "--pgn-dir", games], "f3\ne5\ng4\nQh4#\n/quit\n")
        out = result.stdout.decode("utf-8")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("Checkmate — Black wins (0-1)", out)
        self.assertIn("Last move: 2...Qh4#", out)
        self.assertIn("Result: Checkmate — Black wins (0-1)", out)
        self.assertNotIn("\x1b", out)
        saved = os.listdir(games)
        self.assertEqual(len(saved), 1)
        with open(os.path.join(games, saved[0]), encoding="utf-8") as handle:
            self.assertIn("1. f3 e5 2. g4 Qh4# 0-1", handle.read())

    def test_local_plain_without_stdin_input_exits(self) -> None:
        result = self.run_cli(["local", "--no-save"], "")
        self.assertEqual(result.returncode, 0)
        self.assertIn("White to move", result.stdout.decode("utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_ctrl_c_in_plain_local_game_quits_cleanly(self) -> None:
        child = Child(["local", "--plain", "--no-save"], self.env, ROOT)
        try:
            child.wait_for("-- White to move --")
            child.send("e4")
            child.wait_for("Last move: 1.e4")
            child.proc.send_signal(signal.SIGINT)
            self.assertEqual(child.finish(), 0)
        finally:
            if child.proc.poll() is None:
                child.proc.kill()
                child.finish()
        self.assertIn("The game was left unfinished.", child.output)
        self.assertNotIn("Traceback", child.output)

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_ctrl_c_while_hosting_stops_waiting(self) -> None:
        child = Child(["host", "--port", str(free_tcp_port()), "--plain", "--no-discovery"], self.env, ROOT)
        try:
            child.wait_for("Press Ctrl-C to stop waiting.")
            child.proc.send_signal(signal.SIGINT)
            self.assertEqual(child.finish(), 130)
        finally:
            if child.proc.poll() is None:
                child.proc.kill()
                child.finish()
        self.assertIn("Stopped waiting. No game was played.", child.output)
        self.assertNotIn("Traceback", child.output)

    def test_host_and_join_play_a_game(self) -> None:
        port = free_tcp_port()
        host = Child(["host", "--port", str(port), "--name", "Alice", "--plain", "--no-discovery",
                      "--time", "5+0", "--pgn-dir", os.path.join(self.tmp, "a")], self.env, ROOT)
        joiner: Optional[Child] = None
        try:
            host.wait_for("Waiting for an opponent")
            self.assertIn(f"Waiting for an opponent on TCP port {port}.", host.output)
            self.assertIn(" join ", host.output)
            joiner = Child(["join", f"127.0.0.1:{port}", "--name", "Bob", "--plain",
                            "--pgn-dir", os.path.join(self.tmp, "b")], self.env, ROOT)
            host.wait_for("Bob joined from 127.0.0.1:")
            joiner.wait_for("Joined Alice's game. You play Black, time control 5+0.")
            joiner.wait_for("-- Waiting for Alice to move… --")
            host.send("f3")
            joiner.wait_for("Last move: 1.f3")
            joiner.send("e5")
            host.wait_for("Last move: 1...e5")
            host.send("g4")
            joiner.wait_for("Last move: 2.g4")
            joiner.send("Qh4#")
            for child in (host, joiner):
                child.wait_for("Checkmate — Black wins (0-1)")
            host.send("/c good game")
            joiner.wait_for("Alice: good game")
            host.send("/quit")
            self.assertEqual(host.finish(), 0)
            joiner.wait_for("Alice left.")
            joiner.send("/quit")
            self.assertEqual(joiner.finish(), 0)
        finally:
            for child in (host, joiner):
                if child is not None and child.proc.poll() is None:
                    child.proc.kill()
                    child.finish()
        self.assertIn("You lost.", host.output)
        self.assertIn("You won!", joiner.output)
        for folder in ("a", "b"):
            self.assertEqual(len(os.listdir(os.path.join(self.tmp, folder))), 1)


if __name__ == "__main__":
    unittest.main()

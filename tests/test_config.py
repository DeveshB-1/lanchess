"""Tests for lanchess.config: where settings live, validation, atomic saving and recent hosts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from lanchess import config, net
from lanchess.config import Settings
from lanchess.game import TimeControl


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanchess-config-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "sub", config.FILENAME)

    def write_raw(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)


class LocationTests(unittest.TestCase):
    HOME = os.path.join(os.sep, "home", "alice")

    def test_environment_override_wins_everywhere(self) -> None:
        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform):
                env = {config.ENV_DIR: "/srv/lc", "APPDATA": r"C:\Users\a\AppData\Roaming",
                       "XDG_CONFIG_HOME": "/xdg"}
                self.assertEqual(config.config_dir(env, platform, self.HOME), "/srv/lc")
        self.assertEqual(config.config_dir({config.ENV_DIR: "~/lc"}, "linux", self.HOME),
                         os.path.expanduser("~/lc"))

    def test_platform_defaults(self) -> None:
        self.assertEqual(config.config_dir({}, "linux", self.HOME), os.path.join(self.HOME, ".config", "lanchess"))
        xdg = os.path.abspath(os.path.join(os.sep, "xdg"))
        self.assertEqual(config.config_dir({"XDG_CONFIG_HOME": xdg}, "linux", self.HOME),
                         os.path.join(xdg, "lanchess"))
        # A relative XDG_CONFIG_HOME is invalid by the spec and ignored.
        self.assertEqual(config.config_dir({"XDG_CONFIG_HOME": "rel"}, "linux", self.HOME),
                         os.path.join(self.HOME, ".config", "lanchess"))
        self.assertEqual(config.config_dir({}, "darwin", self.HOME),
                         os.path.join(self.HOME, "Library", "Application Support", "lanchess"))
        self.assertEqual(config.config_dir({"APPDATA": r"C:\Roaming"}, "win32", self.HOME),
                         os.path.join(r"C:\Roaming", "lanchess"))
        self.assertEqual(config.config_dir({}, "win32", self.HOME),
                         os.path.join(self.HOME, "AppData", "Roaming", "lanchess"))
        self.assertEqual(config.config_path({config.ENV_DIR: "/srv/lc"}, "linux", self.HOME),
                         os.path.join("/srv/lc", "config.json"))

    def test_real_environment_is_used_by_default(self) -> None:
        with mock.patch.dict(os.environ, {config.ENV_DIR: "/tmp/somewhere"}):
            self.assertEqual(config.config_path(), os.path.join("/tmp/somewhere", "config.json"))


class LoadSaveTests(TempDirTestCase):
    def test_missing_file_gives_defaults(self) -> None:
        settings = config.load(self.path)
        self.assertEqual(settings, Settings())
        self.assertEqual(settings.port, net.DEFAULT_PORT)
        self.assertEqual(settings.host_color, "white")
        self.assertIsNone(settings.time)
        self.assertTrue(settings.colors and settings.autosave and settings.flip_local)

    def test_round_trip(self) -> None:
        original = Settings(name="Alice", time_control="5+3", host_color="random", port=6001,
                            piece_style="ascii", colors=False, flip_local=False, autosave=False,
                            pgn_dir="/games", recent_hosts=["10.0.0.5:5555", "[fe80::1]:6000"])
        self.assertEqual(config.save(original, self.path), self.path)
        loaded = config.load(self.path)
        self.assertEqual(loaded, original)
        self.assertEqual(loaded.time, TimeControl(300000, 3000))
        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["name"], "Alice")
        self.assertEqual(data["recent_hosts"], ["10.0.0.5:5555", "[fe80::1]:6000"])

    def test_corrupt_or_wrong_type_file_gives_defaults(self) -> None:
        for text in ("{not json", "", "[1, 2, 3]", "null", "42", '"text"', "[" * 100000):
            with self.subTest(text=text[:20]):
                self.write_raw(text)
                self.assertEqual(config.load(self.path), Settings())

    def test_invalid_values_fall_back_one_by_one(self) -> None:
        self.write_raw(json.dumps({
            "name": "Bob\x1b[31m", "time_control": "fast", "host_color": "green", "port": 70000,
            "piece_style": "emoji", "colors": "yes", "flip_local": 0, "autosave": None,
            "pgn_dir": "a\nb", "recent_hosts": ["10.0.0.1:99999", 5, "10.0.0.2", "10.0.0.2:5555"],
            "unknown_key": 1,
        }))
        loaded = config.load(self.path)
        self.assertNotIn("\x1b", loaded.name)
        self.assertTrue(loaded.name.startswith("Bob"))
        defaults = Settings()
        for name in ("time_control", "host_color", "port", "piece_style", "colors", "flip_local",
                     "autosave", "pgn_dir"):
            self.assertEqual(getattr(loaded, name), getattr(defaults, name), name)
        self.assertEqual(loaded.recent_hosts, ["10.0.0.2:5555"])  # invalid dropped, duplicate merged

    def test_values_are_normalised(self) -> None:
        self.write_raw(json.dumps({"time_control": "10", "host_color": " Black ", "port": True,
                                   "piece_style": "ASCII"}))
        loaded = config.load(self.path)
        self.assertEqual(loaded.time_control, "10+0")
        self.assertEqual(loaded.host_color, "black")
        self.assertEqual(loaded.port, net.DEFAULT_PORT)  # booleans are not ports
        self.assertEqual(loaded.piece_style, "ascii")
        self.write_raw(json.dumps({"time_control": "none"}))
        self.assertEqual(config.load(self.path).time_control, "")

    def test_save_is_atomic_and_leaves_no_temp_files(self) -> None:
        config.save(Settings(name="First"), self.path)
        folder = os.path.dirname(self.path)
        with mock.patch.object(config.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                config.save(Settings(name="Second"), self.path)
        self.assertEqual(config.load(self.path).name, "First")  # the old file is intact
        self.assertEqual(os.listdir(folder), [config.FILENAME])  # the temporary file was removed
        config.save(Settings(name="Third"), self.path)
        self.assertEqual(config.load(self.path).name, "Third")
        self.assertEqual(os.listdir(folder), [config.FILENAME])

    def test_save_validates_what_it_writes(self) -> None:
        bad = Settings(port=0, host_color="purple", recent_hosts=["nonsense:port"])
        config.save(bad, self.path)
        loaded = config.load(self.path)
        self.assertEqual((loaded.port, loaded.host_color, loaded.recent_hosts), (net.DEFAULT_PORT, "white", []))

    def test_default_path_follows_environment(self) -> None:
        with mock.patch.dict(os.environ, {config.ENV_DIR: self.dir}):
            config.save(Settings(name="Env"), None)
            self.assertTrue(os.path.exists(os.path.join(self.dir, config.FILENAME)))
            self.assertEqual(config.load().name, "Env")

    def test_copy_is_independent(self) -> None:
        settings = Settings(recent_hosts=["10.0.0.1:5555"])
        copy = settings.copy()
        copy.recent_hosts.append("10.0.0.2:5555")
        self.assertEqual(settings.recent_hosts, ["10.0.0.1:5555"])


class RecentHostTests(TempDirTestCase):
    def test_most_recent_first_without_duplicates_and_at_most_five(self) -> None:
        settings = Settings()
        for number in range(1, 8):
            settings.remember_host(f"10.0.0.{number}", 5555)
        self.assertEqual(len(settings.recent_hosts), config.MAX_RECENT)
        self.assertEqual(settings.recent_hosts[0], "10.0.0.7:5555")
        self.assertEqual(settings.recent_hosts[-1], "10.0.0.3:5555")
        settings.remember_host("10.0.0.5", 5555)
        self.assertEqual(settings.recent_hosts[:2], ["10.0.0.5:5555", "10.0.0.7:5555"])
        self.assertEqual(len(set(settings.recent_hosts)), config.MAX_RECENT)
        settings.remember_host("fe80::1", 6000)
        self.assertEqual(settings.recent_hosts[0], "[fe80::1]:6000")
        self.assertEqual(net.parse_host_port(settings.recent_hosts[0]), ("fe80::1", 6000))

    def test_remember_host_updates_the_file(self) -> None:
        config.save(Settings(name="Keep"), self.path)
        self.assertTrue(config.remember_host("192.168.1.20", 5555, self.path))
        self.assertTrue(config.remember_host("192.168.1.30", 6000, self.path))
        loaded = config.load(self.path)
        self.assertEqual(loaded.name, "Keep")
        self.assertEqual(loaded.recent_hosts, ["192.168.1.30:6000", "192.168.1.20:5555"])

    def test_remember_host_failure_is_silent(self) -> None:
        blocker = os.path.join(self.dir, "a-file")
        with open(blocker, "w") as handle:
            handle.write("x")
        self.assertFalse(config.remember_host("10.0.0.1", 5555, os.path.join(blocker, "config.json")))


if __name__ == "__main__":
    unittest.main()

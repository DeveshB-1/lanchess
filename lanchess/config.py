"""Saved settings for LAN Chess: a small JSON file in the user's configuration folder.

Location: ``$LANCHESS_CONFIG_DIR`` if set; otherwise ``%APPDATA%\\lanchess`` on Windows,
``~/Library/Application Support/lanchess`` on macOS and ``$XDG_CONFIG_HOME/lanchess`` (default
``~/.config/lanchess``) elsewhere. The file is ``config.json``.

Loading never fails: a missing or corrupt file, unknown keys and invalid values all fall back to
the defaults. Saving writes a temporary file next to the real one and renames it into place, so an
interrupted save never leaves a half-written file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import net
from .game import TimeControl, parse_time_control

__all__ = ["ENV_DIR", "FILENAME", "MAX_RECENT", "COLORS", "PIECE_STYLES", "Settings",
           "config_dir", "config_path", "load", "save", "remember_host", "format_host"]

ENV_DIR = "LANCHESS_CONFIG_DIR"
FILENAME = "config.json"
MAX_RECENT = 5
MAX_PATH_LEN = 1024
COLORS = ("white", "black", "random")
PIECE_STYLES = ("unicode", "ascii")


def format_host(host: str, port: int) -> str:
    """``host:port`` (IPv6 literals in brackets), the form parse_host_port reads back."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _text(value: Any, default: str, limit: int = MAX_PATH_LEN) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ch in value for ch in "\0\r\n"):
        return default
    return value.strip()


def _choice(value: Any, choices: Tuple[str, ...], default: str) -> str:
    if isinstance(value, str) and value.strip().lower() in choices:
        return value.strip().lower()
    return default


def _flag(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _port(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        return default
    return value


def _time_text(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    try:
        tc = parse_time_control(value)
    except ValueError:
        return default
    return str(tc) if tc is not None else ""


def _recent(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    hosts: List[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 300:
            continue
        try:
            host, port = net.parse_host_port(item)
        except ValueError:
            continue
        text = format_host(host, port)
        if text not in hosts:
            hosts.append(text)
        if len(hosts) == MAX_RECENT:
            break
    return hosts


@dataclass
class Settings:
    """Everything the menu's Settings screen edits, plus the recently joined hosts."""

    name: str = ""                 # "" means the login name
    time_control: str = ""         # default time control, e.g. "5+3"; "" means untimed
    host_color: str = "white"      # white, black or random
    port: int = net.DEFAULT_PORT
    piece_style: str = "unicode"   # unicode or ascii
    colors: bool = True
    flip_local: bool = True        # turn the board to the side to move in local games
    autosave: bool = True
    pgn_dir: str = ""              # "" means ~/lanchess_games
    recent_hosts: List[str] = field(default_factory=list)

    @property
    def time(self) -> Optional[TimeControl]:
        """The default time control as an object (None for untimed)."""
        try:
            return parse_time_control(self.time_control)
        except ValueError:
            return None

    def copy(self) -> "Settings":
        return Settings.from_dict(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        data = {item.name: getattr(self, item.name) for item in fields(self)}
        data["recent_hosts"] = list(self.recent_hosts)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "Settings":
        """Build validated settings; anything missing, unknown or invalid falls back to the default."""
        base = cls()
        if not isinstance(data, Mapping):
            return base
        name = data.get("name", base.name)
        return cls(
            name=net.sanitize_name(name, default="") if isinstance(name, str) else base.name,
            time_control=_time_text(data.get("time_control"), base.time_control),
            host_color=_choice(data.get("host_color"), COLORS, base.host_color),
            port=_port(data.get("port"), base.port),
            piece_style=_choice(data.get("piece_style"), PIECE_STYLES, base.piece_style),
            colors=_flag(data.get("colors"), base.colors),
            flip_local=_flag(data.get("flip_local"), base.flip_local),
            autosave=_flag(data.get("autosave"), base.autosave),
            pgn_dir=_text(data.get("pgn_dir"), base.pgn_dir),
            recent_hosts=_recent(data.get("recent_hosts")),
        )

    def remember_host(self, host: str, port: int) -> None:
        """Put ``host:port`` first in the recent hosts (at most MAX_RECENT, no duplicates)."""
        entry = format_host(host, port)
        self.recent_hosts = _recent([entry] + [h for h in self.recent_hosts if h != entry])


def config_dir(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
               home: Optional[str] = None) -> str:
    """The folder that holds config.json (arguments are for tests)."""
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = os.path.expanduser("~") if home is None else home
    override = env.get(ENV_DIR, "").strip()
    if override:
        return os.path.expanduser(override)
    if platform.startswith("win"):
        appdata = env.get("APPDATA", "").strip()
        return os.path.join(appdata or os.path.join(home, "AppData", "Roaming"), "lanchess")
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "lanchess")
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "lanchess")
    return os.path.join(home, ".config", "lanchess")


def config_path(environ: Optional[Mapping[str, str]] = None, platform: Optional[str] = None,
                home: Optional[str] = None) -> str:
    """Full path of config.json."""
    return os.path.join(config_dir(environ, platform, home), FILENAME)


def load(path: Optional[str] = None) -> Settings:
    """Read the settings; never raises (problems mean defaults)."""
    target = path or config_path()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.loads(handle.read(1_000_000))
    except (OSError, ValueError, RecursionError):
        return Settings()
    return Settings.from_dict(data)


def save(settings: Settings, path: Optional[str] = None) -> str:
    """Write the settings atomically (temporary file + rename). Returns the path; raises OSError."""
    target = path or config_path()
    folder = os.path.dirname(os.path.abspath(target))
    os.makedirs(folder, exist_ok=True)
    data = json.dumps(Settings.from_dict(settings.to_dict()).to_dict(), indent=2, ensure_ascii=False) + "\n"
    fd, temp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return target


def remember_host(host: str, port: int, path: Optional[str] = None) -> bool:
    """Add a joined host to the saved recent hosts; returns False (silently) if saving fails."""
    settings = load(path)
    settings.remember_host(host, port)
    try:
        save(settings, path)
    except OSError:
        return False
    return True

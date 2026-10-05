#!/usr/bin/env python3
"""Build dist/lanchess.pyz, a single-file zipapp of the lanchess package.

Usage: python3 build.py [-o OUTPUT]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent
PACKAGE = "lanchess"
DEFAULT_TARGET = ROOT / "dist" / "lanchess.pyz"
INTERPRETER = "/usr/bin/env python3"
EXCLUDE = ("__pycache__", "*.pyc", "*.pyo")
MAIN_SOURCE = """\
import sys

if sys.version_info < (3, 8):
    sys.exit("LAN Chess needs Python 3.8 or newer.")

from lanchess.cli import main

sys.exit(main())
"""


class BuildError(Exception):
    """The archive could not be built."""


def stage(package_dir: Path, staging: Path) -> None:
    """Copy the package (minus bytecode) into ``staging`` and add the zipapp entry point."""
    shutil.copytree(str(package_dir), str(staging / PACKAGE), ignore=shutil.ignore_patterns(*EXCLUDE))
    (staging / "__main__.py").write_text(MAIN_SOURCE, encoding="utf-8")


def build(root: Path = ROOT, target: Optional[Path] = None) -> Path:
    """Build the zipapp from ``root/lanchess`` and return the archive path."""
    package_dir = root / PACKAGE
    if not (package_dir / "__init__.py").is_file():
        raise BuildError(f"package not found: {package_dir / '__init__.py'} does not exist")
    target = Path(target) if target is not None else root / "dist" / "lanchess.pyz"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    with tempfile.TemporaryDirectory(prefix="lanchess-build-") as tmp:
        staging = Path(tmp)
        stage(package_dir, staging)
        try:
            zipapp.create_archive(staging, target=partial, interpreter=INTERPRETER, compressed=True)
            os.replace(str(partial), str(target))
        finally:
            if partial.exists():
                partial.unlink()
    return target


def format_size(size: int) -> str:
    """Human-readable byte count, e.g. ``"41.2 KB"``."""
    if size < 1024:
        return f"{size} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def main(argv: Optional[List[str]] = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Build the single-file lanchess.pyz zipapp.")
    parser.add_argument(
        "-o", "--output", type=Path, default=DEFAULT_TARGET,
        help="archive path (default: dist/lanchess.pyz)",
    )
    args = parser.parse_args(argv)
    try:
        target = build(ROOT, args.output)
    except (BuildError, OSError, zipapp.ZipAppError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    if not (ROOT / PACKAGE / "cli.py").is_file():
        print(f"warning: {PACKAGE}/cli.py is missing; the archive will not start until it exists",
              file=sys.stderr)
    print(f"Built {target.resolve()} ({format_size(target.stat().st_size)})")
    print(f"Run it with: python3 {target.name} host   (Windows: py {target.name} host)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

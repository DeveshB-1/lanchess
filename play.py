#!/usr/bin/env python3
"""Launch LAN Chess from a source checkout, e.g. ``python3 play.py host``."""

import os
import sys

if __name__ == "__main__":
    if sys.version_info < (3, 8):
        sys.exit("LAN Chess needs Python 3.8 or newer.")
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from lanchess.cli import main

    raise SystemExit(main())

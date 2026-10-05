"""Helpers shared by the test modules (not a test module itself)."""

from __future__ import annotations

import threading
import time
import unittest
from typing import Iterable, List

from lanchess import net

# Threads that are meant to outlive the code that started them: the stdin reader of the plain
# mode (blocked in readline until the end of input) and the cached host-name lookup (it ends on
# its own, and can take half a minute on macOS when the name does not resolve).
LONG_LIVED_THREADS = ("lanchess-stdin", net.HOSTNAME_LOOKUP_THREAD)


def watch_threads(test: unittest.TestCase, wait: float = 3.0, ignore: Iterable[str] = LONG_LIVED_THREADS) -> None:
    """Make ``test`` fail if a ``lanchess-*`` thread that it started is still running at its end.

    Call it first in setUp: the check then runs after every other cleanup (closing connections
    and servers). Only threads started during the test count, so a thread that is slow to stop
    is reported once, by the test that started it, and not again by every test that follows.
    """
    before = set(threading.enumerate())
    ignored = tuple(ignore)

    def leftovers() -> List[threading.Thread]:
        return [thread for thread in threading.enumerate()
                if thread not in before and thread.is_alive() and thread.name.startswith("lanchess-")
                and thread.name not in ignored]

    def check() -> None:
        deadline = time.monotonic() + wait
        running = leftovers()
        while running and time.monotonic() < deadline:
            running[0].join(max(0.0, min(0.05, deadline - time.monotonic())))
            running = leftovers()
        if running:
            test.fail(f"threads still running {wait:g} s after the test: {sorted(t.name for t in running)}")

    test.addCleanup(check)

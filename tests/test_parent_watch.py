"""The app-owned backend ends when its app does, without importing the server."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from hyadex.ops.parent_watch import owning_parent_pid

ROOT = Path(__file__).resolve().parents[1]

# The launcher plays the app: it starts the watched child, reports its pid and
# then dies abruptly, as a crashed or force-quit app does.
LAUNCHER = """
import os, subprocess, sys
child = subprocess.Popen([sys.executable, "-c", sys.argv[1]], cwd=sys.argv[2])
print(child.pid, flush=True)
sys.stdin.readline()
os._exit(0)
"""
CHILD = """
import asyncio, os, signal, sys
sys.path.insert(0, os.getcwd())
from hyadex.ops.parent_watch import exit_with_parent
asyncio.run(exit_with_parent(os.getppid(), interval=0.05))
signal.pause()
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ParentWatchTests(unittest.TestCase):
    def test_child_exits_after_its_owner_dies(self) -> None:
        launcher = subprocess.Popen([sys.executable, "-c", LAUNCHER, CHILD, str(ROOT)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(launcher.stdout.close)
        self.addCleanup(launcher.stdin.close)
        child = int(launcher.stdout.readline())
        self.addCleanup(lambda: alive(child) and os.kill(child, 9))
        time.sleep(0.5)
        self.assertTrue(alive(child), "the watch must not end while its owner runs")
        launcher.stdin.write("\n")
        launcher.stdin.flush()
        launcher.wait(timeout=5)
        deadline = time.monotonic() + 10
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child), "the watched child outlived its owner")

    def test_only_a_real_parent_pid_is_watched(self) -> None:
        for value, expected in (("4242", 4242), ("", None), ("1", None), ("0", None), ("-5", None), ("x", None)):
            with self.subTest(value=value), patch.dict(os.environ, {"HYADEX_EXIT_WITH_PARENT_PID": value}):
                self.assertEqual(owning_parent_pid(), expected)
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(owning_parent_pid())


if __name__ == "__main__":
    unittest.main()

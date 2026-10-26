"""Run package contracts in a temporary home without importing agent_server.

This small suite covers extracted domain/app/storage modules. The complete
integration, provider and installer suite remains scripts/run_test_shard.py.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


MODULES = (
    "tests.test_job_occurrences",
    "tests.test_job_policy",
    "tests.test_provider_runtime",
    "tests.test_claude_sdk_client",
    "tests.test_codex_app_server",
    "tests.test_group_chat",
    "tests.test_autopilot",
    "tests.test_application_factory",
    "tests.test_package_boundaries",
    "tests.test_transcript_reader",
    "tests.test_release_file_manifest_isolated",
    "tests.test_source_install",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print("\n".join(MODULES))
        return 0
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hyadex-fast-") as home:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("HYADEX_", "ZENITH", "AGENTSDOCK_", "AGENTS_SERVER_"))}
        env.update(
            HOME=home, PYTHONDONTWRITEBYTECODE="1",
            XDG_CONFIG_HOME=str(Path(home) / ".config"),
            XDG_CACHE_HOME=str(Path(home) / ".cache"),
            XDG_DATA_HOME=str(Path(home) / ".local/share"),
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        )
        worker = """
import importlib.abc
import sys
import unittest

class NoServerImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agent_server':
            raise ImportError('The fast suite must not import the server runtime')

sys.meta_path.insert(0, NoServerImport())
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
"""
        return subprocess.run(
            [sys.executable, "-B", "-c", worker, *MODULES],
            cwd=root, env=env,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())

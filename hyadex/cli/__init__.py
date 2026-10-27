"""Helper commands agents run inside a turn: jobs, publish, emergency, notify.

They use only the standard library and are run by path with the agent's own
Python; the server passes their paths to providers.
"""
from __future__ import annotations

from pathlib import Path

CLI_DIR = Path(__file__).resolve().parent
HELPER_SCRIPTS = {name: CLI_DIR / f"{name}.py" for name in ("jobs", "publish", "emergency", "notify")}

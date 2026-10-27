"""Late-bound links from core code to features that sit above it.

The turn engine has to consult scheduled jobs (dispatch revision checks,
pausing jobs of an archived chat), and group discussions have to update the
jobs of their members, yet the job store itself starts turns. Likewise the
engine starts each provider's runner, while the runners finish their turns
through the engine. Core code
reaches those features through this object instead of importing them, so the
packages keep a one-way dependency order. The server installs the real
objects at import time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class Wiring:
    # features.jobs JobStore: assert_dispatch_revision, pause_for_session,
    # plus the job table used by group membership changes.
    jobs: Any = None
    # Provider runners (providers/codex/turns.py, providers/claude/turns.py):
    # called with the turn's session, run and prompt; return the run task.
    run_codex: Any = None
    run_claude: Any = None


WIRING = Wiring()

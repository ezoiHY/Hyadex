"""Source of the server for tests that compile its functions in isolation.

Functions are moving out of agent_server.py into hyadex/ packages. Tests that
select definitions by name read this combined text instead of one file, so a
move does not change what they compile.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "agent_server.py"

# Modules extracted from agent_server.py, in dependency order.
EXTRACTED: tuple[str, ...] = ('hyadex/core/config.py', 'hyadex/core/paths.py', 'hyadex/core/identity.py', 'hyadex/providers/codex/config.py', 'hyadex/providers/instructions.py', 'hyadex/core/util.py', 'hyadex/core/server_logging.py', 'hyadex/core/state.py', 'hyadex/providers/claude/history.py', 'hyadex/features/titles.py', 'hyadex/features/usage.py', )


@lru_cache(maxsize=1)
def text() -> str:
    parts = [SERVER.read_text(encoding="utf-8")]
    parts += [(ROOT / path).read_text(encoding="utf-8") for path in EXTRACTED]
    return "\n\n".join(parts)

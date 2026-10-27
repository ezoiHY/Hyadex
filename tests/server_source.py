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
EXTRACTED: tuple[str, ...] = ('hyadex/core/config.py', 'hyadex/core/paths.py', 'hyadex/core/identity.py', 'hyadex/providers/codex/config.py', 'hyadex/providers/instructions.py', 'hyadex/core/util.py', 'hyadex/core/server_logging.py', 'hyadex/core/state.py', 'hyadex/providers/claude/history.py', 'hyadex/features/titles.py', 'hyadex/features/usage.py', 'hyadex/core/processes.py', 'hyadex/core/sessions.py', 'hyadex/providers/codex/history.py', 'hyadex/providers/authority.py', 'hyadex/core/event_log.py', 'hyadex/features/terminal.py', 'hyadex/features/history.py', 'hyadex/features/files.py', 'hyadex/providers/subagents.py', 'hyadex/providers/claude/runtime.py', 'hyadex/features/side_questions.py', 'hyadex/providers/registry.py', 'hyadex/core/queue.py', 'hyadex/providers/codex/runtime.py', 'hyadex/core/http.py', 'hyadex/features/emergency.py', 'hyadex/core/turns.py', 'hyadex/core/stop.py', 'hyadex/features/forks.py', 'hyadex/features/group_runtime.py', 'hyadex/features/jobs.py', 'hyadex/features/search.py', 'hyadex/ops/lifecycle.py', 'hyadex/api/provider_mcp.py', 'hyadex/api/common.py', 'hyadex/api/health.py', 'hyadex/api/admin.py', 'hyadex/api/terminal.py', 'hyadex/api/sessions.py', 'hyadex/api/search.py', 'hyadex/api/providers.py', 'hyadex/api/turns.py', 'hyadex/api/jobs.py', 'hyadex/api/agent.py', 'hyadex/api/files.py', 'hyadex/main.py', 'hyadex/providers/codex/turns.py', 'hyadex/providers/claude/turns.py', 'hyadex/providers/codex/forks.py', 'hyadex/providers/codex/threads.py', 'hyadex/providers/codex/goals.py', 'hyadex/providers/codex/tools.py', 'hyadex/providers/codex/managers.py', 'hyadex/providers/codex/notifications.py', 'hyadex/providers/codex/interactions.py', 'hyadex/providers/codex/commands.py', 'hyadex/providers/codex/reasoning_stream.py', 'hyadex/providers/codex/exec_runner.py', 'hyadex/providers/codex/controls.py', 'hyadex/providers/codex/native_turns.py', 'hyadex/core/session_alerts.py', 'hyadex/core/session_codex.py', 'hyadex/core/event_seq.py', 'hyadex/core/session_files.py', 'hyadex/core/restart_status.py', 'hyadex/core/session_settings.py', 'hyadex/core/session_titles.py', )


@lru_cache(maxsize=1)
def text() -> str:
    parts = [SERVER.read_text(encoding="utf-8")]
    parts += [(ROOT / path).read_text(encoding="utf-8") for path in EXTRACTED]
    return "\n\n".join(parts)

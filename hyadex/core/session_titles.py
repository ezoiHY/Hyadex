"""Native and generated chat titles."""
from __future__ import annotations

import asyncio
from typing import Any

from hyadex.core.config import BACKEND_CLAUDE, BACKEND_CODEX, DEFAULT_BACKEND
from hyadex.core.restart_status import (
    MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    managed_server_restart_blocks_work,
)
from hyadex.core.server import SERVER
from hyadex.core.session_codex import codex_session_index_thread_names
from hyadex.features.titles import GENERATED_TITLE_TASKS
from hyadex.providers.claude.history import (
    claude_history_candidates,
    claude_transcript_title,
    native_session_title,
    provider_session_identifier,
    session_provider_id,
)


def read_native_session_title(sess: dict[str, Any]) -> str | None:
    """Read existing native metadata only; never launch a CLI or model request.

    Missing/changed provider formats intentionally leave the prompt fallback.
    Claude and Codex use the same roots as history discovery. Cursor reads only
    exact-workspace, exact-session naming metadata, never conversation blobs.
    """
    backend = str(sess.get("backend") or DEFAULT_BACKEND)
    provider_id = provider_session_identifier(session_provider_id(sess))
    if not provider_id:
        return None
    if backend == BACKEND_CLAUDE:
        candidates = claude_history_candidates(provider_id)
        if len(candidates) != 1:
            return None
        return claude_transcript_title(candidates[0], provider_id)
    if backend == BACKEND_CODEX:
        return native_session_title(codex_session_index_thread_names().get(provider_id))
    return None


def title_runtime_key(sess: dict[str, Any]) -> tuple:
    return (sess.get("backend") or DEFAULT_BACKEND, session_provider_id(sess), sess.get("model"))


def cancel_generated_session_title(session_id: str) -> None:
    task = GENERATED_TITLE_TASKS.get(session_id)
    if task is not None and not task.done():
        task.cancel()


async def close_generated_session_titles() -> None:
    tasks = tuple(GENERATED_TITLE_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)



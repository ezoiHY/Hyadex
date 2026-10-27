"""Generated chat titles: eligibility and the in-flight title work."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from hyadex.core.config import BACKEND_CODEX
from hyadex.core.server import SERVER
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS


def session_accepts_auto_title(sess: dict[str, Any]) -> bool:
    # Never infer ownership from a title's wording: a user can intentionally
    # name a chat "New chat" or the exact first line of their prompt.
    return (
        sess.get("_title_source") in {"placeholder", "prompt", "provider", "generated"}
        and sess.get("_title_auto_value") == sess.get("title")
        and not sess.get("_fork_initializing")
        and not sess.get("_history_import_initializing")
    )


GENERATED_TITLE_TASKS: dict[str, asyncio.Task] = {}
GENERATED_TITLE_SLOTS = asyncio.Semaphore(2)


def active_generated_title_work_labels() -> list[str]:
    """Include owned title requests until their provider cleanup has finished."""
    return sorted(
        f"Automatic title for {session_id}"
        for session_id, task in GENERATED_TITLE_TASKS.items()
        if not task.done()
    )


def generated_title_eligible(sess: dict[str, Any], *, allow_attempted: bool = False) -> bool:
    return bool(
        os.environ.get("HYADEX_AUTO_TITLES", "1").strip().lower() not in {"0", "false", "off"}
        and not SERVER.shutting_down
        and sess.get("backend") in {BACKEND_CODEX}
        and sess.get("auto_title_enabled", True) is not False
        and not sess.get("archived") and not sess.get("parent_id") and not sess.get("fork_from")
        and sess.get("id") not in DELETING_SESSIONS
        and sess.get("id") not in DELETED_SESSION_TOMBSTONES
        and session_accepts_auto_title(sess)
        and sess.get("_title_source") in {"placeholder", "prompt"}
        and (allow_attempted or not sess.get("_title_generation_attempted"))
    )

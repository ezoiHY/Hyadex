"""Claude native goals."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from claude_goals import ClaudeGoalProjection
from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_PRINT,
    DEFAULT_BACKEND,
)
from hyadex.core.sessions import append_event, STORE
from hyadex.providers.claude.context_usage import (
    claude_provider_id_for_session,
    claude_sdk_dependency_available,
)
from hyadex.providers.claude.history import find_claude_history

CLAUDE_GOAL_PROJECTIONS: dict[str, ClaudeGoalProjection] = {}
CLAUDE_GOAL_PATHS: dict[str, Path] = {}
CLAUDE_GOAL_LOCKS: dict[str, asyncio.Lock] = {}
CLAUDE_GOAL_PENDING: dict[str, dict[str, Any]] = {}


async def refresh_claude_goal(
    session_id: str, *, provider_id: str | None = None, notify: bool = True,
) -> dict[str, Any] | None:
    """Project provider-owned goal records during existing runtime activity."""
    session = STORE.sessions.get(session_id)
    if not session or str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
        return None
    provider_id = provider_id or claude_provider_id_for_session(session)
    if not provider_id:
        return None
    async with CLAUDE_GOAL_LOCKS.setdefault(session_id, asyncio.Lock()):
        projection = CLAUDE_GOAL_PROJECTIONS.get(session_id)
        if projection is None or projection.provider_session_id != provider_id:
            projection = ClaudeGoalProjection(provider_id)
            CLAUDE_GOAL_PROJECTIONS[session_id] = projection
            CLAUDE_GOAL_PATHS.pop(session_id, None)
        path = CLAUDE_GOAL_PATHS.get(session_id)
        if path is not None and not await asyncio.to_thread(path.exists):
            CLAUDE_GOAL_PATHS.pop(session_id, None)
            path = None
        if path is None:
            path = await asyncio.to_thread(find_claude_history, provider_id)
            if path is not None:
                CLAUDE_GOAL_PATHS[session_id] = path
        if path is None:
            return None
        was_caught_up = projection.caught_up
        changed = await asyncio.to_thread(projection.refresh, path)
        if not projection.caught_up:
            return None
        changed = changed or not was_caught_up
        goal = projection.goal
        pending = CLAUDE_GOAL_PENDING.get(session_id)
        if pending and goal and goal.get("set_at") != pending.get("previous_set_at"):
            CLAUDE_GOAL_PENDING.pop(session_id, None)
            changed = True
        if changed and notify:
            await append_event(session_id, "claude_goal_changed", {})
        return dict(goal) if goal else None


def require_claude_goal_session(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if str(session.get("backend") or DEFAULT_BACKEND) != BACKEND_CLAUDE:
        raise HTTPException(status_code=400, detail="Goals here require a Claude chat")
    if CLAUDE_TRANSPORT == CLAUDE_TRANSPORT_PRINT or not claude_sdk_dependency_available():
        raise HTTPException(status_code=409, detail="Claude Goals require the Claude Agent SDK")
    return session


class ClaudeGoalRequest(BaseModel):
    condition: str = Field(min_length=1, max_length=4000)

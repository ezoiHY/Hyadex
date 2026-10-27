"""Provider controls on a chat: Claude and Codex runtime, goals, interactions, context usage and compaction."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from hyadex.core.config import CLAUDE_TRANSPORT_AGENT_SDK, CODEX_GOALS_ENABLED
from hyadex.core.http import router
from hyadex.core.runs import RUNS
from hyadex.core.sessions import (
    codex_goal_time_budget_is_exhausted,
    ensure_session_not_deleting,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.turns import set_claude_goal, start_claude_goal_command
from hyadex.providers.codex.turns import (
    _delete_codex_goal_locked,
    _post_codex_compact_locked,
    _put_codex_goal_locked,
)
from hyadex.providers.claude.runtime import (
    claude_runtime_snapshot,
    claude_sdk_manager,
    ClaudeGoalRequest,
    ClaudeInteractionResponseRequest,
    refresh_claude_goal,
    refresh_idle_claude_context_usage,
    require_claude_goal_session,
    resolve_claude_interaction,
)
from hyadex.providers.claude.sdk import ClaudeSDKSupervisorError
from hyadex.providers.codex.runtime import codex_runtime_snapshot
from hyadex.providers.codex.interactions import (
    CodexInteractionResponseRequest,
    resolve_codex_interaction,
)
from hyadex.providers.codex.goals import CodexGoalRequest

router = APIRouter()


@router.get("/api/sessions/{session_id}/codex/runtime")
async def get_codex_runtime(session_id: str) -> dict[str, Any]:
    return await codex_runtime_snapshot(session_id)


@router.post(
    "/api/sessions/{session_id}/codex/interactions/{interaction_id}/resolve"
)


async def post_codex_interaction_response(
    session_id: str,
    interaction_id: str,
    req: CodexInteractionResponseRequest,
) -> dict[str, Any]:
    interaction = await resolve_codex_interaction(
        session_id,
        interaction_id,
        req.response,
    )
    return {"interaction": interaction}


@router.put("/api/sessions/{session_id}/claude/goal")
async def put_claude_goal(session_id: str, req: ClaudeGoalRequest) -> dict[str, Any]:
    return await set_claude_goal(session_id, req)


@router.delete("/api/sessions/{session_id}/claude/goal")
async def delete_claude_goal(session_id: str) -> dict[str, Any]:
    require_claude_goal_session(session_id)
    cleared_live = False
    # Serialize with ordinary turn admission while the native priority command
    # interrupts the current turn and returns its separate clear receipt.
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        async with RUNS.active_lock:
            active = dict(RUNS.active.get(session_id) or {})
        if active.get("transport") == CLAUDE_TRANSPORT_AGENT_SDK:
            manager = await claude_sdk_manager()
            try:
                await manager.clear_goal(session_id, run_id=str(active.get("run_id") or ""))
            except ClaudeSDKSupervisorError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            cleared_live = True
    if not cleared_live:
        await start_claude_goal_command(session_id, "clear")
    await refresh_claude_goal(session_id)
    return await claude_runtime_snapshot(session_id)


@router.get("/api/sessions/{session_id}/claude/runtime")
async def get_claude_runtime(session_id: str) -> dict[str, Any]:
    return await claude_runtime_snapshot(session_id)


@router.post("/api/sessions/{session_id}/claude/context-usage/refresh")
async def post_claude_context_usage_refresh(
    session_id: str,
) -> dict[str, Any]:
    refreshed = await refresh_idle_claude_context_usage(session_id)
    if not refreshed:
        raise HTTPException(
            status_code=409,
            detail="Claude context changed while sampling; retry the refresh",
        )
    return {
        **await claude_runtime_snapshot(session_id),
        "context_usage_refreshed": True,
    }


@router.post(
    "/api/sessions/{session_id}/claude/interactions/{interaction_id}/resolve"
)


async def post_claude_interaction_response(
    session_id: str,
    interaction_id: str,
    req: ClaudeInteractionResponseRequest,
) -> dict[str, Any]:
    # Serialize approval resolution with Stop/Delete lifecycle changes. Once
    # deletion has reserved this chat, no late UI response may authorize a
    # provider action while cleanup is fencing the SDK callback.
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        interaction = await resolve_claude_interaction(
            session_id,
            interaction_id,
            req.response,
        )
        return {"interaction": interaction}


@router.get("/api/sessions/{session_id}/codex/goal")
async def get_codex_goal(session_id: str) -> dict[str, Any]:
    session = STORE.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "enabled": CODEX_GOALS_ENABLED,
        "goal": session.get("codex_goal"),
        "time_budget_seconds": session.get("codex_goal_time_budget_seconds"),
        "time_budget_exhausted": codex_goal_time_budget_is_exhausted(session),
    }


@router.put("/api/sessions/{session_id}/codex/goal")
async def put_codex_goal(
    session_id: str,
    req: CodexGoalRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _put_codex_goal_locked(session_id, req)


@router.delete("/api/sessions/{session_id}/codex/goal")
async def delete_codex_goal(session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _delete_codex_goal_locked(session_id)


@router.post("/api/sessions/{session_id}/codex/compact")
async def post_codex_compact(session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        ensure_session_not_deleting(session_id)
        return await _post_codex_compact_locked(session_id)

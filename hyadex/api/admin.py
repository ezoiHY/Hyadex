"""Native administration: Codex goal and subagent settings, managed restart."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from hyadex.api.common import require_native_admin_control
from hyadex.core.http import route_auth, router
from hyadex.ops.lifecycle import (
    _restart_server_endpoint_impl,
    current_server_restart_blocker_snapshot,
    log_server_restart_denial,
    public_server_restart_status,
    require_server_restart_control,
    ServerRestartRequest,
)
from hyadex.providers.codex.goals import (
    CODEX_GOALS_ADMIN_MAX_BODY_BYTES,
    CodexGoalsAdminRequest,
    get_codex_goals_admin,
    put_codex_goals_admin,
)
from hyadex.providers.codex.subagents import (
    CodexSubagentsAdminRequest,
    get_codex_subagents_admin,
    put_codex_subagents_admin,
)

router = APIRouter()


@router.get("/api/admin/codex/goals", dependencies=route_auth("native_admin"))
async def get_codex_goals_admin_endpoint(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    return await get_codex_goals_admin()


@router.put("/api/admin/codex/goals", dependencies=route_auth("native_admin", CODEX_GOALS_ADMIN_MAX_BODY_BYTES))
async def put_codex_goals_admin_endpoint(
    req: CodexGoalsAdminRequest,
    request: Request,
) -> dict[str, Any]:
    require_native_admin_control(request)
    return await put_codex_goals_admin(req)


@router.get("/api/admin/codex/subagents", dependencies=route_auth("native_admin"))
async def get_codex_subagents_admin_endpoint(request: Request) -> dict[str, Any]:
    require_native_admin_control(request)
    return await get_codex_subagents_admin()


@router.put("/api/admin/codex/subagents", dependencies=route_auth("native_admin", CODEX_GOALS_ADMIN_MAX_BODY_BYTES))
async def put_codex_subagents_admin_endpoint(
    req: CodexSubagentsAdminRequest,
    request: Request,
) -> dict[str, Any]:
    require_native_admin_control(request)
    return await put_codex_subagents_admin(req)


@router.get("/api/admin/restart", dependencies=route_auth("restart"))
async def server_restart_status_endpoint(request: Request) -> dict[str, Any]:
    require_server_restart_control(request)
    blocker_snapshot = await current_server_restart_blocker_snapshot()
    return public_server_restart_status(
        blocker_snapshot=blocker_snapshot,
    )


@router.post("/api/admin/restart", status_code=202, dependencies=route_auth("restart"))
async def restart_server_endpoint(
    body: ServerRestartRequest,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    try:
        return await _restart_server_endpoint_impl(
            body,
            request,
            background_tasks,
        )
    except HTTPException as exc:
        log_server_restart_denial(body, exc)
        raise

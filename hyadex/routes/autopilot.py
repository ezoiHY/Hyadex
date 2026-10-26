"""Autopilot HTTP endpoints; the application supplies the run engine."""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Query
from hyadex.autopilot import AutopilotEngine, ApproveRequest, CreateRunRequest, FeedbackRequest

def create_autopilot_router(engine: AutopilotEngine) -> APIRouter:
    router = APIRouter()

    @router.get("/api/autopilot/runs")
    async def list_runs() -> dict[str, Any]:
        return {"runs": engine.list()}

    @router.post("/api/autopilot/runs")
    async def create_run(req: CreateRunRequest) -> dict[str, Any]:
        return {"run": await engine.create(req)}

    @router.get("/api/autopilot/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        return {"run": engine.public(engine.require(run_id))}

    @router.post("/api/autopilot/runs/{run_id}/approve")
    async def approve(run_id: str, req: ApproveRequest | None = None) -> dict[str, Any]:
        return {"run": await engine.approve(run_id, req or ApproveRequest())}

    @router.post("/api/autopilot/runs/{run_id}/feedback")
    async def feedback(run_id: str, req: FeedbackRequest) -> dict[str, Any]:
        return {"run": await engine.feedback(run_id, req)}

    @router.post("/api/autopilot/runs/{run_id}/pause")
    async def pause(run_id: str) -> dict[str, Any]:
        return {"run": await engine.pause(run_id)}

    @router.post("/api/autopilot/runs/{run_id}/resume")
    async def resume(run_id: str) -> dict[str, Any]:
        return {"run": await engine.resume(run_id)}

    @router.post("/api/autopilot/runs/{run_id}/abort")
    async def abort(run_id: str) -> dict[str, Any]:
        return {"run": await engine.abort(run_id)}

    @router.delete("/api/autopilot/runs/{run_id}")
    async def delete(run_id: str, remove_worktree: bool = Query(default=False)) -> dict[str, Any]:
        engine.require(run_id)
        await engine.delete(run_id, remove_worktree_files=remove_worktree)
        return {"deleted": True, "id": run_id}

    return router


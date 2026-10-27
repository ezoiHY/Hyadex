"""Scheduled jobs for users, groups and agents."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from hyadex.core.http import (
    AgentCreateScopedJobRequest,
    AgentUpdateJobRequest,
    CreateJobRequest,
    CreateScopedJobRequest,
    route_auth,
    router,
    UpdateJobRequest,
)
from hyadex.core.queue import request_fields_set
from hyadex.core.sessions import session_lifecycle_lock
from hyadex.features.group_runtime import GROUPS
from hyadex.features.jobs import (
    append_provider_job_mutation_event,
    get_session_job_runs,
    JOBS,
    list_session_jobs,
)
from hyadex.features.job_access import (
    authorize_provider_jobs_operation,
    provider_public_job,
    provider_public_job_run,
    public_job,
)

router = APIRouter()


@router.post("/api/groups/{group_id}/jobs")
async def create_group_job(group_id: str, req: CreateScopedJobRequest) -> dict[str, Any]:
    """Schedule a message into a group. It is anchored to the default responder's chat."""

    group = GROUPS.require(group_id, writable=True)
    if group.get("kind") == "autopilot":
        raise HTTPException(status_code=409, detail="this group belongs to an autopilot run")
    handle = GROUPS.default_responder(group)
    participant = GROUPS.participant(group, handle) if handle else None
    if participant is None:
        raise HTTPException(status_code=409, detail="the group has no agents")
    fields = req.model_dump(exclude_unset=True)
    if fields.get("chat_references") or fields.get("team_references"):
        raise HTTPException(status_code=400, detail="group jobs do not support chat or team references")
    session_id = participant["session_id"]
    async with session_lifecycle_lock(session_id):
        job = await JOBS.create(CreateJobRequest(session_id=session_id, **fields), group_id=group_id)
    return {"job": public_job(job)}


@router.get("/api/groups/{group_id}/jobs")
async def list_group_jobs(group_id: str) -> dict[str, Any]:
    GROUPS.require(group_id)
    return {"jobs": [public_job(job) for job in JOBS.jobs.values() if job.get("group_id") == group_id]}


@router.delete("/api/groups/{group_id}/jobs/{job_id}")
async def delete_group_job(group_id: str, job_id: str) -> dict[str, Any]:
    job = JOBS.jobs.get(job_id)
    if not job or job.get("group_id") != group_id:
        raise HTTPException(status_code=404, detail="job not found")
    session_id = str(job.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        deleted = await JOBS.delete(job_id, expected_session_id=session_id)
    return {"deleted": bool(deleted), "id": job_id}


@router.get("/api/jobs")
async def list_jobs(session_id: str | None = None) -> dict[str, Any]:
    jobs = sorted(
        (
            public_job(job)
            for job in JOBS.jobs.values()
            if session_id is None or job.get("session_id") == session_id
        ),
        key=lambda j: j.get("updated_at") or "",
        reverse=True,
    )
    return {"jobs": jobs}


@router.get("/api/agent/sessions/{session_id}/jobs", dependencies=route_auth("agent_helper", 0))
async def list_agent_session_jobs(request: Request, session_id: str) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="read",
        )
        response = await list_session_jobs(session_id)
        return {
            **response,
            "jobs": [
                provider_public_job(job)
                for job in response.get("jobs") or []
                if isinstance(job, dict)
            ],
        }


@router.get("/api/agent/sessions/{session_id}/jobs/{job_id}/runs", dependencies=route_auth("agent_helper", 0))
async def get_agent_session_job_runs(
    request: Request,
    session_id: str,
    job_id: str,
    timeline_group_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=320,
    ),
    before_seq: int | None = Query(default=None, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="read",
        )
        response = await get_session_job_runs(
            session_id,
            job_id,
            timeline_group_id=timeline_group_id,
            before_seq=before_seq,
            limit=limit,
        )
        return {
            **response,
            "runs": [
                provider_public_job_run(run)
                for run in response.get("runs") or []
                if isinstance(run, dict)
            ],
        }


@router.post("/api/jobs")
async def create_job(req: CreateJobRequest) -> dict[str, Any]:
    async with session_lifecycle_lock(req.session_id):
        job = await JOBS.create(req)
    return {"job": public_job(job)}


@router.post("/api/agent/sessions/{session_id}/jobs", dependencies=route_auth("agent_helper", 512 * 1024))
async def create_agent_session_job(
    request: Request,
    session_id: str,
    req: AgentCreateScopedJobRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        if "team_references" in request_fields_set(req):
            raise HTTPException(
                status_code=403,
                detail=(
                    "provider jobs access cannot create durable team grants"
                ),
            )
        fields = req.model_dump(exclude_unset=True)
        # Both are retired and validated empty; neither has an effect.
        fields.pop("chat_routes", None)
        fields.pop("chat_references", None)
        job = await JOBS.create(CreateJobRequest(
            session_id=session_id,
            **fields,
        ), emit_event=False)
        await append_provider_job_mutation_event(job, "job_created")
        return {"job": provider_public_job(job)}


@router.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, req: UpdateJobRequest) -> dict[str, Any]:
    snapshot = await JOBS.snapshot(job_id)
    session_id = str(snapshot.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        job = await JOBS.update(
            job_id,
            req.model_dump(exclude_unset=True),
            expected_session_id=session_id,
        )
    return {"job": public_job(job)}


@router.patch("/api/agent/sessions/{session_id}/jobs/{job_id}", dependencies=route_auth("agent_helper", 512 * 1024))
async def update_agent_session_job(
    request: Request,
    session_id: str,
    job_id: str,
    req: AgentUpdateJobRequest,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        if (
            "team_references" in request_fields_set(req)
            and req.team_references
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "provider jobs access cannot add or expand durable team grants"
                ),
            )
        fields = req.model_dump(exclude_unset=True)
        # Retired and validated empty. An explicit empty chat_references
        # still clears a legacy job's saved @Chat targets.
        fields.pop("chat_routes", None)
        job = await JOBS.update(
            job_id,
            fields,
            expected_session_id=session_id,
            emit_event=False,
        )
        await append_provider_job_mutation_event(job, "job_updated")
        return {"job": provider_public_job(job)}


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    snapshot = await JOBS.snapshot(job_id)
    session_id = str(snapshot.get("session_id") or "")
    async with session_lifecycle_lock(session_id):
        deleted = await JOBS.delete(
            job_id,
            expected_session_id=session_id,
        )
    return {"ok": True, "deleted": deleted}


@router.delete("/api/agent/sessions/{session_id}/jobs/{job_id}", dependencies=route_auth("agent_helper", 0))
async def delete_agent_session_job(
    request: Request,
    session_id: str,
    job_id: str,
) -> dict[str, Any]:
    async with session_lifecycle_lock(session_id):
        await authorize_provider_jobs_operation(
            request,
            session_id=session_id,
            operation="write",
        )
        deleted = await JOBS.delete(job_id, expected_session_id=session_id)
        return {"ok": True, "deleted": deleted}


@router.post("/api/jobs/{job_id}/run")
async def run_job(job_id: str) -> dict[str, Any]:
    return await JOBS.request_manual_run(job_id)

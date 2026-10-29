"""Chat consult endpoints; the application supplies the engine and the helper authorization.

Agent routes serve the in-turn `ask` helper and are bound to the asking run's
provider capability. User routes serve the app: the consults of a chat, the
approval of a deep consult, Stop, and questions the user asks a chat directly.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hyadex.core.http import route_auth
from hyadex.features.consult_engine import MAX_QUESTION_CHARS, ChatConsults, ConsultError

AGENT_BODY_LIMIT = 64 * 1024


class AgentConsultRequest(BaseModel):
    chat: str = Field(min_length=1, max_length=500)
    mode: str = "basic"
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{8,128}$")


class UserConsultRequest(BaseModel):
    mode: str = "deep"
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


class ConsultDecisionRequest(BaseModel):
    decision: str


class AskChatRequest(BaseModel):
    chat: str = Field(min_length=1, max_length=500)
    mode: str = "basic"
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


def create_consult_router(
    engine: ChatConsults,
    *,
    authorize_agent: Callable[..., Awaitable[dict[str, Any]]],
) -> APIRouter:
    router = APIRouter()

    async def run(action: Awaitable[Any]) -> Any:
        try:
            return await action
        except ConsultError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None

    async def capability(request: Request, session_id: str) -> dict[str, Any]:
        return await authorize_agent(request, action="ask", session_id=session_id)

    @router.get("/api/agent/sessions/{session_id}/consult-targets", dependencies=route_auth("agent_helper"))
    async def agent_consult_targets(request: Request, session_id: str) -> dict[str, Any]:
        await capability(request, session_id)
        try:
            return {"chats": engine.targets(session_id)}
        except ConsultError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None

    @router.post("/api/agent/sessions/{session_id}/consults",
                 dependencies=route_auth("agent_helper", AGENT_BODY_LIMIT))
    async def agent_start_consult(request: Request, session_id: str, req: AgentConsultRequest) -> dict[str, Any]:
        granted = await capability(request, session_id)
        return {"consult": await run(engine.start(
            source_id=session_id, run_id=str(granted.get("source_run_id") or "") or None,
            target=req.chat, mode=req.mode, question=req.question,
            request_id=req.request_id, lease=True,
        ))}

    @router.get("/api/agent/sessions/{session_id}/consults/{consult_id}", dependencies=route_auth("agent_helper"))
    async def agent_wait_consult(request: Request, session_id: str, consult_id: str,
                                 wait: float = Query(default=0.0, ge=0.0, le=60.0)) -> dict[str, Any]:
        await capability(request, session_id)
        return {"consult": await run(engine.wait(consult_id, source_id=session_id, timeout=wait))}

    @router.get("/api/sessions/{session_id}/consults")
    async def list_consults(session_id: str) -> dict[str, Any]:
        return engine.list(session_id)

    @router.post("/api/sessions/{session_id}/consults")
    async def ask_chat(session_id: str, req: UserConsultRequest) -> dict[str, Any]:
        # The user asks this chat directly (its side panel's 深度 switch).
        return {"consult": await run(engine.start(
            source_id=None, target=session_id, mode=req.mode, question=req.question))}

    @router.get("/api/sessions/{session_id}/consult-targets")
    async def consult_targets(session_id: str) -> dict[str, Any]:
        try:
            return {"chats": engine.targets(session_id)}
        except ConsultError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None

    @router.post("/api/sessions/{session_id}/ask")
    async def ask_from_chat(session_id: str, req: AskChatRequest) -> dict[str, Any]:
        # The user asks another chat from this one (@ in its composer).
        return {"consult": await run(engine.start(
            source_id=session_id, target=req.chat, mode=req.mode, question=req.question, by_user=True))}

    @router.post("/api/sessions/{session_id}/consults/{consult_id}/decision")
    async def decide_consult(session_id: str, consult_id: str, req: ConsultDecisionRequest) -> dict[str, Any]:
        return {"consult": await run(engine.decide(consult_id, source_id=session_id, decision=req.decision))}

    @router.post("/api/sessions/{session_id}/consults/{consult_id}/cancel")
    async def cancel_consult(session_id: str, consult_id: str) -> dict[str, Any]:
        return {"consult": await run(engine.cancel(consult_id, session_id=session_id))}

    @router.get("/api/consults/active")
    async def active_consults() -> dict[str, Any]:
        return {"consults": engine.active()}

    return router


__all__ = ["create_consult_router"]

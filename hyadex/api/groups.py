"""Group HTTP/WebSocket endpoints; the application supplies the engine and transport."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Awaitable, Callable
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from hyadex.features.group_engine import (
    GROUP_ID_RE,
    MAX_EVENTS_PAGE,
    CreateGroupRequest,
    GroupEngine,
    GroupError,
    GroupGoalRequest,
    ParticipantSpec,
    PostGroupMessageRequest,
    UpdateGroupRequest,
    hub_key,
)

def create_group_router(
    engine: GroupEngine,
    *,
    websocket_authorized: Callable[[WebSocket], bool],
    reserve: Callable[[str, WebSocket], Awaitable[bool]],
    register: Callable[[str, WebSocket], Awaitable[bool]],
    unsubscribe: Callable[[str, WebSocket], Awaitable[None]],
    subprotocol: Callable[[WebSocket], str | None] = lambda ws: None,
    send_timeout: float = 10.0,
) -> APIRouter:
    router = APIRouter()

    def require_id(group_id: str) -> str:
        if not GROUP_ID_RE.match(group_id):
            raise GroupError(status_code=404, detail="group not found")
        return group_id

    def require_discussion(group_id: str) -> str:
        if engine.require(require_id(group_id)).get("kind") == "autopilot":
            raise GroupError(status_code=409, detail="this group belongs to an autopilot run; use the run's controls")
        return group_id

    @router.get("/api/groups")
    async def list_groups(include_archived: bool = False) -> dict[str, Any]:
        return {"groups": engine.list(include_archived=include_archived)}

    @router.post("/api/groups")
    async def create_group(req: CreateGroupRequest) -> dict[str, Any]:
        return {"group": await engine.create(req)}

    @router.get("/api/groups/{group_id}")
    async def get_group(group_id: str) -> dict[str, Any]:
        return {"group": engine.public(engine.require(require_id(group_id)))}

    @router.patch("/api/groups/{group_id}")
    async def update_group(group_id: str, req: UpdateGroupRequest) -> dict[str, Any]:
        return {"group": await engine.update(require_discussion(group_id), req)}

    @router.delete("/api/groups/{group_id}")
    async def delete_group(group_id: str) -> dict[str, Any]:
        await engine.delete(require_discussion(group_id))
        return {"deleted": True, "id": group_id}

    @router.post("/api/groups/{group_id}/participants")
    async def add_participant(group_id: str, spec: ParticipantSpec) -> dict[str, Any]:
        return {"group": await engine.add_participant(require_discussion(group_id), spec)}

    @router.delete("/api/groups/{group_id}/participants/{handle}")
    async def remove_participant(group_id: str, handle: str) -> dict[str, Any]:
        return {"group": await engine.remove_participant(require_discussion(group_id), handle.lower())}

    @router.post("/api/groups/{group_id}/messages")
    async def post_message(group_id: str, req: PostGroupMessageRequest) -> dict[str, Any]:
        return await engine.post_message(require_discussion(group_id), req)

    @router.get("/api/groups/{group_id}/events")
    async def group_events(group_id: str, after: int = Query(default=0, ge=0),
                           limit: int = Query(default=200, ge=1, le=MAX_EVENTS_PAGE)) -> dict[str, Any]:
        engine.require(require_id(group_id))
        events = await asyncio.to_thread(engine.read_events, group_id, after=after, limit=limit)
        return {"events": events, "last_seq": engine.last_seq.get(group_id, 0)}

    @router.post("/api/groups/{group_id}/stop")
    async def stop_group(group_id: str) -> dict[str, Any]:
        return {"group": await engine.stop(require_id(group_id))}

    @router.put("/api/groups/{group_id}/goal")
    async def set_goal(group_id: str, req: GroupGoalRequest) -> dict[str, Any]:
        return await engine.set_goal(require_discussion(group_id), req)

    @router.websocket("/api/groups/{group_id}/events/stream")
    async def group_event_stream(group_id: str, ws: WebSocket, after: int = 0) -> None:
        selected = subprotocol(ws)
        if not websocket_authorized(ws):
            await ws.accept(subprotocol=selected)
            await ws.close(code=4401)
            return
        if not GROUP_ID_RE.match(group_id) or group_id not in engine.groups:
            await ws.accept(subprotocol=selected)
            await ws.close(code=4404)
            return
        await ws.accept(subprotocol=selected)
        key = hub_key(group_id)
        try:
            if not await reserve(key, ws):
                await ws.close(code=1013)
                return
            cursor = max(0, int(after or 0))
            while True:
                if group_id not in engine.groups:
                    await ws.close(code=4404)
                    return
                boundary = engine.last_seq.get(group_id, 0)
                while cursor < boundary:
                    page = await asyncio.to_thread(engine.read_events, group_id, after=cursor, through=boundary,
                                                   limit=MAX_EVENTS_PAGE)
                    if not page:
                        # Nothing readable remains up to the boundary.
                        cursor = boundary
                        break
                    for event in page:
                        await asyncio.wait_for(ws.send_json(event), timeout=send_timeout)
                    cursor = int(page[-1]["seq"])
                async with engine.lock(group_id):
                    if cursor >= engine.last_seq.get(group_id, 0):
                        if not await register(key, ws):
                            await ws.close(code=1013)
                            return
                        # A control frame, not a timeline event: it carries no
                        # ``seq``. Sent under the lock so it precedes live events.
                        await asyncio.wait_for(
                            ws.send_json({"type": "stream_ready", "group_id": group_id, "through_seq": cursor}),
                            timeout=send_timeout)
                        break
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except (asyncio.TimeoutError, ConnectionError):
            with suppress(Exception):
                await ws.close(code=1011)
        finally:
            await unsubscribe(key, ws)

    return router


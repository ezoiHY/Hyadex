"""History search across and within chats."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from hyadex.core.http import router
from hyadex.core.sessions import ensure_session_not_initializing, STORE
from hyadex.features.search import (
    active_history_search_session_ids,
    search_all_timelines,
    search_timeline_index,
)

router = APIRouter()


@router.get("/api/search")
async def search_all_session_timelines(
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    eligible_session_ids = active_history_search_session_ids()
    return await asyncio.to_thread(
        search_all_timelines,
        q,
        limit,
        eligible_session_ids,
    )


@router.get("/api/sessions/{session_id}/search")
async def search_session_timeline(
    session_id: str,
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    ensure_session_not_initializing(session_id)
    return await asyncio.to_thread(search_timeline_index, session_id, q, limit)

"""Streaming Codex reasoning summaries to live clients and persisting them."""
from __future__ import annotations

import asyncio
import logging.handlers
import time
from typing import Any

from hyadex.core.config import BACKEND_CODEX
from hyadex.core.event_log import reasoning_summary_stream_snapshot
from hyadex.core.paths import events_path, now_iso
from hyadex.core.sessions import append_event, event_delivery_lock, last_event_seq_from_file
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    REASONING_SUMMARY_STREAM_LAST_SENT,
    REASONING_SUMMARY_STREAM_PENDING,
    REASONING_SUMMARY_STREAMS,
)
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation

logger = logging.getLogger("hyadex-server")


async def broadcast_reasoning_summary_stream(session_id: str, packet: dict[str, Any]) -> None:
    try:
        await HUB.broadcast(session_id, packet)
    except Exception:
        logger.debug("reasoning summary live delivery failed session=%s", session_id)


async def flush_reasoning_summary_stream(session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        async with event_delivery_lock(session_id):
            if REASONING_SUMMARY_STREAM_PENDING.get(session_id) is not asyncio.current_task():
                return
            REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)
            REASONING_SUMMARY_STREAM_LAST_SENT[session_id] = time.monotonic()
            packet = reasoning_summary_stream_snapshot(session_id)
        await broadcast_reasoning_summary_stream(session_id, packet)
    finally:
        if REASONING_SUMMARY_STREAM_PENDING.get(session_id) is asyncio.current_task():
            REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)


async def update_reasoning_summary_stream(
    session_id: str, run_id: str, item_id: str, params: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    phase: str = "summary",
) -> None:
    """Keep provider-exposed summary and plaintext reasoning channels distinct."""
    if phase not in {"summary", "reasoning"}:
        return
    index = params.get("contentIndex" if phase == "reasoning" else "summaryIndex", 0)
    delta = params.get("delta", "")
    if not run_id or not item_id or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < 1024:
        return
    if not isinstance(delta, str):
        return
    async with event_delivery_lock(session_id):
        if session_id in DELETING_SESSIONS or session_id in DELETED_SESSION_TOMBSTONES:
            return
        streams = REASONING_SUMMARY_STREAMS.setdefault(session_id, {})
        state = streams.get((run_id, item_id, phase))
        first = state is None or not state["item"]["text"].strip()
        if state is None:
            after_seq = EVENT_SEQ_CACHE.get(session_id)
            if after_seq is None:
                after_seq = await asyncio.to_thread(last_event_seq_from_file, events_path(session_id))
            state = {"parts": {}, "item": {
                **dict(metadata or {}), "run_id": run_id, "item_id": item_id,
                "backend": (metadata or {}).get("backend", BACKEND_CODEX), "phase": phase, "text": "",
                "ts": now_iso(), "after_seq": after_seq,
            }}
            streams[(run_id, item_id, phase)] = state
        parts = state["parts"]
        parts[index] = parts.get(index, "") + delta
        state["item"]["text"] = "\n".join(parts[index] for index in sorted(parts))
        now = time.monotonic()
        delay = max(0.0, 0.05 - (now - REASONING_SUMMARY_STREAM_LAST_SENT.get(session_id, 0.0)))
        if not first and delay:
            if session_id not in REASONING_SUMMARY_STREAM_PENDING:
                REASONING_SUMMARY_STREAM_PENDING[session_id] = asyncio.create_task(
                    flush_reasoning_summary_stream(session_id, delay))
            return
        REASONING_SUMMARY_STREAM_LAST_SENT[session_id] = now
        packet = reasoning_summary_stream_snapshot(session_id)
    # Snapshot revisions fence a reconnect snapshot racing a newer update.
    # Socket writes remain outside the durable event delivery lock.
    await broadcast_reasoning_summary_stream(session_id, packet)


def reasoning_summary_stream_item(session_id: str, run_id: str, item_id: str, phase: str = "summary") -> dict[str, Any]:
    state = REASONING_SUMMARY_STREAMS.get(session_id, {}).get((run_id, item_id, phase))
    return dict(state["item"]) if state else {}


async def clear_reasoning_summary_stream(session_id: str, run_id: str, item_id: str | None = None, phase: str = "summary") -> None:
    async with event_delivery_lock(session_id):
        streams = REASONING_SUMMARY_STREAMS.get(session_id)
        if not streams:
            return
        removed = [key for key in streams if key[0] == run_id and (item_id is None or (key[1] == item_id and key[2] == phase))]
        if not removed:
            return
        for key in removed:
            del streams[key]
        if not streams:
            REASONING_SUMMARY_STREAMS.pop(session_id, None)
            REASONING_SUMMARY_STREAM_LAST_SENT.pop(session_id, None)
        pending = REASONING_SUMMARY_STREAM_PENDING.pop(session_id, None)
        if pending:
            pending.cancel()
        packet = reasoning_summary_stream_snapshot(session_id)
    if pending:
        await asyncio.gather(pending, return_exceptions=True)
    await broadcast_reasoning_summary_stream(session_id, packet)


async def persist_reasoning_summary(
    session_id: str, payload: dict[str, Any], completed_item_ids: set[str | tuple[str, str]],
) -> None:
    # Join the append and identity update together: cancellation after the
    # ledger write must not make the finalizer append the item a second time.
    async def commit() -> None:
        await append_event(session_id, "reasoning_text" if payload.get("phase") == "reasoning" else "reasoning_summary", payload)
        phase = payload.get("phase", "summary")
        completed_item_ids.add(("reasoning", payload["item_id"]) if phase == "reasoning" else payload["item_id"])
        await clear_reasoning_summary_stream(session_id, payload["run_id"], payload["item_id"], phase)
    task = asyncio.create_task(commit())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(task)
        raise


async def finish_reasoning_summary_stream(
    session_id: str, run_id: str, completed_item_ids: set[str | tuple[str, str]],
) -> None:
    """Keep exposed text already received when interruption omits item/completed."""
    items = [dict(state["item"]) for (owner, _, _), state in
             REASONING_SUMMARY_STREAMS.get(session_id, {}).items() if owner == run_id]
    try:
        for item in items:
            item_id = item["item_id"]
            identity = ("reasoning", item_id) if item.get("phase") == "reasoning" else item_id
            if identity in completed_item_ids or not item["text"].strip():
                continue
            payload = {key: value for key, value in item.items() if key not in {"ts", "after_seq"}}
            payload.update(partial=True, reasoning_after_seq=item["after_seq"])
            await persist_reasoning_summary(session_id, payload, completed_item_ids)
    except Exception:
        # Storage failure must not strand the provider subscription or turn
        # ownership while finalizers are releasing this chat.
        logger.exception("could not preserve partial reasoning summary session=%s run=%s", session_id, run_id)
    finally:
        await clear_reasoning_summary_stream(session_id, run_id)

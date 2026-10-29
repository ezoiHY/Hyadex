"""Streaming assistant text to live clients while a provider is still writing it.

An additive, non-durable lane beside the reasoning summary stream: clients that
open a session event socket with ``text_stream=true`` receive
``assistant_text_stream`` snapshots of each answer still being written. The
finished answer arrives as the ordinary durable ``assistant_text`` event, which
clears its live item. A run that ends before an answer finishes (Stop, an
interrupt, a failure) keeps the text received so far as an ``assistant_text``
marked ``partial``; after a normal completion an unfinished answer stays
transient.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from hyadex.core.config import SERVER_INSTANCE_ID
from hyadex.core.event_seq import last_event_seq_from_file
from hyadex.core.paths import events_path, now_iso
from hyadex.core.sessions import append_event, event_delivery_lock
from hyadex.core.state import DELETED_SESSION_TOMBSTONES, DELETING_SESSIONS, EVENT_SEQ_CACHE
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation

logger = logging.getLogger("hyadex-server")

ASSISTANT_TEXT_STREAM_PACKET = "assistant_text_stream"
# Like the reasoning lane: at most one snapshot per session every 50 ms, and a
# bounded packet however long the answers grow.
ASSISTANT_TEXT_STREAM_INTERVAL_SECONDS = 0.05
ASSISTANT_TEXT_STREAM_PACKET_CHARS = 1_000_000

# session id -> (run id, item id) -> {"item": public fields, text}
ASSISTANT_TEXT_STREAMS: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
ASSISTANT_TEXT_STREAM_PENDING: dict[str, asyncio.Task[Any]] = {}
ASSISTANT_TEXT_STREAM_LAST_SENT: dict[str, float] = {}
_revision = 0


def assistant_text_stream_snapshot(session_id: str) -> dict[str, Any]:
    """Every answer of this chat still being written; never advances the event cursor."""
    global _revision
    _revision += 1
    remaining = ASSISTANT_TEXT_STREAM_PACKET_CHARS
    items = []
    for state in ASSISTANT_TEXT_STREAMS.get(session_id, {}).values():
        item = dict(state["item"])
        if not item["text"].strip():
            continue
        if len(item["text"]) > remaining:
            # Keep the newest text: it is the part being written.
            item["text"] = item["text"][-remaining:]
            item["text_truncated"] = True
        remaining -= len(item["text"])
        items.append(item)
        if not remaining or len(items) == 256:
            break
    return {
        "type": ASSISTANT_TEXT_STREAM_PACKET, "session_id": session_id,
        "instance_id": SERVER_INSTANCE_ID, "revision": _revision, "items": items,
    }


async def broadcast_assistant_text_stream(session_id: str, packet: dict[str, Any]) -> None:
    try:
        await HUB.broadcast(session_id, packet)
    except Exception:
        logger.debug("assistant text live delivery failed session=%s", session_id)


async def flush_assistant_text_stream(session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        async with event_delivery_lock(session_id):
            if ASSISTANT_TEXT_STREAM_PENDING.get(session_id) is not asyncio.current_task():
                return
            ASSISTANT_TEXT_STREAM_PENDING.pop(session_id, None)
            ASSISTANT_TEXT_STREAM_LAST_SENT[session_id] = time.monotonic()
            packet = assistant_text_stream_snapshot(session_id)
        await broadcast_assistant_text_stream(session_id, packet)
    finally:
        if ASSISTANT_TEXT_STREAM_PENDING.get(session_id) is asyncio.current_task():
            ASSISTANT_TEXT_STREAM_PENDING.pop(session_id, None)


async def update_assistant_text_stream(
    session_id: str, run_id: str, item_id: str, delta: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one provider text delta to the answer ``item_id`` of ``run_id``."""
    if not run_id or not item_id or not isinstance(delta, str) or not delta:
        return
    async with event_delivery_lock(session_id):
        if session_id in DELETING_SESSIONS or session_id in DELETED_SESSION_TOMBSTONES:
            return
        streams = ASSISTANT_TEXT_STREAMS.setdefault(session_id, {})
        state = streams.get((run_id, item_id))
        first = state is None or not state["item"]["text"].strip()
        if state is None:
            after_seq = EVENT_SEQ_CACHE.get(session_id)
            if after_seq is None:
                after_seq = await asyncio.to_thread(last_event_seq_from_file, events_path(session_id))
            state = {"item": {
                **dict(metadata or {}), "run_id": run_id, "item_id": item_id, "text": "",
                "ts": now_iso(), "after_seq": after_seq,
            }}
            streams[(run_id, item_id)] = state
        state["item"]["text"] += delta
        now = time.monotonic()
        delay = max(0.0, ASSISTANT_TEXT_STREAM_INTERVAL_SECONDS
                    - (now - ASSISTANT_TEXT_STREAM_LAST_SENT.get(session_id, 0.0)))
        if not first and delay:
            if session_id not in ASSISTANT_TEXT_STREAM_PENDING:
                ASSISTANT_TEXT_STREAM_PENDING[session_id] = asyncio.create_task(
                    flush_assistant_text_stream(session_id, delay))
            return
        ASSISTANT_TEXT_STREAM_LAST_SENT[session_id] = now
        packet = assistant_text_stream_snapshot(session_id)
    await broadcast_assistant_text_stream(session_id, packet)


def assistant_text_stream_item(session_id: str, run_id: str, item_id: str) -> dict[str, Any]:
    state = ASSISTANT_TEXT_STREAMS.get(session_id, {}).get((run_id, item_id))
    return dict(state["item"]) if state else {}


async def clear_assistant_text_stream(session_id: str, run_id: str, item_id: str | None = None) -> None:
    """Drop live answers of ``run_id`` (one, or all) once they are durable or abandoned."""
    async with event_delivery_lock(session_id):
        streams = ASSISTANT_TEXT_STREAMS.get(session_id)
        if not streams:
            return
        removed = [key for key in streams if key[0] == run_id and (item_id is None or key[1] == item_id)]
        if not removed:
            return
        for key in removed:
            del streams[key]
        if not streams:
            ASSISTANT_TEXT_STREAMS.pop(session_id, None)
            ASSISTANT_TEXT_STREAM_LAST_SENT.pop(session_id, None)
        pending = ASSISTANT_TEXT_STREAM_PENDING.pop(session_id, None)
        if pending:
            pending.cancel()
        packet = assistant_text_stream_snapshot(session_id)
    if pending:
        await asyncio.gather(pending, return_exceptions=True)
    await broadcast_assistant_text_stream(session_id, packet)


async def move_assistant_text_stream(session_id: str, run_id: str, new_run_id: str) -> None:
    """Carry answers still being written into the logical run that continues them."""
    async with event_delivery_lock(session_id):
        streams = ASSISTANT_TEXT_STREAMS.get(session_id) or {}
        for key in [key for key in streams if key[0] == run_id]:
            state = streams.pop(key)
            state["item"]["run_id"] = new_run_id
            streams.setdefault((new_run_id, key[1]), state)


async def finish_assistant_text_stream(
    session_id: str, run_id: str, completed_item_ids: set[str], *, keep: bool = True,
) -> None:
    """End the run's live answers, keeping those shown if it ended early (``keep``)."""
    items = [dict(state["item"]) for (owner, item_id), state in
             ASSISTANT_TEXT_STREAMS.get(session_id, {}).items()
             if keep and owner == run_id and item_id not in completed_item_ids]
    try:
        for item in items:
            if not item["text"].strip():
                continue
            payload = {key: value for key, value in item.items() if key not in {"ts", "after_seq"}}
            payload.update(partial=True, text_after_seq=item["after_seq"])

            async def commit(payload: dict[str, Any] = payload) -> None:
                await append_event(session_id, "assistant_text", payload)
                completed_item_ids.add(payload["item_id"])
            # Cancellation after the ledger write must not lose the identity
            # that keeps a later finalizer from appending the answer again.
            task = asyncio.create_task(commit())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await join_task_despite_caller_cancellation(task)
                raise
    except Exception:
        # Storage failure must not strand the provider subscription or turn
        # ownership while finalizers are releasing this chat.
        logger.exception("could not preserve partial assistant text session=%s run=%s", session_id, run_id)
    finally:
        await clear_assistant_text_stream(session_id, run_id)

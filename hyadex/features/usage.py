"""Live client hub and provider context and account usage signals."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging.handlers
import math
import signal
from typing import Any

from fastapi import WebSocket

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    EVENT_WEBSOCKET_DELIVERY_FAILURE_CLOSE_CODE,
    EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL,
    EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION,
    EVENT_WEBSOCKET_OVER_CAP_CLOSE_CODE,
    MAX_CODEX_INTERACTION_TEXT_CHARS,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from hyadex.core.server_logging import concise_error_message
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers import usage as provider_usage

logger = logging.getLogger("hyadex-server")


class SubscriberHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = {}
        # A socket reserves capacity before potentially long catch-up I/O, but
        # does not join live broadcasts until the event-delivery boundary is
        # closed. This makes admission bounded without disturbing ordering.
        self._reservations: dict[str, set[WebSocket]] = {}
        self._reasoning_subscribers: set[WebSocket] = set()
        self._reasoning_text_subscribers: set[WebSocket] = set()
        # Sockets that asked for answers while they are written (text_stream).
        self._text_subscribers: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def _connection_count_locked(self) -> int:
        return sum(len(items) for items in self._subscribers.values()) + sum(
            len(items) for items in self._reservations.values()
        )

    async def reserve(self, sid: str, ws: WebSocket) -> bool:
        """Atomically reserve global and per-session event-socket capacity."""

        async with self._lock:
            if ws in self._subscribers.get(sid, set()) or ws in self._reservations.get(sid, set()):
                return True
            session_count = len(self._subscribers.get(sid, set())) + len(
                self._reservations.get(sid, set())
            )
            if (
                self._connection_count_locked() >= EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL
                or session_count >= EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION
            ):
                return False
            self._reservations.setdefault(sid, set()).add(ws)
            return True

    async def subscribe(self, sid: str, ws: WebSocket) -> bool:
        await ws.accept()
        activated = False
        try:
            if not await self.reserve(sid, ws):
                await close_event_websocket_over_capacity(ws)
                return False
            activated = await self.register_accepted(sid, ws)
            if not activated:
                await close_event_websocket_over_capacity(ws)
            return activated
        finally:
            if not activated:
                await self.unsubscribe(sid, ws)

    async def unsubscribe(self, sid: str, ws: WebSocket) -> None:
        async with self._lock:
            self._reasoning_subscribers.discard(ws)
            self._reasoning_text_subscribers.discard(ws)
            self._text_subscribers.discard(ws)
            subs = self._subscribers.get(sid)
            if subs:
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(sid, None)
            reservations = self._reservations.get(sid)
            if reservations:
                reservations.discard(ws)
                if not reservations:
                    self._reservations.pop(sid, None)

    async def register_accepted(
        self, sid: str, ws: WebSocket, *, reasoning_stream: bool = False, reasoning_text: bool = False,
        text_stream: bool = False,
    ) -> bool:
        """Activate a reserved socket after it has completed catch-up."""

        async with self._lock:
            if ws in self._subscribers.get(sid, set()):
                return True
            reserved = self._reservations.get(sid)
            if not reserved or ws not in reserved:
                # Keep direct callers safe: admission is still atomic if they
                # did not explicitly reserve before their catch-up work.
                session_count = len(self._subscribers.get(sid, set())) + len(
                    self._reservations.get(sid, set())
                )
                if (
                    self._connection_count_locked() >= EVENT_WEBSOCKET_MAX_ACTIVE_GLOBAL
                    or session_count >= EVENT_WEBSOCKET_MAX_ACTIVE_PER_SESSION
                ):
                    return False
            else:
                reserved.discard(ws)
                if not reserved:
                    self._reservations.pop(sid, None)
            self._subscribers.setdefault(sid, set()).add(ws)
            if reasoning_stream:
                self._reasoning_subscribers.add(ws)
                if reasoning_text:
                    self._reasoning_text_subscribers.add(ws)
            if text_stream:
                self._text_subscribers.add(ws)
            return True

    async def broadcast(self, sid: str, event: dict[str, Any]) -> None:
        transient = event.get("type") == "reasoning_summary_stream"
        live_text = event.get("type") == "assistant_text_stream"
        async with self._lock:
            subs = [ws for ws in self._subscribers.get(sid, set())
                    if (not transient or ws in self._reasoning_subscribers)
                    and (not live_text or ws in self._text_subscribers)]

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                packet = event
                if transient and ws not in self._reasoning_text_subscribers:
                    packet = {**event, "items": [item for item in event.get("items", [])
                        if item.get("phase") != "reasoning"]}
                await asyncio.wait_for(
                    ws.send_json(packet),
                    timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
                )
                return None
            except Exception:
                return ws

        async def deliver_and_evict() -> None:
            stale = [
                ws
                for ws in await asyncio.gather(*(send(ws) for ws in subs))
                if ws is not None
            ]
            if not stale:
                return

            # A failed send does not necessarily wake the endpoint blocked in
            # receive_text(). Close the transport before releasing its capacity
            # lease so a slow client cannot repeatedly time out, reconnect, and
            # accumulate unaccounted sockets/tasks. If close itself fails, keep
            # the socket counted until the endpoint's finally block unsubscribes.
            closed = await asyncio.gather(
                *(close_event_websocket_after_delivery_failure(ws) for ws in stale)
            )
            releasable = [
                ws for ws, close_succeeded in zip(stale, closed)
                if close_succeeded
            ]
            if not releasable:
                return
            async with self._lock:
                current = self._subscribers.get(sid, set())
                for ws in releasable:
                    current.discard(ws)
                    self._reasoning_subscribers.discard(ws)
                    self._reasoning_text_subscribers.discard(ws)
                    self._text_subscribers.discard(ws)
                if not current:
                    self._subscribers.pop(sid, None)

        # Broadcast is part of the durable event projection path. Once a send
        # has failed, caller cancellation must not interrupt the close/accounting
        # transaction and recreate the resource leak this eviction fixes.
        delivery_task = asyncio.create_task(deliver_and_evict())
        try:
            await asyncio.shield(delivery_task)
        except asyncio.CancelledError:
            await join_task_despite_caller_cancellation(delivery_task)
            raise


HUB = SubscriberHub()


async def close_event_websocket_over_capacity(ws: WebSocket) -> None:
    with suppress(Exception):
        await asyncio.wait_for(
            ws.close(
                code=EVENT_WEBSOCKET_OVER_CAP_CLOSE_CODE,
                reason="event stream capacity reached; retry later",
            ),
            timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
        )


async def close_event_websocket_after_delivery_failure(ws: WebSocket) -> bool:
    """Close one failed event socket before releasing its capacity lease."""

    try:
        await asyncio.wait_for(
            ws.close(
                code=EVENT_WEBSOCKET_DELIVERY_FAILURE_CLOSE_CODE,
                reason="event stream delivery failed; reconnect required",
            ),
            timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
        )
    except Exception:
        return False
    return True


async def join_task_despite_caller_cancellation(task: asyncio.Task[Any]) -> Any:
    """Wait for an already-started side effect before re-raising cancellation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


def bounded_codex_interaction_value(
    value: Any,
    *,
    remaining: int = MAX_CODEX_INTERACTION_TEXT_CHARS,
) -> Any:
    """Bound untrusted app-server prompt data before persistence or UI delivery."""
    if remaining <= 0:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:remaining]
    if isinstance(value, list):
        output: list[Any] = []
        budget = remaining
        for item in value[:100]:
            bounded = bounded_codex_interaction_value(item, remaining=budget)
            output.append(bounded)
            budget -= len(json.dumps(bounded, ensure_ascii=False, default=str))
            if budget <= 0:
                break
        return output
    if isinstance(value, dict):
        output_dict: dict[str, Any] = {}
        budget = remaining
        for raw_key, item in list(value.items())[:100]:
            key = str(raw_key)[:240]
            bounded = bounded_codex_interaction_value(item, remaining=budget)
            output_dict[key] = bounded
            budget -= len(key) + len(
                json.dumps(bounded, ensure_ascii=False, default=str)
            )
            if budget <= 0:
                break
        return output_dict
    return str(value)[:remaining]


def current_provider_context_usage_generation(session: dict[str, Any]) -> int:
    value = session.get("_context_usage_generation")
    return (
        max(0, int(value))
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
        else 0
    )


def next_provider_context_usage_generation(session: dict[str, Any]) -> int:
    generation = current_provider_context_usage_generation(session) + 1
    session["_context_usage_generation"] = generation
    return generation


def provider_context_usage_signal(
    backend: str,
    *,
    state: str,
    generation: int,
    provider_session_id: str | None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded, non-timeline runtime invalidation packet."""

    bounded_snapshot = (
        bounded_codex_interaction_value(snapshot, remaining=4_000)
        if isinstance(snapshot, dict)
        else None
    )
    return {
        "type": "provider_runtime_changed",
        "runtime": "context_usage",
        "ephemeral": True,
        "backend": backend,
        "context_usage_state": state,
        "usage_generation": generation,
        "provider_session_id": provider_session_id,
        "context_usage_snapshot": bounded_snapshot,
        "context_usage": bounded_snapshot,
    }


async def broadcast_provider_runtime_changed(
    session_id: str,
    signal: dict[str, Any],
) -> None:
    """Broadcast control state without allocating a durable event sequence."""

    try:
        await HUB.broadcast(session_id, {**signal, "session_id": session_id})
    except Exception as exc:
        logger.debug(
            "provider runtime broadcast failed session=%s: %s",
            session_id,
            concise_error_message(exc),
        )


def clear_provider_context_usage_locked(
    session: dict[str, Any],
    backend: str,
    *,
    provider_session_id: str | None,
    state: str,
) -> dict[str, Any]:
    generation = next_provider_context_usage_generation(session)
    session["context_usage_state"] = state
    session.pop("context_usage_snapshot", None)
    if backend == BACKEND_CLAUDE:
        session.pop("claude_context_usage_snapshot", None)
    elif backend == BACKEND_CODEX:
        # codex exec cannot produce authoritative app-server telemetry. Clear
        # every legacy alias and attribution marker so an exec/fallback turn
        # can never keep displaying, or later relabel, the previous native
        # turn's context occupancy.
        session.pop("codex_token_usage", None)
        session.pop("codex_token_usage_snapshot", None)
        session.pop("_codex_token_usage_checkpoint", None)
        session.pop("_codex_token_usage_terminal", None)
    return provider_context_usage_signal(
        backend,
        state=state,
        generation=generation,
        provider_session_id=provider_session_id,
    )


def context_usage_runtime_fields(
    session: dict[str, Any],
    *,
    backend: str,
) -> dict[str, Any]:
    snapshot = session.get("context_usage_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = (
            session.get("claude_context_usage_snapshot")
            if backend == BACKEND_CLAUDE
            else session.get("codex_token_usage_snapshot")
        )
    if not isinstance(snapshot, dict):
        snapshot = None
    state = str(session.get("context_usage_state") or "")
    if state not in {"available", "cleared", "unavailable"}:
        state = "available" if snapshot is not None else "unavailable"
    usage_generation = current_provider_context_usage_generation(session)
    return {
        "context_usage_state": state,
        "context_usage_snapshot": dict(snapshot) if snapshot is not None else None,
        "context_usage": dict(snapshot) if snapshot is not None else None,
        "usage_generation": usage_generation,
        "provider_session_id": str(session_provider_id(session) or "") or None,
    }


PROVIDER_USAGE = provider_usage.ProviderUsage()


async def broadcast_provider_usage_changed(session_id: str, backend: str) -> None:
    await broadcast_provider_runtime_changed(session_id, {
        "type": "provider_usage_changed", "backend": backend, "ephemeral": True,
    })

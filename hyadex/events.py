"""In-process fan-out of committed chat events.

The event log writes and sequences events; it does not know who consumes them.
Consumers subscribe here instead: live sockets, group discussions following
their member chats, and anything added later.

Two kinds of subscriber, matching the two guarantees the log gives:

- ``deliver`` subscribers are awaited in registration order while the chat's
  event lock is still held, so they see that chat's events strictly in
  sequence order. Keep them fast; a failure is logged and skips that
  subscriber for this event only.
- ``observe`` subscribers are called synchronously after the event is
  committed and must not block: they only schedule their own work.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
import logging
from typing import Any

logger = logging.getLogger("hyadex-server")

Deliver = Callable[[str, dict[str, Any]], Awaitable[None]]
Observe = Callable[[str, dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._deliver: list[Deliver] = []
        self._observe: list[tuple[Observe, frozenset[str] | None]] = []

    def on_deliver(self, subscriber: Deliver) -> Deliver:
        self._deliver.append(subscriber)
        return subscriber

    def on_observe(self, subscriber: Observe, *, types: Collection[str] | None = None) -> Observe:
        self._observe.append((subscriber, frozenset(types) if types is not None else None))
        return subscriber

    async def deliver(self, session_id: str, event: dict[str, Any]) -> None:
        for subscriber in self._deliver:
            try:
                await subscriber(session_id, event)
            except Exception as exc:
                logger.warning("event delivery failed session=%s seq=%s subscriber=%s: %s",
                               session_id, event.get("seq"), getattr(subscriber, "__name__", subscriber),
                               type(exc).__name__)

    def observe(self, session_id: str, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        for subscriber, types in self._observe:
            if types is not None and event_type not in types:
                continue
            try:
                subscriber(session_id, event)
            except Exception:
                logger.exception("event observer failed session=%s type=%s", session_id, event_type)

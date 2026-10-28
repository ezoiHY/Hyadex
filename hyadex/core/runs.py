"""The one owner of in-memory turn lifecycle state.

A chat's turn passes through admission, the provider run, steering, stop and
the durable queue. Each step used to keep its own module-level registry; they
live together here so a turn's state has one place, one set of locks, and
queries that mean the same thing everywhere.

Nothing here is persisted: queued rows are durable in the chat's event log and
rebuilt into ``queued`` on startup.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable


def queued_turn_is_held(item: dict[str, Any]) -> bool:
    """The row waits for the user: Stop held it or its delivery is uncertain.

    A held row keeps its place but never blocks the rows behind it; it runs
    only after an explicit queue resume or Force Send.
    """
    return (
        item.get("_paused_after_stop") is True
        or item.get("_native_delivery_fenced") is True
    )


def first_runnable_queued_index(items: Iterable[dict[str, Any]] | None) -> int | None:
    """Index of the first queued row that is not held, in queue order."""
    for index, item in enumerate(items or ()):
        if not queued_turn_is_held(item):
            return index
    return None


@dataclass(eq=False)
class RunState:
    # --- Provider slot (guarded by active_lock) ---------------------------
    active_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The live run owning each chat's provider slot.
    active: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Chats whose provider slot is reserved, from admission until release.
    busy: set[str] = field(default_factory=set)
    # The accepted turn each chat is starting or running.
    current_turns: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per-run bookkeeping kept from admission to the terminal event.
    run_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Tasks owning each chat's turn start and provider run.
    turn_tasks: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)
    # Short provider-maintenance requests (for example, loading a selected
    # Codex thread) take part in admission without impersonating a user turn.
    maintenance: set[str] = field(default_factory=set)

    # --- Stop -------------------------------------------------------------
    stop_requests: set[str] = field(default_factory=set)
    stopped_runs: set[str] = field(default_factory=set)
    # Explicit Stop owns only the short admission edge under the chat's
    # lifecycle lock; provider teardown continues afterwards. The operation
    # stays an admission fence until teardown finishes, so a replacement turn
    # cannot bind while cleanup for the old owner is still running.
    explicit_stops: dict[str, asyncio.Task[dict[str, Any]]] = field(default_factory=dict)
    # Inner teardown that outlives the explicit Stop, so Delete cannot remove
    # durable state while the old owner can still append events.
    detached_stop_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    detached_stop_tasks_by_session: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)

    # --- Steering -----------------------------------------------------------
    steering: set[str] = field(default_factory=set)
    # A non-native Force Send keeps the steering fence while the interrupted
    # provider releases its slot. Track that short-lived owner explicitly so a
    # cancelled waiter cannot leave an ownerless queue barrier.
    steering_waits: dict[str, tuple[str, float, asyncio.Task[Any]]] = field(default_factory=dict)

    # --- Durable queue (guarded by queue_lock) ------------------------------
    queue_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queued: dict[str, deque[dict[str, Any]]] = field(default_factory=dict)
    # Rows promoted by Force Send / Run now, until their run starts.
    run_now: dict[str, dict[str, Any]] = field(default_factory=dict)
    run_now_request_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run_now_requests: dict[str, tuple[str, asyncio.Task[dict[str, Any]]]] = field(default_factory=dict)
    # One queue promotion owner per chat. Without it, two drain tasks can pop
    # A then B before either reserves the provider slot, and whichever reaches
    # the lifecycle lock first can run B ahead of A.
    queue_start_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)

    # --- Queries (call under the lock that guards the state they read) -------
    def holds_turn(self, session_id: str) -> bool:
        """A turn owns, reserves or is binding the chat's provider slot."""
        return session_id in self.active or session_id in self.busy or session_id in self.current_turns

    def has_waiting_work(self, session_id: str) -> bool:
        """Queued rows, a promoted Run-now row, or a Force Send in progress."""
        return bool(self.queued.get(session_id) or self.run_now.get(session_id)) or session_id in self.steering

    def has_runnable_waiting_work(self, session_id: str) -> bool:
        """Like ``has_waiting_work``, ignoring queued rows held for the user."""
        return (
            bool(self.run_now.get(session_id))
            or session_id in self.steering
            or first_runnable_queued_index(self.queued.get(session_id)) is not None
        )


RUNS = RunState()


# Module globals each attribute replaced; tests compiling agent_server
# functions in isolation still provide state under these names.
LEGACY_NAMES: dict[str, str] = {
    "ACTIVE_LOCK": "active_lock",
    "ACTIVE": "active",
    "BUSY_SESSIONS": "busy",
    "CURRENT_TURNS": "current_turns",
    "RUN_METADATA": "run_metadata",
    "SESSION_TURN_TASKS": "turn_tasks",
    "SERVER_MAINTENANCE_SESSIONS": "maintenance",
    "STOP_REQUESTS": "stop_requests",
    "STOPPED_RUNS": "stopped_runs",
    "EXPLICIT_STOP_OPERATIONS": "explicit_stops",
    "DETACHED_STOP_TASKS": "detached_stop_tasks",
    "DETACHED_STOP_TASKS_BY_SESSION": "detached_stop_tasks_by_session",
    "STEERING_SESSIONS": "steering",
    "STEERING_WAIT_TASKS": "steering_waits",
    "QUEUE_LOCK": "queue_lock",
    "QUEUED_TURNS": "queued",
    "RUN_NOW_TURNS": "run_now",
    "RUN_NOW_REQUEST_LOCK": "run_now_request_lock",
    "RUN_NOW_REQUESTS": "run_now_requests",
    "QUEUE_START_TASKS": "queue_start_tasks",
}


class NamespaceView:
    """A state object read through a namespace's legacy names (test support).

    Tests that compile server functions into their own namespace still provide
    state under the old global names. Lookups happen on every access, so a test
    that replaces ``ns["ACTIVE"]`` is seen by code compiled against
    ``RUNS.active``. Methods of ``state_class`` work on the view.
    """

    def __init__(self, namespace: dict[str, Any], legacy_names: dict[str, str], state_class: type) -> None:
        object.__setattr__(self, "_namespace", namespace)
        object.__setattr__(self, "_names", {attribute: name for name, attribute in legacy_names.items()})
        object.__setattr__(self, "_class", state_class)

    def __getattr__(self, attribute: str) -> Any:
        method = getattr(self._class, attribute, None)
        if callable(method):
            return method.__get__(self)
        try:
            return self._namespace[self._names[attribute]]
        except KeyError:
            raise AttributeError(attribute) from None

    def __setattr__(self, attribute: str, value: Any) -> None:
        self._namespace[self._names[attribute]] = value


def NamespaceRuns(namespace: dict[str, Any]) -> NamespaceView:
    return NamespaceView(namespace, LEGACY_NAMES, RunState)

"""Shared registries, locks and limits that several layers read: event sequencing caches, timeline index caches, session lifecycle sets and queue recovery bookkeeping."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
from typing import Any, Literal
import weakref

from hyadex.core.config import hyadex_setting
from hyadex.events import EventBus

EVENT_SEQ_CACHE: dict[str, int] = {}
EVENT_SEQ_LOCK = asyncio.Lock()
EVENT_SEQ_REPAIR_LOCKS: dict[str, asyncio.Lock] = {}
REASONING_SUMMARY_STREAMS: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}


REASONING_SUMMARY_STREAM_PENDING: dict[str, asyncio.Task[Any]] = {}
REASONING_SUMMARY_STREAM_LAST_SENT: dict[str, float] = {}
EVENT_DELIVERY_LOCKS: dict[str, asyncio.Lock] = {}


TIMELINE_INDEX_CACHE_MAX = int(hyadex_setting("TIMELINE_INDEX_CACHE_MAX", "24"))
TIMELINE_INDEX_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
TIMELINE_INDEX_PROJECTION_VERSION = 7
# Retained as a compatibility/testing surface; synchronization uses the fixed
# stripe pool below so deleted-session churn cannot leak one lock per chat.
TIMELINE_INDEX_LOCKS: dict[str, threading.Lock] = {}
# Cache membership and the per-session lock registry are shared across every
# request worker.  Protect them independently from the per-session scan locks:
# an LRU eviction for chat A must never remove or recreate chat B's lock while
# chat B is building/reading its index.
TIMELINE_INDEX_CACHE_LOCK = threading.RLock()
TIMELINE_INDEX_LOCK_STRIPES = tuple(threading.Lock() for _ in range(64))


EVENT_INDEX_VERSION = 1
# One sparse checkpoint per this many events keeps events.idx tiny (a
# 100k-event chat needs ~200 entries) while bounding a catch-up scan to
# at most one stride of already-delivered lines.
EVENT_INDEX_STRIDE = 512
# Below this size a full scan is cheaper than building an index for it.
EVENT_INDEX_MIN_FILE_BYTES = 256 * 1024
EVENT_LINE_SEQ_RE = re.compile(rb'^\s*\{"seq":\s*(\d+)')


AGENT_HELPER_PROXY_HEADERS = {
    b"forwarded",
    b"via",
    b"x-forwarded-for",
    b"x-forwarded-host",
    b"x-forwarded-proto",
    b"x-real-ip",
}


SESSION_LIFECYCLE_UPDATE_FIELDS = frozenset({
    "cwd",
    "backend",
    "model",
    "effort",
    "system_prompt",
    "claude_permission_mode",
    "codex_approval_policy",
    "codex_sandbox_mode",
    "codex_permission_profile",
    "codex_approvals_reviewer",
    "provider_jobs_access",
    "archived",
})


NOTIFICATION_EVENT_FIELDS = frozenset({
    "id", "seq", "type", "ts", "session_id", "run_id", "message",
    "notification_level", "notification_request_id", "notification_request_digest",
})


# Serializes concurrent sessions.json writers (worker threads and the
# shutdown fallback) so two writers never interleave on the shared temp file.
SESSIONS_WRITE_LOCK = threading.Lock()


SESSION_STORE_SAVE_DEBOUNCE_SECONDS = max(
    0.0,
    float(hyadex_setting("SESSIONS_SAVE_DEBOUNCE_SECONDS", "0.25")),
)
SESSION_STORE_SAVE_MAX_LATENCY_SECONDS = max(
    SESSION_STORE_SAVE_DEBOUNCE_SECONDS,
    float(hyadex_setting("SESSIONS_SAVE_MAX_LATENCY_SECONDS", "2.0")),
)


# Committed chat events fan out here; the event log does not know its consumers.
EVENTS = EventBus()


QUEUE_RECOVERY_READY: set[str] = set()
QUEUE_CHECKPOINT_DIRTY: set[str] = set()
QUEUE_RECOVERY_ADMISSION_WAIT_SECONDS = 2.0
RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS = max(
    5.0,
    float(
        hyadex_setting(
            "RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS",
            "120",
        )
    ),
)
RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS = max(
    RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
    float(
        hyadex_setting(
            "RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS",
            "300",
        )
    ),
)
RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS = max(
    1.0,
    float(
        hyadex_setting(
            "RUN_NOW_IDLE_OWNER_CLEANUP_TIMEOUT_SECONDS",
            "15",
        )
    ),
)
RUN_NOW_COMPLETED_TTL_SECONDS = max(
    1.0,
    float(hyadex_setting("RUN_NOW_COMPLETED_TTL_SECONDS", "120")),
)
RUN_NOW_COMPLETED_MAX = max(
    16,
    int(hyadex_setting("RUN_NOW_COMPLETED_MAX", "512")),
)
RUN_NOW_COMPLETED_RESULTS: OrderedDict[
    tuple[str, str],
    dict[str, Any],
] = OrderedDict()


SESSION_LIFECYCLE_LOCKS: dict[str, asyncio.Lock] = {}
DELETING_SESSIONS: set[str] = set()
DELETED_SESSION_TOMBSTONES: set[str] = set()


QUEUE_FENCE_LOGGED_AT: dict[tuple[str, str], float] = {}


LIVE_STDOUT_MAX_LINES = 400
LIVE_STDOUT_MAX_LINE_CHARS = 12_000


LEGACY_STEERING_PREFIX = (
    "The previous agent turn was interrupted before completion. Continue that request while "
    "applying the steering instruction below. Treat both messages as user input, and give the "
    "steering instruction priority wherever they conflict.\n\n"
)


QUEUE_RETRY_TASKS: dict[str, asyncio.Task[Any]] = {}


DARWIN_MEMORY_CACHE: dict[str, Any] = {"at": float("-inf"), "mb": None}
DARWIN_MEMORY_CACHE_SECONDS = 5.0


CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES = {
    "turn_started",
    "turn_queued",
    "turn_unqueued",
    "turn_queue_updated",
    "turn_queue_reordered",
    "turn_queue_run_now",
    "turn_queue_paused",
    "turn_queue_delivery_fenced",
}


COMPACT_TIMELINE_HIDDEN_TYPES = {
    "raw_event",
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "process_started",
    "provider_session",
    "cwd_fallback",
    "history_imported",
    "backend_changed",
    "session_created",
    "code_diff",
}


TIMELINE_INDEX_HIDDEN_TYPES = {
    "turn_queued",
    "turn_unqueued",
    "turn_queue_updated",
    "turn_queue_reordered",
    "turn_queue_run_now",
    "turn_queue_paused",
    "turn_queue_delivery_fenced",
    "queue_snapshot",
    "subagent_state",
    "job_updated",
    "job_deleted",
    "raw_event",
    "codex_thread_status",
    "codex_goal_updated",
    "codex_goal_cleared",
    "claude_subagents_stopped",
    "claude_goal_changed",
    "claude_background_tasks_reconciled",
    "claude_background_task_reconciliation_consumed",
    # Legacy beta servers briefly emitted usage as timeline events. Keep those
    # old rows out of pagination even though current servers never emit them.
    "codex_token_usage",
}


TIMELINE_INDEX_TRACE_TYPES = {
    "reasoning_summary", "reasoning_text", "tool_started", "tool_finished", "process_started", "provider_session",
    "cwd_fallback", "history_imported", "backend_changed", "artifact_error", "session_created",
    "idle_warning",
}


RUN_TRACE_EVENT_TYPES = {
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "code_diff",
}


UNSAFE_HTTP_MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
UNSAFE_HTTP_MUTATION_ADMISSION_LOCK = asyncio.Lock()
# Exact leases replace the old scalar counter. A request task can be cancelled
# again while unwinding its middleware ``finally`` block, so any cleanup that
# awaits this admission lock can be cancelled before decrementing and strand a
# permanent lifecycle blocker. Lease cleanup is synchronous and exact;
# completed tasks are also pruned defensively by every authoritative reader.
UNSAFE_HTTP_MUTATION_TASKS: dict[str, asyncio.Task[Any] | None] = {}


RouteAuthKind = Literal["user", "agent_helper", "provider_mcp", "native_admin", "restart"]


@dataclass(frozen=True)
class RouteAuth:
    """How a route is admitted, declared where the route is defined.

    The auth middleware enforces it from request headers before FastAPI reads
    the body, so an unauthenticated caller can never make the server buffer or
    parse one. Routes without a declaration are ``user`` routes (the client
    bearer). As a dependency it does nothing; handlers that need more repeat
    their own checks.

    - ``agent_helper``: an in-chat helper with its run's provider capability,
      from a local provider process; the bearer is not a substitute.
    - ``provider_mcp``: Codex's local MCP tool channel, with its process secret.
    - ``native_admin``: native operator settings; header token only.
    - ``restart``: server restart; header token only, outside mutation admission.

    ``body_limit`` bounds unsafe-method bodies (``None``: no route limit).
    """

    kind: RouteAuthKind = "user"
    body_limit: int | None = None

    def __call__(self) -> None:
        return None


USER_ROUTE = RouteAuth()


ROUTE_AUTH_INDEX: "weakref.WeakKeyDictionary[Any, tuple[tuple[Any, RouteAuth], ...]]" = weakref.WeakKeyDictionary()


HEALTH_QUEUE_RECONCILE_STATE: dict[str, Any] = {
    "last_at": float("-inf"),
    "task": None,
}
SESSION_SNAPSHOT_RECONCILE_AT: dict[str, float] = {}


TURN_RECEIPT_TASKS: dict[tuple[str, str], tuple[str, asyncio.Task]] = {}


EVENT_STREAM_HEARTBEAT_SECONDS = 15.0

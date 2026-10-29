"""The chat store: session records, persistence, lifecycle locks and public projections."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import logging.handlers
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import threading
import time
from typing import Any, Iterator, Literal
import unicodedata
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.providers.codex import auth as codex_auth
from hyadex.core.config import (
    ABANDONED_FORK_THREADS_FILE,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_DEFAULT_PERMISSION_MODE,
    CLAUDE_PERMISSION_MODES,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    CODEX_APPROVAL_POLICIES,
    CODEX_APPROVAL_REVIEWERS,
    CODEX_BIN,
    CODEX_DEFAULT_APPROVAL_POLICY,
    CODEX_DEFAULT_APPROVALS_REVIEWER,
    CODEX_DEFAULT_EFFORT,
    CODEX_DEFAULT_MODEL,
    CODEX_DEFAULT_PERMISSION_PROFILE,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_EFFORT_ALIASES,
    CODEX_EFFORT_ORDER,
    CODEX_EFFORTS,
    CODEX_FALLBACK_MODEL_EFFORTS,
    CODEX_FALLBACK_SERVICE_TIERS,
    CODEX_SANDBOX_MODES,
    CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
    CODEX_SESSION_INDEX_PATH,
    CODEX_TRANSPORT,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    EMERGENCY_MESSAGE_MAX_CHARS,
    FILES_ROOT,
    MAX_CODEX_SESSION_INDEX_BYTES,
    MAX_CODEX_SESSION_INDEX_LINES,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    MAX_WORKSPACE_PATH_CHARS,
    PROVIDER_AUTHORITY_ROOT,
    PROVIDER_JOBS_ACCESS_DEFAULT,
    PROVIDER_JOBS_ACCESS_MODE_SET,
    RUNTIME_DIAGNOSTIC_TTL_SECONDS,
    SERVER_INSTANCE_ID,
    SERVER_RESTART_STATUS_FILE,
    SERVER_ROOT,
    SESSION_ORDER_STEP,
    SESSIONS_FILE,
    VALID_BACKENDS,
)
from hyadex.core.event_seq import forget_event_seq, record_event_index_entry, repair_event_log_tail
from hyadex.core.paths import code_diffs_dir, events_path, existing_cwd, now_iso, session_dir
from hyadex.core.restart_status import (
    MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
    managed_server_restart_blocks_work,
)
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_alerts import (
    active_emergency_alerts,
    apply_emergency_lifecycle_event,
    emergency_lifecycle_events_after,
    emergency_summary,
    reconcile_session_agent_notifications,
    reconcile_session_emergency_alerts,
)
from hyadex.core.session_codex import (
    ABANDONED_FORK_PROVIDER_THREADS,
    bounded_codex_output_text,
    codex_app_server_env,
    codex_compaction_count,
    CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT,
    codex_default_service_tier,
    codex_session_index_thread_names,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_LIVE_GENERATIONS,
    CODEX_SUBAGENT_LIVE_MANAGERS,
    CODEX_SUBAGENT_SESSION_INDEX,
    CODEX_SUBAGENT_STATE,
    CODEX_THREAD_SESSION_INDEX,
    ensure_codex_thread_not_pending_fork_cleanup,
    existing_codex_app_server_manager,
    native_codex_compaction_terminal_aliases,
    session_codex_thread_id,
    TIMELINE_INDEX_CODEX_COMPACTION_TYPES,
    timeline_index_codex_lifecycle_key,
)
from hyadex.core.session_files import delete_session_owned_file_records
from hyadex.core.session_settings import (
    codex_goal_time_budget_is_exhausted,
    codex_runtime_settings,
    CreateSessionRequest,
    effective_claude_permission_mode,
    effective_provider_jobs_access,
    normalize_runtime_effort,
    preview_session_runtime_update,
    RUNTIME_DIAGNOSTICS,
    RUNTIME_DIAGNOSTICS_LOCK,
    session_backend_locked,
)
from hyadex.core.session_titles import (
    cancel_generated_session_title,
    read_native_session_title,
    title_runtime_key,
)
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_DELIVERY_LOCKS,
    EVENT_INDEX_STRIDE,
    EVENT_INDEX_VERSION,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
    EVENT_SEQ_REPAIR_LOCKS,
    EVENTS,
    NOTIFICATION_EVENT_FIELDS,
    QUEUE_CHECKPOINT_DIRTY,
    SESSION_LIFECYCLE_LOCKS,
    SESSION_STORE_SAVE_DEBOUNCE_SECONDS,
    SESSION_STORE_SAVE_MAX_LATENCY_SECONDS,
    SESSIONS_WRITE_LOCK,
    TIMELINE_INDEX_CACHE,
    TIMELINE_INDEX_CACHE_LOCK,
    TIMELINE_INDEX_LOCK_STRIPES,
)
from hyadex.core.util import (
    compact_memory_text,
    ensure_dirs,
    normalized_file_id,
    normalized_network_address,
    runner_env,
    utc_timestamp,
)
from hyadex.features.titles import (
    generated_title_eligible,
    GENERATED_TITLE_SLOTS,
    GENERATED_TITLE_TASKS,
    session_accepts_auto_title,
)
from hyadex.features.usage import (
    broadcast_provider_runtime_changed,
    clear_provider_context_usage_locked,
    join_task_despite_caller_cancellation,
)
from hyadex.providers.claude.history import (
    claude_history_candidates,
    claude_transcript_title,
    HISTORY_SEARCH_DIRTY,
    native_session_title,
    provider_session_identifier,
    session_provider_id,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.config import (
    _codex_config_positive_int,
    codex_effective_thread_config,
    sanitize_codex_thread_config,
)
from hyadex.providers.instructions import (
    CLAUDE_PROMPT_PRELUDE,
    CODEX_THREAD_POLICY_VERSION,
    MAX_SESSION_SYSTEM_PROMPT_CHARS,
    PROVIDER_TOOL_REPLAY,
    PROVIDER_TOOL_REPLAY_LOCK,
    PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT,
    PROVIDER_TOOL_REPLAY_TOMBSTONES,
)
from hyadex.providers.runtime import scrub_provider_runtime_environment
from hyadex.providers.state import PROVIDERS
from hyadex.core.file_store import atomic_json
from hyadex.core.transcripts import jsonl_records
from hyadex.features import title_generation

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 agent hosts
    tomllib = None

logger = logging.getLogger("hyadex-server")

async def next_event_seq(session_id: str, path: Path) -> int:
    # Keep the global lock to a memory-only critical section. A cold chat may
    # need an unbounded reverse scan/tail repair; holding this lock across that
    # worker used to stop healthy chats from allocating any event sequence.
    async with EVENT_SEQ_LOCK:
        seq = EVENT_SEQ_CACHE.get(session_id)
        if seq is not None:
            seq += 1
            EVENT_SEQ_CACHE[session_id] = seq
            return seq

    # append_event already owns the per-chat delivery lock, so next_event_seq
    # cannot acquire it recursively. This narrower lock also makes direct cold
    # callers coalesce on exactly one repair for the same session.
    repair_lock = EVENT_SEQ_REPAIR_LOCKS.setdefault(
        session_id,
        asyncio.Lock(),
    )
    async with repair_lock:
        async with EVENT_SEQ_LOCK:
            seq = EVENT_SEQ_CACHE.get(session_id)
            if seq is not None:
                seq += 1
                EVENT_SEQ_CACHE[session_id] = seq
                return seq

        repair_task = asyncio.create_task(asyncio.to_thread(
            repair_event_log_tail,
            path,
        ))
        try:
            file_seq = await asyncio.shield(repair_task)
        except BaseException:
            # A worker thread cannot be cancelled once it starts. Keep this
            # chat's repair ownership until the filesystem operation settles,
            # without blocking sequence allocation for unrelated sessions.
            try:
                await join_task_despite_caller_cancellation(repair_task)
            except BaseException:
                pass
            raise
        session = STORE.sessions.get(session_id) if "STORE" in globals() else None
        stored_seq = 0
        if isinstance(session, dict):
            value = session.get("latest_event_seq")
            if isinstance(value, int) and not isinstance(value, bool):
                stored_seq = max(0, value)
        async with EVENT_SEQ_LOCK:
            # Recheck under the global memory lock in case a recovery path set
            # a more conservative high-water mark while the worker was active.
            seq = max(
                int(EVENT_SEQ_CACHE.get(session_id) or 0),
                file_seq,
                stored_seq,
            ) + 1
            EVENT_SEQ_CACHE[session_id] = seq
            return seq


async def reconcile_event_seq_after_failed_write(
    session_id: str,
    path: Path,
    *,
    consumed_high_water: int = 0,
) -> None:
    """Repair a failed append and make its next sequence conservative."""

    async with EVENT_SEQ_LOCK:
        EVENT_SEQ_CACHE.pop(session_id, None)
    repair_task = asyncio.create_task(asyncio.to_thread(
        repair_event_log_tail,
        path,
    ))
    try:
        repaired_seq = await asyncio.shield(repair_task)
    except asyncio.CancelledError as cancellation:
        # Retain ownership until the non-cancellable worker settles; releasing
        # event_delivery_lock while it is still truncating would let the next
        # append race the repair. Preserve the caller's cancellation afterward.
        try:
            repaired_seq = await join_task_despite_caller_cancellation(repair_task)
        except BaseException:
            async with EVENT_SEQ_LOCK:
                EVENT_SEQ_CACHE.pop(session_id, None)
            raise cancellation
        session = STORE.sessions.get(session_id) if "STORE" in globals() else None
        stored_seq = 0
        if isinstance(session, dict):
            value = session.get("latest_event_seq")
            if isinstance(value, int) and not isinstance(value, bool):
                stored_seq = max(0, value)
        async with EVENT_SEQ_LOCK:
            EVENT_SEQ_CACHE[session_id] = max(
                consumed_high_water,
                repaired_seq,
                stored_seq,
            )
        raise
    except BaseException:
        # Keep the cache cold. A later append must retry the repair and fail
        # closed rather than writing after a fragment we could not truncate.
        async with EVENT_SEQ_LOCK:
            EVENT_SEQ_CACHE.pop(session_id, None)
        return
    session = STORE.sessions.get(session_id) if "STORE" in globals() else None
    stored_seq = 0
    if isinstance(session, dict):
        value = session.get("latest_event_seq")
        if isinstance(value, int) and not isinstance(value, bool):
            stored_seq = max(0, value)
    async with EVENT_SEQ_LOCK:
        EVENT_SEQ_CACHE[session_id] = max(
            consumed_high_water,
            repaired_seq,
            stored_seq,
        )


def event_delivery_lock(session_id: str) -> asyncio.Lock:
    """Serialize persisted and live event delivery within one chat."""
    return EVENT_DELIVERY_LOCKS.setdefault(session_id, asyncio.Lock())


def clean_session_system_prompt(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_SESSION_SYSTEM_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"system prompt must be at most {MAX_SESSION_SYSTEM_PROMPT_CHARS} characters",
        )
    return clean


def session_folder(sess: dict[str, Any]) -> str:
    folder = str(sess.get("folder") or "General").strip()
    return folder or "General"


def session_order_value(sess: dict[str, Any]) -> float:
    value = sess.get("sort_order")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def session_section_key(sess: dict[str, Any]) -> tuple[str, str]:
    if bool(sess.get("archived")):
        return ("archived", "")
    if bool(sess.get("pinned")):
        return ("pinned", "")
    return ("folder", session_folder(sess))


def legacy_session_sort_key(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(sessions)
    ordered.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    ordered.sort(key=lambda s: not bool(s.get("pinned")))
    ordered.sort(key=lambda s: bool(s.get("archived")))
    return ordered


def sorted_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        sessions,
        key=lambda s: (
            bool(s.get("archived")),
            not bool(s.get("pinned")) if not bool(s.get("archived")) else False,
            "" if bool(s.get("archived")) or bool(s.get("pinned")) else session_folder(s).casefold(),
            session_order_value(s),
            str(s.get("created_at") or ""),
            str(s.get("id") or ""),
        ),
    )


def read_abandoned_fork_thread_ids(
    path: Path | None = None,
) -> set[str]:
    """Read provider forks awaiting post-crash retirement."""

    ledger_path = path or ABANDONED_FORK_THREADS_FILE
    try:
        value = json.loads(ledger_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "could not read abandoned fork cleanup ledger %s: %s",
            ledger_path,
            concise_error_message(exc),
        )
        return set()
    raw_ids = value.get("thread_ids") if isinstance(value, dict) else None
    if not isinstance(raw_ids, list):
        logger.warning("ignoring malformed abandoned fork cleanup ledger %s", ledger_path)
        return set()
    return {
        clean
        for raw in raw_ids
        if (clean := str(raw or "").strip())
    }


def write_abandoned_fork_thread_ids(
    thread_ids: set[str],
    path: Path | None = None,
) -> None:
    """Atomically persist provider forks that must be retired."""

    atomic_json(
        path or ABANDONED_FORK_THREADS_FILE,
        {"thread_ids": sorted(thread_ids)},
    )


def encode_sessions_json(sessions: dict[str, dict[str, Any]]) -> str:
    """Serialize the sessions map with the C encoder.

    ``indent`` forces CPython onto the pure-Python encoder, which is an order
    of magnitude slower on a multi-megabyte map and runs on the event loop.
    Compact output parses to the identical object; indentation was never part
    of the on-disk contract.
    """

    return json.dumps(sessions, separators=(",", ":"))


def write_sessions_json_text(
    path: Path,
    text: str,
    *,
    durable: bool,
    lock_timeout: float | None = None,
) -> None:
    """Atomically replace sessions.json; ``durable`` also fsyncs file and dir.

    ``lock_timeout`` bounds the wait for the writer lock. The shutdown fallback
    runs on the event loop and must not block behind a worker thread that is
    stalled inside a slow write.
    """

    if lock_timeout is None:
        SESSIONS_WRITE_LOCK.acquire()
    elif not SESSIONS_WRITE_LOCK.acquire(timeout=max(0.0, float(lock_timeout))):
        raise RuntimeError("sessions.json writer is busy")
    try:
        _write_sessions_json_text_locked(path, text, durable=durable)
    finally:
        SESSIONS_WRITE_LOCK.release()


def _write_sessions_json_text_locked(path: Path, text: str, *, durable: bool) -> None:
    """Write sessions.json; the caller holds SESSIONS_WRITE_LOCK."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        stream.write(text)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(tmp, path)
    if not durable:
        return
    # Windows does not support opening/fsyncing a directory handle this
    # way; FlushFileBuffers on the temporary file plus atomic replace is
    # its portable boundary. POSIX additionally persists the rename itself.
    if os.name == "nt":
        return
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path.parent, directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class PendingSessionSave:
    """One coalesced sessions.json write covering every mark since the last."""

    __slots__ = (
        "done",
        "durable",
        "error",
        "first_marked_at",
        "last_marked_at",
        "path",
        "wake",
    )

    def __init__(self, path: Path) -> None:
        now = time.monotonic()
        self.done = asyncio.Event()
        # Set when a caller awaits this write; the worker then skips the
        # debounce window so awaited saves keep their immediate semantics.
        self.wake = asyncio.Event()
        self.durable = False
        self.error: BaseException | None = None
        self.first_marked_at = now
        self.last_marked_at = now
        self.path = path


class SessionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._pending_save: PendingSessionSave | None = None
        self._save_task: asyncio.Task[None] | None = None
        self.save_write_count = 0

    def transaction(self) -> asyncio.Lock:
        """Serialize a read-modify-write of ``sessions`` with every other
        registry mutation: ``async with STORE.transaction(): ...``.

        Persist changes inside it with ``save()``. Callers outside the store
        use this, never the lock itself, so the store owns how it locks.
        """
        return self._lock

    async def load(self) -> None:
        ensure_dirs()
        runtime_changed = False
        DELETING_SESSIONS.clear()
        DELETED_SESSION_TOMBSTONES.clear()
        CODEX_THREAD_SESSION_INDEX.clear()
        with CODEX_SUBAGENT_INDEX_LOCK:
            CODEX_SUBAGENT_SESSION_INDEX.clear()
            CODEX_SUBAGENT_STATE.clear()
        if SESSIONS_FILE.exists():
            try:
                loaded_sessions = json.loads(SESSIONS_FILE.read_text())
                if not isinstance(loaded_sessions, dict):
                    raise ValueError("sessions registry must be a JSON object")
                self.sessions = loaded_sessions
            except Exception as e:
                # Starting with an empty registry would make the next ordinary
                # mutation overwrite the only copy of every chat. Preserve the
                # original bytes and fail startup closed instead.
                raise RuntimeError(
                    f"refusing to replace unreadable sessions registry: {e}"
                ) from e
        abandoned_imports = {
            str(session_id): session
            for session_id, session in self.sessions.items()
            if isinstance(session, dict)
            and session.get("_history_import_initializing")
        }
        for session_id in abandoned_imports:
            # History import stages only local Hyadex state. Never add its
            # provider id to the abandoned-fork cleanup ledger: the source
            # transcript belongs to Claude/Codex and must remain untouched.
            self.sessions.pop(session_id, None)
            with suppress(Exception):
                await asyncio.to_thread(delete_session_owned_file_records, session_id)
            with suppress(OSError):
                await asyncio.to_thread(delete_session_local_roots, session_id)
            with suppress(Exception):
                await forget_event_seq(session_id)
            HISTORY_SEARCH_DIRTY.add(session_id)
        if abandoned_imports:
            runtime_changed = True
            logger.warning(
                "removed abandoned staged history imports after restart count=%s",
                len(abandoned_imports),
            )
        abandoned_forks = {
            str(session_id): session
            for session_id, session in self.sessions.items()
            if isinstance(session, dict)
            and session.get("_fork_initializing")
            and not session.get("_history_import_initializing")
        }
        pending_provider_cleanup = read_abandoned_fork_thread_ids()
        durable_provider_cleanup = set(pending_provider_cleanup)
        staged_provider_ids = {
            clean_provider_id
            for session in abandoned_forks.values()
            if str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
            == BACKEND_CODEX
            and (
                clean_provider_id := str(
                    session.get("codex_thread_id")
                    or session.get("session_id")
                    or ""
                ).strip()
            )
        }
        if staged_provider_ids - pending_provider_cleanup:
            proposed_provider_cleanup = (
                pending_provider_cleanup | staged_provider_ids
            )
            try:
                write_abandoned_fork_thread_ids(proposed_provider_cleanup)
                pending_provider_cleanup = proposed_provider_cleanup
                durable_provider_cleanup = set(proposed_provider_cleanup)
            except OSError as exc:
                # Never erase the only durable reference to an unexposed
                # persistent provider fork. The hidden staged session remains
                # available for the next startup to retry ledger persistence.
                logger.warning(
                    "could not persist abandoned provider fork cleanup ledger: %s",
                    concise_error_message(exc),
                )
        ABANDONED_FORK_PROVIDER_THREADS.clear()
        ABANDONED_FORK_PROVIDER_THREADS.update(pending_provider_cleanup)
        for session_id in abandoned_forks:
            provider_id = str(
                abandoned_forks[session_id].get("codex_thread_id")
                or abandoned_forks[session_id].get("session_id")
                or ""
            ).strip()
            if provider_id and provider_id not in durable_provider_cleanup:
                continue
            self.sessions.pop(session_id, None)
            with suppress(Exception):
                await asyncio.to_thread(delete_session_owned_file_records, session_id)
            with suppress(OSError):
                await asyncio.to_thread(delete_session_local_roots, session_id)
            with suppress(Exception):
                await forget_event_seq(session_id)
            HISTORY_SEARCH_DIRTY.add(session_id)
        removed_abandoned_forks = len(abandoned_forks) - sum(
            1
            for session_id in abandoned_forks
            if session_id in self.sessions
        )
        if removed_abandoned_forks:
            runtime_changed = True
            logger.warning(
                "removed abandoned staged session forks after restart count=%s",
                removed_abandoned_forks,
            )
        for session_id, sess in self.sessions.items():
            if reconcile_session_emergency_alerts(session_id, sess):
                runtime_changed = True
            if reconcile_session_agent_notifications(session_id, sess):
                runtime_changed = True
            backend = str(sess.get("backend") or DEFAULT_BACKEND).strip().lower()
            claude_permission_mode = effective_claude_permission_mode(sess)
            if sess.get("claude_permission_mode") != claude_permission_mode:
                sess["claude_permission_mode"] = claude_permission_mode
                runtime_changed = True
            provider_jobs_access = effective_provider_jobs_access(sess)
            if sess.get("provider_jobs_access") != provider_jobs_access:
                sess["provider_jobs_access"] = provider_jobs_access
                runtime_changed = True
            for key, default in (
                ("codex_approval_policy", CODEX_DEFAULT_APPROVAL_POLICY),
                ("codex_sandbox_mode", CODEX_DEFAULT_SANDBOX_MODE),
                ("codex_approvals_reviewer", CODEX_DEFAULT_APPROVALS_REVIEWER),
            ):
                if sess.get(key) is None:
                    sess[key] = default
                    runtime_changed = True
            if "codex_permission_profile" not in sess:
                sess["codex_permission_profile"] = (
                    CODEX_DEFAULT_PERMISSION_PROFILE
                )
                runtime_changed = True
            previous_effort = sess.get("effort")
            # Normalize aliases and truly invalid values, but do not erase a
            # saved preference from a static model-capability table. Codex's
            # discovered model catalog can evolve ahead of HyadexServer; any
            # known-incompatible pair is clamped only when launching a turn.
            normalized_effort = normalize_runtime_effort(
                backend,
                previous_effort,
            )
            if normalized_effort != previous_effort:
                sess["effort"] = normalized_effort
                runtime_changed = True
            if backend == BACKEND_CLAUDE:
                # SDK supervisors and their approval waiters are process-owned.
                # Never revive a stale action badge after HyadexServer restarts.
                for key, value in (
                    ("claude_pending_interaction_count", 0),
                    ("claude_needs_user_action", False),
                ):
                    if sess.get(key) != value:
                        sess[key] = value
                        runtime_changed = True
            if backend == BACKEND_CODEX:
                provider_id = str(
                    sess.get("codex_thread_id") or sess.get("session_id") or ""
                ).strip()
                if provider_id:
                    CODEX_THREAD_SESSION_INDEX[provider_id] = str(
                        sess.get("id") or ""
                    )
                # App-server status and approval cards describe one live
                # process. Bounded token checkpoints are durable so clients
                # can still explain prior compaction after a restart.
                reset_values = {
                    "codex_thread_status": {"type": "notLoaded"},
                    "codex_pending_interaction_count": 0,
                    "codex_needs_user_action": False,
                }
                for key, value in reset_values.items():
                    if sess.get(key) != value:
                        sess[key] = value
                        runtime_changed = True
                cached_goal = sess.get("codex_goal")
                if (
                    isinstance(cached_goal, dict)
                    and str(cached_goal.get("status") or "") == "active"
                ):
                    # A server restart destroys the local run owner. Preserve
                    # the objective, but never advertise it as actively
                    # running until the provider thread has been reconciled.
                    sess["codex_goal"] = {
                        **cached_goal,
                        "status": "paused",
                    }
                    sess["_codex_goal_reconciliation_required"] = {
                        "thread_id": provider_id or None,
                        "recorded_at": now_iso(),
                        "reason": "HyadexServer restarted without a live run owner.",
                    }
                    runtime_changed = True
                if "_codex_token_usage_terminal" in sess:
                    # This marker coordinates independently dispatched live
                    # notifications and has no meaning after process restart.
                    sess.pop("_codex_token_usage_terminal", None)
                    runtime_changed = True
        await self.ensure_sort_orders()
        rebuild_codex_subagent_indexes()
        if runtime_changed:
            await self.save()

    def mark_dirty(self, *, durable: bool = False, wake: bool = False) -> PendingSessionSave:
        """Record that sessions.json must be rewritten and ensure a writer runs.

        Every mark between two writes shares one ``PendingSessionSave``; the
        single writer task encodes a snapshot on the loop (C encoder, so the
        map cannot change under it) and performs the file replacement in a
        worker thread. ``durable`` upgrades the covering write to fsync.
        """

        pending = self._pending_save
        if pending is None:
            pending = self._pending_save = PendingSessionSave(SESSIONS_FILE)
        else:
            pending.path = SESSIONS_FILE
            pending.last_marked_at = time.monotonic()
        pending.durable = pending.durable or durable
        if wake or durable:
            pending.wake.set()
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.get_running_loop().create_task(
                self._save_worker(),
                name="sessions-json-writer",
            )
        return pending

    async def save(self, *, durable: bool = False, flush: bool = True) -> None:
        """Persist ``self.sessions``.

        ``flush=True`` (default) returns once the covering write is on disk and
        re-raises its error, preserving the historical contract for callers
        and tests. ``flush=False`` only marks the store dirty: the write is
        coalesced with neighbours and lands within the debounce window. Hot
        paths that persist per-event metadata use that form so the event
        loop never waits on sessions.json.
        """

        ensure_dirs()
        pending = self.mark_dirty(durable=durable, wake=flush or durable)
        if not (flush or durable):
            return
        wait_task = asyncio.create_task(pending.done.wait())
        cancellation: asyncio.CancelledError | None = None
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError as exc:
            # The sessions writer owns a to_thread filesystem replacement that
            # cannot be cancelled.  Do not let a mutation lock escape while
            # that old snapshot can still land after a rollback/new mutation.
            cancellation = exc
            await join_task_despite_caller_cancellation(wait_task)
        if pending.error is not None:
            raise pending.error
        if cancellation is not None:
            # Awaited saves have a precise cancellation contract: once the
            # caller observes cancellation, its covering snapshot is already
            # committed.  Transactional callers can therefore durably roll it
            # back (or intentionally retain the committed in-memory state).
            raise cancellation

    async def persist_restored_state(
        self,
        *,
        durable: bool = False,
    ) -> None:
        """Persist a rollback even if its request task is being cancelled."""

        rollback_task = asyncio.create_task(self.save(durable=durable))
        await join_task_despite_caller_cancellation(rollback_task)

    async def flush_pending_save(self) -> None:
        """Join the writer so no coalesced sessions.json write is left behind."""

        pending = self._pending_save
        if pending is not None:
            pending.wake.set()
        task = self._save_task
        if task is not None and not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A shutdown timeout/caller cancellation must not propagate
                # into an in-flight worker and orphan its filesystem thread.
                await join_task_despite_caller_cancellation(task)
                raise
            except Exception:
                pass

    async def _wait_for_save_window(self, pending: PendingSessionSave) -> None:
        if pending.wake.is_set() or SESSION_STORE_SAVE_DEBOUNCE_SECONDS <= 0:
            return
        while not pending.wake.is_set():
            now = time.monotonic()
            deadline = min(
                pending.last_marked_at + SESSION_STORE_SAVE_DEBOUNCE_SECONDS,
                pending.first_marked_at + SESSION_STORE_SAVE_MAX_LATENCY_SECONDS,
            )
            remaining = deadline - now
            if remaining <= 0:
                return
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(pending.wake.wait(), remaining)

    async def _save_worker(self) -> None:
        pending: PendingSessionSave | None = None
        try:
            while self._pending_save is not None:
                pending = self._pending_save
                await self._wait_for_save_window(pending)
                # Marks arriving from here on belong to the next write.
                self._pending_save = None
                try:
                    # Encoding can fail (a non-JSON value left in a session);
                    # that failure must reach the awaiters like a write failure
                    # instead of killing the writer and stranding every later
                    # save() behind an event that is never set.
                    text = encode_sessions_json(self.sessions)
                    await asyncio.to_thread(
                        write_sessions_json_text,
                        pending.path,
                        text,
                        durable=pending.durable,
                    )
                    self.save_write_count += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    pending.error = exc
                    logger.error(
                        "failed to persist sessions.json: %s",
                        concise_error_message(exc),
                    )
                finally:
                    pending.done.set()
                pending = None
        except asyncio.CancelledError:
            # Loop teardown without a lifespan join (tests, hard shutdown):
            # write synchronously so already-applied mutations are not lost.
            leftover = self._pending_save
            self._pending_save = None
            if (
                pending is not None
                and pending is not leftover
                and not pending.done.is_set()
            ):
                # Cancelled mid-write: the thread may or may not have landed
                # it, so its waiters must not assume success.
                pending.error = RuntimeError("sessions.json writer cancelled")
                pending.done.set()
            if leftover is not None:
                try:
                    # Bounded: a worker thread stalled inside a slow write still
                    # holds the lock, and this fallback runs on the event loop.
                    write_sessions_json_text(
                        leftover.path,
                        encode_sessions_json(self.sessions),
                        durable=leftover.durable,
                        lock_timeout=1.0,
                    )
                    self.save_write_count += 1
                except Exception as exc:
                    leftover.error = exc
                    if not isinstance(exc, FileNotFoundError):
                        logger.warning(
                            "sessions.json shutdown write failed: %s",
                            concise_error_message(exc),
                        )
                finally:
                    leftover.done.set()
            raise
        finally:
            if self._save_task is asyncio.current_task():
                self._save_task = None

    async def ensure_sort_orders(self) -> None:
        changed = False
        for index, sess in enumerate(legacy_session_sort_key(list(self.sessions.values()))):
            if sess.get("sort_order") is None:
                sess["sort_order"] = (index + 1) * SESSION_ORDER_STEP
                changed = True
        if changed:
            await self.save()

    def top_order_for_section(self, section: tuple[str, str], *, excluding_id: str | None = None) -> float:
        section_orders = [
            session_order_value(sess)
            for sess in self.sessions.values()
            if session_section_key(sess) == section
            and sess.get("id") != excluding_id
            and sess.get("sort_order") is not None
        ]
        if not section_orders:
            return SESSION_ORDER_STEP
        return min(section_orders) - SESSION_ORDER_STEP

    async def create(
        self,
        req: CreateSessionRequest,
        *,
        parent_id: str | None = None,
        initializing_fork: bool = False,
        initializing_import: bool = False,
    ) -> dict[str, Any]:
        backend = (req.backend or DEFAULT_BACKEND).lower()
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
        validate_session_subagent_limit({"backend": backend}, req.subagent_limit)
        runtime_source = {"backend": backend}
        runtime = preview_session_runtime_update(runtime_source, {"model": req.model, "effort": req.effort})
        model, effort = runtime["model"], runtime["effort"]
        session_cwd = req.cwd or DEFAULT_CWD
        provider_id = req.provider_session_id or req.session_id
        claude_session_id = req.claude_session_id or (provider_id if backend == BACKEND_CLAUDE else None)
        codex_thread_id = req.codex_thread_id or (provider_id if backend == BACKEND_CODEX else None)
        active_provider_id = (
            claude_session_id if backend == BACKEND_CLAUDE
            else codex_thread_id
        )
        # A Claude chat can park an inactive Codex identity for later backend
        # switching, so validate every supplied Codex thread, not only the
        # currently active provider identity.
        ensure_codex_thread_not_pending_fork_cleanup(codex_thread_id)
        resume_placeholder = f"Resumed {backend.title()} {str(active_provider_id)[:8]}"
        # Older clients explicitly send this generated resume label. Recognize
        # it only at creation; PATCH renames (even identical wording) stay manual.
        # A staged import already chose a native title or preview. Preserve it
        # even if its literal text happens to match an Hyadex placeholder.
        automatic_title = not req.title or (not initializing_import and (
            req.title == "New chat" or (
                bool(active_provider_id) and req.title == resume_placeholder
            )
        ))
        provider_title = None
        if active_provider_id and automatic_title and not (
            parent_id or initializing_fork or initializing_import
        ):
            try:
                provider_title = await asyncio.wait_for(asyncio.to_thread(
                    read_native_session_title,
                    {"backend": backend, "session_id": active_provider_id,
                     "cwd": req.cwd or DEFAULT_CWD},
                ), timeout=1.0)
            except Exception as exc:
                # A missing/busy/changed native store must not prevent resume.
                logger.debug("resume title lookup skipped error=%s", type(exc).__name__)
        sid = f"sess_{uuid.uuid4().hex[:16]}"
        ensure_dirs(sid)
        now = now_iso()
        title = provider_title or req.title or (
            resume_placeholder if active_provider_id else "New chat"
        )
        archived = bool(req.archived)
        pinned = bool(req.pinned) and not archived
        sess = {
            "id": sid,
            "title": title,
            "auto_title_enabled": req.auto_title_enabled is not False,
            # Clients send "New chat" explicitly for an unnamed creation.
            # Older persisted titles without provenance are left alone.
            "_title_source": (
                "provider" if provider_title else "placeholder" if automatic_title else "manual"
            ),
            "_title_auto_value": (
                title if automatic_title else None
            ),
            "folder": req.folder or "General",
            "cwd": session_cwd,
            "backend": backend,
            "model": model,
            "effort": effort,
            "system_prompt": clean_session_system_prompt(req.system_prompt),
            "session_id": active_provider_id,
            "claude_session_id": claude_session_id,
            "codex_thread_id": codex_thread_id,
            # Backend identity becomes immutable at turn admission, before
            # the provider necessarily returns its native session ID.
            "backend_locked": bool(active_provider_id),
            "claude_permission_mode": (
                req.claude_permission_mode or CLAUDE_DEFAULT_PERMISSION_MODE
            ),
            "codex_approval_policy": (
                req.codex_approval_policy or CODEX_DEFAULT_APPROVAL_POLICY
            ),
            "codex_sandbox_mode": (
                req.codex_sandbox_mode or CODEX_DEFAULT_SANDBOX_MODE
            ),
            "codex_permission_profile": (
                str(
                    req.codex_permission_profile
                    or CODEX_DEFAULT_PERMISSION_PROFILE
                    or ""
                ).strip()
                or CODEX_DEFAULT_PERMISSION_PROFILE
            ),
            "codex_approvals_reviewer": (
                req.codex_approvals_reviewer
                or CODEX_DEFAULT_APPROVALS_REVIEWER
            ),
            "provider_jobs_access": (
                req.provider_jobs_access or PROVIDER_JOBS_ACCESS_DEFAULT
            ),
            "claude_pending_interaction_count": 0,
            "claude_needs_user_action": False,
            "parent_id": parent_id,
            "fork_from": None,
            "pinned": pinned,
            "pinned_at": now if pinned else None,
            "archived": archived,
            "archived_at": now if archived else None,
            "created_at": now,
            "updated_at": now,
        }
        if not active_provider_id and not parent_id and sess["title"] == "New chat":
            sess["_empty_chat_cleanup"] = {"version": 1, "dismissed_by": {}}
        if req.subagent_limit is not None:
            sess["subagent_limit"] = req.subagent_limit
        if initializing_fork and parent_id:
            parent = self.sessions.get(parent_id) or {}
            if isinstance(parent.get("codex_config_overrides"), dict):
                sess["codex_config_overrides"] = sanitize_codex_thread_config(
                    parent["codex_config_overrides"], source=f"session {parent_id}")
        if initializing_fork or initializing_import:
            # Persist the staging marker so a server crash can discard this
            # session instead of exposing a half-written timeline.
            sess["_fork_initializing"] = True
        if initializing_import:
            # Distinguish local history imports from provider-created forks so
            # startup cleanup never retires the source provider thread.
            sess["_history_import_initializing"] = True
        sess["sort_order"] = self.top_order_for_section(session_section_key(sess))
        try:
            async with self._lock:
                self.sessions[sid] = sess
                await self.save()
            await append_event(sid, "session_created", {"session": public_session(sess)})
        except BaseException:
            # The session may enter memory (and even reach disk) before save or
            # the first event fails. Roll back in a separate task so caller
            # cancellation cannot strand an invisible session or directory.
            async def rollback_create() -> None:
                async with self._lock:
                    if self.sessions.get(sid) is sess:
                        self.sessions.pop(sid, None)
                    try:
                        await self.save()
                    except BaseException as cleanup_exc:
                        logger.warning(
                            "failed to persist session-create rollback session=%s: %s",
                            sid,
                            concise_error_message(cleanup_exc),
                        )
                with suppress(BaseException):
                    await asyncio.to_thread(delete_session_owned_file_records, sid)
                with suppress(OSError):
                    await asyncio.to_thread(delete_session_local_roots, sid)
                with suppress(BaseException):
                    await forget_event_seq(sid)
                HISTORY_SEARCH_DIRTY.add(sid)

            cleanup_task = asyncio.create_task(rollback_create())
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
            raise
        return sess

    async def update(self, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            if "subagent_limit" in patch:
                validate_session_subagent_limit({**sess, "backend": patch.get("backend") or sess.get("backend")}, patch["subagent_limit"])
            previous_provider_runtime = dict(sess) if "subagent_limit" in patch else None
            previous_title_runtime = title_runtime_key(sess)
            missing_policy = object()
            previous_provider_jobs_access = sess.get(
                "provider_jobs_access",
                missing_policy,
            )
            runtime_preview = preview_session_runtime_update(sess, patch)
            current_backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
            prospective_backend = str(
                runtime_preview.get("backend") or DEFAULT_BACKEND
            ).lower()
            backend_changed = prospective_backend != current_backend
            prospective_effort = (
                patch.get("effort")
                if "effort" in patch
                else None if backend_changed else sess.get("effort")
            )
            effort_is_explicit = "effort" in patch
            normalized_prospective_effort = runtime_preview.get("effort")
            reset_implicit_incompatible_effort = (
                "model" in patch
                and not effort_is_explicit
                and normalized_prospective_effort != prospective_effort
            )
            old_section = session_section_key(sess)
            if "backend" in patch and patch["backend"] is not None:
                backend = str(patch["backend"]).lower()
                if backend not in VALID_BACKENDS:
                    raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
                old = sess.get("backend") or DEFAULT_BACKEND
                if old != backend:
                    if session_backend_locked(sess):
                        raise HTTPException(
                            status_code=409,
                            detail="backend is locked after the chat starts; fork or create a new chat to use another backend",
                        )
                    def provider_id_field(name: str) -> str:
                        if name == BACKEND_CLAUDE:
                            return "claude_session_id"
                        return "codex_thread_id"
                    if sess.get("session_id"):
                        sess[provider_id_field(old)] = sess["session_id"]
                    sess["session_id"] = sess.get(provider_id_field(backend))
                    sess["backend"] = backend
                    if "model" not in patch:
                        sess["model"] = None
                    if "effort" not in patch:
                        sess["effort"] = None
                    await append_event(sid, "backend_changed", {"old": old, "new": backend})
            for key in ("title", "folder", "cwd"):
                if key in patch and patch[key] is not None:
                    sess[key] = patch[key]
                    if key == "title":
                        # Even renaming to the existing text is a manual choice.
                        sess["_title_source"] = "manual"
                        sess.pop("_title_auto_value", None)
                        sess.pop("_title_seed", None)
            if patch.get("auto_title_enabled") is not None:
                sess["auto_title_enabled"] = bool(patch["auto_title_enabled"])
            if patch.get("consult_inbound") is not None:
                sess["consult_inbound"] = str(patch["consult_inbound"])
            if patch.get("consult_deep_approval") is not None:
                sess["consult_deep_approval"] = bool(patch["consult_deep_approval"])
            if patch.get("consult_deep_allowed") is not None:
                sess["consult_deep_allowed"] = [str(value) for value in patch["consult_deep_allowed"]][:50]
            for key, default, allowed in (
                (
                    "claude_permission_mode",
                    CLAUDE_DEFAULT_PERMISSION_MODE,
                    CLAUDE_PERMISSION_MODES,
                ),
                (
                    "codex_approval_policy",
                    CODEX_DEFAULT_APPROVAL_POLICY,
                    CODEX_APPROVAL_POLICIES,
                ),
                (
                    "codex_sandbox_mode",
                    CODEX_DEFAULT_SANDBOX_MODE,
                    CODEX_SANDBOX_MODES,
                ),
                (
                    "codex_approvals_reviewer",
                    CODEX_DEFAULT_APPROVALS_REVIEWER,
                    CODEX_APPROVAL_REVIEWERS,
                ),
                (
                    "provider_jobs_access",
                    PROVIDER_JOBS_ACCESS_DEFAULT,
                    PROVIDER_JOBS_ACCESS_MODE_SET,
                ),
            ):
                if key not in patch:
                    continue
                value = str(patch[key] or default).strip()
                if value not in allowed:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} must be one of {sorted(allowed)}",
                    )
                sess[key] = value
            if "codex_permission_profile" in patch:
                profile = str(patch["codex_permission_profile"] or "").strip()
                if len(profile) > 240:
                    raise HTTPException(
                        status_code=400,
                        detail="codex_permission_profile must be at most 240 characters",
                    )
                sess["codex_permission_profile"] = profile or None
            if "subagent_limit" in patch:
                sess.pop("_codex_subagent_limit_reset_pending", None)
                if patch["subagent_limit"] is None and sess.get("backend") == BACKEND_CODEX:
                    inherited = codex_effective_thread_config({**sess, "subagent_limit": None}).get("agents", {}).get("max_concurrent_threads_per_session")
                    applied = sess.get("_codex_subagent_limit_applied") or {}
                    if applied.get("process") and not _codex_config_positive_int(inherited):
                        sess["_codex_subagent_limit_reset_pending"] = applied["process"]
                sess["subagent_limit"] = patch["subagent_limit"]
            if "system_prompt" in patch:
                sess["system_prompt"] = clean_session_system_prompt(patch["system_prompt"])
            for key in ("model", "effort"):
                if key in patch:
                    value = patch[key]
                    if key == "effort":
                        sess[key] = normalized_prospective_effort
                    else:
                        sess[key] = runtime_preview["model"]
            if reset_implicit_incompatible_effort:
                # Older clients update the model and effort in separate
                # requests. Let that model change succeed while clearing a
                # now-invalid inherited effort; the response tells the client
                # which value was retained, and a following effort PATCH can
                # choose any supported value.
                sess["effort"] = normalized_prospective_effort
            if "pinned" in patch and patch["pinned"] is not None:
                pinned = bool(patch["pinned"])
                if pinned and not sess.get("pinned"):
                    sess["pinned_at"] = now_iso()
                elif not pinned:
                    sess["pinned_at"] = None
                sess["pinned"] = pinned
            if "archived" in patch and patch["archived"] is not None:
                archived = bool(patch["archived"])
                if archived and not sess.get("archived"):
                    sess["archived_at"] = now_iso()
                elif not archived:
                    sess["archived_at"] = None
                sess["archived"] = archived
                if archived:
                    sess["pinned"] = False
                    sess["pinned_at"] = None
            new_section = session_section_key(sess)
            if new_section != old_section:
                sess["sort_order"] = self.top_order_for_section(new_section, excluding_id=sid)
            # Ordinary/queued turns re-save their backend and may repeat model
            # or provider selections. Only an actual runtime change invalidates
            # the in-flight title; cancelling a no-op consumes its sole attempt.
            # An explicit title PATCH is still manual ownership, even if equal.
            if ("title" in patch or title_runtime_key(sess) != previous_title_runtime
                    or patch.get("auto_title_enabled") is False or patch.get("archived") is True):
                cancel_generated_session_title(sid)
            sess["updated_at"] = now_iso()
            try:
                await self.save()
            except BaseException:
                if previous_provider_runtime is not None:
                    sess.clear()
                    sess.update(previous_provider_runtime)
                    await self.persist_restored_state(durable=True)
                # This field is an agent authorization boundary. Never leave
                # a failed durable PATCH applied only in memory—especially a
                # failed expansion from blocked/read-only to full.
                if "provider_jobs_access" in patch:
                    if previous_provider_jobs_access is missing_policy:
                        sess.pop("provider_jobs_access", None)
                    else:
                        sess["provider_jobs_access"] = (
                            previous_provider_jobs_access
                        )
                    await self.persist_restored_state(durable=True)
                raise
            HISTORY_SEARCH_DIRTY.add(sid)
            return sess

    async def adopt_auto_title(
        self,
        sid: str,
        title: str,
        *,
        source: str,
        backend: str | None = None,
        provider_id: str | None = None,
        expected_title: str | None = None,
        expected_runtime: tuple | None = None,
        prompt_seed: str | None = None,
    ) -> bool:
        """Compare and persist under the same lock as a manual rename."""
        if source not in {"prompt", "provider", "generated"} or not title:
            return False
        if source == "prompt" and title == "New chat":
            # An attachment-only first turn still needs a useful fallback
            # when the user later sends text.
            return False
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess or not session_accepts_auto_title(sess):
                return False
            if sid in DELETING_SESSIONS or sid in DELETED_SESSION_TOMBSTONES:
                return False
            if source == "prompt" and sess.get("_title_source") != "placeholder":
                return False
            if source in {"provider", "generated"} and (
                not provider_id
                or (sess.get("backend") or DEFAULT_BACKEND) != backend
                or session_provider_id(sess) != provider_id
            ):
                return False
            if expected_title is not None and sess.get("title") != expected_title:
                return False
            if source == "generated" and (not generated_title_eligible(sess, allow_attempted=True)
                    or title_runtime_key(sess) != expected_runtime):
                return False
            if sess.get("title") == title and sess.get("_title_source") == source:
                return False
            missing = object()
            previous = {
                key: sess.get(key, missing)
                for key in ("title", "_title_source", "_title_auto_value", "_title_seed", "updated_at")
            }
            sess.update(title=title, _title_source=source, _title_auto_value=title)
            if source == "prompt" and isinstance(prompt_seed, str):
                sess["_title_seed"] = prompt_seed[:1600]
            elif source in {"provider", "generated"}:
                sess.pop("_title_seed", None)
            sess["updated_at"] = now_iso()
            try:
                await self.save()
            except asyncio.CancelledError:
                # Awaited SessionStore.save commits before surfacing caller
                # cancellation. Retain that exact committed state in memory.
                HISTORY_SEARCH_DIRTY.add(sid)
                raise
            except BaseException:
                for key, value in previous.items():
                    if value is missing:
                        sess.pop(key, None)
                    else:
                        sess[key] = value
                raise
            HISTORY_SEARCH_DIRTY.add(sid)
            return True

    async def reorder(
        self,
        sid: str,
        direction: str | None = None,
        target_id: str | None = None,
        placement: str | None = None,
        target_folder: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            section = session_section_key(sess)

            if target_id:
                normalized_placement = (placement or "before").strip().lower()
                if normalized_placement not in {"before", "after"}:
                    raise HTTPException(status_code=400, detail="placement must be before or after")
                target = self.sessions.get(target_id)
                if not target:
                    raise HTTPException(status_code=404, detail="target session not found")
                target_section = session_section_key(target)
                if target_folder is not None:
                    requested_folder = str(target_folder or "General").strip() or "General"
                    if target_section[0] != "folder":
                        raise HTTPException(
                            status_code=400,
                            detail="cross-section moves require a folder target",
                        )
                    if requested_folder != target_section[1]:
                        raise HTTPException(
                            status_code=409,
                            detail="target session moved to another folder",
                        )
                if target_section != section and target_folder is None:
                    raise HTTPException(status_code=400, detail="sessions must be in the same section")
                if target_id == sid:
                    return sorted_sessions(list(self.sessions.values()))

                peers = [
                    peer for peer in sorted_sessions(list(self.sessions.values()))
                    if session_section_key(peer) == target_section
                    and peer.get("id") != sid
                ]
                target_index = next((idx for idx, peer in enumerate(peers) if peer.get("id") == target_id), None)
                if target_index is None:
                    raise HTTPException(status_code=404, detail="target session not found")
                if target_section != section:
                    sess["folder"] = target_section[1]
                    sess["pinned"] = False
                    sess["pinned_at"] = None
                    sess["archived"] = False
                    sess["archived_at"] = None
                insert_index = target_index + 1 if normalized_placement == "after" else target_index
                reordered = peers[:insert_index] + [sess] + peers[insert_index:]
                for index, peer in enumerate(reordered):
                    peer["sort_order"] = (index + 1) * SESSION_ORDER_STEP
                sess["updated_at"] = now_iso()
                await self.save()
                return sorted_sessions(list(self.sessions.values()))

            normalized = (direction or "").strip().lower()
            if normalized not in {"up", "down"}:
                raise HTTPException(status_code=400, detail="direction must be up or down")
            peers = [
                peer for peer in sorted_sessions(list(self.sessions.values()))
                if session_section_key(peer) == section
            ]
            index = next((idx for idx, peer in enumerate(peers) if peer.get("id") == sid), None)
            if index is None:
                raise HTTPException(status_code=404, detail="session not found")
            target_index = index - 1 if normalized == "up" else index + 1
            if 0 <= target_index < len(peers):
                other = peers[target_index]
                current_order = session_order_value(sess)
                other_order = session_order_value(other)
                sess["sort_order"] = other_order
                other["sort_order"] = current_order
                sess["updated_at"] = now_iso()
                await self.save()
            return sorted_sessions(list(self.sessions.values()))

    async def mark_read(self, sid: str, last_read_agent_event_seq: int | None) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            latest = int(sess.get("latest_agent_event_seq") or 0)
            requested = latest if last_read_agent_event_seq is None else max(0, int(last_read_agent_event_seq))
            current = int(sess.get("last_read_agent_event_seq") or 0)
            if requested > latest:
                sess["latest_agent_event_seq"] = requested
                latest = requested
            sess["last_read_agent_event_seq"] = max(current, requested)
            sess["last_read_agent_event_at"] = now_iso()
            sess["manual_unread"] = False
            if sess["last_read_agent_event_seq"] >= int(sess.get("unread_notification_seq") or 0):
                sess.pop("unread_notification_level", None)
                sess.pop("unread_notification_seq", None)
            await self.save()
            return sess

    async def mark_unread(self, sid: str) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            latest = int(sess.get("latest_agent_event_seq") or 0)
            sess["last_read_agent_event_seq"] = max(0, latest - 1)
            sess["last_read_agent_event_at"] = now_iso()
            sess["manual_unread"] = True
            await self.save()
            return sess

    async def apply_emergency_event(
        self,
        sid: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        # Disk replay must not hold the global sessions lock: other chats and
        # health/recovery controls must remain usable during a large scan.
        snapshot = self.sessions.get(sid)
        if not snapshot:
            raise HTTPException(status_code=404, detail="session not found")
        scan_after = max(0, int(snapshot.get("_emergency_reconciled_through_seq") or 0))
        lifecycle_events, reconciled_through = await asyncio.to_thread(
            emergency_lifecycle_events_after, sid, scan_after,
        )
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess or sess is not snapshot:
                raise HTTPException(status_code=404, detail="session not found")
            previous_present = "_emergency_alerts" in sess
            previous = sess.get("_emergency_alerts")
            previous_through_present = "_emergency_reconciled_through_seq" in sess
            previous_through = sess.get("_emergency_reconciled_through_seq")
            previous_reconciled_through = max(
                0,
                int(sess.get("_emergency_reconciled_through_seq") or 0),
            )
            # A concurrent projection may have advanced while we read. Never
            # replay an older raise over a newer acknowledgement.
            lifecycle_events = [candidate for candidate in lifecycle_events
                                if int(candidate.get("seq") or 0) > previous_reconciled_through]
            event_seq = int(event.get("seq") or 0)
            if (
                event_seq > previous_reconciled_through
                and not any(int(candidate.get("seq") or 0) == event_seq for candidate in lifecycle_events)
            ):
                # The caller only passes a record after append_durable_event
                # has fsynced it. Keep the projection robust in tests and on
                # unusual filesystems whose concurrent reader briefly misses
                # the just-extended tail.
                lifecycle_events.append(event)
                lifecycle_events.sort(key=lambda candidate: int(candidate.get("seq") or 0))
                reconciled_through = max(reconciled_through, event_seq)
            alerts = active_emergency_alerts(sess)
            for lifecycle_event in lifecycle_events:
                alerts = apply_emergency_lifecycle_event(alerts, lifecycle_event)
            if alerts:
                sess["_emergency_alerts"] = alerts
            else:
                sess.pop("_emergency_alerts", None)
            if reconciled_through:
                sess["_emergency_reconciled_through_seq"] = max(
                    int(sess.get("_emergency_reconciled_through_seq") or 0),
                    reconciled_through,
                )
            try:
                await self.save()
            except BaseException:
                if previous_present:
                    sess["_emergency_alerts"] = previous
                else:
                    sess.pop("_emergency_alerts", None)
                if previous_through_present:
                    sess["_emergency_reconciled_through_seq"] = previous_through
                else:
                    sess.pop("_emergency_reconciled_through_seq", None)
                await self.persist_restored_state(durable=True)
                raise
            return sess

    async def delete(self, sid: str) -> bool:
        async def commit_and_clean() -> bool:
            async with self._lock:
                existed = self.sessions.pop(sid, None)
                try:
                    await self.save(durable=True)
                except Exception:
                    # The write did not commit.  Keep the registry retryable
                    # and do not remove any session-owned records.
                    if existed is not None:
                        self.sessions[sid] = existed
                    await self.persist_restored_state(durable=True)
                    raise
            if existed:
                await asyncio.to_thread(delete_session_owned_file_records, sid)
                await asyncio.to_thread(delete_session_local_roots, sid)
                await forget_event_seq(sid)
                HISTORY_SEARCH_DIRTY.add(sid)
            return existed is not None

        completion_task = asyncio.create_task(commit_and_clean())
        try:
            return await asyncio.shield(completion_task)
        except asyncio.CancelledError as cancellation:
            # Once registry deletion begins it can commit in the filesystem
            # worker despite caller cancellation.  Finish owned-file and
            # sequence cleanup before allowing the request owner to unwind.
            await join_task_despite_caller_cancellation(completion_task)
            raise cancellation

    async def mark_backend_started(
        self,
        sid: str,
        backend: str,
    ) -> dict[str, Any]:
        """Persist first-turn backend ownership before provider id binding."""

        expected_backend = str(backend or DEFAULT_BACKEND).strip().lower()
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            current_backend = str(
                sess.get("backend") or DEFAULT_BACKEND
            ).strip().lower()
            if current_backend != expected_backend:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "the chat backend changed before the turn could be "
                        "accepted; retry the message"
                    ),
                )
            if not bool(sess.get("backend_locked")):
                sess["backend_locked"] = True
                sess["updated_at"] = now_iso()
                await self.save()
            return sess

    async def save_provider_session(
        self,
        sid: str,
        provider_id: str,
        backend: str,
        *,
        cwd: str | None = None,
        codex_instruction_hash: str | None = None,
        defer_runtime_broadcast: bool = False,
    ) -> dict[str, Any] | None:
        if backend == BACKEND_CODEX:
            ensure_codex_thread_not_pending_fork_cleanup(provider_id)
        usage_signal: dict[str, Any] | None = None
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                return
            previous_codex_thread_id = (
                str(sess.get("codex_thread_id") or sess.get("session_id") or "")
                if backend == BACKEND_CODEX
                else ""
            )
            previous_claude_session_id = (
                str(
                    sess.get("fork_from")
                    or sess.get("claude_session_id")
                    or (
                        sess.get("session_id")
                        if sess.get("backend") == BACKEND_CLAUDE
                        else ""
                    )
                    or ""
                )
                if backend == BACKEND_CLAUDE
                else ""
            )
            sess["session_id"] = provider_id
            sess["backend"] = sess.get("backend") or backend
            sess["backend_locked"] = True
            if backend == BACKEND_CLAUDE:
                sess["claude_session_id"] = provider_id
            elif backend == BACKEND_CODEX:
                sess["codex_thread_id"] = provider_id
            if backend == BACKEND_CLAUDE and cwd:
                sess["claude_session_cwd"] = cwd
            if backend == BACKEND_CODEX and codex_instruction_hash is not None:
                sess["codex_instruction_hash"] = codex_instruction_hash
                sess["codex_instruction_version"] = CODEX_THREAD_POLICY_VERSION
            if backend == BACKEND_CODEX and previous_codex_thread_id != provider_id:
                for key in (
                    "codex_token_usage",
                    "codex_token_usage_snapshot",
                    "_codex_token_usage_checkpoint",
                    "_codex_token_usage_terminal",
                    # Thread hygiene is per provider thread: a fresh thread
                    # starts with zero compactions and no hygiene warning.
                    "codex_compaction_count",
                    "_codex_hygiene_warned",
                ):
                    sess.pop(key, None)
                sess["codex_thread_started_at"] = now_iso()
                usage_signal = clear_provider_context_usage_locked(
                    sess,
                    BACKEND_CODEX,
                    provider_session_id=provider_id,
                    state="cleared",
                )
            if (
                backend == BACKEND_CLAUDE
                and previous_claude_session_id != provider_id
            ):
                sess.pop("claude_context_usage_snapshot", None)
                usage_signal = clear_provider_context_usage_locked(
                    sess,
                    BACKEND_CLAUDE,
                    provider_session_id=provider_id,
                    state="cleared",
                )
            if backend == BACKEND_CLAUDE and sess.get("fork_from") and provider_id != sess.get("fork_from"):
                sess["fork_from"] = None
                sess.pop("fork_resume_session_at", None)
            sess["updated_at"] = now_iso()
            await self.save()
        if usage_signal is not None and not defer_runtime_broadcast:
            await broadcast_provider_runtime_changed(sid, usage_signal)
        if backend == BACKEND_CODEX:
            if (
                previous_codex_thread_id
                and previous_codex_thread_id != provider_id
                and CODEX_THREAD_SESSION_INDEX.get(previous_codex_thread_id) == sid
            ):
                CODEX_THREAD_SESSION_INDEX.pop(previous_codex_thread_id, None)
            CODEX_THREAD_SESSION_INDEX[provider_id] = sid
        return usage_signal if defer_runtime_broadcast else None


STORE = SessionStore()


# A failed durable Claude Stop fence keeps the per-chat maintenance exclusion
# owned until a later explicit Stop can complete the audit marker. This blocks
# managed restart admission without pretending the evicted process is alive.
CLAUDE_STOP_FENCE_SESSIONS: set[str] = set()


PROVIDER_AUTHORITY_LOCK = asyncio.Lock()
# Indexed only by SHA-256(token); plaintext exists solely in the mode-0600
# per-run authority file supplied to the provider. This scopes API authority;
# it is not an OS sandbox against hostile processes running as the same Unix
# account. Host account permissions remain the outer trust boundary.
PROVIDER_AUTHORITIES: dict[str, dict[str, Any]] = {}


def session_lifecycle_lock(session_id: str) -> asyncio.Lock:
    lock = SESSION_LIFECYCLE_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        SESSION_LIFECYCLE_LOCKS[session_id] = lock
    return lock


def ensure_session_not_initializing(session_id: str) -> None:
    session = STORE.sessions.get(session_id)
    if isinstance(session, dict) and session.get("_fork_initializing"):
        raise HTTPException(status_code=409, detail="session fork is still initializing")


def ensure_session_not_deleting(session_id: str) -> None:
    ensure_session_not_initializing(session_id)
    if (
        session_id in DELETING_SESSIONS
        or session_id in DELETED_SESSION_TOMBSTONES
    ):
        raise HTTPException(status_code=409, detail="session is being deleted")


def register_session_task(
    registry: dict[str, set[asyncio.Task[Any]]],
    session_id: str,
    task: asyncio.Task[Any],
) -> None:
    tasks = registry.setdefault(session_id, set())
    tasks.add(task)

    def discard(completed: asyncio.Task[Any]) -> None:
        current = registry.get(session_id)
        if current is None:
            return
        current.discard(completed)
        if not current:
            registry.pop(session_id, None)

    task.add_done_callback(discard)


async def wait_for_session_tasks(
    registry: dict[str, set[asyncio.Task[Any]]],
    session_id: str,
    *,
    timeout: float = CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS,
) -> bool:
    current = asyncio.current_task()
    tasks = [
        task
        for task in tuple(registry.get(session_id) or ())
        if task is not current and not task.done()
    ]
    if not tasks:
        return True
    _done, pending = await asyncio.wait(tasks, timeout=max(0.01, timeout))
    return not pending


async def append_event(
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    def discarded_event() -> dict[str, Any]:
        return {
            "seq": 0,
            "id": f"discarded_{uuid.uuid4().hex[:16]}",
            "session_id": session_id,
            "type": event_type,
            "ts": now_iso(),
            "discarded": True,
            **dict(payload or {}),
        }

    if (
        session_id in DELETING_SESSIONS
        or session_id in DELETED_SESSION_TOMBSTONES
    ):
        return discarded_event()
    async with event_delivery_lock(session_id):
        if (
            session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            return discarded_event()
        ensure_dirs(session_id)
        path = events_path(session_id)
        seq = await next_event_seq(session_id, path)
        ts = now_iso()
        stored_payload = dict(payload or {})
        output = stored_payload.get("output")
        if event_type == "tool_finished" and output is not None:
            output_text = event_output_text(output)
            if len(output_text) > CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS:
                stored_payload["output"] = bounded_codex_output_text(output_text)
                stored_payload["output_chars"] = len(output_text)
                stored_payload["output_truncated"] = True
        event = {
            "seq": seq,
            "id": f"evt_{uuid.uuid4().hex[:16]}",
            "session_id": session_id,
            "type": event_type,
            "ts": ts,
            **stored_payload,
        }
        try:
            with path.open("ab") as f:
                line_offset = f.tell()
                f.write((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
        except BaseException:
            # A short/failed write may have left a non-newline fragment. Force
            # the next append through the repair path while keeping the
            # already-consumed sequence as a high-water mark.
            await reconcile_event_seq_after_failed_write(
                session_id,
                path,
                consumed_high_water=seq,
            )
            raise
        if seq % EVENT_INDEX_STRIDE == 0:
            # Sparse catch-up checkpoint; a failure only costs a full scan.
            with suppress(Exception):
                await asyncio.to_thread(
                    record_event_index_entry, path, seq, line_offset
                )
        HISTORY_SEARCH_DIRTY.add(session_id)
        await update_session_event_metadata(session_id, event)
        await EVENTS.deliver(session_id, event)
    # A stopped run's provider authority ends with its terminal event. Revoke
    # it only after releasing the per-chat event delivery lock.
    if event_type == "turn_stopped":
        await revoke_terminal_run_authority(session_id, event)
    EVENTS.observe(session_id, event)
    return event


def is_agent_visible_event(event_type: str, event: dict[str, Any]) -> bool:
    if event_type == "assistant_text":
        return bool(str(event.get("text") or "").strip())
    if event_type == "turn_finished":
        return bool(str(event.get("result_text") or "").strip())
    if event_type == "codex_goal_updated":
        goal = event.get("goal")
        return isinstance(goal, dict) and goal.get("status") == "complete"
    return event_type in {
        "error",
        "emergency_alert_raised",
        "agent_notification",
        "artifact_created",
        "job_ran",
        "job_finished",
        "job_error",
        "codex_interaction_requested",
        "claude_interaction_requested",
    }


def should_bump_session_updated_at(event_type: str, event: dict[str, Any]) -> bool:
    if (
        event.get("metadata_only") is True and event.get("imported") is True
        and event.get("backend") == BACKEND_CODEX
        and isinstance(event.get("run_id"), str) and event["run_id"].startswith("import_")
        and all(event.get(key) in (None, "") for key in ("prompt", "text", "result_text", "output", "error"))
        and all(event.get(key) in (None, []) for key in ("file_ids", "display_file_ids", "files", "attachments"))
        and all(event.get(key) is None or event.get(key) is False for key in ("is_error", "stopped"))
    ):
        # A control-only import's marker is stamped at import time, whereas
        # its silent input retains source time. Neither represents new work
        # nor may move the chat forward/backward in the sidebar.
        if event_type in {"history_imported", "turn_finished"}:
            return False
        origin = event.get("provider_origin")
        if (event_type == "turn_started" and event.get("prompt") == ""
            and isinstance(origin, dict) and origin.get("provider") == "codex"
            and (
                (event.get("provider_history_repair") == "source_proven_native_replay"
                    and origin.get("kind") == "user"
                    and isinstance(origin.get("native_event_id"), str)
                    and 0 < len(origin["native_event_id"]) <= 256)
                or (event.get("provider_user_authored") is not True
                    and event.get("provider_runtime_context") in ("subagent_notification", "turn_aborted", "provider_notice")
                    and origin.get("kind") == event["provider_runtime_context"])
            )
            and all(isinstance(origin.get(key), str) and 0 < len(origin[key]) <= 256
                    for key in ("event_id", "session_id", "turn_id"))
            and isinstance(origin.get("source_text_sha256"), str)
            and re.fullmatch(r"[a-f0-9]{64}", origin["source_text_sha256"])
            and isinstance(origin.get("timestamp"), str) and len(origin["timestamp"]) <= 64
            and origin["timestamp"] == event.get("ts")):
            try:
                if datetime.fromisoformat(origin["timestamp"].replace("Z", "+00:00")).utcoffset() is not None:
                    return False
            except ValueError:
                pass
    if (
        event.get("metadata_only") is True
        and event.get("imported") is True
        and event.get("backend") == BACKEND_CLAUDE
        and str(event.get("run_id") or "").startswith("import_")
        and event_type in {"history_imported", "turn_finished"}
    ) or event_type == "provider_interruption":
        return False
    if is_agent_visible_event(event_type, event):
        return True
    return event_type in {
        "turn_started",
        "turn_queued",
        "turn_unqueued",
        "turn_queue_updated",
        "turn_queue_reordered",
        "turn_queue_run_now",
        "file_uploaded",
        "job_created",
        "job_deferred",
        "backend_changed",
        "history_imported",
        "session_forked",
        "codex_interaction_resolved",
        "codex_goal_budget_limited",
        "codex_compaction_started",
        "codex_compaction_completed",
        "codex_thread_hygiene",
        "codex_thread_rotated",
        "codex_rollback",
        "codex_review_started",
        "codex_review_finished",
        "codex_shell_started",
        "codex_shell_finished",
        "emergency_alert_acknowledged",
    }


async def update_session_event_metadata(session_id: str, event: dict[str, Any]) -> None:
    QUEUE_CHECKPOINT_DIRTY.add(session_id)
    sess = STORE.sessions.get(session_id)
    if not sess:
        return
    event_type = str(event.get("type") or "")
    run_id = str(event.get("run_id") or "").strip()
    active_run_changed = False
    compaction_state_changed = False
    imported_event = event.get("imported") is True or run_id.startswith("import_")
    if event_type == "turn_started" and run_id and not imported_event:
        # Imported history is a replay of old transcript messages; it never
        # represents a live run and must not make the chat look busy.
        sess["active_run"] = {
            key: event[key]
            for key in (
                "run_id",
                "backend",
                "purpose",
                "provider_context_mode",
                "job_context_mode",
                "job_id",
                "job_title",
                "job_scheduled_run_at",
            )
            if event.get(key) is not None
        }
        active_run_changed = True
    elif event_type in {"turn_finished", "turn_stopped"} and run_id:
        active_run = sess.get("active_run")
        if (
            isinstance(active_run, dict)
            and str(active_run.get("run_id") or "") == run_id
        ):
            sess.pop("active_run", None)
            active_run_changed = True
    if event_type in TIMELINE_INDEX_CODEX_COMPACTION_TYPES:
        lifecycle_key = timeline_index_codex_lifecycle_key(event)
        if lifecycle_key:
            active_compactions = sess.get("_active_codex_compactions")
            if not isinstance(active_compactions, dict):
                active_compactions = {}
            else:
                active_compactions = dict(active_compactions)
            terminal_keys_value = sess.get("_codex_compaction_terminal_keys")
            terminal_keys = [
                str(value)
                for value in (
                    terminal_keys_value
                    if isinstance(terminal_keys_value, list)
                    else []
                )
                if str(value or "").strip()
            ]
            if event_type == "codex_compaction_completed":
                if str(event.get("compaction_id") or "").startswith("native:"):
                    remember_codex_native_compaction_terminal(
                        session_id,
                        str(event.get("thread_id") or ""),
                        str(event.get("turn_id") or "") or None,
                        str(event.get("item_id") or "") or None,
                    )
                if lifecycle_key in active_compactions:
                    active_compactions.pop(lifecycle_key, None)
                    compaction_state_changed = True
                if lifecycle_key in terminal_keys:
                    terminal_keys.remove(lifecycle_key)
                else:
                    # Monotonic per-thread tally of compactions that reached a
                    # terminal row; replays of an already-terminal key do not
                    # count. Reset when the provider thread changes.
                    sess["codex_compaction_count"] = (
                        codex_compaction_count(sess) + 1
                    )
                terminal_keys.append(lifecycle_key)
                terminal_keys = terminal_keys[-CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT:]
                compaction_state_changed = True
            elif lifecycle_key not in terminal_keys:
                tracked = {
                    key: event.get(key)
                    for key in (
                        "compaction_id",
                        "operation_id",
                        "thread_id",
                        "turn_id",
                        "item_id",
                        "run_id",
                    )
                    if event.get(key) is not None
                }
                tracked.update({
                    "lifecycle_key": lifecycle_key,
                    "started_seq": int(event.get("seq") or 0),
                    "started_at": event.get("ts"),
                })
                if active_compactions.get(lifecycle_key) != tracked:
                    active_compactions[lifecycle_key] = tracked
                    compaction_state_changed = True
            if active_compactions:
                sess["_active_codex_compactions"] = active_compactions
            elif "_active_codex_compactions" in sess:
                sess.pop("_active_codex_compactions", None)
                compaction_state_changed = True
            if terminal_keys:
                sess["_codex_compaction_terminal_keys"] = terminal_keys
            elif "_codex_compaction_terminal_keys" in sess:
                sess.pop("_codex_compaction_terminal_keys", None)
                compaction_state_changed = True
    sess["latest_event_seq"] = int(event.get("seq") or 0)
    sess["latest_event_at"] = event.get("ts")
    sess["latest_event_type"] = event_type
    if is_agent_visible_event(event_type, event):
        sess["latest_agent_event_seq"] = int(event.get("seq") or 0)
        sess["latest_agent_event_at"] = event.get("ts")
        sess["latest_agent_event_type"] = event_type
    if event_type == "agent_notification":
        level = event.get("notification_level")
        ranks = {"normal": 1, "important": 2, "emergency": 3}
        sess["latest_notification_level"] = level
        sess["latest_notification_message"] = event.get("message")
        sess["_notification_reconciled_through_seq"] = int(event.get("seq") or 0)
        if level in ranks and ranks[level] >= ranks.get(sess.get("unread_notification_level"), 0):
            sess["unread_notification_level"] = level
            sess["unread_notification_seq"] = int(event.get("seq") or 0)
    bump_updated_at = should_bump_session_updated_at(event_type, event)
    if bump_updated_at:
        sess["updated_at"] = event.get("ts") or now_iso()
    if bump_updated_at or active_run_changed or compaction_state_changed:
        # Called under the per-chat event delivery lock for nearly every
        # event; coalesce instead of rewriting sessions.json per event.
        await STORE.save(flush=False)


class ScheduledJobChatReferenceRepairRequired(HTTPException):
    """A persisted job reference is unsafe to run until the user repairs it."""


async def _revoke_provider_authority(run_id: str) -> None:
    paths: list[str] = []
    async with PROVIDER_AUTHORITY_LOCK:
        for token_hash, capability in list(PROVIDER_AUTHORITIES.items()):
            if str(capability.get("source_run_id") or "") != run_id:
                continue
            paths.append(str(capability.get("authority_path") or ""))
            PROVIDER_AUTHORITIES.pop(token_hash, None)
    # Completed results may each be large. Drop their payloads immediately
    # when the run ends, retaining only compact digest tombstones so a delayed
    # duplicate can never execute the side effect again.
    async with PROVIDER_TOOL_REPLAY_LOCK:
        for replay_key, (input_digest, future) in list(
            PROVIDER_TOOL_REPLAY.items()
        ):
            if replay_key[1] != run_id or not future.done():
                continue
            PROVIDER_TOOL_REPLAY.pop(replay_key, None)
            PROVIDER_TOOL_REPLAY_TOMBSTONES[replay_key] = input_digest
            PROVIDER_TOOL_REPLAY_TOMBSTONES.move_to_end(replay_key)
        while (
            len(PROVIDER_TOOL_REPLAY_TOMBSTONES)
            > PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT
        ):
            PROVIDER_TOOL_REPLAY_TOMBSTONES.popitem(last=False)
    for raw_path in set(paths):
        if not raw_path:
            continue
        path = Path(raw_path)
        if (
            path.parent != PROVIDER_AUTHORITY_ROOT
            or not re.fullmatch(
                re.escape(run_id) + r"-[0-9a-f]{32}\.json",
                path.name,
            )
        ):
            continue
        with suppress(OSError):
            path.unlink()


async def revoke_provider_authority(run_id: str) -> None:
    """Revoke a run grant even when its caller is concurrently cancelled."""

    completion = asyncio.create_task(_revoke_provider_authority(run_id))
    try:
        await asyncio.shield(completion)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(completion)
        raise


def meaningful_chat_cwd(cwd: str | None) -> str | None:
    """Return a cwd only when it is specific enough to link tmux panes to a chat."""
    if not cwd:
        return None
    try:
        path = Path(cwd).expanduser().resolve(strict=False)
        broad_roots = {
            Path("/").resolve(strict=False),
            Path("/tmp").resolve(strict=False),
            Path.home().resolve(strict=False),
        }
        if path in broad_roots:
            return None
        parts = path.parts
        if len(parts) <= 3 and len(parts) >= 2 and parts[1] in {"home", "Users"}:
            return None
        return str(path)
    except Exception:
        clean = str(cwd).strip().rstrip("/")
        if not clean or clean in {"/", "/tmp", str(Path.home())}:
            return None
        if re.fullmatch(r"/(?:home|Users)/[^/]+", clean):
            return None
        return clean


def event_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = str(block.get("type") or "")
                if block_type in {"text", "input_text", "output_text"} and block.get("text"):
                    parts.append(str(block.get("text") or ""))
                elif block_type == "image":
                    parts.append("[image result]")
                elif block_type == "tool_reference":
                    name = str(block.get("tool_name") or block.get("name") or "tool")
                    parts.append(f"[tool reference: {name}]")
                else:
                    with suppress(Exception):
                        parts.append(json.dumps(block, separators=(",", ":")))
            else:
                parts.append(str(block))
        return compact_memory_text("\n".join(part for part in parts if part.strip()), 12_000)
    if isinstance(value, dict):
        if value.get("text"):
            return compact_memory_text(str(value.get("text") or ""), 12_000)
        if value.get("message"):
            return compact_memory_text(str(value.get("message") or ""), 12_000)
        if value.get("type") == "image":
            return "[image result]"
        with suppress(Exception):
            return compact_memory_text(json.dumps(value, separators=(",", ":")), 12_000)
    return str(value)


def delete_session_local_roots(session_id: str) -> None:
    """Remove both session-local storage trees for one retired chat."""

    shutil.rmtree(session_dir(session_id), ignore_errors=True)
    shutil.rmtree(code_diffs_dir(session_id), ignore_errors=True)


def session_codex_subagent_states(session_id: str) -> list[dict[str, Any]]:
    """Read the bounded child index embedded in sessions.json metadata."""

    session = STORE.sessions.get(session_id) or {}
    root_thread_id = session_codex_thread_id(session)
    value = session.get("codex_subagents")
    if isinstance(value, dict):
        states = [dict(state) for state in value.values() if isinstance(state, dict)]
    elif isinstance(value, list):
        states = [dict(state) for state in value if isinstance(state, dict)]
    else:
        states = []
    # A collaboration message sent from a child back to its parent can expose
    # the parent in a receiver list. Older servers briefly persisted that root
    # thread as if it were its own child; never hydrate or present that record.
    return [
        state
        for state in states
        if str(state.get("subagent_id") or "").strip() != root_thread_id
    ]


def rebuild_codex_subagent_indexes() -> None:
    """Restore child ownership/status indexes from durable timeline records."""

    rebuilt: list[tuple[str, str, dict[str, Any]]] = []
    for session_id in STORE.sessions:
        for state in session_codex_subagent_states(session_id):
            if str(state.get("backend") or "") != BACKEND_CODEX:
                continue
            child_thread_id = str(state.get("subagent_id") or "").strip()
            if child_thread_id:
                rebuilt.append((child_thread_id, session_id, dict(state)))
    with CODEX_SUBAGENT_INDEX_LOCK:
        CODEX_SUBAGENT_SESSION_INDEX.clear()
        CODEX_SUBAGENT_STATE.clear()
        CODEX_SUBAGENT_LIVE_GENERATIONS.clear()
        CODEX_SUBAGENT_LIVE_MANAGERS.clear()
        for child_thread_id, session_id, state in rebuilt:
            CODEX_SUBAGENT_SESSION_INDEX[child_thread_id] = session_id
            CODEX_SUBAGENT_STATE[child_thread_id] = state


def remember_codex_native_compaction_terminal(
    session_id: str,
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> None:
    """Remember a native identity suppressed in favor of its manual row.

    The owning manual completion is persisted immediately afterwards, so its
    normal session save also durably carries this bounded alias list. Keeping
    the in-memory mutation synchronous closes the smaller replay race first.
    """

    session = STORE.sessions.get(session_id)
    if session is None:
        return
    aliases = native_codex_compaction_terminal_aliases(
        thread_id,
        turn_id,
        item_id,
    )
    if str(item_id or "").strip() and str(turn_id or "").strip():
        # Also remember the turn-only identity. App-server can replay the
        # lifecycle of this compaction with the item id omitted; that sparse
        # replay must still be recognised, while a lookup carrying a new item
        # id never consults this alias (see codex_native_compaction_was_terminal).
        aliases = tuple(dict.fromkeys((
            *aliases,
            *native_codex_compaction_terminal_aliases(thread_id, turn_id, None),
        )))
    if not aliases:
        return
    alias_set = set(aliases)
    value = session.get("_codex_compaction_native_terminal_keys")
    keys = [
        str(candidate)
        for candidate in (value if isinstance(value, list) else [])
        if str(candidate or "").strip() and str(candidate) not in alias_set
    ]
    keys.extend(aliases)
    session["_codex_compaction_native_terminal_keys"] = keys[
        -(CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT * 3):
    ]


async def revoke_terminal_run_authority(session_id: str, event: dict[str, Any]) -> None:
    """Retire the exact run's provider authority after its terminal event."""

    run_id = str(event.get("run_id") or "")
    if not run_id:
        return
    try:
        await revoke_provider_authority(run_id)
    except Exception as exc:
        logger.exception(
            "provider authority terminal revoke recovered session=%s run=%s error=%s",
            session_id,
            run_id,
            concise_error_message(exc),
        )


def normalized_local_session_cwd(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean or len(clean) > MAX_WORKSPACE_PATH_CHARS:
        return None
    candidate = Path(clean).expanduser()
    if not candidate.is_absolute():
        return None
    return os.path.normpath(str(candidate))


async def refresh_native_session_title(session_id: str) -> None:
    """Best-effort read outside the store lock; recheck ownership before save."""
    sess = STORE.sessions.get(session_id)
    if not sess or not session_accepts_auto_title(sess) or not session_provider_id(sess):
        return
    snapshot = dict(sess)
    try:
        title = None
        if snapshot.get("backend") == BACKEND_CODEX:
            # This release isolates custom-provider transports per chat.
            # Never read a different provider's cached naming metadata.
            manager = existing_codex_app_server_manager(snapshot)
            cached_name = getattr(manager, "cached_thread_name", None)
            if cached_name is not None:
                title = native_session_title(cached_name(session_provider_id(snapshot)))
        if not title:
            title = await asyncio.wait_for(
                asyncio.to_thread(read_native_session_title, snapshot), timeout=1.0,
            )
        if title:
            await STORE.adopt_auto_title(
                session_id, title, source="provider",
                backend=str(snapshot.get("backend") or DEFAULT_BACKEND),
                provider_id=session_provider_id(snapshot),
                expected_title=snapshot.get("title"),
            )
    except Exception as exc:
        # Do not leak titles, transcripts, or paths to logs; metadata failure
        # must not fail a turn or opening a chat. Cancellation still propagates.
        logger.debug(
            "native title lookup skipped session=%s error=%s",
            session_id, type(exc).__name__,
        )


async def generate_session_title(session_id: str, snapshot: dict, reply: str) -> None:
    async with GENERATED_TITLE_SLOTS:
        # Persist the claim before spending any provider usage. A restart does
        # not retry an ambiguous request. This is one optional attempt per chat.
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if (not current or not generated_title_eligible(current)
                    or server_lifecycle_admission_blocker()
                    or title_runtime_key(current) != title_runtime_key(snapshot)
                    or current.get("title") != snapshot.get("title")):
                return
            current["_title_generation_attempted"] = True
            try:
                await STORE.save()
            except asyncio.CancelledError:
                raise
            except Exception:
                current.pop("_title_generation_attempted", None)
                raise
        # A forced restart may close admission during the awaited save. These
        # optional requests must not start a new provider after that fence.
        if SERVER.shutting_down or server_lifecycle_admission_blocker():
            return
        options = {"model": snapshot.get("model"), "env": runner_env()}
        options["model"] = codex_runtime_settings(snapshot)[0]
        options["executable"] = CODEX_BIN
        result = await title_generation.generate_title(
            snapshot["backend"], snapshot["_title_seed"], reply, **options,
        )
        if result:
            await STORE.adopt_auto_title(
                session_id, result, source="generated", backend=snapshot["backend"],
                provider_id=session_provider_id(snapshot), expected_title=snapshot.get("title"),
                expected_runtime=title_runtime_key(snapshot),
            )


def schedule_generated_session_title(session_id: str, event: dict) -> None:
    sess = STORE.sessions.get(session_id)
    if (not sess or not generated_title_eligible(sess)
            or server_lifecycle_admission_blocker()
            or session_id in GENERATED_TITLE_TASKS or len(GENERATED_TITLE_TASKS) >= 16
            or event.get("purpose") or event.get("job_id") or event.get("imported")
            or event.get("stopped") or event.get("exit_code") != 0
            or str(event.get("run_id") or "").startswith("import_")
            or not isinstance(sess.get("_title_seed"), str) or not sess["_title_seed"].strip()
            or not session_provider_id(sess)):
        return
    reply = event.get("result_text")
    if not isinstance(reply, str) or not reply.strip():
        return

    async def work():
        try:
            await generate_session_title(session_id, snapshot, reply[:800])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("automatic title skipped session=%s error=%s", session_id, type(exc).__name__)

    snapshot = dict(sess)
    task = asyncio.create_task(work(), name=f"session-title:{session_id}")
    GENERATED_TITLE_TASKS[session_id] = task

    def finished(done):
        if GENERATED_TITLE_TASKS.get(session_id) is done:
            GENERATED_TITLE_TASKS.pop(session_id, None)

    task.add_done_callback(finished)


HISTORY_MAINTENANCE_STATE: dict[str, dict[str, Any]] = {}


def session_subagent_limit_control(sess: dict[str, Any]) -> dict[str, Any]:
    """Describe native application timing without probing or changing a provider."""
    backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    reason = None
    if backend == BACKEND_CODEX:
        if CODEX_TRANSPORT == CODEX_TRANSPORT_EXEC:
            reason = "unsupported_transport"
        applies_to = "new_or_reloaded_threads"
        message = "Applies to new or reloaded Codex threads; saving does not interrupt current work."
        pending = sess.get("_codex_subagent_limit_reset_pending")
        if sess.get("subagent_limit") is None and isinstance(pending, list) and len(pending) == 3 and pending[0] == SERVER_INSTANCE_ID:
            manager = existing_codex_app_server_manager(sess)
            inherited = codex_effective_thread_config(sess).get("agents", {}).get("max_concurrent_threads_per_session")
            if manager is not None and manager.ready and pending[1:] == [getattr(manager, "_subagent_limit_instance", None), manager.generation] and not _codex_config_positive_int(inherited):
                applies_to = "next_provider_process_start"
                message = "The saved override is cleared. This loaded Codex process can retain its previous limit until a fresh provider process starts. Current work is not interrupted."
    elif backend == BACKEND_CLAUDE:
        with RUNTIME_DIAGNOSTICS_LOCK:
            version = str((RUNTIME_DIAGNOSTICS.get(BACKEND_CLAUDE) or {}).get("version") or "")
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version)
        if not match:
            reason = "claude_version_unknown"
        elif tuple(map(int, match.groups())) < (2, 1, 217):
            reason = "claude_version_unsupported"
        applies_to = "next_idle_provider_start"
        message = "Applies when this chat next starts an idle Claude provider process. Current work keeps its setting. Claude native exceptions may exceed this value."
    else:
        reason = "unsupported_backend"
        applies_to = "new_or_reloaded_threads"
        message = "This provider does not support a per-chat subagent limit."
    if reason and backend == BACKEND_CLAUDE:
        message = "Per-chat subagent limits require a detected Claude Code version of 2.1.217 or newer. Refresh provider status after updating Claude."
    return {"supported": reason is None, "scope": "chat", "mode": "native_concurrent",
            "applies_to": applies_to, "reason": reason, "message": message}


def validate_session_subagent_limit(sess: dict[str, Any], value: Any) -> None:
    if value is None:
        return
    if not _codex_config_positive_int(value):
        raise HTTPException(status_code=422, detail="subagent_limit must be a positive whole number or null")
    control = session_subagent_limit_control(sess)
    if not control["supported"]:
        raise HTTPException(status_code=409, detail=control["message"])


def public_session(sess: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
    detail_fields = () if summary else (
        "auto_title_enabled",
        "system_prompt", "session_id", "claude_session_id", "codex_thread_id",
        "claude_permission_mode",
        "codex_approval_policy", "codex_sandbox_mode",
        "codex_permission_profile", "codex_approvals_reviewer",
        "provider_jobs_access",
        "codex_goal", "codex_goal_time_budget_seconds",
        "consult_inbound", "consult_deep_approval", "consult_deep_allowed",
    )
    public = {
        k: sess.get(k)
        for k in (
            "id", "title", "folder", "cwd", "backend", "model", "effort",
            *detail_fields,
            "codex_thread_status",
            "codex_pending_interaction_count", "codex_needs_user_action",
            "claude_pending_interaction_count", "claude_needs_user_action",
            "parent_id", "fork_from", "memory_forked", "memory_seed_used",
            "pinned", "pinned_at", "archived", "archived_at", "sort_order", "created_at", "updated_at",
            "latest_event_seq", "latest_event_at", "latest_event_type",
            "latest_agent_event_seq", "latest_agent_event_at", "latest_agent_event_type",
            "last_read_agent_event_seq", "last_read_agent_event_at", "manual_unread",
        )
    }
    # Set by turn_started and cleared by its turn_finished/turn_stopped, so a
    # list shows a working chat as busy whatever its latest event is.
    public["running"] = isinstance(sess.get("active_run"), dict)
    if isinstance(sess.get("group_member"), dict):
        public["group_member"] = dict(sess["group_member"])
    if isinstance(sess.get("token_totals"), dict):
        # Provider-reported tokens across every turn; see providers/token_totals.py.
        public["token_totals"] = dict(sess["token_totals"])
    notification_fields = (
        "unread_notification_level", "unread_notification_seq",
        "latest_notification_level", "latest_notification_message",
    )
    for field in notification_fields:
        if not summary or field in sess:
            public[field] = sess.get(field)
    if summary and "latest_notification_level" in sess:
        public.setdefault("unread_notification_level", None)
        public.setdefault("unread_notification_seq", None)
    maintenance = HISTORY_MAINTENANCE_STATE.get(str(sess.get("id") or ""), {})
    # Never-started maintenance adds no useful information to high-volume lists.
    # Once observed, retain idle/cleared fields so caches can retire old errors.
    if not summary or maintenance:
        public["history_maintenance"] = {key: maintenance.get(key) for key in ("status", "reason", "retry_at")}
    # Omit only never-configured defaults from high-volume lists. A stored null
    # is a clear tombstone and must survive summary/cache merges.
    if not summary or "subagent_limit" in sess:
        public["subagent_limit"] = sess.get("subagent_limit") if _codex_config_positive_int(sess.get("subagent_limit")) else None
    control = session_subagent_limit_control(sess)
    if summary:
        public["subagent_limit_control"] = {"supported": control["supported"]}
        if control["applies_to"] != "new_or_reloaded_threads":
            public["subagent_limit_control"]["applies_to"] = control["applies_to"]
        if not control["supported"]:
            public["subagent_limit_control"].update(reason=control["reason"], message=control["message"])
    else:
        public["subagent_limit_control"] = control
    # Provider ids are intentionally omitted from summary responses, but the
    # UI still needs the authoritative first-turn backend fence.
    public["backend_locked"] = session_backend_locked(sess)
    if summary and "codex_goal" in sess:
        goal = sess.get("codex_goal")
        public["codex_goal_status"] = goal.get("status") if isinstance(goal, dict) else None
        public["codex_goal_updated_at"] = goal.get("updatedAt") if isinstance(goal, dict) else None
    emergency_alert, emergency_count = emergency_summary(sess)
    # Ordinary session-list summaries are a high-volume payload. Keep the
    # emergency keys sparse when there is nothing to report; the dedicated
    # emergency stream uses public_emergency_session() so acknowledgement
    # packets still carry the explicit null/zero tombstone needed by clients.
    if not summary or emergency_alert is not None:
        public["emergency_alert"] = emergency_alert
        public["unacknowledged_emergency_count"] = emergency_count
    if not summary:
        public["provider_jobs_access"] = effective_provider_jobs_access(sess)
        public["claude_stop_fence_pending"] = (
            str(sess.get("id") or "") in CLAUDE_STOP_FENCE_SESSIONS
        )
        public["codex_goal_time_budget_exhausted"] = (
            codex_goal_time_budget_is_exhausted(sess)
        )
    return public


async def mark_provider_context_usage_unavailable(
    session_id: str,
    backend: str,
) -> None:
    signal: dict[str, Any] | None = None
    async with STORE.transaction():
        session = STORE.sessions.get(session_id)
        if (
            session is None
            or str(session.get("backend") or DEFAULT_BACKEND) != backend
            or session_id in DELETING_SESSIONS
            or session_id in DELETED_SESSION_TOMBSTONES
        ):
            return
        legacy_snapshot = (
            session.get("claude_context_usage_snapshot")
            if backend == BACKEND_CLAUDE
            else session.get("codex_token_usage_snapshot")
            if backend == BACKEND_CODEX
            else None
        )
        legacy_usage_present = isinstance(legacy_snapshot, dict) or (
            backend == BACKEND_CODEX
            and any(
                key in session
                for key in (
                    "codex_token_usage",
                    "_codex_token_usage_checkpoint",
                    "_codex_token_usage_terminal",
                )
            )
        )
        if (
            not isinstance(session.get("context_usage_snapshot"), dict)
            and not legacy_usage_present
            and str(session.get("context_usage_state") or "")
            in {"", "unavailable"}
        ):
            return
        provider_id = str(session_provider_id(session) or "") or None
        signal = clear_provider_context_usage_locked(
            session,
            backend,
            provider_session_id=provider_id,
            state="unavailable",
        )
        await STORE.save()
    if signal is not None:
        await broadcast_provider_runtime_changed(session_id, signal)


def session_prompt_addendum(sess: dict[str, Any]) -> str:
    custom_prompt = str(sess.get("system_prompt") or "").strip()
    if not custom_prompt:
        return ""
    return (
        "\n[Per-chat system instructions]\n"
        f"{custom_prompt}\n"
        "[End per-chat system instructions]\n\n"
    )


def session_system_prompt(
    session_id: str,
    sess: dict[str, Any],
    manifest_path: Path,
) -> str:
    del session_id, manifest_path
    return CLAUDE_PROMPT_PRELUDE.format() + session_prompt_addendum(sess)


def native_session_available(session_id: str) -> bool:
    if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None:
        return False
    session = STORE.sessions.get(session_id)
    return bool(
        isinstance(session, dict) and session.get("id") == session_id
        and session_id not in DELETING_SESSIONS
        and session_id not in DELETED_SESSION_TOMBSTONES
        and not session.get("_history_import_initializing")
        and not session.get("_fork_initializing")
    )


def empty_chat_revision(session: dict[str, Any]) -> str:
    # Use durable content/settings, not public runtime projections, read cursors
    # or sort order. Reconnecting, reading or normalizing runtime metadata on
    # startup must not resurrect an unchanged blank row.
    revision = {key: session.get(key) for key in (
        "created_at", "updated_at", "title", "folder", "cwd", "backend",
        "model", "effort", "system_prompt", "_title_source",
        "parent_id",
        "codex_goal", "claude_goal",
    )}
    revision["latest_event_seq"] = int(session.get("latest_event_seq") or 0)
    for key in ("backend_locked", "pinned", "archived", "manual_unread"):
        revision[key] = bool(session.get(key))
    return hashlib.sha256(json.dumps(revision, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def empty_chat_dismissed_for(session: dict[str, Any], client_id: str) -> bool:
    if not client_id or session_backend_locked(session):
        return False
    state = session.get("_empty_chat_cleanup") or {}
    return (isinstance(state, dict) and state.get("version") == 1
            and isinstance(state.get("dismissed_by"), dict)
            and state["dismissed_by"].get(client_id) == empty_chat_revision(session))



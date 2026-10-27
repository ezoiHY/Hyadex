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

import codex_auth
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
from hyadex.core.paths import code_diffs_dir, events_path, existing_cwd, now_iso, session_dir
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
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
from hyadex.providers.codex.app_server import CodexAppServerManager
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
from hyadex.store.files import atomic_json
from hyadex.store.transcripts import jsonl_records
import title_generation

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 agent hosts
    tomllib = None

logger = logging.getLogger("hyadex-server")

FORK_INTERNAL_RUN_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
FORK_INTERNAL_RUN_LOCKS: dict[str, threading.Lock] = {}


def durable_event_seq(event: Any) -> int | None:
    """Return a persisted event sequence only for the canonical JSON shape."""

    if not isinstance(event, dict):
        return None
    value = event.get("seq")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def last_event_seq_from_file(path: Path) -> int:
    """Read the last JSONL event seq without scanning huge chat histories."""
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return 0
            # Retain only the one line fragment that crosses the next chunk
            # boundary.  Keeping every previously inspected chunk here makes
            # a cold-cache recovery consume memory proportional to the whole
            # transcript when its newest rows are invalid.
            suffix = b""
            pos = size
            # A tool result can legitimately make the last JSONL row larger
            # than 16 MiB. Stop only after finding a valid event; memory stays
            # bounded to one record plus the fixed-size read chunk.
            while pos > 0:
                chunk_size = min(pos, 256 * 1024)
                pos -= chunk_size
                f.seek(pos)
                lines = (f.read(chunk_size) + suffix).split(b"\n")
                if pos > 0:
                    suffix = lines.pop(0)
                for raw_line in reversed(lines):
                    if not raw_line.strip():
                        continue
                    with suppress(Exception):
                        event = json.loads(raw_line.decode("utf-8", "replace"))
                        seq = durable_event_seq(event)
                        if seq is not None:
                            return seq
    except Exception as exc:
        logger.warning("failed to read last event seq for %s: %s", path, exc)
    return 0


def repair_event_log_tail(path: Path) -> int:
    """Restore a JSONL append boundary and return its last durable sequence.

    ``append_event`` writes each record and its newline together, but a process
    or filesystem failure can still leave a partial final record behind.  A
    later append must truncate that fragment first; otherwise the two JSON
    objects are joined into one permanently unreadable line.  A complete JSON
    object that merely lacks its newline is retained.

    This slow path runs only when a process-local sequence cache is cold (or a
    prior append failed), and callers serialize it with ``event_delivery_lock``.
    """

    if not path.exists():
        return 0
    try:
        with path.open("rb+") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size <= 0:
                return 0
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return last_event_seq_from_file(path)

            # Missing newline: locate the beginning of the final record.  This
            # is deliberately unbounded but off-loop and only happens during
            # recovery; silently keeping an arbitrarily long corrupt fragment
            # is worse than a one-time reverse scan.
            cursor = size
            line_start = 0
            while cursor > 0:
                chunk_size = min(cursor, 256 * 1024)
                cursor -= chunk_size
                stream.seek(cursor)
                chunk = stream.read(chunk_size)
                newline = chunk.rfind(b"\n")
                if newline >= 0:
                    line_start = cursor + newline + 1
                    break
            stream.seek(line_start)
            raw_tail = stream.read(size - line_start)
            try:
                event = json.loads(raw_tail.decode("utf-8"))
                seq = durable_event_seq(event)
                if seq is None:
                    raise ValueError("final JSON value is not a sequenced event")
            except Exception:
                stream.seek(line_start)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                with suppress(OSError):
                    events_index_path(path).unlink()
                return last_event_seq_from_file(path)

            # The record itself is valid. Complete its JSONL framing rather
            # than discarding an event that was fully written before a crash.
            stream.seek(0, os.SEEK_END)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
            return seq
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.error("failed to repair event log tail for %s: %s", path, exc)
        raise


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


def events_index_path(path: Path) -> Path:
    """Sparse seq->byte-offset checkpoints stored next to events.jsonl."""
    return path.with_suffix(".idx")


def read_event_index(path: Path) -> list[tuple[int, int]] | None:
    """Return the validated ``(seq, offset)`` checkpoints for ``path`` or None.

    The index is bound to the transcript's inode and every offset must lie
    inside the current file; anything else is treated as missing so a
    rewritten or truncated transcript can never make a reader skip events.
    """

    index_path = events_index_path(path)
    try:
        raw = json.loads(index_path.read_bytes())
        stat = path.stat()
    except (OSError, ValueError):
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != EVENT_INDEX_VERSION
        or raw.get("inode") != stat.st_ino
        or not isinstance(raw.get("entries"), list)
    ):
        return None
    entries: list[tuple[int, int]] = []
    previous_seq = 0
    previous_offset = -1
    for item in raw["entries"]:
        try:
            seq, offset = int(item[0]), int(item[1])
        except (TypeError, ValueError, IndexError):
            return None
        if seq <= previous_seq or offset <= previous_offset or offset >= stat.st_size:
            return None
        entries.append((seq, offset))
        previous_seq, previous_offset = seq, offset
    return entries


def write_event_index(path: Path, entries: list[tuple[int, int]]) -> None:
    """Atomically replace events.idx; concurrent writers cannot interleave."""

    index_path = events_index_path(path)
    try:
        inode = path.stat().st_ino
    except OSError:
        return
    payload = json.dumps(
        {
            "version": EVENT_INDEX_VERSION,
            "inode": inode,
            "entries": [[seq, offset] for seq, offset in entries],
        },
        separators=(",", ":"),
    )
    tmp = index_path.with_name(
        f"{index_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, index_path)
    except OSError:
        with suppress(OSError):
            tmp.unlink()


def record_event_index_entry(path: Path, seq: int, offset: int) -> None:
    """Append one stride checkpoint after ``append_event`` wrote line ``seq``."""

    entries = [
        entry
        for entry in (read_event_index(path) or [])
        if entry[0] < seq and entry[1] < offset
    ]
    entries.append((seq, offset))
    write_event_index(path, entries)


def event_delivery_lock(session_id: str) -> asyncio.Lock:
    """Serialize persisted and live event delivery within one chat."""
    return EVENT_DELIVERY_LOCKS.setdefault(session_id, asyncio.Lock())


async def forget_event_seq(
    session_id: str,
    *,
    preserve_at_least: int | None = None,
    preserve_delivery_lock: bool = False,
) -> None:
    async with EVENT_SEQ_LOCK:
        if preserve_at_least is None:
            EVENT_SEQ_CACHE.pop(session_id, None)
        else:
            EVENT_SEQ_CACHE[session_id] = max(
                int(EVENT_SEQ_CACHE.get(session_id) or 0),
                max(0, int(preserve_at_least)),
            )
    if not preserve_delivery_lock:
        # A delete owns this lock while SessionStore removes the transcript and
        # calls us. Replacing that still-held object would let an append enter a
        # fresh lock and recreate the deleted log concurrently. An unlocked
        # entry remains safe to evict; event_delivery_lock() enters an unlocked
        # asyncio.Lock without yielding to another task between lookup/acquire.
        delivery_lock = EVENT_DELIVERY_LOCKS.get(session_id)
        if delivery_lock is not None and not delivery_lock.locked():
            EVENT_DELIVERY_LOCKS.pop(session_id, None)
        repair_lock = EVENT_SEQ_REPAIR_LOCKS.get(session_id)
        if repair_lock is not None and not repair_lock.locked():
            EVENT_SEQ_REPAIR_LOCKS.pop(session_id, None)
    # A cold timeline scan holds a native threading.Lock and can take seconds.
    # Wait for it off-loop, but retain ownership through caller cancellation so
    # deletion cannot release its event lock before the late invalidation lands.
    def invalidate_timeline_index() -> None:
        timeline_lock = timeline_index_session_lock(session_id)
        with timeline_lock:
            with TIMELINE_INDEX_CACHE_LOCK:
                TIMELINE_INDEX_CACHE.pop(session_id, None)

    invalidation_task = asyncio.create_task(
        asyncio.to_thread(invalidate_timeline_index)
    )
    try:
        await asyncio.shield(invalidation_task)
    except asyncio.CancelledError as cancellation:
        await join_task_despite_caller_cancellation(invalidation_task)
        raise cancellation
    FORK_INTERNAL_RUN_CACHE.pop(session_id, None)
    FORK_INTERNAL_RUN_LOCKS.pop(session_id, None)


def codex_provider_mcp_connection_host(bind_address: str | None = None) -> str:
    """Resolve a reachable same-host MCP address for every supported bind."""

    configured = str(
        SERVER.bind_address if bind_address is None else bind_address
    ).strip()
    if configured.lower() == "localhost":
        return "127.0.0.1"
    address = normalized_network_address(configured)
    if address.is_unspecified:
        return "::1" if isinstance(address, ipaddress.IPv6Address) else "127.0.0.1"
    return str(address)


# Set only by the independently managed execution entrypoint, before startup.
# Public bind/port remain unchanged for native client discovery.
PROVIDER_CALLBACK_ORIGIN: str | None = None


def provider_helper_server_origin() -> str:
    """Return the exact same-host origin used by provider helper transports."""

    if PROVIDER_CALLBACK_ORIGIN is not None:
        return PROVIDER_CALLBACK_ORIGIN
    host = codex_provider_mcp_connection_host()
    url_host = (
        f"[{host}]"
        if isinstance(normalized_network_address(host), ipaddress.IPv6Address)
        else host
    )
    return f"http://{url_host}:{SERVER.port}"


def add_provider_no_proxy_environment(
    environment: dict[str, str],
    host: str,
) -> None:
    """Keep the process-secret same-host transport out of ambient proxies."""

    required = [host]
    if ":" in host:
        required.append(f"[{host}]")
    for name in ("NO_PROXY", "no_proxy"):
        existing = [
            item.strip()
            for item in str(environment.get(name) or "").split(",")
            if item.strip()
        ]
        for item in required:
            if item not in existing:
                existing.append(item)
        environment[name] = ",".join(existing)


class CreateSessionRequest(BaseModel):
    title: str | None = None
    auto_title_enabled: bool | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    subagent_limit: int | None = Field(default=None, strict=True, ge=1)
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None
    provider_session_id: str | None = None
    session_id: str | None = None
    claude_session_id: str | None = None
    codex_thread_id: str | None = None
    claude_permission_mode: Literal[
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
        "dontAsk",
        "auto",
    ] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    codex_permission_profile: str | None = Field(default=None, max_length=240)
    codex_approvals_reviewer: Literal["user", "auto_review", "guardian_subagent"] | None = None
    provider_jobs_access: Literal["full", "read_only", "blocked"] | None = None
    import_history: bool | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    auto_title_enabled: bool | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    subagent_limit: int | None = Field(default=None, strict=True, ge=1)
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None
    claude_permission_mode: Literal[
        "default",
        "acceptEdits",
        "plan",
        "bypassPermissions",
        "dontAsk",
        "auto",
    ] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None
    codex_permission_profile: str | None = Field(default=None, max_length=240)
    codex_approvals_reviewer: Literal["user", "auto_review", "guardian_subagent"] | None = None
    provider_jobs_access: Literal["full", "read_only", "blocked"] | None = None


class ReadSessionRequest(BaseModel):
    last_read_agent_event_seq: int | None = None


def normalize_runtime_effort(backend: str, effort: Any, *, strict: bool = False) -> str | None:
    clean = str(effort or "").strip().lower()
    if not clean:
        return None
    if str(backend or "").strip().lower() != BACKEND_CODEX:
        return clean

    normalized = CODEX_EFFORT_ALIASES.get(clean, clean)
    if normalized in CODEX_EFFORTS:
        return normalized
    if strict:
        supported = ", ".join(sorted(CODEX_EFFORTS))
        raise HTTPException(status_code=400, detail=f"Codex effort must be one of: {supported}")
    logger.warning("dropping unsupported Codex effort value=%r", effort)
    return None


def normalize_runtime_effort_for_model(
    backend: str,
    model: Any,
    effort: Any,
    *,
    strict: bool = False,
) -> str | None:
    """Normalize an effort and reject known-incompatible Codex model pairs.

    Unknown/custom model ids remain provider-owned. For picker-visible Codex
    models, fail before launch rather than advertising a saved effort that the
    provider will ignore or reject later.
    """
    normalized = normalize_runtime_effort(backend, effort, strict=strict)
    if normalized is None or str(backend or "").strip().lower() != BACKEND_CODEX:
        return normalized

    clean_model = str(model or "").strip()
    if not clean_model:
        configured_model, _configured_effort, _configured_service_tier = (
            codex_user_config_defaults()
        )
        clean_model = configured_model or CODEX_DEFAULT_MODEL
    supported = CODEX_FALLBACK_MODEL_EFFORTS.get(clean_model)
    if supported is None or normalized in supported:
        return normalized
    if strict:
        choices = ", ".join(supported)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Codex model {clean_model} does not support effort {normalized}; "
                f"choose one of: {choices}"
            ),
        )
    logger.warning(
        "dropping unsupported Codex model/effort pair model=%s effort=%s",
        clean_model,
        normalized,
    )
    return None


def clamp_codex_runtime_effort(model: str, effort: Any) -> str:
    """Return a launch-safe effort without rewriting the saved preference.

    The Codex config file can outlive a model or can contain an effort copied
    from another model.  Known fallback models are clamped to their nearest
    supported level at launch. Unknown/discovered model ids remain owned by
    Codex because a static server table must not reject newer capabilities.
    """

    normalized = normalize_runtime_effort(BACKEND_CODEX, effort)
    supported = CODEX_FALLBACK_MODEL_EFFORTS.get(str(model or "").strip())
    if supported is None:
        return normalized or CODEX_DEFAULT_EFFORT
    if normalized in supported:
        return normalized

    if normalized in CODEX_EFFORT_ORDER:
        requested_index = CODEX_EFFORT_ORDER.index(normalized)
        ranked_supported = [
            (CODEX_EFFORT_ORDER.index(candidate), candidate)
            for candidate in supported
            if candidate in CODEX_EFFORT_ORDER
        ]
        lower_or_equal = [
            item for item in ranked_supported if item[0] <= requested_index
        ]
        if lower_or_equal:
            clamped = max(lower_or_equal)[1]
        elif ranked_supported:
            clamped = min(ranked_supported)[1]
        else:
            clamped = supported[0]
    else:
        default_effort = normalize_runtime_effort(
            BACKEND_CODEX,
            CODEX_DEFAULT_EFFORT,
        )
        clamped = (
            default_effort
            if default_effort in supported
            else supported[-1]
        )
    logger.warning(
        "clamping unsupported Codex runtime effort model=%s requested=%r effective=%s",
        model,
        effort,
        clamped,
    )
    return clamped


REMOVED_PROVIDER_MESSAGE = "This provider was removed. Its saved chat history remains readable."
REMOVED_ENDPOINT_MESSAGE = "Custom API endpoints were removed. This chat's saved history remains readable."


def retired_session_runtime_message(sess: dict[str, Any]) -> str | None:
    """Chats whose runtime no longer exists keep readable history but never run.

    A chat saved against a removed custom endpoint must not silently continue
    on the native login: that would send its history to another account.
    """
    if str(sess.get("backend") or DEFAULT_BACKEND).lower() not in VALID_BACKENDS:
        return REMOVED_PROVIDER_MESSAGE
    if "custom" in (sess.get("codex_provider"), sess.get("provider_connection")):
        return REMOVED_ENDPOINT_MESSAGE
    return None


def preview_session_runtime_update(
    sess: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Validate and preview provider runtime fields without mutating state."""

    current_backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    retired = retired_session_runtime_message(sess)
    if retired:
        if set(patch) - {"title", "folder", "pinned", "archived", "auto_title_enabled"}:
            raise HTTPException(410, retired)
        # Organizing saved history must not bind credentials, migrate the
        # provider or validate settings for a runtime that no longer exists.
        return {**sess, "model": sess.get("model"), "effort": sess.get("effort")}
    prospective_backend = (
        str(patch["backend"]).lower()
        if patch.get("backend") is not None
        else current_backend
    )
    if prospective_backend not in VALID_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"backend must be one of {sorted(VALID_BACKENDS)}",
        )
    backend_changed = prospective_backend != current_backend
    if backend_changed and session_backend_locked(sess):
        raise HTTPException(
            status_code=409,
            detail=(
                "backend is locked after the chat starts; fork or create "
                "a new chat to use another backend"
            ),
        )

    prospective_model = (
        str(patch.get("model") or "").strip() or None
        if "model" in patch
        else None if backend_changed else sess.get("model")
    )
    previous_model = prospective_model
    if prospective_backend == BACKEND_CODEX and prospective_model:
        prospective_model = codex_available_model(str(prospective_model))
    prospective_effort = (
        patch.get("effort")
        if "effort" in patch
        else None if backend_changed else sess.get("effort")
    )
    if prospective_model != previous_model and prospective_effort:
        # Saved/queued settings may contain a level unsupported by the new
        # model. Validate the vocabulary, then preserve the nearest level.
        prospective_effort = clamp_codex_runtime_effort(
            str(prospective_model),
            normalize_runtime_effort(BACKEND_CODEX, prospective_effort, strict="effort" in patch),
        )
    normalized_effort = normalize_runtime_effort_for_model(
        prospective_backend, prospective_model, prospective_effort, strict="effort" in patch,
    )

    preview = dict(sess)
    preview["backend"] = prospective_backend
    preview["model"] = prospective_model
    preview["effort"] = normalized_effort
    if backend_changed:
        provider_id_field = {
            BACKEND_CLAUDE: "claude_session_id",
            BACKEND_CODEX: "codex_thread_id",
        }[prospective_backend]
        preview["session_id"] = sess.get(provider_id_field)
    return preview


def effective_claude_permission_mode(sess: dict[str, Any]) -> str:
    """Return one canonical SDK permission mode for persisted/legacy chats."""

    value = str(
        sess.get("claude_permission_mode")
        or CLAUDE_DEFAULT_PERMISSION_MODE
    ).strip()
    return (
        value
        if value in CLAUDE_PERMISSION_MODES
        else CLAUDE_DEFAULT_PERMISSION_MODE
    )


def effective_provider_jobs_access(sess: dict[str, Any] | None) -> str:
    """Return the durable agent Jobs policy for a persisted or legacy chat."""

    value = str(
        (sess or {}).get("provider_jobs_access")
        or PROVIDER_JOBS_ACCESS_DEFAULT
    ).strip().lower()
    return (
        value
        if value in PROVIDER_JOBS_ACCESS_MODE_SET
        else PROVIDER_JOBS_ACCESS_DEFAULT
    )


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


def clean_emergency_message(value: Any) -> str:
    """Return bounded, one-line plain text suitable for high-priority UI."""

    characters: list[str] = []
    for character in str(value or "")[: EMERGENCY_MESSAGE_MAX_CHARS * 4]:
        category = unicodedata.category(character)
        characters.append(" " if category.startswith("C") else character)
    return " ".join("".join(characters).split())[:EMERGENCY_MESSAGE_MAX_CHARS]


def normalized_emergency_alert(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    alert_id = str(value.get("id") or "").strip()
    message = clean_emergency_message(value.get("message"))
    raised_at = str(value.get("raised_at") or "").strip()
    source_run_id = str(value.get("source_run_id") or "").strip()
    if (
        not re.fullmatch(r"emergency_[0-9a-f]{32}", alert_id)
        or not message
        or not raised_at
    ):
        return None
    return {
        "id": alert_id,
        "status": "active",
        "severity": "critical",
        "message": message,
        "raised_at": raised_at[:80],
        "source_run_id": source_run_id[:160] or None,
    }


def reversed_jsonl_events(
    path: Path, *, retained_fields: frozenset[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Read newest-first in linear work, without accumulating a giant line.

    Find offsets in fixed blocks. Emergency readers only retain lifecycle
    fields; unrelated text/media is validated and skipped in bounded chunks.
    Other consumers retain the full event contract.
    """

    if not path.exists():
        return
    chunk_size = 256 * 1024
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        position = end = source.tell()

        def read_event(start: int, stop: int) -> dict[str, Any] | None:
            if stop <= start:
                return None
            source.seek(start)
            if retained_fields is not None:
                for event, _, _ in jsonl_records(
                    source, end=stop, project=False, require_newline=False,
                    retained_fields=retained_fields, deadline=float("inf"),
                    # This is a server-owned journal, not a native import.
                    # A tool payload can legitimately contain many values.
                    # Keep retained metadata bounded; scan omitted values in
                    # linear time without the importer's value-count ceiling.
                    max_nodes=None, max_depth=512,
                ):
                    return event
                return None
            with suppress(ValueError, UnicodeError):
                event = json.loads(source.read(stop - start).decode("utf-8", "replace"))
                if isinstance(event, dict):
                    return event
            return None

        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            source.seek(position)
            block = source.read(read_size)
            offset = len(block)
            while (offset := block.rfind(b"\n", 0, offset)) >= 0:
                start = position + offset + 1
                event = read_event(start, end)
                if event is not None:
                    yield event
                end = start - 1
        event = read_event(0, end)
        if event is not None:
            yield event


EMERGENCY_EVENT_FIELDS = frozenset({
    "id", "seq", "type", "ts", "session_id", "run_id",
    "emergency_alert", "emergency_alert_id", "emergency_request_id", "emergency_request_digest",
})


def notification_events_after(
    session_id: str,
    after_seq: int,
) -> tuple[list[dict[str, Any]], int]:
    newest_seq = max(0, int(after_seq or 0))
    events: list[dict[str, Any]] = []
    for event in reversed_jsonl_events(events_path(session_id), retained_fields=NOTIFICATION_EVENT_FIELDS):
        seq = int(event.get("seq") or 0)
        newest_seq = max(newest_seq, seq)
        if seq <= after_seq:
            break
        if event.get("type") == "agent_notification":
            events.append(event)
    events.reverse()
    return events, newest_seq


def reconcile_session_agent_notifications(session_id: str, sess: dict[str, Any]) -> bool:
    through = max(0, int(sess.get("_notification_reconciled_through_seq") or 0))
    checkpoint = through or max(0, int(sess.get("latest_event_seq") or 0))
    events, newest_seq = notification_events_after(session_id, checkpoint)
    if not events and not through:
        return False
    before = (
        sess.get("latest_notification_level"), sess.get("latest_notification_message"),
        sess.get("unread_notification_level"), sess.get("unread_notification_seq"), through,
    )
    if not through and events:
        sess.pop("unread_notification_level", None)
        sess.pop("unread_notification_seq", None)
    read_seq = int(sess.get("last_read_agent_event_seq") or 0)
    ranks = {"normal": 1, "important": 2, "emergency": 3}
    for event in events:
        level = event.get("notification_level")
        seq = int(event.get("seq") or 0)
        if level not in ranks or seq <= 0:
            continue
        sess["latest_notification_level"] = level
        sess["latest_notification_message"] = event.get("message")
        if seq > int(sess.get("latest_agent_event_seq") or 0):
            sess["latest_agent_event_seq"] = seq
            sess["latest_agent_event_at"] = event.get("ts")
            sess["latest_agent_event_type"] = "agent_notification"
        if seq > int(sess.get("latest_event_seq") or 0):
            sess["latest_event_seq"] = seq
            sess["latest_event_at"] = event.get("ts")
            sess["latest_event_type"] = "agent_notification"
        if seq > read_seq and ranks[level] >= ranks.get(sess.get("unread_notification_level"), 0):
            sess["unread_notification_level"] = level
            sess["unread_notification_seq"] = seq
    if int(sess.get("unread_notification_seq") or 0) <= read_seq:
        sess.pop("unread_notification_level", None)
        sess.pop("unread_notification_seq", None)
    if newest_seq:
        sess["_notification_reconciled_through_seq"] = newest_seq
    after = (
        sess.get("latest_notification_level"), sess.get("latest_notification_message"),
        sess.get("unread_notification_level"), sess.get("unread_notification_seq"),
        int(sess.get("_notification_reconciled_through_seq") or 0),
    )
    return before != after


def emergency_lifecycle_events_after(
    session_id: str,
    after_seq: int,
) -> tuple[list[dict[str, Any]], int]:
    """Return every emergency lifecycle record after a durable projection."""

    newest_seq = max(0, int(after_seq or 0))
    events: list[dict[str, Any]] = []
    for event in reversed_jsonl_events(
        events_path(session_id), retained_fields=EMERGENCY_EVENT_FIELDS,
    ):
        seq = int(event.get("seq") or 0)
        newest_seq = max(newest_seq, seq)
        if seq <= after_seq:
            break
        if str(event.get("type") or "") in {
            "emergency_alert_raised",
            "emergency_alert_acknowledged",
        }:
            events.append(event)
    events.reverse()
    return events, newest_seq


def active_emergency_alerts(sess: dict[str, Any]) -> list[dict[str, Any]]:
    values = sess.get("_emergency_alerts")
    if not isinstance(values, list):
        values = []
    by_id: dict[str, dict[str, Any]] = {}
    for value in values:
        alert = normalized_emergency_alert(value)
        if alert:
            by_id[alert["id"]] = alert
    return list(by_id.values())


def apply_emergency_lifecycle_event(
    alerts: list[dict[str, Any]],
    event: dict[str, Any],
) -> list[dict[str, Any]]:
    by_id = {str(alert["id"]): dict(alert) for alert in alerts}
    event_type = str(event.get("type") or "")
    if event_type == "emergency_alert_raised":
        alert = normalized_emergency_alert(event.get("emergency_alert"))
        if alert:
            by_id[alert["id"]] = alert
    elif event_type == "emergency_alert_acknowledged":
        alert_id = str(event.get("emergency_alert_id") or "").strip()
        by_id.pop(alert_id, None)
    return list(by_id.values())


def reconcile_session_emergency_alerts(
    session_id: str,
    sess: dict[str, Any],
) -> bool:
    """Repair summary state from the durable tail after an interrupted save."""

    before = active_emergency_alerts(sess)
    alerts = before
    previous_through = max(
        0,
        int(sess.get("_emergency_reconciled_through_seq") or 0),
    )
    lifecycle_events, reconciled_through = emergency_lifecycle_events_after(
        session_id,
        previous_through,
    )
    for event in lifecycle_events:
        alerts = apply_emergency_lifecycle_event(alerts, event)
    if alerts:
        sess["_emergency_alerts"] = alerts
    else:
        sess.pop("_emergency_alerts", None)
    if reconciled_through:
        sess["_emergency_reconciled_through_seq"] = reconciled_through
    return alerts != before or reconciled_through != previous_through


def emergency_summary(sess: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    alerts = active_emergency_alerts(sess)
    return (dict(alerts[-1]) if alerts else None, len(alerts))


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


CODEX_RETIRED_APP_SERVER_MANAGERS: list[CodexAppServerManager] = []
CODEX_SESSION_APP_SERVER_MANAGERS: dict[str, CodexAppServerManager] = {}


CODEX_THREAD_SESSION_INDEX: dict[str, str] = {}


CODEX_SUBAGENT_SESSION_INDEX: dict[str, str] = {}
CODEX_SUBAGENT_STATE: dict[str, dict[str, Any]] = {}
# Runtime-only proof that an active child state came from the current provider
# generation. Persisted "running" cards are historical hints, not evidence
# that work survived an HyadexServer/app-server restart.
CODEX_SUBAGENT_LIVE_GENERATIONS: dict[str, int] = {}
CODEX_SUBAGENT_LIVE_MANAGERS: dict[str, Any] = {}
# Timeline indexes are built in worker threads while provider notifications
# update these maps on the event loop. Protect compound mutations and snapshots
# so a semantic-page rebuild never iterates a dictionary that is changing.
CODEX_SUBAGENT_INDEX_LOCK = threading.RLock()


CODEX_COMPACTION_TERMINAL_HISTORY_LIMIT = 128


PROVIDER_AUTHORITY_LOCK = asyncio.Lock()
# Indexed only by SHA-256(token); plaintext exists solely in the mode-0600
# per-run authority file supplied to the provider. This scopes API authority;
# it is not an OS sandbox against hostile processes running as the same Unix
# account. Host account permissions remain the outer trust boundary.
PROVIDER_AUTHORITIES: dict[str, dict[str, Any]] = {}


RUNTIME_DIAGNOSTICS: dict[str, dict[str, Any]] = {}
RUNTIME_DIAGNOSTICS_LOCK = threading.RLock()


ABANDONED_FORK_PROVIDER_THREADS: set[str] = set()


def ensure_codex_thread_not_pending_fork_cleanup(thread_id: Any) -> None:
    """Fence provider identities while post-crash fork cleanup owns them."""

    clean_thread_id = str(thread_id or "").strip()
    if clean_thread_id and clean_thread_id in ABANDONED_FORK_PROVIDER_THREADS:
        raise HTTPException(
            status_code=409,
            detail=(
                "this Codex thread is being retired after an interrupted fork; "
                "retry after cleanup completes"
            ),
        )


def session_codex_thread_id(session: dict[str, Any]) -> str:
    """Return the chat's Codex root whether Codex is active or parked."""

    parked = str(session.get("codex_thread_id") or "").strip()
    if parked:
        return parked
    if (
        str(session.get("backend") or DEFAULT_BACKEND).strip().lower()
        == BACKEND_CODEX
    ):
        return str(session.get("session_id") or "").strip()
    return ""


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


def file_record_belongs_to_session(record: Any, session_id: str) -> bool:
    if not isinstance(record, dict):
        return True
    owner = str(record.get("session_id") or "").strip()
    return not owner or owner == session_id


def event_establishes_session_file_origin(event: dict[str, Any], session_id: str) -> bool:
    """Return whether a nested file record genuinely originated in this chat."""
    event_owner = str(event.get("session_id") or "").strip()
    original_owner = str(event.get("original_session_id") or "").strip()
    return (
        event.get("forked") is not True
        and (not event_owner or event_owner == session_id)
        and (not original_owner or original_owner == session_id)
    )


def read_file_meta_record(file_id: str) -> dict[str, Any] | None:
    clean_file_id = normalized_file_id(file_id)
    if not clean_file_id:
        return None
    meta_path = FILES_ROOT / clean_file_id / "meta.json"
    if not meta_path.is_file():
        return None
    with suppress(Exception):
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            return record
    return None


def delete_session_owned_file_records(session_id: str) -> int:
    """Delete only registry entries explicitly owned by a removed chat."""
    removed = 0
    if not FILES_ROOT.is_dir():
        return removed
    for entry in FILES_ROOT.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        metadata = read_file_meta_record(entry.name)
        if str((metadata or {}).get("session_id") or "").strip() != session_id:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            logger.warning(
                "failed to remove session-owned file session=%s file_id=%s: %s",
                session_id,
                entry.name,
                concise_error_message(exc),
            )
    return removed


def delete_session_local_roots(session_id: str) -> None:
    """Remove both session-local storage trees for one retired chat."""

    shutil.rmtree(session_dir(session_id), ignore_errors=True)
    shutil.rmtree(code_diffs_dir(session_id), ignore_errors=True)


def event_files_belong_to_session(event: dict[str, Any], session_id: str | None = None) -> bool:
    owner = str(session_id or event.get("session_id") or "").strip()
    if not owner:
        return True
    for key in ("file", "artifact"):
        record = event.get(key)
        if not file_record_belongs_to_session(record, owner):
            return False
        if (
            isinstance(record, dict)
            and not str(record.get("session_id") or "").strip()
            and not event_establishes_session_file_origin(event, owner)
        ):
            return False
    return True


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


TIMELINE_INDEX_CODEX_GOAL_TYPES = {
    "codex_goal_budget_limited",
}
TIMELINE_INDEX_CODEX_COMPACTION_TYPES = {
    "codex_compaction_started",
    "codex_compaction_completed",
}


def timeline_index_codex_lifecycle_key(
    event: dict[str, Any],
) -> str | None:
    """Return the stable semantic key for durable Codex control markers."""
    event_type = str(event.get("type") or "")
    if event_type in TIMELINE_INDEX_CODEX_GOAL_TYPES:
        return "codex:goal-budget"
    if event_type not in TIMELINE_INDEX_CODEX_COMPACTION_TYPES:
        return None
    compaction_id = str(event.get("compaction_id") or "").strip()
    if compaction_id:
        return f"codex:compaction:{compaction_id}"
    operation_id = str(event.get("operation_id") or "").strip()
    if operation_id:
        return f"codex:compaction:{operation_id}"
    native_id = (
        str(event.get("turn_id") or "").strip()
        or str(event.get("item_id") or "").strip()
        or str(event.get("id") or "").strip()
        or str(event.get("seq") or "")
    )
    return f"codex:compaction:{native_id}"


def native_codex_compaction_id(
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> str:
    """Return one durable identity shared by native start/completion events."""

    return ":".join((
        "native",
        str(thread_id or "-").strip() or "-",
        str(turn_id or "-").strip() or "-",
        str(item_id or "-").strip() or "-",
    ))


def native_codex_compaction_terminal_aliases(
    thread_id: str,
    turn_id: str | None,
    item_id: str | None,
) -> tuple[str, ...]:
    """Return stable, unambiguous identities for a provider compaction.

    App-server can replay a completion with either the turn or item field
    omitted after the owning ACTIVE record has been released. Thread+turn and
    thread+item remain safe aliases; thread alone does not, because it could
    suppress a later legitimate compaction on the same conversation.

    The thread+turn alias is only produced while the item id is unknown. One
    long turn can compact several times, each with its own item id, so a
    lookup that carries an item id must never be satisfied by the turn alias
    of an earlier compaction in the same turn.
    """

    thread_id = str(thread_id or "").strip()
    turn_id = str(turn_id or "").strip()
    item_id = str(item_id or "").strip()
    if not thread_id or (not turn_id and not item_id):
        return ()

    aliases = [
        f"codex:compaction:{native_codex_compaction_id(thread_id, turn_id, item_id)}"
    ]
    alias_parts = (("item", item_id),) if item_id else (("turn", turn_id),)
    for kind, value in alias_parts:
        if not value:
            continue
        encoded = json.dumps(
            [thread_id, value],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
        aliases.append(
            f"codex:compaction-native:{kind}:{hashlib.sha256(encoded).hexdigest()}"
        )
    return tuple(aliases)


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


def timeline_index_session_lock(session_id: str) -> threading.Lock:
    return TIMELINE_INDEX_LOCK_STRIPES[
        hash(session_id) % len(TIMELINE_INDEX_LOCK_STRIPES)
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


def codex_compaction_count(sess: dict[str, Any]) -> int:
    """Return the persisted per-thread compaction tally, tolerating bad data."""

    value = sess.get("codex_compaction_count")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def normalized_local_session_cwd(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean or len(clean) > MAX_WORKSPACE_PATH_CHARS:
        return None
    candidate = Path(clean).expanduser()
    if not candidate.is_absolute():
        return None
    return os.path.normpath(str(candidate))


def read_native_session_title(sess: dict[str, Any]) -> str | None:
    """Read existing native metadata only; never launch a CLI or model request.

    Missing/changed provider formats intentionally leave the prompt fallback.
    Claude and Codex use the same roots as history discovery. Cursor reads only
    exact-workspace, exact-session naming metadata, never conversation blobs.
    """
    backend = str(sess.get("backend") or DEFAULT_BACKEND)
    provider_id = provider_session_identifier(session_provider_id(sess))
    if not provider_id:
        return None
    if backend == BACKEND_CLAUDE:
        candidates = claude_history_candidates(provider_id)
        if len(candidates) != 1:
            return None
        return claude_transcript_title(candidates[0], provider_id)
    if backend == BACKEND_CODEX:
        return native_session_title(codex_session_index_thread_names().get(provider_id))
    return None


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


def title_runtime_key(sess: dict[str, Any]) -> tuple:
    return (sess.get("backend") or DEFAULT_BACKEND, session_provider_id(sess), sess.get("model"))


def cancel_generated_session_title(session_id: str) -> None:
    task = GENERATED_TITLE_TASKS.get(session_id)
    if task is not None and not task.done():
        task.cancel()


async def close_generated_session_titles() -> None:
    tasks = tuple(GENERATED_TITLE_TASKS.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


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


def codex_session_index_thread_names() -> dict[str, str]:
    """Best-effort id -> human title map. Not authoritative: many real Codex

    sessions (including every ``codex exec`` run) never get an entry here, so
    this is only used to prefer a nicer label, never to decide what counts as
    a session — that comes from scanning the transcripts themselves.
    """
    names: dict[str, str] = {}
    if not CODEX_SESSION_INDEX_PATH.exists():
        return names
    with suppress(OSError):
        if CODEX_SESSION_INDEX_PATH.stat().st_size > MAX_CODEX_SESSION_INDEX_BYTES:
            return names
        with CODEX_SESSION_INDEX_PATH.open("rb") as stream:
            for _index in range(MAX_CODEX_SESSION_INDEX_LINES):
                raw_line = stream.readline(MAX_LOCAL_TRANSCRIPT_LINE_BYTES + 1)
                if not raw_line:
                    break
                if len(raw_line) > MAX_LOCAL_TRANSCRIPT_LINE_BYTES:
                    continue
                if not raw_line.strip():
                    continue
                try:
                    entry = json.loads(raw_line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                provider_id = provider_session_identifier(entry.get("id"))
                thread_name = native_session_title(entry.get("thread_name"))
                if provider_id and thread_name:
                    names[provider_id] = thread_name
    return names


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
    if isinstance(sess.get("group_member"), dict):
        public["group_member"] = dict(sess["group_member"])
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


def codex_app_server_env() -> dict[str, str]:
    """Process-wide environment; chat scope is supplied explicitly per thread."""
    env = runner_env()
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    server_origin = provider_helper_server_origin()
    env["HYADEX_SERVER_URL"] = server_origin
    add_provider_no_proxy_environment(
        env,
        codex_provider_mcp_connection_host(),
    )
    env["HYADEX_JOBS_CLI"] = str(SERVER_ROOT / "hyadex_jobs.py")
    env["HYADEX_PUBLISH_CLI"] = str(SERVER_ROOT / "hyadex_publish.py")
    env["HYADEX_EMERGENCY_CLI"] = str(SERVER_ROOT / "hyadex_emergency.py")
    env["HYADEX_NOTIFY_CLI"] = str(SERVER_ROOT / "hyadex_notify.py")
    scrub_provider_runtime_environment(env)
    return env


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


def codex_app_server_managers() -> tuple[CodexAppServerManager, ...]:
    managers: list[CodexAppServerManager] = []
    for manager in (PROVIDERS.codex_manager, *CODEX_RETIRED_APP_SERVER_MANAGERS):
        if manager is not None and not any(manager is item for item in managers):
            managers.append(manager)
    return tuple(managers)


def existing_codex_app_server_manager(sess: dict[str, Any] | None = None) -> CodexAppServerManager | None:
    session_id = str((sess or {}).get("id") or "")
    owner = CODEX_SESSION_APP_SERVER_MANAGERS.get(session_id)
    if not (owner is not None and any(owner is item for item in codex_app_server_managers())):
        thread_id = str(session_provider_id(sess) or "") if sess else ""
        owner = next((item for item in codex_app_server_managers()
                      if thread_id and item.is_thread_loaded(thread_id)), None)
    return owner or PROVIDERS.codex_manager


def session_backend_locked(sess: dict[str, Any]) -> bool:
    return bool(sess.get("backend_locked")) or any(
        str(sess.get(key) or "").strip()
        for key in (
            "session_id",
            "claude_session_id",
            "codex_thread_id",
            "cursor_session_id",
            "opencode_session_id",
            "gemini_session_id",
        )
    )


def codex_user_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"


def load_codex_user_config(path: Path) -> dict[str, Any]:
    if tomllib is not None:
        with path.open("rb") as f:
            return tomllib.load(f)

    # Python 3.10 has no stdlib TOML parser. Only these top-level strings are
    # needed here; nested Codex configuration remains owned by the CLI.
    wanted = {"model", "model_reasoning_effort", "service_tier", "developer_instructions"}
    payload: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        values = list(lexer)
        if values:
            payload[key] = values[0]
    return payload


def codex_user_config_defaults() -> tuple[str, str, str]:
    path = codex_user_config_path()
    try:
        payload = load_codex_user_config(path)
    except Exception as exc:
        logger.debug("codex config default discovery skipped path=%s: %s", path, exc)
        return "", "", ""
    model = str(payload.get("model") or "").strip()
    effort = str(payload.get("model_reasoning_effort") or "").strip()
    service_tier = str(payload.get("service_tier") or "").strip()
    return model, effort, service_tier


def codex_default_service_tier(model: str) -> str:
    return CODEX_FALLBACK_SERVICE_TIERS.get(str(model or "").strip(), "")


def codex_retired_model_replacements() -> dict[str, str]:
    """Scope retirement to native ChatGPT auth, never API/custom endpoints."""
    with RUNTIME_DIAGNOSTICS_LOCK:
        diagnostic = RUNTIME_DIAGNOSTICS.get(BACKEND_CODEX) or {}
        checked_at = diagnostic.get("_codex_auth_mode_checked_at_epoch", diagnostic.get("checked_at_epoch")) or 0
        observed = (str(diagnostic.get("_codex_auth_mode") or "")
                    if time.time() - checked_at < RUNTIME_DIAGNOSTIC_TTL_SECONDS else "")
        replacement = PROVIDERS.codex_model_migration_target
    mode = codex_auth.native_model_auth_mode(
        codex_app_server_env(), cwd=existing_cwd(DEFAULT_CWD), observed_mode=observed,
    )
    if mode != "chatgpt":
        return {}
    return {model: replacement for model in sorted(codex_auth.CHATGPT_RETIRED_MODELS)}


def codex_available_model(model: str) -> str:
    return codex_retired_model_replacements().get(model, model)


def session_prompt_addendum(sess: dict[str, Any]) -> str:
    custom_prompt = str(sess.get("system_prompt") or "").strip()
    if not custom_prompt:
        return ""
    return (
        "\n[Per-chat system instructions]\n"
        f"{custom_prompt}\n"
        "[End per-chat system instructions]\n\n"
    )


def codex_runtime_settings(sess: dict[str, Any]) -> tuple[str, str, str]:
    configured_model, configured_effort, configured_service_tier = codex_user_config_defaults()
    model = codex_available_model(str(sess.get("model") or configured_model or CODEX_DEFAULT_MODEL).strip())
    effort = clamp_codex_runtime_effort(
        model,
        sess.get("effort") or configured_effort or CODEX_DEFAULT_EFFORT,
    )
    service_tier = configured_service_tier or codex_default_service_tier(model)
    return model, effort, service_tier


def codex_goal_time_budget_remaining(sess: dict[str, Any]) -> float | None:
    goal = sess.get("codex_goal")
    limit = sess.get("codex_goal_time_budget_seconds")
    if (
        not isinstance(goal, dict)
        or isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or limit <= 0
    ):
        return None
    if sess.get("codex_goal_time_budget_exhausted") is True:
        return 0.0
    used = goal.get("timeUsedSeconds")
    used_seconds = (
        float(used)
        if isinstance(used, (int, float)) and not isinstance(used, bool)
        else 0.0
    )
    if used_seconds >= float(limit):
        return 0.0
    if str(goal.get("status") or "") != "active":
        return None
    return max(0.0, float(limit) - used_seconds)


def codex_goal_time_budget_is_exhausted(sess: dict[str, Any]) -> bool:
    return codex_goal_time_budget_remaining(sess) == 0


def session_system_prompt(
    session_id: str,
    sess: dict[str, Any],
    manifest_path: Path,
) -> str:
    del session_id, manifest_path
    return CLAUDE_PROMPT_PRELUDE.format() + session_prompt_addendum(sess)


def codex_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    with suppress(Exception):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def bounded_codex_output_text(value: Any) -> str:
    text = codex_output_text(value)
    if len(text) <= CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS:
        return text
    marker = "[Earlier tool output truncated by HyadexServer]\n"
    tail_chars = max(0, CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS - len(marker))
    return marker + (text[-tail_chars:] if tail_chars else "")


def guess_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".mp4", ".m4v")):
        return "video/mp4"
    if lower.endswith(".mov"):
        return "video/quicktime"
    if lower.endswith(".webm"):
        return "video/webm"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".md", ".markdown")):
        return "text/markdown"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def effective_content_type(filename: str, recorded: str | None = None) -> str:
    content_type = str(recorded or "").strip()
    base_type = content_type.split(";", 1)[0].strip().lower()
    if not base_type or base_type in {"application/octet-stream", "binary/octet-stream"}:
        return guess_content_type(filename)
    return content_type


SERVER_RESTART_PHASES = {"idle", "accepted", "signaling", "complete", "failed"}
SERVER_RESTART_ACTIVE_PHASES = {"accepted", "signaling"}


SERVER_RESTART_ACCEPTED_STALE_SECONDS = 10.0


EXECUTION_MAINTENANCE_ACTIVE_DETAIL = "HyadexServer execution worker is in maintenance"
MANAGED_SERVER_RESTART_ACTIVE_DETAIL = "HyadexServer is restarting"


SERVER_RESTART_SIGNAL_LOCK = threading.Lock()


def execution_maintenance_blocks_work() -> bool:
    """Return whether the owned execution worker is reserved for retirement."""
    return SERVER.execution_maintenance is not None and SERVER.execution_maintenance.is_held()


def server_lifecycle_admission_blocker() -> str | None:
    if execution_maintenance_blocks_work():
        return EXECUTION_MAINTENANCE_ACTIVE_DETAIL
    if managed_server_restart_blocks_work():
        return MANAGED_SERVER_RESTART_ACTIVE_DETAIL
    return None


def read_server_restart_status() -> dict[str, Any]:
    try:
        value = json.loads(SERVER_RESTART_STATUS_FILE.read_text())
        if not isinstance(value, dict):
            value = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}
    phase = str(value.get("phase") or "idle")
    value["phase"] = phase if phase in SERVER_RESTART_PHASES else "idle"
    return value


def write_server_restart_status(**changes: Any) -> dict[str, Any]:
    value = read_server_restart_status()
    value.update(changes)
    value["updated_at"] = utc_timestamp()
    atomic_json(SERVER_RESTART_STATUS_FILE, value)
    return value


def managed_server_restart_blocks_work(
    status: dict[str, Any] | None = None,
) -> bool:
    current = (
        status
        if status is not None
        else reconcile_stale_server_restart_acceptance()
    )
    return str(current.get("phase") or "") in SERVER_RESTART_ACTIVE_PHASES


def server_restart_status_age_seconds(status: dict[str, Any]) -> float:
    timestamp: datetime | None = None
    for name in ("completed_at", "failed_at", "updated_at", "requested_at"):
        try:
            candidate = datetime.fromisoformat(
                str(status.get(name) or "").replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        timestamp = (
            candidate
            if candidate.tzinfo is not None
            else candidate.replace(tzinfo=timezone.utc)
        )
        break
    if timestamp is None:
        return float("inf")
    return max(
        0.0,
        (datetime.now(timezone.utc) - timestamp).total_seconds(),
    )


def reconcile_stale_server_restart_acceptance() -> dict[str, Any]:
    """Fail a same-process acceptance whose response worker never started."""

    status = read_server_restart_status()
    if (
        str(status.get("phase") or "") != "accepted"
        or str(status.get("_source_instance_id") or "") != SERVER_INSTANCE_ID
        or server_restart_status_age_seconds(status)
        < SERVER_RESTART_ACCEPTED_STALE_SECONDS
    ):
        return status
    with SERVER_RESTART_SIGNAL_LOCK:
        status = read_server_restart_status()
        if (
            str(status.get("phase") or "") == "accepted"
            and str(status.get("_source_instance_id") or "")
            == SERVER_INSTANCE_ID
            and server_restart_status_age_seconds(status)
            >= SERVER_RESTART_ACCEPTED_STALE_SECONDS
        ):
            status = write_server_restart_status(
                phase="failed",
                message=(
                    "HyadexServer could not begin the accepted restart. "
                    "Retry after refreshing the server connection."
                ),
                failed_at=utc_timestamp(),
            )
    return status


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


async def ensure_claude_permission_mode_update_allowed(
    session_id: str,
    current: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    """Allow a validated mode save for the next turn.

    ``_start_turn_locked`` snapshots the session before scheduling provider
    work, and Claude's permission callback reads the captured ACTIVE value.
    The lifecycle lock serializes that snapshot with this PATCH, so whichever
    operation wins applies atomically to exactly the intended turn.
    """

    _ = session_id, current, patch


async def ensure_backend_update_allowed(
    session_id: str,
    current: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    """Fence real backend changes while provider work owns the session.

    ``start_turn`` registers its request task before it acquires the session
    lifecycle lock or reserves ``BUSY_SESSIONS``.  Checking the task registry
    closes that launch race, including a first turn with no provider identity
    yet, while still allowing idempotent saves from older clients.
    """

    if "backend" not in patch or patch.get("backend") is None:
        return
    current_backend = str(
        current.get("backend") or DEFAULT_BACKEND
    ).strip().lower()
    requested_backend = str(
        patch.get("backend") or current_backend
    ).strip().lower()
    if requested_backend == current_backend:
        return

    async with RUNS.active_lock:
        provider_starting = any(
            not task.done()
            for task in tuple(RUNS.turn_tasks.get(session_id) or ())
        )
        if (
            session_id in RUNS.busy
            or RUNS.active.get(session_id) is not None
            or provider_starting
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "wait for or stop the active turn before changing its "
                    "backend"
                ),
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


def iter_session_events(session_id: str):
    path = events_path(session_id)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event_files_belong_to_session(event, session_id):
                yield event


def list_session_file_records(session_id: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for meta_path in FILES_ROOT.glob("*/meta.json"):
        try:
            rec = json.loads(meta_path.read_text())
        except Exception:
            continue
        if rec.get("session_id") == session_id and rec.get("id"):
            records[str(rec["id"])] = rec

    for event in iter_session_events(session_id):
        for key in ("file", "artifact"):
            rec = event.get(key)
            if (
                isinstance(rec, dict)
                and rec.get("id")
                and file_record_belongs_to_session(rec, session_id)
            ):
                if (
                    not str(rec.get("session_id") or "").strip()
                    and not event_establishes_session_file_origin(event, session_id)
                ):
                    continue
                meta = read_file_meta_record(str(rec.get("id") or ""))
                meta_owner = str((meta or {}).get("session_id") or "").strip()
                if meta_owner and meta_owner != session_id:
                    continue
                out = dict(rec)
                out["event_id"] = event.get("id")
                out["event_seq"] = event.get("seq")
                out["event_type"] = event.get("type")
                records[str(rec["id"])] = out

    normalized = []
    for rec in records.values():
        current = dict(rec)
        filename = str(current.get("filename") or Path(str(current.get("path") or "")).name)
        current["content_type"] = effective_content_type(filename, str(current.get("content_type") or ""))
        normalized.append(current)

    return sorted(
        normalized,
        key=lambda rec: (str(rec.get("created_at") or ""), str(rec.get("filename") or "")),
        reverse=True,
    )

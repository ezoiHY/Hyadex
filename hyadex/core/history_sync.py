"""Provider history sync and import into chat timelines, and durable event batches."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import hmac
import logging.handlers
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable
import uuid

from fastapi import HTTPException

from hyadex.providers.claude.history_provenance import (
    ClaudeInterruptionTracker,
    normalize_claude_interruption_context,
)
from hyadex.providers.claude.history_repair import (
    enrich_interruption_origins,
    filter_native_claude_mailbox_wake_items,
)
from hyadex.providers.codex.history_repair import (
    codex_public_item_origin,
    CodexNativeHistoryProofUnavailable,
    filter_native_codex_history_items,
)
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_PROJECTS_ROOT,
    CODEX_SESSIONS_ROOT,
    DEFAULT_BACKEND,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
)
from hyadex.core.event_log import append_durable_event_batch_sync, read_events
from hyadex.core.publications import ArtifactPublicationError
from hyadex.core.event_seq import last_event_seq_from_file
from hyadex.core.paths import events_path
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_settings import CreateSessionRequest, retired_session_runtime_message
from hyadex.core.sessions import (
    append_event,
    event_delivery_lock,
    next_event_seq,
    public_session,
    reconcile_event_seq_after_failed_write,
    refresh_native_session_title,
    STORE,
    update_session_event_metadata,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
    EVENTS,
)
from hyadex.core.steering import native_steer_requeue_event_payload, parse_legacy_steering_lineage
from hyadex.core.util import (
    bounded_jsonl_records_range,
    clean_assistant_text,
    contained_jsonl_path,
    ensure_dirs,
    strip_hyadex_memory_context,
)
from hyadex.features.files import split_legacy_attached_files
from hyadex.features.history import (
    committed_history_sync_checkpoint,
    history_commit_guard,
    history_cursor_matches_source_stamp,
    history_file_stamp,
    history_item_cursor_digest,
    history_maintenance_key,
    HISTORY_MAINTENANCE_SLOTS,
    history_maintenance_state,
    HISTORY_MAINTENANCE_TASKS,
    history_projection_notice,
    HISTORY_PROJECTION_REVISIONS,
    history_sync_checkpoint,
    HISTORY_SYNC_SCHEDULED,
    HistoryPreparationStale,
    imported_history_terminal_event,
    persist_history_sync_cursor,
    provider_history_path,
    provider_history_prefix_digest,
    provider_history_source_snapshot,
    provider_history_source_stamp,
    reconcile_cursor_history_items,
    save_history_maintenance_state,
    seed_claude_interruption_context,
    unsynced_history_items,
)
from hyadex.features.usage import HUB, join_task_despite_caller_cancellation
from hyadex.features.job_schedules import parse_job_timestamp
from hyadex.providers.authority import (
    NativeSteerHandoffError,
    strip_all_legacy_hyadex_provider_authority_suffixes,
)
from hyadex.providers.claude.history import (
    add_history_item,
    bounded_jsonl_events,
    claude_history_candidates,
    CLAUDE_METADATA_REPAIR_CACHE,
    claude_transcript_matches_cwd,
    HISTORY_SEARCH_DIRTY,
    HISTORY_SEARCH_REPAIR_DIRTY,
    HISTORY_SYNC_CURSOR_VERSION,
    is_claude_task_notification_history_event,
    message_text,
    normalized_history_import_limit,
    normalized_history_item,
    normalized_history_provider_origin,
    normalized_history_sync_cursor,
    provider_session_identifier,
    session_provider_id,
    text_from_content,
)
from hyadex.providers.claude.runtime import claude_resume_file_for_cwd
from hyadex.providers.codex.history import (
    CODEX_GOAL_HISTORY_REPAIR_CACHE,
    codex_history_assistant_item,
    codex_history_assistant_metadata,
    codex_history_user_record,
    CODEX_NATIVE_HISTORY_REPAIR_CACHE,
    codex_runtime_user_item_kind,
    codex_user_item_has_human_provenance,
    find_codex_history,
    is_codex_goal_runtime_user_item,
    merge_codex_history_duplicate,
)
from hyadex.providers.registry import strip_hyadex_provider_context
from hyadex.core.transcripts import jsonl_records

logger = logging.getLogger("hyadex-server")


async def append_durable_event_batch_locked(
    session_id: str,
    event_specs: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Append a durable batch while the caller owns event_delivery_lock."""
    if not event_specs:
        return []
    if (
        session_id in DELETING_SESSIONS
        or session_id in DELETED_SESSION_TOMBSTONES
    ):
        raise ArtifactPublicationError("chat is being deleted")
    ensure_dirs(session_id)
    path = events_path(session_id)
    first_seq = await next_event_seq(session_id, path)
    write_task = asyncio.create_task(asyncio.to_thread(
        append_durable_event_batch_sync,
        path,
        session_id,
        first_seq,
        event_specs,
    ))
    try:
        events = await asyncio.shield(write_task)
    except BaseException:
        committed: list[dict[str, Any]] = []
        try:
            committed = await join_task_despite_caller_cancellation(write_task)
        except BaseException:
            pass
        if committed:
            async with EVENT_SEQ_LOCK:
                EVENT_SEQ_CACHE[session_id] = int(committed[-1]["seq"])
        else:
            await reconcile_event_seq_after_failed_write(
                session_id,
                path,
                consumed_high_water=first_seq - 1,
            )
        if committed:
            # Once fsync succeeds the events are authoritative. Do not report a
            # failed publication that could prompt an unsafe duplicate retry.
            events = committed
        else:
            raise
    async with EVENT_SEQ_LOCK:
        EVENT_SEQ_CACHE[session_id] = int(events[-1]["seq"])
    HISTORY_SEARCH_DIRTY.add(session_id)

    async def deliver_committed_events() -> None:
        if any(event.get("type") == "history_imported" and event.get("backend") == BACKEND_CLAUDE
               and isinstance(event.get("_history_sync_checkpoint"), dict) for event in events):
            # The complete immutable batch is now fsynced. Refresh once before
            # its public projection; ordinary event delivery performs no reads.
            try:
                await asyncio.to_thread(prepare_claude_history_metadata_repair, session_id, refresh=True)
            except Exception as exc:
                logger.warning("history repair refresh failed (%s); committed events remain visible", type(exc).__name__)
        for event in events:
            try:
                await update_session_event_metadata(session_id, event)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                logger.warning(
                    "artifact event metadata update failed session=%s seq=%s: %s",
                    session_id,
                    event.get("seq"),
                    concise_error_message(exc),
                )
            await EVENTS.deliver(session_id, event)
            # Queued turns start (and unqueue/stop) through this durable path;
            # observers must see them exactly like append_event's.
            EVENTS.observe(session_id, event)

    # After fsync, cancellation cannot turn success into failure: that would
    # invite a duplicate retry or cleanup files referenced by durable events.
    delivery_task = asyncio.create_task(deliver_committed_events())
    try:
        await asyncio.shield(delivery_task)
    except BaseException as exc:
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(delivery_task)
    return events


async def append_durable_event_batch(
    session_id: str,
    event_specs: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    async with event_delivery_lock(session_id):
        return await append_durable_event_batch_locked(session_id, event_specs)


async def append_durable_event(
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one fsynced event whose committed result survives cancellation."""

    events = await append_durable_event_batch(
        session_id,
        [(event_type, dict(payload or {}))],
    )
    return events[0]


async def fence_native_steer_delivery(
    session_id: str,
    selected: dict[str, Any],
    *,
    backend: str,
) -> None:
    """Durably hold a queued message before it can reach a native provider.

    The later run-now/turn-start batch consumes the original queue event. If
    the process instead dies at the provider-delivery boundary, recovery sees
    this fence and keeps the message paused rather than replaying a prompt that
    may already have been accepted.
    """

    fence_lock = selected.get("_native_delivery_fence_lock")
    if not isinstance(fence_lock, asyncio.Lock):
        fence_lock = asyncio.Lock()
        selected["_native_delivery_fence_lock"] = fence_lock
    async with fence_lock:
        # The coordinator watchdog and provider consumer can reach this fence
        # concurrently. Exactly one durable marker must win; both share the
        # same selected object and lock.
        if selected.get("_native_delivery_fenced") is True:
            return
        queued_id = selected.get("queued_id")
        display_prompt = str(
            selected.get("display_prompt")
            if selected.get("display_prompt") is not None
            else selected.get("prompt") or ""
        )
        # The standard tombstone makes rollback to an older server fail closed:
        # beta.7 recovery understands turn_unqueued even though it does not know
        # the additive delivery-fence marker. Beta.8 reconstructs the full queued
        # item from the second event and keeps it paused.
        await append_durable_event_batch(session_id, [
            ("turn_unqueued", {
                "queued_id": queued_id,
                "reason": "native_delivery_fence",
            }),
            ("turn_queue_delivery_fenced", {
                "queued_id": queued_id,
                "backend": backend,
                "prompt": display_prompt,
                "request_prompt": str(selected.get("prompt") or ""),
                "display_prompt": display_prompt,
                "file_ids": list(selected.get("file_ids") or []),
                "display_file_ids": list(
                    selected.get("display_file_ids")
                    if selected.get("display_file_ids") is not None
                    else selected.get("file_ids") or []
                ),
                "model": selected.get("model"),
                "effort": selected.get("effort"),
                "purpose": selected.get("purpose"),
                "client_capabilities": list(
                    selected.get("client_capabilities") or []
                ),
                "position": selected.get("_native_delivery_queue_position"),
                "message": (
                    "Force Send delivery started; keep this message paused until "
                    "its provider lifecycle is durably committed."
                ),
            }),
        ])
        selected["_paused_after_stop"] = True
        selected["_native_delivery_fenced"] = True


async def requeue_native_steer_after_safe_rejection(
    session_id: str,
    selected: dict[str, Any],
    *,
    selected_index: int,
    selected_predecessor_id: str | None,
    selected_successor_id: str | None,
    preserve_pause: bool = False,
) -> None:
    """Restore one definitely rejected steer without leaving a replay fence.

    A safe rejection can happen before or after the provider fence, but Run Now
    has already detached the item and released a possible Stop hold. A full
    turn_queued event plus the standard reorder projection makes that rollback
    authoritative on this server and the previous beta. If compensation cannot
    commit, the item stays visibly paused/fenced and the caller receives a
    non-retryable error instead of a false deferred result.
    """

    rollback_error: BaseException | None = None
    async with RUNS.queue_lock:
        items = list(RUNS.queued.get(session_id) or [])
        successor_index = (
            next(
                (
                    idx
                    for idx, item in enumerate(items)
                    if item.get("queued_id") == selected_successor_id
                ),
                None,
            )
            if selected_successor_id is not None
            else None
        )
        predecessor_index = (
            next(
                (
                    idx
                    for idx, item in enumerate(items)
                    if item.get("queued_id") == selected_predecessor_id
                ),
                None,
            )
            if selected_predecessor_id is not None
            else None
        )
        if successor_index is not None:
            insert_at = successor_index
        elif predecessor_index is not None:
            insert_at = predecessor_index + 1
        else:
            insert_at = min(selected_index, len(items))
        items.insert(insert_at, selected)

        positions = [
            {
                "queued_id": str(item.get("queued_id") or ""),
                "position": index + 1,
            }
            for index, item in enumerate(items)
            if str(item.get("queued_id") or "").strip()
        ]
        try:
            # Even pre-fence rejection needs this compensation: Run Now already
            # released a possible durable Stop hold, and concurrent reorder
            # events may have been written while the selected item was detached.
            event_specs = [
                (
                    "turn_queued",
                    native_steer_requeue_event_payload(
                        session_id,
                        selected,
                        position=insert_at + 1,
                    ),
                ),
                (
                    "turn_queue_reordered",
                    {
                        "positions": positions,
                        "message": (
                            "Queue order restored after Force Send was "
                            "rejected before provider delivery."
                        ),
                    },
                ),
            ]
            if preserve_pause:
                event_specs.append(("turn_queue_paused", {
                    "queued_ids": [str(selected.get("queued_id") or "")],
                    "message": "The goal follow-up was not delivered and remains paused until explicitly retried.",
                }))
            await append_durable_event_batch(session_id, event_specs)
        except BaseException as exc:
            rollback_error = exc
            # Never expose a runnable projection when restart recovery still
            # holds the older queue state. Preserve a provider fence when one
            # exists; otherwise a plain Stop-style hold is sufficient.
            selected["_paused_after_stop"] = True
        else:
            # The durable compensation is the commit point. Never publish a
            # runnable in-memory item before both standard events are fsynced.
            selected["_paused_after_stop"] = preserve_pause
            selected.pop("_native_delivery_fenced", None)
            selected.pop("_native_delivery_fence_lock", None)
        selected.pop("_native_delivery_queue_position", None)
        RUNS.queued[session_id] = deque(items)

    if rollback_error is not None:
        if isinstance(
            rollback_error,
            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
        ):
            raise rollback_error
        raise NativeSteerHandoffError(
            (
                "Force Send was rejected before provider delivery, but its "
                "durable queue rollback failed; the message remains paused: "
                f"{concise_error_message(rollback_error)}"
            ),
            safe_to_requeue=False,
            delivery_uncertain=False,
        ) from rollback_error


def prepare_provider_history_metadata_repair(session_id: str) -> bool | None:
    """Prepare bounded source proofs in background history maintenance."""
    prepare_claude_history_metadata_repair(session_id)
    prepare_codex_goal_history_repair(session_id)
    return prepare_codex_native_history_repair(session_id)


def prepare_codex_native_history_repair(session_id: str) -> bool | None:
    session = STORE.sessions.get(session_id)
    provider_id = provider_session_identifier(session_provider_id(session)) if isinstance(session, dict) else None
    if not provider_id or session.get("backend") != BACKEND_CODEX or session.get("_deleting") is True:
        CODEX_NATIVE_HISTORY_REPAIR_CACHE.forget(session_id)
        return
    if CODEX_NATIVE_HISTORY_REPAIR_CACHE.is_prepared(session_id, provider_id):
        return
    cursor = normalized_history_sync_cursor(session)
    source = Path(cursor["source_path"]) if cursor else find_codex_history(provider_id)
    try:
        changed = CODEX_NATIVE_HISTORY_REPAIR_CACHE.prepare(
            session_id, provider_id, events_path(session_id), source, CODEX_SESSIONS_ROOT,
            lambda event: codex_history_event_item(event, expected_session_id=session_id),
        )
    except CodexNativeHistoryProofUnavailable as exc:
        if getattr(exc, "deterministic", False):
            raise
        # Incomplete evidence is retryable at a later explicit read boundary,
        # not a successful empty proof and not a reason to break chat opening.
        logger.debug("native history repair deferred session=%s", session_id)
        return False
    if changed:
        HISTORY_SEARCH_REPAIR_DIRTY.add(session_id)
        HISTORY_SEARCH_DIRTY.add(session_id)


def prepare_codex_goal_history_repair(session_id: str) -> None:
    session = STORE.sessions.get(session_id)
    if (
        not isinstance(session, dict)
        or str(session.get("backend") or "").lower() != BACKEND_CODEX
        or session.get("_deleting") is True
    ):
        CODEX_GOAL_HISTORY_REPAIR_CACHE.forget(session_id)
        return
    provider_id = provider_session_identifier(session_provider_id(session))
    if not provider_id:
        CODEX_GOAL_HISTORY_REPAIR_CACHE.forget(session_id)
        return
    if CODEX_GOAL_HISTORY_REPAIR_CACHE.is_prepared(session_id, provider_id):
        return
    cursor = normalized_history_sync_cursor(session)
    source_path = Path(cursor["source_path"]) if cursor is not None else find_codex_history(provider_id)

    def normalize_legacy_user(source_event: dict[str, Any]) -> str | None:
        record = codex_history_user_record(source_event)
        if record is None:
            return None
        _payload, text = record
        item = normalized_history_item("user", strip_hyadex_generated_user_text(
            text, expected_session_id=session_id, provider_history=True,
        ))
        return item["text"] if item is not None else None

    def classify_user(source_event: dict[str, Any]) -> str | None:
        record = codex_history_user_record(source_event)
        if record is None:
            return None
        payload, text = record
        if is_codex_goal_runtime_user_item(payload, text):
            return "goal"
        return "human" if codex_user_item_has_human_provenance(payload) else "unknown"

    changed = CODEX_GOAL_HISTORY_REPAIR_CACHE.prepare(
        session_id, provider_id, events_path(session_id), source_path,
        CODEX_SESSIONS_ROOT, normalize_legacy_user, classify_user,
    )
    if changed and CODEX_GOAL_HISTORY_REPAIR_CACHE.signature(session_id):
        # A background FTS scan may predate this requested source proof. Its
        # cached rows must be rebuilt even when the durable ledger is unchanged.
        HISTORY_SEARCH_REPAIR_DIRTY.add(session_id)
        HISTORY_SEARCH_DIRTY.add(session_id)


def prepare_claude_history_metadata_repair(
    session_id: str, *, refresh: bool = False, event_window_end: int | None = None,
):
    """Prove old metadata only for a requested chat, never during event egress.

    This bounded read is called once at history-read/cache-build boundaries.
    Neither the projector nor public snapshot creation discovers provider logs.
    """
    session = STORE.sessions.get(session_id)
    if (
        not isinstance(session, dict)
        or str(session.get("backend") or "").lower() != BACKEND_CLAUDE
        or session.get("_deleting") is True
    ):
        CLAUDE_METADATA_REPAIR_CACHE.forget(session_id)
        return
    provider_id = provider_session_identifier(session_provider_id(session))
    if not provider_id:
        CLAUDE_METADATA_REPAIR_CACHE.forget(session_id)
        return

    def normalize_legacy_user(source_event: dict[str, Any]) -> str | None:
        # Reproduce the old import normalization without its lost isMeta flag.
        # Every real user record is normalized too, so quoted identical text
        # makes the proof ambiguous and remains visible.
        legacy = dict(source_event)
        legacy.pop("isMeta", None)
        legacy.pop("isCompactSummary", None)
        legacy.pop("isSidechain", None)
        item = claude_history_event_item(legacy, expected_session_id=session_id)
        return item["text"] if item and item.get("kind") == "user" else None

    def normalize_full_user(source_event: dict[str, Any]) -> str | None:
        if source_event.get("type") != "user" or source_event.get("isMeta") is True:
            return None
        return strip_hyadex_generated_user_text(
            message_text(source_event.get("message"), compact=False),
            expected_session_id=session_id, provider_history=True,
        )

    if event_window_end is not None:
        # Page-local proof must not change the global index signature: learning
        # about an older import is not a reason to rebuild the entire transcript.
        return CLAUDE_METADATA_REPAIR_CACHE.prepare_window(
            session_id, provider_id, events_path(session_id), CLAUDE_PROJECTS_ROOT,
            normalize_legacy_user, event_window_end=event_window_end,
            normalize_full_user=normalize_full_user,
            normalize_assistant=clean_assistant_text,
        )

    changed = CLAUDE_METADATA_REPAIR_CACHE.prepare(
        session_id, provider_id, events_path(session_id), CLAUDE_PROJECTS_ROOT,
        normalize_legacy_user,
        normalize_full_user=normalize_full_user,
        normalize_assistant=clean_assistant_text,
        refresh=refresh,
    )
    if changed and CLAUDE_METADATA_REPAIR_CACHE.signature(session_id):
        HISTORY_SEARCH_REPAIR_DIRTY.add(session_id)
        HISTORY_SEARCH_DIRTY.add(session_id)


def claude_history_event_item(
    event: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any] | None:
    from hyadex.providers.claude.native_goals import is_claude_synthetic_no_response
    if is_claude_synthetic_no_response(event):
        return None
    if event.get("isSidechain") is True:
        # Provider-owned child transcript rows are not parent-chat messages.
        # The structured scope flag, never interruption wording, establishes
        # this boundary; genuine human text in the main conversation remains.
        return None
    event_type = event.get("type")
    provider_origin = {
        "provider": "claude",
        "event_id": event.get("uuid"),
        "session_id": event.get("sessionId"),
        "timestamp": event.get("timestamp"),
        "parent_event_id": event.get("parentUuid"),
        "prompt_id": event.get("promptId"),
    }
    if event_type == "user":
        if event.get("isMeta") is True or event.get("isCompactSummary") is True:
            # Claude marks generated skill/command context as metadata even
            # though it occupies a user-role transcript row. Compaction
            # summaries are likewise provider context, not human input.
            # Preserve genuine
            # user quotations: the structured flag, never the text, decides.
            return None
        if is_claude_task_notification_history_event(event):
            # Claude records its workflow wake-up as a user-role transcript
            # item so the model can consume it. It is provider control state,
            # not something the human typed; leave it in Claude's transcript
            # and omit it only from the Hyadex timeline projection.
            return None
        return normalized_history_item(
            "user",
            strip_hyadex_generated_user_text(
                message_text(event.get("message"), compact=False),
                expected_session_id=expected_session_id,
                provider_history=True,
            ),
            provider_origin=provider_origin,
        )
    if event_type == "assistant":
        return normalized_history_item(
            "assistant",
            message_text(event.get("message"), compact=False),
            provider_origin=provider_origin,
        )
    return None


def append_claude_history_event(
    items: Any,
    event: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> None:
    item = claude_history_event_item(
        event,
        expected_session_id=expected_session_id,
    )
    if item is not None:
        add_history_item(
            items, item["kind"], item["text"],
            provider_origin=item.get("provider_origin"),
            source_text_sha256=item.get("source_text_sha256"),
        )


def parse_claude_history_events(
    events: Iterable[dict[str, Any] | None],
    limit: int | None,
    *,
    expected_session_id: str | None = None,
    interruption_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    from hyadex.providers.claude.native_goals import ClaudeGoalHistoryNormalizer
    items: deque[dict[str, Any]] = deque(
        maxlen=normalized_history_import_limit(limit)
    )
    tracker = ClaudeInterruptionTracker()
    goal_history = ClaudeGoalHistoryNormalizer()
    for event in events:
        if not isinstance(event, dict):
            tracker = ClaudeInterruptionTracker()
            continue
        origin = tracker.consume(event)
        event = goal_history.consume(event)
        if origin is not None:
            add_history_item(items, "interruption", message_text(event.get("message")), provider_origin=origin)
        else:
            append_claude_history_event(
                items,
                event,
                expected_session_id=expected_session_id,
            )
    if interruption_context is not None:
        interruption_context.clear()
        interruption_context.update(tracker.export_context())
    return list(items)


def parse_claude_history(
    path: Path,
    limit: int | None,
    *,
    expected_session_id: str | None = None,
) -> list[dict[str, str]]:
    return parse_claude_history_events(
        bounded_jsonl_events(path, preserve_invalid=True),
        limit,
        expected_session_id=expected_session_id,
    )


def strip_hyadex_generated_user_text(
    text: str,
    *,
    expected_session_id: str | None = None,
    provider_history: bool = False,
) -> str:
    """Project a proven provider user record back to immutable user text.

    Authority stripping requires provider-record provenance or an explicit
    chat boundary. Generic callers may use this helper for legacy context and
    memory wrappers, so treating an exact user-authored example as authority
    at that boundary would silently delete legitimate quoted evidence.
    """

    cleaned = str(text or "")
    if provider_history or expected_session_id is not None:
        cleaned = strip_all_legacy_hyadex_provider_authority_suffixes(
            cleaned,
            expected_session_id=expected_session_id,
            allow_portable_authority_root=True,
        )
    cleaned = strip_hyadex_provider_context(cleaned)
    cleaned = strip_hyadex_memory_context(cleaned)
    cleaned, latest_file_ids = split_legacy_attached_files(
        cleaned,
        recover_file_ids=False,
    )
    lineage = parse_legacy_steering_lineage(
        cleaned,
        latest_file_ids,
        recover_file_ids=False,
    )
    if lineage:
        return str(lineage[-1].get("prompt") or "")
    return cleaned


def codex_history_user_item(
    payload: dict[str, Any],
    text: str,
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any] | None:
    if is_codex_goal_runtime_user_item(payload, text):
        return None
    item = normalized_history_item(
        "user",
        strip_hyadex_generated_user_text(
            text,
            expected_session_id=expected_session_id,
            provider_history=True,
        ),
        allow_user_boilerplate=(codex_user_item_has_human_provenance(payload)
                               or codex_runtime_user_item_kind(payload, text) == "provider_notice"),
    )
    if item is not None and codex_user_item_has_human_provenance(payload):
        item["provider_user_authored"] = True
    return item


def codex_history_user_event_item(
    event: dict[str, Any], text: str, *, expected_session_id: str | None = None,
) -> dict[str, Any] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    item = codex_history_user_item(payload, text, expected_session_id=expected_session_id)
    if item is not None:
        # Classification and positive human provenance are unchanged. User
        # timestamps are descriptive; assistant phase never applies to input.
        item.update(codex_history_assistant_metadata({"ts": event.get("timestamp")}))
        origin = codex_public_item_origin(event)
        if origin is not None:
            runtime_kind = codex_runtime_user_item_kind(payload, text)
            if (runtime_kind is not None and (runtime_kind == "provider_notice"
                    or item.get("text") == text.strip() and item.get("source_text_sha256") is None)):
                full_source_hash = item.get("source_text_sha256")
                runtime_hash = (full_source_hash if isinstance(full_source_hash, str)
                                and re.fullmatch(r"[0-9a-f]{64}", full_source_hash)
                                else hashlib.sha256(item["text"].encode("utf-8", errors="surrogatepass")).hexdigest())
                origin = {**origin, "kind": runtime_kind,
                    "source_text_sha256": runtime_hash}
                item["provider_runtime_context"] = runtime_kind
            item["provider_origin"] = origin
    return item


def codex_history_event_item(
    event: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> dict[str, Any] | None:
    event_type = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "user_message":
            return codex_history_user_event_item(
                event,
                str(payload.get("message") or ""),
                expected_session_id=expected_session_id,
            )
        if payload_type == "agent_message":
            return codex_history_assistant_item(
                event,
                str(payload.get("message") or ""),
            )
    elif event_type == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role == "user":
            return codex_history_user_event_item(
                event,
                text_from_content(payload.get("content"), compact=False),
                expected_session_id=expected_session_id,
            )
        if role == "assistant":
            return codex_history_assistant_item(
                event,
                text_from_content(payload.get("content"), compact=False),
            )
    return None


def append_codex_history_event(
    items: Any,
    event: dict[str, Any],
    *,
    expected_session_id: str | None = None,
) -> None:
    item = codex_history_event_item(
        event,
        expected_session_id=expected_session_id,
    )
    if item is None:
        return
    if items and merge_codex_history_duplicate(items[-1], item):
        return
    items.append(item)


def parse_codex_history_events(
    events: Iterable[dict[str, Any]],
    limit: int | None,
    *,
    expected_session_id: str | None = None,
) -> list[dict[str, str]]:
    items: deque[dict[str, str]] = deque(
        maxlen=normalized_history_import_limit(limit)
    )
    from hyadex.providers.codex.history_repair import CodexCompactionSummaryTracker
    compaction = CodexCompactionSummaryTracker()
    for event in events:
        compaction.discard_summary(items, event)
        append_codex_history_event(
            items,
            event,
            expected_session_id=expected_session_id,
        )
    return list(items)


def parse_codex_history(
    path: Path,
    limit: int | None,
    *,
    expected_session_id: str | None = None,
) -> list[dict[str, str]]:
    return parse_codex_history_events(
        bounded_jsonl_events(path),
        limit,
        expected_session_id=expected_session_id,
    )


def provider_history(sess: dict[str, Any], limit: int | None) -> tuple[Path | None, list[dict[str, str]]]:
    path = provider_history_path(sess)
    if path is None:
        return None, []
    backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    if backend == BACKEND_CLAUDE:
        return path, parse_claude_history(path, limit)
    if backend == BACKEND_CODEX:
        return path, parse_codex_history(path, limit)
    return None, []


def parse_provider_history_delta(
    path: Path,
    backend: str,
    start: int,
    end: int,
    *,
    limit: int | None,
    expected_stat: dict[str, int],
    previous_last_item_digest: str,
    expected_session_id: str | None = None,
    interruption_context: dict[str, Any] | None = None,
    codex_phase_context: dict[str, str] | None = None,
    source_text_context: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], int, str, bool]:
    """Consume the front of an append-only delta without skipping messages."""

    maximum = normalized_history_import_limit(limit)
    items: list[dict[str, Any]] = []
    cursor_offset = start
    last_item_digest = previous_last_item_digest
    source_context = source_text_context if source_text_context is not None else {}
    last_append_state = None
    blocked_on_unseen_message = False
    tracker = ClaudeInterruptionTracker(interruption_context) if backend == BACKEND_CLAUDE else None
    from hyadex.providers.claude.native_goals import ClaudeGoalHistoryNormalizer
    goal_history = ClaudeGoalHistoryNormalizer() if backend == BACKEND_CLAUDE else None
    from hyadex.providers.codex.history_repair import CodexCompactionSummaryTracker
    compaction = CodexCompactionSummaryTracker() if backend == BACKEND_CODEX else None
    if goal_history is not None:
        goal_history.seed(path, start)
    for event, record_end in bounded_jsonl_records_range(
        path,
        start,
        end,
        expected_stat=expected_stat,
        allow_partial=True,
    ):
        if blocked_on_unseen_message:
            # Exhaust the fixed range so its final fstat validation still
            # runs, but never advance across an unconsumed provider message.
            continue
        if compaction is not None:
            if compaction.discard_summary(items, event) and last_append_state is not None:
                last_item_digest, prior_phase, prior_source = last_append_state
                source_context.clear()
                source_context.update(prior_source)
                if codex_phase_context is not None:
                    codex_phase_context.clear()
                    codex_phase_context.update(prior_phase)
        item = None
        prior_context = tracker.export_context() if tracker is not None else None
        if tracker is not None and event is None:
            tracker = ClaudeInterruptionTracker()
        origin = tracker.consume(event) if tracker is not None else None
        if goal_history is not None:
            event = goal_history.consume(event)
        if event is not None:
            if backend == BACKEND_CLAUDE:
                item = normalized_history_item(
                    "interruption", message_text(event.get("message")), provider_origin=origin,
                ) if origin is not None else claude_history_event_item(
                    event, expected_session_id=expected_session_id,
                )
            elif backend == BACKEND_CODEX:
                item = codex_history_event_item(
                    event,
                    expected_session_id=expected_session_id,
                )
        if item is None:
            cursor_offset = record_end
            continue
        item_digest = history_item_cursor_digest(item)
        source_sha256 = str(item.get("source_text_sha256") or "")
        previous_source_sha256 = str(source_context.get("sha256") or "")
        # Keep the legacy cursor digest stable, but never let its bounded-text
        # equality erase a different long source. Old cursors lack this proof;
        # retain that boundary item conservatively until full matching runs.
        if last_item_digest and source_sha256 == previous_source_sha256 and hmac.compare_digest(
            item_digest,
            last_item_digest,
        ):
            duplicate = True
            if backend == BACKEND_CODEX:
                if items:
                    duplicate = merge_codex_history_duplicate(items[-1], item)
                    if duplicate and codex_phase_context is not None:
                        codex_phase_context.clear()
                        if items[-1].get("phase") in ("commentary", "final_answer"):
                            codex_phase_context["phase"] = items[-1]["phase"]
                elif codex_phase_context is not None:
                    previous_phase = codex_phase_context.get("phase")
                    incoming_phase = item.get("phase")
                    duplicate = not (
                        previous_phase in ("commentary", "final_answer")
                        and incoming_phase in ("commentary", "final_answer")
                        and previous_phase != incoming_phase
                    )
                    if duplicate and previous_phase not in ("commentary", "final_answer") and incoming_phase in ("commentary", "final_answer"):
                        # This consumed raw copy proves only the cursor's
                        # current phase, not the presentation of its old row.
                        codex_phase_context["phase"] = incoming_phase
            if duplicate:
                # A retained copy can be enriched before persistence. Across
                # an already committed cursor, preserve legacy dedup when the
                # old phase is absent; never infer or rewrite historical rows.
                cursor_offset = record_end
                continue
        if len(items) >= maximum:
            blocked_on_unseen_message = True
            if tracker is not None:
                tracker = ClaudeInterruptionTracker(prior_context)
            continue
        last_append_state = (last_item_digest, dict(codex_phase_context or {}), dict(source_context))
        items.append(item)
        source_context.clear()
        if source_sha256:
            source_context["sha256"] = source_sha256
        if backend == BACKEND_CODEX and codex_phase_context is not None:
            codex_phase_context.clear()
            if item.get("phase") in ("commentary", "final_answer"):
                codex_phase_context["phase"] = item["phase"]
        last_item_digest = item_digest
        cursor_offset = record_end
    if tracker is not None and interruption_context is not None:
        interruption_context.clear()
        interruption_context.update(tracker.export_context())
    return items, cursor_offset, last_item_digest, blocked_on_unseen_message


def load_provider_history_with_cursor(
    sess: dict[str, Any],
    limit: int | None,
    previous: dict[str, Any] | None,
    *,
    allow_rebaseline: bool = False,
) -> tuple[Path | None, list[dict[str, str]], dict[str, Any] | None, bool]:
    """Read a stable full transcript or the exact append after a durable cursor."""

    path = provider_history_path(sess)
    if path is None:
        return None, [], None, False
    backend = str(sess.get("backend") or DEFAULT_BACKEND).strip().lower()
    provider_id = str(session_provider_id(sess) or "")
    snapshot, continued = provider_history_source_snapshot(path, previous)
    if previous is not None and not continued and not allow_rebaseline:
        raise ValueError(
            "provider transcript no longer extends the durable history cursor"
        )
    start = int(previous["source_offset"]) if continued and previous else 0
    end = int(snapshot["source_offset"])
    interruption_context: dict[str, Any] = {"version": 1}
    codex_phase_context: dict[str, str] = {}
    source_text_context: dict[str, str] = {}
    if continued and previous and previous.get("last_item_source_text_sha256"):
        source_text_context["sha256"] = previous["last_item_source_text_sha256"]
    if (
        backend == BACKEND_CODEX and continued and previous
        and previous.get("codex_last_item_phase") in ("commentary", "final_answer")
    ):
        codex_phase_context["phase"] = previous["codex_last_item_phase"]
    if backend == BACKEND_CLAUDE and continued and previous:
        interruption_context = (
            normalize_claude_interruption_context(previous["claude_interruption_context"], provider_session_id=provider_id)
            if "claude_interruption_context" in previous
            else seed_claude_interruption_context(path, start, expected_stat=snapshot["expected_stat"], provider_session_id=provider_id)
        )
    (
        items,
        cursor_offset,
        last_item_digest,
        delta_overflow,
    ) = parse_provider_history_delta(
        path,
        backend,
        start,
        end,
        limit=limit,
        expected_stat=snapshot["expected_stat"],
        previous_last_item_digest=str(
            (previous or {}).get("last_item_digest") or ""
        ),
        expected_session_id=str(sess.get("id") or "") or None,
        interruption_context=interruption_context,
        codex_phase_context=codex_phase_context,
        source_text_context=source_text_context,
    )
    caught_up = not delta_overflow and cursor_offset == end and end == int(snapshot["source_size"])
    source_digest = (
        str(snapshot["source_digest"])
        if cursor_offset == end
        else provider_history_prefix_digest(
            path,
            cursor_offset,
            expected_stat=snapshot["expected_stat"],
        )
    )
    interruptions = [item for item in items if item.get("kind") == "interruption"] if backend == BACKEND_CLAUDE else []
    if interruptions:
        # This loader already runs in the import worker. Native-control proof
        # is bounded and only read for newly parsed lifecycle records, never
        # for ordinary refreshes or per-event client projection.
        try:
            enriched = enrich_interruption_origins(
                events_path(str(sess["id"])), provider_id,
                [item["provider_origin"] for item in interruptions],
                session_id=str(sess["id"]),
            )
        except Exception:
            enriched = []
        if isinstance(enriched, list) and len(enriched) == len(interruptions):
            for item, candidate in zip(interruptions, enriched):
                origin = normalized_history_provider_origin(candidate)
                original = item["provider_origin"]
                if origin is not None and origin.get("kind") == "interruption" and all(
                    origin.get(field) == original.get(field)
                    for field in ("event_id", "session_id", "timestamp")
                ):
                    item["provider_origin"] = {**original, "cause": origin["cause"]}
    cursor = {
        "version": HISTORY_SYNC_CURSOR_VERSION,
        "backend": backend,
        "provider_session_id": provider_id,
        "source_path": str(path),
        "source_dev": int(snapshot["source_dev"]),
        "source_ino": int(snapshot["source_ino"]),
        # This is the committed, verified prefix size, not necessarily the
        # captured EOF: a delta larger than the message cap is consumed over
        # multiple passes from the front without losing unseen messages.
        "source_size": cursor_offset,
        "source_mtime_ns": int(snapshot["source_mtime_ns"]),
        "source_offset": cursor_offset,
        "source_digest": source_digest,
        "last_item_digest": last_item_digest,
        "timeline_seq": (
            int(previous.get("timeline_seq") or 0)
            if continued and previous
            else 0
        ),
        "timeline_pending_through_seq": 0,
        "timeline_pending_active": False,
        "checkpoint_seq": (
            int(previous.get("checkpoint_seq", previous.get("timeline_seq", 0)))
            if continued and previous
            else 0
        ),
        # This flag describes only the fixed source snapshot returned by this
        # call. Persisting it is harmless, but it is never trusted for source
        # continuity; the next pass revalidates the prefix digest.
        "source_caught_up": bool(caught_up),
    }
    if backend == BACKEND_CLAUDE:
        cursor["claude_interruption_context"] = normalize_claude_interruption_context(interruption_context, provider_session_id=provider_id)
    if backend == BACKEND_CODEX and codex_phase_context.get("phase") in ("commentary", "final_answer"):
        cursor["codex_last_item_phase"] = codex_phase_context["phase"]
    if source_text_context.get("sha256"):
        cursor["last_item_source_text_sha256"] = source_text_context["sha256"]
    return path, items, cursor, continued


async def sync_provider_history(
    sess: dict[str, Any],
    *,
    limit: int | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Append provider messages added outside this chat since the last sync.

    Hyadex drives the real provider CLI, so anything sent from here is
    already in the provider transcript - but the reverse was not true. A
    conversation continued in the provider's own CLI (or on another machine
    sharing that transcript) grew silently: the next Hyadex turn resumed
    the thread and the model answered with full context the timeline never
    showed, which reads as the assistant knowing things the user never said.
    """

    session_id = str(sess["id"])
    if not session_provider_id(sess):
        return {
            "imported": 0,
            "source_path": None,
            "message": "No provider session ID set.",
        }
    live_session = STORE.sessions.get(session_id) or {}
    previous_cursor = normalized_history_sync_cursor(live_session)
    timeline_stamp = history_file_stamp(events_path(session_id))
    # A durable cursor makes the unchanged fast path survive process restart;
    # all five stat/identity fields must still describe the fully-consumed
    # source before parsing can be skipped.
    stamp = await asyncio.to_thread(provider_history_source_stamp, sess)
    if history_cursor_matches_source_stamp(previous_cursor, stamp):
        return {
            "imported": 0,
            "source_path": stamp[0],
            "message": "Provider transcript unchanged since the last sync.",
        }
    source_path, items, next_cursor, continued = await asyncio.to_thread(
        load_provider_history_with_cursor,
        sess,
        limit,
        previous_cursor,
    )
    if not source_path:
        return {
            "imported": 0,
            "source_path": None,
            "message": "No provider transcript found.",
        }
    if (previous_cursor is not None and next_cursor is not None
            and next_cursor["source_offset"] == previous_cursor["source_offset"]
            and not next_cursor.get("source_caught_up", True)):
        return {"imported": 0, "deferred": True, "reason": "incomplete_source_record"}
    if next_cursor is not None and previous_cursor is not None:
        recovered = await asyncio.to_thread(
            committed_history_sync_checkpoint,
            sess,
            previous_cursor,
        )
        if recovered is not None:
            recovered_cursor, _terminal_seq = recovered
            async with history_commit_guard(sess, previous_cursor, timeline_stamp, stamp, background):
                await persist_history_sync_cursor(
                    sess, recovered_cursor,
                    timeline_seq=int(recovered_cursor.get("timeline_seq") or 0),
                )
            return {
                "imported": 0,
                "source_path": str(source_path),
                "message": "Recovered a committed provider history checkpoint.",
            }
    timeline_latest_seq = await asyncio.to_thread(
        last_event_seq_from_file,
        events_path(session_id),
    )
    bootstrap_boundary = (previous_cursor or {}).get("bootstrap_timeline_seq") if continued else None
    if not continued and next_cursor is not None and not next_cursor.get("source_caught_up", True):
        bootstrap_boundary = timeline_latest_seq
    if (continued and previous_cursor is not None) or (next_cursor is not None and not next_cursor.get("source_caught_up", True)):
        timeline_scan_through_seq = timeline_latest_seq if bootstrap_boundary is None else bootstrap_boundary
        fresh, consumed_timeline_seq = await asyncio.to_thread(
            reconcile_cursor_history_items,
            session_id,
            items,
            timeline_after_seq=int((previous_cursor or {}).get("timeline_seq") or 0) if continued else 0,
            timeline_through_seq=timeline_scan_through_seq,
            **({"bootstrap": True} if bootstrap_boundary is not None else {}),
        )
    else:
        # First sync or invalidated/rotated source: retain the conservative
        # full-tail content alignment and establish a new byte cursor only
        # after that decision is committed.
        fresh = await asyncio.to_thread(
            unsynced_history_items,
            session_id,
            items,
            timeline_through_seq=timeline_latest_seq,
        )
        consumed_timeline_seq = timeline_latest_seq
    if fresh and next_cursor is not None and previous_cursor is None:
        recovered = await asyncio.to_thread(
            committed_history_sync_checkpoint,
            sess,
            previous_cursor,
        )
        if recovered is not None:
            recovered_cursor, _terminal_seq = recovered
            async with history_commit_guard(sess, previous_cursor, timeline_stamp, stamp, background):
                await persist_history_sync_cursor(
                    sess, recovered_cursor,
                    timeline_seq=int(recovered_cursor.get("timeline_seq") or 0),
                )
            return {
                "imported": 0,
                "source_path": str(source_path),
                "message": "Recovered a committed provider history checkpoint.",
            }
    caught_up = True
    checkpoint: dict[str, Any] | None = None
    timeline_seq = timeline_latest_seq
    if next_cursor is not None:
        caught_up = bool(next_cursor.pop("source_caught_up", True))
        if bootstrap_boundary is not None and not caught_up:
            next_cursor["bootstrap_timeline_seq"] = bootstrap_boundary
        next_cursor["timeline_pending_through_seq"] = 0
        next_cursor["timeline_pending_active"] = False
        if not caught_up:
            timeline_seq = consumed_timeline_seq
        next_cursor["timeline_seq"] = timeline_seq
        next_cursor["checkpoint_seq"] = max(
            int(next_cursor.get("checkpoint_seq") or 0),
            timeline_latest_seq,
        )
        checkpoint = history_sync_checkpoint(
            previous_cursor,
            next_cursor,
            caught_up=caught_up,
        )

    if background and fresh:
        try:
            fresh = await prepare_imported_history_items(sess, source_path, fresh, checkpoint)
        except CodexNativeHistoryProofUnavailable:
            return {"imported": 0, "deferred": True, "reason": "native_history_proof_unavailable"}
    async with history_commit_guard(sess, previous_cursor, timeline_stamp, stamp, background):
        result: dict[str, Any]
        if not fresh:
            result = {
                "imported": 0,
                "source_path": str(source_path),
                "message": "Already up to date with the provider transcript.",
            }
        else:
            # The imported batch is fsynced atomically together with its exact next
            # cursor. A cursor moves only after that authoritative commit; if the
            # registry save then fails, retry recovers the embedded checkpoint
            # without relying on ambiguous content alignment.
            try:
                result = await append_imported_history(
                    sess,
                    source_path,
                    fresh,
                    sync_checkpoint=checkpoint,
                    _prepared=background,
                    _expected_timeline_stamp=timeline_stamp,
                )
            except CodexNativeHistoryProofUnavailable:
                # Do not persist next_cursor: these source bytes have not yet been
                # proven against native history. A later explicit sync can retry.
                return {
                    "imported": 0, "source_path": str(source_path), "deferred": True,
                    "reason": "native_history_proof_unavailable",
                    "message": "History reconciliation deferred; no messages were imported.",
                }
        if next_cursor is not None:
            if caught_up:
                timeline_seq = max(
                    timeline_latest_seq,
                    int(result.get("timeline_seq") or 0),
                )
            next_cursor["checkpoint_seq"] = max(
                int(next_cursor.get("checkpoint_seq") or 0),
                timeline_latest_seq,
                int(result.get("timeline_seq") or 0),
            )
            await persist_history_sync_cursor(
                sess,
                next_cursor,
                timeline_seq=timeline_seq,
            )
        result["pending"] = not caught_up
        return result


async def run_provider_history_sync(session_id: str) -> None:
    """Prepare in the background; hold admission only for validated commit."""
    state = history_maintenance_state(session_id)
    key = None
    pending = False
    try:
        async with HISTORY_MAINTENANCE_SLOTS:
            sess = STORE.sessions.get(session_id)
            if (not sess or session_id in DELETING_SESSIONS
                    or session_id in DELETED_SESSION_TOMBSTONES
                    or session_id in RUNS.busy or RUNS.active.get(session_id) is not None
                    or session_id in RUNS.maintenance):
                return
            key = history_maintenance_key(sess)
            state.update(status="running", reason=None)
            await refresh_native_session_title(session_id)
            repair_caches = (CLAUDE_METADATA_REPAIR_CACHE, CODEX_GOAL_HISTORY_REPAIR_CACHE,
                             CODEX_NATIVE_HISTORY_REPAIR_CACHE)
            before_repair = tuple(cache.signature(session_id) for cache in repair_caches)
            result = await sync_provider_history(dict(sess), background=True)
            pending = bool(result.get("pending")) and not result.get("deferred")
            # Forward imports have already proved their own batch. Repair of
            # older displayed rows must not starve the remaining source suffix.
            repaired = None if pending else await asyncio.to_thread(prepare_provider_history_metadata_repair, session_id)
            if before_repair != tuple(cache.signature(session_id) for cache in repair_caches):
                HISTORY_PROJECTION_REVISIONS[session_id] = HISTORY_PROJECTION_REVISIONS.get(session_id, 0) + 1
                await HUB.broadcast(session_id, history_projection_notice(session_id))
            deferred = result.get("deferred") or repaired is False
            state.update(status="deferred" if deferred else "pending" if pending else "idle", deterministic=False,
                         reason=result.get("reason") or ("native_history_proof_unavailable" if repaired is False else None))
            if result.get("imported"):
                logger.info("synced %d provider message(s) added outside session=%s", result["imported"], session_id)
    except asyncio.CancelledError:
        state.update(status="deferred", reason="cancelled")
        raise
    except Exception as exc:
        state.update(status="deferred", reason=type(exc).__name__, deterministic=isinstance(exc, ValueError))
        logger.warning("provider history sync deferred session=%s error=%s", session_id, type(exc).__name__)
    finally:
        if key is not None:
            # A changing input retries on its next open; unchanged failures
            # survive reconnects and process restarts without a scan storm.
            current = STORE.sessions.get(session_id)
            state.update(key=history_maintenance_key(current) if current else key,
                         retry_at=time.time() + (60 if state.get("status") == "deferred" else 1 if pending else 15))
            with suppress(OSError):
                await asyncio.to_thread(save_history_maintenance_state, session_id, dict(state))
        HISTORY_SYNC_SCHEDULED.discard(session_id)
        HISTORY_MAINTENANCE_TASKS.pop(session_id, None)
        if pending and state.get("status") == "pending" and not SERVER.shutting_down:
            def continue_history():
                current = STORE.sessions.get(session_id)
                if current is not None:
                    schedule_provider_history_sync(current)
            asyncio.get_running_loop().call_later(1.1, continue_history)


def schedule_provider_history_sync(sess: dict[str, Any]) -> None:
    session_id = str(sess.get("id") or "")
    if not session_id or session_id in HISTORY_SYNC_SCHEDULED or not session_provider_id(sess):
        return
    if session_id in RUNS.busy or RUNS.active.get(session_id) is not None:
        return
    state = history_maintenance_state(session_id)
    if state.get("key") == history_maintenance_key(sess) and (
            state.get("deterministic") or time.time() < state.get("retry_at", 0)):
        return
    HISTORY_SYNC_SCHEDULED.add(session_id)
    HISTORY_MAINTENANCE_TASKS[session_id] = asyncio.create_task(run_provider_history_sync(session_id))


async def filter_codex_history_for_import(
    session_id: str, provider_id: str, items: list[dict[str, Any]],
    *, source_path: Path | None = None, sync_checkpoint: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Cancellable read-only proof, with no durable effects or event-loop I/O."""
    cancelled = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        filter_native_codex_history_items, session_id, provider_id,
        events_path(session_id), items, cancelled=cancelled.is_set,
        source_path=source_path, root=CODEX_SESSIONS_ROOT, sync_checkpoint=sync_checkpoint,
        parse_item=lambda record: codex_history_event_item(record, expected_session_id=session_id),
    ))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # The worker notices cancellation at its next bounded read. It never
        # writes history; still retrieve its eventual error after detachment.
        def consume_late_result(done):
            if not done.cancelled():
                done.exception()
        worker.add_done_callback(consume_late_result)
        raise


async def prepare_imported_history_items(
    sess: dict[str, Any], source_path: Path, items: list[dict[str, Any]],
    sync_checkpoint: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    session_id = str(sess["id"])
    provider_id = str(session_provider_id(sess) or "")
    backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    if backend == BACKEND_CODEX and sync_checkpoint is not None:
        items = await filter_codex_history_for_import(
            session_id, provider_id, items, source_path=source_path, sync_checkpoint=sync_checkpoint,
        )
    elif backend == BACKEND_CLAUDE and sync_checkpoint is not None:
        def normalize_wake_user(source_event: dict[str, Any]) -> str | None:
            item = claude_history_event_item(source_event, expected_session_id=session_id)
            return item["text"] if item and item.get("kind") == "user" else None

        def normalize_full_wake_user(source_event: dict[str, Any]) -> str | None:
            if source_event.get("type") != "user" or source_event.get("isMeta") is True:
                return None
            return strip_hyadex_generated_user_text(
                message_text(source_event.get("message"), compact=False),
                expected_session_id=session_id, provider_history=True,
            )

        items = await asyncio.to_thread(
            filter_native_claude_mailbox_wake_items, session_id, provider_id, events_path(session_id), items,
            source_path=source_path, root=CLAUDE_PROJECTS_ROOT, sync_checkpoint=sync_checkpoint,
            normalize_user=normalize_wake_user, normalize_full_user=normalize_full_wake_user,
            normalize_assistant=clean_assistant_text,
        )
    return items


async def append_imported_history(
    sess: dict[str, Any],
    source_path: Path,
    items: list[dict[str, Any]],
    *,
    sync_checkpoint: dict[str, Any] | None = None,
    _prepared: bool = False,
    _expected_timeline_stamp: tuple | None = None,
) -> dict[str, Any]:
    session_id = str(sess["id"])
    provider_id = str(session_provider_id(sess) or "")
    backend = str(sess.get("backend") or DEFAULT_BACKEND).lower()
    if not _prepared:
        items = await prepare_imported_history_items(sess, source_path, items, sync_checkpoint)
    metadata_only = bool(items) and all(item.get("kind") == "interruption" or (
        item.get("provider_history_repair") in {"source_proven_import", "source_proven_native_replay", "source_proven_assistant_replay"}
        and item.get("text") == ""
    ) or (
        backend == BACKEND_CODEX and item.get("kind") == "user"
        and item.get("provider_user_authored") is not True
        and item.get("provider_runtime_context") in ("subagent_notification", "turn_aborted", "provider_notice")
        and isinstance(item.get("provider_origin"), dict)
        and item["provider_origin"].get("provider") == "codex"
        and item["provider_origin"].get("kind") == item["provider_runtime_context"]
    ) for item in items)
    items = [item for item in items if item.get("kind") != "interruption" or (
        backend == BACKEND_CLAUDE
        and (normalized_history_provider_origin(item.get("provider_origin")) or {}).get("kind") == "interruption"
    )]
    run_id = f"import_{uuid.uuid4().hex[:12]}"
    message = f"Imported {len(items)} rough messages from {backend} history."
    history_event = {
        "run_id": run_id,
        "backend": backend,
        "provider_session_id": provider_id,
        "source_path": str(source_path),
        "message": message,
        **({"metadata_only": True, "imported": True} if metadata_only else {}),
    }
    if sync_checkpoint is not None:
        history_event["_history_sync_checkpoint"] = sync_checkpoint
    imported_events: list[tuple[str, dict[str, Any]]] = [(
        "history_imported",
        history_event,
    )]
    last_source_timestamp = None
    for item in items:
        origin = normalized_history_provider_origin(item.get("provider_origin")) if backend == BACKEND_CLAUDE else (
            {**item["provider_origin"], "session_id": provider_id}
            if backend == BACKEND_CODEX and isinstance(item.get("provider_origin"), dict) and item["provider_origin"].get("provider") == "codex" else None
        )
        provenance: dict[str, Any] = {"provider_origin": origin} if origin is not None else {}
        runtime_notification = (backend == BACKEND_CODEX and item.get("kind") == "user"
            and item.get("provider_runtime_context") in ("subagent_notification", "turn_aborted", "provider_notice")
            and item.get("provider_user_authored") is not True and origin is not None
            and origin.get("kind") == item["provider_runtime_context"])
        if runtime_notification:
            provenance.update(metadata_only=True, provider_runtime_context=item["provider_runtime_context"])
        if backend == BACKEND_CODEX and item.get("provider_history_repair") == "source_proven_native_replay" and item.get("text") == "":
            provenance.update(metadata_only=True, provider_history_repair="source_proven_native_replay")
        if (backend == BACKEND_CLAUDE and item.get("provider_history_repair") in
                {"source_proven_import", "source_proven_assistant_replay"} and item.get("text") == ""):
            provenance.update(metadata_only=True, provider_history_repair=item["provider_history_repair"])
        source_sha256 = item.get("source_text_sha256")
        if isinstance(source_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
            provenance["source_text_sha256"] = source_sha256
        if backend == BACKEND_CODEX and item.get("provider_user_authored") is True:
            provenance["provider_user_authored"] = True
        if origin is not None and "timestamp" in origin:
            provenance["ts"] = origin["timestamp"]
        if backend == BACKEND_CODEX and item.get("kind") in ("user", "assistant"):
            provenance.update(codex_history_assistant_metadata(
                item if item.get("kind") == "assistant" else {"ts": item.get("ts")},
            ))
        if "ts" in provenance:
            last_source_timestamp = provenance["ts"]
        if item["kind"] == "user":
            imported_events.append(("turn_started", {
                "run_id": run_id,
                "backend": backend,
                "prompt": "" if runtime_notification else item["text"],
                "imported": True,
                "provider_history_sanitized": True,
                **provenance,
            }))
        elif item["kind"] == "assistant":
            imported_events.append(("reasoning_summary" if provenance.get("phase") == "commentary" else "assistant_text", {
                "run_id": run_id,
                "backend": backend,
                "text": item["text"],
                "imported": True,
                **provenance,
            }))
        elif item["kind"] == "interruption":
            imported_events.append(("provider_interruption", {
                "run_id": run_id,
                "backend": backend,
                "imported": True,
                **provenance,
            }))
    imported_events.append(imported_history_terminal_event(
        run_id, backend, metadata_only=metadata_only, source_timestamp=last_source_timestamp,
    ))
    if _prepared:
        async with event_delivery_lock(session_id):
            if history_file_stamp(events_path(session_id)) != _expected_timeline_stamp:
                raise HistoryPreparationStale("timeline changed before history publication")
            committed = await append_durable_event_batch_locked(session_id, imported_events)
    else:
        committed = await append_durable_event_batch(session_id, imported_events)
    if len(committed) != len(imported_events):
        raise RuntimeError("history event batch was not fully persisted")
    if backend == BACKEND_CODEX:
        CODEX_NATIVE_HISTORY_REPAIR_CACHE.forget(session_id)
    return {
        "imported": len(items),
        "source_path": str(source_path),
        "message": message,
        "timeline_seq": int(committed[-1]["seq"]),
    }


async def import_session_history(sess: dict[str, Any], *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
    session_id = sess["id"]
    provider_id = session_provider_id(sess)
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    if retired := retired_session_runtime_message(sess):
        raise HTTPException(410, retired)
    if not provider_id:
        return {"imported": 0, "source_path": None, "message": "No provider session ID set."}
    if not force and any(event.get("type") == "history_imported" for event in read_events(session_id, limit=10000)):
        return {"imported": 0, "source_path": None, "message": "History already imported."}

    previous_cursor = normalized_history_sync_cursor(
        STORE.sessions.get(str(session_id)) or sess
    )
    try:
        source_path, items, next_cursor, continued = await asyncio.to_thread(
            load_provider_history_with_cursor,
            sess,
            limit,
            previous_cursor,
            allow_rebaseline=force,
        )
    except Exception as e:
        logger.warning("history import failed session=%s provider=%s: %s", session_id, provider_id, e)
        message = f"History import failed: {concise_error_message(e)}"
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "message": message,
        })
        return {"imported": 0, "source_path": None, "message": message}

    if not source_path:
        message = f"No local {backend} transcript found for {provider_id}."
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "message": message,
        })
        return {"imported": 0, "source_path": None, "message": message}

    if next_cursor is not None and previous_cursor is not None:
        recovered = await asyncio.to_thread(
            committed_history_sync_checkpoint,
            sess,
            previous_cursor,
        )
        if recovered is not None:
            recovered_cursor, _terminal_seq = recovered
            await persist_history_sync_cursor(
                sess,
                recovered_cursor,
                timeline_seq=int(recovered_cursor.get("timeline_seq") or 0),
            )
            return {
                "imported": 0,
                "source_path": str(source_path),
                "message": "Recovered a committed provider history checkpoint.",
            }

    timeline_latest_seq = await asyncio.to_thread(
        last_event_seq_from_file,
        events_path(str(session_id)),
    )
    if not items:
        message = f"Found {backend} transcript, but no chat messages to import."
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "source_path": str(source_path),
            "message": message,
        })
        result = {
            "imported": 0,
            "source_path": str(source_path),
            "message": message,
        }
        if next_cursor is not None:
            await persist_history_sync_cursor(
                sess,
                next_cursor,
                timeline_seq=timeline_latest_seq,
            )
        return result

    # ``force`` means bypass the one-time import marker, not append the same
    # provider transcript again. Anchor against the durable timeline exactly
    # like automatic catch-up and import only the unseen suffix.
    if continued and previous_cursor is not None:
        timeline_scan_through_seq = timeline_latest_seq
        fresh, consumed_timeline_seq = await asyncio.to_thread(
            reconcile_cursor_history_items,
            session_id,
            items,
            timeline_after_seq=int(previous_cursor.get("timeline_seq") or 0),
            timeline_through_seq=timeline_scan_through_seq,
        )
    else:
        fresh = await asyncio.to_thread(
            unsynced_history_items,
            session_id,
            items,
            timeline_through_seq=timeline_latest_seq,
        )
        consumed_timeline_seq = timeline_latest_seq
    if fresh and next_cursor is not None and previous_cursor is None:
        recovered = await asyncio.to_thread(
            committed_history_sync_checkpoint,
            sess,
            previous_cursor,
        )
        if recovered is not None:
            recovered_cursor, _terminal_seq = recovered
            await persist_history_sync_cursor(
                sess,
                recovered_cursor,
                timeline_seq=int(recovered_cursor.get("timeline_seq") or 0),
            )
            return {
                "imported": 0,
                "source_path": str(source_path),
                "message": "Recovered a committed provider history checkpoint.",
            }
    caught_up = True
    checkpoint: dict[str, Any] | None = None
    timeline_seq = timeline_latest_seq
    if next_cursor is not None:
        caught_up = bool(next_cursor.pop("source_caught_up", True))
        next_cursor["timeline_pending_through_seq"] = 0
        next_cursor["timeline_pending_active"] = False
        if not caught_up:
            timeline_seq = consumed_timeline_seq
        next_cursor["timeline_seq"] = timeline_seq
        next_cursor["checkpoint_seq"] = max(
            int(next_cursor.get("checkpoint_seq") or 0),
            timeline_latest_seq,
        )
        checkpoint = history_sync_checkpoint(
            previous_cursor,
            next_cursor,
            caught_up=caught_up,
        )
    if not fresh:
        result = {
            "imported": 0,
            "source_path": str(source_path),
            "message": "Already up to date with the provider transcript.",
        }
    else:
        try:
            result = await append_imported_history(
                sess,
                source_path,
                fresh,
                sync_checkpoint=checkpoint,
            )
        except CodexNativeHistoryProofUnavailable:
            # Do not persist next_cursor: these source bytes have not yet been
            # proven against native history. A later explicit sync can retry.
            return {
                "imported": 0, "source_path": str(source_path), "deferred": True,
                "reason": "native_history_proof_unavailable",
                "message": "History reconciliation deferred; no messages were imported.",
            }
    if next_cursor is not None:
        if caught_up:
            timeline_seq = max(
                timeline_latest_seq,
                int(result.get("timeline_seq") or 0),
            )
        next_cursor["checkpoint_seq"] = max(
            int(next_cursor.get("checkpoint_seq") or 0),
            timeline_latest_seq,
            int(result.get("timeline_seq") or 0),
        )
        await persist_history_sync_cursor(
            sess,
            next_cursor,
            timeline_seq=timeline_seq,
        )
    return result


async def create_session_with_history(req: CreateSessionRequest) -> dict[str, Any]:
    sess = await STORE.create(req)
    provider_id = session_provider_id(sess)
    should_import = bool(provider_id) if req.import_history is None else req.import_history
    if should_import:
        await import_session_history(sess)
    return {"session": public_session(sess)}


def claude_imported_fork_boundary(
    parent: dict[str, Any],
    provider_id: str,
    events: list[dict[str, Any]],
) -> str:
    """Recover native completion from an import's durable transcript checkpoint.

    Import terminals are bookkeeping, not provider turns. Their saved byte
    prefix must end at an explicit native completion before it can be forked.
    Later source appends are allowed; none are read into this snapshot.
    """
    run_id = str(events[-1].get("run_id") or "")
    batch = [event for event in events if str(event.get("run_id") or "") == run_id]
    marker = next((event for event in batch if event.get("type") == "history_imported"), {})
    checkpoint = marker.get("_history_sync_checkpoint")
    if not isinstance(checkpoint, dict) or checkpoint.get("caught_up") is not True:
        raise ValueError("imported history has no complete native checkpoint")
    cursor = normalized_history_sync_cursor({**parent, "_history_sync_cursor": checkpoint.get("cursor")})
    if cursor is None or cursor["backend"] != BACKEND_CLAUDE or cursor["provider_session_id"] != provider_id:
        raise ValueError("imported history checkpoint belongs to a different provider")
    path = contained_jsonl_path(Path(cursor["source_path"]), CLAUDE_PROJECTS_ROOT)
    if path is None:
        raise ValueError("imported history checkpoint transcript is unavailable")
    remaining = int(cursor["source_offset"])
    digest = hashlib.sha256()
    last_message: dict[str, Any] = {}
    with path.open("rb") as stream:
        original = os.fstat(stream.fileno())
        for record, offset, raw in jsonl_records(stream, end=remaining,
                max_records=MAX_LOCAL_TRANSCRIPT_SCAN_LINES):
            raw.update_hash(digest)
            remaining = int(cursor["source_offset"]) - offset
            if raw.blank:
                continue
            if record is None:
                raise ValueError("imported history checkpoint has a malformed transcript record")
            if record.get("type") in {"user", "assistant"} and not record.get("isSidechain"):
                last_message = record
        current = os.fstat(stream.fileno())
        if (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise ValueError("imported history checkpoint changed during reading")
    if remaining or not hmac.compare_digest(digest.hexdigest(), cursor["source_digest"]):
        raise ValueError("imported history checkpoint no longer matches its native prefix")
    message = last_message.get("message")
    cutoff = str(last_message.get("uuid") or "")
    native_time = parse_job_timestamp(str(last_message.get("timestamp") or ""))
    imported_time = parse_job_timestamp(str(events[-1].get("ts") or ""))
    if (
        last_message.get("type") != "assistant" or not cutoff
        or not isinstance(message, dict) or message.get("stop_reason") != "end_turn"
        or native_time is None or imported_time is None or native_time > imported_time
    ):
        raise ValueError("imported history does not end at a completed native assistant turn")
    visible = [event for event in batch if event.get("type") in {"turn_started", "assistant_text"}]
    native_item = claude_history_event_item(last_message, expected_session_id=str(parent.get("id") or ""))
    if (
        not visible or visible[-1].get("type") != "assistant_text"
        or native_item is None
        or native_item.get("text") != str(visible[-1].get("text") or "")
    ):
        raise ValueError("imported visible history does not match the native completed boundary")
    return cutoff


def claude_completed_fork_boundary(
    parent: dict[str, Any],
    provider_id: str,
    events: list[dict[str, Any]],
) -> str:
    """Resolve the completed reply's exact UUID in the read-only transcript.

    Legacy SDK events lack UUIDs. Accept an exact, unique final-response match
    before the durable terminal timestamp; never take the transcript's tail.
    """
    terminal = events[-1]
    if terminal.get("imported") is True:
        return claude_imported_fork_boundary(parent, provider_id, events)
    run_id = str(terminal.get("run_id") or "")
    completed_provider = str(terminal.get("provider_session_id") or "")
    if completed_provider and completed_provider != provider_id:
        raise HTTPException(status_code=409, detail="The completed Claude turn belongs to a different provider session; the running chat was left unchanged.")
    final_text = clean_assistant_text(str(terminal.get("result_text") or ""))
    final_projection = next((
        event for event in reversed(events[:-1])
        if str(event.get("run_id") or "") == run_id
        and (event.get("type") == "assistant_text" or (
            event.get("type") == "reasoning_summary" and event.get("phase") == "commentary"
        ))
        and str(event.get("text") or "").strip()
    ), {})
    projected_text = clean_assistant_text(str(final_projection.get("text") or ""))
    # A terminal result can be a concatenation of assistant messages. Only its
    # final projected message can identify the inclusive native cutoff.
    expected_text = projected_text if projected_text and final_text.endswith(projected_text) else final_text
    recorded_uuid = str(final_projection.get("provider_message_id") or "") if expected_text == projected_text else ""
    cutoff_ts = str(terminal.get("ts") or "")
    cutoff_time = parse_job_timestamp(cutoff_ts)
    # now_iso() truncates server events to whole seconds; native Claude
    # records retain milliseconds. Only an exact completed-message UUID can
    # disambiguate another turn in that same second. Keep legacy text-only
    # matching and explicitly fractional timestamps on the strict cutoff.
    cutoff_second_is_truncated = bool(recorded_uuid and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cutoff_ts,
    ))
    started = next((
        event for event in reversed(events[:-1])
        if event.get("type") == "turn_started" and str(event.get("run_id") or "") == run_id
    ), {})
    start_time = parse_job_timestamp(str(started.get("ts") or ""))
    cwd = str(parent.get("cwd") or "")
    expected_path = claude_resume_file_for_cwd(provider_id, cwd)
    paths = [expected_path] if expected_path.is_file() else [
        path for path in claude_history_candidates(provider_id)
        if claude_transcript_matches_cwd(path, cwd)
    ]
    matches: set[str] = set()
    for path in paths:
        for record in bounded_jsonl_events(path):
            if record.get("type") != "assistant" or record.get("isSidechain"):
                continue
            record_uuid = str(record.get("uuid") or "")
            if not record_uuid or (recorded_uuid and record_uuid != recorded_uuid):
                continue
            record_time = parse_job_timestamp(str(record.get("timestamp") or ""))
            if cutoff_time is None or record_time is None:
                continue
            if record_time > cutoff_time and not (
                cutoff_second_is_truncated and record_time < cutoff_time + 1
            ):
                continue
            if not recorded_uuid and (start_time is None or record_time < start_time):
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            # A pending tool call is not a completed assistant reply boundary.
            if not isinstance(content, list) or any(
                isinstance(block, dict) and block.get("type") in {"tool_use", "server_tool_use"}
                for block in content
            ):
                continue
            text_blocks = [
                clean_assistant_text(str(block.get("text") or ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if expected_text and expected_text in ["\n\n".join(text_blocks).strip(), *text_blocks[-1:]]:
                matches.add(record_uuid)
    if len(matches) != 1:
        raise HTTPException(status_code=409, detail=(
            "The last completed Claude turn could not be matched to an exact provider "
            "snapshot. The running chat was left unchanged; retry after it finishes."
        ))
    return next(iter(matches))

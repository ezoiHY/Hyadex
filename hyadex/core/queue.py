"""Durable queued turns: enqueue, reorder, steer, Run now and recovery."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import hmac
import json
import logging.handlers
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Iterator
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

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
    CLAUDE_STOP_FENCE_ATTEMPT_TIMEOUT_SECONDS,
    CODEX_SESSIONS_ROOT,
    DEFAULT_BACKEND,
    LEGACY_DELIVERY_IDENTITY_FIELDS,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
    QUEUE_FENCE_LOG_INTERVAL_SECONDS,
    RETIRED_QUEUED_TURN_FIELDS,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
)
from hyadex.core.event_log import append_durable_event_batch_sync, read_events
from hyadex.core.timeline_index import is_native_goal_steer_event
from hyadex.core.publications import (
    ArtifactPublicationError,
    load_publication_events,
    store_publication_events,
    verified_publication_events,
)
from hyadex.core.event_seq import last_event_seq_from_file
from hyadex.core.history_sync import (
    append_durable_event,
    append_durable_event_batch,
    append_durable_event_batch_locked,
)
from hyadex.core.paths import events_path, manifests_dir, now_iso
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.queue_recovery import scan_queued_turns_from_events
from hyadex.core.restart_status import MANAGED_SERVER_RESTART_ACTIVE_DETAIL
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.session_settings import CreateSessionRequest, retired_session_runtime_message
from hyadex.core.lifecycle_admission import server_lifecycle_admission_blocker
from hyadex.core.sessions import (
    append_event,
    ensure_session_not_deleting,
    event_delivery_lock,
    next_event_seq,
    public_session,
    reconcile_event_seq_after_failed_write,
    refresh_native_session_title,
    register_session_task,
    session_lifecycle_lock,
    STORE,
    update_session_event_metadata,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
    EVENTS,
    LEGACY_STEERING_PREFIX,
    QUEUE_CHECKPOINT_DIRTY,
    QUEUE_FENCE_LOGGED_AT,
    QUEUE_RECOVERY_ADMISSION_WAIT_SECONDS,
    QUEUE_RECOVERY_READY,
    RUN_NOW_COMPLETED_RESULTS,
    RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS,
)
from hyadex.core.steering import native_steer_requeue_event_payload, parse_legacy_steering_lineage
from hyadex.core.util import (
    bounded_jsonl_records_range,
    clean_assistant_text,
    contained_jsonl_path,
    ensure_dirs,
    merged_file_ids,
    SkillSelection,
    strip_hyadex_memory_context,
    TransientAdmissionWait,
)
from hyadex.features.files import (
    artifact_entries_digest,
    artifact_publication_lock,
    live_manifest_batch_ready,
    prepare_artifact_records,
    read_artifact_manifest,
    remove_artifact_records_async,
    remove_upload_tree,
    run_event_metadata,
    run_upload_worker,
    split_legacy_attached_files,
    split_legacy_scoped_attachments,
    unlink_manifest_if_unchanged,
    validate_session_file_ids,
)
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
from hyadex.providers.claude.runtime import claude_resume_file_for_cwd, stop_cleanup_in_progress
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
from hyadex.providers.registry import (
    normalize_steering_lineage,
    provider_command_invocation_token,
    queued_turn_uses_retired_routing,
    strip_hyadex_provider_context,
)
from hyadex.providers.state import PROVIDERS
from hyadex.providers.subagents import empty_subagent_stop_result
from hyadex.providers.claude.subagents import (
    build_claude_subagent_snapshot,
    claude_stop_fence_attempt_lock,
    CLAUDE_STOP_FENCE_ATTEMPT_TASKS,
    discard_unused_claude_stop_fence_attempt_lock,
    evict_claude_sdk_chat,
)
from hyadex.core.transcripts import jsonl_records

logger = logging.getLogger("hyadex-server")


class UpdateQueuedTurnRequest(BaseModel):
    prompt: str | None = None
    expected_message_revision: int | None = Field(default=None, ge=0, strict=True)
    file_ids: list[str] | None = None
    client_capabilities: list[str] | None = Field(default=None, max_length=16)
    # Saved @Chat and Team targets were retired; reject rather than drop them.
    chat_references: list[dict[str, Any]] | None = Field(default=None, max_length=0)
    team_references: list[dict[str, Any]] | None = Field(default=None, max_length=0)
    skill_selection: SkillSelection | None = None


class MoveQueuedTurnRequest(BaseModel):
    direction: str
    expected_adjacent_queued_id: str | None = Field(default=None, min_length=1, max_length=128)


class RunQueuedTurnNowRequest(BaseModel):
    accept_deferred_queue_response: bool = False


def request_fields_set(request_model: BaseModel) -> set[str]:
    """Read explicit Pydantic fields without eagerly touching v1 aliases."""

    fields = getattr(request_model, "model_fields_set", None)
    if fields is None:
        fields = getattr(request_model, "__fields_set__", set())
    return set(fields)


async def unqueue_turn(session_id: str, queued_id: str) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")

    removed: dict[str, Any] | None = None
    async with RUNS.queue_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        reject_promoted_queue_mutation(session_id, queued_id)
        queue = RUNS.queued.get(session_id)
        if queue:
            original_items = list(queue)
            removed_index = next(
                (
                    index
                    for index, item in enumerate(original_items)
                    if item.get("queued_id") == queued_id
                ),
                None,
            )
            if removed_index is not None:
                removed = original_items[removed_index]
            kept = deque(
                item
                for index, item in enumerate(original_items)
                if index != removed_index
            )
            if kept:
                RUNS.queued[session_id] = kept
                remaining = len(kept)
            else:
                RUNS.queued.pop(session_id, None)
                remaining = 0
        else:
            remaining = 0
        if removed is None:
            raise HTTPException(status_code=404, detail="queued turn not found")
        try:
            await append_durable_event(session_id, "turn_unqueued", {
                "queued_id": queued_id,
                "backend": removed.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
                "prompt": removed.get("prompt") or "",
                "file_ids": list(removed.get("file_ids") or []),
                "message": "Removed queued message.",
                "remaining": remaining,
                "purpose": removed.get("purpose"),
            })
        except BaseException:
            RUNS.queued[session_id] = deque(original_items)
            raise
    return {
        "ok": True,
        "unqueued": True,
        "queued_id": queued_id,
        "remaining": remaining,
    }


def queue_positions(queue: deque[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"queued_id": str(item.get("queued_id") or ""), "position": idx + 1}
        for idx, item in enumerate(queue)
    ]


def public_queued_turn(
    session_id: str,
    item: dict[str, Any],
    position: int,
    *,
    promoted: bool = False,
) -> dict[str, Any]:
    display_file_ids = item.get("display_file_ids")
    display_prompt = item.get("display_prompt")
    purpose = item.get("purpose")
    secure_peer_barrier = purpose == "secure_peer_handoff_delivery"
    public_display_prompt: str | None
    if secure_peer_barrier:
        # Project only the immutable FIFO barrier. The remote envelope,
        # provider wrapper, peer identity, body, and attachment identifiers
        # remain private to the secure-peer lifecycle surface.
        public_prompt = "Incoming secure-peer delivery"
        public_display_prompt = public_prompt
        public_file_ids = []
    else:
        public_prompt = str(
            display_prompt
            if display_prompt is not None
            else item.get("steering_prompt") or item.get("prompt") or ""
        )
        public_display_prompt = item.get("display_prompt")
        public_file_ids = list(
            display_file_ids
            if display_file_ids is not None
            else item.get("file_ids") or []
        )
    delivery_uncertain = item.get("_native_delivery_fenced") is True
    paused = item.get("_paused_after_stop") is True or delivery_uncertain
    return {
        "queued_id": str(item.get("queued_id") or ""),
        "session_id": session_id,
        "prompt": public_prompt,
        "file_ids": public_file_ids,
        "backend": None if secure_peer_barrier else item.get("backend"),
        "model": None if secure_peer_barrier else item.get("model"),
        "effort": None if secure_peer_barrier else item.get("effort"),
        "skill_selection": (
            None if secure_peer_barrier else item.get("skill_selection")
        ),
        "display_prompt": public_display_prompt,
        **({key: item[key] for key in ("shared_chat_id", "shared_chat_request_id", "author_label") if key in item}
           if purpose is None and item.get("shared_chat_id") else {}),
        "purpose": purpose,
        "team_references": [] if secure_peer_barrier else list(item.get("team_references") or []),
        "created_at": item.get("created_at"),
        "position": position,
        "promoted": promoted,
        "paused": paused,
        "pause_reason": (
            "delivery_uncertain"
            if delivery_uncertain
            else "stopped" if paused else None
        ),
    }


async def queued_turns_snapshot(session_id: str) -> list[dict[str, Any]]:
    await wait_for_queue_recovery_admission(session_id)
    async with RUNS.queue_lock:
        queue = list(RUNS.queued.get(session_id) or [])
        run_now = RUNS.run_now.get(session_id)
    items: list[tuple[dict[str, Any], bool]] = []
    if run_now is not None:
        items.append((run_now, True))
    items.extend((item, False) for item in queue)
    return [
        public_queued_turn(
            session_id,
            item,
            idx + 1,
            promoted=promoted,
        )
        for idx, (item, promoted) in enumerate(items)
        if str(item.get("queued_id") or "").strip()
    ]


def reject_promoted_queue_mutation(session_id: str, queued_id: str) -> None:
    """Return a truthful stable contract for a row already handed to a turn."""

    promoted = RUNS.run_now.get(session_id)
    if promoted is None or str(promoted.get("queued_id") or "") != queued_id:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "queued_turn_already_promoted",
            "message": (
                "This queued turn is already being promoted to the provider "
                "and can no longer be edited, moved, or removed."
            ),
            "action": "Wait for the promotion to finish, then refresh the queue.",
            "retryable": True,
            "queued_id": queued_id,
        },
    )


async def update_queued_turn(session_id: str, queued_id: str, req: UpdateQueuedTurnRequest) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    validated_file_ids = (
        validate_session_file_ids(session_id, req.file_ids)
        if req.file_ids is not None
        else None
    )
    update_fields_set = request_fields_set(req)

    updated: dict[str, Any] | None = None
    async with RUNS.queue_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        reject_promoted_queue_mutation(session_id, queued_id)
        queue = RUNS.queued.get(session_id)
        if queue:
            for idx, item in enumerate(queue):
                if item.get("queued_id") == queued_id:
                    if queued_turn_uses_retired_routing(item):
                        raise HTTPException(status_code=400, detail="saved routing target is no longer supported")
                    original = {
                        **item,
                        "file_ids": list(item.get("file_ids") or []),
                    }
                    candidate = {
                        **original,
                        "file_ids": list(original.get("file_ids") or []),
                    }
                    if req.prompt is not None:
                        # Validate emptiness without rewriting the text; the
                        # user's exact editor value is what the queue shows.
                        prompt = req.prompt
                        if not prompt.strip():
                            raise HTTPException(status_code=400, detail="prompt is empty")
                        candidate["prompt"] = prompt
                        if "skill_selection" not in update_fields_set:
                            old_token = provider_command_invocation_token(
                                original.get("prompt")
                            )
                            new_token = provider_command_invocation_token(prompt)
                            if old_token is None or new_token != old_token:
                                candidate["skill_selection"] = None
                        # A steered/requeued turn can carry separate raw,
                        # display, and lineage copies of the user's latest
                        # message.  Editing only ``prompt`` makes the PATCH
                        # succeed while queue snapshots continue projecting
                        # the stale display value (and provider launch can
                        # still prefer the stale final lineage entry).
                        if candidate.get("display_prompt") is not None:
                            candidate["display_prompt"] = prompt
                        if candidate.get("steering_prompt") is not None:
                            candidate["steering_prompt"] = prompt
                        steering_lineage = normalize_steering_lineage(
                            candidate.get("steering_lineage")
                        )
                        if steering_lineage:
                            steering_lineage[-1] = {
                                **steering_lineage[-1],
                                "prompt": prompt,
                            }
                            candidate["steering_lineage"] = steering_lineage
                    if "skill_selection" in update_fields_set:
                        candidate["skill_selection"] = (
                            req.skill_selection.model_dump()
                            if req.skill_selection is not None
                            else None
                        )
                    if req.file_ids is not None:
                        candidate["file_ids"] = list(validated_file_ids or [])
                    if req.client_capabilities is not None:
                        candidate["client_capabilities"] = list(req.client_capabilities)
                    item.clear()
                    item.update(candidate)
                    updated = dict(item)
                    updated["position"] = idx + 1
                    break
        if updated is None:
            raise HTTPException(status_code=404, detail="queued turn not found")
        try:
            await append_durable_event(session_id, "turn_queue_updated", {
                "queued_id": queued_id,
                "backend": updated.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
                "prompt": updated.get("prompt") or "",
                "request_prompt": updated.get("prompt") or "",
                "display_prompt": updated.get("display_prompt"),
                "file_ids": list(updated.get("file_ids") or []),
                "steering_lineage": normalize_steering_lineage(
                    updated.get("steering_lineage")
                ),
                "client_capabilities": list(updated.get("client_capabilities") or []),
                "skill_selection": updated.get("skill_selection"),
                "position": updated.get("position"),
            })
        except BaseException:
            item.clear()
            item.update(original)
            raise
    return {"ok": True, "queued_id": queued_id, "item": updated}


async def move_queued_turn(session_id: str, queued_id: str, req: MoveQueuedTurnRequest) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    direction = req.direction.strip().lower()
    if direction not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="direction must be up or down")

    moved: dict[str, Any] | None = None
    positions: list[dict[str, Any]] = []
    async with RUNS.queue_lock:
        lifecycle_blocker = server_lifecycle_admission_blocker()
        if lifecycle_blocker:
            raise HTTPException(status_code=503, detail=lifecycle_blocker)
        reject_promoted_queue_mutation(session_id, queued_id)
        queue = RUNS.queued.get(session_id)
        if queue:
            original_items = list(queue)
            items = list(original_items)
            idx = next((i for i, item in enumerate(items) if item.get("queued_id") == queued_id), None)
            if idx is not None:
                new_idx = idx - 1 if direction == "up" else idx + 1
                new_idx = max(0, min(len(items) - 1, new_idx))
                expected_adjacent = req.expected_adjacent_queued_id
                adjacent_id = items[new_idx].get("queued_id") if new_idx != idx else None
                if expected_adjacent is not None and adjacent_id != expected_adjacent:
                    raise HTTPException(status_code=409, detail="Queue changed; refresh before moving this message")
                if new_idx != idx:
                    reject_promoted_queue_mutation(session_id, str(adjacent_id or ""))
                    # An explicit, identity-bound user reorder can change FIFO
                    # priority, but never a scheduled run's content or status.
                    # Legacy clients keep the old fence until they opt into the
                    # advertised exact-reorder contract.
                    if expected_adjacent is None and any(
                        item.get("purpose") == "scheduled_job"
                        for item in (items[idx], items[new_idx])
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail="Update the app to arrange queued deliveries safely",
                        )
                    items[idx], items[new_idx] = items[new_idx], items[idx]
                    RUNS.queued[session_id] = deque(items)
                    moved = dict(items[new_idx])
                else:
                    moved = dict(items[idx])
                positions = queue_positions(RUNS.queued[session_id])
        if moved is None:
            raise HTTPException(status_code=404, detail="queued turn not found")
        try:
            await append_durable_event(session_id, "turn_queue_reordered", {
                "queued_id": queued_id,
                "direction": direction,
                "positions": positions,
            })
        except BaseException:
            RUNS.queued[session_id] = deque(original_items)
            raise
    return {"ok": True, "queued_id": queued_id, "positions": positions}


class NonNativeForceSendRequiresLifecycleLock(Exception):
    """Internal retry signal emitted before a queued turn is mutated."""


def codex_goal_followup_requires_native(
    session: dict[str, Any], active: dict[str, Any], current: dict[str, Any],
) -> bool:
    """A goal follow-up must never fall through the explicit Stop lifecycle."""
    goal = session.get("codex_goal")
    return bool(
        str(session.get("backend") or DEFAULT_BACKEND) == BACKEND_CODEX
        # This only forbids Stop fallback; it does not authorize steering.
        # A native goal can outlive its local owner or still be pre-binding.
        and (
            (isinstance(goal, dict) and goal.get("status") == "active")
            or active.get("codex_native_operation_kind") == "goal_resume"
            or current.get("purpose") == "codex_goal_resume"
        )
    )


async def requeue_turn_front(session_id: str, item: dict[str, Any]) -> None:
    async with RUNS.queue_lock:
        queue = RUNS.queued.setdefault(session_id, deque())
        queue.appendleft(item)


async def requeue_turn_in_place(
    session_id: str,
    item: dict[str, Any],
    *,
    predecessor_id: str | None,
    successor_id: str | None,
) -> None:
    """Return a row the scheduler took from behind held rows to its place.

    Its durable position never changed, so a deferred start must not move it
    ahead of the held rows: before its old successor, else after its old
    predecessor, else at the front.
    """
    async with RUNS.queue_lock:
        queue = RUNS.queued.setdefault(session_id, deque())
        ids = [str(candidate.get("queued_id") or "") for candidate in queue]
        if successor_id and successor_id in ids:
            index = ids.index(successor_id)
        elif predecessor_id and predecessor_id in ids:
            index = ids.index(predecessor_id) + 1
        else:
            index = 0
        queue.insert(index, item)


async def pause_queued_turns_after_explicit_stop(session_id: str) -> int:
    """Durably hold queued successors after the user explicitly stops.

    Stopping a parent turn must not silently delete its queued user messages,
    but neither should a restart reinterpret those messages as permission to
    run them. A later explicit Run Now operation clears the per-item hold.
    """

    # Match Force Send completion's lock order. This makes pausing the promoted
    # item and invalidating its short-lived idempotency result one transition.
    async with RUNS.run_now_request_lock:
        async with RUNS.queue_lock:
            items = list(RUNS.queued.get(session_id) or ())
            run_now = RUNS.run_now.get(session_id)
            if run_now is not None:
                items.insert(0, run_now)
            queued_ids = [
                str(item.get("queued_id") or "").strip()
                for item in items
                if str(item.get("queued_id") or "").strip()
            ]
            if not queued_ids:
                return 0
            # Persist before changing the in-memory scheduling state. If durable
            # storage fails, mark the entries provisional so restart admission
            # remains fail-closed, while Stop can still terminate the parent.
            durable = True
            try:
                await append_durable_event(session_id, "turn_queue_paused", {
                    "queued_ids": queued_ids,
                    "message": "Queued messages were kept after the parent turn stopped.",
                })
            except Exception as exc:
                durable = False
                logger.error(
                    "could not persist stopped queue hold session=%s error=%s",
                    session_id,
                    concise_error_message(exc),
                )
            for item in items:
                if str(item.get("queued_id") or "").strip() in queued_ids:
                    item["_paused_after_stop"] = True
                    if not durable:
                        item["_durable"] = False
            if run_now is not None and str(run_now.get("queued_id") or "").strip() in queued_ids:
                # Stop has withdrawn this promotion before provider admission.
                # Return it to the held queue in the same transition: the Stop
                # fence may prevent the scheduler from doing so for some time.
                RUNS.run_now.pop(session_id, None)
                queue = RUNS.queued.setdefault(session_id, deque())
                if not any(
                    str(candidate.get("queued_id") or "") == str(run_now.get("queued_id") or "")
                    for candidate in queue
                ):
                    queue.appendleft(run_now)
            for queued_id in queued_ids:
                RUN_NOW_COMPLETED_RESULTS.pop((session_id, queued_id), None)
    return len(queued_ids)


def legacy_delivery_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Copy the identity that keeps an old internal delivery row hidden."""

    return {
        key: row[key]
        for key in LEGACY_DELIVERY_IDENTITY_FIELDS
        if row.get(key)
    }


def queued_turn_unqueued_payload(
    session_id: str,
    item: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    return {
        "queued_id": item.get("queued_id"),
        "backend": (
            item.get("backend")
            or (STORE.sessions.get(session_id) or {}).get("backend")
            or DEFAULT_BACKEND
        ),
        "prompt": item.get("prompt") or "",
        "file_ids": list(item.get("file_ids") or []),
        "purpose": item.get("purpose"),
        **legacy_delivery_identity(item),
        "message": message,
    }


async def terminally_discard_queued_turn(
    session_id: str,
    item: dict[str, Any],
    reason: str,
) -> None:
    """Finish a queue item that can never run without recreating chat state."""
    logger.info(
        "discarding queued turn session=%s queued_id=%s reason=%s",
        session_id,
        item.get("queued_id"),
        reason,
    )
    session_can_persist = (
        session_id in STORE.sessions
        and session_id not in DELETING_SESSIONS
        and session_id not in DELETED_SESSION_TOMBSTONES
    )
    if session_can_persist:
        # The reducer removes queue entries only from durable transition
        # events. Persist the terminal pop so a restart cannot resurrect it.
        try:
            await append_durable_event(
                session_id,
                "turn_unqueued",
                queued_turn_unqueued_payload(
                    session_id,
                    item,
                    f"Queued turn discarded: {reason}",
                ),
            )
        except BaseException:
            await requeue_turn_front(session_id, item)
            raise
        with suppress(Exception):
            await append_event(session_id, "error", {
                "queued_id": item.get("queued_id"),
                "message": f"queued turn discarded: {reason}",
            })
    promotion_owner = RUNS.queue_start_tasks.get(session_id)
    current_task = asyncio.current_task()
    if current_task is not None and promotion_owner is current_task:
        # The current promotion task cannot start its successor until its
        # QUEUE_START_TASK ownership is released. Remember that this exact
        # popped row ended terminally; start_next_queued_turn schedules the
        # remaining head after this task reaches its done boundary.
        setattr(current_task, "_hyadex_continue_queue_after_discard", True)


def should_schedule_queue_after_finish(session_id: str, stopped: bool) -> bool:
    # Always schedule one post-release reconciliation. A live steering owner
    # remains fenced in start_next_queued_turn, while an ownerless flag is
    # repaired there immediately. QUEUE_START_TASKS serializes the normal
    # waiter and this terminal path, so neither can pop a second row. Explicit
    # Stop holds remain fail-closed on the queue item itself.
    return True


def rebuild_queued_turns_from_events() -> int:
    recovered = scan_queued_turns_from_events()
    for session_id, items in recovered.items():
        RUNS.queued[session_id] = deque(items)
    return sum(len(items) for items in recovered.values())


def queued_turn_run_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        **({key: item[key] for key in ("shared_chat_id", "shared_chat_request_id", "author_label") if key in item}
           if item.get("purpose") is None and item.get("shared_chat_id") else {}),
        "purpose": item.get("purpose"),
        "job_id": item.get("job_id"),
        "job_title": item.get("job_title"),
        "steer_interrupted_run_id": item.get("steer_interrupted_run_id"),
    }
    return {key: value for key, value in metadata.items() if value is not None}



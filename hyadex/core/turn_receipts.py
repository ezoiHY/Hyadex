"""Turn receipts, durable turn starts and run traces."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from hyadex.core.config import DEFAULT_BACKEND
from hyadex.core.event_log import client_safe_event
from hyadex.core.event_seq import timeline_index_session_lock
from hyadex.core.paths import events_path
from hyadex.core.queue_admission import wait_for_queue_recovery_admission
from hyadex.core.turn_start import _start_turn_locked, reconcile_idle_queue_session
from hyadex.core.runs import RUNS
from hyadex.core.session_files import event_files_belong_to_session
from hyadex.core.sessions import (
    ensure_session_not_deleting,
    public_session,
    register_session_task,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    RUN_TRACE_EVENT_TYPES,
    TIMELINE_INDEX_CACHE_LOCK,
    TURN_RECEIPT_TASKS,
)
from hyadex.core.timeline_index import (
    _build_timeline_index_locked,
    evict_timeline_index_cache_locked,
    fork_internal_run_ids,
    is_fork_internal_event,
    scheduled_job_occurrence_key,
    timeline_index_cached_entry,
)
from hyadex.features.history import history_file_stamp
from hyadex.providers.claude.runtime import TurnRequest


def read_indexed_run_trace(
    session_id: str,
    run_id: str,
    *,
    anchor_seq: int | None = None,
    after_seq: int = 0,
    limit: int = 160,
) -> dict[str, Any]:
    """Read trace detail only within byte bounds recorded by the main index."""
    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        raise KeyError("run trace not found")
    normalized_after = max(0, int(after_seq or 0))
    bounded_limit = max(1, min(int(limit or 160), 1_000))
    lock = timeline_index_session_lock(session_id)
    with lock:
        _build_timeline_index_locked(session_id)
        timeline_cache = timeline_index_cached_entry(session_id, touch=True) or {}
        run_records = timeline_cache.get("run_history_records") or {}
        candidate_keys = list(
            (timeline_cache.get("run_history_keys_by_run_id") or {}).get(
                clean_run_id
            )
            or [f"run:{clean_run_id}"]
        )
        candidates = [
            run_records[key]
            for key in candidate_keys
            if isinstance(run_records.get(key), dict)
        ]
        normalized_anchor = max(0, int(anchor_seq or 0))
        if normalized_anchor > 0:
            record = next(
                (
                    candidate
                    for candidate in sorted(
                        candidates,
                        key=lambda item: (
                            int(item.get("start_seq") or 0),
                            int(item.get("end_seq") or 0),
                        ),
                        reverse=True,
                    )
                    if int(candidate.get("start_seq") or 0)
                    <= normalized_anchor
                    <= int(candidate.get("end_seq") or 0)
                ),
                None,
            )
        else:
            record = candidates[-1] if candidates else None
        if not isinstance(record, dict):
            raise KeyError("run trace not found")
        bounds = {
            "start_offset": max(0, int(record.get("start_offset") or 0)),
            "end_offset": max(0, int(record.get("end_offset") or 0)),
            "end_seq": max(0, int(record.get("end_seq") or 0)),
            "occurrence_key": str(record.get("occurrence_key") or "").strip(),
        }
        fork_internal_run_ids = set(
            timeline_cache.get("fork_internal_run_ids") or ()
        )
    with TIMELINE_INDEX_CACHE_LOCK:
        evict_timeline_index_cache_locked()

    path = events_path(session_id)
    if not path.exists():
        raise KeyError("run trace not found")
    selected: list[dict[str, Any]] = []
    has_more = False
    with path.open("rb") as source:
        source.seek(bounds["start_offset"])
        while source.tell() < bounds["end_offset"]:
            line_start = source.tell()
            raw_line = source.readline()
            if not raw_line or line_start >= bounds["end_offset"]:
                break
            if not raw_line.endswith(b"\n"):
                break
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq") or 0)
            if (
                seq <= normalized_after
                or str(event.get("run_id") or "").strip() != clean_run_id
                or (
                    bounds["occurrence_key"]
                    and scheduled_job_occurrence_key(event)
                    != bounds["occurrence_key"]
                )
                or str(event.get("type") or "") not in RUN_TRACE_EVENT_TYPES
                or is_fork_internal_event(event, fork_internal_run_ids)
                or not event_files_belong_to_session(event, session_id)
            ):
                continue
            if len(selected) >= bounded_limit:
                has_more = True
                break
            selected.append(client_safe_event(event))

    next_after = (
        int(selected[-1].get("seq") or normalized_after)
        if has_more and selected
        else max(normalized_after, bounds["end_seq"])
    )
    return {
        "events": selected,
        "has_more": has_more,
        "next_after": next_after,
    }


async def start_turn(
    session_id: str,
    req: TurnRequest,
    *,
    queue_if_busy: bool = True,
    queued_id: str | None = None,
    display_file_ids: list[str] | None = None,
    steering_lineage: list[dict[str, Any]] | None = None,
    provider_context_mode: Literal["chat", "standalone"] = "chat",
    scheduled_job_revision: str | None = None,
    scheduled_job_manual_run: bool = False,
) -> dict[str, Any]:
    await wait_for_queue_recovery_admission(session_id)
    if queue_if_busy and provider_context_mode == "chat":
        await reconcile_idle_queue_session(
            session_id,
            schedule=True,
            reason="turn_admission",
        )
    # Capture the chat backend before this request can wait on lifecycle work.
    # If a concurrent backend PATCH wins the lock, the request must be retried
    # instead of applying a now-stale per-turn backend after the PATCH.
    admission_backend: str | None = None
    if provider_context_mode == "chat":
        admission_session = STORE.sessions.get(session_id)
        if admission_session is not None:
            admission_backend = str(
                admission_session.get("backend") or DEFAULT_BACKEND
            ).strip().lower()
    # Register the request task before BUSY_SESSIONS is reserved. Stop can then
    # distinguish a legitimate slow launch from an orphaned reservation and
    # leave a deferred stop marker for the runner to reconcile when it binds.
    request_task = asyncio.current_task()
    if request_task is not None:
        register_session_task(RUNS.turn_tasks, session_id, request_task)
    try:
        async with session_lifecycle_lock(session_id):
            ensure_session_not_deleting(session_id)
            return await _start_turn_locked(
                session_id,
                req,
                queue_if_busy=queue_if_busy,
                queued_id=queued_id,
                display_file_ids=display_file_ids,
                steering_lineage=steering_lineage,
                provider_context_mode=provider_context_mode,
                admission_backend=admission_backend,
                scheduled_job_revision=scheduled_job_revision,
                scheduled_job_manual_run=scheduled_job_manual_run,
            )
    finally:
        if request_task is not None:
            tasks = RUNS.turn_tasks.get(session_id)
            if tasks is not None:
                tasks.discard(request_task)
                if not tasks:
                    RUNS.turn_tasks.pop(session_id, None)


def turn_receipt_path(session_id: str, request_id: str) -> Path:
    return events_path(session_id).parent / "turn-receipts" / f"{request_id}.json"


def write_turn_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_turn_receipt(session_id: str, value: dict) -> dict | None:
    path = events_path(session_id)
    stamp = history_file_stamp(path)
    before = value.get("journal")
    offset = before[2] if before else 0
    if stamp is None:
        if before is not None:
            raise ValueError("send receipt journal disappeared")
        return None
    if before and (stamp[:2] != tuple(before[:2]) or stamp[2] < offset):
        raise ValueError("send receipt journal was replaced")
    # An ambiguous old receipt must not cause an unbounded scan or a resend.
    if stamp[2] - offset > 32 * 1024 * 1024:
        raise ValueError("send receipt recovery requires a larger journal scan")
    with path.open("rb") as stream:
        stream.seek(offset)
        while stream.tell() < stamp[2]:
            line = stream.readline(stamp[2] - stream.tell())
            if not line.endswith(b"\n"):
                raise ValueError("send receipt journal has an incomplete record")
            if b'"client_request_id"' not in line:
                continue
            event = json.loads(line)
            if event.get("client_request_id") != value["request_id"]:
                continue
            if event.get("type") == "turn_started":
                return {"run_id": event["run_id"], "queued": False}
            if event.get("type") == "turn_queued":
                return {"queued_id": event["queued_id"], "queued": True,
                        "position": event.get("position", 1)}
    return None


async def execute_receipted_turn(session_id: str, req: TurnRequest, fingerprint: str) -> dict:
    path = turn_receipt_path(session_id, req.client_request_id)
    try:
        value = await asyncio.to_thread(lambda: json.loads(path.read_text()))
    except FileNotFoundError:
        value = {"request_id": req.client_request_id, "fingerprint": fingerprint,
                 "journal": history_file_stamp(events_path(session_id)), "status": "pending"}
        await asyncio.to_thread(write_turn_receipt, path, value)
    else:
        if value.get("fingerprint") != fingerprint:
            raise HTTPException(status_code=409, detail="request ID was already used for a different message")
        if value.get("status") == "accepted":
            return {**value["response"], "session": public_session(STORE.sessions[session_id])}
        try:
            recovered = await asyncio.to_thread(recover_turn_receipt, session_id, value)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail="message acceptance is uncertain; retain this request ID") from exc
        if recovered is not None:
            recovered["session"] = public_session(STORE.sessions[session_id])
            value.update(status="accepted", response=recovered)
            await asyncio.to_thread(write_turn_receipt, path, value)
            return recovered
    response = await post_turn_without_receipt(session_id, req)
    value.update(status="accepted", response=response)
    await asyncio.to_thread(write_turn_receipt, path, value)
    return response


async def post_turn_with_receipt(session_id: str, req: TurnRequest) -> Any:
    if session_id not in STORE.sessions or session_id in DELETED_SESSION_TOMBSTONES:
        raise HTTPException(status_code=404, detail="session not found")
    fingerprint = hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
    key = (session_id, req.client_request_id)
    existing = TURN_RECEIPT_TASKS.get(key)
    if existing is not None and existing[0] != fingerprint:
        raise HTTPException(status_code=409, detail="request ID was already used for a different message")
    if existing is None:
        task = asyncio.create_task(execute_receipted_turn(session_id, req, fingerprint))
        TURN_RECEIPT_TASKS[key] = (fingerprint, task)
        def completed(done):
            TURN_RECEIPT_TASKS.pop(key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
    else:
        task = existing[1]
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.TimeoutError:
        return JSONResponse(status_code=202, content={"request_id": req.client_request_id,
                            "acceptance": "pending", "retry_same_request": True})


async def post_turn_without_receipt(session_id: str, req: TurnRequest) -> dict[str, Any]:
    return await start_turn(session_id, req)

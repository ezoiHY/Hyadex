"""Rebuilding durable queue rows from chat events and queue checkpoints."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
from typing import Any, Iterator
import uuid

from hyadex.core.config import RETIRED_QUEUED_TURN_FIELDS
from hyadex.core.event_log import is_native_goal_steer_event
from hyadex.core.paths import events_path, now_iso
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import STORE
from hyadex.core.state import DELETING_SESSIONS, QUEUE_CHECKPOINT_DIRTY
from hyadex.core.steering import parse_legacy_steering_lineage
from hyadex.providers.registry import normalize_steering_lineage

logger = logging.getLogger("hyadex-server")


async def restore_missing_durable_force_send_row(
    session_id: str,
    queued_id: str,
) -> bool:
    """Restore one detached durable queue row after its volatile owner dies.

    The event reducer is the authority at this boundary. In particular, a
    native delivery fence reconstructs a paused row, while a committed
    ``turn_started`` removes it entirely. That distinction prevents a
    liveness repair from replaying a prompt the provider may have accepted.
    """

    session = STORE.sessions.get(session_id)
    if session is None:
        return False
    try:
        recovered = await asyncio.to_thread(
            scan_queued_turns_from_events,
            [(session_id, dict(session))],
        )
    except Exception as exc:
        logger.error(
            "could not restore stale Force Send row session=%s queued_id=%s "
            "error=%s",
            session_id,
            queued_id,
            concise_error_message(exc),
        )
        return False
    durable_items = list(recovered.get(session_id) or ())
    durable_index = next(
        (
            index
            for index, item in enumerate(durable_items)
            if str(item.get("queued_id") or "") == queued_id
        ),
        None,
    )
    if durable_index is None:
        return False
    selected = durable_items[durable_index]
    durable_ids = [
        str(item.get("queued_id") or "")
        for item in durable_items
    ]
    async with RUNS.queue_lock:
        promoted = RUNS.run_now.get(session_id)
        if (
            promoted is not None
            and str(promoted.get("queued_id") or "") == queued_id
        ):
            return False
        items = list(RUNS.queued.get(session_id) or ())
        if any(
            str(item.get("queued_id") or "") == queued_id
            for item in items
        ):
            return False

        current_positions = {
            str(item.get("queued_id") or ""): index
            for index, item in enumerate(items)
        }
        successor_index = next(
            (
                current_positions[candidate_id]
                for candidate_id in durable_ids[durable_index + 1:]
                if candidate_id in current_positions
            ),
            None,
        )
        predecessor_index = next(
            (
                current_positions[candidate_id]
                for candidate_id in reversed(durable_ids[:durable_index])
                if candidate_id in current_positions
            ),
            None,
        )
        if successor_index is not None:
            insert_at = successor_index
        elif predecessor_index is not None:
            insert_at = predecessor_index + 1
        else:
            insert_at = min(durable_index, len(items))
        items.insert(insert_at, selected)
        RUNS.queued[session_id] = deque(items)
    return True


def queued_turn_from_event(event: dict[str, Any], sess: dict[str, Any], position: int) -> dict[str, Any]:
    request_prompt = event.get("request_prompt")
    steering_lineage = normalize_steering_lineage(event.get("steering_lineage"))
    if (
        event.get("type") == "turn_queue_run_now"
        and event.get("replays_interrupted_message")
        and not steering_lineage
    ):
        # Legacy run-now events persisted a generated replay envelope in
        # request_prompt. Recover its immutable messages when possible.
        steering_lineage = parse_legacy_steering_lineage(
            str(request_prompt or ""),
            list(event.get("file_ids") or []),
        )
        request_prompt = (
            steering_lineage[-1]["prompt"]
            if steering_lineage
            else event.get("prompt")
        )
    return {
        # Older servers persisted routing fields that no longer exist. Keep
        # them verbatim so promotion can discard the row instead of running
        # it with only part of its routing.
        **{
            key: event[key]
            for key in RETIRED_QUEUED_TURN_FIELDS
            if event.get(key)
        },
        "queued_id": str(event.get("queued_id") or ""),
        "prompt": request_prompt if request_prompt is not None else event.get("prompt") or "",
        "file_ids": list(event.get("file_ids") or []),
        "display_file_ids": (
            list(event.get("display_file_ids") or [])
            if event.get("display_file_ids") is not None
            else None
        ),
        "backend": event.get("backend") or sess.get("backend"),
        "model": event.get("model") if event.get("model") is not None else sess.get("model"),
        "effort": event.get("effort") if event.get("effort") is not None else sess.get("effort"),
        "skill_selection": event.get("skill_selection"),
        "display_prompt": event.get("display_prompt"),
        "shared_chat_id": event.get("shared_chat_id"),
        "shared_chat_request_id": event.get("shared_chat_request_id"),
        "author_label": event.get("author_label"),
        "purpose": event.get("purpose"),
        "steer_interrupted_run_id": event.get("interrupted_run_id") or event.get("steer_interrupted_run_id"),
        "replays_interrupted_message": bool(event.get("replays_interrupted_message")),
        "steering_lineage": steering_lineage,
        "client_capabilities": list(event.get("client_capabilities") or []),
        "created_at": event.get("ts") or now_iso(),
        "position": int(event.get("position") or position),
        "_durable": True,
        "_paused_after_stop": False,
    }


def queued_event_lines(path: Path, *, scan: dict[str, Any] | None = None) -> Iterator[bytes]:
    with path.open("rb") as source:
        if scan is not None:
            source.seek(scan["offset"])
        while True:
            remaining = scan["end"] - source.tell() if scan is not None else -1
            if remaining == 0:
                break
            raw_line = source.readline(remaining)
            if not raw_line:
                break
            if scan is not None:
                if not raw_line.endswith(b"\n"):
                    break
                scan["offset"] = source.tell()
            if b'"queued_id"' in raw_line or b'"turn_queue_paused"' in raw_line:
                yield raw_line


def queue_checkpoint_anchor(path: Path, offset: int) -> str:
    with path.open("rb") as stream:
        stream.seek(max(0, offset - 4096))
        return hashlib.sha256(stream.read(min(offset, 4096))).hexdigest()


def queue_checkpoint_seed(path: Path, stamp, *, enabled: bool) -> tuple[dict, list]:
    if enabled:
        try:
            checkpoint_path = path.with_name("queue-recovery.json")
            if checkpoint_path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("oversized queue checkpoint")
            value = json.loads(checkpoint_path.read_text())
            offset = value["offset"]
            rows = value["pending"]
            if (value["version"] == 1 and value["dev"] == stamp.st_dev
                    and value["ino"] == stamp.st_ino and isinstance(offset, int)
                    and 0 <= offset <= stamp.st_size
                    and (offset != stamp.st_size or value["mtime_ns"] == stamp.st_mtime_ns)
                    and value["anchor"] == queue_checkpoint_anchor(path, offset)
                    and isinstance(rows, list)
                    and all(isinstance(row, dict) and isinstance(row.get("queued_id"), str) for row in rows)):
                return {"offset": offset, "end": stamp.st_size}, rows
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {"offset": 0, "end": stamp.st_size}, []


def save_queue_checkpoint(path: Path, stamp, offset: int, rows: list) -> None:
    # These logs are server-owned append-only journals. Replacement/truncation
    # invalidates the checkpoint; a bounded boundary anchor detects regrowth.
    current = path.stat()
    if (current.st_dev, current.st_ino) != (stamp.st_dev, stamp.st_ino) or current.st_size < offset:
        return
    if current.st_size == stamp.st_size and current.st_mtime_ns != stamp.st_mtime_ns:
        return
    value = {"version": 1, "dev": stamp.st_dev, "ino": stamp.st_ino,
             "mtime_ns": stamp.st_mtime_ns, "offset": offset,
             "anchor": queue_checkpoint_anchor(path, offset), "pending": rows}
    target = path.with_name("queue-recovery.json")
    temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def scan_queued_turns_from_events(
    sessions: list[tuple[str, dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    recovered: dict[str, list[dict[str, Any]]] = {}
    session_items = sessions if sessions is not None else [
        (session_id, dict(sess))
        for session_id, sess in STORE.sessions.items()
    ]
    for session_id, sess in session_items:
        path = events_path(session_id)
        if not path.exists():
            continue
        scan_stamp = path.stat()
        proof_complete = True
        scan, prior_rows = queue_checkpoint_seed(path, scan_stamp, enabled=True)
        pending: dict[str, dict[str, Any]] = {row["queued_id"]: row for row in prior_rows}
        order: list[str] = [row["queued_id"] for row in prior_rows]
        for raw_line in queued_event_lines(path, scan=scan):
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                proof_complete = False
                continue
            queued_id = str(event.get("queued_id") or "")
            event_type = str(event.get("type") or "")
            if event_type == "turn_queue_run_now":
                for superseded_id in event.get("superseded_queued_ids") or []:
                    superseded_id = str(superseded_id or "")
                    pending.pop(superseded_id, None)
                    if superseded_id in order:
                        order.remove(superseded_id)
            if event_type == "turn_queue_paused":
                for paused_id in event.get("queued_ids") or []:
                    paused_id = str(paused_id or "")
                    if paused_id in pending:
                        pending[paused_id]["_paused_after_stop"] = True
                continue
            if event_type == "turn_queue_delivery_fenced" and queued_id:
                if queued_id not in pending:
                    pending[queued_id] = queued_turn_from_event(
                        event,
                        sess,
                        len(order) + 1,
                    )
                    try:
                        fenced_index = max(
                            0,
                            min(
                                int(
                                    event.get("position")
                                    or len(order) + 1
                                ) - 1,
                                len(order),
                            ),
                        )
                    except (TypeError, ValueError):
                        fenced_index = len(order)
                    order.insert(fenced_index, queued_id)
                pending[queued_id]["_paused_after_stop"] = True
                pending[queued_id]["_native_delivery_fenced"] = True
                continue
            if event_type == "turn_queued" and queued_id:
                pending[queued_id] = queued_turn_from_event(event, sess, len(order) + 1)
                if queued_id not in order:
                    order.append(queued_id)
            elif event_type in {"turn_queue_updated", "turn_queue_run_now"} and queued_id in pending:
                if event_type == "turn_queue_run_now" and event.get("native_goal_steer") is not True:
                    pending[queued_id]["_paused_after_stop"] = False
                    pending[queued_id].pop("_native_delivery_fenced", None)
                # A goal follow-up continues an existing provider turn. Its
                # run-now marker does not authorize another launch or clear an
                # uncertain-delivery fence: only the accepted turn_steered
                # marker below consumes it. Keep a truncated batch paused.
                legacy_generated_replay = (
                    event_type == "turn_queue_run_now"
                    and event.get("replays_interrupted_message")
                    and not normalize_steering_lineage(event.get("steering_lineage"))
                )
                if legacy_generated_replay:
                    lineage = parse_legacy_steering_lineage(
                        str(event.get("request_prompt") or ""),
                        list(event.get("file_ids") or []),
                    )
                    pending[queued_id]["prompt"] = (
                        lineage[-1]["prompt"]
                        if lineage
                        else event.get("prompt") or ""
                    )
                    pending[queued_id]["steering_lineage"] = lineage
                elif event.get("request_prompt") is not None:
                    pending[queued_id]["prompt"] = event.get("request_prompt") or ""
                elif event.get("prompt") is not None:
                    pending[queued_id]["prompt"] = event.get("prompt") or ""
                if event.get("display_prompt") is not None:
                    pending[queued_id]["display_prompt"] = event.get("display_prompt")
                if event.get("file_ids") is not None:
                    pending[queued_id]["file_ids"] = list(event.get("file_ids") or [])
                if event.get("display_file_ids") is not None:
                    pending[queued_id]["display_file_ids"] = list(event.get("display_file_ids") or [])
                # Older servers rewrote these legacy routing fields on edits.
                for legacy_key in RETIRED_QUEUED_TURN_FIELDS:
                    if event.get(legacy_key) is not None:
                        if event.get(legacy_key):
                            pending[queued_id][legacy_key] = event[legacy_key]
                        else:
                            pending[queued_id].pop(legacy_key, None)
                if event.get("client_capabilities") is not None:
                    pending[queued_id]["client_capabilities"] = list(event.get("client_capabilities") or [])
                if "skill_selection" in event:
                    pending[queued_id]["skill_selection"] = event.get(
                        "skill_selection"
                    )
                if event.get("interrupted_run_id") is not None:
                    pending[queued_id]["steer_interrupted_run_id"] = event.get("interrupted_run_id")
                if event.get("replays_interrupted_message") is not None:
                    pending[queued_id]["replays_interrupted_message"] = bool(event.get("replays_interrupted_message"))
                if event.get("steering_lineage") is not None:
                    pending[queued_id]["steering_lineage"] = normalize_steering_lineage(event.get("steering_lineage"))
                if event_type == "turn_queue_run_now" and queued_id in order:
                    order.remove(queued_id)
                    order.insert(0, queued_id)
            elif event_type == "turn_queue_reordered":
                positions = event.get("positions") or []
                try:
                    ordered = sorted(
                        [item for item in positions if item.get("queued_id") in pending],
                        key=lambda item: int(item.get("position") or 0),
                    )
                    seen = [str(item.get("queued_id")) for item in ordered]
                    order = seen + [qid for qid in order if qid not in seen]
                except Exception:
                    pass
            elif queued_id and (
                event_type in {"turn_started", "turn_unqueued"}
                or is_native_goal_steer_event(event)
            ):
                pending.pop(queued_id, None)
                if queued_id in order:
                    order.remove(queued_id)
        items = [
            pending[qid]
            for qid in order
            if qid in pending and (
                str(pending[qid].get("prompt") or "").strip()
                or bool(pending[qid].get("file_ids"))
            )
        ]
        if items:
            recovered[session_id] = items
        if proof_complete:
            with suppress(OSError):
                save_queue_checkpoint(path, scan_stamp, scan["offset"], [pending[qid] for qid in order if qid in pending])
    return recovered


async def queue_checkpoint_loop() -> None:
    while True:
        await asyncio.sleep(60)
        if SERVER.queue_recovery_task is not None:
            continue
        for session_id in tuple(QUEUE_CHECKPOINT_DIRTY):
            sess = STORE.sessions.get(session_id)
            QUEUE_CHECKPOINT_DIRTY.discard(session_id)
            if not sess or session_id in DELETING_SESSIONS:
                continue
            try:
                await asyncio.to_thread(scan_queued_turns_from_events, [(session_id, dict(sess))])
            except Exception as exc:
                QUEUE_CHECKPOINT_DIRTY.add(session_id)
                logger.warning("queue checkpoint deferred session=%s error=%s", session_id, type(exc).__name__)

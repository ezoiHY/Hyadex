"""The per-chat event log: appending, sequencing, indexing, timeline reads and live delivery."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
import json
import logging.handlers
import math
import mmap
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Iterator
import uuid

from fastapi import WebSocket

from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    FORK_INTERNAL_PURPOSES,
    hyadex_setting,
    JOB_MAX_ACTIVE_RUNS,
    JOB_MIN_AVAILABLE_MEM_MB,
    MAX_ACTIVE_AGENT_RUNS,
    MAX_EVENT_RESPONSE_LIMIT,
    MIN_START_AVAILABLE_MEM_MB,
    SERVER_INSTANCE_ID,
    WEBSOCKET_SEND_TIMEOUT_SECONDS,
)
from hyadex.core.event_seq import (
    FORK_INTERNAL_RUN_CACHE,
    FORK_INTERNAL_RUN_LOCKS,
    last_event_seq_from_file,
    read_event_index,
    timeline_index_session_lock,
    write_event_index,
)
from hyadex.core.paths import artifact_publications_dir, code_diffs_dir, events_path, now_iso
from hyadex.core.session_alerts import reversed_jsonl_events
from hyadex.core.session_codex import (
    bounded_codex_output_text,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_SESSION_INDEX,
    session_codex_thread_id,
    TIMELINE_INDEX_CODEX_COMPACTION_TYPES,
    TIMELINE_INDEX_CODEX_GOAL_TYPES,
    timeline_index_codex_lifecycle_key,
)
from hyadex.core.session_files import (
    event_establishes_session_file_origin,
    event_files_belong_to_session,
    file_record_belongs_to_session,
    read_file_meta_record,
)
from hyadex.core.sessions import event_output_text, session_codex_subagent_states, STORE
from hyadex.core.state import (
    COMPACT_TIMELINE_HIDDEN_TYPES,
    CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES,
    DARWIN_MEMORY_CACHE,
    DARWIN_MEMORY_CACHE_SECONDS,
    EVENT_INDEX_MIN_FILE_BYTES,
    EVENT_INDEX_STRIDE,
    EVENT_LINE_SEQ_RE,
    EVENTS,
    REASONING_SUMMARY_STREAMS,
    TIMELINE_INDEX_CACHE,
    TIMELINE_INDEX_CACHE_LOCK,
    TIMELINE_INDEX_CACHE_MAX,
    TIMELINE_INDEX_HIDDEN_TYPES,
    TIMELINE_INDEX_PROJECTION_VERSION,
    TIMELINE_INDEX_TRACE_TYPES,
)
from hyadex.core.timeline_index import (
    fork_internal_run_ids,
    is_client_visible_event,
    is_visible_timeline_event,
    project_legacy_imported_provider_event,
    TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD,
)
from hyadex.core.util import bounded_jsonl_records_range, normalized_file_id
from hyadex.features.usage import HUB
from hyadex.providers.authority import strip_all_legacy_hyadex_provider_authority_suffixes
from hyadex.providers.claude.history import (
    CLAUDE_METADATA_REPAIR_CACHE,
    CLAUDE_TASK_NOTIFICATION_RE,
)
from hyadex.providers.codex.history import (
    CODEX_GOAL_HISTORY_REPAIR_CACHE,
    CODEX_NATIVE_HISTORY_REPAIR_CACHE,
    codex_user_item_has_human_provenance,
)

logger = logging.getLogger("hyadex-server")


def _snapshot_object_dir_is_owned(session_id: str, path: Path) -> bool:
    try:
        root = code_diffs_dir(session_id).resolve()
        return (
            path.name.startswith(".snapshot-")
            and path.parent.resolve() == root
            and not path.is_symlink()
        )
    except (OSError, RuntimeError):
        return False


REASONING_SUMMARY_STREAM_REVISION = 0


def rebuild_event_index(path: Path) -> list[tuple[int, int]]:
    """Scan the transcript once and persist every stride checkpoint."""

    entries: list[tuple[int, int]] = []
    offset = 0
    last_seq = 0
    with path.open("rb") as source:
        for raw_line in source:
            line_offset = offset
            offset += len(raw_line)
            if not raw_line.strip():
                continue
            match = EVENT_LINE_SEQ_RE.match(raw_line)
            if match is not None:
                seq = int(match.group(1))
            else:
                try:
                    seq = int(json.loads(raw_line.decode("utf-8", "replace")).get("seq") or 0)
                except Exception:
                    continue
            if seq <= last_seq:
                continue
            last_seq = seq
            if seq % EVENT_INDEX_STRIDE == 0:
                entries.append((seq, line_offset))
    write_event_index(path, entries)
    return entries


def event_index_resume_offset(path: Path, after: int) -> int:
    """Byte offset of the greatest indexed line with ``seq <= after``, or 0.

    The chosen line is re-read and its ``seq`` compared before it is trusted,
    so a stale checkpoint degrades to the historical full scan instead of
    skipping events. The transcript is append-only with ascending ``seq``,
    which makes every line before that offset ``<= after`` as well.
    """

    if after <= 0:
        return 0
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    entries = read_event_index(path)
    rebuilt = False
    if entries is None:
        if size < EVENT_INDEX_MIN_FILE_BYTES:
            return 0
        try:
            entries = rebuild_event_index(path)
        except OSError:
            return 0
        rebuilt = True
    for _attempt in range(2):
        candidate: tuple[int, int] | None = None
        for seq, offset in entries:
            if seq > after:
                break
            candidate = (seq, offset)
        if candidate is None:
            return 0
        seq, offset = candidate
        try:
            with path.open("rb") as source:
                source.seek(offset)
                raw_line = source.readline()
            event = json.loads(raw_line.decode("utf-8", "replace"))
            if int(event.get("seq") or 0) == seq:
                return offset
        except Exception:
            pass
        if rebuilt:
            break
        try:
            entries = rebuild_event_index(path)
        except OSError:
            return 0
        rebuilt = True
    return 0


@EVENTS.on_deliver
async def deliver_event_to_live_clients(session_id: str, event: dict[str, Any]) -> None:
    if event_files_belong_to_session(event, session_id) and is_client_visible_event(event):
        await HUB.broadcast(session_id, client_safe_event(event))


def append_durable_event_batch_sync(
    path: Path,
    session_id: str,
    first_seq: int,
    event_specs: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Append and fsync an event batch, rolling back every partial write."""
    original_size = path.stat().st_size if path.exists() else 0
    events: list[dict[str, Any]] = []
    try:
        with path.open("a", encoding="utf-8") as stream:
            for offset, (event_type, payload) in enumerate(event_specs):
                event = {
                    "seq": first_seq + offset,
                    "id": f"evt_{uuid.uuid4().hex[:16]}",
                    "session_id": session_id,
                    "type": event_type,
                    "ts": now_iso(),
                    **dict(payload),
                }
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
                events.append(event)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            with path.open("r+b") as stream:
                stream.truncate(original_size)
                stream.flush()
                os.fsync(stream.fileno())
        raise
    return events


def legacy_session_event_file_ids(session_id: str) -> set[str]:
    """Return ownerless/owned file IDs whose origin is recorded in this chat.

    A historical turn merely referencing a file ID is not proof of ownership:
    only the nested record emitted when the file was uploaded or created
    establishes legacy membership.
    """
    path = events_path(session_id)
    if not path.is_file():
        return set()
    found: set[str] = set()
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        for line in stream:
            if not line.strip():
                continue
            with suppress(Exception):
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                event_owner = str(event.get("session_id") or "").strip()
                if event_owner and event_owner != session_id:
                    continue
                if not event_establishes_session_file_origin(event, session_id):
                    continue
                for key in ("file", "artifact"):
                    record = event.get(key)
                    if not isinstance(record, dict):
                        continue
                    file_id = normalized_file_id(record.get("id"))
                    if file_id and file_record_belongs_to_session(record, session_id):
                        found.add(file_id)
    return found


def client_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return one client-safe persisted event.

    Provider-history projection lives at this shared boundary so REST pages,
    websocket replay, live broadcasts, and less common client readers cannot
    accidentally diverge. Events without a durable session owner (for
    example synthesized job summaries) still receive the ordinary field
    redaction below.
    """

    projection_session_id = str(event.get("session_id") or "").strip()
    if projection_session_id:
        event = project_provider_history_event_for_egress(
            event,
            projection_session_id,
        )
    safe = event
    if (
        "provider_cross_chat_route_snapshot" in event
        or "secure_peer_route_snapshots" in event
        or "_history_sync_checkpoint" in event
    ):
        # This is a durable recovery/auth ceiling, not client timeline state.
        safe = dict(event)
        safe.pop("provider_cross_chat_route_snapshot", None)
        safe.pop("secure_peer_route_snapshots", None)
        safe.pop("_history_sync_checkpoint", None)
    if str(event.get("type") or "").startswith("emergency_alert_") and (
        "emergency_request_id" in event
        or "emergency_request_digest" in event
    ):
        if safe is event:
            safe = dict(event)
        safe.pop("emergency_request_id", None)
        safe.pop("emergency_request_digest", None)
    if event.get("type") == "agent_notification" and (
        "notification_request_id" in event or "notification_request_digest" in event
    ):
        if safe is event:
            safe = dict(event)
        safe.pop("notification_request_id", None)
        safe.pop("notification_request_digest", None)
    output = event.get("output")
    if output is not None and not isinstance(output, str):
        if safe is event:
            safe = dict(event)
        safe["output"] = event_output_text(output)
        output = safe["output"]
    if isinstance(output, str) and len(output) > CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS:
        if safe is event:
            safe = dict(event)
        safe["output"] = bounded_codex_output_text(output)
    job = event.get("job")
    # Timeline job summaries omit the private prompt. Older clients still
    # require a string field, so add an empty compatibility value at egress.
    if isinstance(job, dict) and not isinstance(job.get("prompt"), str):
        if safe is event:
            safe = dict(event)
        safe["job"] = {**job, "prompt": ""}
    return safe


def read_events(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 500,
    *,
    tail: bool = False,
    visible: bool = False,
    cap_to_response_limit: bool = True,
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    path = events_path(session_id)
    if not path.exists():
        return []
    limit = max(1, int(limit or 500))
    if cap_to_response_limit:
        # API responses are bounded; internal scans (history dedup) may ask
        # for a larger window and must not be silently clamped.
        limit = min(limit, MAX_EVENT_RESPONSE_LIMIT)
    internal_run_ids = fork_internal_run_ids(session_id) if visible else set()
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    with path.open("r", encoding="utf-8", errors="ignore") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq > after and (before is None or seq < before):
                if not event_files_belong_to_session(event, session_id):
                    continue
                if not include_internal:
                    event = client_safe_event(event)
                if visible and not is_visible_timeline_event(event, fork_internal_run_ids=internal_run_ids):
                    continue
                if tail_out is not None:
                    tail_out.append(event)
                else:
                    out.append(event)
            if tail_out is None and len(out) >= limit:
                break
    return list(tail_out) if tail_out is not None else out


def read_client_events_page(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 500,
    *,
    tail: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    """Read raw client events while excluding server-only admission records."""
    path = events_path(session_id)
    if not path.exists():
        return [], 0, 0, 0, 0
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    latest_seq = 0
    client_count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq > 0:
                latest_seq = seq
            if seq <= after or (before is not None and seq >= before):
                continue
            if not event_files_belong_to_session(event, session_id):
                continue
            if not is_client_visible_event(event):
                continue
            safe_event = client_safe_event(event)
            client_count += 1
            if tail_out is not None:
                tail_out.append(safe_event)
            elif len(out) < limit:
                out.append(safe_event)
    events = list(tail_out) if tail_out is not None else out
    if tail:
        omitted_before = max(0, client_count - len(events))
        omitted_after = 0
    else:
        omitted_before = 0
        omitted_after = max(0, client_count - len(events))
    return events, latest_seq, client_count, omitted_before, omitted_after


EventCatchupContinuation = tuple[int, int, int, int, int]


def normalized_event_catchup_continuation(
    value: int | EventCatchupContinuation,
) -> EventCatchupContinuation | None:
    """Return a trusted (offset, device, inode, size, scanned-seq) cursor."""

    if (
        not isinstance(value, tuple)
        or len(value) != 5
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        or any(item < 0 for item in value)
    ):
        return None
    return value


def read_event_catchup_batch(
    session_id: str,
    *,
    after: int,
    through: int,
    offset: int | EventCatchupContinuation = 0,
    limit: int = 500,
    visible: bool = False,
) -> tuple[list[dict[str, Any]], EventCatchupContinuation, bool]:
    """Read one forward websocket catch-up batch from one file generation.

    The opaque continuation binds its byte offset to the event file's device,
    inode, and observed size. Maintenance can atomically replace or shrink the
    log between websocket pages; a bare offset from the old generation could
    then seek past surviving events in the replacement and falsely look like
    EOF. A stale continuation is discarded and resumed by sequence instead.
    """

    path = events_path(session_id)
    previous = normalized_event_catchup_continuation(offset)
    if through <= after:
        if previous is not None:
            return [], previous, True
        return [], (0, 0, 0, 0, max(0, int(after))), True
    if not path.exists():
        return [], (0, 0, 0, 0, max(0, int(after))), False
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    internal_run_ids: set[str] | None = None
    out: list[dict[str, Any]] = []
    highest_scanned_seq = max(0, int(after))
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        generation_matches = bool(
            previous is not None
            and previous[1] == int(opened.st_dev)
            and previous[2] == int(opened.st_ino)
            and previous[3] <= int(opened.st_size)
            and previous[0] <= int(opened.st_size)
        )
        next_offset = previous[0] if generation_matches and previous else 0
        if next_offset == 0 and after > 0:
            # First page, or a stale-generation retry: use the sparse index
            # only if it still describes the exact file currently open.
            indexed_offset = event_index_resume_offset(path, after)
            with suppress(OSError):
                current = path.stat()
                if (
                    int(current.st_dev) == int(opened.st_dev)
                    and int(current.st_ino) == int(opened.st_ino)
                    and 0 <= indexed_offset <= int(current.st_size)
                ):
                    next_offset = indexed_offset
        try:
            source.seek(next_offset)
        except OSError:
            next_offset = 0
            source.seek(0)
        for raw_line in source:
            line_start = next_offset
            next_offset = source.tell()
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq <= after:
                continue
            if seq > through:
                # This record belongs to a later gap/live pass. Do not bake an
                # undelivered record into the continuation byte offset.
                next_offset = line_start
                highest_scanned_seq = max(highest_scanned_seq, through)
                break
            highest_scanned_seq = max(highest_scanned_seq, seq)
            if not event_files_belong_to_session(event, session_id):
                continue
            if not is_client_visible_event(event):
                continue
            event = client_safe_event(event)
            if visible and event.get("forked") is True and internal_run_ids is None:
                internal_run_ids = fork_internal_run_ids(session_id)
            if visible and not is_visible_timeline_event(
                event,
                fork_internal_run_ids=internal_run_ids,
            ):
                continue
            out.append(event)
            if len(out) >= limit:
                break
        finished = os.fstat(source.fileno())

    generation_stable = bool(
        int(finished.st_dev) == int(opened.st_dev)
        and int(finished.st_ino) == int(opened.st_ino)
        and int(finished.st_size) >= int(opened.st_size)
        and next_offset <= int(finished.st_size)
    )
    current_stat: os.stat_result | None = None
    with suppress(OSError):
        current_stat = path.stat()
    if current_stat is None or (
        int(current_stat.st_dev) != int(opened.st_dev)
        or int(current_stat.st_ino) != int(opened.st_ino)
    ):
        generation_stable = False

    if generation_stable:
        continuation: EventCatchupContinuation = (
            next_offset,
            int(finished.st_dev),
            int(finished.st_ino),
            int(finished.st_size),
            highest_scanned_seq,
        )
    else:
        # Preserve only sequence progress. The next page must resolve a fresh
        # byte offset against the replacement/current generation.
        continuation = (
            0,
            int(current_stat.st_dev) if current_stat is not None else 0,
            int(current_stat.st_ino) if current_stat is not None else 0,
            int(current_stat.st_size) if current_stat is not None else 0,
            highest_scanned_seq,
        )
    return out, continuation, highest_scanned_seq >= through


async def send_event_catchup(
    session_id: str,
    ws: WebSocket,
    *,
    after: int,
    through: int,
    visible: bool,
) -> int:
    """Stream every persisted event through a fixed boundary in sequence."""
    cursor = max(0, int(after or 0))
    offset: int | EventCatchupContinuation = 0
    stalled_once = False
    while cursor < through:
        events, offset, exhausted = await asyncio.to_thread(
            read_event_catchup_batch,
            session_id,
            after=cursor,
            through=through,
            offset=offset,
            limit=MAX_EVENT_RESPONSE_LIMIT,
            visible=visible,
        )
        for event in events:
            await asyncio.wait_for(
                ws.send_json(event),
                timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS,
            )
        # Raw/internal rows are intentionally not sent, but scanning them is
        # still authoritative sequence progress. Advance only after every
        # visible row from this page has been delivered successfully.
        previous_cursor = cursor
        cursor = max(cursor, min(int(offset[4]), int(through)))
        if exhausted and cursor >= through:
            return through
        if cursor > previous_cursor:
            stalled_once = False
            continue
        # A replacement can land after the reader's final identity check. One
        # forced sequence-based retry closes that final-stat/return window;
        # if the fixed boundary truly is unavailable, return actual progress
        # so the caller's locked gap pass can re-evaluate it safely.
        if stalled_once:
            break
        offset = 0
        stalled_once = True
    return cursor


def read_visible_events_page(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 500,
    *,
    tail: bool = False,
    compact: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    path = events_path(session_id)
    if not path.exists():
        return [], 0, 0, 0, 0
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    internal_run_ids = fork_internal_run_ids(session_id)
    latest_seq = 0
    visible_count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq > 0:
                latest_seq = seq
            if seq <= after or (before is not None and seq >= before):
                continue
            if not event_files_belong_to_session(event, session_id):
                continue
            if not is_client_visible_event(event):
                continue
            event = client_safe_event(event)
            if not is_visible_timeline_event(
                event,
                compact=compact,
                fork_internal_run_ids=internal_run_ids,
            ):
                continue
            visible_count += 1
            if tail_out is not None:
                tail_out.append(event)
            elif len(out) < limit:
                out.append(event)
    events = list(tail_out) if tail_out is not None else out
    if tail:
        omitted_before = max(0, visible_count - len(events))
        omitted_after = 0
    else:
        omitted_before = 0
        omitted_after = max(0, visible_count - len(events))
    return events, latest_seq, visible_count, omitted_before, omitted_after


def read_visible_events_after_page(
    session_id: str,
    after: int,
    limit: int = 500,
    *,
    compact: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    """Read a recent append-only delta without rescanning the whole transcript."""
    path = events_path(session_id)
    if not path.exists() or path.stat().st_size <= 0:
        return [], 0, 0, 0, 0
    latest_seq = last_event_seq_from_file(path)
    if latest_seq <= after:
        return [], latest_seq, 0, 0, 0
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    selected: deque[dict[str, Any]] = deque(maxlen=limit)
    internal_run_ids: set[str] | None = None
    visible_count = 0

    try:
        with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            cursor = len(mapped)
            while cursor > 0:
                line_end = cursor
                newline = mapped.rfind(b"\n", 0, line_end)
                line_start = newline + 1
                cursor = newline if newline >= 0 else 0
                raw = mapped[line_start:line_end].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                seq = int(event.get("seq", 0))
                if seq <= after:
                    break
                if not event_files_belong_to_session(event, session_id):
                    continue
                if not is_client_visible_event(event):
                    continue
                event = client_safe_event(event)
                if event.get("forked") is True and internal_run_ids is None:
                    internal_run_ids = fork_internal_run_ids(session_id)
                if not is_visible_timeline_event(
                    event,
                    compact=compact,
                    fork_internal_run_ids=internal_run_ids,
                ):
                    continue
                visible_count += 1
                selected.appendleft(event)
    except Exception as exc:
        logger.warning("fast event delta failed session=%s: %s", session_id, exc)
        return read_visible_events_page(session_id, after=after, limit=limit, tail=False, compact=compact)

    events = list(selected)
    return events, latest_seq, visible_count, 0, max(0, visible_count - len(events))


def project_provider_history_event_for_egress(
    event: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    """Project one persisted provider-history row at an egress/context edge.

    A generated provider-only user row has no user-authored content to carry,
    but its empty turn boundary must remain: one imported run can contain
    several user/assistant pairs, so dropping the row could attach a following
    answer to the preceding question. The private semantic-only marker never
    leaves this process.
    """

    projected = project_legacy_imported_provider_event(event, session_id)
    if projected.get(TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD):
        projected = dict(projected)
        projected.pop(TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD, None)
    return projected


def find_agent_notification_event(
    session_id: str,
    request_id: str,
    source_run_id: str,
) -> dict[str, Any] | None:
    retained = frozenset({
        "type", "id", "seq", "run_id", "notification_request_id",
        "notification_request_digest", "notification_level",
    })
    for event in reversed_jsonl_events(events_path(session_id), retained_fields=retained):
        if (
            event.get("type") == "agent_notification"
            and event.get("notification_request_id") == request_id
            and event.get("run_id") == source_run_id
        ):
            return event
    return None


def bounded_jsonl_events_range(
    path: Path,
    start: int,
    end: int,
    *,
    expected_stat: dict[str, int],
    preserve_invalid: bool = False,
) -> Iterator[dict[str, Any] | None]:
    for event, _record_end in bounded_jsonl_records_range(
        path,
        start,
        end,
        expected_stat=expected_stat,
    ):
        if event is not None or preserve_invalid:
            yield event


def reasoning_summary_stream_snapshot(session_id: str) -> dict[str, Any]:
    """An additive, non-durable lane; never advances the event-log cursor."""
    global REASONING_SUMMARY_STREAM_REVISION
    REASONING_SUMMARY_STREAM_REVISION += 1
    remaining = 1_000_000
    items = []
    # Keep summaries available to compact/older clients when plaintext consumes
    # the shared packet budget; chronology still uses each item's after_seq.
    states = sorted(REASONING_SUMMARY_STREAMS.get(session_id, {}).values(),
        key=lambda state: state["item"].get("phase") == "reasoning")
    for state in states:
        item = dict(state["item"])
        if not item["text"].strip():
            continue
        if len(item["text"]) > remaining:
            item["text"] = item["text"][:remaining]
            item["text_truncated"] = True
        remaining -= len(item["text"])
        items.append(item)
        if not remaining or len(items) == 256:
            break
    return {
        "type": "reasoning_summary_stream", "session_id": session_id,
        "instance_id": SERVER_INSTANCE_ID,
        "revision": REASONING_SUMMARY_STREAM_REVISION,
        "items": items,
    }


async def send_event_stream_status(ws: WebSocket, session_id: str, phase: str) -> None:
    # Ephemeral, opt-in control frames: never persisted or assigned a seq.
    await asyncio.wait_for(ws.send_json({
        "type": "event_stream_status", "version": 1,
        "session_id": session_id, "phase": phase,
    }), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)

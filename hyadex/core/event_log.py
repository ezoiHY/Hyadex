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
from hyadex.core.paths import artifact_publications_dir, code_diffs_dir, events_path, now_iso
from hyadex.core.sessions import (
    bounded_codex_output_text,
    CODEX_SUBAGENT_INDEX_LOCK,
    CODEX_SUBAGENT_SESSION_INDEX,
    event_establishes_session_file_origin,
    event_files_belong_to_session,
    event_output_text,
    file_record_belongs_to_session,
    FORK_INTERNAL_RUN_CACHE,
    FORK_INTERNAL_RUN_LOCKS,
    last_event_seq_from_file,
    read_event_index,
    read_file_meta_record,
    reversed_jsonl_events,
    session_codex_subagent_states,
    session_codex_thread_id,
    STORE,
    TIMELINE_INDEX_CODEX_COMPACTION_TYPES,
    TIMELINE_INDEX_CODEX_GOAL_TYPES,
    timeline_index_codex_lifecycle_key,
    timeline_index_session_lock,
    write_event_index,
)
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


FORK_INTERNAL_RUN_CACHE_MAX = int(hyadex_setting("FORK_INTERNAL_RUN_CACHE_MAX", "128"))


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


def parse_vm_stat_available_mb(stdout: str) -> int | None:
    """Derive available memory from ``vm_stat`` output.

    Free, inactive, speculative, and purgeable pages are all reclaimable
    without swapping, which is the closest macOS analogue of Linux
    ``MemAvailable``.
    """

    page_size = 4096
    pages: dict[str, int] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Mach Virtual Memory Statistics"):
            match = re.search(r"page size of (\d+) bytes", stripped)
            if match:
                page_size = int(match.group(1))
            continue
        if ":" not in stripped:
            continue
        name, _, raw = stripped.partition(":")
        digits = raw.strip().rstrip(".")
        if digits.isdigit():
            pages[name.strip().lower()] = int(digits)
    if not pages:
        return None
    reclaimable = sum(
        pages.get(name, 0)
        for name in (
            "pages free",
            "pages inactive",
            "pages speculative",
            "pages purgeable",
        )
    )
    return int(reclaimable * page_size / (1024 * 1024))


def darwin_available_memory_mb() -> int | None:
    now = time.monotonic()
    if now - float(DARWIN_MEMORY_CACHE["at"]) < DARWIN_MEMORY_CACHE_SECONDS:
        return DARWIN_MEMORY_CACHE["mb"]
    value: int | None = None
    try:
        result = subprocess.run(
            ["vm_stat"],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            value = parse_vm_stat_available_mb(result.stdout)
    except (OSError, subprocess.SubprocessError):
        value = None
    DARWIN_MEMORY_CACHE["at"] = now
    DARWIN_MEMORY_CACHE["mb"] = value
    return value


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


def is_client_visible_event(event: dict[str, Any]) -> bool:
    """Keep legacy internal handoff admission rows out of every client.

    Event logs written while cross-chat messaging existed still contain the
    server-generated delivery prompt in queue/start rows; clients would label
    it as user-authored. The target agent's reasoning, tools, artifacts, and
    assistant output from those runs remain visible.
    """

    if str(event.get("type") or "") == "_event_sequence_checkpoint":
        return False
    cross_chat_owner = any(
        str(event.get(key) or "").strip()
        for key in (
            "cross_chat_envelope_id",
            "cross_chat_exchange_id",
            "cross_chat_exchange_leg_id",
            # Native exchange events carry both aliases. Accept canonical
            # names too so a recovered/mixed-version event cannot expose the
            # synthetic prompt merely because one alias was omitted.
            "exchange_id",
            "exchange_leg_id",
        )
    )
    return not (
        cross_chat_owner
        and event.get("purpose") == "cross_chat_handoff_delivery"
        and str(event.get("type") or "") in CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES
    )


def fork_internal_run_ids(session_id: str) -> set[str]:
    """Return copied digest workflow run IDs, incrementally cached per transcript."""
    path = events_path(session_id)
    if not path.exists():
        return set()
    lock = FORK_INTERNAL_RUN_LOCKS.setdefault(session_id, threading.Lock())
    with lock:
        stat = path.stat()
        signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = FORK_INTERNAL_RUN_CACHE.get(session_id)
        if cached and cached.get("signature") == signature:
            FORK_INTERNAL_RUN_CACHE.move_to_end(session_id)
            return set(cached.get("run_ids") or ())

        can_append = bool(
            cached and cached.get("inode") == stat.st_ino and
            0 <= int(cached.get("offset") or 0) < stat.st_size
        )
        run_ids = set(cached.get("run_ids") or ()) if can_append else set()
        offset = int(cached.get("offset") or 0) if can_append else 0
        final_offset = offset
        with path.open("rb") as source:
            source.seek(offset)
            for raw_line in source:
                if not raw_line.strip():
                    continue
                with suppress(Exception):
                    event = json.loads(raw_line.decode("utf-8", "replace"))
                    if event.get("forked") is not True or event.get("purpose") not in FORK_INTERNAL_PURPOSES:
                        continue
                    run_id = str(event.get("run_id") or "").strip()
                    if run_id:
                        run_ids.add(run_id)
            final_offset = source.tell()

        FORK_INTERNAL_RUN_CACHE[session_id] = {
            "signature": signature,
            "inode": stat.st_ino,
            "offset": final_offset,
            "run_ids": run_ids,
        }
        FORK_INTERNAL_RUN_CACHE.move_to_end(session_id)
        while len(FORK_INTERNAL_RUN_CACHE) > max(1, FORK_INTERNAL_RUN_CACHE_MAX):
            stale_session_id, _ = FORK_INTERNAL_RUN_CACHE.popitem(last=False)
            FORK_INTERNAL_RUN_LOCKS.pop(stale_session_id, None)
        return set(run_ids)


def is_fork_internal_event(event: dict[str, Any], internal_run_ids: set[str] | None = None) -> bool:
    if event.get("forked") is not True:
        return False
    if event.get("purpose") in FORK_INTERNAL_PURPOSES:
        return True
    run_id = str(event.get("run_id") or "").strip()
    return bool(run_id and internal_run_ids and run_id in internal_run_ids)


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


def is_visible_timeline_event(
    event: dict[str, Any],
    *,
    compact: bool = False,
    fork_internal_run_ids: set[str] | None = None,
) -> bool:
    if is_fork_internal_event(event, fork_internal_run_ids):
        return False
    event_type = str(event.get("type") or "")
    if compact:
        return event_type not in COMPACT_TIMELINE_HIDDEN_TYPES
    return event_type != "raw_event"


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


def codex_subagent_ownership_snapshot() -> dict[str, str]:
    """Return one thread-safe point-in-time copy for worker-thread readers."""

    with CODEX_SUBAGENT_INDEX_LOCK:
        return dict(CODEX_SUBAGENT_SESSION_INDEX)


TIMELINE_INDEX_JOB_TYPES = {"job_created", "job_ran", "job_started", "job_deferred", "job_finished", "job_error"}


JOB_RUN_HISTORY_TEXT_LIMIT = 64 * 1024


def timeline_index_is_native_steer_transition_stop(
    event: dict[str, Any],
) -> bool:
    """Return whether a stop only retires the pre-steer logical run."""
    return (
        str(event.get("type") or "") == "turn_stopped"
        and (
            event.get("native_steer") is True
            or bool(str(event.get("superseded_by_run_id") or "").strip())
        )
    )


# Cross-chat messaging was removed, but event logs written while it existed
# keep their handoff, exchange and mailbox lifecycle rows. These read-only
# projections keep those legacy rows grouped exactly as before.
def timeline_index_async_cross_chat_key(event: dict[str, Any]) -> str | None:
    if event.get("conversation_mode") != "async_route_v1":
        return None
    event_type = str(event.get("type") or "")
    if not (
        re.fullmatch(r"chat_conversation_message_(registered|received|mailbox_migrated|read|deleted|queued|started|delivered|cancelled|failed)", event_type)
        or event.get("purpose") == "cross_chat_handoff_delivery"
    ):
        return None
    envelope_id = str(event.get("cross_chat_envelope_id") or event.get("handoff_id") or "").strip()
    if not envelope_id or event.get("exchange_id") or event.get("cross_chat_exchange_id"):
        return None
    return f"cross-chat:handoff:{envelope_id}"


def update_async_cross_chat_timeline_record(
    record: dict[str, Any], event: dict[str, Any], session_id: str,
) -> None:
    """Keep pending recipient messages out of the visible navigation spine."""

    incoming = event.get("target_session_id") == session_id and event.get("source_session_id") != session_id
    event_type = str(event.get("type") or "")
    record["_async_cross_chat_message"] = True
    if event_type.startswith("chat_conversation_message_"):
        arrived = not incoming or event.get("delivery_mode") == "mailbox" or event_type in {
            "chat_conversation_message_started", "chat_conversation_message_delivered",
        }
        if arrived and not record.get("_async_message_anchor_seq"):
            migrated = event_type == "chat_conversation_message_mailbox_migrated"
            record["_async_message_anchor_seq"] = int((record.get("start_seq") if migrated else None) or event.get("seq") or 0)
            record["_async_message_anchor_ts"] = (event.get("received_at") or event.get("ts")) if migrated else event.get("ts")
        counterpart = str(event.get("source_title" if incoming else "target_title") or "Unknown agent")
        record["title"] = compact_timeline_index_text(
            f"Message from {counterpart}" if incoming else f"Sent to {counterpart}", 72,
        )
        preview = event.get("handoff_preview")
        if isinstance(preview, str):
            record["preview"] = compact_timeline_index_text(preview)


def timeline_index_cross_chat_key(event: dict[str, Any]) -> str | None:
    """Return one semantic identity for every transition of a handoff."""

    async_key = timeline_index_async_cross_chat_key(event)
    if async_key:
        return async_key
    event_type = str(event.get("type") or "")
    exchange_id = str(
        event.get("exchange_id")
        or event.get("cross_chat_exchange_id")
        or ""
    ).strip()
    if exchange_id:
        leg_id = str(
            event.get("exchange_leg_id")
            or event.get("cross_chat_exchange_leg_id")
            or ""
        ).strip()
        return (
            f"cross_chat_exchange:{exchange_id}:leg:{leg_id}"
            if leg_id
            else f"cross_chat_exchange:{exchange_id}:summary"
        )
    if not (
        event_type.startswith("cross_chat_")
        or event.get("purpose") == "cross_chat_handoff_delivery"
        or event.get("cross_chat_envelope_id")
        or event.get("handoff_id")
    ):
        return None
    envelope_id = str(
        event.get("handoff_id") or event.get("cross_chat_envelope_id") or ""
    ).strip()
    return f"cross_chat:{envelope_id}" if envelope_id else None


def timeline_index_event_is_hidden(event: dict[str, Any]) -> bool:
    # Native-steer stops need routing context before they can be classified:
    # ordinary chat transitions are hidden, while a scheduled-job transition
    # is the terminal status for that job run. Both index builders handle that
    # distinction using their run-to-job maps before calling this predicate.
    event_type = str(event.get("type") or "")
    if event_type in TIMELINE_INDEX_HIDDEN_TYPES:
        return True
    # A status delivery only carries an exchange's terminal state to the other
    # agent. Its exchange summary is the user-facing row; neither the internal
    # status leg nor its provider-turn stream may consume a pagination slot.
    return event.get("cross_chat_exchange_status") is True or (
        event_type.startswith("cross_chat_exchange_leg_")
        and str(event.get("exchange_leg_kind") or "") == "status"
    )


TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD = "_hyadex_imported_prompt_hidden"


def project_legacy_imported_provider_event(
    event: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    """Hide or clean only source-proven legacy provider-import records.

    Native turns, unproven assistant output, partial markers, and ordinary
    pasted text are never rewritten. The import provenance and generated run shape
    are required because older durable events no longer retain Claude's richer
    provider-origin metadata.
    """

    replay = CODEX_NATIVE_HISTORY_REPAIR_CACHE.project_event(session_id, event)
    if replay is not None:
        return replay
    interruption = CLAUDE_METADATA_REPAIR_CACHE.project_event(session_id, event)
    if interruption is not None:
        return interruption
    if (
        str(event.get("type") or "") != "turn_started"
        or event.get("imported") is not True
        or not str(event.get("run_id") or "").startswith("import_")
        or (
            bool(str(event.get("session_id") or "").strip())
            and str(event.get("session_id") or "").strip() != session_id
        )
        or str(event.get("backend") or "").strip().lower()
        not in {BACKEND_CLAUDE, BACKEND_CODEX}
        or not isinstance(event.get("prompt"), str)
    ):
        return event
    if (
        str(event.get("backend") or "").strip().lower() == BACKEND_CODEX
        and not codex_user_item_has_human_provenance(event)
        and CODEX_GOAL_HISTORY_REPAIR_CACHE.is_hidden(session_id, event)
    ):
        projected = dict(event)
        projected["prompt"] = ""
        projected["provider_runtime_context"] = "goal"
        projected["metadata_only"] = True
        projected[TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD] = True
        return projected
    if CLAUDE_METADATA_REPAIR_CACHE.is_hidden(session_id, event):
        projected = dict(event)
        projected["prompt"] = ""
        projected["provider_history_repair"] = "source_proven_import"
        projected[TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD] = True
        return projected
    prompt = str(event["prompt"])
    generated_task_notification = bool(
        event.get("provider_history_sanitized") is not True
        and str(event.get("backend") or "").strip().lower() == BACKEND_CLAUDE
        and CLAUDE_TASK_NOTIFICATION_RE.fullmatch(prompt.strip())
    )
    cleaned = strip_all_legacy_hyadex_provider_authority_suffixes(
        prompt,
        # Provider-history provenance is already established above. Permit a
        # structurally valid envelope from the chat where a migrated local
        # transcript originally ran, while still validating its bound path,
        # nonce, and embedded chat-id syntax.
        expected_session_id=None,
        allow_portable_authority_root=True,
    )
    if not generated_task_notification and cleaned == prompt:
        return event
    projected = dict(event)
    if generated_task_notification or not cleaned.strip():
        projected["prompt"] = ""
        projected[TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD] = True
    else:
        projected["prompt"] = cleaned
    return projected


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


def timeline_index_retire_native_steer_turn(
    event: dict[str, Any],
    current_turn_by_run: dict[str, str],
    active_turn_key: str | None,
) -> str | None:
    """Retire hidden pre-steer routing state without adding a semantic row."""
    if not timeline_index_is_native_steer_transition_stop(event):
        return active_turn_key
    run_id = str(event.get("run_id") or "").strip()
    retired_key = (
        current_turn_by_run.pop(run_id, None)
        if run_id
        else active_turn_key
    )
    return None if retired_key and retired_key == active_turn_key else active_turn_key


def compact_timeline_index_text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, dict):
            value = value.get("message") or value.get("error") or value
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def timeline_index_event_text(event: dict[str, Any]) -> str:
    if event.get("type") == "reasoning_text":
        return ""
    for field in ("result_text", "text", "prompt", "digest", "message", "error", "output"):
        text = compact_timeline_index_text(event.get(field))
        if text:
            return text
    return ""


def timeline_index_is_error(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type in {"tool_started", "tool_finished", "raw_event"}:
        return False
    return event_type == "error" or event_type.endswith("_error") or event.get("is_error") is True or bool(event.get("error"))


def timeline_index_cached_entry(
    session_id: str,
    *,
    touch: bool = False,
) -> dict[str, Any] | None:
    with TIMELINE_INDEX_CACHE_LOCK:
        cached = TIMELINE_INDEX_CACHE.get(session_id)
        if cached is not None and touch:
            TIMELINE_INDEX_CACHE.move_to_end(session_id)
        return cached


def evict_timeline_index_cache_locked() -> None:
    """Evict inactive LRU entries while preserving in-flight session scans."""
    target = max(1, TIMELINE_INDEX_CACHE_MAX)
    if len(TIMELINE_INDEX_CACHE) <= target:
        return
    for cache_session_id in list(TIMELINE_INDEX_CACHE):
        if len(TIMELINE_INDEX_CACHE) <= target:
            break
        session_lock = timeline_index_session_lock(cache_session_id)
        if session_lock.locked():
            continue
        TIMELINE_INDEX_CACHE.pop(cache_session_id, None)


def bounded_job_history_text(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def job_run_history_event_snapshot(
    event: dict[str, Any],
    *,
    include_output: bool = True,
    text_limit: int = JOB_RUN_HISTORY_TEXT_LIMIT,
) -> dict[str, Any]:
    """Keep the useful/status fields for one run without retaining full events."""
    fields = [
        "id",
        "session_id",
        "seq",
        "type",
        "ts",
        "run_id",
        "purpose",
        "job_id",
        "job_title",
        "job_occurrence_id",
        "job_scheduled_run_at",
        "job_scheduled_run_at_iso",
        "backend",
        "message",
        "error",
        "exit_code",
        "is_error",
        "stopped",
        "native_interrupt",
    ]
    if include_output:
        fields.extend(("result_text", "text", "output"))
    snapshot = {
        field: bounded_job_history_text(event[field], text_limit)
        for field in fields
        if event.get(field) is not None
    }
    return snapshot


def scheduled_job_occurrence_key(event: dict[str, Any]) -> str:
    """Return the stable scheduler occurrence represented by an event.

    A busy retry moves ``next_run_at`` but deliberately leaves
    ``scheduled_run_at`` unchanged.  That canonical timestamp therefore
    identifies all defer/error notices for the one pending occurrence without
    conflating later scheduled executions of the same job.
    """
    explicit = str(event.get("job_occurrence_id") or "").strip()
    if explicit:
        return f"id:{explicit[:160]}"
    job = event.get("job") if isinstance(event.get("job"), dict) else {}
    scheduled_at = event.get("job_scheduled_run_at")
    if scheduled_at is None:
        scheduled_at = job.get("scheduled_run_at")
    if scheduled_at is not None:
        try:
            numeric = float(scheduled_at)
        except (TypeError, ValueError):
            numeric = math.nan
        if math.isfinite(numeric):
            return f"scheduled:{numeric:.6f}"
    scheduled_at_iso = str(
        event.get("job_scheduled_run_at_iso")
        or job.get("scheduled_run_at_iso")
        or ""
    ).strip()
    if scheduled_at_iso:
        return f"scheduled-iso:{scheduled_at_iso[:160]}"
    return ""


def scheduled_job_run_status(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    if timeline_index_is_error(event):
        return "failed"
    if event_type == "turn_stopped" or event.get("stopped") is True:
        return "stopped"
    if event_type == "job_deferred":
        return "deferred"
    if event_type in {"turn_finished", "job_finished"}:
        if event.get("exit_code") not in (None, 0):
            return "failed"
        return "completed"
    if event_type in {"turn_started", "job_started", "job_ran"}:
        return "running"
    return None


def scheduled_job_status_should_replace(
    current_event: dict[str, Any] | None,
    current_status: str | None,
    event: dict[str, Any],
    status: str,
) -> bool:
    if not isinstance(current_event, dict):
        return True
    current_run_id = str(current_event.get("run_id") or "").strip()
    run_id = str(event.get("run_id") or "").strip()
    if (
        current_run_id
        and current_run_id == run_id
        and current_status in {"completed", "failed", "stopped"}
        and status == "running"
        and str(event.get("type") or "") == "job_ran"
    ):
        # job_ran is emitted by the scheduler just after turn launch. A very
        # fast run can finish before that marker reaches disk; do not regress
        # the already terminal run back to "running".
        return False
    return int(event.get("seq") or 0) >= int(current_event.get("seq") or 0)


def scheduled_job_output_rank(event: dict[str, Any]) -> tuple[int, int, int]:
    event_type = str(event.get("type") or "")
    if timeline_index_is_error(event) and any(
        str(event.get(field) or "").strip()
        for field in ("result_text", "text", "output")
    ):
        return 1, int(event.get("seq") or 0), 4
    if event_type == "turn_finished" and str(event.get("result_text") or "").strip():
        return 1, int(event.get("seq") or 0), 3
    if event_type == "assistant_text" and str(event.get("text") or "").strip():
        return 1, int(event.get("seq") or 0), 2
    if event_type == "job_finished" and any(
        str(event.get(field) or "").strip()
        for field in ("result_text", "text", "message")
    ):
        return 1, int(event.get("seq") or 0), 1
    return 0, int(event.get("seq") or 0), 0


def build_timeline_index(session_id: str) -> dict[str, Any]:
    lock = timeline_index_session_lock(session_id)
    with lock:
        payload = _build_timeline_index_locked(session_id)
    with TIMELINE_INDEX_CACHE_LOCK:
        evict_timeline_index_cache_locked()
    return payload


def _build_timeline_index_locked(session_id: str) -> dict[str, Any]:
    claude_metadata_signature = CLAUDE_METADATA_REPAIR_CACHE.signature(session_id)
    codex_goal_history_signature = (
        CODEX_GOAL_HISTORY_REPAIR_CACHE.signature(session_id),
        CODEX_NATIVE_HISTORY_REPAIR_CACHE.signature(session_id),
    )
    path = events_path(session_id)
    if not path.exists():
        with TIMELINE_INDEX_CACHE_LOCK:
            TIMELINE_INDEX_CACHE.pop(session_id, None)
        return {
            "session_id": session_id,
            "landmarks": [],
            "latest_seq": 0,
            "event_count": 0,
            "generated_at": now_iso(),
        }
    session = STORE.sessions.get(session_id) or {}
    root_codex_thread_id = session_codex_thread_id(session)
    cached = timeline_index_cached_entry(session_id, touch=True)
    durable_child_codex_thread_ids = {
        str(thread_id or "").strip()
        for thread_id in (
            cached.get("durable_child_codex_thread_ids") or set()
            if cached
            else set()
        )
        if str(thread_id or "").strip()
    }
    child_codex_thread_ids = {
        str(state.get("subagent_id") or "").strip()
        for state in session_codex_subagent_states(session_id)
        if str(state.get("backend") or "") == BACKEND_CODEX
    }
    child_codex_thread_ids.update(durable_child_codex_thread_ids)
    subagent_ownership = codex_subagent_ownership_snapshot()
    child_codex_thread_ids.update(
        thread_id
        for thread_id, owner_session_id in subagent_ownership.items()
        if owner_session_id == session_id and thread_id != root_codex_thread_id
    )
    child_codex_thread_ids.discard("")
    codex_scope_signature = (
        root_codex_thread_id,
        tuple(sorted(child_codex_thread_ids)),
    )
    stat = path.stat()
    signature = (
        stat.st_size,
        stat.st_mtime_ns,
        *codex_scope_signature,
        codex_goal_history_signature,
    )
    if (
        cached
        and cached.get("projection_version") == TIMELINE_INDEX_PROJECTION_VERSION
        and cached.get("claude_metadata_signature") == claude_metadata_signature
        and cached.get("signature") == signature
        and int(cached.get("offset") or 0) >= stat.st_size
    ):
        return cached["payload"]

    can_append = bool(
        cached and
        cached.get("projection_version") == TIMELINE_INDEX_PROJECTION_VERSION and
        cached.get("claude_metadata_signature") == claude_metadata_signature and
        cached.get("codex_goal_history_signature") == codex_goal_history_signature and
        cached.get("inode") == stat.st_ino and
        cached.get("codex_scope_signature") == codex_scope_signature and
        "internal_status_run_ids" in cached and
        0 <= int(cached.get("offset") or 0) < stat.st_size
    )
    records: list[dict[str, Any]] = cached["records"] if can_append else []
    by_key: dict[str, dict[str, Any]] = (
        cached.get("by_key") or {record["key"]: record for record in records}
        if can_append
        else {}
    )
    landmarks_by_key: dict[str, dict[str, Any]] = (
        cached.get("landmarks_by_key") or {}
        if can_append
        else {}
    )
    landmark_order: list[str] = (
        cached.get("landmark_order") or []
        if can_append
        else []
    )
    # Keep provisional turn lookup linear in the journal size. Scheduled-job
    # streams can have hundreds of thousands of events with no ordinary turn;
    # scanning every preceding landmark for each one makes cold opens quadratic.
    turn_record_keys_by_run: dict[str, list[str]] = {}
    for existing in records:
        existing_key = str(existing.get("key") or "")
        if existing_key.startswith("turn:"):
            existing_run = existing_key[5:].split(":start-", 1)[0]
            turn_record_keys_by_run.setdefault(existing_run, []).append(existing_key)
    dirty_record_keys: set[str] = set()
    active_turn_key: str | None = cached.get("active_turn_key") if can_append else None
    current_turn_by_run: dict[str, str] = (
        cached.get("current_turn_by_run") or {}
        if can_append
        else {}
    )
    job_by_run: dict[str, str] = (
        cached.get("job_by_run") or {}
        if can_append
        else {}
    )
    job_timeline_group_by_run: dict[str, str] = (
        cached.get("job_timeline_group_by_run") or {}
        if can_append
        else {}
    )
    job_timeline_group_by_occurrence: dict[str, str] = (
        cached.get("job_timeline_group_by_occurrence") or {}
        if can_append
        else {}
    )
    job_timeline_group_by_run_start_seq: dict[int, str] = (
        cached.get("job_timeline_group_by_run_start_seq") or {}
        if can_append
        else {}
    )
    job_timeline_group_by_runless_event_seq: dict[int, str] = (
        cached.get("job_timeline_group_by_runless_event_seq") or {}
        if can_append
        else {}
    )
    job_timeline_group_keys: dict[str, list[str]] = (
        cached.get("job_timeline_group_keys") or {}
        if can_append
        else {}
    )
    latest_timeline_landmark_key: str | None = (
        cached.get("latest_timeline_landmark_key")
        if can_append
        else None
    )
    fork_internal_run_ids: set[str] = (
        cached.get("fork_internal_run_ids") or set()
        if can_append
        else set()
    )
    internal_status_run_ids: set[str] = (
        cached.get("internal_status_run_ids") or set()
        if can_append
        else set()
    )
    visible_count = int(cached.get("visible_count") or 0) if can_append else 0
    latest_seq = int(cached.get("latest_seq") or 0) if can_append else 0
    run_history_records: dict[str, dict[str, Any]] = (
        cached.get("run_history_records") or {}
        if can_append
        else {}
    )
    job_run_keys: dict[str, list[str]] = (
        cached.get("job_run_keys") or {}
        if can_append
        else {}
    )
    job_run_key_sets: dict[str, set[str]] = (
        cached.get("job_run_key_sets")
        or {
            job_id: set(keys)
            for job_id, keys in job_run_keys.items()
        }
        if can_append
        else {}
    )
    job_timeline_group_run_keys: dict[str, list[str]] = (
        cached.get("job_timeline_group_run_keys") or {}
        if can_append
        else {}
    )
    job_timeline_group_run_key_sets: dict[str, set[str]] = (
        cached.get("job_timeline_group_run_key_sets")
        or {
            group_id: set(keys)
            for group_id, keys in job_timeline_group_run_keys.items()
        }
        if can_append
        else {}
    )
    run_history_keys_by_run_id: dict[str, list[str]] = (
        cached.get("run_history_keys_by_run_id") or {}
        if can_append
        else {}
    )
    run_history_key_by_occurrence: dict[str, str] = (
        cached.get("run_history_key_by_occurrence") or {}
        if can_append
        else {}
    )
    current_run_history_key: dict[str, str] = (
        cached.get("current_run_history_key") or {}
        if can_append
        else {}
    )
    scan_offset = int(cached.get("offset") or 0) if can_append else 0
    event_offset = scan_offset

    def attach_run_to_job(
        history_key: str,
        record: dict[str, Any],
        job_id: str,
        *,
        job_title: str = "",
    ) -> None:
        clean_job_id = str(job_id or "").strip()
        if not clean_job_id:
            return
        prior_job_id = str(record.get("job_id") or "").strip()
        if prior_job_id and prior_job_id != clean_job_id:
            prior_keys = job_run_keys.get(prior_job_id)
            if prior_keys and history_key in prior_keys:
                prior_keys.remove(history_key)
                job_run_key_sets.get(prior_job_id, set()).discard(history_key)
        record["job_id"] = clean_job_id
        if job_title:
            record["job_title"] = job_title
        keys = job_run_keys.setdefault(clean_job_id, [])
        key_set = job_run_key_sets.setdefault(clean_job_id, set(keys))
        if history_key not in key_set:
            keys.append(history_key)
            key_set.add(history_key)

    def index_run_event(
        event: dict[str, Any],
        *,
        line_end_offset: int,
        resolved_job_id: str = "",
        resolved_job_title: str = "",
    ) -> None:
        seq = int(event.get("seq") or 0)
        run_id = str(event.get("run_id") or "").strip()
        event_type = str(event.get("type") or "")
        status = scheduled_job_run_status(event)
        occurrence_key = scheduled_job_occurrence_key(event)
        occurrence_map_key = (
            f"{resolved_job_id}\0{occurrence_key}"
            if resolved_job_id and occurrence_key
            else ""
        )
        # Runless deferrals/failures are real scheduled attempts and belong in
        # lazy history, but job creation/deletion markers are not run history.
        if not run_id:
            if not resolved_job_id or (
                event_type != "job_deferred"
                and not timeline_index_is_error(event)
            ):
                return
            history_key = (
                f"status:{resolved_job_id}:occurrence:{occurrence_key}"
                if occurrence_key
                else f"status:{resolved_job_id}:{seq}"
            )
        else:
            keys = run_history_keys_by_run_id.setdefault(run_id, [])
            if event_type == "turn_started":
                deferred_history_key = (
                    f"status:{resolved_job_id}:occurrence:{occurrence_key}"
                    if occurrence_map_key
                    else ""
                )
                occurrence_history_key = (
                    run_history_key_by_occurrence.get(occurrence_map_key)
                    if occurrence_map_key
                    else ""
                )
                # Busy retries and the eventual execution are one scheduler
                # occurrence. Reuse its runless history record once the turn
                # finally starts, instead of showing both a deferred row and
                # a completed row for the same tick.
                if (
                    deferred_history_key
                    and deferred_history_key in run_history_records
                ):
                    history_key = deferred_history_key
                    run_history_records[history_key]["run_id"] = run_id
                elif (
                    occurrence_history_key
                    and occurrence_history_key in run_history_records
                ):
                    history_key = occurrence_history_key
                else:
                    base_key = f"run:{run_id}"
                    history_key = (
                        base_key
                        if not keys
                        else f"{base_key}:start-{seq}"
                    )
                if history_key not in keys:
                    keys.append(history_key)
                current_run_history_key[run_id] = history_key
            else:
                occurrence_history_key = (
                    run_history_key_by_occurrence.get(occurrence_map_key)
                    if occurrence_map_key
                    else ""
                )
                history_key = (
                    occurrence_history_key
                    or current_run_history_key.get(run_id)
                    or (keys[-1] if keys else f"run:{run_id}")
                )
                if history_key not in keys:
                    keys.append(history_key)
        if occurrence_map_key:
            run_history_key_by_occurrence[occurrence_map_key] = history_key
        record = run_history_records.get(history_key)
        if record is None:
            record = {
                "key": history_key,
                "session_id": session_id,
                "run_id": run_id,
                "job_id": "",
                "job_title": resolved_job_title or "Scheduled job",
                "occurrence_key": occurrence_key,
                "start_seq": seq,
                "end_seq": seq,
                "start_offset": max(0, event_offset),
                "end_offset": max(0, line_end_offset),
                "timestamp": event.get("ts"),
                "output_event": None,
                "status_event": None,
                "status": None,
            }
            run_history_records[history_key] = record
        if occurrence_key and not str(record.get("occurrence_key") or "").strip():
            record["occurrence_key"] = occurrence_key
        record["start_seq"] = min(int(record.get("start_seq", seq)), seq)
        record["end_seq"] = max(int(record.get("end_seq") or seq), seq)
        record["start_offset"] = min(
            int(record.get("start_offset", event_offset)),
            max(0, event_offset),
        )
        record["end_offset"] = max(
            int(record.get("end_offset") or line_end_offset),
            max(0, line_end_offset),
        )
        record["timestamp"] = event.get("ts") or record.get("timestamp")
        output_rank = scheduled_job_output_rank(event)
        current_output = record.get("output_event")
        is_known_scheduled_run = bool(
            resolved_job_id
            or str(record.get("job_id") or "").strip()
            or event.get("purpose") == "scheduled_job"
        )
        if is_known_scheduled_run and output_rank[0] > 0 and (
            not isinstance(current_output, dict)
            or output_rank > scheduled_job_output_rank(current_output)
        ):
            record["output_event"] = job_run_history_event_snapshot(
                event,
                text_limit=JOB_RUN_HISTORY_TEXT_LIMIT,
            )
        if status is not None:
            current_status = record.get("status_event")
            if scheduled_job_status_should_replace(
                current_status if isinstance(current_status, dict) else None,
                str(record.get("status") or "") or None,
                event,
                status,
            ):
                record["status_event"] = job_run_history_event_snapshot(
                    event,
                    include_output=False,
                )
                record["status"] = status
        if resolved_job_id:
            attach_run_to_job(
                history_key,
                record,
                resolved_job_id,
                job_title=resolved_job_title,
            )
        if run_id and (
            event_type in {"turn_finished", "turn_stopped"}
            or timeline_index_is_error(event)
        ) and current_run_history_key.get(run_id) == history_key:
            current_run_history_key.pop(run_id, None)

    def ensure_record(
        key: str,
        kind: str,
        event: dict[str, Any],
        *,
        safe_start_offset: int | None = None,
        advance_timeline: bool = True,
    ) -> dict[str, Any]:
        nonlocal latest_timeline_landmark_key
        record = by_key.get(key)
        seq = int(event.get("seq") or 0)
        record_start_offset = max(
            0,
            int(event_offset if safe_start_offset is None else safe_start_offset),
        )
        if record is None:
            record = {
                "key": key,
                "kind": kind,
                "start_seq": seq,
                "end_seq": seq,
                "start_offset": record_start_offset,
                "title": "",
                "preview": "",
                "meta": "",
                "timestamp": event.get("ts"),
                "tool_count": 0,
                "thought_count": 0,
                "event_count": 0,
                "file_names": [],
                "has_user": False,
            }
            by_key[key] = record
            records.append(record)
            if key.startswith("turn:"):
                turn_record_keys_by_run.setdefault(key[5:].split(":start-", 1)[0], []).append(key)
            if advance_timeline:
                latest_timeline_landmark_key = key
        record["start_seq"] = min(int(record["start_seq"]), seq)
        record["end_seq"] = max(int(record["end_seq"]), seq)
        record["start_offset"] = min(
            int(record.get("start_offset", record_start_offset)),
            record_start_offset,
        )
        record["event_count"] += 1
        dirty_record_keys.add(key)
        return record

    def new_job_timeline_group_key(job_id: str, first_seq: int) -> str:
        """Allocate one stable card key for a contiguous run of one job."""
        keys = job_timeline_group_keys.setdefault(job_id, [])
        if not keys:
            key = f"job:{job_id}"
        else:
            key = f"job:{job_id}:segment:{first_seq}"
            collision = 1
            while key in by_key:
                collision += 1
                key = f"job:{job_id}:segment:{first_seq}:{collision}"
        keys.append(key)
        return key

    def prior_landmark_for_record(
        provisional: dict[str, Any],
    ) -> dict[str, Any] | None:
        provisional_seq = int(provisional.get("start_seq") or 0)
        return max(
            (
                candidate
                for candidate in records
                if candidate is not provisional
                and int(candidate.get("start_seq") or 0) < provisional_seq
            ),
            key=lambda candidate: (
                int(candidate.get("start_seq") or 0),
                int(candidate.get("end_seq") or 0),
            ),
            default=None,
        )

    def assign_job_timeline_group(
        event: dict[str, Any],
        job_id: str,
        *,
        provisional: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Resolve a run to its original contiguous scheduled-job card.

        New runs join the immediately preceding card only when it represents
        the same logical job. Late events retain their run's original card, so
        a completion cannot jump across intervening chat or another job.
        """
        clean_job_id = str(job_id or "").strip()
        seq = int(event.get("seq") or 0)
        event_type = str(event.get("type") or "")
        run_id = str(event.get("run_id") or "").strip()
        occurrence = scheduled_job_occurrence_key(event)
        occurrence_map_key = (
            f"{clean_job_id}\0{occurrence}"
            if clean_job_id and occurrence
            else ""
        )
        key_from_run = (
            job_timeline_group_by_run.get(run_id) if run_id else None
        )
        if key_from_run:
            run_group = by_key.get(key_from_run)
            if (
                not isinstance(run_group, dict)
                or str(run_group.get("job_id") or "") != clean_job_id
            ):
                key_from_run = None
        if run_id and event_type == "turn_started":
            history_keys = run_history_keys_by_run_id.get(run_id) or []
            current_history_key = current_run_history_key.get(run_id)
            current_history = run_history_records.get(current_history_key or "")
            if (
                len(history_keys) > 1
                and isinstance(current_history, dict)
                and int(current_history.get("start_seq") or 0) == seq
            ):
                # Providers occasionally recycle run IDs. A distinct start is
                # a new scheduled attempt and must observe any intervening
                # timeline boundary instead of inheriting the earlier card.
                key_from_run = None
        key_from_occurrence = (
            job_timeline_group_by_occurrence.get(occurrence_map_key)
            if occurrence_map_key
            else None
        )
        # Occurrence metadata identifies the actual scheduler firing and is
        # therefore authoritative when a provider recycles run IDs. A late
        # event without occurrence metadata is inherently ambiguous and falls
        # back to the most recently started attempt for that run ID.
        key = key_from_occurrence or key_from_run
        if key:
            if run_id and (event_type == "turn_started" or not key_from_run):
                job_timeline_group_by_run[run_id] = key
            if occurrence_map_key:
                job_timeline_group_by_occurrence[occurrence_map_key] = key
            if run_id and event_type == "turn_started":
                job_timeline_group_by_run_start_seq[seq] = key
            elif not run_id:
                job_timeline_group_by_runless_event_seq[seq] = key
            return key, False

        preceding_key = latest_timeline_landmark_key
        anchor_seq = seq
        if provisional is not None:
            anchor_seq = int(provisional.get("start_seq") or seq)
            preceding = prior_landmark_for_record(provisional)
            preceding_key = (
                str(preceding.get("key") or "")
                if isinstance(preceding, dict)
                else None
            )
        preceding_record = by_key.get(preceding_key or "")
        if (
            isinstance(preceding_record, dict)
            and preceding_record.get("kind") == "job"
            and str(preceding_record.get("job_id") or "") == clean_job_id
        ):
            key = str(preceding_record.get("key") or "")
            created = False
        else:
            key = new_job_timeline_group_key(clean_job_id, anchor_seq)
            created = True
        if run_id:
            job_timeline_group_by_run[run_id] = key
        if occurrence_map_key:
            job_timeline_group_by_occurrence[occurrence_map_key] = key
        if run_id and str(event.get("type") or "") == "turn_started":
            job_timeline_group_by_run_start_seq[seq] = key
        elif not run_id:
            job_timeline_group_by_runless_event_seq[seq] = key
        return key, created

    def attach_event_history_to_timeline_group(
        event: dict[str, Any],
        group_id: str,
        job_id: str,
    ) -> None:
        """Associate this attempt's global history row with one card segment."""
        run_id = str(event.get("run_id") or "").strip()
        occurrence = scheduled_job_occurrence_key(event)
        occurrence_map_key = (
            f"{job_id}\0{occurrence}" if job_id and occurrence else ""
        )
        occurrence_history_key = (
            run_history_key_by_occurrence.get(occurrence_map_key)
            if occurrence_map_key
            else ""
        )
        if run_id:
            keys = run_history_keys_by_run_id.get(run_id) or []
            history_key = (
                occurrence_history_key
                or current_run_history_key.get(run_id)
                or (keys[-1] if keys else "")
            )
        else:
            history_key = (
                occurrence_history_key
                or f"status:{job_id}:occurrence:{occurrence}"
                if occurrence
                else f"status:{job_id}:{int(event.get('seq') or 0)}"
            )
        if not history_key or history_key not in run_history_records:
            return
        keys = job_timeline_group_run_keys.setdefault(group_id, [])
        key_set = job_timeline_group_run_key_sets.setdefault(
            group_id,
            set(keys),
        )
        if history_key not in key_set:
            keys.append(history_key)
            key_set.add(history_key)

    final_offset = scan_offset
    with path.open("rb") as source:
        source.seek(scan_offset)
        for raw_line in source:
            event_offset = final_offset
            # append_event() writes one complete newline-terminated JSON
            # record. Never advance the cached cursor past an incomplete tail
            # left by a concurrent writer or interrupted write.
            if raw_line and not raw_line.endswith(b"\n"):
                final_offset = event_offset
                break
            final_offset += len(raw_line)
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq") or 0)
            event_type = str(event.get("type") or "")
            run_id = str(event.get("run_id") or "").strip()
            if seq <= 0:
                continue
            if not event_files_belong_to_session(event, session_id):
                continue
            # The synthetic status turn_started row is intentionally not
            # client-visible. Learn its run identity before that filter so
            # legacy unmarked provider traces from the same run stay hidden.
            if event.get("cross_chat_exchange_status") is True and run_id:
                internal_status_run_ids.add(run_id)
            latest_seq = max(latest_seq, seq)
            if not is_client_visible_event(event):
                continue
            event = project_legacy_imported_provider_event(event, session_id)
            hidden_imported_prompt = bool(
                event.get(TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD)
            )
            if (
                event_type == "subagent_state"
                and str(event.get("backend") or "") == BACKEND_CODEX
            ):
                durable_child_thread_id = str(
                    event.get("subagent_id") or ""
                ).strip()
                if (
                    durable_child_thread_id
                    and durable_child_thread_id != root_codex_thread_id
                ):
                    durable_child_codex_thread_ids.add(durable_child_thread_id)
                    child_codex_thread_ids.add(durable_child_thread_id)
            event_thread_id = str(event.get("thread_id") or "").strip()
            usage_thread_ids = {
                str(usage.get("thread_id") or "").strip()
                for usage in (
                    event.get("token_usage_before"),
                    event.get("token_usage_after"),
                )
                if isinstance(usage, dict)
                and str(usage.get("thread_id") or "").strip()
            }
            leaked_child_compaction = bool(
                event_type in TIMELINE_INDEX_CODEX_COMPACTION_TYPES
                and event_thread_id
                and (
                    event_thread_id in child_codex_thread_ids
                    or any(
                        usage_thread_id != event_thread_id
                        for usage_thread_id in usage_thread_ids
                    )
                )
            )
            if leaked_child_compaction:
                # Older app-server builds projected child-native compactions
                # into the parent's JSONL. Keep the durable audit record, but
                # do not expose it as a top-level chat lifecycle landmark.
                continue
            is_forked = event.get("forked") is True
            is_fork_internal = event.get("purpose") in FORK_INTERNAL_PURPOSES
            if is_forked and is_fork_internal and run_id:
                fork_internal_run_ids.add(run_id)
            if is_forked and (is_fork_internal or run_id in fork_internal_run_ids):
                continue
            # Older records tagged only some events in an internal status
            # provider turn. Once its run identity is known, suppress the
            # entire run before it can affect active-turn or pagination state.
            if (
                timeline_index_event_is_hidden(event)
                or run_id in internal_status_run_ids
            ):
                continue
            indexed_job = (
                event.get("job")
                if isinstance(event.get("job"), dict)
                else {}
            )
            explicit_job_id = str(
                event.get("job_id") or indexed_job.get("id") or ""
            ).strip()
            resolved_job_id = explicit_job_id or (
                str(job_by_run.get(run_id) or "").strip()
                if run_id
                else ""
            ) or (run_id if event.get("purpose") == "scheduled_job" else "")
            # A native steer silently retires an ordinary logical chat turn,
            # but interrupting a scheduled run produces the job's terminal
            # Stopped status. Preserve and index that event so the job card no
            # longer remains falsely active with its trace paging disabled.
            if (
                timeline_index_is_native_steer_transition_stop(event)
                and not resolved_job_id
            ):
                active_turn_key = timeline_index_retire_native_steer_turn(
                    event,
                    current_turn_by_run,
                    active_turn_key,
                )
                continue
            if not hidden_imported_prompt:
                visible_count += 1
            indexed_job_title = str(
                event.get("job_title") or indexed_job.get("title") or ""
            ).strip()
            if run_id and explicit_job_id:
                job_by_run[run_id] = explicit_job_id
            index_run_event(
                event,
                line_end_offset=final_offset,
                resolved_job_id=resolved_job_id,
                resolved_job_title=indexed_job_title,
            )

            # A reset is a visible change of provider context even when it
            # belongs to a digest source, ordinary turn or scheduled job.
            # Route it before every metadata-based grouping branch.
            if event_type == "provider_session_reset":
                record = ensure_record(f"event:{event.get('id') or seq}", "system", event)
                record["title"] = "Provider Context Reset"
                record["preview"] = timeline_index_event_text(event) or record["title"]
                continue

            digest_id = str(event.get("digest_job_id") or "").strip()
            cross_chat_key = timeline_index_cross_chat_key(event)
            if cross_chat_key:
                async_message_key = timeline_index_async_cross_chat_key(event)
                async_pending_receipt = bool(
                    async_message_key
                    and event.get("delivery_mode") != "mailbox"
                    and event.get("target_session_id") == session_id
                    and event.get("source_session_id") != session_id
                    and event_type not in {"chat_conversation_message_started", "chat_conversation_message_delivered"}
                    and not (by_key.get(cross_chat_key) or {}).get("_async_message_anchor_seq")
                )
                record = ensure_record(cross_chat_key, "cross_chat", event, advance_timeline=not async_pending_receipt)
                is_target_delivery_event = (
                    event.get("purpose") == "cross_chat_handoff_delivery"
                )
                if run_id and is_target_delivery_event:
                    current_turn_by_run[run_id] = record["key"]
                    active_turn_key = record["key"]
                action = str(event.get("handoff_action") or "").strip()
                status = str(
                    event.get("exchange_leg_status")
                    or event.get("exchange_status")
                    or event.get("handoff_status")
                    or ""
                ).strip()
                if async_message_key:
                    had_async_anchor = bool(record.get("_async_message_anchor_seq"))
                    update_async_cross_chat_timeline_record(record, event, session_id)
                    if not had_async_anchor and record.get("_async_message_anchor_seq"):
                        latest_timeline_landmark_key = record["key"]
                else:
                    record["title"] = compact_timeline_index_text(
                        (
                            "Cross-Chat Exchange"
                            if event.get("exchange_id") or event.get("cross_chat_exchange_id")
                            else
                            "Final Result Handoff"
                            if action == "final_result"
                            else "Cross-Chat Handoff"
                        ),
                        72,
                    )
                    text = timeline_index_event_text(event)
                    if text:
                        record["preview"] = text
                if status:
                    record["status"] = status
                if is_target_delivery_event and event_type in {"turn_finished", "turn_stopped"}:
                    if active_turn_key == record["key"]:
                        active_turn_key = None
                    if run_id:
                        current_turn_by_run.pop(run_id, None)
                continue

            is_digest_lifecycle = event_type.startswith("handoff_digest_")
            is_digest_generation = event.get("purpose") == "handoff_digest"
            if digest_id and (is_digest_lifecycle or is_digest_generation):
                record = ensure_record(f"digest:{digest_id}", "digest", event)
                if run_id and is_digest_generation:
                    current_turn_by_run[run_id] = record["key"]
                    active_turn_key = record["key"]
                text = timeline_index_event_text(event)
                if text and (is_digest_lifecycle or not record.get("preview")):
                    record["preview"] = text
                if event_type == "handoff_digest_received":
                    record["title"] = "Context Digest"
                elif event_type == "handoff_digest_sent":
                    record["title"] = "Digest Sent"
                elif event_type == "handoff_digest_error":
                    record["title"] = "Digest Failed"
                elif not record.get("title"):
                    record["title"] = "Creating Digest"
                if event_type == "turn_finished":
                    if active_turn_key == record["key"]:
                        active_turn_key = None
                    if run_id:
                        current_turn_by_run.pop(run_id, None)
                continue

            codex_lifecycle_key = timeline_index_codex_lifecycle_key(event)
            if codex_lifecycle_key:
                record = ensure_record(
                    codex_lifecycle_key,
                    "codex_lifecycle",
                    event,
                )
                if event_type in TIMELINE_INDEX_CODEX_COMPACTION_TYPES:
                    record["_codex_thread_id"] = event_thread_id or record.get(
                        "_codex_thread_id"
                    )
                text = timeline_index_event_text(event)
                record["title"] = (
                    "Codex Goal"
                    if event_type in TIMELINE_INDEX_CODEX_GOAL_TYPES
                    else "Codex Compaction"
                )
                if event_type == "codex_compaction_completed":
                    record["_codex_lifecycle_terminal_seq"] = seq
                    record["preview"] = text or record["title"]
                elif not record.get("_codex_lifecycle_terminal_seq"):
                    # Provider retries can duplicate a late start after the
                    # terminal notification. Keep the completed/interrupted
                    # state authoritative without moving the arrival anchor.
                    record["preview"] = text or record["title"]
                # A lifecycle row is anchored where it started. Its latest
                # event updates the row's content without moving it later in
                # the timeline.
                if not record.get("timestamp"):
                    record["timestamp"] = event.get("ts")
                continue

            # Alerts are independently actionable timeline facts. Keep them
            # outside turn traces and scheduled-job summaries.
            if event_type == "emergency_alert_raised":
                record = ensure_record(
                    f"event:{event.get('id') or seq}",
                    "system",
                    event,
                )
                record["title"] = "Emergency Alert Raised"
                record["preview"] = (
                    timeline_index_event_text(event) or record["title"]
                )
                continue

            if (
                event_type in TIMELINE_INDEX_JOB_TYPES
                or event.get("purpose") == "scheduled_job"
                or event.get("job_id")
                or (run_id and run_id in job_by_run)
            ):
                job = event.get("job") if isinstance(event.get("job"), dict) else {}
                job_id = str(
                    event.get("job_id")
                    or job.get("id")
                    or job_by_run.get(run_id)
                    or run_id
                    or f"job-{seq}"
                )
                if run_id:
                    job_by_run[run_id] = job_id
                prior_key = current_turn_by_run.pop(run_id, None) if run_id else None
                if not prior_key and run_id:
                    prior_keys = turn_record_keys_by_run.get(run_id, [])
                    while prior_keys and prior_keys[-1] not in by_key:
                        prior_keys.pop()
                    prior_key = prior_keys[-1] if prior_keys else None
                prior = by_key.get(prior_key or "")
                group_key, group_created = assign_job_timeline_group(
                    event,
                    job_id,
                    provisional=(prior if isinstance(prior, dict) else None),
                )
                record = ensure_record(
                    group_key,
                    "job",
                    event,
                    advance_timeline=bool(group_created and prior is None),
                )
                record["job_id"] = job_id
                record["job_timeline_group_id"] = group_key
                if isinstance(prior, dict) and run_id:
                    provisional_start_seq = int(prior.get("start_seq") or 0)
                    if provisional_start_seq > 0:
                        # Legacy providers can reveal scheduled-job ownership
                        # only after an ordinary-looking turn has started. Keep
                        # that original start bound to this segment so a later
                        # reuse of the same run ID cannot steal its events when
                        # semantic pages are reconstructed from JSONL.
                        job_timeline_group_by_run_start_seq[
                            provisional_start_seq
                        ] = group_key
                attach_event_history_to_timeline_group(
                    event,
                    group_key,
                    job_id,
                )
                if prior_key and prior_key != record["key"]:
                    prior = by_key.pop(prior_key, None)
                    if prior is not None:
                        landmarks_by_key.pop(prior_key, None)
                        dirty_record_keys.discard(prior_key)
                        if run_id:
                            history_keys = run_history_keys_by_run_id.get(run_id) or []
                            history_record = (
                                run_history_records.get(history_keys[-1])
                                if history_keys
                                else None
                            )
                            prior_preview = str(prior.get("preview") or "").strip()
                            if (
                                isinstance(history_record, dict)
                                and prior_preview
                                and not isinstance(
                                    history_record.get("output_event"),
                                    dict,
                                )
                            ):
                                status_event = history_record.get("status_event")
                                status_type = (
                                    str(status_event.get("type") or "")
                                    if isinstance(status_event, dict)
                                    else ""
                                )
                                history_record["output_event"] = {
                                    "id": f"legacy_job_output:{run_id}",
                                    "session_id": session_id,
                                    "seq": int(prior.get("end_seq") or seq),
                                    "type": (
                                        "turn_finished"
                                        if status_type == "turn_finished"
                                        else "assistant_text"
                                    ),
                                    "ts": prior.get("timestamp"),
                                    "run_id": run_id,
                                    "purpose": "scheduled_job",
                                    "job_id": str(job_id),
                                    "job_title": str(
                                        job.get("title")
                                        or event.get("job_title")
                                        or "Scheduled job"
                                    ),
                                    (
                                        "result_text"
                                        if status_type == "turn_finished"
                                        else "text"
                                    ): prior_preview,
                                }
                        record["start_seq"] = min(int(record["start_seq"]), int(prior["start_seq"]))
                        record["end_seq"] = max(int(record["end_seq"]), int(prior["end_seq"]))
                        record["start_offset"] = min(
                            int(record.get("start_offset", event_offset)),
                            int(prior.get("start_offset", event_offset)),
                        )
                        for field in ("event_count", "tool_count", "thought_count"):
                            record[field] = int(record.get(field) or 0) + int(prior.get(field) or 0)
                        for file_name in prior.get("file_names") or []:
                            if file_name not in record["file_names"]:
                                record["file_names"].append(file_name)
                        for field in ("prompt", "trace_preview"):
                            if not record.get(field) and prior.get(field):
                                record[field] = prior[field]
                        if int(prior.get("start_seq") or seq) <= int(
                            record.get("start_seq") or seq
                        ):
                            record["timestamp"] = (
                                prior.get("timestamp")
                                or record.get("timestamp")
                            )
                        records.remove(prior)
                    if latest_timeline_landmark_key == prior_key:
                        latest_timeline_landmark_key = record["key"]
                    if active_turn_key == prior_key:
                        active_turn_key = None
                record["title"] = compact_timeline_index_text(job.get("title") or record["title"] or event.get("message") or "Scheduled job", 72)
                text = timeline_index_event_text(event)
                if text:
                    record["preview"] = text
                continue

            if timeline_index_is_error(event):
                text = timeline_index_event_text(event)
                record = ensure_record(f"event:{event.get('id') or seq}", "error", event)
                record["title"] = compact_timeline_index_text(event_type.replace("_", " ").title() or "Error", 72)
                record["preview"] = text or "Agent error"
                continue

            if event_type == "turn_started" or is_native_goal_steer_event(event):
                run_key = run_id or f"seq-{seq}"
                key = f"turn:{run_key}"
                base_record = by_key.get(key)
                safe_start_offset = None
                if is_native_goal_steer_event(event):
                    # A human follow-up splits presentation only. Its stable
                    # key also works when paging starts after the goal began.
                    key = f"turn:{run_key}:start-{seq}"
                elif base_record is not None and (
                    base_record.get("has_turn_start")
                    or base_record.get("has_user")
                ):
                    # Replaying from a later repeated run ID needs to see the
                    # original turn first so the collector assigns the same
                    # ``:start-{seq}`` disambiguated key as the index.
                    safe_start_offset = int(base_record.get("start_offset") or 0)
                    key = f"turn:{run_key}:start-{seq}"
                active_turn_key = key
                if run_id:
                    current_turn_by_run[run_id] = key
                if hidden_imported_prompt:
                    # Preserve routing for a following provider answer without
                    # exposing the generated input. The placeholder retains
                    # the turn boundary offset for tail semantic paging and is
                    # omitted unless a later visible event fills the turn.
                    record = ensure_record(
                        key,
                        "assistant",
                        event,
                        safe_start_offset=safe_start_offset,
                    )
                    record["has_turn_start"] = True
                    record["_hidden_imported_prompt_only"] = True
                    continue
                if event.get("purpose") == "handoff_digest_delivery":
                    ensure_record(
                        key,
                        "assistant",
                        event,
                        safe_start_offset=safe_start_offset,
                    )
                    continue
                record = ensure_record(
                    key,
                    "user",
                    event,
                    safe_start_offset=safe_start_offset,
                )
                record["has_turn_start"] = True
                record["has_user"] = True
                prompt = compact_timeline_index_text(event.get("prompt"))
                if prompt:
                    record["title"] = compact_timeline_index_text(prompt, 72)
                    record["prompt"] = prompt
                continue

            key = current_turn_by_run.get(run_id) if run_id else active_turn_key
            if run_id and not key:
                key = f"turn:{run_id}"
            if event_type in {"assistant_text", "turn_finished"}:
                key = key or f"turn:seq-{seq}"
                record = ensure_record(key, "assistant", event)
                if not record.get("has_user") and record.get("kind") != "digest":
                    record["kind"] = "assistant"
                response = compact_timeline_index_text(event.get("result_text") if event_type == "turn_finished" else event.get("text"))
                if response:
                    record.pop("_hidden_imported_prompt_only", None)
                    record["preview"] = response
                    if not record["title"]:
                        record["title"] = compact_timeline_index_text(response, 72)
                if event_type == "turn_finished" and active_turn_key == key:
                    active_turn_key = None
                if event_type == "turn_finished" and run_id:
                    current_turn_by_run.pop(run_id, None)
                continue

            if event_type in {"artifact_created", "file_uploaded"}:
                file_payload = event.get("artifact") or event.get("file")
                if isinstance(file_payload, dict):
                    if event_type == "file_uploaded" and not key:
                        continue
                    key = key or f"turn:seq-{seq}"
                    record = ensure_record(key, "media", event)
                    record.pop("_hidden_imported_prompt_only", None)
                    file_name = compact_timeline_index_text(file_payload.get("title") or file_payload.get("filename"), 72)
                    if file_name and file_name not in record["file_names"]:
                        record["file_names"].append(file_name)
                    if not record["title"] and file_name:
                        record["title"] = file_name
                continue

            if event_type in TIMELINE_INDEX_TRACE_TYPES or key:
                key = key or f"turn:seq-{seq}"
                record = ensure_record(key, "trace", event)
                if event_type == "tool_started":
                    record["tool_count"] += 1
                elif event_type in {"reasoning_summary", "reasoning_text"}:
                    record["thought_count"] += 1
                text = timeline_index_event_text(event)
                if text and not record.get("trace_preview"):
                    record.pop("_hidden_imported_prompt_only", None)
                    record["trace_preview"] = text
                continue

            text = timeline_index_event_text(event)
            record = ensure_record(f"event:{event.get('id') or seq}", "system", event)
            record["title"] = compact_timeline_index_text(event_type.replace("_", " ").title() or "System", 72)
            record["preview"] = text or record["title"]

    leaked_child_lifecycle_keys = [
        str(record.get("key") or "")
        for record in records
        if record.get("kind") == "codex_lifecycle"
        and str(record.get("_codex_thread_id") or "")
        in durable_child_codex_thread_ids
    ]
    for leaked_key in leaked_child_lifecycle_keys:
        leaked_record = by_key.pop(leaked_key, None)
        if leaked_record is None:
            continue
        visible_count = max(
            0,
            visible_count - int(leaked_record.get("event_count") or 0),
        )
        with suppress(ValueError):
            records.remove(leaked_record)
        landmarks_by_key.pop(leaked_key, None)
        dirty_record_keys.discard(leaked_key)
        with suppress(ValueError):
            landmark_order.remove(leaked_key)

    def landmark_from_record(stored: dict[str, Any]) -> dict[str, Any]:
        file_names = list(stored.get("file_names") or [])
        tool_count = int(stored.get("tool_count") or 0)
        thought_count = int(stored.get("thought_count") or 0)
        event_count = int(stored.get("event_count") or 0)
        has_user = bool(stored.get("has_user"))
        prompt = str(stored.get("prompt") or "")
        trace_preview = str(stored.get("trace_preview") or "")
        kind = "user" if has_user and stored["kind"] != "digest" else stored["kind"]
        async_message = stored.get("_async_cross_chat_message") is True
        if async_message:
            kind = "system"
        title = compact_timeline_index_text(
            stored.get("title") or prompt or stored.get("preview") or trace_preview or
            (file_names[0] if file_names else "Agent turn"),
            72,
        )
        preview = compact_timeline_index_text(
            stored.get("preview") or trace_preview or ", ".join(file_names) or prompt or title
        )
        meta_parts: list[str] = []
        if tool_count:
            meta_parts.append(f"{tool_count} tool{'s' if tool_count != 1 else ''}")
        if thought_count:
            meta_parts.append(f"{thought_count} thought{'s' if thought_count != 1 else ''}")
        if file_names:
            meta_parts.append(f"{len(file_names)} file{'s' if len(file_names) != 1 else ''}")
        if kind == "job" and not meta_parts:
            meta_parts.append(f"{event_count} update{'s' if event_count != 1 else ''}")
        landmark = {
            "key": stored["key"],
            "kind": kind,
            "start_seq": stored["_async_message_anchor_seq"] if async_message else stored["start_seq"],
            "end_seq": stored["end_seq"],
            "title": title,
            "preview": preview,
            "meta": " · ".join(meta_parts),
            "timestamp": stored.get("_async_message_anchor_ts") if async_message else stored.get("timestamp"),
        }
        if kind == "job":
            landmark["job_id"] = str(stored.get("job_id") or "")
            landmark["job_timeline_group_id"] = str(
                stored.get("job_timeline_group_id") or stored.get("key") or ""
            )
        return landmark

    if not can_append or not landmarks_by_key:
        dirty_record_keys = set(by_key)
        landmarks_by_key.clear()
        landmark_order = []
    for key in dirty_record_keys:
        stored = by_key.get(key)
        if stored is not None and (
            stored.get("_hidden_imported_prompt_only")
            or stored.get("_async_cross_chat_message") is True and not stored.get("_async_message_anchor_seq")
        ):
            landmarks_by_key.pop(key, None)
            with suppress(ValueError):
                landmark_order.remove(key)
        elif stored is not None:
            landmarks_by_key[key] = landmark_from_record(stored)

    def landmark_key(key: str) -> tuple[int, int, str]:
        item = landmarks_by_key[key]
        return (
            int(item["start_seq"]),
            int(item["end_seq"]),
            str(item["key"]),
        )

    unchanged_order = [
        key
        for key in landmark_order
        if key in landmarks_by_key and key not in dirty_record_keys
    ]
    dirty_order = sorted(
        (
            key
            for key in dirty_record_keys
            if key in landmarks_by_key
        ),
        key=landmark_key,
    )
    merged_order: list[str] = []
    unchanged_index = 0
    dirty_index = 0
    while (
        unchanged_index < len(unchanged_order)
        or dirty_index < len(dirty_order)
    ):
        if dirty_index >= len(dirty_order):
            merged_order.extend(unchanged_order[unchanged_index:])
            break
        if unchanged_index >= len(unchanged_order):
            merged_order.extend(dirty_order[dirty_index:])
            break
        unchanged_key = unchanged_order[unchanged_index]
        dirty_key = dirty_order[dirty_index]
        if landmark_key(unchanged_key) <= landmark_key(dirty_key):
            merged_order.append(unchanged_key)
            unchanged_index += 1
        else:
            merged_order.append(dirty_key)
            dirty_index += 1
    landmark_order = merged_order
    landmarks = [landmarks_by_key[key] for key in landmark_order]

    payload = {
        "session_id": session_id,
        "landmarks": landmarks,
        "latest_seq": latest_seq,
        "event_count": visible_count,
        "generated_at": now_iso(),
    }
    final_stat = path.stat()
    codex_scope_signature = (
        root_codex_thread_id,
        tuple(sorted(child_codex_thread_ids)),
    )
    final_signature = (
        final_stat.st_size,
        final_stat.st_mtime_ns,
        *codex_scope_signature,
        codex_goal_history_signature,
    )
    cache_entry = {
        "projection_version": TIMELINE_INDEX_PROJECTION_VERSION,
        "claude_metadata_signature": claude_metadata_signature,
        "codex_goal_history_signature": codex_goal_history_signature,
        "signature": final_signature,
        "codex_scope_signature": codex_scope_signature,
        "payload": payload,
        "records": records,
        "by_key": by_key,
        "landmarks_by_key": landmarks_by_key,
        "landmark_order": landmark_order,
        "active_turn_key": active_turn_key,
        "current_turn_by_run": current_turn_by_run,
        "job_by_run": job_by_run,
        "job_timeline_group_by_run": job_timeline_group_by_run,
        "job_timeline_group_by_occurrence": job_timeline_group_by_occurrence,
        "job_timeline_group_by_run_start_seq": (
            job_timeline_group_by_run_start_seq
        ),
        "job_timeline_group_by_runless_event_seq": (
            job_timeline_group_by_runless_event_seq
        ),
        "job_timeline_group_keys": job_timeline_group_keys,
        "run_history_records": run_history_records,
        "job_run_keys": job_run_keys,
        "job_run_key_sets": job_run_key_sets,
        "job_timeline_group_run_keys": job_timeline_group_run_keys,
        "job_timeline_group_run_key_sets": job_timeline_group_run_key_sets,
        "run_history_keys_by_run_id": run_history_keys_by_run_id,
        "run_history_key_by_occurrence": run_history_key_by_occurrence,
        "current_run_history_key": current_run_history_key,
        "fork_internal_run_ids": fork_internal_run_ids,
        "internal_status_run_ids": internal_status_run_ids,
        "durable_child_codex_thread_ids": durable_child_codex_thread_ids,
        "visible_count": visible_count,
        "latest_seq": latest_seq,
        "latest_timeline_landmark_key": latest_timeline_landmark_key,
        "offset": final_offset,
        "inode": final_stat.st_ino,
    }
    with TIMELINE_INDEX_CACHE_LOCK:
        TIMELINE_INDEX_CACHE[session_id] = cache_entry
        TIMELINE_INDEX_CACHE.move_to_end(session_id)
        evict_timeline_index_cache_locked()
    return payload


def is_native_goal_steer_event(event: dict[str, Any]) -> bool:
    """An accepted human follow-up, not a new run or provider goal prompt."""
    return bool(
        event.get("type") == "turn_steered"
        and event.get("native_goal_steer") is True
        and event.get("native_steer") is True
        and event.get("provider_user_authored") is True
        and event.get("backend") == BACKEND_CODEX
        and event.get("purpose") == "codex_goal_resume"
        and str(event.get("run_id") or "").strip()
    )


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


def host_pressure_snapshot() -> dict[str, Any]:
    load_1m: float | None = None
    load_per_cpu: float | None = None
    cpu_count = os.cpu_count() or 1
    with suppress(Exception):
        load_1m = float(os.getloadavg()[0])
        load_per_cpu = load_1m / max(cpu_count, 1)

    available_mem_mb: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        with suppress(Exception):
            for line in meminfo.read_text(errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        available_mem_mb = int(int(parts[1]) / 1024)
                    break
    elif sys.platform == "darwin":
        with suppress(Exception):
            available_mem_mb = darwin_available_memory_mb()

    return {
        "load_1m": load_1m,
        "load_per_cpu": load_per_cpu,
        "cpu_count": cpu_count,
        "available_mem_mb": available_mem_mb,
        "job_min_available_mem_mb": JOB_MIN_AVAILABLE_MEM_MB,
        "job_max_active_runs": JOB_MAX_ACTIVE_RUNS,
        "start_min_available_mem_mb": MIN_START_AVAILABLE_MEM_MB,
        "start_max_active_runs": MAX_ACTIVE_AGENT_RUNS,
    }


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


class ArtifactPublicationError(RuntimeError):
    pass


def publication_receipt_path(session_id: str, publication_id: str) -> Path:
    filename = hashlib.sha256(publication_id.encode("utf-8")).hexdigest() + ".json"
    return artifact_publications_dir(session_id) / filename


def load_publication_events(
    session_id: str,
    publication_id: str,
) -> list[dict[str, Any]]:
    path = publication_receipt_path(session_id, publication_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactPublicationError(
            "the durable publication receipt is unreadable"
        ) from exc
    if (
        not isinstance(data, dict)
        or data.get("publication_id") != publication_id
        or not isinstance(data.get("events"), list)
        or not data.get("events")
    ):
        raise ArtifactPublicationError("the durable publication receipt is invalid")
    return list(data["events"])


def recover_publication_events_from_tail(
    session_id: str,
    publication_id: str,
    *,
    max_bytes: int = 16 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Rare recovery path for a lost response before its sidecar was written."""
    path = events_path(session_id)
    if not path.is_file():
        return []
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        start = max(0, size - max_bytes)
        stream.seek(start)
        raw = stream.read()
    if start:
        _, _, raw = raw.partition(b"\n")
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        with suppress(Exception):
            event = json.loads(line.decode("utf-8", errors="replace"))
            if (
                event.get("type") == "artifact_created"
                and event.get("publication_id") == publication_id
            ):
                events.append(event)
    return events


def store_publication_events(
    session_id: str,
    publication_id: str,
    events: list[dict[str, Any]],
) -> None:
    directory = artifact_publications_dir(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = publication_receipt_path(session_id, publication_id)
    temp = directory / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = {
        "version": 1,
        "session_id": session_id,
        "publication_id": publication_id,
        "events": events,
    }
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(OSError):
            temp.unlink()


def verified_publication_events(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    publication_id: str,
    publication_digest: str,
    expected_count: int,
) -> list[dict[str, Any]]:
    if not events:
        return []
    by_index: dict[int, dict[str, Any]] = {}
    for event in events:
        if (
            event.get("publication_id") != publication_id
            or event.get("publication_digest") != publication_digest
            or event.get("run_id") != run_id
            or event.get("publication_count") != expected_count
        ):
            raise ArtifactPublicationError(
                "publication_id is already bound to another artifact batch or run"
            )
        index = event.get("publication_index")
        artifact = event.get("artifact")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= expected_count
            or index in by_index
            or not isinstance(artifact, dict)
            or not str(artifact.get("id") or "").strip()
            or not isinstance(event.get("seq"), int)
            or int(event["seq"]) <= 0
        ):
            raise ArtifactPublicationError(
                "publication_id has an incomplete or invalid durable receipt"
            )
        artifact_id = str(artifact["id"])
        metadata = read_file_meta_record(artifact_id)
        artifact_path = Path(str(artifact.get("path") or ""))
        if (
            not isinstance(metadata, dict)
            or str(metadata.get("session_id") or "")
            != str(event.get("session_id") or "")
            or str(metadata.get("path") or "") != str(artifact_path)
            or not artifact_path.is_file()
        ):
            raise ArtifactPublicationError(
                "publication_id references an unavailable artifact"
            )
        by_index[index] = event
    if set(by_index) != set(range(expected_count)):
        raise ArtifactPublicationError(
            "publication_id has an incomplete durable receipt"
        )
    return [by_index[index] for index in range(expected_count)]


async def send_event_stream_status(ws: WebSocket, session_id: str, phase: str) -> None:
    # Ephemeral, opt-in control frames: never persisted or assigned a seq.
    await asyncio.wait_for(ws.send_json({
        "type": "event_stream_status", "version": 1,
        "session_id": session_id, "phase": phase,
    }), timeout=WEBSOCKET_SEND_TIMEOUT_SECONDS)

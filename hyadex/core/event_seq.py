"""Event sequence numbers and the per-chat event index files."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
import json
import logging.handlers
import os
from pathlib import Path
import stat
import threading
from typing import Any

from hyadex.core.state import (
    EVENT_DELIVERY_LOCKS,
    EVENT_INDEX_VERSION,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
    EVENT_SEQ_REPAIR_LOCKS,
    TIMELINE_INDEX_CACHE,
    TIMELINE_INDEX_CACHE_LOCK,
    TIMELINE_INDEX_LOCK_STRIPES,
)
from hyadex.features.usage import join_task_despite_caller_cancellation

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


def timeline_index_session_lock(session_id: str) -> threading.Lock:
    return TIMELINE_INDEX_LOCK_STRIPES[
        hash(session_id) % len(TIMELINE_INDEX_LOCK_STRIPES)
    ]

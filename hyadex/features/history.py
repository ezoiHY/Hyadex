"""Provider history import and sync into chat timelines."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager, suppress
import ctypes
import fcntl
import hashlib
import hmac
import json
import logging.handlers
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Any, Iterable
import uuid

from fastapi import HTTPException

from hyadex.providers.claude.history_provenance import (
    ClaudeInterruptionTracker,
    normalize_claude_interruption_context,
)
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS,
    CODEX_SESSIONS_ROOT,
    DEFAULT_BACKEND,
    FILES_ROOT,
    FORK_INTERNAL_PURPOSES,
    HISTORY_SYNC_BATCH_BYTES,
    HISTORY_SYNC_EVENT_SCAN_LIMIT,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
    SERVER_INSTANCE_ID,
)
from hyadex.core.event_log import read_events
from hyadex.core.timeline_index import (
    is_native_goal_steer_event,
    project_legacy_imported_provider_event,
)
from hyadex.core.paths import code_diffs_dir, events_path, now_iso, safe_name
from hyadex.core.runs import RUNS
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    event_delivery_lock,
    event_output_text,
    HISTORY_MAINTENANCE_STATE,
    next_event_seq,
    reconcile_event_seq_after_failed_write,
    session_lifecycle_lock,
    STORE,
)
from hyadex.core.session_files import (
    event_establishes_session_file_origin,
    event_files_belong_to_session,
    file_record_belongs_to_session,
    iter_session_events,
    read_file_meta_record,
)
from hyadex.core.event_seq import last_event_seq_from_file
from hyadex.core.session_codex import bounded_codex_output_text
from hyadex.core.state import (
    DELETED_SESSION_TOMBSTONES,
    DELETING_SESSIONS,
    EVENT_SEQ_CACHE,
    EVENT_SEQ_LOCK,
)
from hyadex.core.util import (
    clean_assistant_text,
    contained_jsonl_path,
    ensure_dirs,
    merged_file_ids,
    normalized_file_id,
)
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.claude.history import (
    find_claude_history,
    history_dedup_key,
    HISTORY_SEARCH_DIRTY,
    normalized_history_import_limit,
    normalized_history_provider_origin,
    normalized_history_sync_cursor,
    session_provider_id,
)
from hyadex.providers.codex.history import (
    CODEX_ROLLOUT_PATH_CACHE,
    codex_transcript_meta,
    find_codex_history,
)
from hyadex.core.transcripts import scan_check, SCAN_SECONDS
from hyadex.ops import session_ownership as local_session_ownership

logger = logging.getLogger("hyadex-server")

# Chats with a provider-transcript catch-up already queued or running, so a
# burst of opens cannot start overlapping syncs of the same chat.
HISTORY_SYNC_SCHEDULED: set[str] = set()


def append_imported_events_sync(
    path: Path,
    session_id: str,
    first_seq: int,
    imported_events: list[tuple[str, dict[str, Any]]],
) -> int:
    """Write one rollback-capable imported JSONL batch off the event loop."""
    original_size = path.stat().st_size if path.exists() else 0
    last_seq = first_seq - 1
    try:
        with path.open("a", encoding="utf-8") as stream:
            for event_type, payload in imported_events:
                last_seq += 1
                stored_payload = dict(payload)
                output = stored_payload.get("output")
                if event_type == "tool_finished" and output is not None:
                    output_text = event_output_text(output)
                    if len(output_text) > CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS:
                        stored_payload["output"] = bounded_codex_output_text(output_text)
                        stored_payload["output_chars"] = len(output_text)
                        stored_payload["output_truncated"] = True
                event = {
                    "seq": last_seq,
                    "id": f"evt_{uuid.uuid4().hex[:16]}",
                    "session_id": session_id,
                    "type": event_type,
                    "ts": now_iso(),
                    **stored_payload,
                }
                stream.write(json.dumps(event, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        with suppress(OSError):
            with path.open("r+b") as stream:
                stream.truncate(original_size)
                stream.flush()
                os.fsync(stream.fileno())
        raise
    return last_seq


async def append_imported_events(
    session_id: str,
    imported_events: list[tuple[str, dict[str, Any]]],
) -> int:
    """Transactionally append historical events without live-state side effects.

    A forked chat does not become observable until its endpoint returns, so a
    single locked file append replaces thousands of per-event locks, metadata
    writes, and websocket broadcasts.
    """
    if not imported_events:
        return 0
    async with event_delivery_lock(session_id):
        ensure_dirs(session_id)
        path = events_path(session_id)
        first_seq = await next_event_seq(session_id, path)
        write_task = asyncio.create_task(asyncio.to_thread(
            append_imported_events_sync,
            path,
            session_id,
            first_seq,
            imported_events,
        ))
        try:
            last_seq = await asyncio.shield(write_task)
        except BaseException:
            # asyncio.to_thread cannot be cancelled once the filesystem write
            # has begun. Join it before releasing the per-chat event lock so a
            # caller cancellation cannot race deletion or a later event seq.
            completed_seq: int | None = None
            try:
                completed_seq = await join_task_despite_caller_cancellation(
                    write_task
                )
            except BaseException:
                pass
            if completed_seq is None:
                await reconcile_event_seq_after_failed_write(
                    session_id,
                    path,
                    consumed_high_water=first_seq - 1,
                )
            else:
                async with EVENT_SEQ_LOCK:
                    EVENT_SEQ_CACHE[session_id] = completed_seq
            raise
        async with EVENT_SEQ_LOCK:
            EVENT_SEQ_CACHE[session_id] = last_seq
        HISTORY_SEARCH_DIRTY.add(session_id)
        return len(imported_events)


def clear_imported_active_runs() -> int:
    """Drop persisted active_run records that point at history import runs.

    Older servers set ``active_run`` from imported ``turn_started`` events and
    nothing ever cleared it, so a stopped chat stayed "running" in every
    client until its next real turn.
    """

    cleared = 0
    for session in STORE.sessions.values():
        active = session.get("active_run")
        if isinstance(active, dict) and str(active.get("run_id") or "").startswith("import_"):
            session.pop("active_run", None)
            cleared += 1
    if cleared:
        logger.warning("cleared %d stale imported active_run record(s)", cleared)
    return cleared


@contextmanager
def local_history_import_guard():
    guard = local_session_ownership.history_import_lock()
    try:
        guard.__enter__()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Local history import is busy or unavailable. Retry when the other import has finished.",
        ) from exc
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


HISTORY_SYNC_CHECKPOINT_VERSION = 1


def provider_history_path(sess: dict[str, Any]) -> Path | None:
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    provider_id = session_provider_id(sess)
    if not provider_id:
        return None
    if backend == BACKEND_CLAUDE:
        return find_claude_history(provider_id)
    if backend == BACKEND_CODEX:
        cursor = normalized_history_sync_cursor(sess)
        if cursor and cursor.get("provider_session_id") == provider_id:
            path = contained_jsonl_path(Path(cursor["source_path"]), CODEX_SESSIONS_ROOT)
            if path is not None and codex_transcript_meta(path)[0] == provider_id:
                CODEX_ROLLOUT_PATH_CACHE[provider_id] = path
                return path
        return find_codex_history(provider_id)
    return None


def history_item_cursor_digest(item: dict[str, Any]) -> str:
    identity = [str(item.get("kind") or ""), str(item.get("text") or "").strip()]
    runtime_origin = item.get("provider_origin")
    if item.get("kind") == "assistant":
        origin = normalized_history_provider_origin(runtime_origin)
        if origin is not None and all(origin.get(field) for field in ("event_id", "session_id")):
            # Legacy text-only cursors safely mismatch the first identified
            # boundary row; never discard a newly appended repeated reply.
            identity = ["assistant", "claude", origin["session_id"].lower(), origin["event_id"].lower()]
    if (item.get("kind") == "user" and item.get("provider_runtime_context") in ("subagent_notification", "turn_aborted", "provider_notice")
        and item.get("provider_user_authored") is not True and isinstance(runtime_origin, dict)
        and runtime_origin.get("provider") == "codex" and runtime_origin.get("kind") == item["provider_runtime_context"]
        and all(isinstance(runtime_origin.get(key), str) and runtime_origin[key] for key in ("event_id", "turn_id"))):
        identity = [item["provider_runtime_context"], runtime_origin["event_id"], runtime_origin["turn_id"], identity[1]]
    if item.get("kind") == "interruption":
        origin = normalized_history_provider_origin(item.get("provider_origin"))
        if origin is not None and origin.get("kind") == "interruption":
            identity = ["interruption", origin["session_id"], origin["event_id"]]
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def history_sync_checkpoint(
    previous: dict[str, Any] | None,
    cursor: dict[str, Any],
    *,
    caught_up: bool,
) -> dict[str, Any]:
    """Build the recovery record committed with an imported event batch."""

    stored_cursor = dict(cursor)
    stored_cursor.pop("source_caught_up", None)
    return {
        "version": HISTORY_SYNC_CHECKPOINT_VERSION,
        "previous_present": previous is not None,
        "previous_source_offset": (
            int(previous.get("source_offset") or 0) if previous else 0
        ),
        "previous_source_digest": (
            str(previous.get("source_digest") or "") if previous else ""
        ),
        "caught_up": bool(caught_up),
        "cursor": stored_cursor,
    }


def committed_history_sync_checkpoint(
    sess: dict[str, Any],
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], int] | None:
    """Recover a batch fsynced before its cursor registry save failed."""

    session_id = str(sess.get("id") or "")
    previous_checkpoint_seq = (
        int(previous.get("checkpoint_seq") or 0) if previous else 0
    )
    scan_limit = (
        HISTORY_SYNC_EVENT_SCAN_LIMIT
        + normalized_history_import_limit(None)
        + 10
        if previous is not None
        else MAX_LOCAL_TRANSCRIPT_SCAN_LINES
    )
    events = read_events(
        session_id,
        after=previous_checkpoint_seq,
        limit=scan_limit + 1,
        tail=False,
        cap_to_response_limit=False,
        include_internal=True,
    )
    truncated = len(events) > scan_limit
    if truncated:
        events = events[:scan_limit]
    terminal_seq_by_run = {}
    for event in events:
        seq = event.get("seq")
        run_id = str(event.get("run_id") or "")
        if (
            event.get("type") == "turn_finished"
            and event.get("imported") is True
            and run_id
            and isinstance(seq, int)
            and not isinstance(seq, bool)
        ):
            terminal_seq_by_run[run_id] = seq
    for event in reversed(events):
        if event.get("type") != "history_imported":
            continue
        raw = event.get("_history_sync_checkpoint")
        if (
            not isinstance(raw, dict)
            or raw.get("version") != HISTORY_SYNC_CHECKPOINT_VERSION
            or not isinstance(raw.get("previous_present"), bool)
            or raw["previous_present"] != (previous is not None)
            or not isinstance(raw.get("caught_up"), bool)
        ):
            continue
        run_id = str(event.get("run_id") or "")
        terminal_seq = terminal_seq_by_run.get(run_id, 0)
        checkpoint_seq = event.get("seq")
        if (
            not isinstance(checkpoint_seq, int)
            or isinstance(checkpoint_seq, bool)
            or terminal_seq <= checkpoint_seq
        ):
            continue
        expected_offset = int(previous.get("source_offset") or 0) if previous else 0
        expected_digest = str(previous.get("source_digest") or "") if previous else ""
        if (
            raw.get("previous_source_offset") != expected_offset
            or str(raw.get("previous_source_digest") or "") != expected_digest
        ):
            continue
        candidate_session = dict(sess)
        candidate_session["_history_sync_cursor"] = raw.get("cursor")
        cursor = normalized_history_sync_cursor(candidate_session)
        if cursor is None or int(cursor["source_offset"]) <= expected_offset:
            continue
        path = provider_history_path(sess)
        if path is None:
            return None
        _snapshot, continued = provider_history_source_snapshot(path, cursor)
        if not continued:
            raise ValueError(
                "provider transcript no longer extends the committed history checkpoint"
            )
        if bool(raw.get("caught_up")):
            cursor["timeline_seq"] = max(
                int(cursor.get("timeline_seq") or 0),
                terminal_seq,
            )
            cursor["timeline_pending_through_seq"] = 0
            cursor["timeline_pending_active"] = False
        cursor["checkpoint_seq"] = max(
            int(cursor.get("checkpoint_seq") or 0),
            terminal_seq,
        )
        return cursor, terminal_seq
    if truncated:
        # A bounded search that cannot prove absence must never fall through to
        # content reconciliation: doing so could replay a batch whose durable
        # checkpoint merely fell outside the scan window.
        raise ValueError(
            "history checkpoint search exceeded its bounded event window"
        )
    return None


def provider_history_source_snapshot(
    path: Path,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Hash a stable transcript through its last complete JSONL record."""

    initial = path.stat()
    # Provider CLIs may rotate a JSONL file with atomic replacement or move it
    # between dated directories. Backend/provider identity is validated before
    # this helper; byte-for-byte prefix continuity is authoritative across a
    # path/dev/inode change, while identity metadata remains a fast-path hint.
    verify_offset = (
        int(previous["source_offset"])
        if previous is not None
        and int(previous.get("source_offset") or 0) <= int(initial.st_size)
        else None
    )
    complete_digest = hashlib.sha256()
    prefix_digest = hashlib.sha256() if verify_offset is not None else None
    prefix_hashed = 0
    complete_size = 0
    running_digest = hashlib.sha256()
    deadline = time.monotonic() + SCAN_SECONDS
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        expected_identity = (
            int(initial.st_dev),
            int(initial.st_ino),
            int(initial.st_size),
            int(initial.st_mtime_ns),
        )
        if (
            int(opened.st_dev),
            int(opened.st_ino),
            int(opened.st_size),
            int(opened.st_mtime_ns),
        ) != expected_identity:
            raise ValueError("transcript changed before cursor snapshot")
        # Verify the existing prefix, then prepare at most one bounded suffix.
        # Total file size is not an import limit. Prefix verification remains
        # necessary: provider transcripts can be replaced or rewritten.
        snapshot_limit = min(int(initial.st_size), (verify_offset or 0) + HISTORY_SYNC_BATCH_BYTES)
        remaining = snapshot_limit
        consumed = 0
        while remaining > 0:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("transcript was truncated during cursor snapshot")
            remaining -= len(chunk)
            if prefix_digest is not None and prefix_hashed < int(verify_offset):
                take = min(len(chunk), int(verify_offset) - prefix_hashed)
                prefix_digest.update(chunk[:take])
                prefix_hashed += take
            scan_check(deadline, budget_seconds=SCAN_SECONDS)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                running_digest.update(chunk[:newline + 1])
                complete_digest = running_digest.copy()
                complete_size = consumed + newline + 1
                running_digest.update(chunk[newline + 1:])
            else:
                running_digest.update(chunk)
            consumed += len(chunk)
            # A single streamed record may exceed a batch. Extend only until
            # at least one new complete record exists, under the scan deadline.
            if remaining == 0 and complete_size <= (verify_offset or 0) and consumed < int(initial.st_size):
                remaining = min(1024 * 1024, int(initial.st_size) - consumed)
        finished = os.fstat(stream.fileno())
        if (
            int(finished.st_dev),
            int(finished.st_ino),
            int(finished.st_size),
            int(finished.st_mtime_ns),
        ) != expected_identity:
            raise ValueError("transcript changed during cursor snapshot")

    snapshot = {
        "source_dev": int(initial.st_dev),
        "source_ino": int(initial.st_ino),
        "source_size": int(initial.st_size),
        "source_mtime_ns": int(initial.st_mtime_ns),
        "source_offset": complete_size,
        "source_digest": complete_digest.hexdigest(),
        "expected_stat": {
            "st_dev": int(initial.st_dev),
            "st_ino": int(initial.st_ino),
            "st_size": int(initial.st_size),
            "st_mtime_ns": int(initial.st_mtime_ns),
        },
    }
    continued = bool(
        verify_offset is not None
        and verify_offset <= complete_size
        and prefix_hashed == verify_offset
        and prefix_digest is not None
        and hmac.compare_digest(
            prefix_digest.hexdigest(),
            str(previous.get("source_digest") or "") if previous else "",
        )
    )
    return snapshot, continued


def provider_history_prefix_digest(
    path: Path,
    end: int,
    *,
    expected_stat: dict[str, int],
) -> str:
    digest = hashlib.sha256()
    remaining = end
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if any(
            int(getattr(before, field)) != int(expected_stat[field])
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            raise ValueError("transcript changed before cursor hashing")
        while remaining > 0:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("transcript was truncated during cursor hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(stream.fileno())
        if any(
            int(getattr(after, field)) != int(expected_stat[field])
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        ):
            raise ValueError("transcript changed during cursor hashing")
    return digest.hexdigest()


def seed_claude_interruption_context(
    path: Path,
    end: int,
    *,
    expected_stat: dict[str, int],
    provider_session_id: str,
) -> dict[str, Any]:
    """Seed one legacy cursor from at most 2 MiB ending at its consumed offset."""
    if end < 0 or end > int(expected_stat["st_size"]):
        raise ValueError("transcript interruption seed range is invalid")
    if end == 0:
        return {"version": 1}
    start = max(0, end - 2 * 1024 * 1024)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("transcript interruption seed is not a regular file")
        if any(int(getattr(before, field)) != int(expected_stat[field]) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("transcript changed before interruption seed")
        stream.seek(start)
        region = stream.read(end - start)
        if len(region) != end - start:
            raise ValueError("transcript was truncated during interruption seed")
        after = os.fstat(stream.fileno())
        if any(int(getattr(after, field)) != int(expected_stat[field]) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("transcript changed during interruption seed")
    if start:
        # A leading partial record cannot establish lineage. Discarding an
        # exactly aligned first record too is conservative and keeps reads bounded.
        _partial, _separator, region = region.partition(b"\n")
    if region and not region.endswith(b"\n"):
        raise ValueError("transcript interruption seed does not end on a complete line")
    tracker = ClaudeInterruptionTracker()
    for index, line in enumerate(region.splitlines()):
        if index >= MAX_LOCAL_TRANSCRIPT_SCAN_LINES:
            return {"version": 1}
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            tracker = ClaudeInterruptionTracker()
            continue
        if isinstance(event, dict):
            tracker.consume(event)
        else:
            tracker = ClaudeInterruptionTracker()
    return normalize_claude_interruption_context(tracker.export_context(), provider_session_id=provider_session_id)


def history_message_match_details(
    kind: str, text: Any, metadata: dict[str, Any], *, provider_item: bool = False,
) -> dict[str, Any]:
    """Keep exact text and Claude ownership separate from display cleaning."""
    key = history_dedup_key(kind, text, source_text_sha256=metadata.get("source_text_sha256"))
    details: dict[str, Any] = {"key": key, "backend": str(metadata.get("backend") or ""), "provider_item": provider_item}
    if kind != "assistant":
        return details
    raw_origin = metadata.get("provider_origin")
    if not details["backend"] and isinstance(raw_origin, dict):
        details["backend"] = str(raw_origin.get("provider") or "")
    if details["backend"] not in ("", BACKEND_CLAUDE):
        return details
    origin = normalized_history_provider_origin(metadata.get("provider_origin")) or {}
    if origin or details["backend"] == BACKEND_CLAUDE:
        details["backend"] = BACKEND_CLAUDE
        identity = normalized_history_provider_origin({
            "provider": "claude",
            "event_id": origin.get("event_id") or metadata.get("provider_message_id"),
            "session_id": origin.get("session_id") or metadata.get("provider_session_id"),
        }) or {}
        details["message_id"] = str(identity.get("event_id") or "").lower()
        details["provider_session_id"] = str(identity.get("session_id") or "").lower()
    # Never derive a fallback from a truncated display prefix. Its complete
    # source hash cannot be transformed back into a cleaned-text hash.
    if details["backend"] in ("", BACKEND_CLAUDE) and key == history_dedup_key(kind, text):
        details["canonical_key"] = history_dedup_key(kind, clean_assistant_text(text))
    return details


def history_messages_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["key"][0] != right["key"][0]:
        return False
    claude = BACKEND_CLAUDE in (left.get("backend"), right.get("backend"))
    if claude and left["key"][0] == "assistant":
        if any(side.get("backend") not in (None, "", BACKEND_CLAUDE) for side in (left, right)):
            return False
        sessions = [side.get("provider_session_id") for side in (left, right)]
        if all(sessions) and sessions[0] != sessions[1]:
            return False
        identities = [side.get("message_id") for side in (left, right)]
        if all(identities):
            # A distinct UUID is a distinct occurrence even if text is equal.
            return identities[0] == identities[1]
        if any(identities) and any(side.get("provider_item") and not side.get("message_id") for side in (left, right)):
            # An unidentified source row cannot consume a known live UUID.
            # The fallback is for legacy live projections missing identity.
            return False
    if left["key"] == right["key"]:
        return True
    # Missing identity retains only the pre-existing exact-text fallback.
    # Decoration equivalence alone cannot establish ownership of a reply.
    return False


def history_message_match_tokens(details: dict[str, Any]) -> list[tuple[str, ...]]:
    kind, digest = details["key"]
    tokens = [(kind, "text", digest)]
    if details.get("message_id"):
        tokens.insert(0, (kind, "claude_id", details["message_id"]))
    if details.get("canonical_key", ("", ""))[1]:
        tokens.append((kind, "claude_clean", details["canonical_key"][1]))
    return tokens


def history_timeline_message_keys(
    session_id: str,
    *,
    timeline_after_seq: int,
    timeline_through_seq: int,
    tail: bool,
    include_imported: bool,
    message_details: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[tuple[int, tuple[str, str]]], bool, bool]:
    """Scan a fixed timeline boundary while bounding message credits only.

    Event files commonly contain far more tool/status rows than chat messages.
    Applying the history limit to raw rows can therefore either wedge a valid
    cursor forever or hide the last usable first-sync anchor.  Pin one file
    generation and stream every row through ``timeline_through_seq`` while
    retaining at most the configured number of message keys.
    """

    after_seq = max(0, int(timeline_after_seq))
    through_seq = max(after_seq, int(timeline_through_seq))
    if through_seq == after_seq:
        return [], False, False
    path = events_path(session_id)
    try:
        initial = path.stat()
    except FileNotFoundError:
        raise ValueError("timeline disappeared during history reconciliation")
    maximum = max(1, int(HISTORY_SYNC_EVENT_SCAN_LIMIT))
    selected: list[dict[str, Any]] | deque[dict[str, Any]]
    selected = deque(maxlen=maximum) if tail else []
    provider_runs: dict[str, str] = {}
    prior_message: dict[str, Any] | None = None
    timeline_has_messages = False
    front_window_truncated = False
    last_seq = 0
    remaining = int(initial.st_size)
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (
            int(opened.st_dev) != int(initial.st_dev)
            or int(opened.st_ino) != int(initial.st_ino)
            or int(opened.st_size) < remaining
        ):
            raise ValueError("timeline changed before history reconciliation")
        while remaining > 0:
            raw_line = source.readline(
                min(MAX_LOCAL_TRANSCRIPT_LINE_BYTES + 1, remaining)
            )
            if not raw_line:
                raise ValueError("timeline was truncated during history reconciliation")
            remaining -= len(raw_line)
            if (
                len(raw_line) > MAX_LOCAL_TRANSCRIPT_LINE_BYTES
                and not raw_line.endswith(b"\n")
            ):
                raise ValueError("timeline event exceeds the history scan limit")
            if not raw_line.endswith(b"\n"):
                # A concurrent append may have exposed an incomplete final
                # record to the stat snapshot. It cannot own a committed seq.
                break
            if not raw_line.strip():
                raise ValueError(
                    "timeline contains an empty event during history sync"
                )
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError(
                    "timeline contains a malformed event during history sync"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(
                    "timeline contains a non-object event during history sync"
                )
            raw_seq = event.get("seq")
            if (
                isinstance(raw_seq, bool)
                or not isinstance(raw_seq, int)
                or raw_seq <= 0
                or raw_seq <= last_seq
            ):
                raise ValueError(
                    "timeline event sequence is invalid during history sync"
                )
            last_seq = raw_seq
            if raw_seq > through_seq:
                break
            if not event_files_belong_to_session(event, session_id):
                continue
            event_type = event.get("type")
            run_id = str(event.get("run_id") or "")
            if run_id and event_type == "provider_session" and event.get("backend") == BACKEND_CLAUDE:
                provider_runs[run_id] = str(event.get("provider_session_id") or "")
                if len(provider_runs) > maximum:
                    provider_runs.pop(next(iter(provider_runs)))
            before_window = raw_seq <= after_seq
            if event_type == "turn_started" or is_native_goal_steer_event(event):
                key = history_dedup_key("user", event.get("prompt"), source_text_sha256=event.get("source_text_sha256"))
            elif event_type == "assistant_text":
                key = history_dedup_key("assistant", event.get("text"), source_text_sha256=event.get("source_text_sha256"))
            elif (
                event_type == "reasoning_summary"
                and event.get("phase") == "commentary"
                and event.get("backend") in (None, "", BACKEND_CLAUDE, BACKEND_CODEX)
            ):
                # Explicit public commentary from either provider corresponds
                # to assistant transcript records, unlike private reasoning.
                # Count these public text blocks as
                # ownership credits so the next history sync cannot import
                # this chat's own progress back as duplicate messages.
                # Older native streams omitted backend on progress events;
                # explicit public phase still establishes assistant content.
                key = history_dedup_key("assistant", event.get("text"), source_text_sha256=event.get("source_text_sha256"))
            elif tail and event_type in {"turn_finished", "job_summary"}:
                # A compacted scheduled run can retain only its canonical
                # result event.  The provider transcript still contains that
                # same assistant message, so ignoring the result leaves the
                # first history sync anchored to an older ordinary turn and
                # re-imports every intervening cron run as raw chat output.
                # Final results are used only for the conservative first-sync
                # tail anchor; cursor reconciliation keeps its one-credit-per-
                # provider-message contract unchanged.
                result_text = event.get("result_text")
                if not isinstance(result_text, str) or not result_text.strip():
                    continue
                key = history_dedup_key("assistant", result_text, source_text_sha256=event.get("source_text_sha256"))
            else:
                continue
            if not before_window:
                timeline_has_messages = True
            if not include_imported and event.get("imported") is True:
                # Messages written by an earlier history-import batch are not
                # evidence that Hyadex authored matching provider input.
                # Its exact source checkpoint, not content, makes crash replay
                # idempotent.
                continue
            text = event.get("prompt") if key[0] == "user" else event.get(
                "result_text" if event_type in {"turn_finished", "job_summary"} else "text"
            )
            metadata = {**event}
            if run_id in provider_runs and not event.get("imported"):
                metadata["backend"] = metadata.get("backend") or BACKEND_CLAUDE
                metadata["provider_session_id"] = metadata.get("provider_session_id") or provider_runs[run_id]
            details = history_message_match_details(key[0], text, metadata)
            details.update(seq=raw_seq, run_id=run_id)
            if details.get("message_id") and details.get("canonical_key"):
                # Hash block joins incrementally: retaining complete text for
                # every ownership credit would undo the scanner's memory cap.
                details["_block_hashes"] = [hashlib.sha256(value.encode("utf-8", errors="surrogatepass")) for value in (
                    " ".join(str(text or "").split()), " ".join(clean_assistant_text(text).split()),
                )]
            previous = (selected[-1] if selected else prior_message) if not front_window_truncated else None
            if previous and run_id and previous.get("run_id") == run_id and key[0] == "assistant":
                same_identity = bool(
                    details.get("message_id") and previous.get("message_id")
                    and history_messages_match(previous, details)
                )
                final_alias = bool(
                    event_type in {"assistant_text", "turn_finished", "job_summary"}
                    and previous.get("backend") == BACKEND_CLAUDE
                    and history_messages_match(previous, details)
                )
                if same_identity or final_alias:
                    # SDK TextBlocks and its terminal echo are projections of
                    # one provider message, not extra occurrence credits.
                    if same_identity and event_type == "reasoning_summary":
                        hashes = previous.get("_block_hashes")
                        if hashes and details.get("_block_hashes"):
                            for digest, value in zip(hashes, (
                                " ".join(str(text or "").split()), " ".join(clean_assistant_text(text).split()),
                            )):
                                digest.update((" " + value).encode("utf-8", errors="surrogatepass"))
                            previous["key"] = ("assistant", hashes[0].hexdigest())
                            previous["canonical_key"] = ("assistant", hashes[1].hexdigest())
                        else:
                            previous["key"] = ("assistant", "")
                            previous.pop("canonical_key", None)
                    previous["seq"] = raw_seq
                    continue
            if before_window:
                # A later terminal echo of this already-consumed projection
                # must not create a new ownership credit after the watermark.
                prior_message = details
                continue
            if not tail and len(selected) >= maximum:
                # A valid cursor consumes ownership credits from the front.
                # Keep validating the complete fixed physical snapshot, but
                # leave later credits for the next pass so the durable
                # timeline watermark advances monotonically with the provider
                # byte cursor instead of wedging forever at this boundary.
                front_window_truncated = True
                continue
            selected.append(details)
    if last_seq < through_seq:
        raise ValueError("timeline changed during bounded history reconciliation")
    if message_details is not None:
        message_details.update((entry["seq"], {key: value for key, value in entry.items() if not key.startswith("_")}) for entry in selected)
    return [(entry["seq"], entry["key"]) for entry in selected], timeline_has_messages, front_window_truncated


def reconcile_cursor_history_items(
    session_id: str,
    items: list[dict[str, str]],
    *,
    timeline_after_seq: int,
    timeline_through_seq: int,
    bootstrap: bool = False,
) -> tuple[list[dict[str, str]], int]:
    """Subtract timeline-owned occurrences from one exact provider delta.

    Once a durable byte cursor exists, every item in ``items`` was appended
    after the prior provider position. Timeline messages after the cursor are
    ownership credits, not a suffix anchor: consume each credit at most once
    in chronological order and retain unmatched provider items before,
    between, and after them. This is what preserves an external exchange that
    precedes a later Hyadex-owned turn in the same provider delta.

    A credit that carries Claude's message id can only ever match the record
    with that id, so it is matched by id wherever it falls and never blocks
    the in-order walk. Claude can deliver one message again in a later turn's
    stream (the result of a ``/goal clear`` sent mid-turn arrives inside the
    next ``/compact`` turn): that live copy sits out of transcript order, and
    its record may even precede this delta. Holding the walk on it made every
    later message of the delta look new and be imported a second time.
    """

    after_seq = max(0, int(timeline_after_seq))
    through_seq = max(after_seq, int(timeline_through_seq))
    message_details: dict[int, dict[str, Any]] = {}
    (
        timeline_messages,
        _timeline_has_messages,
        timeline_window_truncated,
    ) = history_timeline_message_keys(
        session_id,
        timeline_after_seq=after_seq,
        timeline_through_seq=through_seq,
        tail=False,
        include_imported=bootstrap,
        message_details=message_details,
    )

    def credit(index: int) -> dict[str, Any]:
        seq, key = timeline_messages[index]
        return message_details.get(seq, {"key": key})

    by_message_id: dict[str, list[int]] = {}
    for index in range(len(timeline_messages)):
        message_id = credit(index).get("message_id")
        if message_id:
            by_message_id.setdefault(message_id, []).append(index)
    consumed: set[int] = set()
    fresh: list[dict[str, str]] = []
    timeline_index = 0
    last_match_index = -1
    for item_index, item in enumerate(items):
        item_details = history_message_match_details(item.get("kind", ""), item.get("text", ""), item, provider_item=True)
        matched = next((
            index for index in by_message_id.get(item_details.get("message_id") or "", ())
            if index not in consumed and history_messages_match(item_details, credit(index))
        ), None)
        if matched is None:
            while timeline_index < len(timeline_messages) and (
                timeline_index in consumed or credit(timeline_index).get("message_id")
            ):
                timeline_index += 1
            if (
                timeline_index < len(timeline_messages)
                and history_messages_match(item_details, credit(timeline_index))
            ):
                matched = timeline_index
                timeline_index += 1
        if matched is None:
            fresh.append(item)
        else:
            consumed.add(matched)
            last_match_index = item_index

    # The watermark covers the consumed prefix of credits. An unconsumed
    # message-id credit before a consumed one is a re-delivered copy whose
    # record was already read; an unconsumed credit without an id still stops
    # it, so a capped pass leaves that credit for the next pass.
    consumed_seq = after_seq
    for index, (seq, _key) in enumerate(timeline_messages):
        if index in consumed:
            consumed_seq = seq
        elif not credit(index).get("message_id"):
            break
    if len(consumed) == len(timeline_messages):
        # All non-import ownership credits in the fixed window were consumed.
        # Advancing across intervening non-message/import events is safe. If
        # the credit window filled, however, later credits deliberately belong
        # to the next pass; stop at the last selected credit rather than
        # claiming the entire fixed boundary.
        consumed_seq = (
            timeline_messages[-1][0]
            if timeline_window_truncated and timeline_messages
            else through_seq
        )
    if bootstrap:
        # Initial backfill uses the same conservative tail semantics as a
        # single full import. Old imported rows are credits too; keep only
        # source items after the entire captured preexisting timeline.
        fresh = items[last_match_index + 1:] if consumed_seq == through_seq and not timeline_window_truncated else []
    return fresh, consumed_seq


def unsynced_history_items(
    session_id: str,
    items: list[dict[str, str]],
    *,
    timeline_after_seq: int = 0,
    timeline_through_seq: int | None = None,
) -> list[dict[str, str]]:
    """Return only the transcript tail this chat's timeline has not shown.

    A provider transcript holds the whole conversation, including turns this
    server itself ran, so re-importing it wholesale duplicates everything.
    This walks the transcript against messages already on the timeline and
    keeps only what follows the last message we recognize.

    Deliberately anchored to the *last* match rather than to every unmatched
    item: if one mid-history message fails to match (cleaning differences, an
    edited transcript), the safe outcome is importing nothing extra rather
    than splicing a duplicate into the middle of the conversation.
    """

    after_seq = max(0, int(timeline_after_seq))
    if after_seq > 0:
        if timeline_through_seq is None:
            raise ValueError("cursor history reconciliation requires a timeline bound")
        return reconcile_cursor_history_items(
            session_id,
            items,
            timeline_after_seq=after_seq,
            timeline_through_seq=int(timeline_through_seq),
        )[0]

    through_seq = (
        max(0, int(timeline_through_seq))
        if timeline_through_seq is not None
        else last_event_seq_from_file(events_path(session_id))
    )
    message_details: dict[int, dict[str, Any]] = {}
    (
        timeline_messages,
        timeline_has_messages,
        _timeline_window_truncated,
    ) = history_timeline_message_keys(
        session_id,
        timeline_after_seq=after_seq,
        timeline_through_seq=through_seq,
        # A first/invalid-cursor sync needs the newest bounded anchor. A valid
        # cursor instead needs every occurrence credit after its watermark;
        # silently tail-truncating those credits can hide a later duplicate.
        tail=True,
        include_imported=True,
        message_details=message_details,
    )
    timeline_details = [message_details.get(seq, {"key": key}) for seq, key in timeline_messages]

    transcript_details = [
        history_message_match_details(item.get("kind", ""), item.get("text", ""), item, provider_item=True)
        for item in items
    ]
    # Content alone cannot distinguish an original ``A, B`` prefix from the
    # newest copy in ``A, B, A, B``.  Prefer the exact prefix when the whole
    # bounded timeline aligns there: the later complete exchange is then a
    # real transcript append and must remain visible.  If the timeline is only
    # a suffix (for example ``Q, A`` within ``A, Q, A``), fall through to the
    # newest-backward alignment below so an older repeated answer is not
    # imported ahead of the already-shown current turn.
    if (
        timeline_details
        and len(timeline_details) <= len(transcript_details)
        and all(history_messages_match(native, source) for native, source in zip(timeline_details, transcript_details))
    ):
        return items[len(timeline_details):]

    last_matched = -1
    # Align the newest bounded timeline tail to the transcript in reverse
    # chronological order.  Preserving order is essential: a bare occurrence
    # counter cannot distinguish an already-shown ``A, B`` prefix from the
    # still-unseen repeated ``A`` in ``A, B, A``.  Greedily selecting the
    # newest occurrence that remains before the prior match handles repeated
    # text without letting an old duplicate consume the current-tail anchor.
    transcript_positions: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, details in enumerate(transcript_details):
        for token in history_message_match_tokens(details):
            transcript_positions[token].append(index)
    transcript_index = len(items) - 1
    for details in reversed(timeline_details):
        candidates: set[int] = set()
        matched_index = -1
        for token in history_message_match_tokens(details):
            positions = transcript_positions.get(token, [])
            while positions and positions[-1] > transcript_index:
                positions.pop()
            if token[1] == "claude_id":
                matched_index = next((index for index in reversed(positions)
                    if history_messages_match(details, transcript_details[index])), -1)
                if matched_index >= 0:
                    break
            candidates.update(positions)
        if matched_index < 0:
            matched_index = next((index for index in sorted(candidates, reverse=True)
                if history_messages_match(details, transcript_details[index])), -1)
        if matched_index < 0:
            continue
        last_matched = max(last_matched, matched_index)
        transcript_index = matched_index - 1
    if last_matched == -1 and timeline_has_messages and items:
        # The timeline already has a conversation but none of the transcript
        # matched it. Importing the whole transcript here would duplicate the
        # chat wholesale; the safe outcome is to import nothing.
        logger.warning(
            "provider history sync found no anchor session=%s transcript_items=%d; "
            "skipping import to avoid duplicating the timeline",
            session_id,
            len(items),
        )
        return []
    return items[last_matched + 1:]


class HistoryPreparationStale(RuntimeError):
    """The read-only plan lost its ownership or immutable input generation."""


def history_file_stamp(path: Path) -> tuple[int, int, int, int] | None:
    try:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns
    except FileNotFoundError:
        return None


@asynccontextmanager
async def history_commit_guard(sess, cursor, timeline_stamp, source_stamp, enabled):
    if not enabled:
        yield
        return
    session_id = str(sess["id"])
    async with session_lifecycle_lock(session_id):
        current = STORE.sessions.get(session_id)
        if (not current or session_id in DELETING_SESSIONS
                or session_id in DELETED_SESSION_TOMBSTONES
                or session_id in RUNS.busy or RUNS.active.get(session_id) is not None
                or session_id in RUNS.maintenance
                or any(not task.done() for task in RUNS.turn_tasks.get(session_id, ()))
                or current.get("backend") != sess.get("backend")
                or session_provider_id(current) != session_provider_id(sess)
                or normalized_history_sync_cursor(current) != cursor
                or history_file_stamp(events_path(session_id)) != timeline_stamp):
            raise HistoryPreparationStale("chat changed during history preparation")
        if source_stamp is not None:
            path, size, mtime, dev, ino = source_stamp
            if history_file_stamp(Path(path)) != (dev, ino, size, mtime):
                raise HistoryPreparationStale("provider transcript changed during history preparation")
        yield


def provider_history_source_stamp(sess: dict[str, Any]) -> list[Any] | None:
    """Return the current path/stat identity of the provider transcript."""

    path = provider_history_path(sess)
    if not path:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return [
        str(path),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_dev),
        int(stat.st_ino),
    ]


def history_cursor_matches_source_stamp(
    cursor: dict[str, Any] | None,
    stamp: list[Any] | None,
) -> bool:
    return bool(
        cursor is not None
        and stamp is not None
        and len(stamp) >= 5
        and str(cursor.get("source_path") or "") == str(stamp[0])
        and int(cursor.get("source_offset") or 0) == int(stamp[1])
        and int(cursor.get("source_mtime_ns") or 0) == int(stamp[2])
        and int(cursor.get("source_dev") or -1) == int(stamp[3])
        and int(cursor.get("source_ino") or -1) == int(stamp[4])
    )


async def persist_history_sync_cursor(
    sess: dict[str, Any],
    cursor: dict[str, Any],
    *,
    timeline_seq: int,
) -> bool:
    """Durably advance a cursor only while its provider identity is current."""

    session_id = str(sess.get("id") or "")
    backend = str(sess.get("backend") or DEFAULT_BACKEND).strip().lower()
    provider_id = str(session_provider_id(sess) or "")
    stored = {
        **cursor,
        "timeline_seq": max(0, int(timeline_seq)),
    }
    stored.pop("source_caught_up", None)
    async with STORE.transaction():
        current = STORE.sessions.get(session_id)
        if (
            not current
            or str(current.get("backend") or DEFAULT_BACKEND).strip().lower()
            != backend
            or str(session_provider_id(current) or "") != provider_id
        ):
            return False
        previous = current.get("_history_sync_cursor")
        current["_history_sync_cursor"] = stored
        current.pop("_history_sync_source_stamp", None)
        try:
            await STORE.save(durable=True)
        except asyncio.CancelledError:
            # SessionStore.save joins its non-cancellable writer before
            # propagating cancellation, so ``stored`` is already authoritative
            # on disk. Retaining it in memory keeps both views identical.
            raise
        except Exception:
            if previous is None:
                current.pop("_history_sync_cursor", None)
            else:
                current["_history_sync_cursor"] = previous
            raise
    return True


HISTORY_MAINTENANCE_SLOTS = asyncio.Semaphore(2)
HISTORY_MAINTENANCE_TASKS: dict[str, asyncio.Task] = {}
HISTORY_PROJECTION_REVISIONS: dict[str, int] = {}


def history_projection_notice(session_id: str) -> dict[str, Any]:
    return {"type": "history_projection_changed", "session_id": session_id,
            "revision": f"{SERVER_INSTANCE_ID}:{HISTORY_PROJECTION_REVISIONS.get(session_id, 0)}"}


def history_maintenance_key(sess: dict[str, Any]) -> list[Any]:
    provider_id = session_provider_id(sess)
    cursor = normalized_history_sync_cursor(sess)
    source = (Path(cursor["source_path"]) if cursor else CODEX_ROLLOUT_PATH_CACHE.get(provider_id))
    return [2, sess.get("backend"), provider_id,
            list(history_file_stamp(events_path(str(sess["id"]))) or ()),
            str(source) if source else None,
            list(history_file_stamp(source) or ()) if source else None]


def history_maintenance_state(session_id: str) -> dict[str, Any]:
    if session_id not in HISTORY_MAINTENANCE_STATE:
        try:
            raw = json.loads((events_path(session_id).parent / "history-maintenance.json").read_text())
            HISTORY_MAINTENANCE_STATE[session_id] = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            HISTORY_MAINTENANCE_STATE[session_id] = {}
    return HISTORY_MAINTENANCE_STATE[session_id]


def save_history_maintenance_state(session_id: str, value: dict[str, Any]) -> None:
    path = events_path(session_id).parent / "history-maintenance.json"
    if not path.parent.is_dir():
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":")))
    temporary.replace(path)


def imported_history_terminal_event(
    run_id: str,
    backend: str,
    *,
    metadata_only: bool = False,
    source_timestamp: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Close an import run so no client can mistake replayed history for a live turn."""

    return ("turn_finished", {
        "run_id": run_id,
        "backend": backend,
        "imported": True,
        "result_text": "",
        "message": "Imported history replay finished.",
        **({"metadata_only": True} if metadata_only else {}),
        # The synthetic boundary is not evidence that historical work ran
        # until import time. Callers pass only validated source timestamps.
        **({"ts": source_timestamp} if source_timestamp is not None else {}),
    })


FORK_HISTORY_EVENT_TYPES = {
    "turn_started",
    "turn_steered",
    "assistant_text",
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "turn_finished",
    # Interrupted Codex commentary is promoted into durable assistant chat by
    # this lifecycle event. Omitting it leaves the copied text trapped in the
    # folded reasoning trace.
    "turn_stopped",
    # Files remain chat-owned. These events are copied only after their file
    # records have been cloned and rebound to the child session below.
    "file_uploaded",
    "artifact_created",
    "artifact_error",
    "code_diff",
    "error",
    # These are user-visible timeline landmarks rather than live provider
    # state, so retaining them preserves the chronology of the conversation.
    "codex_compaction_started",
    "codex_compaction_completed",
    "claude_compaction_completed",
}


def fork_file_source_path(file_id: str, record: dict[str, Any]) -> Path | None:
    """Resolve one registered file without trusting an arbitrary metadata path."""
    clean_file_id = normalized_file_id(file_id)
    if not clean_file_id:
        return None
    file_root = FILES_ROOT / clean_file_id
    try:
        registry_root = FILES_ROOT.resolve(strict=True)
        resolved_root = file_root.resolve(strict=True)
    except OSError:
        return None
    # Reject a registry-entry symlink even when it points to another otherwise
    # valid file directory. Ownership is scoped to the literal registered ID.
    if resolved_root != registry_root / clean_file_id:
        return None

    raw_path = str(record.get("path") or "").strip()
    if raw_path:
        with suppress(OSError, ValueError):
            candidate = Path(raw_path).resolve(strict=True)
            candidate.relative_to(resolved_root)
            if candidate.is_file() and candidate.name != "meta.json":
                return candidate

    with suppress(OSError):
        candidates: list[Path] = []
        for candidate in file_root.iterdir():
            if candidate.name == "meta.json":
                continue
            with suppress(OSError, ValueError):
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(resolved_root)
                if resolved.is_file():
                    candidates.append(resolved)
        if len(candidates) == 1:
            return candidates[0]
    return None


def copy_fork_file_data(source: Path, destination: Path) -> None:
    """Create an isolated copy, preferring copy-on-write where available."""
    if sys.platform == "darwin":
        with suppress(Exception):
            clonefile = ctypes.CDLL(None, use_errno=True).clonefile
            clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
            clonefile.restype = ctypes.c_int
            if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
                shutil.copystat(source, destination)
                return
            with suppress(OSError):
                destination.unlink()
    elif sys.platform.startswith("linux"):
        # Linux FICLONE creates a reflink: independent file semantics without
        # eagerly duplicating a potentially multi-gigabyte upload.
        with suppress(Exception):
            with source.open("rb") as source_stream, destination.open("xb") as destination_stream:
                fcntl.ioctl(destination_stream.fileno(), 0x40049409, source_stream.fileno())
            shutil.copystat(source, destination)
            return
        with suppress(OSError):
            destination.unlink()
    # Hardlinks are intentionally not used: attachment paths are visible to
    # local agents, so an in-place write must never mutate the parent snapshot.
    shutil.copy2(source, destination)


def delete_cloned_fork_code_diffs(
    child_id: str,
    filenames: Iterable[str],
) -> None:
    root = code_diffs_dir(child_id)
    for filename in filenames:
        if Path(filename).name != filename:
            continue
        with suppress(OSError):
            (root / filename).unlink()


def clone_fork_code_diffs(
    parent_id: str,
    child_id: str,
    run_ids: Iterable[str],
) -> list[str]:
    """Copy every visible parent diff into the child's ownership root."""

    source_root = code_diffs_dir(parent_id)
    destination_root = code_diffs_dir(child_id)
    destination_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    temporary: list[Path] = []
    try:
        for run_id in dict.fromkeys(str(value or "") for value in run_ids):
            if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
                raise RuntimeError("fork history contains an invalid code-diff run id")
            source_patch = source_root / f"{run_id}.patch"
            try:
                patch_metadata = source_patch.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f"fork history code diff is unavailable: {run_id}"
                ) from exc
            if not stat.S_ISREG(patch_metadata.st_mode):
                raise RuntimeError(
                    f"fork history code diff is not a regular file: {run_id}"
                )
            for suffix, required in (("patch", True), ("json", False)):
                source = source_root / f"{run_id}.{suffix}"
                if not source.exists():
                    if required:
                        raise RuntimeError(
                            f"fork history code diff is unavailable: {run_id}"
                        )
                    continue
                if not stat.S_ISREG(source.lstat().st_mode):
                    raise RuntimeError(
                        f"fork history code diff metadata is not regular: {run_id}"
                    )
                filename = f"{run_id}.{suffix}"
                destination = destination_root / filename
                staged = destination_root / f".{filename}-{uuid.uuid4().hex}.tmp"
                temporary.append(staged)
                copy_fork_file_data(source, staged)
                staged.chmod(0o600)
                staged.replace(destination)
                temporary.remove(staged)
                copied.append(filename)
        return copied
    except BaseException:
        delete_cloned_fork_code_diffs(child_id, copied)
        raise
    finally:
        for staged in temporary:
            with suppress(OSError):
                staged.unlink()


async def clone_fork_code_diffs_async(
    parent_id: str,
    child_id: str,
    run_ids: Iterable[str],
) -> list[str]:
    clone_task = asyncio.create_task(asyncio.to_thread(
        clone_fork_code_diffs,
        parent_id,
        child_id,
        list(run_ids),
    ))
    try:
        return await asyncio.shield(clone_task)
    except BaseException:
        copied: list[str] = []
        try:
            copied = await join_task_despite_caller_cancellation(clone_task)
        except BaseException:
            pass
        if copied:
            cleanup_task = asyncio.create_task(asyncio.to_thread(
                delete_cloned_fork_code_diffs,
                child_id,
                copied,
            ))
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
        raise


def clone_fork_file_record(
    parent_id: str,
    child_id: str,
    source_record: dict[str, Any],
) -> dict[str, Any] | None:
    """Atomically clone one parent-owned file into the child ownership scope."""
    source_id = normalized_file_id(source_record.get("id"))
    if not source_id or not file_record_belongs_to_session(source_record, parent_id):
        return None
    metadata = read_file_meta_record(source_id)
    if not isinstance(metadata, dict):
        return None
    metadata_owner = str(metadata.get("session_id") or "").strip()
    if metadata_owner and metadata_owner != parent_id:
        return None
    source = fork_file_source_path(source_id, metadata)
    if source is None:
        return None

    kind = str(metadata.get("kind") or source_record.get("kind") or "artifact")
    prefix = "file" if kind == "upload" else "art"
    cloned_id = f"{prefix}_{uuid.uuid4().hex[:16]}"
    filename = safe_name(
        str(metadata.get("filename") or source_record.get("filename") or source.name)
    )
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    final_root = FILES_ROOT / cloned_id
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{cloned_id}-", dir=str(FILES_ROOT))
    )
    try:
        temporary_file = temporary_root / filename
        copy_fork_file_data(source, temporary_file)
        cloned = {
            **metadata,
            **source_record,
            "id": cloned_id,
            "session_id": child_id,
            "kind": kind,
            "filename": filename,
            "path": str(final_root / filename),
            "size": temporary_file.stat().st_size,
            "forked_from_file_id": source_id,
            "forked_from_session_id": parent_id,
        }
        (temporary_root / "meta.json").write_text(
            json.dumps(cloned, indent=2),
            encoding="utf-8",
        )
        temporary_root.replace(final_root)
        return cloned
    except Exception:
        with suppress(OSError):
            shutil.rmtree(temporary_root)
        raise


async def clone_fork_file_record_async(
    parent_id: str,
    child_id: str,
    source_record: dict[str, Any],
) -> dict[str, Any] | None:
    """Join an in-flight clone before propagating caller cancellation."""
    clone_task = asyncio.create_task(asyncio.to_thread(
        clone_fork_file_record,
        parent_id,
        child_id,
        source_record,
    ))
    try:
        return await asyncio.shield(clone_task)
    except BaseException:
        cloned: dict[str, Any] | None = None
        try:
            cloned = await join_task_despite_caller_cancellation(clone_task)
        except BaseException:
            pass
        cloned_id = normalized_file_id((cloned or {}).get("id"))
        if cloned_id:
            cleanup_task = asyncio.create_task(asyncio.to_thread(
                shutil.rmtree,
                FILES_ROOT / cloned_id,
                True,
            ))
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
        raise


def delete_cloned_fork_files(
    cloned_files: dict[str, dict[str, Any]],
) -> None:
    for cloned in cloned_files.values():
        cloned_id = normalized_file_id(cloned.get("id"))
        if cloned_id:
            with suppress(OSError):
                shutil.rmtree(FILES_ROOT / cloned_id)


def fork_source_file_records(
    parent_id: str,
    parent_events: list[dict[str, Any]],
    referenced_file_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return only file records whose parent-chat origin can be established."""
    event_records: dict[str, dict[str, Any]] = {}
    for event in parent_events:
        if not event_establishes_session_file_origin(event, parent_id):
            continue
        for key in ("file", "artifact"):
            record = event.get(key)
            if not isinstance(record, dict):
                continue
            file_id = normalized_file_id(record.get("id"))
            if not file_id or not file_record_belongs_to_session(record, parent_id):
                continue
            metadata = read_file_meta_record(file_id)
            metadata_owner = str((metadata or {}).get("session_id") or "").strip()
            if metadata_owner and metadata_owner != parent_id:
                continue
            event_records[file_id] = {**(metadata or {}), **record}

    # Resolve only files referenced by copied events. Scanning the complete
    # global file registry made one chat fork scale with every chat's files.
    records: dict[str, dict[str, Any]] = {}
    for file_id in referenced_file_ids:
        metadata = read_file_meta_record(file_id)
        metadata_owner = str((metadata or {}).get("session_id") or "").strip()
        if metadata_owner == parent_id:
            records[file_id] = dict(metadata or {})
        elif not metadata_owner and file_id in event_records:
            records[file_id] = event_records[file_id]
    return records


def fork_event_file_ids(event: dict[str, Any]) -> list[str]:
    file_ids = list(event.get("file_ids") or []) if isinstance(event.get("file_ids"), list) else []
    for key in ("file", "artifact"):
        record = event.get(key)
        if isinstance(record, dict):
            file_ids.append(str(record.get("id") or ""))
    return merged_file_ids(file_ids)


async def copy_fork_history(
    parent_id: str,
    child_id: str,
    *,
    source_events: list[dict[str, Any]] | None = None,
) -> int:
    # Fork history copy is an internal clone operation, not an API page. Do not
    # route it through read_events(), which clamps responses for UI pagination.
    stored_parent_events = source_events if source_events is not None else await asyncio.to_thread(
        lambda: list(iter_session_events(parent_id))
    )
    parent_events = [
        # Internal clone keeps the hidden-boundary marker durable so the
        # child's semantic index omits this empty provider-only turn while
        # still routing a following imported assistant event correctly.
        project_legacy_imported_provider_event(event, parent_id)
        for event in stored_parent_events
    ]
    internal_run_ids = {
        str(event.get("run_id"))
        for event in parent_events
        if event.get("purpose") in FORK_INTERNAL_PURPOSES and str(event.get("run_id") or "").strip()
    }
    eligible_events = [
        event
        for event in parent_events
        if event.get("purpose") not in FORK_INTERNAL_PURPOSES
        and str(event.get("run_id") or "").strip() not in internal_run_ids
        and event.get("type") in FORK_HISTORY_EVENT_TYPES
    ]
    assistant_runs = {
        str(event.get("run_id") or "").strip()
        for event in eligible_events
        if event.get("type") == "assistant_text"
        and str(event.get("run_id") or "").strip()
        and str(event.get("text") or "").strip()
    }
    terminal_run_ids = {
        str(event.get("run_id") or "").strip()
        for event in eligible_events
        if event.get("type") in {"turn_finished", "turn_stopped", "error"}
        and str(event.get("run_id") or "").strip()
    }
    open_run_ids = list(dict.fromkeys(
        str(event.get("run_id") or "").strip()
        for event in eligible_events
        if event.get("type") == "turn_started"
        and str(event.get("run_id") or "").strip()
        and str(event.get("run_id") or "").strip() not in terminal_run_ids
    ))
    referenced_file_ids = merged_file_ids(*[
        fork_event_file_ids(event) for event in eligible_events
    ])
    source_records = fork_source_file_records(
        parent_id,
        parent_events,
        referenced_file_ids,
    )
    cloned_files: dict[str, dict[str, Any]] = {}
    file_clone_failures: dict[str, str] = {}
    for file_id in referenced_file_ids:
        source_record = source_records.get(file_id)
        if not source_record:
            logger.warning(
                "fork file metadata unavailable parent_session=%s child_session=%s file_id=%s",
                parent_id,
                child_id,
                file_id,
            )
            file_clone_failures[file_id] = "registered attachment metadata is unavailable"
            continue
        try:
            cloned = await clone_fork_file_record_async(
                parent_id,
                child_id,
                source_record,
            )
        except asyncio.CancelledError:
            cleanup_task = asyncio.create_task(asyncio.to_thread(
                delete_cloned_fork_files,
                cloned_files,
            ))
            with suppress(BaseException):
                await join_task_despite_caller_cancellation(cleanup_task)
            raise
        except Exception as exc:
            logger.warning(
                "fork file clone failed parent_session=%s child_session=%s file_id=%s: %s",
                parent_id,
                child_id,
                file_id,
                concise_error_message(exc),
            )
            file_clone_failures[file_id] = concise_error_message(exc)
            continue
        if cloned:
            cloned_files[file_id] = cloned
        else:
            file_clone_failures[file_id] = "attachment could not be cloned safely"

    code_diff_run_ids = [
        str(event.get("run_id") or "")
        for event in eligible_events
        if event.get("type") == "code_diff"
    ]
    try:
        cloned_code_diffs = await clone_fork_code_diffs_async(
            parent_id,
            child_id,
            code_diff_run_ids,
        )
    except BaseException:
        cleanup_task = asyncio.create_task(asyncio.to_thread(
            delete_cloned_fork_files,
            cloned_files,
        ))
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(cleanup_task)
        raise

    imported_events: list[tuple[str, dict[str, Any]]] = []
    surfaced_file_failures: set[str] = set()
    for event in eligible_events:
        event_type = event.get("type")
        for file_id in fork_event_file_ids(event):
            if file_id not in file_clone_failures or file_id in surfaced_file_failures:
                continue
            source_record = source_records.get(file_id) or {}
            source_ts = str(event.get("ts") or "").strip()
            error_payload: dict[str, Any] = {
                "error": "A parent attachment could not be copied into this fork.",
                "detail": file_clone_failures[file_id],
                "source_file_id": file_id,
                "filename": source_record.get("filename"),
                "forked": True,
                "original_session_id": parent_id,
                "original_seq": event.get("seq"),
            }
            if source_ts:
                error_payload["ts"] = source_ts
                error_payload["original_ts"] = source_ts
            imported_events.append(("artifact_error", error_payload))
            surfaced_file_failures.add(file_id)
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"seq", "id", "session_id", "ts"}
        }
        if (
            event_type == "turn_finished"
            and str(event.get("run_id") or "").strip() in assistant_runs
        ):
            # Preserve the terminal lifecycle so desktop and iOS flush the
            # run, while keeping the established final-answer dedupe behavior.
            payload["result_text"] = ""
        source_ts = str(event.get("ts") or "").strip()
        if source_ts:
            # Preserve the original timeline chronology while retaining an
            # explicit provenance field for clients and future migrations.
            payload["ts"] = source_ts
            payload["original_ts"] = source_ts
        if event_type == "turn_started":
            source_file_ids = (
                list(event.get("file_ids") or [])
                if isinstance(event.get("file_ids"), list)
                else []
            )
            payload["file_ids"] = [
                cloned_files[file_id]["id"]
                for file_id in merged_file_ids(source_file_ids)
                if file_id in cloned_files
            ]
        nested_file_key = next(
            (key for key in ("file", "artifact") if isinstance(event.get(key), dict)),
            None,
        )
        if nested_file_key:
            source_file_id = normalized_file_id(event[nested_file_key].get("id"))
            cloned = cloned_files.get(source_file_id)
            if not cloned:
                # Never copy a parent-owned record into the child. The text
                # history remains useful even if one best-effort file copy
                # could not be completed.
                continue
            payload[nested_file_key] = dict(cloned)
        payload["forked"] = True
        payload["original_session_id"] = parent_id
        payload["original_seq"] = event.get("seq")
        imported_events.append((str(event_type), payload))
    for run_id in open_run_ids:
        # The parent may still be running, but a fork is a point-in-time
        # snapshot. Close that run locally so copied commentary is durable and
        # the child never renders historical work as live.
        imported_events.append((
            "turn_stopped",
            {
                "run_id": run_id,
                "reason": "fork_snapshot",
                "message": "This run was still active at the fork point.",
                "forked": True,
                "synthetic": True,
                "original_session_id": parent_id,
            },
        ))
    try:
        return await append_imported_events(child_id, imported_events)
    except BaseException:
        # The JSONL append is transactional; roll back the corresponding file
        # registry entries too so a failed fork does not leak large orphaned
        # copies outside the child session directory.
        cleanup_task = asyncio.create_task(asyncio.to_thread(
            delete_cloned_fork_files,
            cloned_files,
        ))
        try:
            await join_task_despite_caller_cancellation(cleanup_task)
        except BaseException:
            pass
        diff_cleanup_task = asyncio.create_task(asyncio.to_thread(
            delete_cloned_fork_code_diffs,
            child_id,
            cloned_code_diffs,
        ))
        with suppress(BaseException):
            await join_task_despite_caller_cancellation(diff_cleanup_task)
        raise

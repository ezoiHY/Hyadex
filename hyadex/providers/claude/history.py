"""Reading native Claude transcripts: locating them, cleaning their text and recognising task notifications."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime
import hashlib
import json
import logging.handlers
import os
from pathlib import Path
import re
from typing import Any, Iterator
import unicodedata

from hyadex.providers.claude.history_provenance import normalize_claude_interruption_context
from hyadex.providers.claude.history_repair import ClaudeMetadataRepairCache
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CLAUDE_PROJECTS_ROOT,
    DEFAULT_BACKEND,
    MAX_IMPORT_MESSAGES,
    MAX_IMPORTED_TEXT_CHARS,
    MAX_LOCAL_SESSION_SCAN_FILES,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
    MAX_WORKSPACE_PATH_CHARS,
)
from hyadex.core.util import contained_jsonl_path
from hyadex.core.transcripts import jsonl_records

logger = logging.getLogger("hyadex-server")

HISTORY_SEARCH_DIRTY: set[str] = set()
HISTORY_SEARCH_REPAIR_DIRTY: set[str] = set()


PROVIDER_SESSION_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
)


CLAUDE_METADATA_REPAIR_CACHE = ClaudeMetadataRepairCache()


def compact_import_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= MAX_IMPORTED_TEXT_CHARS:
        return text
    return text[:MAX_IMPORTED_TEXT_CHARS].rstrip() + "\n\n[import trimmed]"


def is_import_boilerplate(text: str) -> bool:
    stripped = text.strip()
    boilerplate_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<recommended_plugins>",
        "# AGENTS.md instructions",
        "# Context from my IDE setup:",
    )
    return any(stripped.startswith(prefix) for prefix in boilerplate_prefixes)


def text_from_content(content: Any, *, compact: bool = True) -> str:
    def finish(value: str) -> str:
        return compact_import_text(value) if compact else value

    if content is None:
        return ""
    if isinstance(content, str):
        return finish(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in {"text", "input_text", "output_text"} and block.get("text"):
                    parts.append(str(block["text"]))
        return finish("\n".join(p for p in parts if p.strip()))
    if isinstance(content, dict):
        if content.get("text"):
            return finish(str(content["text"]))
        if content.get("message"):
            return finish(str(content["message"]))
    return ""


def message_text(message: Any, *, compact: bool = True) -> str:
    if isinstance(message, dict):
        return text_from_content(message.get("content"), compact=compact)
    return text_from_content(message, compact=compact)


def normalized_history_provider_origin(value: Any) -> dict[str, str] | None:
    """Keep only validated Claude identifiers and the original aware timestamp.

    This metadata is descriptive, never authority. Assistant reconciliation
    can correlate its validated message identity with this chat's live events.
    Validate fields independently so one malformed timestamp cannot erase a
    valid source identity, and never copy transcript paths or context fields.
    """
    if not isinstance(value, dict) or value.get("provider") != "claude":
        return None
    origin = {"provider": "claude"}
    for field in ("event_id", "session_id", "parent_event_id", "prompt_id"):
        candidate = value.get(field)
        if isinstance(candidate, str) and re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            candidate,
        ):
            origin[field] = candidate
    timestamp = value.get("timestamp")
    if isinstance(timestamp, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        timestamp,
    ):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.utcoffset() is not None:
                origin["timestamp"] = timestamp
        except ValueError:
            pass
    if value.get("kind") == "interruption":
        if not all(field in origin for field in ("event_id", "session_id", "timestamp")):
            return None
        origin["kind"] = "interruption"
        cause = value.get("cause")
        origin["cause"] = cause if cause in ("steer", "stop", "unknown") else "unknown"
    return origin if len(origin) > 1 else None


def normalized_history_item(
    kind: str,
    text: str,
    *,
    provider_origin: Any = None,
    source_text_sha256: Any = None,
    allow_user_boilerplate: bool = False,
) -> dict[str, Any] | None:
    origin = normalized_history_provider_origin(provider_origin)
    if kind == "interruption" and (origin is None or origin.get("kind") != "interruption"):
        return None
    source_key = history_dedup_key(kind, text, source_text_sha256=source_text_sha256)[1]
    text = compact_import_text(text)
    if not text or (kind == "user" and not allow_user_boilerplate and is_import_boilerplate(text)):
        return None
    item: dict[str, Any] = {"kind": kind, "text": text}
    if source_key != history_dedup_key(kind, text)[1]:
        # Presentation stays bounded; matching retains the entire source text.
        item["source_text_sha256"] = source_key
    if origin is not None:
        item["provider_origin"] = origin
    return item


def add_history_item(
    items: Any,
    kind: str,
    text: str,
    *,
    provider_origin: Any = None,
    source_text_sha256: Any = None,
) -> None:
    item = normalized_history_item(
        kind, text, provider_origin=provider_origin,
        source_text_sha256=source_text_sha256,
    )
    if item is None:
        return
    if items and items[-1]["kind"] == item["kind"]:
        if item["kind"] == "interruption":
            previous_origin = normalized_history_provider_origin(items[-1].get("provider_origin"))
            origin = item["provider_origin"]
            if previous_origin is not None and all(previous_origin.get(field) == origin.get(field) for field in ("event_id", "session_id")):
                return
        elif item["kind"] == "assistant" and any(
            origin is not None and all(origin.get(field) for field in ("event_id", "session_id"))
            for origin in (
                normalized_history_provider_origin(items[-1].get("provider_origin")),
                normalized_history_provider_origin(item.get("provider_origin")),
            )
        ):
            previous_origin = normalized_history_provider_origin(items[-1].get("provider_origin"))
            origin = normalized_history_provider_origin(item.get("provider_origin"))
            # A distinct provider message remains a real occurrence even if
            # its text repeats. An identityless row cannot consume its credit.
            if previous_origin is not None and origin is not None and all(
                previous_origin.get(field, "").lower() == origin.get(field, "").lower()
                for field in ("event_id", "session_id")
            ):
                return
        elif items[-1]["text"].strip() == item["text"].strip() and history_dedup_key(
            kind, items[-1]["text"], source_text_sha256=items[-1].get("source_text_sha256"),
        ) == history_dedup_key(kind, item["text"], source_text_sha256=item.get("source_text_sha256")):
            return
    items.append(item)


def normalized_history_import_limit(limit: int | None) -> int:
    configured = MAX_IMPORT_MESSAGES if MAX_IMPORT_MESSAGES > 0 else 400
    requested = configured if limit is None or limit <= 0 else limit
    return max(1, min(requested, configured, 2_000))


def provider_session_identifier(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not PROVIDER_SESSION_IDENTIFIER_RE.fullmatch(clean):
        return None
    return clean


def bounded_jsonl_paths(
    root: Path,
    *,
    excluded_directory_names: frozenset[str] = frozenset(),
) -> Iterator[Path]:
    try:
        resolved_root = root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return
    if resolved_root.name in excluded_directory_names:
        return
    scanned = 0
    pending = [resolved_root]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                scanned += 1
                if scanned > MAX_LOCAL_SESSION_SCAN_FILES:
                    logger.warning(
                        "local session scan reached entry cap root=%s cap=%s",
                        root,
                        MAX_LOCAL_SESSION_SCAN_FILES,
                    )
                    return
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in excluded_directory_names:
                            pending.append(Path(entry.path))
                    elif (
                        entry.name.endswith(".jsonl")
                        and entry.is_file(follow_symlinks=False)
                    ):
                        path = contained_jsonl_path(Path(entry.path), resolved_root)
                        if path is not None:
                            yield path
                except OSError:
                    continue


def bounded_jsonl_events(path: Path, *, preserve_invalid: bool = False) -> Iterator[dict[str, Any] | None]:
    """Project native records with bounded memory, not a source-byte cap."""
    initial = path.stat()
    expected = {field: int(getattr(initial, field)) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if any(int(getattr(opened, field)) != expected[field] for field in expected):
            raise ValueError("transcript changed before parsing")
        for event, _end, raw in jsonl_records(stream, end=initial.st_size,
                require_newline=False, max_records=MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
                max_retained_bytes=MAX_LOCAL_TRANSCRIPT_LINE_BYTES):
            if not raw.blank and (event is not None or preserve_invalid):
                yield event
        finished = os.fstat(stream.fileno())
        if any(int(getattr(finished, field)) != expected[field] for field in expected):
            raise ValueError("transcript changed during parsing")


def path_if_jsonl(value: str, root: Path) -> Path | None:
    raw = value.strip()
    if not raw:
        return None
    return contained_jsonl_path(Path(raw), root)


def find_claude_history(provider_id: str) -> Path | None:
    candidates = claude_history_candidates(provider_id)
    return candidates[0] if candidates else None


def claude_history_candidates(provider_id: str) -> list[Path]:
    direct = path_if_jsonl(provider_id, CLAUDE_PROJECTS_ROOT)
    if direct:
        return [direct]
    clean_provider_id = provider_session_identifier(provider_id)
    if clean_provider_id is None:
        return []
    if not CLAUDE_PROJECTS_ROOT.exists():
        return []
    matches = [
        path
        for path in bounded_jsonl_paths(CLAUDE_PROJECTS_ROOT)
        if path.stem == clean_provider_id
    ]

    def modified_at(path: Path) -> float:
        with suppress(OSError):
            return path.stat().st_mtime
        return 0.0

    return sorted(matches, key=modified_at, reverse=True)


CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES = 4 * 1024 * 1024
CLAUDE_TRANSCRIPT_CWD_LINE_BYTES = 4 * 1024 * 1024


def bounded_claude_transcript_regions(path: Path) -> Iterator[bytes]:
    """Yield complete JSONL regions near both ends without loading a huge transcript."""

    regions: list[bytes] = []
    try:
        size = path.stat().st_size
        window = max(1, int(CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES))
        with path.open("rb") as handle:
            prefix = handle.read(window)
            # A short read means the transcript was truncated after stat; a
            # longer read when the original file fit in the window means it
            # grew. Do not combine regions from different file snapshots.
            if len(prefix) != min(size, window):
                return
            if size > len(prefix) and not prefix.endswith(b"\n"):
                prefix = prefix.rsplit(b"\n", 1)[0] if b"\n" in prefix else b""
            if prefix:
                regions.append(prefix)

            start = 0
            if size > window:
                start = max(0, size - window)
                read_offset = max(0, start - 1)
                read_size = size - read_offset
                handle.seek(read_offset)
                # Read only the intended suffix window plus its boundary byte
                # and one sentinel. A sentinel byte means the file grew after
                # stat; a short read means it shrank. Either case fails closed
                # without ever reading to EOF.
                suffix = handle.read(read_size + 1)
                if len(suffix) != read_size:
                    return
            else:
                suffix = b""
            if start > 0:
                previous = suffix[:1]
                suffix = suffix[1:]
                if previous != b"\n":
                    suffix = suffix.split(b"\n", 1)[1] if b"\n" in suffix else b""
            if suffix:
                regions.append(suffix)
    except OSError:
        return
    yield from regions


def canonical_claude_cwd(cwd: str) -> str:
    """Return the real directory Claude records for a working directory.

    Claude resolves symlinks before it records ``cwd`` and before it names a
    project directory, so ``/tmp/work`` is stored as ``/private/tmp/work`` on
    macOS. Comparing real paths keeps one folder reached through a symlink
    resumable while two genuinely different folders still never match.
    """

    expanded = os.path.expanduser(str(cwd or ""))
    try:
        return os.path.realpath(expanded)
    except (OSError, ValueError):
        return os.path.normpath(expanded)


def claude_cwds_match(left: str, right: str) -> bool:
    """Whether two working directories name the same folder for Claude."""

    if not str(left or "").strip() or not str(right or "").strip():
        return False
    if str(Path(left).expanduser()) == str(Path(right).expanduser()):
        return True
    return canonical_claude_cwd(left) == canonical_claude_cwd(right)


def claude_transcript_matches_cwd(path: Path, cwd: str) -> bool:
    """Validate a provider transcript using Claude's recorded cwd metadata."""

    expected_cwd = str(Path(cwd).expanduser())
    expected_canonical = canonical_claude_cwd(cwd)
    canonical_by_recorded: dict[str, str] = {}
    for region in bounded_claude_transcript_regions(path):
        for raw_line in region.splitlines():
            if not raw_line or len(raw_line) > CLAUDE_TRANSCRIPT_CWD_LINE_BYTES:
                continue
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            recorded_cwd = event.get("cwd") if isinstance(event, dict) else None
            if not recorded_cwd or not isinstance(recorded_cwd, str):
                continue
            if str(Path(recorded_cwd).expanduser()) == expected_cwd:
                return True
            # A transcript repeats its cwd on nearly every row; resolve each
            # distinct value once.
            canonical = canonical_by_recorded.get(recorded_cwd)
            if canonical is None:
                if len(canonical_by_recorded) >= 64:
                    continue
                canonical = canonical_claude_cwd(recorded_cwd)
                canonical_by_recorded[recorded_cwd] = canonical
            if canonical == expected_canonical:
                return True
    return False


CLAUDE_TASK_NOTIFICATION_ORIGIN = "task-notification"
CLAUDE_TASK_NOTIFICATION_PROMPT_SOURCES = frozenset({"sdk", "system"})
CLAUDE_TASK_NOTIFICATION_RE = re.compile(
    r"\A<task-notification>\n"
    r"<task-id>[A-Za-z0-9_-]{1,512}</task-id>\n"
    r"<tool-use-id>[A-Za-z0-9_-]{1,512}</tool-use-id>\n"
    r"(?:<output-file>[^<>\r\n]{1,8192}</output-file>\n)?"
    r"<status>(?:completed|failed|stopped)</status>\n"
    r"<summary>.+</summary>\n"
    r"</task-notification>\Z",
    re.DOTALL,
)


def claude_history_event_is_task_notification(event: dict[str, Any]) -> bool:
    """Identify provider-generated task notices without hiding user tag text.

    Current Claude transcripts carry an authoritative structured origin.  The
    exact wrapper fallback is for older records that predate that field and is
    intentionally gated by two independent provider-owned metadata fields.
    """

    if event.get("type") != "user":
        return False
    origin = event.get("origin")
    if isinstance(origin, dict) and str(origin.get("kind") or ""):
        return str(origin.get("kind")) == CLAUDE_TASK_NOTIFICATION_ORIGIN
    if (
        event.get("queueSkipAttachments") is not True
        or str(event.get("promptSource") or "")
        not in CLAUDE_TASK_NOTIFICATION_PROMPT_SOURCES
        or event.get("isSidechain") is True
        or event.get("userType") not in (None, "external")
    ):
        return False
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") not in (None, "user"):
        return False
    content = message.get("content")
    return bool(
        isinstance(content, str)
        and CLAUDE_TASK_NOTIFICATION_RE.fullmatch(content.strip())
    )


def is_claude_task_notification_history_event(event: dict[str, Any]) -> bool:
    """Return whether Claude marked a transcript row as SDK task control."""

    return claude_history_event_is_task_notification(event)


def session_provider_id(sess: dict[str, Any]) -> str | None:
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    if backend == BACKEND_CLAUDE:
        return sess.get("claude_session_id") or sess.get("session_id")
    if backend == BACKEND_CODEX:
        return sess.get("codex_thread_id") or sess.get("session_id")
    # Identity-only tombstones keep old saved records attributable; they never
    # resolve a runtime or permit execution of a retired provider.
    if backend in {"opencode", "gemini", "cursor"}:
        return sess.get(f"{backend}_session_id") or sess.get("session_id")
    return sess.get("session_id")


def native_session_title(value: Any) -> str | None:
    """Treat provider titles only as bounded, single-line display metadata."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    display_text = "".join(
        character for character in value
        if character.isspace()
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    clean = " ".join(display_text.split())[:120]
    if not clean or clean.lower() in {"new chat", "new session", "untitled"}:
        return None
    if re.match(r"^(?:New|Child) session - \d{4}-\d{2}-\d{2}", clean):
        return None
    return clean


def claude_transcript_title(path: Path, provider_id: str) -> str | None:
    """Prefer an exact-session custom name over its latest AI-generated title.

    Share the bounded reader between import discovery and existing sessions;
    never perform another whole-project scan for each import candidate.
    """
    custom_title = ai_title = None
    for region in bounded_claude_transcript_regions(path):
        for line in region.splitlines():
            if len(line) > CLAUDE_TRANSCRIPT_CWD_LINE_BYTES:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if not isinstance(event, dict) or event.get("sessionId") != provider_id:
                continue
            if event.get("isSidechain") is True:
                continue
            if event.get("type") == "custom-title":
                custom_title = native_session_title(event.get("customTitle"))
            elif event.get("type") == "ai-title":
                ai_title = native_session_title(event.get("aiTitle"))
    return custom_title or ai_title


HISTORY_SYNC_CURSOR_VERSION = 1


def normalized_history_sync_cursor(
    sess: dict[str, Any],
) -> dict[str, Any] | None:
    raw = sess.get("_history_sync_cursor")
    if not isinstance(raw, dict) or raw.get("version") != HISTORY_SYNC_CURSOR_VERSION:
        return None
    backend = str(sess.get("backend") or DEFAULT_BACKEND).strip().lower()
    provider_id = str(session_provider_id(sess) or "")
    if (
        str(raw.get("backend") or "") != backend
        or str(raw.get("provider_session_id") or "") != provider_id
    ):
        return None
    source_path = str(raw.get("source_path") or "")
    source_digest = str(raw.get("source_digest") or "")
    last_item_digest = str(raw.get("last_item_digest") or "")
    timeline_pending_active = raw.get("timeline_pending_active", False)
    required_integer_fields = (
        "source_dev",
        "source_ino",
        "source_size",
        "source_mtime_ns",
        "source_offset",
        "timeline_seq",
    )
    optional_integer_fields = (
        "timeline_pending_through_seq",
        "checkpoint_seq",
    )
    if (
        not source_path
        or len(source_path) > MAX_WORKSPACE_PATH_CHARS
        or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
        or (
            last_item_digest
            and re.fullmatch(r"[0-9a-f]{64}", last_item_digest) is None
        )
        or not isinstance(timeline_pending_active, bool)
        or any(
            isinstance(raw.get(field), bool)
            or not isinstance(raw.get(field), int)
            or int(raw[field]) < 0
            for field in required_integer_fields
        )
        or any(
            isinstance(raw.get(field, 0), bool)
            or not isinstance(raw.get(field, 0), int)
            or int(raw.get(field, 0)) < 0
            for field in optional_integer_fields
        )
        or int(raw["source_offset"]) > int(raw["source_size"])
        or (
            timeline_pending_active
            and int(raw.get("timeline_pending_through_seq") or 0)
            < int(raw["timeline_seq"])
        )
        or int(raw.get("checkpoint_seq", raw["timeline_seq"]))
        < int(raw["timeline_seq"])
    ):
        return None
    cursor = {
        "version": HISTORY_SYNC_CURSOR_VERSION,
        "backend": backend,
        "provider_session_id": provider_id,
        "source_path": source_path,
        "source_dev": int(raw["source_dev"]),
        "source_ino": int(raw["source_ino"]),
        "source_size": int(raw["source_size"]),
        "source_mtime_ns": int(raw["source_mtime_ns"]),
        "source_offset": int(raw["source_offset"]),
        "source_digest": source_digest,
        "last_item_digest": last_item_digest,
        "timeline_seq": int(raw["timeline_seq"]),
        # Retained for cursor-schema compatibility. Current reconciliation
        # excludes imported events explicitly and can therefore include new
        # Hyadex-authored ownership credits between bounded source passes.
        "timeline_pending_through_seq": int(
            raw.get("timeline_pending_through_seq") or 0
        ),
        "timeline_pending_active": timeline_pending_active,
        **({"bootstrap_timeline_seq": raw["bootstrap_timeline_seq"]}
           if type(raw.get("bootstrap_timeline_seq")) is int and raw["bootstrap_timeline_seq"] >= 0 else {}),
        "checkpoint_seq": int(raw.get("checkpoint_seq", raw["timeline_seq"])),
    }
    # Distinguish a pre-feature cursor from an initialized conservative empty
    # context. The former receives one bounded seed; the latter never rescans.
    if backend == BACKEND_CLAUDE and "claude_interruption_context" in raw:
        cursor["claude_interruption_context"] = normalize_claude_interruption_context(
            raw["claude_interruption_context"], provider_session_id=provider_id,
        )
    if backend == BACKEND_CODEX and raw.get("codex_last_item_phase") in ("commentary", "final_answer"):
        cursor["codex_last_item_phase"] = raw["codex_last_item_phase"]
    source_sha256 = raw.get("last_item_source_text_sha256")
    if isinstance(source_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        cursor["last_item_source_text_sha256"] = source_sha256
    return cursor


def history_dedup_key(
    kind: str, text: Any, *, source_text_sha256: Any = None,
) -> tuple[str, str]:
    """Compare a transcript message to a timeline message by shape, not bytes.

    The same message reaches the two sides by different paths (import
    compaction on one, assistant-text cleaning on the other), so whitespace
    is normalized before hashing. Compacted imports carry the hash captured
    from their complete source; display prefixes never establish a match.
    """

    if isinstance(source_text_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", source_text_sha256):
        return str(kind or ""), source_text_sha256
    normalized = " ".join(str(text or "").split())
    # JSON permits escaped lone surrogates. Keep their identity losslessly;
    # replacing them would collide with genuine replacement characters.
    return str(kind or ""), hashlib.sha256(normalized.encode("utf-8", errors="surrogatepass")).hexdigest() if normalized else ""

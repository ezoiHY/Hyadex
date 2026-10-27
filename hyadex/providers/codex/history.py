"""Reading native Codex rollouts and threads for history import."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime
import json
from pathlib import Path
import re
import stat
import time
from typing import Any, Iterator

from codex_history_repair import (
    codex_public_item_origin,
    CodexGoalHistoryRepairCache,
    CodexNativeHistoryRepairCache,
)
from hyadex.core.config import CODEX_SESSIONS_ROOT, env_setting, MAX_LOCAL_TRANSCRIPT_LINE_BYTES
from hyadex.core.paths import iso_from_timestamp
from hyadex.core.sessions import normalized_local_session_cwd
from hyadex.core.util import contained_jsonl_path
from hyadex.providers.claude.history import (
    bounded_jsonl_paths,
    history_dedup_key,
    normalized_history_item,
    path_if_jsonl,
    provider_session_identifier,
    text_from_content,
)


def codex_hygiene_int_setting(name: str, default: int) -> int:
    """Read one env-overridable hygiene threshold, never failing at import."""

    raw = env_setting(name, None, f"HYADEX_{name}")
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


# A Codex thread's on-disk rollout grows without bound and every resume
# re-reads it. Past these sizes resumes become slow enough to look wedged, so
# the runtime snapshot surfaces a warning and suggests starting a fresh thread.
CODEX_ROLLOUT_WARN_BYTES = codex_hygiene_int_setting(
    "CODEX_ROLLOUT_WARN_BYTES", 150 * 1024 * 1024
)
CODEX_ROLLOUT_OVERSIZED_BYTES = codex_hygiene_int_setting(
    "CODEX_ROLLOUT_OVERSIZED_BYTES", 400 * 1024 * 1024
)


CODEX_ROLLOUT_PATH_CACHE: dict[str, Path] = {}
CODEX_ROLLOUT_PATH_MISSES: dict[str, float] = {}
CODEX_ROLLOUT_MISS_TTL_SECONDS = 30.0


CODEX_GOAL_HISTORY_REPAIR_CACHE = CodexGoalHistoryRepairCache()
CODEX_NATIVE_HISTORY_REPAIR_CACHE = CodexNativeHistoryRepairCache()


def find_codex_history(provider_id: str) -> Path | None:
    direct = path_if_jsonl(provider_id, CODEX_SESSIONS_ROOT)
    if direct:
        return direct
    clean_provider_id = provider_session_identifier(provider_id)
    if clean_provider_id is None:
        return None
    return codex_rollout_path(clean_provider_id)


def codex_rollout_path(thread_id: str) -> Path | None:
    """Locate ``rollout-*-<thread_id>.jsonl`` under CODEX_SESSIONS_ROOT.

    Blocking filesystem work; call from a worker thread. Hits are cached per
    thread id and re-validated with a bounded metadata read. Misses are cached briefly so a
    thread whose rollout has not been flushed yet does not trigger a directory
    walk on every runtime snapshot.
    """

    clean_id = provider_session_identifier(thread_id)
    if clean_id is None:
        return None
    cached = CODEX_ROLLOUT_PATH_CACHE.get(clean_id)
    if cached is not None:
        if contained_jsonl_path(cached, CODEX_SESSIONS_ROOT) is not None and codex_transcript_meta(cached)[0] == clean_id:
            return cached
        CODEX_ROLLOUT_PATH_CACHE.pop(clean_id, None)
    missed_at = CODEX_ROLLOUT_PATH_MISSES.get(clean_id)
    now = time.monotonic()
    if missed_at is not None and now - missed_at < CODEX_ROLLOUT_MISS_TTL_SECONDS:
        return None
    if not CODEX_SESSIONS_ROOT.exists():
        CODEX_ROLLOUT_PATH_MISSES[clean_id] = now
        return None
    suffix = f"-{clean_id}.jsonl"
    selected: Path | None = None
    selected_mtime = -1.0
    legacy_paths: list[Path] = []
    for path in bounded_jsonl_paths(CODEX_SESSIONS_ROOT):
        name = path.name
        if not (name.startswith("rollout-") and name.endswith(suffix)):
            legacy_paths.append(path)
            continue
        if codex_transcript_meta(path)[0] != clean_id:
            continue
        with suppress(OSError):
            mtime = path.stat().st_mtime
            if mtime > selected_mtime:
                selected = path
                selected_mtime = mtime
    if selected is None:
        # Preserve discovery of renamed/legacy transcripts without reading
        # unrelated headers on the ordinary filename lookup path.
        for path in legacy_paths:
            if codex_transcript_meta(path)[0] != clean_id:
                continue
            with suppress(OSError):
                mtime = path.stat().st_mtime
                if mtime > selected_mtime:
                    selected, selected_mtime = path, mtime
    if selected is None:
        CODEX_ROLLOUT_PATH_MISSES[clean_id] = now
        if len(CODEX_ROLLOUT_PATH_MISSES) > 1024:
            stale = [
                key
                for key, stamp in CODEX_ROLLOUT_PATH_MISSES.items()
                if now - stamp >= CODEX_ROLLOUT_MISS_TTL_SECONDS
            ]
            for key in stale:
                CODEX_ROLLOUT_PATH_MISSES.pop(key, None)
        return None
    CODEX_ROLLOUT_PATH_MISSES.pop(clean_id, None)
    CODEX_ROLLOUT_PATH_CACHE[clean_id] = selected
    return selected


def codex_rollout_stats(thread_id: str) -> dict[str, Any]:
    """Blocking helper: rollout path, size, and mtime for one thread."""

    path = codex_rollout_path(thread_id)
    if path is None:
        return {"rollout_path": None, "rollout_bytes": None, "rollout_mtime": None}
    try:
        stat = path.stat()
    except OSError:
        return {"rollout_path": str(path), "rollout_bytes": None, "rollout_mtime": None}
    return {
        "rollout_path": str(path),
        "rollout_bytes": int(stat.st_size),
        "rollout_mtime": iso_from_timestamp(stat.st_mtime),
    }


def codex_user_item_has_human_provenance(item: dict[str, Any]) -> bool:
    """Prefer an explicit client-authored user item over a reserved-text match.

    A human can paste an entire runtime envelope as evidence. Preserve that
    message when the provider or the earlier import retained its authorship.
    Item ids alone are not authorship: Codex assigns ids to runtime items too.
    """
    if item.get("provider_user_authored") is True:
        return True
    metadata = item.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if isinstance(kinds, list) and "user.text" in kinds:
        return True
    if any(
        isinstance(item.get(field), str) and item[field].strip()
        for field in ("clientUserMessageId", "clientId", "client_user_message_id", "client_id")
    ):
        return True
    return any(
        isinstance(origin, dict)
        and origin.get("provider", "codex") == "codex"
        and origin.get("kind") in ("human", "user", "user_input", "user-input")
        for origin in (item.get("origin"), item.get("provider_origin"))
    )


def is_codex_goal_runtime_user_item(item: dict[str, Any], text: str) -> bool:
    """Recognize only Codex's complete, runtime-only goal continuation input.

    Provider role=user alone is not authorship. Require the typed rollout
    content provenance as well as the complete envelope; unknown/mixed input,
    a partial envelope, and a known human quotation always remain visible.
    """
    if codex_user_item_has_human_provenance(item):
        return False
    metadata = item.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(kinds, list) or not kinds or any(kind != "goal.internal_context" for kind in kinds):
        return False
    candidate = str(text or "").strip()
    match = re.fullmatch(
        r"<codex_internal_context source=([\"'])goal\1>\s*"
        r"Continue working toward the active thread goal\.\s+"
        r"(?P<body>[\s\S]*)</codex_internal_context>",
        candidate,
    )
    if match is None:
        return False
    body = match.group("body")
    objective = re.search(r"<objective>([\s\S]*?)</objective>", body)
    return bool(
        candidate.count("<codex_internal_context") == 1
        and candidate.count("</codex_internal_context>") == 1
        and body.count("<objective>") == 1
        and body.count("</objective>") == 1
        and objective is not None
        and objective.group(1).strip()
    )


def codex_history_user_record(event: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event.get("type") == "event_msg" and payload.get("type") == "user_message":
        return payload, str(payload.get("message") or "")
    if event.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        return payload, text_from_content(payload.get("content"), compact=False)
    return None


def codex_runtime_user_item_kind(item: dict[str, Any], text: str) -> str | None:
    """Known explicitly typed provider inputs, never a text-only quotation filter."""
    if item.get("type") != "message" or item.get("role") != "user" or codex_user_item_has_human_provenance(item):
        return None
    metadata = item.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    if not isinstance(kinds, list) or not kinds:
        return None
    pure_notices = {
        "compaction.summary", "apply_patch.legacy_exec_command_warning",
        "model_switch.legacy_mismatch_warning", "unified_exec.legacy_process_limit_warning",
        "guardian.node_repl_review_evidence", "plugins.recommendations", "agents_md.instructions",
    }
    if isinstance(kinds[0], str) and kinds[0] in pure_notices and all(kind == kinds[0] for kind in kinds):
        candidate = str(text or "").strip()
        if not candidate:
            return None
        wrapper = {"guardian.node_repl_review_evidence": "node_repl_review_evidence",
                   "plugins.recommendations": "recommended_plugins"}.get(kinds[0])
        if wrapper and not re.fullmatch(r"<" + wrapper + r">\s*[\s\S]+?\s*</" + wrapper + r">", candidate):
            return None
        if kinds[0] == "agents_md.instructions" and not (
                candidate.startswith("# AGENTS.md instructions")
                and "\n<INSTRUCTIONS>" in candidate and candidate.endswith("</INSTRUCTIONS>")):
            return None
        return "provider_notice"
    if all(kind == "generic.turn_aborted" for kind in kinds):
        match = re.fullmatch(r"<turn_aborted>\s*([\s\S]*?)\s*</turn_aborted>", str(text or "").strip())
        return "turn_aborted" if match is not None and match.group(1).strip() else None
    if any(kind != "multi_agent.subagent_notification" for kind in kinds):
        return None
    match = re.fullmatch(r"<subagent_notification>\s*([\s\S]*?)\s*</subagent_notification>", str(text or "").strip())
    if match is None:
        return None
    try:
        notification = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None
    if not isinstance(notification, dict) or set(notification) != {"agent_path", "status"}:
        return None
    path, status = notification["agent_path"], notification["status"]
    terminal_status = (
        isinstance(status, str) and status in {"shutdown", "not_found"}
        or isinstance(status, dict) and (
            set(status) == {"completed"} and (status["completed"] is None or isinstance(status["completed"], str))
            or set(status) == {"errored"} and isinstance(status["errored"], str)
        )
    )
    return "subagent_notification" if (isinstance(path, str) and 0 < len(path.strip()) <= 256
            and terminal_status) else None


def codex_history_assistant_metadata(value: Any) -> dict[str, str]:
    """Allow only descriptive public phase and an original aware timestamp.

    No provider identifiers, authority, or private reasoning are inferred from
    text. These fields never participate in the legacy kind/text digest.
    """
    metadata: dict[str, str] = {}
    if not isinstance(value, dict):
        return metadata
    if value.get("phase") in ("commentary", "final_answer"):
        metadata["phase"] = value["phase"]
    timestamp = value.get("ts")
    if isinstance(timestamp, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        timestamp,
    ):
        try:
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is not None:
                metadata["ts"] = timestamp
        except ValueError:
            pass
    return metadata


def codex_history_assistant_item(event: dict[str, Any], text: str) -> dict[str, Any] | None:
    item = normalized_history_item("assistant", text)
    if item is not None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        item.update(codex_history_assistant_metadata({
            "phase": payload.get("phase"), "ts": event.get("timestamp"),
        }))
        origin = codex_public_item_origin(event)
        if origin is not None:
            item["provider_origin"] = origin
    return item


def merge_codex_history_duplicate(previous: dict[str, Any], item: dict[str, Any]) -> bool:
    """Enrich a retained duplicate, but never collapse known distinct phases."""
    old_origin, new_origin = previous.get("provider_origin"), item.get("provider_origin")
    if previous.get("provider_runtime_context") != item.get("provider_runtime_context"):
        return False
    if isinstance(old_origin, dict) and isinstance(new_origin, dict) and any(
        old_origin.get(key) != new_origin.get(key) for key in ("event_id", "turn_id")
    ):
        return False
    if previous.get("kind") != item.get("kind") or str(previous.get("text") or "").strip() != str(item.get("text") or "").strip():
        return False
    if history_dedup_key(
        previous.get("kind", ""), previous.get("text", ""),
        source_text_sha256=previous.get("source_text_sha256"),
    ) != history_dedup_key(
        item.get("kind", ""), item.get("text", ""),
        source_text_sha256=item.get("source_text_sha256"),
    ):
        return False
    if item.get("kind") == "assistant":
        prior = codex_history_assistant_metadata(previous)
        incoming = codex_history_assistant_metadata(item)
        if prior.get("phase") and incoming.get("phase") and prior["phase"] != incoming["phase"]:
            return False
    else:
        prior = codex_history_assistant_metadata({"ts": previous.get("ts")})
        incoming = codex_history_assistant_metadata({"ts": item.get("ts")})
    for key, value in incoming.items():
        if key not in prior:
            previous[key] = value
    if item.get("provider_user_authored") is True:
        previous["provider_user_authored"] = True
    if isinstance(item.get("provider_origin"), dict) and "provider_origin" not in previous:
        previous["provider_origin"] = item["provider_origin"]
    return True


CODEX_TRANSCRIPT_SCAN_LINES = 200


def codex_session_meta_is_subagent(payload: dict[str, Any]) -> bool:
    # Parent identity denotes a child, whereas forked_from_id alone can be an
    # ordinary user-created fork and must remain resumable in the picker.
    if any(isinstance(payload.get(key), str) and payload[key].strip()
           for key in ("parent_thread_id", "parentThreadId")):
        return True
    for key in ("source", "thread_source", "threadSource"):
        source = payload.get(key)
        if isinstance(source, dict) and any(name in source for name in ("subagent", "subAgent")):
            return True
        if isinstance(source, str) and source.replace("_", "").lower().startswith("subagent"):
            return True
    return False


def codex_transcript_meta(
    path: Path, *, exclude_subagents: bool = False,
) -> tuple[str | None, str | None]:
    """Return (session_id, cwd) from a Codex transcript's session_meta record."""
    with suppress(OSError, ValueError):
        for index, event in enumerate(codex_transcript_metadata_records(path)):
            if index >= CODEX_TRANSCRIPT_SCAN_LINES:
                break
            if event.get("type") != "session_meta":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if exclude_subagents and codex_session_meta_is_subagent(payload):
                return None, None
            session_id = provider_session_identifier(
                payload.get("id") or payload.get("session_id")
            )
            cwd = normalized_local_session_cwd(payload.get("cwd"))
            return session_id, cwd
    return None, None


def codex_transcript_metadata_records(path: Path) -> Iterator[dict[str, Any]]:
    """Discovery reads a bounded header, independent of total rollout size."""
    remaining = 1024 * 1024
    with path.open("rb") as stream:
        for _ in range(CODEX_TRANSCRIPT_SCAN_LINES):
            line = stream.readline(min(remaining, MAX_LOCAL_TRANSCRIPT_LINE_BYTES) + 1)
            if not line or len(line) > remaining or not line.endswith(b"\n"):
                return
            remaining -= len(line)
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield record

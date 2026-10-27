"""File records owned by a chat."""
from __future__ import annotations

from contextlib import suppress
import json
import logging.handlers
from pathlib import Path
import shutil
from typing import Any

from hyadex.core.config import FILES_ROOT
from hyadex.core.paths import events_path
from hyadex.core.server_logging import concise_error_message
from hyadex.core.util import normalized_file_id

logger = logging.getLogger("hyadex-server")


def file_record_belongs_to_session(record: Any, session_id: str) -> bool:
    if not isinstance(record, dict):
        return True
    owner = str(record.get("session_id") or "").strip()
    return not owner or owner == session_id


def event_establishes_session_file_origin(event: dict[str, Any], session_id: str) -> bool:
    """Return whether a nested file record genuinely originated in this chat."""
    event_owner = str(event.get("session_id") or "").strip()
    original_owner = str(event.get("original_session_id") or "").strip()
    return (
        event.get("forked") is not True
        and (not event_owner or event_owner == session_id)
        and (not original_owner or original_owner == session_id)
    )


def read_file_meta_record(file_id: str) -> dict[str, Any] | None:
    clean_file_id = normalized_file_id(file_id)
    if not clean_file_id:
        return None
    meta_path = FILES_ROOT / clean_file_id / "meta.json"
    if not meta_path.is_file():
        return None
    with suppress(Exception):
        record = json.loads(meta_path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            return record
    return None


def delete_session_owned_file_records(session_id: str) -> int:
    """Delete only registry entries explicitly owned by a removed chat."""
    removed = 0
    if not FILES_ROOT.is_dir():
        return removed
    for entry in FILES_ROOT.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        metadata = read_file_meta_record(entry.name)
        if str((metadata or {}).get("session_id") or "").strip() != session_id:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            logger.warning(
                "failed to remove session-owned file session=%s file_id=%s: %s",
                session_id,
                entry.name,
                concise_error_message(exc),
            )
    return removed


def event_files_belong_to_session(event: dict[str, Any], session_id: str | None = None) -> bool:
    owner = str(session_id or event.get("session_id") or "").strip()
    if not owner:
        return True
    for key in ("file", "artifact"):
        record = event.get(key)
        if not file_record_belongs_to_session(record, owner):
            return False
        if (
            isinstance(record, dict)
            and not str(record.get("session_id") or "").strip()
            and not event_establishes_session_file_origin(event, owner)
        ):
            return False
    return True


def guess_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".mp4", ".m4v")):
        return "video/mp4"
    if lower.endswith(".mov"):
        return "video/quicktime"
    if lower.endswith(".webm"):
        return "video/webm"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".md", ".markdown")):
        return "text/markdown"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def effective_content_type(filename: str, recorded: str | None = None) -> str:
    content_type = str(recorded or "").strip()
    base_type = content_type.split(";", 1)[0].strip().lower()
    if not base_type or base_type in {"application/octet-stream", "binary/octet-stream"}:
        return guess_content_type(filename)
    return content_type


def iter_session_events(session_id: str):
    path = events_path(session_id)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="ignore") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event_files_belong_to_session(event, session_id):
                yield event


def list_session_file_records(session_id: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for meta_path in FILES_ROOT.glob("*/meta.json"):
        try:
            rec = json.loads(meta_path.read_text())
        except Exception:
            continue
        if rec.get("session_id") == session_id and rec.get("id"):
            records[str(rec["id"])] = rec

    for event in iter_session_events(session_id):
        for key in ("file", "artifact"):
            rec = event.get(key)
            if (
                isinstance(rec, dict)
                and rec.get("id")
                and file_record_belongs_to_session(rec, session_id)
            ):
                if (
                    not str(rec.get("session_id") or "").strip()
                    and not event_establishes_session_file_origin(event, session_id)
                ):
                    continue
                meta = read_file_meta_record(str(rec.get("id") or ""))
                meta_owner = str((meta or {}).get("session_id") or "").strip()
                if meta_owner and meta_owner != session_id:
                    continue
                out = dict(rec)
                out["event_id"] = event.get("id")
                out["event_seq"] = event.get("seq")
                out["event_type"] = event.get("type")
                records[str(rec["id"])] = out

    normalized = []
    for rec in records.values():
        current = dict(rec)
        filename = str(current.get("filename") or Path(str(current.get("path") or "")).name)
        current["content_type"] = effective_content_type(filename, str(current.get("content_type") or ""))
        normalized.append(current)

    return sorted(
        normalized,
        key=lambda rec: (str(rec.get("created_at") or ""), str(rec.get("filename") or "")),
        reverse=True,
    )

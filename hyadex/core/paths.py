"""Paths of a chat's files under the state directory, working-directory helpers and timestamps."""
from __future__ import annotations

import errno
import os
from datetime import datetime
from datetime import timezone
from fastapi import HTTPException
from hyadex.core.config import CODE_DIFFS_ROOT
from hyadex.core.config import DEFAULT_CWD
from hyadex.core.config import MAX_WORKING_DIRECTORY_COMPLETIONS
from hyadex.core.config import MAX_WORKING_DIRECTORY_SCAN_ENTRIES
from hyadex.core.config import MAX_WORKSPACE_PATH_CHARS
from hyadex.core.config import STATE_DIR
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".", " ") else "_" for c in name)
    return cleaned.strip(" .") or "file"


def session_dir(session_id: str) -> Path:
    return STATE_DIR / "sessions" / session_id


def events_path(session_id: str) -> Path:
    return session_dir(session_id) / "events.jsonl"


def uploads_dir(session_id: str) -> Path:
    return session_dir(session_id) / "uploads"


def manifests_dir(session_id: str) -> Path:
    return session_dir(session_id) / "manifests"


def artifact_publications_dir(session_id: str) -> Path:
    return session_dir(session_id) / "artifact_publications"


def codex_manifest_path(session_id: str) -> Path:
    """Stable per-chat artifact handoff path used by provider turns."""
    return manifests_dir(session_id) / "current.json"


def code_diffs_dir(session_id: str) -> Path:
    return CODE_DIFFS_ROOT / session_id


def existing_cwd(requested: str | None) -> str:
    candidates = [requested, DEFAULT_CWD, str(Path.home()), "/tmp"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        if path.is_dir():
            return str(path)
    return "/tmp"


def complete_working_directory_sync(requested: str | None, limit: int = 24) -> dict[str, Any]:
    """Return bounded directory-only completions from the HyadexServer host.

    Electron may be connected to a remote server, so resolving this field on
    the desktop host would offer paths that the agent cannot use.  This helper
    intentionally performs one shallow scan and returns no file names.
    """
    raw = str(requested or "").strip()
    if "\x00" in raw:
        raise HTTPException(status_code=400, detail="working directory paths cannot contain NUL bytes")
    if len(raw) > MAX_WORKSPACE_PATH_CHARS:
        raise HTTPException(status_code=400, detail="working directory path is too long")
    bounded_limit = max(1, min(int(limit), MAX_WORKING_DIRECTORY_COMPLETIONS))
    source = raw or existing_cwd(DEFAULT_CWD)
    expanded = os.path.expanduser(source)
    if not os.path.isabs(expanded):
        expanded = os.path.join(existing_cwd(DEFAULT_CWD), expanded)
    normalized = os.path.normpath(expanded)
    exact = os.path.isdir(normalized)
    scan_path = normalized if exact else os.path.dirname(normalized)
    prefix = "" if exact else os.path.basename(normalized)
    if not scan_path:
        scan_path = os.path.abspath(os.sep)

    suggestions: list[dict[str, Any]] = []
    truncated = False
    scan_error: str | None = None
    try:
        with os.scandir(scan_path) as entries:
            candidates: list[tuple[str, bool]] = []
            for scanned, entry in enumerate(entries):
                if scanned >= MAX_WORKING_DIRECTORY_SCAN_ENTRIES:
                    truncated = True
                    break
                name = entry.name
                if not prefix.startswith(".") and name.startswith("."):
                    continue
                if prefix and not name.casefold().startswith(prefix.casefold()):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    candidates.append((name, entry.is_symlink()))
                except OSError:
                    continue
        candidates.sort(key=lambda item: (item[0].casefold(), item[0]))
        truncated = truncated or len(candidates) > bounded_limit
        for name, is_symlink in candidates[:bounded_limit]:
            completed = os.path.join(scan_path, name)
            if not completed.endswith(os.sep):
                completed += os.sep
            suggestions.append({"name": name, "path": completed, "symlink": is_symlink})
    except FileNotFoundError:
        scan_error = "Parent directory not found."
    except NotADirectoryError:
        scan_error = "Parent path is not a directory."
    except PermissionError:
        scan_error = "Permission denied while reading this directory."
    except OSError:
        scan_error = "This directory could not be read."

    return {
        "input": raw,
        "resolved_path": normalized,
        "exists": exact,
        "base_path": scan_path,
        "suggestions": suggestions,
        "truncated": truncated,
        "message": scan_error,
    }


def validated_fork_cwd(session: dict[str, Any]) -> str:
    """Require an exact usable parent workspace instead of silently falling back."""
    raw = str(session.get("cwd") or "").strip()
    if not raw:
        raise HTTPException(
            status_code=409,
            detail="the parent chat has no working directory; set one before forking",
        )
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise HTTPException(
            status_code=409,
            detail="the parent chat working directory must be an absolute path",
        )
    normalized = Path(os.path.normpath(str(expanded)))
    if not normalized.is_dir():
        raise HTTPException(
            status_code=409,
            detail=f"the parent chat working directory is unavailable: {normalized}",
        )
    return str(normalized)


def write_all(file_fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(file_fd, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short workspace write")
        offset += written

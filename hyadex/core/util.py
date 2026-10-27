"""Small helpers shared across the server: timestamps, bounded reads, text cleanup and normalisers."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import ipaddress
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, AsyncIterator, Iterator

from fastapi import HTTPException
from pydantic import BaseModel, Field

from hyadex.core.config import (
    CODE_DIFFS_ROOT,
    FILES_ROOT,
    MAX_FORK_MEMORY_ITEM_CHARS,
    MAX_LOCAL_TRANSCRIPT_LINE_BYTES,
    MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
    PROVIDER_AUTHORITY_ROOT,
    STATE_DIR,
)
from hyadex.core.identity import (
    legacy_server_identity,
    read_server_identity_file,
    SERVER_IDENTITY_CACHE,
    SERVER_IDENTITY_FILE,
    SERVER_IDENTITY_LOCK,
    write_server_identity_file,
)
from hyadex.core.paths import (
    artifact_publications_dir,
    code_diffs_dir,
    manifests_dir,
    session_dir,
    uploads_dir,
)
from hyadex.providers.runtime import scrub_provider_runtime_environment
from hyadex.core.transcripts import jsonl_records


def server_identity() -> str:
    """Return a stable identity, freezing the legacy value on first use."""

    path = SERVER_IDENTITY_FILE
    cache_key = str(path)
    cached = SERVER_IDENTITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    with SERVER_IDENTITY_LOCK:
        cached = SERVER_IDENTITY_CACHE.get(cache_key)
        if cached is not None:
            return cached
        persisted = read_server_identity_file(path)
        if persisted is not None:
            SERVER_IDENTITY_CACHE[cache_key] = persisted
            return persisted

        identity = legacy_server_identity()
        if not write_server_identity_file(path, identity):
            persisted = read_server_identity_file(path)
            if persisted is None:
                raise RuntimeError(
                    "persisted server identity disappeared during creation"
                )
            identity = persisted
        SERVER_IDENTITY_CACHE[cache_key] = identity
        return identity


def ensure_dirs(session_id: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    CODE_DIFFS_ROOT.mkdir(parents=True, exist_ok=True)
    PROVIDER_AUTHORITY_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if session_id:
        session_dir(session_id).mkdir(parents=True, exist_ok=True)
        uploads_dir(session_id).mkdir(parents=True, exist_ok=True)
        manifests_dir(session_id).mkdir(parents=True, exist_ok=True)
        artifact_publications_dir(session_id).mkdir(parents=True, exist_ok=True)
        code_diffs_dir(session_id).mkdir(parents=True, exist_ok=True)


def _patch_changed_paths(source: str) -> set[str]:
    paths: set[str] = set()
    # Codex's orchestration tool embeds apply_patch in a JavaScript string, so
    # patch newlines arrive as literal `\\n` sequences rather than line breaks.
    expanded = str(source or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    for raw_line in expanded.splitlines():
        line = raw_line.strip()
        match = re.match(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", line)
        if match:
            paths.add(match.group(1).rstrip("\\").strip())
            continue
        match = re.match(r"^\*\*\* Move to:\s*(.+?)\s*$", line)
        if match:
            paths.add(match.group(1).rstrip("\\").strip())
            continue
        match = re.match(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(.+?)\s*$", line)
        if match and match.group(1) != "/dev/null":
            paths.add(match.group(1).rstrip("\\").strip())
    return paths


def normalized_tool_name(tool: dict[str, Any]) -> str:
    raw_name = str(tool.get("name") or "").strip().replace("-", "_")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", raw_name).lower()


def decoded_exact_header_secret(value: bytes) -> str | None:
    """Decode one native secret without normalizing an ambiguous wire value."""

    try:
        candidate = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    if (
        not candidate
        or candidate != candidate.strip()
        or any(character.isspace() for character in candidate)
    ):
        return None
    return candidate


def normalized_network_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse an address and collapse IPv4-mapped IPv6 for exact comparisons."""

    address = ipaddress.ip_address(str(host or ""))
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


class SkillSelection(BaseModel):
    """Opaque selection of one server-discovered provider command."""

    model_config = {"extra": "forbid"}

    id: str = Field(pattern=r"^pcmd_[0-9a-f]{32}$")
    revision: str = Field(pattern=r"^pcmdrev_[0-9a-f]{32}$")


TERMINAL_MIN_COLUMNS = 2
TERMINAL_MAX_COLUMNS = 500
TERMINAL_MIN_ROWS = 1
TERMINAL_MAX_ROWS = 200


def merged_file_ids(*groups: list[str] | tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            file_id = str(value or "").strip()
            if file_id and file_id not in seen:
                seen.add(file_id)
                merged.append(file_id)
    return merged


def terminal_dimensions(columns: int | None = None, rows: int | None = None) -> tuple[int, int]:
    cols = max(TERMINAL_MIN_COLUMNS, min(int(columns or 120), TERMINAL_MAX_COLUMNS))
    lines = max(TERMINAL_MIN_ROWS, min(int(rows or 36), TERMINAL_MAX_ROWS))
    return cols, lines


def normalized_file_id(value: Any) -> str:
    file_id = str(value or "").strip()
    if (
        not file_id
        or file_id in {".", ".."}
        or Path(file_id).name != file_id
        or "/" in file_id
        or "\\" in file_id
    ):
        return ""
    return file_id


def compact_memory_text(text: str, max_chars: int = MAX_FORK_MEMORY_ITEM_CHARS) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[trimmed]"


LEADING_DECORATION_RE = re.compile(
    r"(?m)^[ \t]*(?:(?::[A-Za-z0-9_+\-]+:|[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?)[ \t]*)+"
)


def clean_assistant_text(text: str) -> str:
    return LEADING_DECORATION_RE.sub("", str(text or "")).strip()


def contained_jsonl_path(path: Path, root: Path) -> Path | None:
    """Resolve a regular transcript without permitting root escape or symlinks."""

    try:
        resolved_root = root.expanduser().resolve(strict=True)
        expanded = path.expanduser()
        if expanded.is_symlink():
            return None
        resolved = expanded.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if resolved.suffix != ".jsonl" or not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def bounded_jsonl_records_range(
    path: Path, start: int, end: int, *, expected_stat: dict[str, int],
    require_newline: bool = True, allow_partial: bool = False,
) -> Iterator[tuple[dict[str, Any] | None, int]]:
    """Read every record and ending offset in one immutable JSONL range."""
    if start < 0 or end < start or end > expected_stat["st_size"]:
        raise ValueError("transcript cursor range is invalid")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if any(int(getattr(before, field)) != int(expected_stat[field]) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("transcript changed before cursor parsing")
        stream.seek(start)
        for count, (event, offset, _raw) in enumerate(jsonl_records(stream, end=end,
                require_newline=require_newline, max_records=MAX_LOCAL_TRANSCRIPT_SCAN_LINES,
                max_retained_bytes=MAX_LOCAL_TRANSCRIPT_LINE_BYTES), 1):
            yield event, offset
            if allow_partial and count >= MAX_LOCAL_TRANSCRIPT_SCAN_LINES:
                break
        after = os.fstat(stream.fileno())
        if any(int(getattr(after, field)) != int(expected_stat[field]) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("transcript changed during cursor parsing")


def strip_hyadex_memory_context(text: str) -> str:
    """Remove exact fallback-memory wrappers while preserving the current user text."""
    cleaned = text
    while True:
        previous = cleaned
        length_match = re.match(
            r"^\[(Fork memory context|Codex rollover memory); chars=(\d+)\]\n",
            cleaned,
        )
        if length_match:
            label = length_match.group(1)
            memory_chars = int(length_match.group(2))
            memory_end = length_match.end() + memory_chars
            boundary = f"\n[End {label}]\n\n[Current user prompt]\n"
            if cleaned.startswith(boundary, memory_end):
                cleaned = cleaned[memory_end + len(boundary):]
                continue
        for label, end_label in (
            ("Fork memory context", "fork memory context"),
            ("Codex rollover memory", "Codex rollover memory"),
        ):
            prefix = f"[{label}]\n"
            boundary = f"\n[End {end_label}]\n\n[Current user prompt]\n"
            if cleaned.startswith(prefix):
                boundary_index = cleaned.rfind(boundary, len(prefix))
                if boundary_index >= 0:
                    cleaned = cleaned[boundary_index + len(boundary):]
                    break
        if cleaned == previous:
            return cleaned


def low_available_memory_message(available_mb: int, minimum_mb: int, *, action: str) -> str:
    minimum = f"{minimum_mb} MiB"
    if minimum_mb >= 1024 and minimum_mb % 1024 == 0:
        minimum = f"{minimum_mb // 1024} GiB ({minimum_mb} MiB)"
    return (
        f"low available memory on the server: {available_mb} MiB available; "
        f"at least {minimum} required to {action}. "
        "Close unused applications or stop other agent runs on the server, then retry."
    )


def runner_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    # Every caller launches provider-controlled/runtime code. Never let a
    # legacy service bearer or a per-turn authority path leak through an
    # inherited environment (including summarizers and runtime probes).
    scrub_provider_runtime_environment(env)
    home = env.get("HOME", str(Path.home()))
    extra = [
        f"{home}/.local/bin",
        f"{home}/.npm-global/bin",
        f"{home}/.cargo/bin",
        f"{home}/.bun/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    nvm_root = Path(home) / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        extra.extend(str(path / "bin") for path in sorted(nvm_root.glob("*"), reverse=True))
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


class TransientAdmissionWait(HTTPException):
    """A 409/503 that only means "try again shortly", never a rejection.

    Raised for Codex goals reconfiguration, per-chat maintenance, and host
    capacity/memory deferrals. Queue promotion requeues such rows without
    counting them toward the cross-chat delivery discard bound.
    """


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@asynccontextmanager
async def bounded_lock(
    lock: asyncio.Lock,
    timeout_seconds: float,
) -> AsyncIterator[bool]:
    """Hold ``lock`` if it can be acquired within ``timeout_seconds``.

    Yields ``True`` when the lock is held and ``False`` when the wait timed
    out. Restart status and emergency restart paths use this so a lock that is
    wedged by the very fault an operator is trying to escape cannot make the
    restart control hang or refuse.
    """

    acquired = False
    try:
        await asyncio.wait_for(
            lock.acquire(),
            timeout=max(0.0, float(timeout_seconds)),
        )
        acquired = True
    except (TimeoutError, asyncio.TimeoutError):
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def _safe_config_parent(path: Path) -> tuple[Path, int]:
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PermissionError("HyadexServer configuration directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return parent, os.open(parent, flags)

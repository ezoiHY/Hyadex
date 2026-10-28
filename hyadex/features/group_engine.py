"""Group discussions: one shared timeline for the user and several agents.

A group owns an append-only timeline, a short brief and a list of
participants. Every participant is an agent (Claude Code, Codex, ...) backed by
its own hidden member session, so each agent keeps its native provider
continuity, MCP servers, skills and project configuration.

Token budget rules (see docs/GROUP_DISCUSSIONS.md):

* Only mentioned members run; silent members cost nothing.
* A member receives only the group messages after its cursor, as one new turn
  appended to its native session. Earlier context is never re-sent, so provider
  prompt caches keep hitting.
* Only final replies (plus a server-computed receipt of changed files) reach the
  group. Tool calls and reasoning stay in the member session.
* Long messages are clipped in deltas and referenced by file path.
* The charter is sent once per member and the brief once per version.
* Agent-to-agent mentions inside one round are limited by ``max_agent_hops``.

This module never imports the server. the application supplies a runtime
adapter (see :class:`GroupRuntime`); HTTP transport lives in ``hyadex.routes.groups``.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import secrets
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field


logger = logging.getLogger("hyadex.groups")

GROUP_ID_RE = re.compile(r"^grp_[0-9a-f]{16}$")
HANDLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@./-])@([A-Za-z][A-Za-z0-9_-]{0,23})(?![A-Za-z0-9_-])")
FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
BROADCAST_HANDLES = {"all", "everyone"}
RESERVED_HANDLES = BROADCAST_HANDLES | {"user", "you", "me", "system", "group", "hyadex"}

MAX_PARTICIPANTS = 6
MAX_MESSAGE_CHARS = 100_000
DEFAULT_MAX_AGENT_HOPS = 4
DEFAULT_DELTA_CHAR_LIMIT = 6_000
FULL_TEXT_FILE_THRESHOLD = 2_000
MAX_BRIEF_CHARS = 8_000
MAX_RETAINED_FINISHED_DISPATCHES = 200
MAX_RETAINED_FINISHED_ROUNDS = 50
MAX_SNAPSHOT_ENTRIES = 5_000
MAX_EVENTS_PAGE = 500
WORKSPACE_SNAPSHOT_TIMEOUT_SECONDS = 5.0

MAX_EARLY_TERMINALS = 64

ACTIVE_DISPATCH_STATES = {"starting", "queued", "running"}
TERMINAL_EVENT_TYPES = {"turn_finished", "turn_stopped"}
# ``turn_queue_paused``: a Stop in the member chat held queued rows, which may
# include a group message that has not started.
OBSERVED_EVENT_TYPES = {"turn_started", "turn_unqueued", "turn_queue_paused", *TERMINAL_EVENT_TYPES}

ANALYSIS_ONLY_SUFFIX = (
    "This is analysis only. Do NOT edit, create, or delete any files. Do NOT write code."
)


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def strip_code(text: str) -> str:
    return INLINE_CODE_RE.sub(" ", FENCED_CODE_RE.sub(" ", text))


def parse_mentions(text: str, handles: list[str]) -> list[str]:
    """Return known participant handles mentioned outside code, in order."""

    known = {handle.lower(): handle for handle in handles}
    found: list[str] = []
    for match in MENTION_RE.finditer(strip_code(text or "")):
        name = match.group(1).lower()
        if name in BROADCAST_HANDLES:
            candidates = list(handles)
        elif name in known:
            candidates = [known[name]]
        else:
            continue
        for handle in candidates:
            if handle not in found:
                found.append(handle)
    return found


def neutralize_markup(text: str) -> str:
    """Keep one member's text from opening or closing a delta message block."""

    return re.sub(r"<(/?)(group_message)", r"&lt;\1\2", text, flags=re.I)


def clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# Workspace receipts (zero model tokens)
# ---------------------------------------------------------------------------

def workspace_snapshot(cwd: str) -> dict[str, list[Any]] | None:
    """Map changed paths in a Git worktree to (status, mtime_ns, size).

    Returns None when ``cwd`` is not a Git worktree or Git is unavailable.
    Comparing two snapshots reveals files a turn touched, including files that
    were already dirty before it started.
    """

    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
             "--untracked-files=all", "--no-renames"],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=WORKSPACE_SNAPSHOT_TIMEOUT_SECONDS, check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        if result.returncode != 0:
            return None
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=WORKSPACE_SNAPSHOT_TIMEOUT_SECONDS,
            check=False,
        )
        root = Path(os.fsdecode(top.stdout.strip())) if top.returncode == 0 else Path(cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    snapshot: dict[str, list[Any]] = {}
    for entry in result.stdout.split(b"\0"):
        if len(entry) < 4:
            continue
        status = entry[:2].decode("ascii", "replace")
        path = os.fsdecode(entry[3:])
        try:
            stat = (root / path).stat()
            signature = [status, stat.st_mtime_ns, stat.st_size]
        except OSError:
            signature = [status, None, None]
        snapshot[path] = signature
        if len(snapshot) >= MAX_SNAPSHOT_ENTRIES:
            break
    return snapshot


def workspace_changes(before: dict[str, list[Any]] | None, after: dict[str, list[Any]] | None) -> list[str] | None:
    if before is None or after is None:
        return None
    changed = [path for path, signature in after.items() if before.get(path) != signature]
    changed.extend(path for path in before if path not in after)
    return sorted(set(changed))


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

BackendName = Literal["claude", "codex"]


class ParticipantSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendName
    handle: str | None = Field(default=None, pattern=HANDLE_RE.pattern)
    label: str | None = Field(default=None, max_length=40)
    role: str | None = Field(default=None, max_length=300)
    model: str | None = Field(default=None, max_length=200)
    effort: str | None = Field(default=None, max_length=40)
    claude_permission_mode: Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"] | None = None
    codex_approval_policy: Literal["never", "on-request", "untrusted"] | None = None
    codex_sandbox_mode: Literal["read-only", "workspace-write", "danger-full-access"] | None = None


class GroupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_responder: str | None = Field(default=None, pattern=HANDLE_RE.pattern)
    max_agent_hops: int | None = Field(default=None, ge=0, le=12)
    delta_char_limit: int | None = Field(default=None, ge=500, le=50_000)


class CreateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    cwd: str | None = Field(default=None, max_length=4096)
    participants: list[ParticipantSpec] = Field(min_length=1, max_length=MAX_PARTICIPANTS)
    brief: str | None = Field(default=None, max_length=MAX_BRIEF_CHARS)
    settings: GroupSettings | None = None


class UpdateGroupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    brief: str | None = Field(default=None, max_length=MAX_BRIEF_CHARS)
    settings: GroupSettings | None = None
    archived: bool | None = None


class PostGroupMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    mentions: list[str] | None = Field(default=None, max_length=MAX_PARTICIPANTS + 1)
    mode: Literal["sequential", "parallel"] = "sequential"
    analysis_only: bool = False


class GroupGoalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(pattern=HANDLE_RE.pattern)
    objective: str = Field(min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Runtime adapter supplied by the server
# ---------------------------------------------------------------------------

class GroupRuntime(Protocol):
    def resolve_cwd(self, cwd: str | None) -> str: ...

    def validate_backend(self, backend: str) -> None: ...

    async def create_member_session(self, *, group_id: str, title: str, handle: str,
                                    spec: dict[str, Any], cwd: str) -> str: ...

    async def start_member_turn(self, session_id: str, *, prompt: str, request_id: str) -> dict[str, Any]:
        """Start or queue a turn; ``request_id`` is recorded on its turn events."""

    async def stop_member(self, session_id: str) -> None: ...

    async def cancel_queued(self, session_id: str, queued_id: str) -> bool:
        """Remove a queued turn; False when it already started or is gone."""

    async def retire_member_session(self, session_id: str) -> None: ...

    def member_status(self, session_id: str) -> dict[str, Any]: ...

    def reply_text(self, event: dict[str, Any]) -> str: ...

    async def find_member_progress(self, session_id: str, *, run_id: str | None, queued_id: str | None,
                                   request_id: str) -> dict[str, Any]:
        """Return {exists, run_id, queued_id, terminal, unqueued, held} from the member chat.

        ``held`` is true when the queued turn is still waiting and a Stop in
        the member chat holds it. Runtimes that cannot tell may omit it.
        """

    async def set_member_goal(self, session_id: str, objective: str) -> dict[str, Any]: ...

    async def broadcast(self, key: str, event: dict[str, Any]) -> None: ...


def hub_key(group_id: str) -> str:
    return f"group:{group_id}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GroupError(HTTPException):
    pass


class GroupEngine:
    def __init__(self, root: Path, runtime: GroupRuntime) -> None:
        self.root = root
        self.runtime = runtime
        self.groups: dict[str, dict[str, Any]] = {}
        self.last_seq: dict[str, int] = {}
        self.member_index: dict[str, tuple[str, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()
        self._snapshots: dict[str, dict[str, list[Any]] | None] = {}
        self._early_terminals: dict[tuple[str, str], dict[str, Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        # Called synchronously with every appended event (under the group's
        # lock); listeners must only schedule work, never await engine calls.
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.loaded = False

    # -- persistence -------------------------------------------------------

    def group_dir(self, group_id: str) -> Path:
        return self.root / group_id

    def events_file(self, group_id: str) -> Path:
        return self.group_dir(group_id) / "events.jsonl"

    def message_file(self, group_id: str, seq: int) -> Path:
        return self.group_dir(group_id) / "messages" / f"{seq}.md"

    def lock(self, group_id: str) -> asyncio.Lock:
        lock = self._locks.get(group_id)
        if lock is None:
            lock = self._locks[group_id] = asyncio.Lock()
        return lock

    async def save(self, group: dict[str, Any]) -> None:
        group["updated_at"] = now()
        self._trim(group)
        snapshot = json.loads(json.dumps(group))
        await asyncio.to_thread(atomic_write_json, self.group_dir(group["id"]) / "group.json", snapshot)

    def _trim(self, group: dict[str, Any]) -> None:
        dispatches = group.setdefault("dispatches", {})
        finished = [item for item in dispatches.values() if item.get("status") not in ACTIVE_DISPATCH_STATES]
        if len(finished) > MAX_RETAINED_FINISHED_DISPATCHES:
            finished.sort(key=lambda item: item.get("finished_at") or 0)
            for item in finished[:len(finished) - MAX_RETAINED_FINISHED_DISPATCHES]:
                dispatches.pop(item["id"], None)
        rounds = group.setdefault("rounds", {})
        closed = [item for item in rounds.values() if item.get("status") != "open"]
        if len(closed) > MAX_RETAINED_FINISHED_ROUNDS:
            closed.sort(key=lambda item: item.get("finished_at") or 0)
            for item in closed[:len(closed) - MAX_RETAINED_FINISHED_ROUNDS]:
                rounds.pop(item["id"], None)

    @staticmethod
    def _repair_tail(path: Path) -> None:
        """Drop a torn final line left by a crash or a full disk."""

        if not path.exists():
            return
        with path.open("rb+") as stream:
            size = stream.seek(0, os.SEEK_END)
            if size == 0:
                return
            stream.seek(size - 1)
            if stream.read(1) == b"\n":
                return
            keep = 0
            position = size
            while position > 0:
                step = min(65_536, position)
                position -= step
                stream.seek(position)
                index = stream.read(step).rfind(b"\n")
                if index >= 0:
                    keep = position + index + 1
                    break
            stream.truncate(keep)
            logger.warning("repaired torn group timeline tail %s", path)

    @classmethod
    def _last_seq_from_file(cls, path: Path) -> int:
        if not path.exists():
            return 0
        cls._repair_tail(path)
        last = 0
        with path.open("r", encoding="utf-8", errors="ignore") as source:
            for line in source:
                with suppress(ValueError, AttributeError):
                    last = max(last, int(json.loads(line).get("seq") or 0))
        return last

    def read_events(self, group_id: str, *, after: int = 0, through: int | None = None,
                    limit: int | None = None, types: set[str] | None = None) -> list[dict[str, Any]]:
        path = self.events_file(group_id)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="ignore") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                seq = int(event.get("seq") or 0)
                if seq <= after or (through is not None and seq > through):
                    continue
                if types is not None and event.get("type") not in types:
                    continue
                out.append(event)
                if limit is not None and len(out) >= limit:
                    break
        return out

    async def append(self, group: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one timeline event. Callers hold the group's lock."""

        group_id = group["id"]
        seq = self.last_seq.get(group_id, 0) + 1
        event = {"seq": seq, "type": event_type, "ts": now(), "group_id": group_id, **payload}
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

        def write() -> None:
            path = self.events_file(group_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as stream:
                start = os.fstat(stream.fileno()).st_size
                try:
                    stream.write(line.encode("utf-8"))
                    stream.flush()
                except BaseException:
                    # Never leave a partial line for the next event to join.
                    with suppress(OSError):
                        stream.truncate(start)
                    raise

        await asyncio.to_thread(write)
        self.last_seq[group_id] = seq
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("group listener failed group=%s seq=%s", group_id, seq)
        try:
            await self.runtime.broadcast(hub_key(group_id), event)
        except Exception:
            logger.exception("group broadcast failed group=%s seq=%s", group_id, seq)
        return event

    async def load(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.groups.clear()
        self.member_index.clear()
        for entry in sorted(self.root.iterdir()):
            record = entry / "group.json"
            if not GROUP_ID_RE.match(entry.name) or not record.is_file():
                continue
            try:
                group = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.exception("skipping unreadable group record %s", record)
                continue
            if not isinstance(group, dict) or group.get("id") != entry.name:
                continue
            self.groups[entry.name] = group
            self.last_seq[entry.name] = await asyncio.to_thread(self._last_seq_from_file, self.events_file(entry.name))
            self._index(group)
        self.loaded = True

    async def recover(self) -> None:
        """Reconcile dispatches that were active when the server stopped.

        Group state is saved before every member turn is admitted and after it
        is recorded, so each active dispatch is resolved from its member chat's
        events (matched by run id, queued id or the dispatch id recorded as the
        turn's client request id) without re-sending or re-recording anything.
        """

        for group_id in list(self.groups):
            async with self.lock(group_id):
                group = self.groups.get(group_id)
                if not group:
                    continue
                active = [item for item in group.get("dispatches", {}).values()
                          if item.get("status") in ACTIVE_DISPATCH_STATES]
                recorded = {}
                if active:
                    recorded = {item["dispatch_id"]: item for item in await asyncio.to_thread(
                        self.read_events, group_id, types={"message"}) if item.get("dispatch_id")}
                for dispatch in active:
                    try:
                        await self._recover_dispatch(group, dispatch, recorded.get(dispatch["id"]))
                    except Exception:
                        logger.exception("group recovery failed group=%s dispatch=%s", group_id, dispatch.get("id"))
                for round_ in list(group.get("rounds", {}).values()):
                    if round_.get("status") == "open":
                        await self._advance_round(group, round_)
                await self.save(group)

    async def _recover_dispatch(self, group: dict[str, Any], dispatch: dict[str, Any],
                                recorded: dict[str, Any] | None) -> None:
        if recorded is not None:
            # The reply was recorded but the state after it was not saved.
            dispatch["status"] = ("done" if recorded.get("author_kind") == "agent" and not recorded.get("stopped")
                                  else "stopped" if recorded.get("stopped") else "failed")
            dispatch["finished_at"] = now()
            self._snapshots.pop(dispatch["id"], None)
            round_ = group.get("rounds", {}).get(dispatch.get("round_id"))
            if (dispatch["status"] == "done" and round_ is not None and round_.get("status") == "open"
                    and group.get("kind") != "autopilot"):
                await self._route_mentions(group, round_, list(recorded.get("mentions") or []))
            return
        progress = await self.runtime.find_member_progress(
            dispatch["session_id"], run_id=dispatch.get("run_id"), queued_id=dispatch.get("queued_id"),
            request_id=dispatch["id"])
        if not progress.get("exists", True):
            await self._finish_dispatch_without_reply(
                group, dispatch, "failed", f"@{dispatch['handle']} is unavailable because its member chat was removed.")
            return
        if progress.get("run_id"):
            dispatch["run_id"] = progress["run_id"]
        if progress.get("queued_id"):
            dispatch["queued_id"] = progress["queued_id"]
        if progress.get("terminal") is not None:
            await self._complete_dispatch(group, dispatch, progress["terminal"], advance=False)
        elif progress.get("unqueued"):
            self._restore_member(group, dispatch)
            await self._finish_dispatch_without_reply(
                group, dispatch, "stopped", f"@{dispatch['handle']}'s message was removed from its queue.")
        elif progress.get("held") and dispatch.get("queued_id") and not dispatch.get("run_id"):
            # Stop held it in the member chat before the restart.
            dispatch["status"] = "queued"
            await self._withdraw_held_dispatch(group, dispatch)
        elif dispatch["run_id"]:
            dispatch["status"] = "running"
        elif dispatch["queued_id"]:
            dispatch["status"] = "queued"
        elif dispatch["status"] == "starting":
            self._restore_member(group, dispatch)
            await self._finish_dispatch_without_reply(
                group, dispatch, "failed", "The server restarted before this message was delivered.")

    def _index(self, group: dict[str, Any]) -> None:
        for participant in group.get("participants", []):
            session_id = participant.get("session_id")
            if session_id:
                self.member_index[session_id] = (group["id"], participant["handle"])

    # -- lookups -----------------------------------------------------------

    def require(self, group_id: str, *, writable: bool = False) -> dict[str, Any]:
        group = self.groups.get(group_id)
        if not group:
            raise GroupError(status_code=404, detail="group not found")
        if writable and group.get("archived"):
            raise GroupError(status_code=409, detail="archived groups cannot be changed")
        return group

    @staticmethod
    def participant(group: dict[str, Any], handle: str) -> dict[str, Any] | None:
        return next((item for item in group.get("participants", []) if item["handle"] == handle), None)

    @staticmethod
    def handles(group: dict[str, Any]) -> list[str]:
        return [item["handle"] for item in group.get("participants", [])]

    @staticmethod
    def settings(group: dict[str, Any]) -> dict[str, Any]:
        settings = group.setdefault("settings", {})
        settings.setdefault("max_agent_hops", DEFAULT_MAX_AGENT_HOPS)
        settings.setdefault("delta_char_limit", DEFAULT_DELTA_CHAR_LIMIT)
        return settings

    def default_responder(self, group: dict[str, Any]) -> str | None:
        handles = self.handles(group)
        configured = self.settings(group).get("default_responder")
        if configured in handles:
            return configured
        return handles[0] if handles else None

    def participant_status(self, participant: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
        status = dict(self.runtime.member_status(participant["session_id"]))
        active = [item for item in group.get("dispatches", {}).values()
                  if item.get("handle") == participant["handle"] and item.get("status") in ACTIVE_DISPATCH_STATES]
        if not status.get("exists", True):
            state = "missing"
        elif status.get("needs_action"):
            state = "needs_action"
        elif status.get("running"):
            state = "running"
        elif status.get("queued") or any(item.get("status") == "queued" for item in active):
            state = "queued"
        else:
            state = "idle"
        return {"state": state, "queued": int(status.get("queued") or 0),
                "needs_action": bool(status.get("needs_action"))}

    def public(self, group: dict[str, Any], *, summary: bool = False) -> dict[str, Any]:
        participants = []
        for item in group.get("participants", []):
            entry = {key: item.get(key) for key in (
                "handle", "label", "backend", "model", "effort", "role", "session_id", "joined_at")}
            entry["status"] = self.participant_status(item, group)
            if not summary:
                entry["cursor_seq"] = item.get("cursor_seq", 0)
            participants.append(entry)
        result = {
            "id": group["id"],
            "title": group.get("title"),
            "cwd": group.get("cwd"),
            "archived": bool(group.get("archived")),
            "created_at": group.get("created_at"),
            "updated_at": group.get("updated_at"),
            "last_seq": self.last_seq.get(group["id"], 0),
            "participants": participants,
            "settings": dict(self.settings(group)),
            "brief": dict(group.get("brief") or {"text": "", "version": 0}),
            "goal": group.get("goal"),
            "stats": dict(group.get("stats") or {}),
            "kind": group.get("kind") or "discussion",
            "meta": dict(group.get("meta") or {}),
        }
        if not summary:
            result["open_rounds"] = [
                {key: item.get(key) for key in ("id", "mode", "analysis_only", "hops", "queue", "created_at")}
                for item in group.get("rounds", {}).values() if item.get("status") == "open"
            ]
            result["active_dispatches"] = [
                {key: item.get(key) for key in ("id", "round_id", "handle", "status", "created_at")}
                for item in group.get("dispatches", {}).values() if item.get("status") in ACTIVE_DISPATCH_STATES
            ]
        return result

    # -- lifecycle -----------------------------------------------------------

    def _new_participant(self, group: dict[str, Any], spec: ParticipantSpec) -> dict[str, Any]:
        self.runtime.validate_backend(spec.backend)
        taken = set(self.handles(group))
        handle = (spec.handle or spec.backend).lower()
        if handle in RESERVED_HANDLES:
            raise GroupError(status_code=400, detail=f"@{handle} is reserved")
        if spec.handle and handle in taken:
            raise GroupError(status_code=409, detail=f"@{handle} is already in this group")
        base, counter = handle, 2
        while handle in taken:
            handle = f"{base}{counter}"[:24]
            counter += 1
        default_labels = {"claude": "Claude Code", "codex": "Codex"}
        return {
            "handle": handle,
            "label": spec.label or default_labels.get(spec.backend, spec.backend),
            "backend": spec.backend,
            "model": spec.model,
            "effort": spec.effort,
            "role": spec.role,
            "session_id": None,
            "cursor_seq": 0,
            "brief_version_seen": 0,
            "charter_sent": False,
            "joined_at": now(),
        }

    async def _attach_member(self, group: dict[str, Any], participant: dict[str, Any], spec: ParticipantSpec) -> None:
        session_id = await self.runtime.create_member_session(
            group_id=group["id"], title=f"{group['title']} · @{participant['handle']}",
            handle=participant["handle"], spec=spec.model_dump(exclude_none=True), cwd=group["cwd"])
        participant["session_id"] = session_id
        self.member_index[session_id] = (group["id"], participant["handle"])

    async def create(self, req: CreateGroupRequest, *, kind: str = "discussion",
                     meta: dict[str, Any] | None = None) -> dict[str, Any]:
        cwd = self.runtime.resolve_cwd(req.cwd)
        group_id = f"grp_{secrets.token_hex(8)}"
        title = (req.title or "").strip() or "Group discussion"
        group: dict[str, Any] = {
            "id": group_id, "title": title, "cwd": cwd, "archived": False,
            "kind": kind, "meta": dict(meta or {}),
            "created_at": now(), "updated_at": now(),
            "settings": {}, "brief": {"text": (req.brief or "").strip(), "version": 1 if (req.brief or "").strip() else 0,
                                     "updated_at": now() if (req.brief or "").strip() else None},
            "participants": [], "rounds": {}, "dispatches": {}, "goal": None,
            "stats": {"messages": 0, "dispatches": 0, "prompt_chars": 0},
        }
        specs = list(req.participants)
        for spec in specs:
            group["participants"].append(self._new_participant(group, spec))
        if req.settings is not None:
            self._apply_settings(group, req.settings)
        async with self._registry_lock:
            self.groups[group_id] = group
        async with self.lock(group_id):
            try:
                for participant, spec in zip(group["participants"], specs):
                    await self._attach_member(group, participant, spec)
            except BaseException:
                for participant in group["participants"]:
                    if participant.get("session_id"):
                        self.member_index.pop(participant["session_id"], None)
                        with suppress(Exception):
                            await self.runtime.retire_member_session(participant["session_id"])
                self.groups.pop(group_id, None)
                raise
            await self.append(group, "group_created", {
                "title": title,
                "participants": [{key: item[key] for key in ("handle", "label", "backend")} for item in group["participants"]],
            })
            if group["brief"]["version"]:
                await self.append(group, "brief_updated", {"version": group["brief"]["version"], "text": group["brief"]["text"]})
            await self.save(group)
            return self.public(group)

    def _apply_settings(self, group: dict[str, Any], settings: GroupSettings) -> dict[str, Any]:
        current = self.settings(group)
        changes = settings.model_dump(exclude_unset=True)
        if changes.get("default_responder") is not None and changes["default_responder"] not in self.handles(group):
            raise GroupError(status_code=400, detail="default_responder must be a participant handle")
        current.update({key: value for key, value in changes.items()})
        return changes

    async def update(self, group_id: str, req: UpdateGroupRequest) -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id)
            fields = req.model_dump(exclude_unset=True)
            if group.get("archived") and set(fields) != {"archived"}:
                raise GroupError(status_code=409, detail="archived groups cannot be changed")
            if req.title is not None:
                group["title"] = req.title.strip() or group["title"]
            if req.brief is not None and req.brief.strip() != (group.get("brief") or {}).get("text", ""):
                brief = group.setdefault("brief", {"text": "", "version": 0})
                brief.update({"text": req.brief.strip(), "version": int(brief.get("version") or 0) + 1, "updated_at": now()})
                await self.append(group, "brief_updated", {"version": brief["version"], "text": brief["text"]})
            if req.settings is not None:
                changes = self._apply_settings(group, req.settings)
                if changes:
                    await self.append(group, "settings_updated", {"settings": changes})
            archiving = req.archived is True and not group.get("archived")
            if req.archived is not None and bool(req.archived) != bool(group.get("archived")):
                group["archived"] = bool(req.archived)
                await self.append(group, "group_archived" if req.archived else "group_restored", {})
            await self.save(group)
            if not archiving:
                return self.public(group)
        # An archived group runs no further hand-offs.
        return await self.stop(group_id, reason="archived")

    async def add_participant(self, group_id: str, spec: ParticipantSpec) -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id, writable=True)
            if len(group["participants"]) >= MAX_PARTICIPANTS:
                raise GroupError(status_code=409, detail=f"a group has at most {MAX_PARTICIPANTS} agents")
            participant = self._new_participant(group, spec)
            await self._attach_member(group, participant, spec)
            # A new member starts from the present; the brief carries context.
            participant["cursor_seq"] = self.last_seq.get(group_id, 0)
            group["participants"].append(participant)
            await self.append(group, "participant_added", {
                key: participant[key] for key in ("handle", "label", "backend")})
            await self.save(group)
            return self.public(group)

    async def remove_participant(self, group_id: str, handle: str) -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id, writable=True)
            participant = self.participant(group, handle)
            if participant is None:
                raise GroupError(status_code=404, detail="participant not found")
            if len(group["participants"]) == 1:
                raise GroupError(status_code=409, detail="a group needs at least one agent")
            if not self.runtime.member_status(participant["session_id"]).get("exists", True):
                await self._settle_orphaned_dispatches(group)
            active = [item for item in group.get("dispatches", {}).values()
                      if item.get("handle") == handle and item.get("status") in ACTIVE_DISPATCH_STATES]
            if active:
                raise GroupError(status_code=409, detail=f"@{handle} is still working; stop the group first")
            group["participants"].remove(participant)
            for round_ in group.get("rounds", {}).values():
                round_["queue"] = [item for item in round_.get("queue", []) if item != handle]
            if self.settings(group).get("default_responder") == handle:
                self.settings(group)["default_responder"] = None
            self.member_index.pop(participant["session_id"], None)
            await self.append(group, "participant_removed", {"handle": handle})
            await self.save(group)
            responder = self.default_responder(group)
            anchor = (self.participant(group, responder) or {}).get("session_id") if responder else None
        hook = getattr(self.runtime, "participant_removed", None)
        if hook is not None and anchor:
            # Scheduled group jobs anchored to the removed member move to the
            # current default responder instead of pausing with its chat.
            with suppress(Exception):
                await hook(group_id, participant["session_id"], anchor)
        with suppress(Exception):
            await self.runtime.retire_member_session(participant["session_id"])
        return self.public(group)

    async def delete(self, group_id: str) -> None:
        await self.stop(group_id, reason="deleted")
        async with self.lock(group_id):
            group = self.require(group_id)
            members = [item["session_id"] for item in group.get("participants", []) if item.get("session_id")]
            for session_id in members:
                self.member_index.pop(session_id, None)
            self.groups.pop(group_id, None)
            self.last_seq.pop(group_id, None)
            target = self.group_dir(group_id)
            trash = target.with_name(f".deleted-{group_id}-{int(now())}")
            await asyncio.to_thread(os.replace, target, trash)
        for session_id in members:
            with suppress(Exception):
                await self.runtime.retire_member_session(session_id)
        hook = getattr(self.runtime, "group_deleted", None)
        if hook is not None:
            with suppress(Exception):
                await hook(group_id)
        await asyncio.to_thread(self._remove_tree, trash)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    # -- messages and rounds -------------------------------------------------

    def _resolve_targets(self, group: dict[str, Any], text: str, mentions: list[str] | None) -> list[str]:
        handles = self.handles(group)
        if mentions:
            targets: list[str] = []
            for raw in mentions:
                name = raw.strip().lstrip("@").lower()
                if name in BROADCAST_HANDLES:
                    candidates = handles
                elif name in handles:
                    candidates = [name]
                else:
                    raise GroupError(status_code=400, detail=f"@{name} is not in this group")
                targets.extend(item for item in candidates if item not in targets)
            return targets
        targets = parse_mentions(text, handles)
        if targets:
            return targets
        responder = self.default_responder(group)
        return [responder] if responder else []

    async def _append_message(self, group: dict[str, Any], *, author: str, kind: str, text: str,
                              round_id: str | None, mentions: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        seq = self.last_seq.get(group["id"], 0) + 1
        payload: dict[str, Any] = {
            "author": author, "author_kind": kind, "text": text, "mentions": mentions,
            "round_id": round_id, "length": len(text), **(extra or {}),
        }
        if len(text) > FULL_TEXT_FILE_THRESHOLD:
            path = self.message_file(group["id"], seq)

            def write() -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")

            await asyncio.to_thread(write)
            payload["full_text_path"] = str(path)
        event = await self.append(group, "message", payload)
        stats = group.setdefault("stats", {})
        stats["messages"] = int(stats.get("messages") or 0) + 1
        return event

    async def skip_backlog(self, group_id: str, handle: str) -> None:
        """Mark everything so far as seen by ``handle``.

        Autopilot requests are self-contained, so a member only needs the next
        message addressed to it, not other members' replies.
        """

        async with self.lock(group_id):
            group = self.require(group_id)
            participant = self.participant(group, handle)
            if participant is None:
                raise GroupError(status_code=404, detail="participant not found")
            participant["cursor_seq"] = self.last_seq.get(group_id, 0)
            await self.save(group)

    async def post_message(self, group_id: str, req: PostGroupMessageRequest, *, author: str = "user",
                           origin: str = "user", meta: dict[str, Any] | None = None, kind: str = "user",
                           audience: list[str] | None = None) -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id, writable=True)
            text = req.text.strip()
            if not text:
                raise GroupError(status_code=400, detail="message text is required")
            targets = self._resolve_targets(group, text, req.mentions)
            round_ = {
                "id": new_id("gr"), "mode": req.mode, "analysis_only": bool(req.analysis_only),
                "hops": 0, "queue": list(targets), "status": "open", "created_at": now(),
                "origin": origin, "parallel_started": False,
            }
            group.setdefault("rounds", {})[round_["id"]] = round_
            extra = {**(meta or {}), **({"origin": origin} if origin != "user" else {}),
                     # Only these members receive the message in their deltas.
                     **({"audience": list(audience)} if audience else {})}
            message = await self._append_message(
                group, author=author, kind=kind, text=text, round_id=round_["id"], mentions=targets,
                extra=extra or None)
            round_["started_seq"] = message["seq"]
            await self._advance_round(group, round_)
            await self.save(group)
            return {"message": message, "round_id": round_["id"], "targets": targets, "group": self.public(group)}

    def _round_active_dispatches(self, group: dict[str, Any], round_id: str) -> list[dict[str, Any]]:
        return [item for item in group.get("dispatches", {}).values()
                if item.get("round_id") == round_id and item.get("status") in ACTIVE_DISPATCH_STATES]

    async def _advance_round(self, group: dict[str, Any], round_: dict[str, Any]) -> None:
        if round_.get("status") != "open":
            return
        if round_.get("mode") == "parallel" and not round_.get("parallel_started"):
            round_["parallel_started"] = True
            batch, round_["queue"] = list(round_.get("queue", [])), []
            for handle in batch:
                await self._dispatch(group, round_, handle)
        elif not self._round_active_dispatches(group, round_["id"]):
            while round_.get("queue"):
                handle = round_["queue"].pop(0)
                dispatch = await self._dispatch(group, round_, handle)
                if dispatch is not None and dispatch.get("status") in ACTIVE_DISPATCH_STATES:
                    break
        if not round_.get("queue") and not self._round_active_dispatches(group, round_["id"]):
            round_["status"] = "done"
            round_["finished_at"] = now()
            await self.append(group, "round_finished", {"round_id": round_["id"], "status": "done", "hops": round_.get("hops", 0)})

    def build_prompt(self, group: dict[str, Any], participant: dict[str, Any], round_: dict[str, Any],
                     messages: list[dict[str, Any]]) -> str:
        handle = participant["handle"]
        limit = int(self.settings(group).get("delta_char_limit") or DEFAULT_DELTA_CHAR_LIMIT)
        parts: list[str] = []
        if not participant.get("charter_sent") and group.get("kind") == "autopilot":
            parts.append("\n".join([
                f'[Hyadex autopilot run "{group.get("title")}"]',
                f"You are @{handle}, the {participant.get('role') or handle} in an automated "
                "plan -> implement -> review loop run by the Hyadex server.",
                "- Instructions arrive in <group_message from=\"autopilot\"> blocks. Do exactly what each one asks and "
                "end your final reply with the JSON block it requests. Only your final reply is read.",
                "- Mentions do not hand off work here; the server decides who acts next.",
                f"- Work only in {group.get('cwd')}.",
            ]))
        elif not participant.get("charter_sent"):
            members = []
            for item in group.get("participants", []):
                description = f"@{item['handle']} ({item.get('label') or item['backend']}"
                description += f", {item['role']})" if item.get("role") else ")"
                members.append(description + (" — you" if item["handle"] == handle else ""))
            parts.append("\n".join([
                f'[Hyadex group discussion "{group.get("title")}"]',
                f"You are @{handle} in a group discussion with the user and other coding agents.",
                "Members: " + "; ".join(members) + ".",
                "How this group works:",
                "- New group messages arrive in <group_message> blocks. Only your final reply is posted to the "
                "group; your tool calls and reasoning stay private.",
                "- Keep replies short and concrete. Refer to files by path and to earlier messages by seq instead "
                "of quoting them. Clipped messages name a file that holds the full text; read it only if needed.",
                "- To ask another member to act, mention them, for example \"@codex please run the tests\". "
                "Mention nobody when you are finished; the user decides what happens next.",
                f"- All members share the working directory {group.get('cwd')}. Do not edit files another member "
                "is working on.",
            ]))
        brief = group.get("brief") or {}
        if brief.get("text") and int(brief.get("version") or 0) > int(participant.get("brief_version_seen") or 0):
            parts.append(f"[Group brief v{brief['version']}]\n{brief['text']}")
        goal = group.get("goal")
        if goal and goal.get("handle") == handle and not participant.get("goal_seen") == goal.get("set_at"):
            parts.append(f"[Group goal assigned to you]\n{goal.get('objective')}")
        blocks = []
        for message in messages:
            author = message.get("author")
            sender = {"user": "user", "system": "system", "orchestrator": "autopilot"}.get(
                str(message.get("author_kind")), f"@{author}")
            attributes = f'seq="{message["seq"]}" from="{sender}"'
            changed = (message.get("receipt") or {}).get("changed_files")
            if changed:
                shown = ", ".join(changed[:12]) + (f", +{len(changed) - 12} more" if len(changed) > 12 else "")
                attributes += f' changed_files="{html.escape(shown, quote=True)}"'
            body, clipped = clip(neutralize_markup(str(message.get("text") or "")), limit)
            if clipped:
                reference = message.get("full_text_path")
                body += (f"\n[clipped {len(message.get('text') or '') - len(body)} characters"
                         + (f"; full text: {reference}]" if reference else "]"))
            blocks.append(f"<group_message {attributes}>\n{body}\n</group_message>")
        count = len(blocks)
        parts.append(f"[{count} new group message{'s' if count != 1 else ''} since your last turn]\n" + "\n".join(blocks))
        footer = [f"Reply to the group as @{handle}."]
        if round_.get("mode") == "parallel" and not round_.get("parallel_followup"):
            footer.append("Other members are answering the same message independently; give your own view.")
        if round_.get("analysis_only"):
            footer.append(ANALYSIS_ONLY_SUFFIX)
        parts.append(" ".join(footer))
        return "\n\n".join(parts)

    async def _dispatch(self, group: dict[str, Any], round_: dict[str, Any], handle: str) -> dict[str, Any] | None:
        participant = self.participant(group, handle)
        if participant is None:
            return None
        status = self.runtime.member_status(participant["session_id"])
        if not status.get("exists", True):
            await self._append_message(group, author="system", kind="system",
                                       text=f"@{handle} is unavailable because its member chat was removed.",
                                       round_id=round_["id"], mentions=[])
            return None
        through = self.last_seq.get(group["id"], 0)
        messages = [item for item in await asyncio.to_thread(
            self.read_events, group["id"], after=int(participant.get("cursor_seq") or 0), through=through,
            types={"message"}) if item.get("author") != handle
            and (not item.get("audience") or handle in item["audience"])]
        if not messages:
            return None
        prompt = self.build_prompt(group, participant, round_, messages)
        dispatch = {
            "id": new_id("gd"), "round_id": round_["id"], "handle": handle,
            "session_id": participant["session_id"], "status": "starting", "run_id": None, "queued_id": None,
            "through_seq": through, "prompt_chars": len(prompt), "created_at": now(), "finished_at": None,
            # Restored if this dispatch is cancelled before the member sees it.
            "previous": {key: participant.get(key) for key in
                         ("cursor_seq", "charter_sent", "brief_version_seen", "goal_seen")},
        }
        group.setdefault("dispatches", {})[dispatch["id"]] = dispatch
        self._snapshots[dispatch["id"]] = await asyncio.to_thread(workspace_snapshot, group["cwd"])
        participant["cursor_seq"] = through
        participant["charter_sent"] = True
        participant["brief_version_seen"] = int((group.get("brief") or {}).get("version") or 0)
        if group.get("goal") and group["goal"].get("handle") == handle:
            participant["goal_seen"] = group["goal"].get("set_at")
        # Write ahead: once the turn is admitted, a restart must find this
        # dispatch (by its id, recorded as the turn's client request id) rather
        # than send the same messages again.
        await self.save(group)
        try:
            # The member chat shows exactly what the agent received.
            result = await self.runtime.start_member_turn(participant["session_id"], prompt=prompt,
                                                          request_id=dispatch["id"])
        except BaseException as exc:
            self._restore_member(group, dispatch)
            if not isinstance(exc, Exception):
                # Cancelled (for example at shutdown): never leave it "starting".
                dispatch["status"] = "failed"
                dispatch["finished_at"] = now()
                self._snapshots.pop(dispatch["id"], None)
                with suppress(BaseException):
                    await self.save(group)
                raise
            detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
            await self._finish_dispatch_without_reply(group, dispatch, "failed", f"Could not deliver to @{handle}: {detail}")
            await self.save(group)
            return dispatch
        dispatch["run_id"] = result.get("run_id")
        dispatch["queued_id"] = result.get("queued_id")
        dispatch["status"] = "queued" if result.get("queued") or (dispatch["queued_id"] and not dispatch["run_id"]) else "running"
        await self.save(group)
        stats = group.setdefault("stats", {})
        stats["dispatches"] = int(stats.get("dispatches") or 0) + 1
        stats["prompt_chars"] = int(stats.get("prompt_chars") or 0) + len(prompt)
        await self.append(group, "dispatch", {
            "dispatch_id": dispatch["id"], "round_id": round_["id"], "handle": handle,
            "through_seq": through, "messages": len(messages), "prompt_chars": len(prompt),
            "state": dispatch["status"], "session_id": participant["session_id"],
        })
        await self.append(group, "member_status", {"handle": handle, "state": dispatch["status"], "dispatch_id": dispatch["id"]})
        early = self._early_terminals.pop((participant["session_id"], str(dispatch["run_id"] or "")), None)
        if early is not None:
            await self._complete_dispatch(group, dispatch, early, advance=False)
        return dispatch

    async def _withdraw_held_dispatch(self, group: dict[str, Any], dispatch: dict[str, Any]) -> bool:
        """Settle a group message that a Stop in its member chat held.

        A held row waits for that chat's user. For the group it is a stop: the
        row is withdrawn, so it can never run detached from its round, and the
        dispatch ends as stopped. If the row cannot be withdrawn (it already
        started or left the queue, whose own event settles it, or the queue is
        briefly fenced), the dispatch stays queued.
        """

        if not await self.runtime.cancel_queued(dispatch["session_id"], str(dispatch["queued_id"])):
            return False
        self._restore_member(group, dispatch)
        await self._finish_dispatch_without_reply(
            group, dispatch, "stopped", f"@{dispatch['handle']} was stopped before it started.")
        return True

    def _restore_member(self, group: dict[str, Any], dispatch: dict[str, Any]) -> None:
        """Undo a dispatch's member bookkeeping if no later dispatch built on it."""

        participant = self.participant(group, dispatch["handle"])
        previous = dispatch.get("previous")
        if participant is None or not isinstance(previous, dict):
            return
        if int(participant.get("cursor_seq") or 0) != int(dispatch.get("through_seq") or 0):
            return
        later = any(item is not dispatch and item.get("handle") == dispatch["handle"]
                    and (item.get("created_at") or 0) > (dispatch.get("created_at") or 0)
                    for item in group.get("dispatches", {}).values())
        if later:
            return
        for key, value in previous.items():
            if value is None:
                participant.pop(key, None)
            else:
                participant[key] = value

    async def _finish_dispatch_without_reply(self, group: dict[str, Any], dispatch: dict[str, Any], status: str,
                                             note: str) -> None:
        if dispatch.get("status") not in ACTIVE_DISPATCH_STATES:
            return
        dispatch["status"] = status
        dispatch["finished_at"] = now()
        self._snapshots.pop(dispatch["id"], None)
        await self._append_message(group, author="system", kind="system", text=note,
                                   round_id=dispatch.get("round_id"), mentions=[], extra={"dispatch_id": dispatch["id"]})
        await self.append(group, "member_status", {"handle": dispatch["handle"], "state": status, "dispatch_id": dispatch["id"]})

    async def _complete_dispatch(self, group: dict[str, Any], dispatch: dict[str, Any], event: dict[str, Any],
                                 *, advance: bool = True) -> None:
        if dispatch.get("status") not in ACTIVE_DISPATCH_STATES:
            return
        handle = dispatch["handle"]
        stopped = event.get("type") == "turn_stopped" or bool(event.get("stopped"))
        exit_code = event.get("exit_code")
        failed = not stopped and exit_code not in (None, 0)
        text = self.runtime.reply_text(event).strip()
        before = self._snapshots.pop(dispatch["id"], None)
        receipt: dict[str, Any] = {}
        if before is not None:
            after = await asyncio.to_thread(workspace_snapshot, group["cwd"])
            changed = workspace_changes(before, after)
            if changed:
                receipt["changed_files"] = changed[:200]
                if len(changed) > 200:
                    receipt["changed_files_truncated"] = len(changed)
        if event.get("run_id") and not dispatch.get("run_id"):
            dispatch["run_id"] = event.get("run_id")
        dispatch["status"] = "stopped" if stopped else ("failed" if failed else "done")
        dispatch["finished_at"] = now()
        mentions: list[str] = []
        if text:
            mentions = [item for item in parse_mentions(text, self.handles(group)) if item != handle]
            extra = {"dispatch_id": dispatch["id"], "origin": "group"}
            if receipt:
                extra["receipt"] = receipt
            if stopped:
                extra["stopped"] = True
            await self._append_message(group, author=handle, kind="agent", text=text,
                                       round_id=dispatch.get("round_id"), mentions=mentions, extra=extra)
        else:
            note = (f"@{handle} was stopped." if stopped else
                    f"@{handle} could not finish this turn." if failed else
                    f"@{handle} finished without a reply.")
            await self._append_message(group, author="system", kind="system", text=note,
                                       round_id=dispatch.get("round_id"), mentions=[],
                                       extra={"dispatch_id": dispatch["id"], **({"receipt": receipt} if receipt else {})})
        await self.append(group, "member_status", {"handle": handle, "state": "idle" if dispatch["status"] == "done" else dispatch["status"],
                                                   "dispatch_id": dispatch["id"]})
        # Persist the recorded reply before any hand-off starts another turn.
        await self.save(group)
        round_ = group.get("rounds", {}).get(dispatch.get("round_id"))
        if round_ is None or round_.get("status") != "open":
            return
        # In autopilot groups the run's state machine, not mentions, decides who acts.
        if dispatch["status"] == "done" and mentions and group.get("kind") != "autopilot":
            await self._route_mentions(group, round_, mentions)
        if advance:
            await self._advance_round(group, round_)

    async def _route_mentions(self, group: dict[str, Any], round_: dict[str, Any], mentions: list[str]) -> None:
        """Queue members an agent's reply handed work to, within the hop budget."""

        handles = set(self.handles(group))
        limit = int(self.settings(group).get("max_agent_hops") or 0)
        pending = set(round_.get("queue", [])) | {item["handle"] for item in self._round_active_dispatches(group, round_["id"])}
        for target in mentions:
            if target in pending or target not in handles:
                continue
            if int(round_.get("hops") or 0) >= limit:
                await self._append_message(
                    group, author="system", kind="system", round_id=round_["id"], mentions=[],
                    text=(f"Agent hand-offs for this message reached the limit of {limit}. "
                          "Mention an agent to continue."))
                break
            round_["hops"] = int(round_.get("hops") or 0) + 1
            round_.setdefault("queue", []).append(target)
            round_["parallel_followup"] = True
            pending.add(target)

    # -- member session observation ------------------------------------------

    def observe_member_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Called synchronously from the session event path; never blocks it."""

        if event.get("type") not in OBSERVED_EVENT_TYPES:
            return
        reference = self.member_index.get(session_id)
        if reference is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._handle_member_event(reference[0], session_id, dict(event)))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle_member_event(self, group_id: str, session_id: str, event: dict[str, Any]) -> None:
        try:
            async with self.lock(group_id):
                group = self.groups.get(group_id)
                if not group:
                    return
                await self._apply_member_event(group, session_id, event)
                await self.save(group)
        except Exception:
            logger.exception("group member event handling failed group=%s type=%s", group_id, event.get("type"))

    async def _apply_member_event(self, group: dict[str, Any], session_id: str, event: dict[str, Any]) -> None:
        dispatches = [item for item in group.get("dispatches", {}).values()
                      if item.get("session_id") == session_id and item.get("status") in ACTIVE_DISPATCH_STATES]
        run_id = str(event.get("run_id") or "")
        if event.get("type") == "turn_started":
            queued_id = str(event.get("queued_id") or "")
            match = next((item for item in dispatches if queued_id and item.get("queued_id") == queued_id), None)
            if match is not None:
                match["run_id"] = run_id or match.get("run_id")
                match["status"] = "running"
                await self.append(group, "member_status", {"handle": match["handle"], "state": "running",
                                                           "dispatch_id": match["id"]})
            return
        if event.get("type") == "turn_queue_paused":
            held = {str(item) for item in event.get("queued_ids") or () if item}
            for match in [item for item in dispatches
                          if item.get("status") == "queued" and not item.get("run_id")
                          and str(item.get("queued_id") or "") in held]:
                if await self._withdraw_held_dispatch(group, match):
                    round_ = group.get("rounds", {}).get(match.get("round_id"))
                    if round_ is not None:
                        await self._advance_round(group, round_)
            return
        if event.get("type") == "turn_unqueued":
            queued_id = str(event.get("queued_id") or "")
            match = next((item for item in dispatches if queued_id and item.get("queued_id") == queued_id
                          and not item.get("run_id")), None)
            if match is not None:
                self._restore_member(group, match)
                await self._finish_dispatch_without_reply(
                    group, match, "stopped", f"@{match['handle']}'s message was removed from its queue.")
                round_ = group.get("rounds", {}).get(match.get("round_id"))
                if round_ is not None:
                    await self._advance_round(group, round_)
            return
        match = next((item for item in dispatches if run_id and item.get("run_id") == run_id), None)
        if match is not None:
            await self._complete_dispatch(group, match, event)
            return
        if run_id and any(item.get("status") == "starting" for item in dispatches):
            # The turn ended before start_member_turn returned its run id.
            self._early_terminals[(session_id, run_id)] = event
            while len(self._early_terminals) > MAX_EARLY_TERMINALS:
                self._early_terminals.pop(next(iter(self._early_terminals)))
            return
        if event.get("type") != "turn_finished":
            return
        text = self.runtime.reply_text(event).strip()
        handle = self.member_index.get(session_id, (None, None))[1]
        if text and handle:
            # A turn the group did not start: a native goal continuation or a
            # message sent directly in the member chat. Mirror it, do not route.
            await self._append_message(group, author=handle, kind="agent", text=text, round_id=None,
                                       mentions=[], extra={"origin": "direct"})

    # -- control -------------------------------------------------------------

    async def stop(self, group_id: str, *, reason: str = "stopped") -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id)
            for round_ in group.get("rounds", {}).values():
                if round_.get("status") == "open":
                    round_["status"] = "stopped"
                    round_["queue"] = []
                    round_["finished_at"] = now()
                    await self.append(group, "round_finished", {"round_id": round_["id"], "status": "stopped",
                                                                "hops": round_.get("hops", 0), "reason": reason})
            dispatch_ids = [item["id"] for item in group.get("dispatches", {}).values()
                            if item.get("status") in ACTIVE_DISPATCH_STATES]
            await self.save(group)
        stopped_sessions: set[str] = set()
        for dispatch_id in dispatch_ids:
            # Re-read live state each time: a queued turn may have started.
            async with self.lock(group_id):
                group = self.groups.get(group_id)
                live = (group or {}).get("dispatches", {}).get(dispatch_id)
                if not group or not live or live.get("status") not in ACTIVE_DISPATCH_STATES:
                    continue
                status, session_id, queued_id = live["status"], live["session_id"], live.get("queued_id")
            try:
                if status == "queued" and queued_id:
                    if await self.runtime.cancel_queued(session_id, queued_id):
                        async with self.lock(group_id):
                            group = self.groups.get(group_id)
                            live = (group or {}).get("dispatches", {}).get(dispatch_id)
                            if group and live and live.get("status") == "queued":
                                self._restore_member(group, live)
                                await self._finish_dispatch_without_reply(
                                    group, live, "stopped", f"@{live['handle']} was stopped before it started.")
                                await self.save(group)
                        continue
                if session_id not in stopped_sessions:
                    stopped_sessions.add(session_id)
                    await self.runtime.stop_member(session_id)
            except Exception:
                logger.exception("group stop failed for member session=%s", session_id)
        async with self.lock(group_id):
            group = self.require(group_id)
            await self._settle_orphaned_dispatches(group)
            await self.save(group)
            return self.public(group)

    async def _settle_orphaned_dispatches(self, group: dict[str, Any]) -> None:
        """Finish dispatches that can no longer report an end of their own."""

        for dispatch in list(group.get("dispatches", {}).values()):
            if dispatch.get("status") not in ACTIVE_DISPATCH_STATES:
                continue
            status = self.runtime.member_status(dispatch["session_id"])
            if not status.get("exists", True):
                await self._finish_dispatch_without_reply(
                    group, dispatch, "failed", f"@{dispatch['handle']} is unavailable because its member chat was removed.")
            elif not status.get("running") and not status.get("queued") and dispatch.get("status") != "starting":
                await self._finish_dispatch_without_reply(group, dispatch, "stopped", f"@{dispatch['handle']} was stopped.")

    async def set_goal(self, group_id: str, req: GroupGoalRequest) -> dict[str, Any]:
        async with self.lock(group_id):
            group = self.require(group_id, writable=True)
            participant = self.participant(group, req.handle)
            if participant is None:
                raise GroupError(status_code=404, detail="participant not found")
            session_id = participant["session_id"]
        result = await self.runtime.set_member_goal(session_id, req.objective.strip())
        async with self.lock(group_id):
            group = self.require(group_id)
            group["goal"] = {"handle": req.handle, "objective": req.objective.strip(), "set_at": now()}
            await self.append(group, "goal_set", {"handle": req.handle, "objective": req.objective.strip()})
            await self.save(group)
            return {"group": self.public(group), "result": result}

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        groups = [group for group in self.groups.values() if include_archived or not group.get("archived")]
        groups.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
        return [self.public(group, summary=True) for group in groups]

    async def drain(self) -> None:
        """Wait for scheduled member-event handlers (tests and shutdown)."""

        while True:
            pending = [task for task in self._tasks if not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                continue
            # Awaiting finished tasks never yields; let done-callbacks and any
            # handlers they scheduled run before deciding that nothing is left.
            await asyncio.sleep(0)
            if all(task.done() for task in self._tasks):
                return

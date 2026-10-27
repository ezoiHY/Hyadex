"""The server side of group discussions: how the group engine drives member chats."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any
import uuid

from fastapi import HTTPException

from hyadex import groups as group_chat
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    DEFAULT_BACKEND,
    DEFAULT_CWD,
    require_enabled_backend,
    STATE_DIR,
)
from hyadex.core.event_log import read_events
from hyadex.core.paths import existing_cwd, now_iso
from hyadex.core.queue import unqueue_turn
from hyadex.core.runs import RUNS
from hyadex.core.sessions import (
    CreateSessionRequest,
    ensure_session_not_deleting,
    session_lifecycle_lock,
    STORE,
    UpdateSessionRequest,
)
from hyadex.core.state import EVENTS
from hyadex.core.stop import request_explicit_stop
from hyadex.core.turns import interactive_client_capabilities, set_claude_goal, start_turn_durably
from hyadex.providers.codex.controls import _put_codex_goal_locked
from hyadex.core.util import clean_assistant_text
from hyadex.core.wiring import WIRING
from hyadex.features.terminal import _update_session
from hyadex.features.usage import HUB
from hyadex.providers.claude.runtime import ClaudeGoalRequest, TurnRequest
from hyadex.providers.codex.goals import CodexGoalRequest


def new_job_revision() -> str:
    """Return a private persisted CAS revision for one scheduled job."""

    return "job_rev_" + uuid.uuid4().hex


class HyadexGroupRuntime:
    """Connect group discussions to ordinary member chats.

    Every group participant is a normal session marked with ``group_member``:
    its turns use the same admission, queue, provider runtime and recovery as
    any chat, and it loads the agent's own MCP servers, skills and settings.
    """

    def resolve_cwd(self, cwd: str | None) -> str:
        requested = (cwd or "").strip()
        if requested:
            path = Path(requested).expanduser()
            if not path.is_dir():
                raise HTTPException(status_code=400, detail="The group working directory does not exist.")
            return str(path.resolve())
        return str(Path(existing_cwd(DEFAULT_CWD)).resolve())

    def validate_backend(self, backend: str) -> None:
        require_enabled_backend(backend)
        interactive_client_capabilities({"backend": backend})

    async def create_member_session(self, *, group_id: str, title: str, handle: str,
                                    spec: dict[str, Any], cwd: str) -> str:
        request = CreateSessionRequest(
            title=title[:200], auto_title_enabled=False, cwd=cwd, backend=spec["backend"],
            model=spec.get("model"), effort=spec.get("effort"),
            claude_permission_mode=spec.get("claude_permission_mode"),
            codex_approval_policy=spec.get("codex_approval_policy"),
            codex_sandbox_mode=spec.get("codex_sandbox_mode"),
        )
        session = await STORE.create(request)
        async with STORE.transaction():
            session["group_member"] = {"group_id": group_id, "handle": handle}
        await STORE.save()
        return str(session["id"])

    async def start_member_turn(self, session_id: str, *, prompt: str, request_id: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="member chat not found")
        # The dispatch id is recorded on turn_queued/turn_started, so restart
        # recovery can find this exact turn.
        request = TurnRequest(prompt=prompt, client_request_id=request_id,
                              client_capabilities=interactive_client_capabilities(session))
        result = await start_turn_durably(session_id, request)
        return {"run_id": result.get("run_id"), "queued_id": result.get("queued_id"),
                "queued": bool(result.get("queued"))}

    async def stop_member(self, session_id: str) -> None:
        if session_id in STORE.sessions:
            await request_explicit_stop(session_id)

    async def cancel_queued(self, session_id: str, queued_id: str) -> bool:
        if session_id not in STORE.sessions:
            return False
        try:
            await unqueue_turn(session_id, queued_id)
        except HTTPException:
            # Already started, already gone, or the queue is temporarily fenced.
            return False
        return True

    async def retire_member_session(self, session_id: str) -> None:
        session = STORE.sessions.get(session_id)
        if session and not session.get("archived"):
            await _update_session(session_id, UpdateSessionRequest(archived=True))

    def member_status(self, session_id: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            return {"exists": False}
        return {
            "exists": True,
            "running": session_id in RUNS.busy,
            "queued": len(RUNS.queued.get(session_id) or ()),
            "needs_action": bool(session.get("claude_needs_user_action") or session.get("codex_needs_user_action")),
        }

    def reply_text(self, event: dict[str, Any]) -> str:
        return clean_assistant_text(str(event.get("result_text") or ""))

    async def find_member_progress(self, session_id: str, *, run_id: str | None, queued_id: str | None,
                                   request_id: str) -> dict[str, Any]:
        if session_id not in STORE.sessions:
            return {"exists": False}
        events = await asyncio.to_thread(
            read_events, session_id, 0, None, 1_000_000,
            include_internal=True, cap_to_response_limit=False,
        )
        resolved_run, resolved_queue = run_id, queued_id
        for event in events:
            if event.get("client_request_id") != request_id:
                continue
            if event.get("type") == "turn_started" and event.get("run_id"):
                resolved_run = resolved_run or str(event["run_id"])
            elif event.get("type") == "turn_queued" and event.get("queued_id"):
                resolved_queue = resolved_queue or str(event["queued_id"])
        if not resolved_run and resolved_queue:
            resolved_run = next((str(event["run_id"]) for event in events
                                 if event.get("type") == "turn_started" and event.get("queued_id") == resolved_queue
                                 and event.get("run_id")), None)
        terminal = None
        if resolved_run:
            terminal = next((event for event in reversed(events)
                             if event.get("type") in group_chat.TERMINAL_EVENT_TYPES
                             and str(event.get("run_id") or "") == resolved_run), None)
        unqueued = bool(resolved_queue and not resolved_run and any(
            event.get("type") == "turn_unqueued" and event.get("queued_id") == resolved_queue for event in events))
        return {"exists": True, "run_id": resolved_run, "queued_id": resolved_queue,
                "terminal": terminal, "unqueued": unqueued}

    async def set_member_goal(self, session_id: str, objective: str) -> dict[str, Any]:
        session = STORE.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="member chat not found")
        backend = str(session.get("backend") or DEFAULT_BACKEND)
        if backend == BACKEND_CLAUDE:
            return await set_claude_goal(session_id, ClaudeGoalRequest(condition=objective))
        if backend == BACKEND_CODEX:
            async with session_lifecycle_lock(session_id):
                ensure_session_not_deleting(session_id)
                return await _put_codex_goal_locked(
                    session_id, CodexGoalRequest(objective=objective, status="active"))
        raise HTTPException(status_code=400, detail="Goals require a Claude or Codex member")

    async def broadcast(self, key: str, event: dict[str, Any]) -> None:
        await HUB.broadcast(key, event)

    async def participant_removed(self, group_id: str, removed_session_id: str, anchor_session_id: str) -> None:
        async with WIRING.jobs._lock:
            moved = False
            for job in WIRING.jobs.jobs.values():
                if job.get("group_id") == group_id and job.get("session_id") == removed_session_id:
                    job["session_id"] = anchor_session_id
                    job["updated_at"] = now_iso()
                    job["_revision"] = new_job_revision()
                    moved = True
            if moved:
                await WIRING.jobs.save()

    async def group_deleted(self, group_id: str) -> None:
        for job_id, job in list(WIRING.jobs.jobs.items()):
            if job.get("group_id") == group_id:
                with suppress(HTTPException):
                    await WIRING.jobs.delete(job_id, expected_session_id=job.get("session_id"))


GROUPS = group_chat.GroupEngine(STATE_DIR / "groups", HyadexGroupRuntime())

# Group discussions follow their member chats' turns; observing only schedules
# the engine's own work and never delays the chat's event path.
EVENTS.on_observe(lambda session_id, event: GROUPS.observe_member_event(session_id, event),
                  types=group_chat.OBSERVED_EVENT_TYPES)


async def start_group_job_run(group_id: str, job: dict[str, Any], job_id: str) -> dict[str, Any]:
    posted = await GROUPS.post_message(
        group_id,
        group_chat.PostGroupMessageRequest(text=str(job.get("prompt") or "")),
        origin="job",
        meta={"job_id": job_id, "job_title": str(job.get("title") or job_id)},
    )
    return {"run_id": None, "group_id": group_id, "group_message_seq": posted["message"]["seq"],
            "targets": posted["targets"]}

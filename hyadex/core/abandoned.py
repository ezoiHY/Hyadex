"""Recovering turns abandoned by a server restart."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging.handlers
from pathlib import Path
from typing import Any

from hyadex.core.paths import events_path
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import append_event, STORE

logger = logging.getLogger("hyadex-server")

RESTART_ORPHAN_ACTIVITY_TYPES = {
    "turn_started",
    "process_started",
    "provider_session",
    "assistant_text",
    "reasoning_summary",
    "reasoning_text",
    "tool_started",
    "tool_finished",
    "artifact_created",
    "idle_warning",
    # Provider errors are emitted before the normal turn_finished marker. A
    # crash in that narrow window still leaves an open turn to retire.
    "error",
}
RESTART_ORPHAN_TERMINAL_TYPES = {
    "turn_finished",
    "turn_stopped",
}


def abandoned_turn_after_restart(
    session_id: str,
    session: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the newest run left open when the server process disappeared.

    Active turns live in memory, so a SIGKILL cannot persist their normal
    terminal event.  Restrict the recovery scan to chats whose last durable
    event is turn activity; this keeps startup proportional to the number of
    interrupted chats instead of rescanning every large history.
    """

    persisted_active = (
        dict(session.get("active_run"))
        if isinstance(session.get("active_run"), dict)
        else {}
    )
    latest_type = str(session.get("latest_event_type") or "")
    if (
        not str(persisted_active.get("run_id") or "").strip()
        and latest_type not in RESTART_ORPHAN_ACTIVITY_TYPES
        and not latest_type.startswith("codex_")
    ):
        return None
    records = tail_jsonl_file(
        events_path(session_id),
        limit=200,
        max_bytes=8 * 1024 * 1024,
    )
    active_run_id = str(persisted_active.get("run_id") or "").strip()
    open_runs: dict[str, dict[str, Any]] = (
        {active_run_id: persisted_active}
        if active_run_id
        else {}
    )
    closed_runs: set[str] = set()
    for event in records:
        event_type = str(event.get("type") or "")
        run_id = str(event.get("run_id") or "").strip()
        if not run_id:
            continue
        if event_type in RESTART_ORPHAN_TERMINAL_TYPES:
            open_runs.pop(run_id, None)
            closed_runs.add(run_id)
            continue
        if event_type == "turn_started":
            closed_runs.discard(run_id)
            open_runs[run_id] = dict(event)
            open_runs[run_id]["last_activity_seq"] = int(event.get("seq") or 0)
            continue
        if event_type in RESTART_ORPHAN_ACTIVITY_TYPES and run_id not in closed_runs:
            candidate = open_runs.setdefault(run_id, {"run_id": run_id})
            candidate["last_activity_seq"] = int(event.get("seq") or 0)
            for key in (
                "backend",
                "purpose",
                "provider_context_mode",
                "job_context_mode",
                "job_id",
                "job_title",
                "digest_job_id",
                "source_session_id",
                "target_session_id",
                "cross_chat_envelope_id",
            ):
                if candidate.get(key) is None and event.get(key) is not None:
                    candidate[key] = event.get(key)
    if not open_runs:
        return None
    return max(
        open_runs.values(),
        key=lambda event: int(event.get("last_activity_seq") or event.get("seq") or 0),
    )


async def recover_abandoned_turns_after_start(
    *,
    forced_restart_request_id: str | None = None,
) -> int:
    """Close runs orphaned by a crash without replaying accepted user input."""

    session_items = [
        (session_id, dict(session))
        for session_id, session in STORE.sessions.items()
    ]

    def scan_candidates() -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for session_id, session in session_items:
            try:
                abandoned = abandoned_turn_after_restart(session_id, session)
            except Exception as exc:
                logger.warning(
                    "could not inspect abandoned turn session=%s error=%s",
                    session_id,
                    concise_error_message(exc),
                )
                continue
            if abandoned:
                found.append((session_id, abandoned))
        return found

    candidates = await asyncio.to_thread(scan_candidates)
    recovered = 0
    for session_id, abandoned in candidates:
        run_id = str(abandoned.get("run_id") or "").strip()
        if not run_id:
            continue
        payload: dict[str, Any] = {
            "run_id": run_id,
            "backend": abandoned.get("backend")
            or (STORE.sessions.get(session_id) or {}).get("backend"),
            "stopped": True,
            "recovered_after_restart": True,
            "message": (
                "This agent turn was interrupted when HyadexServer restarted. "
                "Its accepted user message was not replayed."
            ),
        }
        if forced_restart_request_id:
            payload.update({
                "forced_restart": True,
                "restart_request_id": forced_restart_request_id,
                "message": (
                    "This agent turn was forcibly interrupted when HyadexServer "
                    "restarted. Its accepted user message was not replayed."
                ),
            })
        for key in (
            "purpose",
            "provider_context_mode",
            "job_context_mode",
            "job_id",
            "job_title",
            "digest_job_id",
            "source_session_id",
            "target_session_id",
            "cross_chat_envelope_id",
            "secure_peer_envelope_id",
        ):
            if abandoned.get(key) is not None:
                payload[key] = abandoned[key]
        try:
            await append_event(session_id, "turn_stopped", payload)
        except Exception as exc:
            logger.warning(
                "could not close abandoned turn session=%s run=%s error=%s",
                session_id,
                run_id,
                concise_error_message(exc),
            )
        else:
            recovered += 1
    if recovered:
        logger.warning(
            "closed abandoned agent turns after restart count=%d",
            recovered,
        )
    return recovered


def tail_jsonl_file(path: Path, limit: int = 40, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    line_count = max(1, min(int(limit or 40), 200))
    size = path.stat().st_size
    byte_count = max(4096, min(int(max_bytes or 2 * 1024 * 1024), 8 * 1024 * 1024))
    with path.open("rb") as f:
        if size > byte_count:
            f.seek(size - byte_count)
        data = f.read(byte_count)
    records: list[dict[str, Any]] = []
    for line in data.decode("utf-8", "replace").splitlines()[-line_count:]:
        with suppress(Exception):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    return records

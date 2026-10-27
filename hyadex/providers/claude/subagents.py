"""Claude subagents: SDK lifecycle and snapshots."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging.handlers
import re
from typing import Any

from claude_background_reconciliation import normalize_task_receipts
from hyadex.core.config import (
    BACKEND_CLAUDE,
    STOP_CONFIRM_TIMEOUT_SECONDS,
    SUBAGENT_SNAPSHOT_LOG_LIMIT,
    SUBAGENT_SNAPSHOT_STATE_LIMIT,
    SUBAGENT_SNAPSHOT_TEXT_LIMIT,
)
from hyadex.core.paths import events_path
from hyadex.core.server_logging import concise_error_message
from hyadex.providers.claude.sdk import ClaudeSDKSupervisorManager
from hyadex.providers.state import PROVIDERS

logger = logging.getLogger("hyadex-server")

CLAUDE_STOP_FENCE_ATTEMPT_LOCKS: dict[str, asyncio.Lock] = {}
CLAUDE_STOP_FENCE_ATTEMPT_TASKS: dict[str, set[asyncio.Task[Any]]] = {}


def compact_subagent_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:SUBAGENT_SNAPSHOT_TEXT_LIMIT]


def normalize_subagent_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status == "tracking_lost":
        return "tracking_lost"
    if status in {"completed", "complete", "done"}:
        return "completed"
    if status in {"failed", "error", "errored", "systemerror", "notfound"}:
        return "failed"
    if status in {
        "stopped",
        "cancelled",
        "canceled",
        "killed",
        "interrupted",
        "shutdown",
        "closed",
    }:
        return "stopped"
    if status in {"starting", "pending", "pendinginit", "queued"}:
        return "starting"
    return "running"


def claude_subagent_child_activity(raw: dict[str, Any]) -> str:
    """Return a safe, compact activity label without exposing child output."""
    if raw.get("type") != "assistant":
        return ""
    message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {"tool_use", "server_tool_use"}:
            continue
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        description = compact_subagent_text(tool_input.get("description"))
        if description:
            return description
        tool_name = compact_subagent_text(block.get("name")) or "tool"
        return f"Using {tool_name}"
    return ""


def claude_subagent_progress_activity(*records: dict[str, Any]) -> str:
    for record in records:
        progress = record.get("progress")
        if isinstance(progress, dict):
            for key in ("summary", "description", "message"):
                text = compact_subagent_text(progress.get(key))
                if text:
                    return text
        elif progress is not None:
            text = compact_subagent_text(progress)
            if text:
                return text
        for key in ("summary", "description", "message"):
            text = compact_subagent_text(record.get(key))
            if text:
                return text
    for record in records:
        tool_name = compact_subagent_text(record.get("last_tool_name"))
        if tool_name:
            return f"Using {tool_name}"
    return ""


def build_claude_subagent_snapshot(session_id: str, limit: int = 64) -> dict[str, Any]:
    """Fold Claude's provider events into a bounded, durable subagent snapshot.

    Only explicitly selected lifecycle fields are projected. Provider prompts,
    tool-result output, commands, output files, and the raw event are never
    copied into the response.
    """
    limit = max(1, min(int(limit or 64), SUBAGENT_SNAPSHOT_STATE_LIMIT))
    states: OrderedDict[str, dict[str, Any]] = OrderedDict()
    task_keys: dict[str, str] = {}
    tool_keys: dict[str, str] = {}
    latest_seq = 0

    def scoped(run_id: str, provider_id: str) -> str:
        return f"{run_id}\x00{provider_id}"

    def forget(state_key: str) -> None:
        states.pop(state_key, None)
        for index in (task_keys, tool_keys):
            for provider_key, candidate in list(index.items()):
                if candidate == state_key:
                    index.pop(provider_key, None)

    def prune() -> None:
        while len(states) > SUBAGENT_SNAPSHOT_STATE_LIMIT:
            terminal_key = next((
                key for key, state in states.items()
                if state["status"] not in {"starting", "running"}
            ), None)
            forget(terminal_key or next(iter(states)))

    def find(run_id: str, task_id: str = "", tool_id: str = "") -> tuple[str | None, dict[str, Any] | None]:
        state_key = task_keys.get(scoped(run_id, task_id)) if task_id else None
        if state_key is None and tool_id:
            state_key = tool_keys.get(scoped(run_id, tool_id))
        return state_key, states.get(state_key) if state_key else None

    def merge_states(primary_key: str, secondary_key: str) -> dict[str, Any]:
        primary = states[primary_key]
        secondary = states[secondary_key]
        if primary["name"] == "Claude subagent" and secondary["name"] != "Claude subagent":
            primary["name"] = secondary["name"]
        if primary["kind"] == "agent" and secondary["kind"] != "agent":
            primary["kind"] = secondary["kind"]
        started = [value for value in (primary["started_at"], secondary["started_at"]) if value]
        if started:
            primary["started_at"] = min(started)
        if int(secondary["seq"]) > int(primary["seq"]):
            primary["updated_at"] = secondary["updated_at"]
            primary["activity"] = secondary["activity"] or primary["activity"]
            primary["summary"] = secondary["summary"] or primary["summary"]
            primary["status"] = secondary["status"]
        primary["seq"] = max(int(primary["seq"]), int(secondary["seq"]))
        primary["background"] = bool(primary["background"] or secondary["background"])
        primary["saw_task_started"] = bool(primary["saw_task_started"] or secondary["saw_task_started"])
        combined_log = sorted(
            [*primary["log"], *secondary["log"]],
            key=lambda entry: str(entry.get("ts") or ""),
        )
        primary["log"] = []
        for entry in combined_log:
            if primary["log"] and primary["log"][-1]["text"] == entry["text"]:
                continue
            primary["log"].append(entry)
        primary["log"] = primary["log"][-SUBAGENT_SNAPSHOT_LOG_LIMIT:]
        forget(secondary_key)
        states.move_to_end(primary_key)
        return primary

    def ensure(
        event: dict[str, Any],
        *,
        task_id: str = "",
        tool_id: str = "",
        name: Any = "",
        kind: Any = "",
        status: str = "running",
        background: bool | None = None,
    ) -> dict[str, Any]:
        run_id = str(event.get("run_id") or "")
        task_state_key = task_keys.get(scoped(run_id, task_id)) if task_id else None
        tool_state_key = tool_keys.get(scoped(run_id, tool_id)) if tool_id else None
        if task_state_key and tool_state_key and task_state_key != tool_state_key:
            merge_states(task_state_key, tool_state_key)
        state_key, state = find(run_id, task_id, tool_id)
        if state is None:
            provider_id = task_id or tool_id
            state_key = f"{run_id}\x00{provider_id}\x00{event.get('seq', 0)}"
            state = {
                "task_id": task_id or tool_id,
                "tool_id": tool_id,
                "run_id": run_id,
                "name": compact_subagent_text(name) or "Claude subagent",
                "kind": compact_subagent_text(kind) or "agent",
                "status": normalize_subagent_status(status),
                "started_at": str(event.get("ts") or ""),
                "updated_at": str(event.get("ts") or ""),
                "seq": int(event.get("seq") or 0),
                "summary": "",
                "activity": "",
                "log": [],
                "background": bool(background),
                "saw_task_started": False,
            }
            states[state_key] = state
        if task_id:
            state["task_id"] = task_id
            task_keys[scoped(run_id, task_id)] = state_key
        if tool_id:
            state["tool_id"] = tool_id
            tool_keys[scoped(run_id, tool_id)] = state_key
        clean_name = compact_subagent_text(name)
        clean_kind = compact_subagent_text(kind)
        if clean_name:
            state["name"] = clean_name
        if clean_kind:
            state["kind"] = clean_kind
        if background is not None:
            state["background"] = bool(background)
        state["status"] = normalize_subagent_status(status)
        state["updated_at"] = str(event.get("ts") or state["updated_at"])
        state["seq"] = max(int(state["seq"]), int(event.get("seq") or 0))
        states.move_to_end(state_key)
        prune()
        return state

    def note(state: dict[str, Any], event: dict[str, Any], value: Any) -> None:
        text = compact_subagent_text(value)
        state["updated_at"] = str(event.get("ts") or state["updated_at"])
        state["seq"] = max(int(state["seq"]), int(event.get("seq") or 0))
        if not text:
            return
        state["activity"] = text
        if not state["log"] or state["log"][-1]["text"] != text:
            state["log"].append({"ts": state["updated_at"], "text": text})
            del state["log"][:-SUBAGENT_SNAPSHOT_LOG_LIMIT]

    path = events_path(session_id)
    if path.exists():
        with path.open("r", encoding="utf-8", errors="ignore") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                seq = int(event.get("seq") or 0)
                latest_seq = max(latest_seq, seq)
                event_type = str(event.get("type") or "")
                run_id = str(event.get("run_id") or "")

                if event_type == "claude_background_tasks_reconciled" and event.get("backend") == BACKEND_CLAUDE and event.get("imported") is not True:
                    for receipt in normalize_task_receipts(
                        event.get("tasks"), run_id=run_id,
                        provider_session_id=event.get("provider_session_id"),
                    ):
                        _, previous = find(run_id, receipt["task_id"], receipt.get("tool_use_id", ""))
                        state = ensure(
                            event, task_id=receipt["task_id"],
                            tool_id=receipt.get("tool_use_id", ""),
                            name="" if previous is not None else "Claude background task",
                            kind="" if previous is not None else receipt["task_type"],
                            status=receipt["status"], background=True,
                        )
                        note(state, event, (
                            "Background task tracking lost; completion is not confirmed"
                            if receipt["status"] == "tracking_lost"
                            else f"Background task {state['status']}"
                        ))
                    continue

                if event_type == "tool_started":
                    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
                    if str(tool.get("name") or "").strip().lower() != "agent":
                        continue
                    tool_id = str(tool.get("id") or "")
                    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
                    state = ensure(
                        event,
                        tool_id=tool_id,
                        name=tool_input.get("description") or tool_input.get("subagent_type"),
                        kind=tool_input.get("subagent_type"),
                        status="starting",
                        background=tool_input.get("run_in_background") is True,
                    )
                    note(state, event, f"Starting {state['name']}")
                    continue

                if event_type == "tool_finished":
                    tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
                    tool_id = str(event.get("tool_id") or tool.get("id") or "")
                    _, state = find(run_id, tool_id=tool_id)
                    if state is None:
                        continue
                    if event.get("is_error") is True:
                        state["status"] = "failed"
                        note(state, event, "Subagent failed")
                    elif state["background"] or state["saw_task_started"]:
                        if state["status"] == "starting":
                            state["status"] = "running"
                        note(state, event, state["activity"])
                    elif state["status"] in {"starting", "running"}:
                        state["status"] = "completed"
                        note(state, event, "Subagent completed")
                    continue

                if event_type == "raw_event" and event.get("backend") == BACKEND_CLAUDE:
                    try:
                        raw = json.loads(str(event.get("raw") or ""))
                    except Exception:
                        continue
                    if not isinstance(raw, dict):
                        continue
                    subtype = str(raw.get("subtype") or "")
                    task_id = str(raw.get("task_id") or "")
                    tool_id = str(raw.get("tool_use_id") or "")

                    if raw.get("type") == "user":
                        tool_result = raw.get("tool_use_result") if isinstance(raw.get("tool_use_result"), dict) else {}
                        result_status = str(tool_result.get("status") or "").strip().lower()
                        is_async = tool_result.get("isAsync") is True or result_status in {"async_launched", "running", "pending"}
                        if is_async:
                            message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
                            content = message.get("content") if isinstance(message.get("content"), list) else []
                            result_tool_id = next((
                                str(block.get("tool_use_id") or "")
                                for block in content
                                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
                            ), "")
                            async_task_id = str(tool_result.get("agentId") or tool_result.get("task_id") or "")
                            _, state = find(run_id, async_task_id, result_tool_id)
                            if state is not None:
                                state = ensure(
                                    event,
                                    task_id=async_task_id,
                                    tool_id=result_tool_id,
                                    status="running",
                                    background=True,
                                )
                                note(state, event, "Subagent running")
                        continue

                    if raw.get("type") == "system" and subtype == "background_tasks_changed":
                        tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
                        for task in tasks:
                            if not isinstance(task, dict) or task.get("task_type") != "local_agent":
                                continue
                            background_task_id = str(task.get("task_id") or "")
                            if not background_task_id:
                                continue
                            state = ensure(
                                event,
                                task_id=background_task_id,
                                name=task.get("description"),
                                status=normalize_subagent_status(task.get("status")),
                                background=True,
                            )
                            note(state, event, task.get("description") or f"Subagent {state['status']}")
                        continue

                    if raw.get("type") == "system" and subtype == "task_started" and raw.get("task_type") == "local_agent" and task_id:
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            name=raw.get("description") or raw.get("subagent_type"),
                            kind=raw.get("subagent_type"),
                            status="running",
                        )
                        state["saw_task_started"] = True
                        note(state, event, raw.get("description") or "Subagent started")
                        continue

                    if raw.get("type") == "system" and subtype == "task_progress" and task_id:
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status="running",
                        )
                        state["status"] = "running"
                        note(state, event, claude_subagent_progress_activity(raw) or "Working")
                        continue

                    if raw.get("type") == "system" and subtype == "task_updated" and task_id:
                        patch = raw.get("patch") if isinstance(raw.get("patch"), dict) else {}
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        status_value = patch.get("status") or raw.get("status") or (state["status"] if state else "running")
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status=normalize_subagent_status(status_value),
                            background=True if patch.get("is_backgrounded") is True else None,
                        )
                        state["status"] = normalize_subagent_status(status_value)
                        summary = compact_subagent_text(patch.get("summary") or raw.get("summary"))
                        if summary:
                            state["summary"] = summary
                        activity = claude_subagent_progress_activity(patch, raw)
                        note(state, event, activity or f"Subagent {state['status']}")
                        continue

                    if raw.get("type") == "system" and subtype == "task_notification" and task_id:
                        _, state = find(run_id, task_id, tool_id)
                        is_agent = state is not None or raw.get("task_type") == "local_agent" or bool(raw.get("subagent_type"))
                        if not is_agent:
                            continue
                        state = ensure(
                            event,
                            task_id=task_id,
                            tool_id=tool_id,
                            kind=raw.get("subagent_type"),
                            status=normalize_subagent_status(raw.get("status")),
                        )
                        state["status"] = normalize_subagent_status(raw.get("status"))
                        state["summary"] = compact_subagent_text(raw.get("summary"))
                        note(state, event, state["summary"] or f"Subagent {state['status']}")
                        continue

                    parent_tool_id = str(raw.get("parent_tool_use_id") or "")
                    if parent_tool_id:
                        _, state = find(run_id, tool_id=parent_tool_id)
                        activity = claude_subagent_child_activity(raw)
                        if state is not None and activity:
                            note(state, event, activity)
                    continue

                if event_type in {"turn_finished", "turn_stopped", "error"}:
                    if event_type == "turn_stopped":
                        terminal_status, activity = "stopped", "Parent turn stopped"
                    elif event_type == "error":
                        terminal_status, activity = "failed", "Parent turn ended with an error"
                    else:
                        exit_code = event.get("exit_code")
                        terminal_status = "failed" if exit_code not in (None, 0) else "completed"
                        activity = "Parent turn ended with an error" if terminal_status == "failed" else "Parent turn completed"
                    for state in list(states.values()):
                        if state["run_id"] != run_id or state["status"] not in {"starting", "running"}:
                            continue
                        if event_type == "turn_finished" and state["background"]:
                            continue
                        state["status"] = terminal_status
                        note(state, event, activity)
                    continue

                if event_type == "claude_subagents_stopped":
                    target_run_ids = {
                        str(value or "").strip()
                        for value in event.get("run_ids") or []
                        if str(value or "").strip()
                    }
                    target_task_ids = {
                        str(value or "").strip()
                        for value in event.get("subagent_ids") or []
                        if str(value or "").strip()
                    }
                    stop_all = event.get("all") is True
                    for state in list(states.values()):
                        if state["status"] not in {"starting", "running"}:
                            continue
                        if not stop_all and (
                            state["run_id"] not in target_run_ids
                            and state["task_id"] not in target_task_ids
                        ):
                            continue
                        state["status"] = "stopped"
                        note(state, event, "Stopped with parent chat")

    active_statuses = {"starting", "running"}
    retained = list(states.values())
    retained.sort(key=lambda state: (
        0 if state["status"] in active_statuses else 1,
        -int(state["seq"]),
    ))
    selected = retained[:limit]
    projected = [{
        "seq": int(state["seq"]),
        "id": f"subagent:{session_id}:{state['run_id']}:{state['task_id']}",
        "session_id": session_id,
        "run_id": state["run_id"],
        "backend": BACKEND_CLAUDE,
        "type": "subagent_state",
        "ts": state["updated_at"],
        "subagent_id": state["task_id"],
        "subagent_tool_id": state["tool_id"] or None,
        "subagent_name": state["name"],
        "subagent_kind": state["kind"],
        "subagent_status": state["status"],
        "subagent_activity": state["activity"] or None,
        "subagent_summary": state["summary"] or None,
        "subagent_started_at": state["started_at"],
        "subagent_log": list(state["log"]),
    } for state in selected]
    return {
        "session_id": session_id,
        "subagents": projected,
        "count": len(retained),
        "active_count": sum(state["status"] in active_statuses for state in retained),
        "latest_seq": latest_seq,
    }


async def evict_claude_sdk_chat(
    session_id: str,
    *,
    force: bool,
    manager: ClaudeSDKSupervisorManager | None = None,
    ownership_token: str | None = None,
) -> bool:
    """Return when one chat is absent/closed, without touching other clients."""

    selected_manager = manager or PROVIDERS.claude_sdk_manager
    if selected_manager is None:
        return True
    try:
        evict_kwargs: dict[str, Any] = {"force": force}
        if ownership_token is not None:
            evict_kwargs["ownership_token"] = ownership_token
        await asyncio.wait_for(
            selected_manager.evict(session_id, **evict_kwargs),
            timeout=STOP_CONFIRM_TIMEOUT_SECONDS,
        )
        # ``False`` means there was no supervisor, which is already the
        # desired settled state.
        return True
    except asyncio.TimeoutError:
        logger.error(
            "Claude SDK chat eviction timed out session=%s force=%s",
            session_id,
            force,
        )
        return False
    except Exception as exc:
        logger.error(
            "Claude SDK chat eviction failed session=%s force=%s: %s",
            session_id,
            force,
            concise_error_message(exc),
        )
        return False


def claude_stop_fence_attempt_lock(session_id: str) -> asyncio.Lock:
    lock = CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        CLAUDE_STOP_FENCE_ATTEMPT_LOCKS[session_id] = lock
    return lock


def discard_unused_claude_stop_fence_attempt_lock(session_id: str) -> None:
    if CLAUDE_STOP_FENCE_ATTEMPT_TASKS.get(session_id):
        return
    lock = CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.get(session_id)
    if lock is not None and not lock.locked():
        CLAUDE_STOP_FENCE_ATTEMPT_LOCKS.pop(session_id, None)


def claude_sdk_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def claude_sdk_type(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("type") or "")
    return type(value).__name__


def claude_sdk_subagent_lifecycle_raw(message: Any) -> dict[str, Any] | None:
    """Return the minimum legacy-shaped record needed by subagent folding.

    ``claude -p`` stores the provider's complete stream record as ``raw_event``.
    The typed SDK must not regress background-agent status, but it also should
    not persist prompts, child output, or arbitrary provider payloads merely to
    feed that fold.  Project only the lifecycle identifiers and short labels
    consumed by :func:`build_claude_subagent_snapshot`.
    """

    message_type = claude_sdk_type(message)
    if message_type == "UserMessage":
        result = claude_sdk_field(message, "tool_use_result")
        if not isinstance(result, dict):
            return None
        status = str(result.get("status") or "").strip().lower()
        is_async = result.get("isAsync") is True or status in {
            "async_launched", "running", "pending",
        }
        if not is_async:
            return None
        tool_results = []
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolResultBlock", "ServerToolResultBlock",
                "tool_result", "server_tool_result", "advisor_tool_result",
            }:
                continue
            tool_id = str(claude_sdk_field(block, "tool_use_id") or "")[:512]
            if tool_id:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                })
            if len(tool_results) >= 32:
                break
        return {
            "type": "user",
            "tool_use_result": {
                "status": status[:64],
                "isAsync": True,
                "agentId": str(result.get("agentId") or "")[:512],
                "task_id": str(result.get("task_id") or "")[:512],
            },
            "message": {"content": tool_results},
        }

    if message_type == "AssistantMessage":
        parent_tool_id = str(
            claude_sdk_field(message, "parent_tool_use_id") or ""
        )[:512]
        if not parent_tool_id:
            return None
        safe_blocks: list[dict[str, Any]] = []
        content = claude_sdk_field(message, "content", [])
        for block in content if isinstance(content, list) else []:
            if claude_sdk_type(block) not in {
                "ToolUseBlock", "ServerToolUseBlock",
                "tool_use", "server_tool_use",
            }:
                continue
            raw_input = claude_sdk_field(block, "input", {})
            raw_input = raw_input if isinstance(raw_input, dict) else {}
            safe_input = {
                key: compact_subagent_text(raw_input.get(key))
                for key in ("description", "subagent_type")
                if compact_subagent_text(raw_input.get(key))
            }
            safe_blocks.append({
                "type": "tool_use",
                "name": compact_subagent_text(
                    claude_sdk_field(block, "name") or "tool"
                ),
                "input": safe_input,
            })
            # The snapshot uses only the first child tool for its activity.
            break
        if not safe_blocks:
            return None
        return {
            "type": "assistant",
            "parent_tool_use_id": parent_tool_id,
            "message": {"content": safe_blocks},
        }

    data = claude_sdk_field(message, "data", {})
    if not isinstance(data, dict):
        return None
    subtype = str(
        data.get("subtype") or claude_sdk_field(message, "subtype") or ""
    )
    if subtype == "background_tasks_changed":
        tasks: list[dict[str, Any]] = []
        for task in data.get("tasks") if isinstance(data.get("tasks"), list) else []:
            if not isinstance(task, dict):
                continue
            tasks.append({
                "task_id": str(task.get("task_id") or "")[:512],
                "task_type": compact_subagent_text(task.get("task_type")),
                "description": compact_subagent_text(task.get("description")),
                "status": compact_subagent_text(task.get("status")),
            })
            if len(tasks) >= SUBAGENT_SNAPSHOT_STATE_LIMIT:
                break
        return {
            "type": "system",
            "subtype": subtype,
            "tasks": tasks,
        }
    if subtype not in {
        "task_started", "task_progress", "task_updated", "task_notification",
    }:
        return None

    raw_patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
    patch = {
        key: (
            value
            if isinstance(value, bool)
            else compact_subagent_text(value)
        )
        for key in (
            "status", "is_backgrounded", "summary", "description", "message",
        )
        if (value := raw_patch.get(key)) not in (None, "")
    }
    record: dict[str, Any] = {
        "type": "system",
        "subtype": subtype,
        "task_id": str(
            data.get("task_id") or claude_sdk_field(message, "task_id") or ""
        )[:512],
        "tool_use_id": str(
            data.get("tool_use_id")
            or claude_sdk_field(message, "tool_use_id")
            or ""
        )[:512],
        "task_type": compact_subagent_text(
            data.get("task_type") or claude_sdk_field(message, "task_type")
        ),
        "subagent_type": compact_subagent_text(data.get("subagent_type")),
        "description": compact_subagent_text(
            data.get("description") or claude_sdk_field(message, "description")
        ),
        "last_tool_name": compact_subagent_text(
            data.get("last_tool_name")
            or claude_sdk_field(message, "last_tool_name")
        ),
        "status": compact_subagent_text(
            data.get("status") or claude_sdk_field(message, "status")
        ),
        "summary": compact_subagent_text(
            data.get("summary") or claude_sdk_field(message, "summary")
        ),
    }
    if patch:
        record["patch"] = patch
    return record

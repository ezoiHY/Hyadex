"""The Codex exec runner: one codex exec process per turn."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging.handlers
import os
from pathlib import Path
import time
from typing import Any
import uuid

from hyadex.core.config import (
    BACKEND_CODEX,
    CODEX_BIN,
    CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
    CODEX_TRANSPORT_EXEC,
    DEFAULT_CWD,
    IDLE_KILL_SECONDS,
    IDLE_WARN_SECONDS,
    PROCESS_STREAM_LIMIT,
)
from hyadex.core.paths import existing_cwd, now_iso
from hyadex.core.processes import process_group_for_pid, terminate_process_tree
from hyadex.core.artifacts import (
    collect_manifest,
    collect_recent_leftover_manifests,
    watch_manifest_artifacts,
)
from hyadex.core.runs import RUNS
from hyadex.core.sessions import append_event, STORE
from hyadex.core.session_codex import codex_output_text
from hyadex.core.state import LIVE_STDOUT_MAX_LINES
from hyadex.core.turns import (
    append_active_stdout,
    append_turn_finished_event,
    bind_active_turn,
    clear_active_process,
    finalize_owned_turn_finished,
    release_turn_slot,
    schedule_next_queued_turn,
)
from hyadex.core.util import clean_assistant_text, compact_memory_text
from hyadex.features.files import capture_git_baseline, publish_turn_code_diff, run_event_metadata
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import agent_runner_env
from hyadex.providers.claude.history import session_provider_id
from hyadex.providers.claude.runtime import mark_provider_turn_ready, tool_changed_paths
from hyadex.providers.codex.goals import build_codex_cmd, rollover_codex_provider_session
from hyadex.providers.codex.history import find_codex_history
from hyadex.providers.codex.notifications import (
    codex_call_tool,
    codex_exec_agent_message,
    codex_reasoning_text,
    CodexExecMessageBuffer,
)
from hyadex.providers.codex.runtime import (
    codex_exec_raw_event_text,
    codex_output_exit_code,
    codex_result_error,
    is_silent_codex_completion,
    mark_codex_exec_context_usage_unavailable,
)
from hyadex.providers.codex.threads import should_recover_codex_resume
from hyadex.providers.registry import (
    persist_run_provider_session,
    record_runtime_failure,
    record_runtime_success,
    redacted_provider_argv,
    standalone_provider_session,
)
from hyadex.providers.runtime import validate_provider_runtime_env

logger = logging.getLogger("hyadex-server")


def build_memory_augmented_prompt(label: str, memory: str, prompt: str) -> str:
    """Wrap fallback memory with an exact character length for collision-free recovery."""
    return (
        f"[{label}; chars={len(memory)}]\n"
        f"{memory}\n"
        f"[End {label}]\n\n"
        "[Current user prompt]\n"
        f"{prompt}"
    )


async def run_codex_exec(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    allow_compaction_rollover: bool = True,
    diff_baseline: dict[str, str] | None = None,
    standalone_provider_context: bool = False,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if not standalone_provider_context:
        # codex exec exposes no authoritative live context measurement. This
        # also covers the automatic app-server -> exec compatibility fallback.
        await mark_codex_exec_context_usage_unavailable(session_id)
    if standalone_provider_context:
        sess = standalone_provider_session(sess)
    resumed_provider_id = session_provider_id(sess)
    memory_seed = str(sess.get("memory_seed") or "").strip()
    if memory_seed and (not sess.get("memory_seed_used") or not resumed_provider_id):
        prompt = build_memory_augmented_prompt(
            "Fork memory context",
            memory_seed,
            prompt,
        )
        async with STORE.transaction():
            current = STORE.sessions.get(session_id)
            if current:
                current["memory_seed_used"] = True
                current["updated_at"] = now_iso()
                sess = dict(current)
                await STORE.save()
        await append_event(session_id, "history_imported", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": "Applied memory fork context to this first Codex exec turn.",
        })
    cmd = build_codex_cmd(
        session_id,
        sess,
        prompt,
        manifest_path,
        disable_provider_subagents=bool(runtime_env),
    )
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    if diff_baseline is None:
        diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    public_cmd = redacted_provider_argv(cmd, BACKEND_CODEX)
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CODEX, "argv": public_cmd, "cwd": cwd})
    env = agent_runner_env(session_id, runtime_env)
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        released = await release_turn_slot(
            session_id,
            expected_run_id=run_id,
        )
        try:
            if released:
                record_runtime_failure(BACKEND_CODEX, e, spawn_failure=True)
                await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CODEX, "message": f"failed to start Codex: {e}", **run_event_metadata(run_id)})
                await append_turn_finished_event(session_id, {
                    "run_id": run_id,
                    "backend": BACKEND_CODEX,
                    "exit_code": None,
                    "result_text": "",
                    **run_event_metadata(run_id),
                })
        finally:
            RUNS.run_metadata.pop(run_id, None)
            RUNS.stopped_runs.discard(run_id)
            if released:
                schedule_next_queued_turn(session_id)
        return
    pgid = process_group_for_pid(proc.pid)
    bound, stop_requested = await bind_active_turn(
        session_id,
        run_id,
        {
            "proc": proc,
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "transport": CODEX_TRANSPORT_EXEC,
            "pid": proc.pid,
            "pgid": pgid,
            "cwd": cwd,
            "argv": public_cmd,
            "started_at": time.time(),
            "started_at_iso": now_iso(),
            "provider_turn_ready": False,
            "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
            "stdout_total_lines": 0,
            "stdout_updated_at": None,
        },
    )
    if not bound:
        await terminate_process_tree(proc, grace=0.5)
        return
    if stop_requested:
        await terminate_process_tree(proc)

    text_parts: list[str] = []
    provider_id: str | None = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    last_event = time.time()
    resume_started_at = last_event
    idle_killed = False
    resume_stalled = False
    stream_error: str | None = None
    codex_error: str | None = None
    seen_artifacts: set[str] = set()
    seen_raw_lines: set[str] = set()
    seen_text_parts: set[str] = set()
    seen_reasoning: set[str] = set()
    message_buffer = CodexExecMessageBuffer()
    tool_calls: dict[str, dict[str, Any]] = {}
    started_tool_ids: set[str] = set()
    finished_tool_ids: set[str] = set()
    changed_paths: set[str] = set()
    codex_history_path = find_codex_history(provider_id) if provider_id else None
    codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    async def emit_provider_session(new_provider_id: str) -> None:
        nonlocal provider_id, codex_history_path, codex_history_pos
        if not new_provider_id:
            return
        provider_id = new_provider_id
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CODEX,
            provider_id,
            standalone_provider_context=standalone_provider_context,
        )
        if not codex_history_path:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0

    async def emit_assistant_text(text: str) -> None:
        text = clean_assistant_text(text)
        if not text or text in seen_text_parts:
            return
        seen_text_parts.add(text)
        text_parts.append(text)
        await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text, **run_event_metadata(run_id)})

    async def emit_reasoning_text(text: str, *, phase: str = "") -> None:
        text = str(text or "").strip()
        if not text or text in seen_reasoning:
            return
        seen_reasoning.add(text)
        payload = {"run_id": run_id, "text": text}
        if phase:
            payload["phase"] = phase
        await append_event(session_id, "reasoning_summary", payload)

    async def emit_buffered_messages(
        messages: list[tuple[str, str]],
    ) -> None:
        for phase, text in messages:
            if phase == "final_answer":
                await emit_assistant_text(text)
            else:
                await emit_reasoning_text(text, phase="commentary")

    async def observe_agent_message(text: str, phase: str = "") -> None:
        await emit_buffered_messages(message_buffer.observe(text, phase))

    async def flush_agent_message(*, final: bool) -> None:
        await emit_buffered_messages(message_buffer.flush(final=final))

    async def emit_tool_started(tool: dict[str, Any]) -> None:
        await flush_agent_message(final=False)
        tool_id = str(tool.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
        tool["id"] = tool_id
        tool_calls[tool_id] = tool
        changed_paths.update(tool_changed_paths(tool))
        if tool_id in started_tool_ids:
            return
        started_tool_ids.add(tool_id)
        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})

    async def emit_tool_finished(tool_id: str, output: Any, exit_code: int | None = None) -> None:
        await flush_agent_message(final=False)
        tool_id = str(tool_id or f"tool_{uuid.uuid4().hex[:8]}")
        if tool_id in finished_tool_ids:
            return
        finished_tool_ids.add(tool_id)
        output_text = codex_output_text(output)
        tool = tool_calls.get(tool_id) or {"id": tool_id, "name": "Tool", "input": {}}
        if exit_code is None:
            exit_code = codex_output_exit_code(output_text)
        await append_event(session_id, "tool_finished", {
            "run_id": run_id,
            "tool_id": tool_id,
            "tool": tool,
            "output": output_text,
            "exit_code": exit_code,
        })

    async def handle_codex_event(event: dict[str, Any]) -> bool:
        handled = False
        etype = str(event.get("type") or "")
        result_error = codex_result_error(event)
        if result_error:
            nonlocal codex_error
            codex_error = result_error
            return True
        if etype == "thread.started" and event.get("thread_id"):
            await emit_provider_session(str(event["thread_id"]))
            return True
        if etype == "turn.started":
            await mark_provider_turn_ready(session_id, run_id, provider_id)
            return True
        agent_message = codex_exec_agent_message(event)
        if agent_message is not None:
            await observe_agent_message(*agent_message)
            return True
        if etype in ("item.started", "item.completed"):
            item = event.get("item", {}) or {}
            itype = item.get("type", "")
            if itype == "command_execution":
                tool = {
                    "id": item.get("id") or f"cmd_{uuid.uuid4().hex[:8]}",
                    "name": "Bash",
                    "input": {"command": item.get("command", "")},
                }
                if etype == "item.started":
                    await emit_tool_started(tool)
                else:
                    await emit_tool_finished(str(tool["id"]), item.get("aggregated_output", ""), item.get("exit_code"))
                handled = True
            elif itype in ("reasoning", "agent_reasoning") and etype == "item.completed":
                await flush_agent_message(final=False)
                await emit_reasoning_text(codex_reasoning_text(item))
                handled = True
            return handled
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if etype == "event_msg":
            payload_type = str(payload.get("type") or "")
            if payload_type == "agent_reasoning":
                await flush_agent_message(final=False)
                await emit_reasoning_text(str(payload.get("text") or ""))
                return True
            return False
        if etype == "response_item":
            payload_type = str(payload.get("type") or "")
            if payload_type in {"function_call", "custom_tool_call"}:
                call_id = str(payload.get("call_id") or payload.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
                arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
                await emit_tool_started(codex_call_tool(call_id, str(payload.get("name") or "tool"), arguments))
                return True
            if payload_type in {"function_call_output", "custom_tool_call_output"}:
                await emit_tool_finished(str(payload.get("call_id") or payload.get("id") or ""), payload.get("output"))
                return True
            if payload_type in {"reasoning", "agent_reasoning"}:
                await flush_agent_message(final=False)
                await emit_reasoning_text(codex_reasoning_text(payload))
                return True
        return False

    async def handle_codex_line(line: str, *, record_raw: bool) -> bool:
        line = line.strip()
        if not line or not line.startswith("{") or line in seen_raw_lines:
            return False
        seen_raw_lines.add(line)
        try:
            event = json.loads(line)
        except Exception:
            raw_event = codex_exec_raw_event_text(line, handled=False)
            if record_raw and raw_event:
                await append_event(session_id, "raw_event", {
                    "run_id": run_id,
                    "backend": BACKEND_CODEX,
                    "raw": raw_event,
                })
            return False
        handled = await handle_codex_event(event)
        raw_event = codex_exec_raw_event_text(line, handled=handled)
        if record_raw and raw_event:
            await append_event(session_id, "raw_event", {
                "run_id": run_id,
                "backend": BACKEND_CODEX,
                "raw": raw_event,
            })
        return handled

    async def drain_codex_history() -> bool:
        nonlocal codex_history_path, codex_history_pos
        if not codex_history_path and provider_id:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
        if not codex_history_path or not codex_history_path.exists():
            return False
        try:
            size = codex_history_path.stat().st_size
            if size < codex_history_pos:
                codex_history_pos = 0
            if size <= codex_history_pos:
                return False
            with codex_history_path.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(codex_history_pos)
                lines = f.readlines()
                codex_history_pos = f.tell()
        except OSError:
            return False
        consumed = False
        for history_line in lines:
            if await handle_codex_line(history_line, record_raw=False):
                consumed = True
            elif history_line.strip():
                consumed = True
        return consumed

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                if await drain_codex_history():
                    last_event = time.time()
                produced_activity = bool(
                    text_parts
                    or seen_reasoning
                    or message_buffer.has_pending
                    or started_tool_ids
                    or finished_tool_ids
                    or seen_artifacts
                )
                if (
                    resumed_provider_id
                    and CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS > 0
                    and not produced_activity
                    and time.time() - resume_started_at >= CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS
                ):
                    resume_stalled = True
                    logger.warning(
                        "Codex resume produced no activity before startup timeout session=%s run=%s provider=%s timeout=%ss",
                        session_id,
                        run_id,
                        resumed_provider_id,
                        CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
                    )
                    await terminate_process_tree(proc)
                    break
                idle = time.time() - last_event
                if idle >= IDLE_WARN_SECONDS:
                    await append_event(session_id, "idle_warning", {"run_id": run_id, "idle_seconds": int(idle)})
                if idle >= IDLE_KILL_SECONDS:
                    idle_killed = True
                    await terminate_process_tree(proc)
                    break
                continue
            if not raw:
                break
            last_event = time.time()
            decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
            await append_active_stdout(session_id, decoded)
            line = decoded.strip()
            if line.startswith("{"):
                await handle_codex_line(line, record_raw=True)
            if await drain_codex_history():
                last_event = time.time()
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Codex run failed session=%s run=%s", session_id, run_id)
    finally:
        with suppress(Exception):
            await drain_codex_history()
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id, expected_run_id=run_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in RUNS.stopped_runs
    await flush_agent_message(
        final=(
            not stopped
            and not stream_error
            and not idle_killed
            and not resume_stalled
            and not codex_error
            and proc.returncode in (0, None)
        )
    )
    terminal_error = codex_error or stderr
    produced_activity = bool(
        text_parts
        or seen_reasoning
        or started_tool_ids
        or finished_tool_ids
        or seen_artifacts
    )
    silent_completion = is_silent_codex_completion(
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        returncode=proc.returncode,
        produced_activity=produced_activity,
    )
    should_rollover = should_recover_codex_resume(
        allow_rollover=allow_compaction_rollover,
        resumed_provider_id=resumed_provider_id,
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        resume_stalled=resume_stalled,
        returncode=proc.returncode,
        produced_activity=produced_activity,
        terminal_error=terminal_error,
    )
    if should_rollover:
        recovery_reason = (
            f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds."
            if resume_stalled
            else terminal_error or "Codex resume exited successfully without producing any response events."
        )
        if resume_stalled:
            recovery_message = "Codex resume stalled before producing a reply. Retrying this turn once on a fresh thread with bounded recent memory."
        elif silent_completion:
            recovery_message = "Codex resume produced no reply. Retrying this turn once on a fresh thread with bounded recent memory."
        else:
            recovery_message = "Codex could not continue the provider thread. Retrying this turn once on a fresh thread with bounded recent memory."
        logger.warning(
            "recovering empty Codex resume session=%s run=%s provider=%s exit=%s reason=%s",
            session_id,
            run_id,
            resumed_provider_id,
            proc.returncode,
            compact_memory_text(recovery_reason, 500),
        )
        rollover = await rollover_codex_provider_session(
            session_id,
            run_id,
            str(resumed_provider_id),
            recovery_reason,
            message=recovery_message,
        )
        if rollover:
            fresh_session, memory = rollover
            retry_prompt = build_memory_augmented_prompt(
                "Codex rollover memory",
                memory,
                prompt,
            )
            await run_codex_exec(
                session_id,
                run_id,
                retry_prompt,
                fresh_session,
                manifest_path,
                allow_compaction_rollover=False,
                diff_baseline=diff_baseline,
                standalone_provider_context=standalone_provider_context,
                provider_runtime_env=runtime_env,
            )
            return
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if resume_stalled:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    elif not stopped and proc.returncode not in (0, None):
        if codex_error:
            await append_event(session_id, "error", {"run_id": run_id, "message": codex_error, "exit_code": proc.returncode, **run_event_metadata(run_id)})
        elif stderr:
            await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
        else:
            await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex exited {proc.returncode} without error output.", "exit_code": proc.returncode, **run_event_metadata(run_id)})
    elif silent_completion:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": "Codex exited without producing a reply. Retry the message; if this was a resumed external thread, create a fresh memory-backed continuation.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    if not stopped and (stream_error or idle_killed or resume_stalled or proc.returncode not in (0, None) or silent_completion):
        record_runtime_failure(BACKEND_CODEX, terminal_error or stream_error or f"exit {proc.returncode}")
    elif not stopped:
        record_runtime_success(BACKEND_CODEX)
    if provider_id and not standalone_provider_context:
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CODEX,
            provider_id,
        )
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CODEX, cwd, diff_baseline, changed_paths)
    terminal_payload = {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "exit_code": proc.returncode,
        "result_text": clean_assistant_text("\n\n".join(text_parts).strip()),
        "stopped": stopped,
        **run_event_metadata(run_id),
    }
    finalize_task = asyncio.create_task(finalize_owned_turn_finished(
        session_id,
        run_id,
        stopped=stopped,
        payload=terminal_payload,
    ))
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError:
        await join_task_despite_caller_cancellation(finalize_task)
        raise

"""Running Claude turns: the Agent SDK and print runners and their start failures."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import logging.handlers
from pathlib import Path
import secrets
import time
from typing import Any
import uuid

from hyadex.providers.claude.background_reconciliation import (
    pending_task_reconciliations,
    reconciliation_envelope,
    SESSION_FIELD as CLAUDE_BACKGROUND_SESSION_FIELD,
)
from hyadex.core.config import (
    BACKEND_CLAUDE,
    CLAUDE_SDK_IDLE_TIMEOUT_SECONDS,
    CLAUDE_SDK_IDLE_WARN_SECONDS,
    CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS,
    CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS,
    CLAUDE_SDK_TURN_TIMEOUT_SECONDS,
    CLAUDE_TRANSPORT,
    CLAUDE_TRANSPORT_AGENT_SDK,
    CLAUDE_TRANSPORT_PRINT,
    DEFAULT_CWD,
    IDLE_KILL_SECONDS,
    IDLE_WARN_SECONDS,
    PROCESS_STREAM_LIMIT,
)
from hyadex.core.paths import existing_cwd, now_iso
from hyadex.core.processes import process_group_for_pid, terminate_process_tree
from hyadex.core.queue import queued_turn_run_metadata, should_schedule_queue_after_finish
from hyadex.core.artifacts import (
    collect_manifest,
    collect_recent_leftover_manifests,
    watch_manifest_artifacts,
)
from hyadex.core.history_sync import append_durable_event_batch, fence_native_steer_delivery
from hyadex.core.steering import mark_native_steer_accepted, turn_steering_lineage
from hyadex.core.runs import RUNS
from hyadex.core.server import SERVER
from hyadex.core.server_logging import concise_error_message
from hyadex.core.sessions import (
    append_event,
    event_output_text,
    mark_provider_context_usage_unavailable,
    revoke_provider_authority,
    STORE,
)
from hyadex.core.session_settings import effective_claude_permission_mode
from hyadex.core.state import LIVE_STDOUT_MAX_LINES
from hyadex.core.turns import (
    append_active_stdout,
    append_turn_finished_event,
    bind_active_turn,
    clear_active_process,
    finalize_owned_turn_finished,
)
from hyadex.core.turn_start import release_turn_slot, schedule_next_queued_turn
from hyadex.core.util import clean_assistant_text
from hyadex.core.wiring import WIRING
from hyadex.features.files import capture_git_baseline, publish_turn_code_diff, run_event_metadata
from hyadex.features.usage import join_task_despite_caller_cancellation
from hyadex.providers.authority import agent_runner_env, NativeSteerHandoffError
from hyadex.providers.claude.runtime import (
    claude_empty_turn_failure_message,
    claude_result_error,
    claude_sdk_manager,
    claude_supports_no_session_persistence,
    interrupt_claude_sdk_run_bounded,
    is_expected_claude_interruption_result,
    mark_claude_sdk_turn_ready_after_ack,
    resolve_claude_resume_provider,
)
from hyadex.providers.claude.projection import (
    claude_sdk_result_details,
    mark_provider_turn_ready,
    project_claude_sdk_message,
    tool_changed_paths,
)
from hyadex.providers.claude.interactions import (
    cancel_claude_interactions,
    claude_run_has_pending_interaction,
)
from hyadex.providers.claude.goals import CLAUDE_GOAL_PENDING, refresh_claude_goal
from hyadex.providers.claude.context_usage import (
    claude_provider_id_for_session,
    sample_claude_context_usage,
)
from hyadex.providers.claude.catalog import build_claude_cmd
from hyadex.providers.claude.background_state import (
    acknowledge_claude_background_reconciliation,
    persist_claude_background_task_receipts,
)
from hyadex.providers.claude.sdk import (
    ClaudeSDKQueryError,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKUnavailable,
)
from hyadex.providers.claude.sdk_manager import ClaudeSDKSupervisorManager
from hyadex.providers.codex.commands import build_claude_sdk_options
from hyadex.providers.registry import (
    build_user_provider_prompt,
    issue_native_steer_provider_authority,
    persist_run_provider_session,
    record_runtime_failure,
    record_runtime_success,
    redacted_provider_argv,
    standalone_provider_session,
    turn_slot_is_owned,
)
from hyadex.providers.runtime import (
    assert_provider_user_message_unchanged,
    validate_provider_runtime_env,
)
from hyadex.providers.claude.subagents import (
    claude_sdk_field,
    claude_sdk_type,
    evict_claude_sdk_chat,
)
from hyadex.providers.commands import ProviderCommandRecord

logger = logging.getLogger("hyadex-server")


async def run_claude_print(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    standalone_provider_context: bool = False,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    runtime_env = validate_provider_runtime_env(provider_runtime_env)
    if standalone_provider_context:
        sess = standalone_provider_session(sess)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    resume_provider_id, resume_skip_message = resolve_claude_resume_provider(sess, cwd)
    no_session_persistence = (
        standalone_provider_context
        and await asyncio.to_thread(claude_supports_no_session_persistence)
    )
    cmd = build_claude_cmd(
        session_id,
        sess,
        manifest_path,
        provider_id=resume_provider_id,
        no_session_persistence=no_session_persistence,
        disable_provider_subagents=bool(runtime_env),
    )
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    if resume_skip_message:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
            "provider_session_id": claude_provider_id_for_session(sess),
            "message": resume_skip_message,
        })
    public_cmd = redacted_provider_argv(cmd, BACKEND_CLAUDE)
    await append_event(session_id, "process_started", {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_PRINT,
        "argv": public_cmd,
        "cwd": cwd,
    })
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=agent_runner_env(session_id, runtime_env),
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
                record_runtime_failure(BACKEND_CLAUDE, e, spawn_failure=True)
                await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": f"failed to start Claude: {e}", **run_event_metadata(run_id)})
                await append_turn_finished_event(session_id, {
                    "run_id": run_id,
                    "backend": BACKEND_CLAUDE,
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
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_PRINT,
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
    if not stop_requested and proc.stdin:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    final_text = ""
    provider_id: str | None = None
    current_tools: dict[str, dict[str, Any]] = {}
    had_tool_activity = False
    changed_paths: set[str] = set()
    last_event = time.time()
    idle_killed = False
    stream_error: str | None = None
    result_error: str | None = None
    terminal_result_received = False
    seen_artifacts: set[str] = set()
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
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
            if not line:
                continue
            await append_event(session_id, "raw_event", {"run_id": run_id, "backend": BACKEND_CLAUDE, "raw": line})
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("session_id"):
                if not provider_id:
                    provider_id = event["session_id"]
                await mark_provider_turn_ready(
                    session_id,
                    run_id,
                    str(event["session_id"]),
                )
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        text = clean_assistant_text(block["text"])
                        if text:
                            await append_event(session_id, "assistant_text", {
                                "run_id": run_id,
                                "text": text,
                                "backend": BACKEND_CLAUDE,
                                **run_event_metadata(run_id),
                            })
                    elif btype == "thinking" and block.get("thinking"):
                        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": block["thinking"]})
                    elif btype in ("tool_use", "server_tool_use"):
                        had_tool_activity = True
                        tid = block.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tool = {"id": tid, "name": block.get("name", "tool"), "input": block.get("input", {})}
                        current_tools[tid] = tool
                        changed_paths.update(tool_changed_paths(tool))
                        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        had_tool_activity = True
                        tid = block.get("tool_use_id")
                        content = event_output_text(block.get("content", ""))
                        await append_event(session_id, "tool_finished", {
                            "run_id": run_id,
                            "tool_id": tid,
                            "tool": current_tools.pop(tid, None),
                            "output": content,
                            "is_error": block.get("is_error") is True,
                        })
            elif etype == "result":
                terminal_result_received = True
                if run_id in RUNS.stopped_runs and is_expected_claude_interruption_result(event):
                    continue
                result_error = claude_result_error(event)
                if result_error:
                    provider_id = None
                    await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": result_error, **run_event_metadata(run_id)})
                    continue
                final_text = event.get("result", "") or final_text
                if event.get("session_id"):
                    provider_id = event["session_id"]
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Claude run failed session=%s run=%s", session_id, run_id)
    finally:
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id, expected_run_id=run_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in RUNS.stopped_runs
    if not stopped and not terminal_result_received and not stream_error:
        stream_error = (
            "Claude print stream ended before a terminal result became "
            "available."
        )
    result_text = clean_assistant_text(final_text)
    empty_result_error = claude_empty_turn_failure_message(
        prompt=prompt,
        result_text=result_text,
        had_tool_activity=had_tool_activity,
        stopped=stopped,
        terminal_result_received=terminal_result_received,
        existing_error=bool(
            result_error
            or stream_error
            or idle_killed
            or proc.returncode not in (0, None)
        ),
    )
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Claude stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if not stopped and proc.returncode not in (0, None) and stderr:
        await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
    if empty_result_error:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_PRINT,
            "message": empty_result_error,
            **run_event_metadata(run_id),
        })
    if not stopped and (
        result_error
        or stream_error
        or proc.returncode not in (0, None)
        or empty_result_error
    ):
        record_runtime_failure(
            BACKEND_CLAUDE,
            result_error
            or stream_error
            or stderr
            or empty_result_error
            or f"exit {proc.returncode}",
        )
    elif not stopped:
        record_runtime_success(BACKEND_CLAUDE)
    if provider_id and not result_error:
        await persist_run_provider_session(
            session_id,
            run_id,
            BACKEND_CLAUDE,
            provider_id,
            cwd=cwd,
            standalone_provider_context=standalone_provider_context,
        )
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CLAUDE, cwd, diff_baseline, changed_paths)
    terminal_payload = {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_PRINT,
        "exit_code": (
            1
            if (
                empty_result_error
                or stream_error
                or result_error
                or idle_killed
            ) and proc.returncode in (0, None)
            else proc.returncode
        ),
        "result_text": result_text,
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


async def finish_claude_sdk_start_failure(
    session_id: str,
    run_id: str,
    message: str,
    *,
    delivery_uncertain: bool = False,
    ownership_token: str | None = None,
) -> None:
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if active and str(active.get("run_id") or "") == run_id:
            active["provider_turn_ready"] = False
            active["native_steer_queue"] = None
            active["claude_permissions_open"] = False
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    try:
        if not released:
            return
        await cancel_claude_interactions(
            session_id,
            resolution="turn_failed",
            expected_run_id=run_id,
            ownership_token=ownership_token,
        )
        record_runtime_failure(BACKEND_CLAUDE, message, spawn_failure=True)
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_AGENT_SDK,
            "message": message,
            "delivery_unknown": delivery_uncertain,
            **run_event_metadata(run_id),
        })
        await append_turn_finished_event(session_id, {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "transport": CLAUDE_TRANSPORT_AGENT_SDK,
            "exit_code": None,
            "result_text": "",
            "delivery_unknown": delivery_uncertain,
            **run_event_metadata(run_id),
        })
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        if released:
            schedule_next_queued_turn(session_id)


async def finish_cancelled_claude_sdk_start(
    session_id: str,
    run_id: str,
    manager: ClaudeSDKSupervisorManager,
    *,
    ownership_token: str | None = None,
) -> None:
    """Retire a possibly accepted query and clear its reservation safely."""

    RUNS.stopped_runs.add(run_id)
    if await turn_slot_is_owned(session_id, run_id):
        if ownership_token:
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=ownership_token,
            )
        await cancel_claude_interactions(
            session_id,
            resolution="turn_stopped",
            expected_run_id=run_id,
            ownership_token=ownership_token,
        )
    drain_queue = should_schedule_queue_after_finish(
        session_id,
        stopped=True,
    )
    released = await release_turn_slot(
        session_id,
        expected_run_id=run_id,
    )
    try:
        if released:
            await append_event(session_id, "turn_stopped", {
                "run_id": run_id,
                "backend": BACKEND_CLAUDE,
                "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                "message": "Claude was stopped while the SDK turn was starting.",
                **run_event_metadata(run_id),
            })
    finally:
        RUNS.run_metadata.pop(run_id, None)
        RUNS.stopped_runs.discard(run_id)
        # This cleanup owns the successful release. The outer task supervisor
        # deliberately sees no remaining owner, so the drain must happen here.
        # A concurrent Stop may request the same drain; QUEUE_START_TASKS keeps
        # promotion single-owner and prevents a double launch.
        if released and drain_queue:
            schedule_next_queued_turn(session_id)


async def run_claude_sdk(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    """Run Claude on one persistent, chat-owned Agent SDK process."""

    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    resume_provider_id, resume_skip_message = resolve_claude_resume_provider(
        sess,
        cwd,
    )
    diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    try:
        options, configuration_key, cli_path = build_claude_sdk_options(
            session_id,
            sess,
            cwd,
            manifest_path,
            provider_runtime_env,
        )
        manager = await claude_sdk_manager()
    except ClaudeSDKUnavailable:
        raise
    except Exception as exc:
        raise ClaudeSDKUnavailable(
            f"Claude Agent SDK setup failed before prompt delivery: {exc}"
        ) from exc
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
        })
    if resume_skip_message:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
            "provider_session_id": claude_provider_id_for_session(sess),
            "message": resume_skip_message,
        })
    await append_event(session_id, "process_started", {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
        "argv": [
            cli_path,
            "<ClaudeSDKClient>",
            "--permission-mode",
            effective_claude_permission_mode(sess),
        ],
        "cwd": cwd,
    })

    # Publish the chat-owned SDK reservation before query delivery. The SDK
    # may invoke ``can_use_tool`` from inside ``client.query()`` and therefore
    # before ``manager.start_run()`` returns a handle. The approval callback
    # must already be able to verify an opted-in ACTIVE turn at that point.
    native_steer_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
    startup_active = {
        "proc": None,
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
        "claude_sdk_run": None,
        # Authority-bearing runs receive a fresh provider-tool owner binding.
        # Force Send therefore follows Stop -> queued start for those runs;
        # retain the legacy native path only for callers without authority.
        "native_steer_queue": (
            None
            if provider_runtime_env or provider_command is not None
            else native_steer_queue
        ),
        "interactive_agent_sdk": True,
        "provider_model": str(sess.get("model") or ""),
        "provider_effort": str(sess.get("effort") or ""),
        # Freeze process policy for the lifetime of this provider turn. The
        # persisted session value may change concurrently for the next turn.
        "claude_permission_mode": effective_claude_permission_mode(sess),
        "configuration_key": configuration_key,
        "provider_session_id": resume_provider_id,
        "provider_turn_ready": False,
        "provider_tools_ready": asyncio.Event(),
        "claude_sdk_owner_token": "",
        "claude_permission_run_id": run_id,
        "claude_permissions_open": False,
        "cwd": cwd,
        "argv": [cli_path, "<ClaudeSDKClient>"],
        "started_at": time.time(),
        "started_at_iso": now_iso(),
        "native_interrupt_sent": False,
        "stop_requested": False,
        "provider_starting": True,
        "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
        "stdout_total_lines": 0,
        "stdout_updated_at": None,
    }
    bound, stop_requested = await bind_active_turn(
        session_id,
        run_id,
        startup_active,
    )
    if not bound:
        return
    if stop_requested:
        # Stop won before provider delivery. Do not submit the prompt merely
        # to acquire a handle that would immediately be interrupted.
        await finish_cancelled_claude_sdk_start(
            session_id,
            run_id,
            manager,
            ownership_token=(
                str(startup_active.get("claude_sdk_owner_token") or "")
                or None
            ),
        )
        return

    async def activate_initial_supervisor(ownership_token: str) -> None:
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if (
                active is not startup_active
                or str(active.get("run_id") or "") != run_id
                or active.get("stop_requested")
                or session_id not in RUNS.busy
            ):
                # This hook runs before supervisor.start_run/query. Raising
                # cancellation guarantees that a Stop/delete winner cannot
                # deliver the prompt afterward.
                raise asyncio.CancelledError
            active["claude_sdk_owner_token"] = str(ownership_token)
            active["claude_permission_run_id"] = run_id
            active["claude_permissions_open"] = True
    initial_reconciliation_batches = pending_task_reconciliations(
        (STORE.sessions.get(session_id) or {}).get(CLAUDE_BACKGROUND_SESSION_FIELD),
        provider_session_id=str(resume_provider_id or ""),
    )
    try:
        provider_command_start_options = (
            {
                "validated_provider_command_name": str(
                    provider_command.native.get("name") or ""
                ),
                "expected_provider_command_generation": str(
                    provider_command.native.get("control_generation") or ""
                ),
            }
            if provider_command is not None
            else {}
        )
        handle = await manager.start_run(
            session_id,
            prompt,
            run_id=run_id,
            options=options,
            configuration_key=configuration_key,
            on_supervisor_ready=activate_initial_supervisor,
            **provider_command_start_options,
            **({"background_task_reconciliation": reconciliation_envelope(initial_reconciliation_batches)}
               if initial_reconciliation_batches else {}),
        )
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(
            finish_cancelled_claude_sdk_start(
                session_id,
                run_id,
                manager,
                ownership_token=(
                    str(startup_active.get("claude_sdk_owner_token") or "")
                    or None
                ),
            )
        )
        await join_task_despite_caller_cancellation(cleanup_task)
        raise
    except ClaudeSDKUnavailable:
        raise
    except ClaudeSDKQueryError as exc:
        startup_ownership_token = str(
            startup_active.get("claude_sdk_owner_token") or ""
        )
        if startup_ownership_token and await turn_slot_is_owned(
            session_id,
            run_id,
        ):
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=startup_ownership_token,
            )
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            (
                "Claude SDK prompt delivery could not be confirmed. The prompt "
                "was not replayed through the fallback transport."
            ),
            delivery_uncertain=True,
            ownership_token=startup_ownership_token or None,
        )
        logger.warning(
            "Claude SDK query delivery uncertain session=%s run=%s: %s",
            session_id,
            run_id,
            concise_error_message(exc),
        )
        return
    except Exception as exc:
        startup_ownership_token = str(
            startup_active.get("claude_sdk_owner_token") or ""
        )
        if startup_ownership_token and await turn_slot_is_owned(
            session_id,
            run_id,
        ):
            await evict_claude_sdk_chat(
                session_id,
                force=True,
                manager=manager,
                ownership_token=startup_ownership_token,
            )
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            f"Claude SDK could not start: {concise_error_message(exc)}",
            ownership_token=startup_ownership_token or None,
        )
        return

    attached = False
    initial_provider_ready = bool(
        getattr(handle, "acknowledged", False)
        and not getattr(handle, "done", False)
    )
    async with RUNS.active_lock:
        active = RUNS.active.get(session_id)
        if (
            active is startup_active
            and str(active.get("run_id") or "") == run_id
            and session_id in RUNS.busy
        ):
            active.update({
                "claude_sdk_run": handle,
                "provider_turn_ready": initial_provider_ready,
                "provider_starting": not initial_provider_ready,
            })
            tools_ready = active.get("provider_tools_ready")
            if initial_provider_ready and isinstance(tools_ready, asyncio.Event):
                tools_ready.set()
            stop_requested = bool(active.get("stop_requested"))
            attached = True
        else:
            stop_requested = True
    if not attached:
        # Stop/delete may have hard-terminalized and released the placeholder
        # while a cancellation-hostile SDK query was returning. Interrupt only
        # this old handle; chat-ID eviction could target a newer replacement.
        with suppress(Exception):
            await interrupt_claude_sdk_run_bounded(handle)
        return
    if stop_requested:
        try:
            await interrupt_claude_sdk_run_bounded(handle)
        except Exception:
            await finish_cancelled_claude_sdk_start(
                session_id,
                run_id,
                manager,
                ownership_token=(
                    str(startup_active.get("claude_sdk_owner_token") or "")
                    or None
                ),
            )
            return

    sdk_ownership_token = str(
        startup_active.get("claude_sdk_owner_token") or ""
    )
    current_run_id = run_id
    current_handle = handle
    current_reconciliation_batches = initial_reconciliation_batches
    receipts_persisted_run_ids: set[str] = set()
    current_diff_baseline = diff_baseline
    current_prompt = prompt
    text_parts: list[str] = []
    current_tools: dict[str, dict[str, Any]] = {}
    tool_activity_run_ids: set[str] = set()
    projection_error_run_ids: set[str] = set()
    changed_paths: set[str] = set()
    seen_artifacts: set[str] = set()
    provider_id = str(resume_provider_id or "")
    provider_persisted_id = str(resume_provider_id or "")
    result_details: dict[str, Any] | None = None
    stream_error: str | None = None
    delivery_unknown = False
    cancelled_error: asyncio.CancelledError | None = None
    retire_supervisor = False
    pending_steer: dict[str, Any] | None = None
    message_task: asyncio.Task[Any] | None = None
    steer_task: asyncio.Task[Any] | None = None
    first_activity_task: asyncio.Task[bool] | None = None
    provider_ready_tasks: set[asyncio.Task[bool]] = set()
    outputs_finished_run_ids: set[str] = set()
    shutdown_interrupted_run_ids: set[str] = set()
    logical_started_monotonic = time.monotonic()
    last_activity_monotonic = logical_started_monotonic
    deadline_clock_checked_monotonic = logical_started_monotonic
    deadline_clock_was_paused = False
    provider_acknowledged = initial_provider_ready
    idle_warning_emitted = False
    manifest_watch_task: asyncio.Task[Any] | None = asyncio.create_task(
        watch_manifest_artifacts(
            session_id,
            current_run_id,
            manifest_path,
            seen_artifacts,
        )
    )

    def record_sdk_stream_exception(exc: Exception) -> bool:
        nonlocal stream_error
        # Latch the cause at failure time. A later shutdown must not relabel
        # an earlier provider/projection fault, and an unexpected closed
        # supervisor must remain an error. This never retries the prompt.
        shutdown_interrupted = bool(
            SERVER.shutting_down
            and isinstance(exc, ClaudeSDKSupervisorClosed)
            and not (
                stream_error
                or (result_details or {}).get("error")
                or (result_details or {}).get("is_error")
                or current_run_id in projection_error_run_ids
            )
        )
        if shutdown_interrupted:
            RUNS.stopped_runs.add(current_run_id)
            shutdown_interrupted_run_ids.add(current_run_id)
        stream_error = stream_error or concise_error_message(exc)
        return shutdown_interrupted

    def watch_provider_readiness(
        logical_run_id: str,
        logical_handle: Any,
        *,
        already_published_ready: bool,
    ) -> None:
        if already_published_ready:
            return
        task = asyncio.create_task(
            mark_claude_sdk_turn_ready_after_ack(
                session_id,
                logical_run_id,
                logical_handle,
            )
        )
        provider_ready_tasks.add(task)
        task.add_done_callback(provider_ready_tasks.discard)

    async def first_activity_timeout_after_ack(
        logical_handle: Any,
    ) -> bool:
        """Return true only when an acknowledged turn stays fully silent."""

        wait_acknowledged = getattr(logical_handle, "wait_acknowledged", None)
        if not callable(wait_acknowledged):
            return False
        await wait_acknowledged()
        await asyncio.sleep(
            CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS
        )
        # A terminal result can complete immediately before this timeout wins
        # its scheduling race. That completion is provider activity even if
        # the iterator consumer has not projected the frame yet.
        return not bool(getattr(logical_handle, "done", False))

    async def cancel_first_activity_watchdog() -> None:
        nonlocal first_activity_task

        task = first_activity_task
        first_activity_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def reset_first_activity_watchdog(logical_handle: Any) -> None:
        nonlocal first_activity_task

        await cancel_first_activity_watchdog()
        first_activity_task = asyncio.create_task(
            first_activity_timeout_after_ack(logical_handle)
        )

    watch_provider_readiness(
        current_run_id,
        current_handle,
        already_published_ready=initial_provider_ready,
    )
    await reset_first_activity_watchdog(current_handle)

    async def finish_outputs(
        logical_run_id: str,
        baseline: dict[str, Any],
        paths: set[str],
        artifacts: set[str],
        logical_manifest_path: Path,
    ) -> None:
        if logical_run_id in outputs_finished_run_ids:
            return
        await collect_manifest(
            session_id,
            logical_run_id,
            logical_manifest_path,
            seen_artifacts=artifacts,
            final=True,
        )
        await publish_turn_code_diff(
            session_id,
            logical_run_id,
            BACKEND_CLAUDE,
            cwd,
            baseline,
            paths,
        )
        outputs_finished_run_ids.add(logical_run_id)

    async def release_transition(event: asyncio.Event) -> None:
        event.set()
        async with RUNS.active_lock:
            active = RUNS.active.get(session_id)
            if active and active.get("logical_transition_ready") is event:
                active.pop("logical_transition_ready", None)
                active.pop("logical_transition_candidate_run_id", None)
                active.pop("logical_transition_accepting_permissions", None)
                active.pop("logical_transition_authority_open", None)
                active.pop("logical_transition_predecessor_run_id", None)
                active.pop("logical_transition_authority_nonce", None)

    async def sdk_deadline_expired(
        now_monotonic: float | None = None,
    ) -> bool:
        nonlocal provider_acknowledged
        nonlocal logical_started_monotonic
        nonlocal last_activity_monotonic
        nonlocal deadline_clock_checked_monotonic
        nonlocal deadline_clock_was_paused
        nonlocal idle_warning_emitted
        nonlocal stream_error
        nonlocal retire_supervisor
        nonlocal delivery_unknown

        observed_at = (
            time.monotonic()
            if now_monotonic is None
            else now_monotonic
        )
        if deadline_clock_was_paused:
            # Exclude elapsed time during which this exact run was waiting on
            # an unresolved user interaction. Once that wait settles, both
            # watchdog clocks resume normally.
            paused_for = max(
                0.0,
                observed_at - deadline_clock_checked_monotonic,
            )
            logical_started_monotonic += paused_for
            last_activity_monotonic += paused_for
        watchdog_paused = await claude_run_has_pending_interaction(
            session_id,
            current_run_id,
        )
        deadline_clock_checked_monotonic = observed_at
        deadline_clock_was_paused = watchdog_paused
        acknowledged_now = bool(
            getattr(current_handle, "acknowledged", False)
        )
        if acknowledged_now and not provider_acknowledged:
            provider_acknowledged = True
            last_activity_monotonic = observed_at
            idle_warning_emitted = False
        if watchdog_paused:
            # Publishing an exact-run approval is proof that this logical
            # provider turn is active, even if the SDK emits no frame while it
            # waits.
            provider_acknowledged = True
            last_activity_monotonic = observed_at
            idle_warning_emitted = False
            # The provider request itself is first activity. Do not let a
            # completed post-ACK watchdog spin or fail as soon as the user
            # answers after a long wait.
            await cancel_first_activity_watchdog()
            return False
        elapsed = observed_at - logical_started_monotonic
        idle = observed_at - last_activity_monotonic
        if (
            not provider_acknowledged
            and elapsed >= CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK did not confirm prompt delivery within "
                f"{CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS:g}s. The prompt "
                "was not replayed."
            )
            delivery_unknown = True
            retire_supervisor = True
            logger.warning(
                "Claude SDK pre-ACK timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        if (
            provider_acknowledged
            and idle >= CLAUDE_SDK_IDLE_WARN_SECONDS
            and not idle_warning_emitted
        ):
            await append_event(session_id, "idle_warning", {
                "run_id": current_run_id,
                "idle_seconds": int(idle),
            })
            idle_warning_emitted = True
        if (
            provider_acknowledged
            and idle >= CLAUDE_SDK_IDLE_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK produced no provider activity for "
                f"{CLAUDE_SDK_IDLE_TIMEOUT_SECONDS:g}s."
            )
            delivery_unknown = not provider_acknowledged
            retire_supervisor = True
            logger.warning(
                "Claude SDK idle timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        if (
            CLAUDE_SDK_TURN_TIMEOUT_SECONDS > 0
            and elapsed >= CLAUDE_SDK_TURN_TIMEOUT_SECONDS
        ):
            stream_error = (
                "Claude SDK exceeded the absolute turn timeout of "
                f"{CLAUDE_SDK_TURN_TIMEOUT_SECONDS:g}s."
            )
            delivery_unknown = not provider_acknowledged
            retire_supervisor = True
            logger.warning(
                "Claude SDK absolute timeout session=%s run=%s",
                session_id,
                current_run_id,
            )
            return True
        return False

    try:
        while True:
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            now_monotonic = time.monotonic()
            if await sdk_deadline_expired(now_monotonic):
                break
            if message_task is None:
                message_task = asyncio.create_task(current_handle.__anext__())
            if pending_steer is None and steer_task is None:
                steer_task = asyncio.create_task(native_steer_queue.get())
            waiters = {message_task}
            if steer_task is not None:
                waiters.add(steer_task)
            if first_activity_task is not None:
                waiters.add(first_activity_task)
            elapsed = now_monotonic - logical_started_monotonic
            idle = now_monotonic - last_activity_monotonic
            timeout_candidates = [5.0]
            if CLAUDE_SDK_TURN_TIMEOUT_SECONDS > 0:
                timeout_candidates.append(
                    CLAUDE_SDK_TURN_TIMEOUT_SECONDS
                    if deadline_clock_was_paused
                    else max(
                        0.01,
                        CLAUDE_SDK_TURN_TIMEOUT_SECONDS - elapsed,
                    )
                )
            if provider_acknowledged:
                idle_deadline = (
                    CLAUDE_SDK_IDLE_TIMEOUT_SECONDS
                    if idle_warning_emitted
                    else CLAUDE_SDK_IDLE_WARN_SECONDS
                )
                timeout_candidates.append(max(0.01, idle_deadline - idle))
            else:
                timeout_candidates.append(
                    max(0.01, CLAUDE_SDK_PRE_ACK_TIMEOUT_SECONDS - elapsed)
                )
            done, _pending = await asyncio.wait(
                waiters,
                timeout=max(0.01, min(timeout_candidates)),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                if await sdk_deadline_expired():
                    break
                continue
            if message_task in done:
                # A provider frame ready at the deadline is current activity;
                # give it the idle lease while still enforcing the absolute
                # and pre-ACK bounds below.
                last_activity_monotonic = time.monotonic()
                idle_warning_emitted = False
            if await sdk_deadline_expired():
                break

            if (
                first_activity_task is not None
                and first_activity_task in done
                and message_task not in done
            ):
                timed_out = first_activity_task.result()
                first_activity_task = None
                if timed_out:
                    stream_error = (
                        "Claude SDK acknowledged the turn but produced no provider "
                        "activity within "
                        f"{CLAUDE_SDK_POST_ACK_FIRST_ACTIVITY_TIMEOUT_SECONDS:g}s."
                    )
                    retire_supervisor = True
                    logger.warning(
                        "Claude SDK post-ACK first-activity timeout "
                        "session=%s run=%s",
                        session_id,
                        current_run_id,
                    )
                    break

            if steer_task is not None and steer_task in done:
                request = steer_task.result()
                steer_task = None
                mark_native_steer_accepted(request)
                selected = request.get("selected") or {}
                future = request.get("future")
                candidate_run_id = f"run_{uuid.uuid4().hex[:16]}"
                transition_nonce = secrets.token_hex(16)
                try:
                    request_prompt = build_user_provider_prompt(
                        session_id,
                        str(selected.get("prompt") or ""),
                        list(selected.get("file_ids") or []),
                    )
                except Exception as exc:
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            concise_error_message(exc),
                            safe_to_requeue=True,
                        ))
                    continue

                transition_ready = asyncio.Event()
                transition_reserved = False
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if not active or active.get("stop_requested"):
                        if isinstance(future, asyncio.Future) and not future.done():
                            future.set_exception(NativeSteerHandoffError(
                                "the active Claude turn is already stopping",
                                safe_to_requeue=True,
                            ))
                        continue
                    active["logical_transition_ready"] = transition_ready
                    active["logical_transition_candidate_run_id"] = candidate_run_id
                    active["logical_transition_predecessor_run_id"] = current_run_id
                    active["logical_transition_authority_nonce"] = transition_nonce
                    active["logical_transition_accepting_permissions"] = False
                    active["claude_permissions_open"] = False
                    transition_reserved = True
                interrupt_sent = False
                try:
                    await cancel_claude_interactions(
                        session_id,
                        resolution="turn_steered",
                    )
                    interrupt_sent = await interrupt_claude_sdk_run_bounded(
                        current_handle,
                    )
                except asyncio.TimeoutError as exc:
                    if transition_reserved:
                        await release_transition(transition_ready)
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            (
                                "Claude steering interrupt timed out before "
                                "the next prompt was sent"
                            ),
                            safe_to_requeue=True,
                        ))
                    # The selected steering prompt has not been queried, but
                    # the old run's interrupt state is unknowable. Retire only
                    # this chat supervisor and finish the current logical run.
                    retire_supervisor = True
                    stream_error = concise_error_message(exc)
                    RUNS.stopped_runs.add(current_run_id)
                    break
                except Exception as exc:
                    async with RUNS.active_lock:
                        active = RUNS.active.get(session_id)
                        if (
                            active
                            and not active.get("stop_requested")
                            and active.get("logical_transition_ready")
                            is transition_ready
                        ):
                            active["claude_permissions_open"] = True
                    if transition_reserved:
                        await release_transition(transition_ready)
                    if isinstance(future, asyncio.Future) and not future.done():
                        future.set_exception(NativeSteerHandoffError(
                            f"Claude steering interrupt failed: {concise_error_message(exc)}",
                            safe_to_requeue=True,
                        ))
                    continue
                pending_steer = {
                    "selected": selected,
                    "remaining": int(request.get("remaining") or 0),
                    "future": future,
                    "candidate_run_id": candidate_run_id,
                    "predecessor_run_id": current_run_id,
                    "transition_nonce": transition_nonce,
                    "request_prompt": request_prompt,
                    "transition_ready": transition_ready,
                    "transition_reserved": transition_reserved,
                    "interrupt_sent": interrupt_sent,
                }

            if message_task not in done:
                continue
            try:
                message = message_task.result()
            except StopAsyncIteration:
                message_task = None
                if not bool(getattr(current_handle, "done", False)):
                    stream_error = (
                        "Claude SDK message stream ended before a terminal result "
                        "became available."
                    )
                    retire_supervisor = True
                    result_details = None
                else:
                    result_details = claude_sdk_result_details(
                        await current_handle.wait_result()
                    )
            except Exception as exc:
                message_task = None
                if bool(getattr(exc, "delivery_uncertain", False)):
                    retire_supervisor = True
                record_sdk_stream_exception(exc)
                result_details = None
            else:
                message_task = None
                last_activity_monotonic = time.monotonic()
                provider_acknowledged = bool(
                    getattr(current_handle, "acknowledged", False)
                ) or provider_acknowledged
                idle_warning_emitted = False
                await cancel_first_activity_watchdog()
                message_provider_id = str(
                    claude_sdk_field(message, "session_id") or ""
                )
                if claude_sdk_type(message) == "SystemMessage":
                    system_data = claude_sdk_field(message, "data", {})
                    if isinstance(system_data, dict):
                        message_provider_id = str(
                            system_data.get("session_id")
                            or message_provider_id
                        )
                if message_provider_id:
                    provider_id = message_provider_id
                    if message_provider_id != provider_persisted_id:
                        await persist_run_provider_session(
                            session_id,
                            current_run_id,
                            BACKEND_CLAUDE,
                            message_provider_id,
                            cwd=cwd,
                        )
                        provider_persisted_id = message_provider_id
                if provider_id and claude_sdk_type(message) != "StreamEvent":
                    with suppress(Exception):
                        await refresh_claude_goal(session_id, provider_id=provider_id)
                projected_result = await project_claude_sdk_message(
                    session_id,
                    current_run_id,
                    message,
                    text_parts=text_parts,
                    current_tools=current_tools,
                    changed_paths=changed_paths,
                    tool_activity_run_ids=tool_activity_run_ids,
                    error_run_ids=projection_error_run_ids,
                )
                if projected_result is None:
                    continue
                result_details = projected_result

            if result_details and result_details.get("session_id"):
                provider_id = str(result_details["session_id"])
            if provider_id and provider_id != provider_persisted_id:
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )
                provider_persisted_id = provider_id
            if result_details is not None and provider_id:
                # The SDK's authoritative usage RPC is only sampled after its
                # terminal ResultMessage and remains serialized through the
                # chat-owned actor. Failure is telemetry-only and cannot hold
                # up finalization beyond the short bounded timeout.
                await sample_claude_context_usage(
                    session_id,
                    current_run_id,
                    provider_id,
                    manager,
                )
            if pending_steer is None:
                # Stop advertising native steering as soon as this provider
                # result is terminal. A Force Send that raced the result is
                # rejected below and safely requeued onto the next turn.
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and str(active.get("run_id") or "") == current_run_id
                    ):
                        active["provider_turn_ready"] = False
                        active["native_steer_queue"] = None
                        active["claude_permissions_open"] = False
                break

            steer_state = pending_steer
            selected = steer_state["selected"]
            future = steer_state["future"]
            transition_ready = steer_state["transition_ready"]
            # Prefer the provider's terminal reason when available. An
            # interrupt acknowledgement can lose a race with natural
            # completion; it is only a compatibility fallback for older CLIs
            # that omit terminal_reason.
            terminal_reason = str(
                (result_details or {}).get("terminal_reason") or ""
            )
            prior_aborted = (
                bool((result_details or {}).get("aborted"))
                if terminal_reason
                else bool(steer_state.get("interrupt_sent"))
            )
            active_stopping = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                active_stopping = not active or bool(active.get("stop_requested"))
            if active_stopping or stream_error or (
                result_details
                and result_details.get("is_error")
                and not prior_aborted
            ):
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        stream_error or str((result_details or {}).get("error") or "Claude ended before steering"),
                        safe_to_requeue=True,
                    ))
                break

            candidate_run_id = str(steer_state["candidate_run_id"])
            if provider_id:
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )
            candidate_query_allowed = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if (
                    active
                    and not active.get("stop_requested")
                    and active.get("logical_transition_ready")
                    is transition_ready
                    and str(
                        active.get("logical_transition_candidate_run_id") or ""
                    ) == candidate_run_id
                ):
                    # The old response is terminal. Open approval routing for
                    # the candidate before query(), which may synchronously
                    # invoke can_use_tool before returning its run handle.
                    active["logical_transition_accepting_permissions"] = True
                    candidate_query_allowed = True
            if not candidate_query_allowed:
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        "the active Claude turn stopped before steering delivery",
                        safe_to_requeue=True,
                    ))
                break

            # Seal every old-run observable before candidate delivery. A fast
            # steered query can write the manifest/worktree before query()
            # returns; rolling or diffing afterward would attribute those
            # writes to the interrupted run and contaminate its diff.
            previous_run_id = current_run_id
            previous_metadata = run_event_metadata(previous_run_id)
            previous_watcher = manifest_watch_task
            manifest_watch_task = None
            if previous_watcher is not None:
                previous_watcher.cancel()
                await asyncio.gather(previous_watcher, return_exceptions=True)
            previous_manifest_path = manifest_path.with_name(
                f".{previous_run_id}.json"
            )
            with suppress(OSError):
                previous_manifest_path.unlink()
            with suppress(OSError):
                manifest_path.replace(previous_manifest_path)
            previous_baseline = current_diff_baseline
            previous_paths = set(changed_paths)
            previous_artifacts = set(seen_artifacts)
            previous_result_details = dict(result_details or {})
            previous_prompt = current_prompt
            try:
                await finish_outputs(
                    previous_run_id,
                    previous_baseline,
                    previous_paths,
                    previous_artifacts,
                    previous_manifest_path,
                )
                candidate_baseline = await capture_git_baseline(
                    session_id,
                    candidate_run_id,
                    cwd,
                )
            except Exception as exc:
                # Keep the rolled old manifest reachable by the ordinary
                # finalizer if sealing itself failed. A partial collection may
                # already have consumed it, in which case retrying remains
                # harmless and diff publication stays idempotent.
                with suppress(Exception):
                    await finish_outputs(
                        previous_run_id,
                        previous_baseline,
                        previous_paths,
                        previous_artifacts,
                        previous_manifest_path,
                    )
                if previous_run_id not in outputs_finished_run_ids:
                    with suppress(OSError):
                        previous_manifest_path.replace(manifest_path)
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and active.get("logical_transition_ready")
                        is transition_ready
                    ):
                        active["claude_permissions_open"] = False
                await release_transition(transition_ready)
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        f"Claude steering preparation failed: {concise_error_message(exc)}",
                        safe_to_requeue=True,
                    ))
                break
            candidate_paths: set[str] = set()
            candidate_artifacts: set[str] = set()
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            previous_task_batch = await persist_claude_background_task_receipts(
                session_id, current_run_id, provider_id, current_handle,
                tracking_lost=prior_aborted,
            )
            if previous_task_batch is not None:
                receipts_persisted_run_ids.add(current_run_id)
            candidate_reconciliation_batches = (
                [previous_task_batch] if previous_task_batch is not None
                else current_reconciliation_batches
            )
            steer_state.update({
                "candidate_baseline": candidate_baseline,
                "candidate_paths": candidate_paths,
                "candidate_artifacts": candidate_artifacts,
                "candidate_query_attempted": False,
                "candidate_failure_handled": False,
            })
            with suppress(OSError):
                manifest_path.unlink()
            manifest_watch_task = asyncio.create_task(
                watch_manifest_artifacts(
                    session_id,
                    candidate_run_id,
                    manifest_path,
                    candidate_artifacts,
                )
            )

            async def activate_candidate_supervisor(
                ownership_token: str,
            ) -> None:
                steer_state["candidate_ownership_token"] = str(
                    ownership_token
                )
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        not active
                        or active.get("stop_requested")
                        or active.get("logical_transition_ready")
                        is not transition_ready
                        or str(
                            active.get("logical_transition_candidate_run_id")
                            or ""
                        ) != candidate_run_id
                    ):
                        raise NativeSteerHandoffError(
                            "the active Claude turn stopped before steering delivery",
                            safe_to_requeue=True,
                        )
                    active["claude_sdk_owner_token"] = str(ownership_token)
                    active["claude_permission_run_id"] = candidate_run_id
                    active["claude_permissions_open"] = True
            try:
                try:
                    await fence_native_steer_delivery(
                        session_id,
                        selected,
                        backend=BACKEND_CLAUDE,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise NativeSteerHandoffError(
                        (
                            "Claude steering was not sent because its durable "
                            f"delivery fence failed: {concise_error_message(exc)}"
                        ),
                        safe_to_requeue=True,
                    ) from exc
                try:
                    candidate_user_prompt = str(
                        steer_state["request_prompt"]
                    )
                    (
                        steer_state["request_prompt"],
                        steer_state["candidate_authority_path"],
                        steer_state["candidate_runtime_env"],
                    ) = await issue_native_steer_provider_authority(
                        session_id,
                        candidate_run_id,
                        selected,
                        str(steer_state["request_prompt"]),
                        str(steer_state["transition_nonce"]),
                        backend=BACKEND_CLAUDE,
                    )
                    assert_provider_user_message_unchanged(
                        str(steer_state["request_prompt"]),
                        candidate_user_prompt,
                    )
                except BaseException as exc:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    if isinstance(exc, NativeSteerHandoffError):
                        raise
                    raise NativeSteerHandoffError(
                        (
                            "Claude steering was not sent because replacement "
                            "authority issuance failed: "
                            f"{concise_error_message(exc)}"
                        ),
                        safe_to_requeue=True,
                    ) from exc
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    delivery_still_allowed = bool(
                        active
                        and not active.get("stop_requested")
                        and active.get("logical_transition_ready")
                        is transition_ready
                        and str(
                            active.get("logical_transition_candidate_run_id")
                            or ""
                        ) == candidate_run_id
                    )
                    if delivery_still_allowed:
                        active["logical_transition_authority_open"] = True
                if not delivery_still_allowed:
                    await revoke_provider_authority(candidate_run_id)
                    steer_state.pop("candidate_authority_path", None)
                    raise NativeSteerHandoffError(
                        "the active Claude turn stopped before steering delivery",
                        safe_to_requeue=True,
                    )
                candidate_sess = dict(
                    STORE.sessions.get(session_id) or sess
                )
                if provider_id:
                    candidate_sess.update({
                        "backend": BACKEND_CLAUDE,
                        "session_id": provider_id,
                        "claude_session_id": provider_id,
                        "claude_session_cwd": cwd,
                    })
                (
                    candidate_options,
                    candidate_configuration_key,
                    _candidate_cli_path,
                ) = build_claude_sdk_options(
                    session_id,
                    candidate_sess,
                    cwd,
                    manifest_path,
                    steer_state["candidate_runtime_env"],
                )
                # Once manager.start_run is entered, cancellation cannot
                # prove whether client.query accepted the prompt: the SDK
                # actor deliberately shields its command response. Preserve
                # this boundary for Force Send replay decisions and cleanup.
                steer_state["candidate_query_attempted"] = True
                candidate_handle = await manager.start_run(
                    session_id,
                    str(steer_state["request_prompt"]),
                    run_id=candidate_run_id,
                    options=candidate_options,
                    configuration_key=candidate_configuration_key,
                    on_supervisor_ready=activate_candidate_supervisor,
                    **({"background_task_reconciliation": reconciliation_envelope(candidate_reconciliation_batches)}
                       if candidate_reconciliation_batches else {}),
                )
                # start_run returns only after query() acceptance. From this
                # exact point onward the steering message is never safe to
                # replay, even if local projection or bookkeeping fails.
                steer_state["candidate_accepted"] = True
                await reset_first_activity_watchdog(candidate_handle)
            except Exception as exc:
                steer_state["candidate_failure_handled"] = True
                if steer_state.get("candidate_authority_path") is not None:
                    await revoke_provider_authority(candidate_run_id)
                    steer_state.pop("candidate_authority_path", None)
                safe_to_requeue = bool(getattr(exc, "safe_to_requeue", False))
                if not safe_to_requeue:
                    await revoke_provider_authority(previous_run_id)
                candidate_watcher = manifest_watch_task
                manifest_watch_task = None
                if candidate_watcher is not None:
                    candidate_watcher.cancel()
                    await asyncio.gather(
                        candidate_watcher,
                        return_exceptions=True,
                    )
                with suppress(Exception):
                    await finish_outputs(
                        candidate_run_id,
                        candidate_baseline,
                        candidate_paths,
                        candidate_artifacts,
                        manifest_path,
                    )
                async with RUNS.active_lock:
                    active = RUNS.active.get(session_id)
                    if (
                        active
                        and active.get("logical_transition_ready")
                        is transition_ready
                    ):
                        active["claude_permissions_open"] = False
                await release_transition(transition_ready)
                if bool(getattr(exc, "delivery_uncertain", False)):
                    retire_supervisor = True
                if isinstance(future, asyncio.Future) and not future.done():
                    future.set_exception(NativeSteerHandoffError(
                        concise_error_message(exc),
                        safe_to_requeue=safe_to_requeue,
                        delivery_uncertain=not safe_to_requeue,
                    ))
                if not safe_to_requeue:
                    stream_error = (
                        "Claude steering delivery could not be confirmed. The "
                        "message was not replayed."
                    )
                pending_steer = None
                break

            remaining = int(steer_state.get("remaining") or 0)

            metadata = queued_turn_run_metadata(selected)
            metadata["steer_interrupted_run_id"] = previous_run_id
            RUNS.run_metadata[candidate_run_id] = metadata
            display_prompt = str(
                selected.get("display_prompt")
                if selected.get("display_prompt") is not None
                else selected.get("prompt") or ""
            )
            display_file_ids = list(
                selected.get("display_file_ids")
                if selected.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            )
            lineage = turn_steering_lineage(RUNS.current_turns.get(session_id))
            steering_message = {
                "prompt": str(selected.get("prompt") or ""),
                "file_ids": list(selected.get("file_ids") or []),
            }
            if not lineage or lineage[-1] != steering_message:
                lineage.append(steering_message)

            current_run_id = candidate_run_id
            current_handle = candidate_handle
            current_reconciliation_batches = candidate_reconciliation_batches
            options = candidate_options
            configuration_key = candidate_configuration_key
            sdk_ownership_token = str(
                steer_state.get("candidate_ownership_token") or ""
            )
            provider_runtime_env = dict(
                steer_state.get("candidate_runtime_env") or {}
            )
            current_diff_baseline = candidate_baseline
            current_prompt = str(steer_state["request_prompt"])
            text_parts = []
            current_tools = {}
            changed_paths = candidate_paths
            seen_artifacts = candidate_artifacts
            result_details = None
            stream_error = None
            stopped_during_handoff = False
            candidate_provider_ready = bool(
                getattr(candidate_handle, "acknowledged", False)
                and not getattr(candidate_handle, "done", False)
            )
            logical_started_monotonic = time.monotonic()
            last_activity_monotonic = logical_started_monotonic
            deadline_clock_checked_monotonic = logical_started_monotonic
            deadline_clock_was_paused = False
            provider_acknowledged = candidate_provider_ready
            idle_warning_emitted = False
            delivery_unknown = False
            async with RUNS.active_lock:
                active = RUNS.active.get(session_id)
                if active:
                    stopped_during_handoff = bool(active.get("stop_requested"))
                    active.update({
                        "run_id": candidate_run_id,
                        "claude_sdk_run": candidate_handle,
                        "started_at": time.time(),
                        "started_at_iso": now_iso(),
                        "native_interrupt_sent": False,
                        "stop_requested": stopped_during_handoff,
                        "provider_turn_ready": candidate_provider_ready,
                        "provider_starting": not candidate_provider_ready,
                    })
                    if stopped_during_handoff:
                        RUNS.stopped_runs.add(candidate_run_id)
                    current = RUNS.current_turns.get(session_id)
                    if current is not None:
                        current.update({
                            "run_id": candidate_run_id,
                            "prompt": str(selected.get("prompt") or ""),
                            "display_prompt": display_prompt,
                            "file_ids": list(selected.get("file_ids") or []),
                            "queued_id": selected.get("queued_id"),
                            "skill_selection": selected.get("skill_selection"),
                            "steering_lineage": lineage,
                        })
                    steer_state["candidate_committed"] = True

            watch_provider_readiness(
                candidate_run_id,
                candidate_handle,
                already_published_ready=candidate_provider_ready,
            )

            if prior_aborted:
                await append_event(session_id, "turn_stopped", {
                    "run_id": previous_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "native_steer": True,
                    "provider_session_id": provider_id or None,
                    "superseded_by_run_id": candidate_run_id,
                    **previous_metadata,
                })
            else:
                previous_result_text = clean_assistant_text(
                    str(previous_result_details.get("result_text") or "")
                )
                previous_projection_error = (
                    previous_run_id in projection_error_run_ids
                )
                previous_empty_error = claude_empty_turn_failure_message(
                    prompt=previous_prompt,
                    result_text=previous_result_text,
                    had_tool_activity=(
                        previous_run_id in tool_activity_run_ids
                    ),
                    stopped=False,
                    terminal_result_received=True,
                    existing_error=bool(
                        previous_result_details.get("error")
                        or previous_result_details.get("is_error")
                        or previous_projection_error
                    ),
                )
                if previous_empty_error:
                    await append_event(session_id, "error", {
                        "run_id": previous_run_id,
                        "backend": BACKEND_CLAUDE,
                        "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                        "message": previous_empty_error,
                        **previous_metadata,
                    })
                if previous_empty_error or previous_projection_error:
                    record_runtime_failure(
                        BACKEND_CLAUDE,
                        previous_empty_error or "Claude assistant error",
                    )
                await append_turn_finished_event(session_id, {
                    "run_id": previous_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "exit_code": (
                        1
                        if previous_empty_error or previous_projection_error
                        else 0
                    ),
                    "result_text": previous_result_text,
                    **previous_metadata,
                })
            await revoke_provider_authority(previous_run_id)
            await append_durable_event_batch(session_id, [
                ("turn_queue_run_now", {
                    "queued_id": selected.get("queued_id"),
                    "backend": BACKEND_CLAUDE,
                    "prompt": display_prompt,
                    "request_prompt": str(selected.get("prompt") or ""),
                    "display_prompt": display_prompt,
                    "file_ids": list(selected.get("file_ids") or []),
                    "display_file_ids": display_file_ids,
                    "interrupted_run_id": previous_run_id,
                    "replays_interrupted_message": False,
                    "native_steer": True,
                    "message": "Steering message sent to the active Claude chat.",
                    "remaining": remaining,
                    "superseded_queued_ids": [],
                }),
                ("turn_started", {
                    "run_id": candidate_run_id,
                    "backend": BACKEND_CLAUDE,
                    "prompt": display_prompt,
                    "file_ids": display_file_ids,
                    "queued_id": selected.get("queued_id"),
                    "native_steer": True,
                    **metadata,
                }),
            ])
            await release_transition(transition_ready)
            if stopped_during_handoff:
                with suppress(Exception):
                    await interrupt_claude_sdk_run_bounded(candidate_handle)
            RUNS.run_metadata.pop(previous_run_id, None)
            RUNS.stopped_runs.discard(previous_run_id)
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_result({
                    "ok": True,
                    "queued_id": selected.get("queued_id"),
                    "run_id": candidate_run_id,
                    "interrupted": prior_aborted,
                    "native_steer": True,
                    "replays_interrupted_message": False,
                    "superseded_queued_ids": [],
                })
            # Keep the accepted handoff visible to cancellation/error cleanup
            # until its HTTP waiter has been resolved. Clearing this earlier
            # can orphan Force Send after provider delivery succeeded but
            # before local timeline bookkeeping completed.
            pending_steer = None
    except asyncio.CancelledError as exc:
        cancelled_error = exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
    except Exception as exc:
        shutdown_interrupted = record_sdk_stream_exception(exc)
        retire_supervisor = True
        if not shutdown_interrupted:
            logger.exception(
                "Claude SDK run failed session=%s run=%s",
                session_id,
                current_run_id,
            )

    async def cleanup_live_sdk_state() -> None:
        nonlocal retire_supervisor, stream_error
        unhandled_steer: dict[str, Any] | None = None
        readiness_tasks = tuple(provider_ready_tasks)
        for task in readiness_tasks:
            if not task.done():
                task.cancel()
        if readiness_tasks:
            await asyncio.gather(*readiness_tasks, return_exceptions=True)
        if (
            pending_steer is None
            and steer_task is not None
            and steer_task.done()
            and not steer_task.cancelled()
        ):
            with suppress(Exception):
                candidate = steer_task.result()
                if isinstance(candidate, dict):
                    unhandled_steer = candidate
        for task in (message_task, steer_task, first_activity_task):
            if task is not None and not task.done():
                task.cancel()
        if (
            message_task is not None
            or steer_task is not None
            or first_activity_task is not None
        ):
            await asyncio.gather(
                *(
                    task
                    for task in (
                        message_task,
                        steer_task,
                        first_activity_task,
                    )
                    if task is not None
                ),
                return_exceptions=True,
            )
        if pending_steer is not None:
            transition_ready = pending_steer.get("transition_ready")
            future = pending_steer.get("future")
            candidate_accepted = bool(
                pending_steer.get("candidate_accepted")
            )
            candidate_delivery_uncertain = bool(
                candidate_accepted
                or (
                    pending_steer.get("candidate_query_attempted")
                    and not pending_steer.get("candidate_failure_handled")
                )
            )
            candidate_run_id = str(
                pending_steer.get("candidate_run_id") or ""
            )
            if (
                candidate_run_id
                and pending_steer.get("candidate_authority_path") is not None
            ):
                with suppress(Exception):
                    await revoke_provider_authority(candidate_run_id)
                pending_steer.pop("candidate_authority_path", None)
            if candidate_delivery_uncertain:
                predecessor_run_id = str(
                    pending_steer.get("predecessor_run_id") or ""
                )
                if predecessor_run_id:
                    with suppress(Exception):
                        await revoke_provider_authority(predecessor_run_id)
            if isinstance(transition_ready, asyncio.Event):
                await release_transition(transition_ready)
            if candidate_delivery_uncertain:
                retire_supervisor = True
                stream_error = stream_error or (
                    "Claude steering may have been accepted, but its local "
                    "handoff did not complete. The message was not replayed."
                )
                candidate_baseline = pending_steer.get("candidate_baseline")
                candidate_paths = pending_steer.get("candidate_paths")
                candidate_artifacts = pending_steer.get("candidate_artifacts")
                if (
                    candidate_run_id
                    and isinstance(candidate_baseline, dict)
                    and isinstance(candidate_paths, set)
                    and isinstance(candidate_artifacts, set)
                ):
                    with suppress(Exception):
                        await finish_outputs(
                            candidate_run_id,
                            candidate_baseline,
                            candidate_paths,
                            candidate_artifacts,
                            manifest_path,
                        )
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_exception(NativeSteerHandoffError(
                    stream_error or "Claude steering ended before delivery",
                    safe_to_requeue=not candidate_delivery_uncertain,
                    delivery_uncertain=candidate_delivery_uncertain,
                ))
        elif unhandled_steer is None:
            # A queue.put() can win just after asyncio.wait() selects the
            # terminal ResultMessage. Drain the detached queue so its HTTP
            # waiter is never stranded after ACTIVE is released.
            with suppress(asyncio.QueueEmpty):
                candidate = native_steer_queue.get_nowait()
                if isinstance(candidate, dict):
                    unhandled_steer = candidate
        if unhandled_steer is not None:
            future = unhandled_steer.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.set_exception(NativeSteerHandoffError(
                    "Claude finished before the steering message was delivered",
                    safe_to_requeue=True,
                ))
        if manifest_watch_task is not None:
            manifest_watch_task.cancel()
            await asyncio.gather(manifest_watch_task, return_exceptions=True)
        await cancel_claude_interactions(
            session_id,
            resolution=(
                "turn_stopped"
                if cancelled_error is not None or current_run_id in shutdown_interrupted_run_ids
                else "turn_finished"
            ),
            expected_run_id=current_run_id,
            ownership_token=sdk_ownership_token or None,
        )

    cleanup_task = asyncio.create_task(cleanup_live_sdk_state())
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError as exc:
        cancelled_error = cancelled_error or exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
        await join_task_despite_caller_cancellation(cleanup_task)

    async def finalize_sdk_run() -> None:
        nonlocal current_reconciliation_batches, cancelled_error
        if retire_supervisor:
            with suppress(Exception):
                await interrupt_claude_sdk_run_bounded(current_handle)
            # Token-qualified eviction cannot target a newer SDK generation.
            # Without a supervisor token, fail closed: interrupting the exact
            # handle above is safe, while chat-wide eviction is not.
            if sdk_ownership_token:
                await evict_claude_sdk_chat(
                    session_id,
                    force=True,
                    manager=manager,
                    ownership_token=sdk_ownership_token or None,
                )
        if provider_id:
            with suppress(Exception):
                await persist_run_provider_session(
                    session_id,
                    current_run_id,
                    BACKEND_CLAUDE,
                    provider_id,
                    cwd=cwd,
                )

        # Native goal verdicts are persisted before the terminal result. The
        # provider, not an app-owned continuation loop, decides when to finish.
        with suppress(Exception):
            await refresh_claude_goal(session_id, provider_id=provider_id)
            if (CLAUDE_GOAL_PENDING.get(session_id) or {}).get("run_id") == current_run_id:
                CLAUDE_GOAL_PENDING.pop(session_id, None)
                await append_event(session_id, "claude_goal_changed", {})

        stopped = bool(
            cancelled_error is not None
            or current_run_id in RUNS.stopped_runs
            or (result_details or {}).get("aborted")
        )
        try:
            current_reconciliation_batches = await acknowledge_claude_background_reconciliation(
                session_id, current_run_id, provider_id, current_handle,
                current_reconciliation_batches,
            )
            if current_run_id not in receipts_persisted_run_ids:
                await persist_claude_background_task_receipts(
                    session_id, current_run_id, provider_id, current_handle,
                    tracking_lost=stopped or retire_supervisor,
                )
        except asyncio.CancelledError as exc:
            cancelled_error = cancelled_error or exc
            stopped = True
        except Exception:
            # A failed receipt write must not retain the parent execution
            # slot. Its unconsumed checkpoint remains available for retry.
            logger.exception("Claude background task reconciliation persistence failed")
        result_text = clean_assistant_text(
            str((result_details or {}).get("result_text") or "")
        )
        result_error = str((result_details or {}).get("error") or "")
        projection_error = current_run_id in projection_error_run_ids
        empty_result_error = claude_empty_turn_failure_message(
            prompt=current_prompt,
            result_text=result_text,
            had_tool_activity=current_run_id in tool_activity_run_ids,
            stopped=stopped,
            terminal_result_received=result_details is not None,
            existing_error=bool(
                stream_error
                or result_error
                or (result_details or {}).get("is_error")
                or projection_error
            ),
        )
        if stream_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": f"Claude SDK stream failed: {stream_error}",
                    **({"delivery_unknown": True} if delivery_unknown else {}),
                    **run_event_metadata(current_run_id),
                })
        if result_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": result_error,
                    **run_event_metadata(current_run_id),
                })
        if empty_result_error and not stopped:
            with suppress(Exception):
                await append_event(session_id, "error", {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "message": empty_result_error,
                    **run_event_metadata(current_run_id),
                })
        if (
            stream_error
            or (result_details or {}).get("is_error")
            or projection_error
            or empty_result_error
        ):
            if not stopped:
                record_runtime_failure(
                    BACKEND_CLAUDE,
                    stream_error
                    or result_error
                    or empty_result_error
                    or "Claude assistant error",
                )
        elif not stopped:
            record_runtime_success(BACKEND_CLAUDE)
        with suppress(Exception):
            await finish_outputs(
                current_run_id,
                current_diff_baseline,
                changed_paths,
                seen_artifacts,
                manifest_path,
            )
            await collect_recent_leftover_manifests(
                session_id,
                current_run_id,
                manifest_path,
                seen_artifacts=seen_artifacts,
            )
        drain_queue = should_schedule_queue_after_finish(session_id, stopped)
        released = await release_turn_slot(
            session_id,
            expected_run_id=current_run_id,
        )
        if released:
            try:
                await append_turn_finished_event(session_id, {
                    "run_id": current_run_id,
                    "backend": BACKEND_CLAUDE,
                    "transport": CLAUDE_TRANSPORT_AGENT_SDK,
                    "provider_session_id": provider_id or None,
                    "exit_code": (
                        None
                        if stopped
                        else 1 if (
                            stream_error
                            or result_error
                            or projection_error
                            or empty_result_error
                        ) else 0
                    ),
                    "result_text": result_text,
                    "stopped": stopped,
                    **({"reason": "server_shutdown"} if current_run_id in shutdown_interrupted_run_ids else {}),
                    **({"delivery_unknown": True} if delivery_unknown else {}),
                    **run_event_metadata(current_run_id),
                })
            except Exception:
                # Retire ownership, but do not automatically run more work
                # after its predecessor's completion could not be recorded.
                drain_queue = False
                logger.exception("Claude completion recording failed; automatic queue drain deferred")
        RUNS.run_metadata.pop(current_run_id, None)
        RUNS.stopped_runs.discard(current_run_id)
        if released and drain_queue:
            schedule_next_queued_turn(session_id)

    finalize_task = asyncio.create_task(finalize_sdk_run())
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError as exc:
        cancelled_error = cancelled_error or exc
        retire_supervisor = True
        RUNS.stopped_runs.add(current_run_id)
        await join_task_despite_caller_cancellation(finalize_task)
    if cancelled_error is not None:
        raise cancelled_error


async def run_claude(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    interactive_agent_sdk: bool = False,
    standalone_provider_context: bool = False,
    provider_command: ProviderCommandRecord | None = None,
    provider_runtime_env: dict[str, str] | None = None,
) -> None:
    use_sdk = bool(
        interactive_agent_sdk
        and not standalone_provider_context
        and CLAUDE_TRANSPORT != CLAUDE_TRANSPORT_PRINT
    )
    if not use_sdk:
        if provider_command is not None:
            await finish_claude_sdk_start_failure(
                session_id,
                run_id,
                "Claude provider commands require the interactive Agent SDK transport.",
            )
            return
        # A compatibility print turn advances the same provider conversation
        # outside the in-memory SDK client. Retire an idle supervisor first so
        # a later desktop SDK turn reconnects from the newly persisted ID
        # instead of continuing stale process-local context.
        if not await evict_claude_sdk_chat(session_id, force=True):
            await finish_claude_sdk_start_failure(
                session_id,
                run_id,
                (
                    "Claude's previous chat process is still shutting down. "
                    "No compatibility turn was started; retry shortly."
                ),
            )
            return
        if not standalone_provider_context:
            # The print transport advances the same Claude conversation but
            # cannot provide an authoritative live context sample. Never keep
            # advertising a stale SDK measurement after this compatibility
            # path wins.
            await mark_provider_context_usage_unavailable(
                session_id,
                BACKEND_CLAUDE,
            )
        await run_claude_print(
            session_id,
            run_id,
            prompt,
            sess,
            manifest_path,
            standalone_provider_context=standalone_provider_context,
            provider_runtime_env=provider_runtime_env,
        )
        return
    try:
        await run_claude_sdk(
            session_id,
            run_id,
            prompt,
            sess,
            manifest_path,
            provider_runtime_env=provider_runtime_env,
            **(
                {"provider_command": provider_command}
                if provider_command is not None
                else {}
            ),
        )
    except ClaudeSDKUnavailable as exc:
        # Interactive desktop clients opted into approval/question semantics.
        # The legacy print path deliberately skips permissions, so silently
        # downgrading an opted-in turn would execute with weaker safety than
        # the UI promised. Old clients and standalone jobs still select print
        # before entering this function and remain fully compatible.
        await finish_claude_sdk_start_failure(
            session_id,
            run_id,
            f"Claude Agent SDK is unavailable: {concise_error_message(exc)}",
        )


# The turn engine starts Claude turns through WIRING (it sits below this module).
WIRING.run_claude = run_claude

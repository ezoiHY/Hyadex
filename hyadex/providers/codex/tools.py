"""Provider tools for Codex: the MCP endpoint configuration and tool execution."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import hmac
import json
from pathlib import Path
import sys
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from hyadex.cli import HELPER_SCRIPTS
from hyadex.core.config import (
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    CODEX_DEFAULT_SANDBOX_MODE,
    CODEX_NONINTERACTIVE_APPROVAL_POLICY,
    CODEX_PROVIDER_MCP_HEADER_NAME,
    CODEX_PROVIDER_MCP_HEADER_SECRET,
    CODEX_PROVIDER_MCP_NAME,
    CODEX_PROVIDER_MCP_PATH,
    CODEX_PROVIDER_MCP_PROOF_KEY,
    SERVER_INSTANCE_ID,
    SERVER_ROOT,
)
from hyadex.core.processes import terminate_process_tree
from hyadex.core.runs import RUNS
from hyadex.core.session_codex import (
    existing_codex_app_server_manager,
    provider_helper_server_origin,
)
from hyadex.core.session_settings import codex_runtime_settings
from hyadex.core.sessions import PROVIDER_AUTHORITIES, PROVIDER_AUTHORITY_LOCK, STORE
from hyadex.features.side_questions import (
    codex_app_server_service_tier,
    flatten_codex_config_overrides,
)
from hyadex.providers.authority import (
    agent_runner_env,
    provider_capability_is_attached_to_live_run,
    provider_tool_active_matches,
    provider_tool_authority_path,
    ProviderToolError,
    redact_provider_tool_output,
    resolve_provider_tool_arguments,
    validate_provider_tool_input,
)
from hyadex.providers.codex.app_server_manager import CodexAppServerManager
from hyadex.providers.codex.config import codex_effective_thread_config
from hyadex.providers.codex.goals import codex_native_goal_owner_matches
from hyadex.providers.codex.threads import codex_thread_instructions
from hyadex.providers.instructions import (
    PROVIDER_TOOL_DESCRIPTION,
    PROVIDER_TOOL_INPUT_SCHEMA,
    PROVIDER_TOOL_MAX_OUTPUT_BYTES,
    PROVIDER_TOOL_REPLAY,
    PROVIDER_TOOL_REPLAY_LIMIT,
    PROVIDER_TOOL_REPLAY_LOCK,
    PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT,
    PROVIDER_TOOL_REPLAY_TOMBSTONES,
    PROVIDER_TOOL_HELPER_TIMEOUT_SECONDS,
    PROVIDER_TOOL_LONGEST_TIMEOUT_SECONDS,
    PROVIDER_TOOL_TIMEOUT_SECONDS,
)
from hyadex.providers.registry import request_client_is_provider_helper_local
from hyadex.providers.runtime import validate_provider_runtime_env
from hyadex.providers.state import PROVIDERS


def request_client_is_codex_provider_mcp_local(request: Request) -> bool:
    """Compatibility-specific name for the shared provider-local predicate."""

    return request_client_is_provider_helper_local(request)


async def provider_tool_capability_snapshot(
    session_id: str,
    run_id: str,
    *,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[Path, dict[str, str]]:
    """Resolve exactly one capability between two exact ACTIVE checks."""

    async with RUNS.active_lock:
        matches, ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches and ready is not None and not ready.is_set():
        try:
            await asyncio.wait_for(ready.wait(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            raise ProviderToolError("provider tool turn is not ready") from exc
    if backend == BACKEND_CLAUDE:
        manager = PROVIDERS.claude_sdk_manager
        if manager is None or not manager.owns_active_run(
            session_id,
            claude_owner_token,
            run_id,
        ):
            raise ProviderToolError("provider tool owner is stale")
    elif backend == BACKEND_CODEX:
        manager = existing_codex_app_server_manager(STORE.sessions.get(session_id))
        active_turn = manager.active_turn(provider_thread_id) if manager else None
        if (
            not (active_turn is not None
                 and str(active_turn.thread_id or "") == provider_thread_id
                 and str(active_turn.turn_id or "") == provider_turn_id)
            and not (manager is not None and manager.ready
                     and codex_native_goal_owner_matches(session_id, run_id, provider_thread_id, provider_turn_id))
        ):
            raise ProviderToolError("provider tool turn is stale")
    else:
        raise ProviderToolError("provider tool backend is unsupported")

    async with RUNS.active_lock:
        matches, _ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches:
        raise ProviderToolError("provider tool run is no longer active")

    async with PROVIDER_AUTHORITY_LOCK:
        candidates = [
            capability
            for capability in PROVIDER_AUTHORITIES.values()
            if str(capability.get("source_session_id") or "") == session_id
            and str(capability.get("source_run_id") or "") == run_id
            and not str(capability.get("native_transition_nonce") or "")
        ]
        if len(candidates) != 1 or not provider_capability_is_attached_to_live_run(
            session_id,
            run_id,
        ):
            raise ProviderToolError("provider authority is not active")
        capability = candidates[0]
        authority_path = provider_tool_authority_path(capability, run_id)
        runtime_env = validate_provider_runtime_env(
            capability.get("provider_runtime_env")
        )

    async with RUNS.active_lock:
        matches, _ready = provider_tool_active_matches(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
    if not matches:
        raise ProviderToolError("provider tool run ended during authorization")
    return authority_path, runtime_env


async def execute_provider_tool(
    session_id: str,
    run_id: str,
    value: Any,
    *,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[str, bool]:
    """Run one allow-listed helper subprocess with server-owned authority."""

    helper, arguments, stdin = validate_provider_tool_input(value)
    authority_path, runtime_env = await provider_tool_capability_snapshot(
        session_id,
        run_id,
        backend=backend,
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
        claude_owner_token=claude_owner_token,
    )
    resolved = resolve_provider_tool_arguments(helper, arguments, runtime_env)
    scripts = {
        "jobs": HELPER_SCRIPTS["jobs"],
        "publish": HELPER_SCRIPTS["publish"],
        "emergency": HELPER_SCRIPTS["emergency"],
        "notify": HELPER_SCRIPTS["notify"],
        "ask": HELPER_SCRIPTS["ask"],
    }
    command = [sys.executable, str(scripts[helper]), "--authority-file", str(authority_path)]
    if helper in {"jobs", "publish", "emergency", "notify", "ask"}:
        command.extend(["--chat-id", session_id])
    command.extend(resolved)
    proc: asyncio.subprocess.Process | None = None
    stream_tasks: list[asyncio.Task[Any]] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SERVER_ROOT),
            env=agent_runner_env(session_id, runtime_env),
            limit=PROVIDER_TOOL_MAX_OUTPUT_BYTES + 1,
            start_new_session=True,
        )
        output_size = {"value": 0}

        async def read_bounded(stream: asyncio.StreamReader | None) -> bytes:
            chunks: list[bytes] = []
            if stream is None:
                return b""
            while True:
                chunk = await stream.read(16 * 1024)
                if not chunk:
                    return b"".join(chunks)
                output_size["value"] += len(chunk)
                if output_size["value"] > PROVIDER_TOOL_MAX_OUTPUT_BYTES:
                    raise ProviderToolError(
                        "Hyadex helper output exceeded its safe limit"
                    )
                chunks.append(chunk)

        async def write_stdin() -> None:
            if proc is None or proc.stdin is None:
                return
            proc.stdin.write(stdin.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        stdout_task = asyncio.create_task(read_bounded(proc.stdout))
        stderr_task = asyncio.create_task(read_bounded(proc.stderr))
        stdin_task = asyncio.create_task(write_stdin())
        wait_task = asyncio.create_task(proc.wait())
        stream_tasks = [stdout_task, stderr_task, stdin_task, wait_task]
        stdout, stderr, _written, _returncode = await asyncio.wait_for(
            asyncio.gather(*stream_tasks),
            timeout=PROVIDER_TOOL_HELPER_TIMEOUT_SECONDS.get(helper, PROVIDER_TOOL_TIMEOUT_SECONDS),
        )
    except asyncio.TimeoutError as exc:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise ProviderToolError("Hyadex helper timed out") from exc
    except asyncio.CancelledError:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise
    except ProviderToolError:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise
    except Exception as exc:
        if proc is not None:
            await terminate_process_tree(proc, grace=0.5)
        raise ProviderToolError("Hyadex helper could not start") from exc
    finally:
        for task in stream_tasks:
            if not task.done():
                task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)
    selected = stdout if proc.returncode == 0 else stderr or stdout
    result = redact_provider_tool_output(selected.decode("utf-8", "replace"), authority_path).strip()
    if not result:
        result = "Hyadex helper completed." if proc.returncode == 0 else "Hyadex helper failed."
    return result, proc.returncode != 0


async def execute_provider_tool_once(
    session_id: str,
    run_id: str,
    value: Any,
    *,
    replay_key: str,
    backend: str,
    provider_thread_id: str = "",
    provider_turn_id: str = "",
    claude_owner_token: str = "",
) -> tuple[str, bool]:
    """Single-flight/replay one provider call."""

    helper, arguments, _stdin = validate_provider_tool_input(value)
    # A replay cache is idempotency state, never authority. Revalidate the
    # exact live owner and capability before even looking up an earlier call;
    # otherwise a stopped/revoked run could replay a cached success.
    await provider_tool_capability_snapshot(
        session_id,
        run_id,
        backend=backend,
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
        claude_owner_token=claude_owner_token,
    )
    key = (session_id, run_id, str(replay_key))
    input_digest = hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    leader = False
    async with PROVIDER_TOOL_REPLAY_LOCK:
        prior = PROVIDER_TOOL_REPLAY.get(key)
        if prior is not None and not hmac.compare_digest(prior[0], input_digest):
            raise ProviderToolError("provider tool call identity was reused with different input")
        tombstone_digest = PROVIDER_TOOL_REPLAY_TOMBSTONES.get(key)
        if tombstone_digest is not None:
            PROVIDER_TOOL_REPLAY_TOMBSTONES.move_to_end(key)
            if not hmac.compare_digest(tombstone_digest, input_digest):
                raise ProviderToolError(
                    "provider tool call identity was reused with different input"
                )
            raise ProviderToolError(
                "provider tool call was already completed; its result expired"
            )
        future = prior[1] if prior is not None else None
        if future is None:
            future = asyncio.get_running_loop().create_future()
            PROVIDER_TOOL_REPLAY[key] = (input_digest, future)
            leader = True
    if not leader:
        result = await asyncio.shield(future)
        # The original call may have outlived Stop/revocation while this
        # follower waited. Never surface its cached result to a stale owner.
        await provider_tool_capability_snapshot(
            session_id,
            run_id,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
        return result
    try:
        result = await execute_provider_tool(
            session_id,
            run_id,
            value,
            backend=backend,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            claude_owner_token=claude_owner_token,
        )
        if not future.done():
            future.set_result(result)
        return result
    except BaseException as exc:
        if not future.done():
            future.set_exception(exc)
            # This function is the first observer; consume the stored future's
            # exception so a failed non-replayed call never warns at shutdown.
            with suppress(BaseException):
                future.exception()
        raise
    finally:
        async with PROVIDER_TOOL_REPLAY_LOCK:
            PROVIDER_TOOL_REPLAY.move_to_end(key)
            while len(PROVIDER_TOOL_REPLAY) > PROVIDER_TOOL_REPLAY_LIMIT:
                evicted = False
                for old_key, (old_digest, old_future) in list(
                    PROVIDER_TOOL_REPLAY.items()
                ):
                    if not old_future.done():
                        continue
                    PROVIDER_TOOL_REPLAY.pop(old_key, None)
                    PROVIDER_TOOL_REPLAY_TOMBSTONES[old_key] = old_digest
                    PROVIDER_TOOL_REPLAY_TOMBSTONES.move_to_end(old_key)
                    evicted = True
                    break
                if not evicted:
                    # In-flight calls are concurrency state and cannot be
                    # evicted safely. Their count is bounded elsewhere by
                    # live provider/tool concurrency.
                    break
            while (
                len(PROVIDER_TOOL_REPLAY_TOMBSTONES)
                > PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT
            ):
                PROVIDER_TOOL_REPLAY_TOMBSTONES.popitem(last=False)


def codex_thread_params(
    sess: dict[str, Any],
    cwd: str,
    *,
    developer_instructions: str | None = None,
) -> dict[str, Any]:
    model, _effort, service_tier = codex_runtime_settings(sess)
    params: dict[str, Any] = {
        "cwd": cwd,
        "approvalPolicy": CODEX_NONINTERACTIVE_APPROVAL_POLICY,
        "sandbox": CODEX_DEFAULT_SANDBOX_MODE,
    }
    if developer_instructions is not None:
        params["developerInstructions"] = developer_instructions
    if model:
        params["model"] = model
    if service_tier:
        params["serviceTier"] = codex_app_server_service_tier(service_tier)
    # Honor explicit per-thread settings for start, resume and fork without
    # replacing the provider's subagent defaults or changing global config.
    config = codex_effective_thread_config(sess)
    if config:
        # Dotted keys: Codex applies the per-thread config map like ``-c``
        # overrides, and a nested ``{"agents": {...}}`` would replace the
        # user's whole [agents] table instead of only the leaf we set.
        params["config"] = flatten_codex_config_overrides(config)
    # This process-secret HTTP MCP is a provider tool transport, not model
    # context.  The static endpoint learns the exact live run only from
    # Codex-owned turn metadata supplied below at turn/start.
    flat_config = params.setdefault("config", {})
    reserved_prefix = f"mcp_servers.{CODEX_PROVIDER_MCP_NAME}"
    for key in tuple(flat_config):
        if key == reserved_prefix or key.startswith(reserved_prefix + "."):
            flat_config.pop(key, None)
    flat_config.update(codex_provider_mcp_config())
    return params


def codex_provider_mcp_config() -> dict[str, Any]:
    """Return the static, process-scoped provider-tool MCP configuration."""

    prefix = f"mcp_servers.{CODEX_PROVIDER_MCP_NAME}"
    return {
        f"{prefix}.url": (
            f"{provider_helper_server_origin()}{CODEX_PROVIDER_MCP_PATH}"
        ),
        f"{prefix}.http_headers": {
            CODEX_PROVIDER_MCP_HEADER_NAME: CODEX_PROVIDER_MCP_HEADER_SECRET,
        },
        f"{prefix}.required": True,
        f"{prefix}.default_tools_approval_mode": "approve",
        f"{prefix}.supports_parallel_tool_calls": False,
        f"{prefix}.startup_timeout_sec": 10,
        f"{prefix}.tool_timeout_sec": int(PROVIDER_TOOL_LONGEST_TIMEOUT_SECONDS + 5),
    }


def codex_provider_mcp_run_proof(
    session_id: str,
    provider_thread_id: str,
    run_id: str,
) -> str:
    """Bind provider metadata to one server process, chat, thread, and run."""

    payload = json.dumps(
        [
            "hyadex-provider-mcp-v1",
            SERVER_INSTANCE_ID,
            str(session_id),
            str(provider_thread_id),
            str(run_id),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        CODEX_PROVIDER_MCP_PROOF_KEY,
        payload,
        hashlib.sha256,
    ).hexdigest()


async def start_standalone_codex_app_server_thread(
    manager: CodexAppServerManager,
    session_id: str,
    sess: dict[str, Any],
    cwd: str,
) -> str:
    """Start one non-persistent Codex thread for a standalone job run."""

    return await manager.start_thread({
        **codex_thread_params(
            sess,
            cwd,
            developer_instructions=codex_thread_instructions(
                session_id,
                sess,
            ),
        ),
        # Output is persisted in the parent Hyadex job run. Avoid creating
        # a second durable Codex chat that the user cannot navigate directly.
        "ephemeral": True,
        "serviceName": "Hyadex Scheduled Job",
    })


def request_exact_codex_provider_mcp_secret(request: Request) -> bool:
    """Authenticate the process-private Codex MCP transport header."""

    if request.url.query:
        return False
    raw_headers = request.scope.get("headers", [])
    names = [bytes(name).lower() for name, _value in raw_headers]
    forbidden = {
        b"authorization",
        b"cookie",
        b"x-hyadex-token",
        b"x-zenithdock-token",
        b"x-hyadex-provider-capability",
        b"x-hyadex-cross-chat-capability",
    }
    if any(name in forbidden for name in names):
        return False
    expected_name = CODEX_PROVIDER_MCP_HEADER_NAME.lower().encode("ascii")
    values = [
        bytes(value)
        for name, value in raw_headers
        if bytes(name).lower() == expected_name
    ]
    if len(values) != 1:
        return False
    try:
        supplied = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    return hmac.compare_digest(supplied, CODEX_PROVIDER_MCP_HEADER_SECRET)


def codex_provider_mcp_tool_definition() -> dict[str, Any]:
    return {
        "name": "run",
        "description": PROVIDER_TOOL_DESCRIPTION,
        "inputSchema": PROVIDER_TOOL_INPUT_SCHEMA,
    }


def codex_provider_mcp_jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
) -> JSONResponse:
    return JSONResponse({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    })

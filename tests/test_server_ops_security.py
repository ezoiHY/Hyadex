import asyncio
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import httpx
from pydantic import ValidationError

from tests import hyadex_server
from hyadex.providers import runtime as provider_runtime
from hyadex.cli import emergency as hyadex_emergency
from hyadex.cli import jobs as hyadex_jobs
from hyadex.cli import publish as hyadex_publish


def http_request(
    method: str,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    receive=None,
) -> hyadex_server.Request:
    scope = {
        "type": "http", "app": hyadex_server.app,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 7870),
    }
    if receive is None:
        return hyadex_server.Request(scope)
    return hyadex_server.Request(scope, receive=receive)


class FakeProviderStream:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def read(self, _size: int) -> bytes:
        payload, self.payload = self.payload, b""
        return payload


class FakeProviderStdin:
    def __init__(self):
        self.payload = b""
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payload += payload

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProviderProcess:
    def __init__(self, *, stdout: bytes, stderr: bytes, returncode: int):
        self.stdout = FakeProviderStream(stdout)
        self.stderr = FakeProviderStream(stderr)
        self.stdin = FakeProviderStdin()
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


class ServerOpsSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_agent_notification_route_has_bounded_helper_access(self):
        self.assertEqual(
            hyadex_server.agent_helper_route_body_limit(
                "POST", "/api/agent/sessions/chat/notifications"
            ),
            8 * 1024,
        )
        self.assertIsNone(
            hyadex_server.agent_helper_route_body_limit(
                "GET", "/api/agent/sessions/chat/notifications"
            )
        )

    async def test_user_authored_authority_lookalike_is_never_stripped(self):
        lookalike = (
            "Keep this literal text.\n\n"
            "[Hyadex provider authority]\n"
            "authority-file=/Users/zen/.hyadex/cross_chat_authority/"
            "run_0123456789abcdef-0123456789abcdef0123456789abcdef.json "
            "chat-id=sess_0123456789abcdef "
            "(bound to this server, chat, and live run)\n"
            "actions=none\n"
            "usage: see Hyadex instructions\n"
            "[End Hyadex provider authority]\n"
        )
        self.assertEqual(
            hyadex_server.strip_hyadex_generated_user_text(lookalike),
            lookalike,
        )

    async def test_helper_parsers_disable_protected_option_abbreviations(self):
        cases = (
            (hyadex_jobs.build_parser(), ["--auth", "evil", "list"]),
            (hyadex_publish.build_parser(), ["--auth", "evil"]),
            (
                hyadex_emergency.build_parser(),
                ["--auth", "evil", "alert", "--message", "x"],
            ),

        )
        for parser, arguments in cases:
            with self.subTest(prog=parser.prog), patch.object(
                parser,
                "error",
                side_effect=ValueError("abbreviation rejected"),
            ):
                with self.assertRaises(ValueError):
                    parser.parse_args(arguments)

    async def test_helper_subcommands_disable_all_option_abbreviations(self):
        cases = (
            (
                hyadex_jobs.build_parser(),
                ["create", "--title", "x", "--prompt", "x", "--int", "20"],
            ),
            (
                hyadex_emergency.build_parser(),
                ["alert", "--m", "urgent"],
            ),

        )
        for parser, arguments in cases:
            with self.subTest(prog=parser.prog, arguments=arguments), patch(
                "sys.stderr", MagicMock()
            ):
                with self.assertRaises(SystemExit):
                    parser.parse_args(arguments)

    async def test_provider_tool_rejects_retired_chat_routing_flags(self):
        for arguments in (
            ["list", "--async-response"],
            ["list", "--async"],
            ["list", "--target-index", "1"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(hyadex_server.ProviderToolError):
                    hyadex_server.resolve_provider_tool_arguments(
                        "jobs", arguments, {},
                    )
        self.assertEqual(
            hyadex_server.resolve_provider_tool_arguments("jobs", ["list"], {}),
            ["list"],
        )

    async def test_provider_tool_cached_result_never_outlives_live_authority(self):
        value = {"helper": "jobs", "arguments": ["list"]}
        for backend, owner_kwargs in (
            (
                hyadex_server.BACKEND_CODEX,
                {
                    "provider_thread_id": "thread-live",
                    "provider_turn_id": "turn-live",
                },
            ),
            (
                hyadex_server.BACKEND_CLAUDE,
                {"claude_owner_token": "owner-live"},
            ),
        ):
            with self.subTest(backend=backend):
                capability = AsyncMock(
                    return_value=(Path("/private/authority"), {})
                )
                executor = AsyncMock(return_value=("ok", False))
                with patch.object(
                    hyadex_server,
                    "PROVIDER_TOOL_REPLAY",
                    hyadex_server.OrderedDict(),
                ), patch.object(
                    hyadex_server,
                    "PROVIDER_TOOL_REPLAY_LOCK",
                    asyncio.Lock(),
                ), patch.object(
                    hyadex_server,
                    "provider_tool_capability_snapshot",
                    capability,
                ), patch.object(
                    hyadex_server,
                    "execute_provider_tool",
                    executor,
                ):
                    first = await hyadex_server.execute_provider_tool_once(
                        "chat-live",
                        "run_live",
                        value,
                        replay_key="same-call",
                        backend=backend,
                        **owner_kwargs,
                    )
                    self.assertEqual(first, ("ok", False))
                    self.assertEqual(
                        await hyadex_server.execute_provider_tool_once(
                            "chat-live",
                            "run_live",
                            value,
                            replay_key="same-call",
                            backend=backend,
                            **owner_kwargs,
                        ),
                        ("ok", False),
                    )
                    executor.assert_awaited_once()

                    capability.side_effect = hyadex_server.ProviderToolError(
                        "provider authority is not active"
                    )
                    with self.assertRaises(hyadex_server.ProviderToolError):
                        await hyadex_server.execute_provider_tool_once(
                            "chat-live",
                            "run_live",
                            value,
                            replay_key="same-call",
                            backend=backend,
                            **owner_kwargs,
                        )
                    executor.assert_awaited_once()

    async def test_provider_tool_replay_payload_cache_has_hard_bound(self):
        value = {"helper": "jobs", "arguments": ["list"]}
        capability = AsyncMock(return_value=(Path("/private/authority"), {}))
        executor = AsyncMock(return_value=("x" * 100_000, False))
        replay = hyadex_server.OrderedDict()
        tombstones = hyadex_server.OrderedDict()
        with patch.object(
            hyadex_server,
            "PROVIDER_TOOL_REPLAY",
            replay,
        ), patch.object(
            hyadex_server,
            "PROVIDER_TOOL_REPLAY_TOMBSTONES",
            tombstones,
        ), patch.object(
            hyadex_server,
            "PROVIDER_TOOL_REPLAY_LIMIT",
            1,
        ), patch.object(
            hyadex_server,
            "PROVIDER_TOOL_REPLAY_LOCK",
            asyncio.Lock(),
        ), patch.object(
            hyadex_server,
            "provider_tool_capability_snapshot",
            capability,
        ), patch.object(
            hyadex_server,
            "execute_provider_tool",
            executor,
        ):
            for replay_key in ("call-1", "call-2"):
                await hyadex_server.execute_provider_tool_once(
                    "chat-live",
                    "run_live",
                    value,
                    replay_key=replay_key,
                    backend=hyadex_server.BACKEND_CODEX,
                    provider_thread_id="thread-live",
                    provider_turn_id="turn-live",
                )
            self.assertEqual(len(replay), 1)
            self.assertEqual(len(tombstones), 1)
            self.assertIn(("chat-live", "run_live", "call-1"), tombstones)
            with self.assertRaises(hyadex_server.ProviderToolError):
                await hyadex_server.execute_provider_tool_once(
                    "chat-live",
                    "run_live",
                    value,
                    replay_key="call-1",
                    backend=hyadex_server.BACKEND_CODEX,
                    provider_thread_id="thread-live",
                    provider_turn_id="turn-live",
                )
            self.assertEqual(executor.await_count, 2)

    async def test_provider_tool_generic_failure_keeps_stderr_precedence(self):
        process = FakeProviderProcess(
            stdout=b"partial non-JSON output\n",
            stderr=b"specific helper failure\n",
            returncode=2,
        )
        with patch.object(
            hyadex_server,
            "provider_tool_capability_snapshot",
            AsyncMock(return_value=(Path("/private/authority"), {})),
        ), patch.object(
            hyadex_server,
            "agent_runner_env",
            return_value={},
        ), patch.object(
            hyadex_server.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ):
            text, is_error = await hyadex_server.execute_provider_tool(
                "chat-live",
                "run_live",
                {"helper": "jobs", "arguments": ["list"]},
                backend=hyadex_server.BACKEND_CODEX,
                provider_thread_id="thread-live",
                provider_turn_id="turn-live",
            )

        self.assertTrue(is_error)
        self.assertEqual(text, "specific helper failure")

    async def test_provider_generated_context_and_tool_inputs_are_bounded(self):
        exact_user_text = "User-authored text must remain byte-for-byte exact."
        payload = provider_runtime.ProviderTurnPayload(
            exact_user_text,
            "runtime",
            {},
        )
        self.assertEqual(payload.user_prompt, exact_user_text)
        self.assertLessEqual(
            len(hyadex_server.PROVIDER_THREAD_INSTRUCTION_ADDENDUM),
            hyadex_server.MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS,
        )
        with self.assertRaises(provider_runtime.ProviderRuntimeContextError):
            provider_runtime.validate_provider_runtime_context(
                "\U0001f4e6" * (provider_runtime.MAX_PROVIDER_RUNTIME_CONTEXT_BYTES // 4 + 1)
            )
        with self.assertRaises(hyadex_server.ProviderToolError):
            hyadex_server.validate_provider_tool_input({
                "helper": "jobs",
                "arguments": [
                    "get",
                    "x" * (hyadex_server.PROVIDER_TOOL_MAX_ARGUMENT_CHARS + 1),
                ],
            })
        with self.assertRaises(hyadex_server.ProviderToolError):
            hyadex_server.validate_provider_tool_input({
                "helper": "publish",
                "arguments": ["--title", "report"],
                "stdin": "\U0001f4e6" * (
                    hyadex_server.PROVIDER_TOOL_MAX_STDIN_BYTES // 4 + 1
                ),
            })
        for malformed in ("\ud800", "prefix\udfff"):
            with self.subTest(malformed=repr(malformed)):
                with self.assertRaises(hyadex_server.ProviderToolError):
                    hyadex_server.validate_provider_tool_input({
                        "helper": "jobs",
                        "arguments": ["get", malformed],
                    })
                with self.assertRaises(hyadex_server.ProviderToolError):
                    hyadex_server.validate_provider_tool_input({
                        "helper": "publish",
                        "arguments": ["--title", "report"],
                        "stdin": malformed,
                    })
        with self.assertRaises(provider_runtime.ProviderRuntimeContextError):
            provider_runtime.validate_provider_runtime_context("\ud800")

    async def test_provider_tool_rejects_huge_target_index_as_safe_error(self):
        with self.assertRaises(hyadex_server.ProviderToolError):
            hyadex_server.resolve_provider_tool_arguments(
                "jobs",
                ["list", "--target-index", "9" * 5_000],
                {},
            )

    async def test_provider_tool_preserves_supported_message_and_job_boundaries(self):
        job_prompt = "j" * hyadex_server.MAX_JOB_PROMPT_CHARS
        helper, arguments, _stdin = hyadex_server.validate_provider_tool_input({
            "helper": "jobs",
            "arguments": [
                "create",
                "--title",
                "Boundary job",
                "--prompt",
                job_prompt,
                "--interval-seconds",
                "3600",
            ],
        })
        self.assertEqual(helper, "jobs")
        self.assertEqual(arguments[4], job_prompt)

        message = "m" * hyadex_server.PROVIDER_TOOL_MAX_ARGUMENT_CHARS
        helper, arguments, stdin = hyadex_server.validate_provider_tool_input({
            "helper": "notify",
            "arguments": ["--message", message],
        })
        self.assertEqual(helper, "notify")
        self.assertEqual(arguments[-1], message)
        self.assertEqual(stdin, "")

    async def test_provider_tool_rejects_abbreviated_identity_overrides(self):
        for argument in (
            "--a",
            "--auth",
            "--cha",
            "--sess",
            "--source",
            "--run",
            "--provider-t",
            "--tok",
            "--cap",
            "--tar",
            "--server-u",
            "--en",
        ):
            with self.subTest(argument=argument):
                with self.assertRaises(hyadex_server.ProviderToolError):
                    hyadex_server.validate_provider_tool_input({
                        "helper": "jobs",
                        "arguments": [argument, "attacker", "list"],
                    })

    async def test_codex_provider_mcp_rejects_nonexclusive_transport_auth_before_body(self):
        secret = "process-scoped-mcp-secret"
        secret_name = hyadex_server.CODEX_PROVIDER_MCP_HEADER_NAME.lower().encode(
            "ascii"
        )

        async def body_must_not_be_read():
            self.fail("Codex provider MCP body was read before authentication")

        header_cases = {
            "missing": [],
            "wrong": [(secret_name, b"wrong-secret")],
            "duplicate": [
                (secret_name, secret.encode("ascii")),
                (secret_name, secret.encode("ascii")),
            ],
            "authorization": [
                (secret_name, secret.encode("ascii")),
                (b"authorization", b"Bearer ambient-authority"),
            ],
            "origin": [
                (secret_name, secret.encode("ascii")),
                (b"origin", b"https://attacker.example"),
            ],
            "cookie": [
                (secret_name, secret.encode("ascii")),
                (b"cookie", b"ambient=value"),
            ],
            "sec-fetch": [
                (secret_name, secret.encode("ascii")),
                (b"sec-fetch-site", b"cross-site"),
            ],
        }
        for label, transport_headers in header_cases.items():
            with self.subTest(case=label):
                request = http_request(
                    "POST",
                    hyadex_server.CODEX_PROVIDER_MCP_PATH,
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"content-length", b"1"),
                        *transport_headers,
                    ],
                    receive=body_must_not_be_read,
                )
                downstream = AsyncMock()
                with patch.object(
                    hyadex_server,
                    "CODEX_PROVIDER_MCP_HEADER_SECRET",
                    secret,
                ):
                    response = await hyadex_server.require_agent_token(
                        request,
                        downstream,
                    )

                self.assertEqual(response.status_code, 403)
                downstream.assert_not_awaited()

    async def test_codex_provider_mcp_returns_safe_error_for_non_utf8_scalar(self):
        session_id = "chat-live"
        thread_id = "thread-live"
        turn_id = "turn-live"
        run_id = "run_live"
        payload = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "run",
                "arguments": {
                    "helper": "jobs",
                    "arguments": ["get", "\ud800"],
                },
                "_meta": {
                    "callId": "call-malformed-unicode",
                    "x-codex-turn-metadata": {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "hyadex_run_id": run_id,
                        "hyadex_run_proof": (
                            hyadex_server.codex_provider_mcp_run_proof(
                                session_id,
                                thread_id,
                                run_id,
                            )
                        ),
                    },
                },
            },
        }
        body = json.dumps(payload).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = http_request(
            "POST",
            hyadex_server.CODEX_PROVIDER_MCP_PATH,
            headers=[
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
            receive=receive,
        )
        request.state.codex_provider_mcp_authenticated = True
        with patch.object(
            hyadex_server.STORE,
            "sessions",
            {
                session_id: {
                    "id": session_id,
                    "backend": hyadex_server.BACKEND_CODEX,
                    "codex_thread_id": thread_id,
                }
            },
        ), patch.object(
            hyadex_server.RUNS,
            "active",
            {
                session_id: {
                    "run_id": run_id,
                    "backend": hyadex_server.BACKEND_CODEX,
                    "transport": hyadex_server.CODEX_TRANSPORT_APP_SERVER,
                    "provider_thread_id": thread_id,
                }
            },
        ), patch.object(
            hyadex_server.RUNS,
            "busy",
            {session_id},
        ), patch.object(
            hyadex_server.RUNS,
            "active_lock",
            asyncio.Lock(),
        ):
            response = await hyadex_server.codex_provider_mcp(request)

        decoded = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(decoded["result"]["isError"])
        self.assertIn("valid UTF-8", decoded["result"]["content"][0]["text"])

    async def test_codex_provider_mcp_exact_turn_metadata_invokes_executor(self):
        secret = "process-scoped-mcp-secret"
        session_id = "chat-live"
        thread_id = "thread-live"
        turn_id = "turn-live"
        run_id = "run_live"
        arguments = {"helper": "jobs", "arguments": ["list"]}
        proof = hyadex_server.codex_provider_mcp_run_proof(
            session_id,
            thread_id,
            run_id,
        )
        payload = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "run",
                "arguments": arguments,
                "_meta": {
                    "callId": "call-live",
                    "x-codex-turn-metadata": {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        # Top-level chats created through thread/fork retain
                        # ancestry metadata; that alone is not a subagent.
                        "forked_from_thread_id": "thread-parent",
                        "hyadex_run_id": run_id,
                        "hyadex_run_proof": proof,
                    },
                },
            },
        }
        executor = AsyncMock(return_value=("provider result", False))
        transport = httpx.ASGITransport(app=hyadex_server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:7870",
        ) as client:
            with patch.object(
                hyadex_server,
                "CODEX_PROVIDER_MCP_HEADER_SECRET",
                secret,
            ), patch.object(
                hyadex_server.STORE,
                "sessions",
                {
                    session_id: {
                        "id": session_id,
                        "backend": hyadex_server.BACKEND_CODEX,
                        "codex_thread_id": thread_id,
                    }
                },
            ), patch.object(
                hyadex_server.RUNS,
                "active",
                {
                    session_id: {
                        "run_id": run_id,
                        "backend": hyadex_server.BACKEND_CODEX,
                        "transport": hyadex_server.CODEX_TRANSPORT_APP_SERVER,
                        "provider_thread_id": thread_id,
                        "provider_turn_id": turn_id,
                        "provider_turn_ready": True,
                        "stop_requested": False,
                    }
                },
            ), patch.object(
                hyadex_server.RUNS,
                "current_turns",
                {session_id: {"run_id": run_id}},
            ), patch.object(
                hyadex_server.RUNS,
                "busy",
                {session_id},
            ), patch.object(
                hyadex_server,
                "DELETING_SESSIONS",
                set(),
            ), patch.object(
                hyadex_server,
                "DELETED_SESSION_TOMBSTONES",
                set(),
            ), patch.object(
                hyadex_server.RUNS,
                "active_lock",
                asyncio.Lock(),
            ), patch.object(
                hyadex_server,
                "UNSAFE_HTTP_MUTATION_ADMISSION_LOCK",
                asyncio.Lock(),
            ), patch.object(
                hyadex_server,
                "UNSAFE_HTTP_MUTATION_TASKS",
                {},
            ), patch.object(
                hyadex_server,
                "managed_server_restart_blocks_work",
                return_value=False,
            ), patch.object(
                hyadex_server,
                "execution_maintenance_blocks_work",
                return_value=False,
            ), patch.object(
                hyadex_server,
                "execute_provider_tool_once",
                executor,
            ):
                normal_response = await client.post(
                    hyadex_server.CODEX_PROVIDER_MCP_PATH,
                    json=payload,
                    headers={
                        hyadex_server.CODEX_PROVIDER_MCP_HEADER_NAME: secret,
                    },
                )

                # A standalone scheduled job owns a live ephemeral thread,
                # intentionally absent from persistent session identity.
                hyadex_server.STORE.sessions[session_id][
                    "codex_thread_id"
                ] = "parked-chat-thread"
                hyadex_server.RUNS.active[session_id][
                    "standalone_provider_context"
                ] = True
                standalone_payload = json.loads(json.dumps(payload))
                standalone_payload["id"] = 9
                standalone_payload["params"]["_meta"]["callId"] = (
                    "call-standalone"
                )
                standalone_response = await client.post(
                    hyadex_server.CODEX_PROVIDER_MCP_PATH,
                    json=standalone_payload,
                    headers={
                        hyadex_server.CODEX_PROVIDER_MCP_HEADER_NAME: secret,
                    },
                )

        self.assertEqual(normal_response.status_code, 200)
        self.assertEqual(normal_response.json(), {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "content": [{"type": "text", "text": "provider result"}],
                "isError": False,
            },
        })
        self.assertEqual(standalone_response.status_code, 200)
        self.assertEqual(standalone_response.json()["result"]["isError"], False)
        self.assertEqual(executor.await_count, 2)
        self.assertEqual(
            executor.await_args_list[0].kwargs["replay_key"],
            "codex:call-live",
        )
        executor.assert_awaited_with(
            session_id,
            run_id,
            arguments,
            replay_key="codex:call-standalone",
            backend=hyadex_server.BACKEND_CODEX,
            provider_thread_id=thread_id,
            provider_turn_id=turn_id,
        )

    async def test_main_cli_port_updates_required_provider_endpoint(self):
        original_bind = hyadex_server.SERVER.bind_address
        original_port = hyadex_server.SERVER.port
        run_server = MagicMock()
        try:
            with patch.object(
                sys,
                "argv",
                ["agent_server.py", "--bind", "127.0.0.1", "--port", "17850"],
            ), patch.object(
                hyadex_server,
                "configure_server_logging",
            ), patch.object(
                hyadex_server.uvicorn,
                "run",
                run_server,
            ):
                self.assertEqual(hyadex_server.main(), 0)

            self.assertEqual(hyadex_server.SERVER.bind_address, "127.0.0.1")
            self.assertEqual(hyadex_server.SERVER.port, 17850)
            config = hyadex_server.codex_provider_mcp_config()
            self.assertEqual(
                config[
                    f"mcp_servers.{hyadex_server.CODEX_PROVIDER_MCP_NAME}.url"
                ],
                "http://127.0.0.1:17850/api/agent/provider-tools/mcp",
            )
            run_server.assert_called_once()
        finally:
            hyadex_server.SERVER.bind_address = original_bind
            hyadex_server.SERVER.port = original_port

    async def test_codex_provider_mcp_config_supports_every_bind_family(self):
        prefix = f"mcp_servers.{hyadex_server.CODEX_PROVIDER_MCP_NAME}.url"
        cases = {
            "0.0.0.0": "http://127.0.0.1:17850/api/agent/provider-tools/mcp",
            "127.0.0.1": "http://127.0.0.1:17850/api/agent/provider-tools/mcp",
            "localhost": "http://127.0.0.1:17850/api/agent/provider-tools/mcp",
            "::": "http://[::1]:17850/api/agent/provider-tools/mcp",
            "::1": "http://[::1]:17850/api/agent/provider-tools/mcp",
            "192.0.2.40": "http://192.0.2.40:17850/api/agent/provider-tools/mcp",
            "2001:db8::40": "http://[2001:db8::40]:17850/api/agent/provider-tools/mcp",
        }
        for bind_address, expected in cases.items():
            with self.subTest(bind=bind_address), patch.object(
                hyadex_server.SERVER,
                "bind_address",
                bind_address,
            ), patch.object(hyadex_server.SERVER, "port", 17850):
                self.assertEqual(
                    hyadex_server.codex_provider_mcp_config()[prefix],
                    expected,
                )

    async def test_provider_runner_env_uses_exact_origin_and_bypasses_proxies(self):
        with patch.object(
            hyadex_server.SERVER,
            "bind_address",
            "2001:db8::40",
        ), patch.object(
            hyadex_server.SERVER,
            "port",
            17850,
        ), patch.dict(
            hyadex_server.os.environ,
            {
                "HTTP_PROXY": "http://proxy.example:8080",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "NO_PROXY": "example.test",
                "no_proxy": "other.test",
            },
            clear=False,
        ):
            runner = hyadex_server.agent_runner_env("chat-specific-bind")
            codex = hyadex_server.codex_app_server_env()

        expected = "http://[2001:db8::40]:17850"
        for environment in (runner, codex):
            self.assertEqual(environment["HYADEX_SERVER_URL"], expected)
            self.assertIn("2001:db8::40", environment["NO_PROXY"].split(","))
            self.assertIn("[2001:db8::40]", environment["NO_PROXY"].split(","))
            self.assertIn("2001:db8::40", environment["no_proxy"].split(","))
            self.assertIn("[2001:db8::40]", environment["no_proxy"].split(","))
        self.assertIn("example.test", runner["NO_PROXY"].split(","))
        self.assertIn("other.test", runner["no_proxy"].split(","))

    async def test_agent_helper_middleware_accepts_exact_specific_bind_peer(self):
        token = "specific-bind-provider-capability"
        token_hash = hyadex_server.hashlib.sha256(token.encode()).hexdigest()
        request = http_request(
            "GET",
            "/api/agent/sessions/chat/jobs",
            headers=[
                (b"x-hyadex-provider-capability", token.encode()),
            ],
        )
        request.scope["client"] = ("192.0.2.40", 50000)
        downstream = AsyncMock(
            return_value=hyadex_server.JSONResponse({"jobs": []})
        )
        with patch.object(
            hyadex_server.SERVER,
            "bind_address",
            "192.0.2.40",
        ), patch.object(
            hyadex_server,
            "PROVIDER_AUTHORITIES",
            {token_hash: {"source_run_id": "run-specific-bind"}},
        ):
            response = await hyadex_server.require_agent_token(
                request,
                downstream,
            )

        self.assertEqual(response.status_code, 200)
        downstream.assert_awaited_once_with(request)

    async def test_codex_provider_mcp_accepts_only_exact_local_specific_bind_peer(self):
        secret = "process-scoped-mcp-secret"

        async def invoke(client_host: str, *, proxy_header: bool = False):
            frames = [{
                "type": "http.request",
                "body": b"{}",
                "more_body": False,
            }]

            async def receive():
                return frames.pop(0)

            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", b"2"),
                (
                    hyadex_server.CODEX_PROVIDER_MCP_HEADER_NAME.lower().encode("ascii"),
                    secret.encode("ascii"),
                ),
            ]
            if proxy_header:
                headers.append((b"x-forwarded-for", b"127.0.0.1"))
            request = http_request(
                "POST",
                hyadex_server.CODEX_PROVIDER_MCP_PATH,
                headers=headers,
                receive=receive,
            )
            request.scope["client"] = (client_host, 50000)
            downstream = AsyncMock(
                return_value=hyadex_server.JSONResponse({"ok": True})
            )
            with patch.object(
                hyadex_server.SERVER,
                "bind_address",
                "192.0.2.40",
            ), patch.object(
                hyadex_server,
                "CODEX_PROVIDER_MCP_HEADER_SECRET",
                secret,
            ):
                response = await hyadex_server.require_agent_token(
                    request,
                    downstream,
                )
            return response, downstream

        accepted, accepted_downstream = await invoke("192.0.2.40")
        self.assertEqual(accepted.status_code, 200)
        accepted_downstream.assert_awaited_once()

        remote, remote_downstream = await invoke("192.0.2.41")
        self.assertEqual(remote.status_code, 403)
        remote_downstream.assert_not_awaited()

        proxied, proxied_downstream = await invoke(
            "192.0.2.40",
            proxy_header=True,
        )
        self.assertEqual(proxied.status_code, 403)
        proxied_downstream.assert_not_awaited()

    async def test_codex_provider_mcp_rejects_untrusted_turn_metadata(self):
        session_id = "chat-live"
        thread_id = "thread-live"
        turn_id = "turn-live"
        run_id = "run_live"
        arguments = {"helper": "jobs", "arguments": ["list"]}
        proof = hyadex_server.codex_provider_mcp_run_proof(
            session_id,
            thread_id,
            run_id,
        )
        base_payload = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "run",
                "arguments": arguments,
                "_meta": {
                    "callId": "call-untrusted",
                    "x-codex-turn-metadata": {
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "hyadex_run_id": run_id,
                        "hyadex_run_proof": proof,
                    },
                },
            },
        }
        sessions = {
            session_id: {
                "id": session_id,
                "backend": hyadex_server.BACKEND_CODEX,
                "codex_thread_id": thread_id,
            }
        }
        active = {
            session_id: {
                "run_id": run_id,
                "backend": hyadex_server.BACKEND_CODEX,
                "transport": hyadex_server.CODEX_TRANSPORT_APP_SERVER,
                "provider_thread_id": thread_id,
            }
        }
        busy = {session_id}
        executor = AsyncMock()

        async def invoke(payload: dict) -> hyadex_server.Response:
            body = json.dumps(payload).encode("utf-8")

            async def receive():
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }

            request = http_request(
                "POST",
                hyadex_server.CODEX_PROVIDER_MCP_PATH,
                headers=[
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
                receive=receive,
            )
            request.state.codex_provider_mcp_authenticated = True
            return await hyadex_server.codex_provider_mcp(request)

        def reset_state() -> dict:
            sessions.clear()
            sessions[session_id] = {
                "id": session_id,
                "backend": hyadex_server.BACKEND_CODEX,
                "codex_thread_id": thread_id,
            }
            active.clear()
            active[session_id] = {
                "run_id": run_id,
                "backend": hyadex_server.BACKEND_CODEX,
                "transport": hyadex_server.CODEX_TRANSPORT_APP_SERVER,
                "provider_thread_id": thread_id,
            }
            busy.clear()
            busy.add(session_id)
            return json.loads(json.dumps(base_payload))

        with patch.object(
            hyadex_server.STORE,
            "sessions",
            sessions,
        ), patch.object(
            hyadex_server.RUNS,
            "active",
            active,
        ), patch.object(
            hyadex_server.RUNS,
            "busy",
            busy,
        ), patch.object(
            hyadex_server,
            "DELETING_SESSIONS",
            set(),
        ), patch.object(
            hyadex_server,
            "DELETED_SESSION_TOMBSTONES",
            set(),
        ), patch.object(
            hyadex_server.RUNS,
            "active_lock",
            asyncio.Lock(),
        ), patch.object(
            hyadex_server,
            "execute_provider_tool_once",
            executor,
        ):
            cases = []

            payload = reset_state()
            active.clear()
            cases.append(("no active owner", payload, "Stale turn metadata"))

            payload = reset_state()
            active["chat-other"] = dict(active[session_id])
            busy.add("chat-other")
            cases.append(("two active owners", payload, "Stale turn metadata"))

            payload = reset_state()
            sessions["chat-other"] = {
                "id": "chat-other",
                "backend": hyadex_server.BACKEND_CODEX,
                "codex_thread_id": thread_id,
            }
            cases.append(("two stored owners", payload, "Ambiguous thread owner"))

            payload = reset_state()
            sessions["chat-other"] = {
                "id": "chat-other",
                "backend": hyadex_server.BACKEND_CLAUDE,
                # A backend switch parks the Codex identity; it remains an
                # owner and must keep this thread ambiguous.
                "codex_thread_id": thread_id,
                "claude_session_id": "claude-other",
            }
            cases.append(("parked stored owner", payload, "Ambiguous thread owner"))

            payload = reset_state()
            payload["params"]["_meta"]["x-codex-turn-metadata"][
                "hyadex_run_proof"
            ] = "0" * 64
            cases.append(("wrong proof", payload, "Invalid turn proof"))

            payload = reset_state()
            payload["params"]["_meta"]["x-codex-turn-metadata"][
                "parent_thread_id"
            ] = "parent-thread"
            cases.append((
                "subagent marker",
                payload,
                "Subagent tool calls are forbidden",
            ))

            for label, payload, expected_message in cases:
                with self.subTest(case=label):
                    # Restore the state paired with each payload before invoking
                    # it; the cases above deliberately mutate shared live maps.
                    reset_state()
                    if label == "no active owner":
                        active.clear()
                    elif label == "two active owners":
                        active["chat-other"] = dict(active[session_id])
                        busy.add("chat-other")
                    elif label == "two stored owners":
                        sessions["chat-other"] = {
                            "id": "chat-other",
                            "backend": hyadex_server.BACKEND_CODEX,
                            "codex_thread_id": thread_id,
                        }
                    elif label == "parked stored owner":
                        sessions["chat-other"] = {
                            "id": "chat-other",
                            "backend": hyadex_server.BACKEND_CLAUDE,
                            "codex_thread_id": thread_id,
                            "claude_session_id": "claude-other",
                        }
                    response = await invoke(payload)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        json.loads(response.body)["error"],
                        {"code": -32602, "message": expected_message},
                    )

        executor.assert_not_awaited()

    async def test_agent_helper_rejects_missing_unknown_and_ambiguous_capability_before_body(self):
        async def body_must_not_be_read():
            self.fail("agent-helper body was read before capability authentication")

        header_cases = {
            "missing": [],
            "unknown": [
                (b"x-hyadex-provider-capability", b"unknown-capability"),
            ],
            "legacy_name": [
                (b"x-hyadex-cross-chat-capability", b"legacy-capability"),
            ],
            "canonical_plus_legacy": [
                (b"x-hyadex-provider-capability", b"capability-one"),
                (b"x-hyadex-cross-chat-capability", b"capability-one"),
            ],
            "duplicate": [
                (b"x-hyadex-provider-capability", b"capability-one"),
                (b"x-hyadex-provider-capability", b"capability-two"),
            ],
        }
        for label, capability_headers in header_cases.items():
            with self.subTest(case=label):
                request = http_request(
                    "POST",
                    "/api/agent/sessions/chat/emergency-alerts",
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"content-length", b"1"),
                        *capability_headers,
                    ],
                    receive=body_must_not_be_read,
                )
                downstream = AsyncMock()
                with patch.object(hyadex_server, "PROVIDER_AUTHORITIES", {}):
                    response = await hyadex_server.require_agent_token(
                        request,
                        downstream,
                    )

                self.assertEqual(response.status_code, 403)
                downstream.assert_not_awaited()

    async def test_agent_helper_rejects_browser_metadata_before_body(self):
        token = "live-provider-capability"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        async def body_must_not_be_read():
            self.fail("browser-origin agent-helper body was read")

        for name, value in (
            (b"origin", b"https://attacker.example"),
            (b"cookie", b"ambient=value"),
            (b"sec-fetch-site", b"cross-site"),
            (b"x-forwarded-for", b"192.0.2.44"),
        ):
            with self.subTest(header=name):
                request = http_request(
                    "POST",
                    "/api/agent/sessions/chat/emergency-alerts",
                    headers=[
                        (b"content-type", b"application/json"),
                        (b"content-length", b"1"),
                        (b"x-hyadex-provider-capability", token.encode()),
                        (name, value),
                    ],
                    receive=body_must_not_be_read,
                )
                downstream = AsyncMock()
                with patch.object(
                    hyadex_server,
                    "PROVIDER_AUTHORITIES",
                    {token_hash: {"source_run_id": "run-live"}},
                ):
                    response = await hyadex_server.require_agent_token(
                        request,
                        downstream,
                    )

                self.assertEqual(response.status_code, 403)
                downstream.assert_not_awaited()

    async def test_agent_helper_rejects_declared_oversize_body_before_receive(self):
        token = "live-provider-capability"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        receive_called = False

        async def body_must_not_be_read():
            nonlocal receive_called
            receive_called = True
            return {
                "type": "http.request",
                "body": b"{}",
                "more_body": False,
            }

        request = http_request(
            "POST",
            "/api/agent/sessions/chat/emergency-alerts",
            headers=[
                (b"content-type", b"application/json"),
                # The emergency payload permits only a 500-character message;
                # its transport ceiling is deliberately 8 KiB.
                (b"content-length", b"8193"),
                (b"x-hyadex-provider-capability", token.encode()),
            ],
            receive=body_must_not_be_read,
        )
        downstream = AsyncMock()
        with patch.object(
            hyadex_server,
            "PROVIDER_AUTHORITIES",
            {token_hash: {"source_run_id": "run-live"}},
        ):
            response = await hyadex_server.require_agent_token(request, downstream)

        self.assertEqual(response.status_code, 413)
        self.assertFalse(receive_called)
        downstream.assert_not_awaited()

    async def test_agent_helper_bounds_chunked_body_between_receive_frames(self):
        token = "live-provider-capability"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        frames = [
            {
                "type": "http.request",
                "body": b"a" * 5_000,
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b"b" * 4_000,
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b"must-not-be-consumed",
                "more_body": False,
            },
        ]
        receive_calls = 0
        handler_completed = False

        async def receive():
            nonlocal receive_calls
            frame = frames[receive_calls]
            receive_calls += 1
            return frame

        async def body_handler(request):
            nonlocal handler_completed
            await request.body()
            handler_completed = True
            return hyadex_server.JSONResponse({"ok": True})

        request = http_request(
            "POST",
            "/api/agent/sessions/chat/emergency-alerts",
            headers=[
                (b"content-type", b"application/json"),
                (b"transfer-encoding", b"chunked"),
                (b"x-hyadex-provider-capability", token.encode()),
            ],
            receive=receive,
        )
        with patch.object(
            hyadex_server,
            "PROVIDER_AUTHORITIES",
            {token_hash: {"source_run_id": "run-live"}},
        ):
            response = await hyadex_server.require_agent_token(
                request,
                body_handler,
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(receive_calls, 2)
        self.assertFalse(handler_completed)

    async def test_agent_helper_accepts_current_cli_header_and_small_unframed_body(self):
        token = "live-provider-capability"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        frames = [
            {
                "type": "http.request",
                "body": b'{"request_id":"request-1",',
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b'"message":"help"}',
                "more_body": False,
            },
        ]
        received_body = b""

        async def receive():
            return frames.pop(0)

        async def body_handler(request):
            nonlocal received_body
            received_body = await request.body()
            return hyadex_server.JSONResponse({"ok": True})

        request = http_request(
            "POST",
            "/api/agent/sessions/chat/emergency-alerts",
            headers=[
                (b"content-type", b"application/json"),
                (b"x-hyadex-provider-capability", token.encode()),
            ],
            receive=receive,
        )
        with patch.object(
            hyadex_server,
            "PROVIDER_AUTHORITIES",
            {token_hash: {"source_run_id": "run-live"}},
        ):
            response = await hyadex_server.require_agent_token(
                request,
                body_handler,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            received_body,
            b'{"request_id":"request-1","message":"help"}',
        )

    def test_agent_helper_loopback_rejects_proxy_identity_headers(self):
        self.assertTrue(
            hyadex_server.request_client_is_loopback(
                http_request("POST", "/api/agent/sessions/chat/jobs")
            )
        )
        for name in (b"x-forwarded-for", b"forwarded", b"tailscale-user-login"):
            with self.subTest(header=name):
                request = http_request(
                    "POST",
                    "/api/agent/sessions/chat/jobs",
                    headers=[(name, b"remote.example")],
                )
                self.assertFalse(hyadex_server.request_client_is_loopback(request))

    async def test_body_stall_does_not_hold_mutation_lease(self):
        release_final_frame = asyncio.Event()
        waiting_for_final_frame = asyncio.Event()
        body_frames = 0
        handler_reached = False

        async def receive():
            nonlocal body_frames
            body_frames += 1
            if body_frames == 1:
                return {
                    "type": "http.request",
                    "body": b'{"name":',
                    "more_body": True,
                }
            waiting_for_final_frame.set()
            await release_final_frame.wait()
            return {
                "type": "http.request",
                "body": b'"stalled"}',
                "more_body": False,
            }

        async def body_handler(request):
            nonlocal handler_reached
            await request.body()
            handler_reached = True
            return hyadex_server.JSONResponse({"saved": True})

        update_active = False
        request = http_request(
            "POST",
            "/api/sessions",
            headers=[(b"content-type", b"application/json")],
            receive=receive,
        )
        with patch.object(hyadex_server, "AGENT_TOKEN", ""), \
             patch.object(hyadex_server, "UNSAFE_HTTP_MUTATION_TASKS", {}), \
             patch.object(
                 hyadex_server,
                 "managed_server_restart_blocks_work",
                 return_value=False,
             ), \
             patch.object(
                 hyadex_server,
                 "execution_maintenance_blocks_work",
                 side_effect=lambda: update_active,
             ):
            response_task = asyncio.create_task(
                hyadex_server.require_agent_token(request, body_handler)
            )
            await asyncio.wait_for(waiting_for_final_frame.wait(), timeout=1)
            self.assertEqual(hyadex_server.unsafe_http_mutation_count_locked(), 0)
            update_active = True
            release_final_frame.set()
            response = await asyncio.wait_for(response_task, timeout=1)

        self.assertEqual(response.status_code, 409)
        self.assertIn(b"execution worker is in maintenance", response.body)
        self.assertFalse(handler_reached)
        self.assertEqual(hyadex_server.unsafe_http_mutation_count_locked(), 0)

    async def test_completed_body_acquires_lease_before_handler(self):
        sent = False
        observed_count = 0

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {
                "type": "http.request",
                "body": b'{"name":"ready"}',
                "more_body": False,
            }

        async def body_handler(request):
            nonlocal observed_count
            await request.body()
            observed_count = hyadex_server.unsafe_http_mutation_count_locked()
            return hyadex_server.JSONResponse({"saved": True})

        request = http_request(
            "POST",
            "/api/sessions",
            headers=[(b"content-type", b"application/json")],
            receive=receive,
        )
        with patch.object(hyadex_server, "AGENT_TOKEN", ""), \
             patch.object(hyadex_server, "UNSAFE_HTTP_MUTATION_TASKS", {}), \
             patch.object(
                 hyadex_server,
                 "managed_server_restart_blocks_work",
                 return_value=False,
             ), \
             patch.object(
                 hyadex_server,
                 "execution_maintenance_blocks_work",
                 return_value=False,
             ):
            response = await hyadex_server.require_agent_token(
                request,
                body_handler,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(observed_count, 1)
        self.assertEqual(hyadex_server.unsafe_http_mutation_count_locked(), 0)

    async def test_asgi_body_stall_cannot_reserve_lifecycle_fence(self):
        first_frame_requested = asyncio.Event()
        release_final_frame = asyncio.Event()
        update_active = False

        async def body_chunks():
            first_frame_requested.set()
            yield b'{"name":'
            await release_final_frame.wait()
            yield b'"stalled"}'

        transport = httpx.ASGITransport(app=hyadex_server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:7870",
        ) as client:
            with patch.object(hyadex_server, "AGENT_TOKEN", ""), \
                 patch.object(hyadex_server, "UNSAFE_HTTP_MUTATION_TASKS", {}), \
                 patch.object(
                     hyadex_server,
                     "managed_server_restart_blocks_work",
                     return_value=False,
                 ), \
                 patch.object(
                     hyadex_server,
                     "execution_maintenance_blocks_work",
                     side_effect=lambda: update_active,
                 ):
                request_task = asyncio.create_task(
                    client.post(
                        "/api/sessions",
                        content=body_chunks(),
                        headers={"content-type": "application/json"},
                    )
                )
                await asyncio.wait_for(first_frame_requested.wait(), timeout=1)
                await asyncio.sleep(0)
                self.assertEqual(
                    hyadex_server.unsafe_http_mutation_count_locked(),
                    0,
                )
                update_active = True
                release_final_frame.set()
                response = await asyncio.wait_for(request_task, timeout=1)

        self.assertEqual(response.status_code, 409)
        self.assertIn("execution worker is in maintenance", response.text)
        self.assertEqual(hyadex_server.unsafe_http_mutation_count_locked(), 0)

    async def test_asgi_final_frame_lease_survives_receiver_task(self):
        handler_entered = asyncio.Event()
        release_handler = asyncio.Event()
        observed_count = 0

        async def slow_create(request):
            nonlocal observed_count
            observed_count = hyadex_server.unsafe_http_mutation_count_locked()
            handler_entered.set()
            await release_handler.wait()
            return {
                "id": "created-chat",
                "title": request.title,
                "backend": hyadex_server.BACKEND_CLAUDE,
            }

        transport = httpx.ASGITransport(app=hyadex_server.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:7870",
        ) as client:
            with patch.object(hyadex_server, "AGENT_TOKEN", ""), \
                 patch.object(hyadex_server, "UNSAFE_HTTP_MUTATION_TASKS", {}), \
                 patch.object(
                     hyadex_server,
                     "managed_server_restart_blocks_work",
                     return_value=False,
                 ), \
                 patch.object(
                     hyadex_server,
                     "execution_maintenance_blocks_work",
                     return_value=False,
                 ), \
                 patch.object(hyadex_server.STORE, "create", new=slow_create):
                request_task = asyncio.create_task(
                    client.post(
                        "/api/sessions",
                        json={"title": "lease-test", "import_history": False},
                    )
                )
                await asyncio.wait_for(handler_entered.wait(), timeout=1)
                self.assertEqual(observed_count, 1)
                self.assertEqual(
                    hyadex_server.unsafe_http_mutation_count_locked(),
                    1,
                )
                self.assertFalse(request_task.done())
                release_handler.set()
                response = await asyncio.wait_for(request_task, timeout=1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(hyadex_server.unsafe_http_mutation_count_locked(), 0)

    async def test_claude_event_scan_runs_off_event_loop(self):
        loop_thread = threading.get_ident()
        scan_threads: list[int] = []
        manager = MagicMock()
        manager.is_loaded.return_value = True

        def scan(_session_id, *, limit):
            self.assertEqual(limit, 1)
            scan_threads.append(threading.get_ident())
            return {"active_count": 1}

        with patch.object(hyadex_server, "active_codex_work_labels", return_value=[]), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", manager), \
             patch.object(
                 hyadex_server.STORE,
                 "sessions",
                 {"claude-chat": {"backend": hyadex_server.BACKEND_CLAUDE}},
             ), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(
                 hyadex_server,
                 "build_claude_subagent_snapshot",
                 side_effect=scan,
             ):
            labels = await hyadex_server.active_provider_background_work_labels_async()

        self.assertEqual(labels, ["Claude background work in claude-chat"])
        self.assertEqual(len(scan_threads), 1)
        self.assertNotEqual(scan_threads[0], loop_thread)

    async def test_claude_event_scan_finishes_before_admission_locks(self):
        scan_started = threading.Event()
        release_scan = threading.Event()
        manager = MagicMock()
        manager.is_loaded.return_value = True

        def scan(_session_id, *, limit):
            self.assertEqual(limit, 1)
            scan_started.set()
            release_scan.wait()
            return {"active_count": 0}

        with patch.object(hyadex_server, "active_codex_work_labels", return_value=[]), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", manager), \
             patch.object(
                 hyadex_server.STORE,
                 "sessions",
                 {"claude-chat": {"backend": hyadex_server.BACKEND_CLAUDE}},
             ), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(
                 hyadex_server,
                 "build_claude_subagent_snapshot",
                 side_effect=scan,
             ), \
             patch.object(
                 hyadex_server,
                 "server_restart_tmux_cgroup_state",
                 return_value={},
             ):
            snapshot_task = asyncio.create_task(
                hyadex_server.current_server_restart_blocker_snapshot()
            )
            started = await asyncio.wait_for(
                asyncio.to_thread(scan_started.wait, 1),
                timeout=2,
            )
            self.assertTrue(started)
            await asyncio.wait_for(
                hyadex_server.UNSAFE_HTTP_MUTATION_ADMISSION_LOCK.acquire(),
                timeout=0.2,
            )
            hyadex_server.UNSAFE_HTTP_MUTATION_ADMISSION_LOCK.release()
            release_scan.set()
            snapshot = await asyncio.wait_for(snapshot_task, timeout=2)

        self.assertFalse(snapshot["has_blockers"])

    async def test_changed_claude_events_fail_closed_after_prelock_fold(self):
        manager = MagicMock()
        manager.is_loaded.return_value = True
        with tempfile.TemporaryDirectory() as temporary:
            event_file = Path(temporary) / "events.jsonl"
            event_file.write_text("")
            with patch.object(hyadex_server, "active_codex_work_labels", return_value=[]), \
                 patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", manager), \
                 patch.object(
                     hyadex_server.STORE,
                     "sessions",
                     {"claude-chat": {"backend": hyadex_server.BACKEND_CLAUDE}},
                 ), \
                 patch.object(hyadex_server.RUNS, "busy", set()), \
                 patch.object(hyadex_server.RUNS, "maintenance", set()), \
                 patch.object(hyadex_server, "events_path", return_value=event_file), \
                 patch.object(
                     hyadex_server,
                     "build_claude_subagent_snapshot",
                     return_value={"active_count": 0},
                 ):
                snapshot = (
                    await hyadex_server.prepare_provider_background_work_snapshot()
                )
                event_file.write_text("changed\n")
                labels = hyadex_server.provider_background_work_labels_from_snapshot(
                    snapshot
                )

        self.assertEqual(
            labels,
            ["Claude provider state changed during inspection"],
        )
        self.assertTrue(snapshot["stale"])

    async def test_replayed_running_codex_child_with_terminal_native_turn_is_idle(self):
        class CodexManager:
            ready = True
            generation = 17

            def __init__(self):
                self.list_turns_calls = []

            @staticmethod
            def active_turn(_thread_id):
                return None

            async def list_turns(self, thread_id, **kwargs):
                self.list_turns_calls.append((thread_id, kwargs))
                return [{"id": "turn-terminal", "status": "interrupted"}]

        manager = CodexManager()
        child_state = {
            "session_id": "idle-chat",
            "run_id": "parent-run",
            "subagent_status": "running",
        }
        with patch.object(hyadex_server.PROVIDERS, "codex_manager", manager), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", None), \
             patch.object(hyadex_server.STORE, "sessions", {}), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(hyadex_server.RUNS, "active", {}), \
             patch.object(hyadex_server.RUNS, "current_turns", {}), \
             patch.object(hyadex_server, "CODEX_NATIVE_ACTION_TASKS", {}), \
             patch.object(hyadex_server, "CODEX_PENDING_INTERACTIONS", {}), \
             patch.object(
                 hyadex_server,
                 "CODEX_SUBAGENT_STATE",
                 {"child-thread": child_state},
             ), \
             patch.object(
                 hyadex_server,
                 "CODEX_SUBAGENT_SESSION_INDEX",
                 {"child-thread": "idle-chat"},
             ), \
             patch.object(
                 hyadex_server,
                 "CODEX_SUBAGENT_LIVE_GENERATIONS",
                 {"child-thread": manager.generation},
             ):
            snapshot = await hyadex_server.prepare_provider_background_work_snapshot()
            labels = hyadex_server.provider_background_work_labels_from_snapshot(
                snapshot
            )

        self.assertEqual(labels, [])
        self.assertEqual(
            manager.list_turns_calls,
            [(
                "child-thread",
                {
                    "limit": 1,
                    "items_view": "summary",
                    "sort_direction": "desc",
                },
            )],
        )

    async def test_terminal_native_turn_retires_stale_local_turn_handle(self):
        class CodexManager:
            ready = True
            generation = 20

            @staticmethod
            def active_turn(_thread_id):
                return SimpleNamespace(turn_id="turn-terminal")

            @staticmethod
            async def list_turns(_thread_id, **_kwargs):
                return [{"id": "turn-terminal", "status": "completed"}]

        manager = CodexManager()
        with patch.object(hyadex_server.PROVIDERS, "codex_manager", manager), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", None), \
             patch.object(hyadex_server.STORE, "sessions", {}), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(hyadex_server.RUNS, "active", {}), \
             patch.object(hyadex_server.RUNS, "current_turns", {}), \
             patch.object(hyadex_server, "CODEX_NATIVE_ACTION_TASKS", {}), \
             patch.object(hyadex_server, "CODEX_PENDING_INTERACTIONS", {}), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_STATE", {
                 "child-thread": {
                     "session_id": "idle-chat",
                     "subagent_status": "running",
                 },
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_SESSION_INDEX", {
                 "child-thread": "idle-chat",
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_LIVE_GENERATIONS", {}):
            snapshot = await hyadex_server.prepare_provider_background_work_snapshot()
            labels = hyadex_server.provider_background_work_labels_from_snapshot(
                snapshot
            )

        self.assertEqual(labels, [])

    async def test_codex_terminal_scan_batches_make_forward_progress(self):
        class CodexManager:
            ready = True
            generation = 21

            def __init__(self):
                self.inspected = []

            @staticmethod
            def active_turn(_thread_id):
                return None

            async def list_turns(self, thread_id, **_kwargs):
                self.inspected.append(thread_id)
                return [{"id": f"turn-{thread_id}", "status": "completed"}]

        manager = CodexManager()
        states = {
            f"child-{index}": {
                "id": f"event-{index}",
                "session_id": "idle-chat",
                "subagent_status": "running",
            }
            for index in range(3)
        }
        generations = {
            thread_id: manager.generation for thread_id in states
        }
        with patch.object(hyadex_server.PROVIDERS, "codex_manager", manager), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", None), \
             patch.object(hyadex_server.STORE, "sessions", {}), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(hyadex_server.RUNS, "active", {}), \
             patch.object(hyadex_server.RUNS, "current_turns", {}), \
             patch.object(hyadex_server, "CODEX_NATIVE_ACTION_TASKS", {}), \
             patch.object(hyadex_server, "CODEX_PENDING_INTERACTIONS", {}), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_STATE", states), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_SESSION_INDEX", {}), \
             patch.object(
                 hyadex_server,
                 "CODEX_SUBAGENT_LIVE_GENERATIONS",
                 generations,
             ), \
             patch.object(
                 hyadex_server,
                 "CODEX_SUBAGENT_SCAN_LIMIT",
                 2,
             ):
            first = await hyadex_server.prepare_provider_background_work_snapshot()
            first_labels = hyadex_server.provider_background_work_labels_from_snapshot(
                first
            )
            second = await hyadex_server.prepare_provider_background_work_snapshot()
            second_labels = hyadex_server.provider_background_work_labels_from_snapshot(
                second
            )

        self.assertEqual(first_labels, ["Codex subagent child-2"])
        self.assertEqual(second_labels, [])
        self.assertEqual(manager.inspected, ["child-0", "child-1", "child-2"])

    async def test_codex_child_with_in_progress_native_turn_still_blocks(self):
        class CodexManager:
            ready = True
            generation = 18

            @staticmethod
            def active_turn(_thread_id):
                return None

            @staticmethod
            async def list_turns(_thread_id, **_kwargs):
                return [{"id": "turn-live", "status": "inProgress"}]

        manager = CodexManager()
        with patch.object(hyadex_server.PROVIDERS, "codex_manager", manager), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", None), \
             patch.object(hyadex_server.STORE, "sessions", {}), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(hyadex_server.RUNS, "active", {}), \
             patch.object(hyadex_server.RUNS, "current_turns", {}), \
             patch.object(hyadex_server, "CODEX_NATIVE_ACTION_TASKS", {}), \
             patch.object(hyadex_server, "CODEX_PENDING_INTERACTIONS", {}), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_STATE", {
                 "child-thread": {
                     "session_id": "idle-chat",
                     "subagent_status": "running",
                 },
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_SESSION_INDEX", {
                 "child-thread": "idle-chat",
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_LIVE_GENERATIONS", {
                 "child-thread": manager.generation,
             }):
            snapshot = await hyadex_server.prepare_provider_background_work_snapshot()
            labels = hyadex_server.provider_background_work_labels_from_snapshot(
                snapshot
            )

        self.assertEqual(labels, ["Codex subagent child-thread"])

    async def test_failed_codex_native_turn_inspection_keeps_child_blocking(self):
        class CodexManager:
            ready = True
            generation = 19

            @staticmethod
            def active_turn(_thread_id):
                return None

            @staticmethod
            async def list_turns(_thread_id, **_kwargs):
                raise RuntimeError("app-server unavailable")

        manager = CodexManager()
        with patch.object(hyadex_server.PROVIDERS, "codex_manager", manager), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", None), \
             patch.object(hyadex_server.STORE, "sessions", {}), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(hyadex_server.RUNS, "active", {}), \
             patch.object(hyadex_server.RUNS, "current_turns", {}), \
             patch.object(hyadex_server, "CODEX_NATIVE_ACTION_TASKS", {}), \
             patch.object(hyadex_server, "CODEX_PENDING_INTERACTIONS", {}), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_STATE", {
                 "child-thread": {
                     "session_id": "idle-chat",
                     "subagent_status": "running",
                 },
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_SESSION_INDEX", {
                 "child-thread": "idle-chat",
             }), \
             patch.object(hyadex_server, "CODEX_SUBAGENT_LIVE_GENERATIONS", {
                 "child-thread": manager.generation,
             }):
            snapshot = await hyadex_server.prepare_provider_background_work_snapshot()
            labels = hyadex_server.provider_background_work_labels_from_snapshot(
                snapshot
            )

        self.assertEqual(snapshot["codex"]["error"], "scan_failed")
        self.assertEqual(labels, ["Codex subagent child-thread"])

    def test_oversized_claude_events_fail_closed_without_folding(self):
        event_file = MagicMock()
        event_file.stat.return_value.st_size = (
            hyadex_server.MAX_CLAUDE_BACKGROUND_EVENT_SCAN_BYTES + 1
        )
        with patch.object(hyadex_server, "events_path", return_value=event_file), \
             patch.object(
                 hyadex_server,
                 "build_claude_subagent_snapshot",
             ) as fold:
            labels = hyadex_server.claude_background_work_labels_from_events(
                ("claude-chat",)
            )

        self.assertEqual(labels, ["Claude provider state too large in claude-chat"])
        fold.assert_not_called()

    async def test_loaded_claude_overflow_blocks_restart_and_worker_retirement(self):
        session_ids = [f"claude-{index:02d}" for index in range(33)]
        last_session_id = session_ids[-1]
        sessions = {
            session_id: {"backend": hyadex_server.BACKEND_CLAUDE}
            for session_id in session_ids
        }
        scanned: list[str] = []
        manager = MagicMock()
        manager.is_loaded.return_value = True

        def scan(session_id, *, limit):
            self.assertEqual(limit, 1)
            scanned.append(session_id)
            return {"active_count": int(session_id == last_session_id)}

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(hyadex_server, "active_codex_work_labels", return_value=[]), \
             patch.object(hyadex_server.PROVIDERS, "claude_sdk_manager", manager), \
             patch.object(hyadex_server.STORE, "sessions", sessions), \
             patch.object(hyadex_server.RUNS, "busy", set()), \
             patch.object(hyadex_server.RUNS, "maintenance", set()), \
             patch.object(
                 hyadex_server,
                 "events_path",
                 side_effect=lambda session_id: Path(temporary) / session_id,
             ), \
             patch.object(
                 hyadex_server,
                 "build_claude_subagent_snapshot",
                 side_effect=scan,
             ):
            provider_snapshot = (
                await hyadex_server.prepare_provider_background_work_snapshot()
            )
            labels = hyadex_server.provider_background_work_labels_from_snapshot(
                provider_snapshot
            )

        self.assertEqual(
            labels,
            [hyadex_server.CLAUDE_PROVIDER_INSPECTION_OVERFLOW_LABEL],
        )
        self.assertEqual(scanned, session_ids[:-1])
        self.assertNotIn(last_session_id, scanned)

        restart_snapshot = hyadex_server.server_restart_blocker_snapshot_locked(
            tmux_cgroup_state={},
            provider_background_work_labels=labels,
        )
        self.assertTrue(restart_snapshot["has_blockers"])
        self.assertEqual(restart_snapshot["provider_background_count"], 1)

        update_counts = hyadex_server.server_lifecycle_blocker_counts(
            [],
            0,
            labels,
            0,
        )
        self.assertTrue(any(update_counts.values()))
        self.assertEqual(update_counts["provider_background_tasks"], 1)







    def test_job_models_bound_title_and_prompt(self):
        with self.assertRaises(ValidationError):
            hyadex_server.CreateJobRequest(
                session_id="chat",
                title="t" * (hyadex_server.MAX_JOB_TITLE_CHARS + 1),
                prompt="run",
            )
        with self.assertRaises(ValidationError):
            hyadex_server.UpdateJobRequest(
                prompt="p" * (hyadex_server.MAX_JOB_PROMPT_CHARS + 1),
            )
        with self.assertRaises(HTTPException) as internal:
            hyadex_server.validate_job_text_bounds(
                "t" * (hyadex_server.MAX_JOB_TITLE_CHARS + 1),
                "run",
            )
        self.assertEqual(internal.exception.status_code, 400)

    async def test_job_registry_write_runs_off_event_loop(self):
        store = hyadex_server.JobStore()
        store.jobs = {"job": {"id": "job"}}
        loop_thread = threading.get_ident()
        write_threads: list[int] = []

        def record_write(jobs):
            self.assertIsNot(jobs, store.jobs)
            self.assertEqual(jobs, store.jobs)
            write_threads.append(threading.get_ident())

        with patch.object(
            hyadex_server,
            "write_jobs_registry",
            side_effect=record_write,
        ):
            await store.save()

        self.assertEqual(len(write_threads), 1)
        self.assertNotEqual(write_threads[0], loop_thread)

    async def test_cancelled_job_save_finishes_before_next_mutation(self):
        store = hyadex_server.JobStore()
        store.jobs = {"job": {"id": "job", "title": "initial"}}
        first_write_started = threading.Event()
        release_first_write = threading.Event()
        second_write_started = threading.Event()
        written_titles: list[str] = []

        def record_write(snapshot):
            title = snapshot["job"]["title"]
            self.assertIsNot(snapshot, store.jobs)
            if title == "first":
                first_write_started.set()
                self.assertTrue(release_first_write.wait(timeout=2))
            elif title == "second":
                second_write_started.set()
            written_titles.append(title)

        async def mutate_and_save(title: str) -> None:
            async with store._lock:
                store.jobs["job"]["title"] = title
                await store.save()

        with patch.object(
            hyadex_server,
            "write_jobs_registry",
            side_effect=record_write,
        ):
            first = asyncio.create_task(mutate_and_save("first"))
            self.assertTrue(
                await asyncio.to_thread(first_write_started.wait, 1)
            )
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())

            second = asyncio.create_task(mutate_and_save("second"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(second_write_started.is_set())
            self.assertEqual(store.jobs["job"]["title"], "first")

            release_first_write.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await asyncio.wait_for(second, timeout=1)

        self.assertEqual(written_titles, ["first", "second"])
        self.assertEqual(store.jobs["job"]["title"], "second")
        self.assertEqual(store._last_saved_sequence, 2)

    async def test_cancelled_job_create_retains_committed_memory_state(self):
        store = hyadex_server.JobStore()
        first_write_started = threading.Event()
        release_first_write = threading.Event()
        real_write = hyadex_server.write_jobs_registry

        def blocked_write(snapshot):
            first_write_started.set()
            self.assertTrue(release_first_write.wait(timeout=2))
            real_write(snapshot)

        with tempfile.TemporaryDirectory() as temporary:
            jobs_path = Path(temporary) / "jobs.json"
            sessions = {
                "chat": {
                    "id": "chat",
                    "backend": hyadex_server.BACKEND_CODEX,
                    "archived": False,
                },
            }
            with (
                patch.object(hyadex_server, "JOBS_FILE", jobs_path),
                patch.object(hyadex_server.STORE, "sessions", sessions),
                patch.object(
                    hyadex_server,
                    "write_jobs_registry",
                    side_effect=blocked_write,
                ),
                patch.object(
                    hyadex_server,
                    "append_event",
                    new_callable=AsyncMock,
                ) as append_event,
            ):
                create_task = asyncio.create_task(store.create(
                    hyadex_server.CreateJobRequest(
                        session_id="chat",
                        title="Committed during cancellation",
                        prompt="Run once",
                    )
                ))
                try:
                    self.assertTrue(
                        await asyncio.to_thread(first_write_started.wait, 1)
                    )
                    create_task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(create_task.done())
                finally:
                    release_first_write.set()
                with self.assertRaises(asyncio.CancelledError):
                    await create_task

            persisted = json.loads(jobs_path.read_text(encoding="utf-8"))

        self.assertEqual(len(store.jobs), 1)
        self.assertEqual(persisted, store.jobs)
        self.assertEqual(
            next(iter(store.jobs.values()))["title"],
            "Committed during cancellation",
        )
        append_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

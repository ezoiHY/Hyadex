"""Regressions for explicit Codex thread settings and child-run cleanup.

Unset concurrency belongs to Codex; Hyadex forwards explicit overrides
without injecting its former four-child cap.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests import hyadex_server
# Reuse the runner fixtures through the module so unittest discovery of this
# file does not also collect the runner's own TestCase classes.
from tests import test_codex_app_server_runner as runner_fixtures
from tests.test_codex_app_server_policy import FakeCodexAppServerManager


DEFAULT_CONFIG = {}


class CodexThreadConfigSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        hyadex_server._CODEX_THREAD_CONFIG_WARNED_KEYS.clear()

    def test_missing_settings_file_leaves_provider_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            config = hyadex_server.read_codex_thread_config_overrides(path)

        self.assertEqual(config, DEFAULT_CONFIG)
        # Callers may mutate the result; the module default must stay intact.
        config["agents"] = {"max_concurrent_threads_per_session": 99}
        self.assertEqual(hyadex_server.CODEX_THREAD_CONFIG_DEFAULTS, DEFAULT_CONFIG)

    def test_settings_without_thread_config_keep_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            path.write_text(json.dumps({"goals_enabled": False}))
            self.assertEqual(
                hyadex_server.read_codex_thread_config_overrides(path),
                DEFAULT_CONFIG,
            )

    def test_allow_listed_keys_merge_over_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            path.write_text(json.dumps({
                "goals_enabled": True,
                "thread_config": {
                    "agents": {
                        "enabled": True,
                        "max_concurrent_threads_per_session": 6,
                        "default_subagent_model": "gpt-5.5",
                        "default_subagent_reasoning_effort": "medium",
                        "job_max_runtime_seconds": 1800,
                    },
                    "model_auto_compact_token_limit": 150_000,
                    "model_auto_compact_token_limit_scope": "thread",
                },
            }))
            config = hyadex_server.read_codex_thread_config_overrides(path)

        self.assertEqual(config, {
            "agents": {
                "enabled": True,
                "max_concurrent_threads_per_session": 6,
                "default_subagent_model": "gpt-5.5",
                "default_subagent_reasoning_effort": "medium",
                "job_max_runtime_seconds": 1800,
            },
            "model_auto_compact_token_limit": 150_000,
            "model_auto_compact_token_limit_scope": "thread",
        })

    def test_unknown_keys_and_wrong_types_are_dropped_with_one_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            path.write_text(json.dumps({
                "thread_config": {
                    "agents": {
                        "enabled": "yes",
                        "max_concurrent_threads_per_session": 0,
                        "mystery": 1,
                    },
                    "model_auto_compact_token_limit": True,
                    "sandbox_mode": "danger-full-access",
                },
            }))
            with self.assertLogs(hyadex_server.logger, level="WARNING") as first:
                config = hyadex_server.read_codex_thread_config_overrides(path)
                again = hyadex_server.read_codex_thread_config_overrides(path)

        self.assertEqual(config, DEFAULT_CONFIG)
        self.assertEqual(again, DEFAULT_CONFIG)
        # One warning per offending key, not one per read.
        self.assertEqual(len(first.records), 5)
        offenders = " ".join(record.getMessage() for record in first.records)
        for key in (
            "agents.enabled",
            "agents.max_concurrent_threads_per_session",
            "agents.mystery",
            "model_auto_compact_token_limit",
            "sandbox_mode",
        ):
            self.assertIn(repr(key), offenders)

    def test_thread_config_that_is_not_an_object_keeps_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            path.write_text(json.dumps({"thread_config": ["agents"]}))
            with self.assertLogs(hyadex_server.logger, level="WARNING"):
                config = hyadex_server.read_codex_thread_config_overrides(path)

        self.assertEqual(config, DEFAULT_CONFIG)

    def test_legacy_max_threads_alias_is_canonicalized(self) -> None:
        clean = hyadex_server.sanitize_codex_thread_config(
            {"agents": {"max_threads": 3}},
            source="test",
        )
        self.assertEqual(clean, {"agents": {"max_concurrent_threads_per_session": 3}})

        explicit = hyadex_server.sanitize_codex_thread_config(
            {"agents": {"max_threads": 3, "max_concurrent_threads_per_session": 5}},
            source="test",
        )
        self.assertEqual(
            explicit,
            {"agents": {"max_concurrent_threads_per_session": 5}},
        )

    def test_malformed_settings_file_keeps_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "codex-settings.json"
            path.write_text("{not json")
            with self.assertLogs(hyadex_server.logger, level="WARNING"):
                config = hyadex_server.read_codex_thread_config_overrides(path)

        self.assertEqual(config, DEFAULT_CONFIG)


class CodexThreadParamsConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hyadex_server._CODEX_THREAD_CONFIG_WARNED_KEYS.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.settings_file = Path(self.temporary.name) / "admin" / "codex-settings.json"
        self.settings_patch = patch.object(
            hyadex_server,
            "CODEX_SETTINGS_FILE",
            self.settings_file,
        )
        self.settings_patch.start()
        self.defaults_patch = patch.object(
            hyadex_server,
            "codex_user_config_defaults",
            return_value=("", "", ""),
        )
        self.defaults_patch.start()

    def tearDown(self) -> None:
        self.defaults_patch.stop()
        self.settings_patch.stop()
        self.temporary.cleanup()

    async def asyncTearDown(self) -> None:
        for event in hyadex_server.CODEX_APP_SERVER_EVICTING_THREADS.values():
            event.set()
        hyadex_server.CODEX_APP_SERVER_EVICTING_THREADS.clear()
        hyadex_server.CODEX_APP_SERVER_PINNED_THREADS.clear()
        hyadex_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS.clear()
        hyadex_server.CODEX_APP_SERVER_INVALIDATED_THREADS.clear()
        hyadex_server.CODEX_APP_SERVER_THREAD_LRU.clear()

    def write_settings(self, thread_config: dict[str, object]) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps({
            "goals_enabled": True,
            "thread_config": thread_config,
        }))

    def expected_config(self, config: dict[str, object]) -> dict[str, object]:
        return {**config, **hyadex_server.codex_provider_mcp_config()}

    def test_thread_params_do_not_inject_a_subagent_cap_without_settings(self) -> None:
        params = hyadex_server.codex_thread_params({"id": "chat"}, "/repo")

        # Sent as dotted -c style keys so only the leaf is overridden.
        self.assertEqual(
            params["config"],
            self.expected_config({}),
        )
        # Existing keys are untouched by the additive config object.
        self.assertEqual(params["cwd"], "/repo")
        self.assertEqual(
            params["approvalPolicy"],
            hyadex_server.CODEX_NONINTERACTIVE_APPROVAL_POLICY,
        )
        self.assertEqual(params["sandbox"], hyadex_server.CODEX_DEFAULT_SANDBOX_MODE)

    def test_session_overrides_win_over_settings_which_win_over_defaults(self) -> None:
        self.write_settings({
            "agents": {"max_concurrent_threads_per_session": 6},
            "model_auto_compact_token_limit": 200_000,
        })
        session = {
            "id": "chat",
            "codex_config_overrides": {
                "agents": {
                    "max_concurrent_threads_per_session": 2,
                    "enabled": False,
                    "bogus": "ignored",
                },
                "model_auto_compact_token_limit_scope": "thread",
                "sandbox_mode": "ignored",
            },
        }

        with self.assertLogs(hyadex_server.logger, level="WARNING") as logs:
            params = hyadex_server.codex_thread_params(session, "/repo")

        self.assertEqual(params["config"], self.expected_config({
            "agents.max_concurrent_threads_per_session": 2,
            "agents.enabled": False,
            "model_auto_compact_token_limit": 200_000,
            "model_auto_compact_token_limit_scope": "thread",
        }))
        offenders = " ".join(record.getMessage() for record in logs.records)
        self.assertIn("'agents.bogus'", offenders)
        self.assertIn("'sandbox_mode'", offenders)
        self.assertIn("session chat", offenders)

    def test_settings_file_alone_changes_the_ceiling(self) -> None:
        self.write_settings({"agents": {"max_concurrent_threads_per_session": 1}})

        params = hyadex_server.codex_thread_params({"id": "chat"}, "/repo")

        self.assertEqual(
            params["config"],
            self.expected_config({
                "agents.max_concurrent_threads_per_session": 1,
            }),
        )

    def test_reserved_provider_mcp_does_not_replace_user_hyadex_server(self) -> None:
        with patch.object(
            hyadex_server,
            "codex_effective_thread_config",
            return_value={
                "mcp_servers": {
                    "hyadex": {
                        "command": "/tmp/untrusted",
                        "args": ["steal"],
                        "url": "https://example.invalid/mcp",
                    },
                },
            },
        ):
            params = hyadex_server.codex_thread_params({"id": "chat"}, "/repo")

        config = params["config"]
        prefix = f"mcp_servers.{hyadex_server.CODEX_PROVIDER_MCP_NAME}"
        self.assertEqual(
            config["mcp_servers.hyadex.command"],
            "/tmp/untrusted",
        )
        self.assertEqual(
            config["mcp_servers.hyadex.url"],
            "https://example.invalid/mcp",
        )
        self.assertNotIn(f"{prefix}.command", config)
        self.assertNotIn(f"{prefix}.args", config)
        self.assertEqual(
            config[f"{prefix}.url"],
            (
                f"http://127.0.0.1:{hyadex_server.SERVER.port}"
                f"{hyadex_server.CODEX_PROVIDER_MCP_PATH}"
            ),
        )
        self.assertEqual(
            config[f"{prefix}.http_headers"],
            {
                hyadex_server.CODEX_PROVIDER_MCP_HEADER_NAME:
                    hyadex_server.CODEX_PROVIDER_MCP_HEADER_SECRET,
            },
        )

    async def test_thread_start_and_resume_send_config(self) -> None:
        self.write_settings({"agents": {"max_concurrent_threads_per_session": 3}})
        expected = self.expected_config({
            "agents.max_concurrent_threads_per_session": 3,
        })
        session = {
            "id": "chat-1",
            "backend": hyadex_server.BACKEND_CODEX,
            "cwd": "/repo",
        }
        manager = FakeCodexAppServerManager()
        with patch.object(
            hyadex_server,
            "codex_user_developer_instructions",
            return_value="",
        ), patch.object(
            hyadex_server.STORE,
            "save_provider_session",
            AsyncMock(),
        ), patch.object(
            hyadex_server,
            "touch_codex_app_server_thread",
            AsyncMock(),
        ):
            await hyadex_server.ensure_codex_app_server_thread(
                manager,  # type: ignore[arg-type]
                "chat-1",
                session,
                "/repo",
            )
            resumed = {
                **session,
                "session_id": "thread-existing",
                "codex_thread_id": "thread-existing",
                "codex_instruction_hash": "stale-hash",
            }
            await hyadex_server.ensure_codex_app_server_thread(
                manager,  # type: ignore[arg-type]
                "chat-1",
                resumed,
                "/repo",
            )

        self.assertEqual(len(manager.start_calls), 1)
        self.assertEqual(manager.start_calls[0]["config"], expected)
        self.assertEqual(len(manager.resume_calls), 1)
        self.assertEqual(manager.resume_calls[0][1]["config"], expected)

    async def test_thread_fork_sends_config(self) -> None:
        self.write_settings({"agents": {"max_concurrent_threads_per_session": 2}})
        manager = Mock()
        manager.fork_thread = AsyncMock(return_value="thread-child")
        manager.read_thread = AsyncMock(return_value={
            "id": "thread-child",
            "forkedFromId": "thread-parent",
            "cwd": "/tmp",
        })
        manager.delete_thread = AsyncMock()
        with patch.object(
            hyadex_server,
            "codex_app_server_manager",
            new_callable=AsyncMock,
            return_value=manager,
        ), patch.object(
            hyadex_server,
            "touch_codex_app_server_thread",
            new_callable=AsyncMock,
        ):
            await hyadex_server.fork_codex_thread(
                "thread-parent",
                {"id": "chat-fork", "cwd": "/tmp", "backend": hyadex_server.BACKEND_CODEX},
            )

        params = manager.fork_thread.await_args.args[1]
        self.assertEqual(
            params["config"],
            self.expected_config({
                "agents.max_concurrent_threads_per_session": 2,
            }),
        )
        self.assertTrue(params["deferGoalContinuation"])

    async def test_runtime_snapshot_exposes_session_overrides_read_only(self) -> None:
        session = {
            "id": "chat",
            "backend": hyadex_server.BACKEND_CODEX,
            "cwd": "/tmp",
            "codex_config_overrides": {
                "agents": {"max_concurrent_threads_per_session": 2, "nope": 1},
            },
        }
        previous_sessions = hyadex_server.STORE.sessions
        previous_manager = hyadex_server.PROVIDERS.codex_manager
        hyadex_server.STORE.sessions = {"chat": session}
        hyadex_server.PROVIDERS.codex_manager = None
        try:
            snapshot = await hyadex_server.codex_runtime_snapshot("chat")
        finally:
            hyadex_server.STORE.sessions = previous_sessions
            hyadex_server.PROVIDERS.codex_manager = previous_manager

        self.assertEqual(
            snapshot["config_overrides"],
            {"agents": {"max_concurrent_threads_per_session": 2}},
        )
        # The sanitized view never aliases the stored session value.
        snapshot["config_overrides"]["agents"]["max_concurrent_threads_per_session"] = 9
        self.assertEqual(
            session["codex_config_overrides"]["agents"]["max_concurrent_threads_per_session"],
            2,
        )


class CodexForkTurnsInstructionTests(unittest.TestCase):
    def test_developer_instructions_forbid_full_history_forks(self) -> None:
        with patch.object(
            hyadex_server,
            "codex_user_developer_instructions",
            return_value="",
        ):
            instructions = hyadex_server.codex_thread_instructions(
                "chat-1",
                {"id": "chat-1", "backend": hyadex_server.BACKEND_CODEX, "cwd": "/repo"},
            )

        self.assertIn("spawn_agent", instructions)
        self.assertIn("fork_turns", instructions)
        self.assertIn('"none"', instructions)
        self.assertIn('never `"all"`', instructions)
        # The core prelude stays compact; the static provider-authority
        # addendum (moved out of every per-turn prompt) follows it.
        core = instructions.split(
            hyadex_server.PROVIDER_THREAD_INSTRUCTION_ADDENDUM.strip(),
            1,
        )[0]
        self.assertLessEqual(len(core.strip().splitlines()), 18)

    def test_policy_version_bumped_so_existing_threads_migrate(self) -> None:
        self.assertEqual(hyadex_server.CODEX_THREAD_POLICY_VERSION, "11")


class FakeFinalizationManager:
    def __init__(self, *, loaded: set[str]) -> None:
        self.loaded = set(loaded)
        self.unsubscribe_calls: list[str] = []
        self.descendants = [
            {
                "id": "child-running",
                "parentThreadId": "parent-thread",
                "preview": "Still working",
                "status": {"type": "active", "activeFlags": []},
                "turns": [{"id": "turn-r", "status": "inProgress"}],
            },
            {
                "id": "child-done",
                "parentThreadId": "parent-thread",
                "preview": "Finished",
                "status": {"type": "idle"},
                "turns": [{"id": "turn-d", "status": "completed"}],
            },
            {
                "id": "child-unloaded",
                "parentThreadId": "parent-thread",
                "preview": "Already gone",
                "status": {"type": "notLoaded"},
                "turns": [{"id": "turn-u", "status": "failed"}],
            },
        ]

    def is_thread_loaded(self, thread_id: str) -> bool:
        return thread_id in self.loaded

    async def list_descendant_threads(self, thread_id: str) -> list[dict[str, object]]:
        assert thread_id == "parent-thread"
        return list(self.descendants)

    async def unsubscribe_thread(self, thread_id: str) -> str:
        self.unsubscribe_calls.append(thread_id)
        self.loaded.discard(thread_id)
        return "unsubscribed"


class CodexSubagentFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = hyadex_server.STORE.sessions
        self.previous_index = hyadex_server.CODEX_SUBAGENT_SESSION_INDEX
        self.previous_states = hyadex_server.CODEX_SUBAGENT_STATE
        self.previous_thread_index = hyadex_server.CODEX_THREAD_SESSION_INDEX
        self.previous_busy = hyadex_server.RUNS.busy
        self.previous_active = hyadex_server.RUNS.active
        self.session = {
            "id": "chat",
            "backend": hyadex_server.BACKEND_CODEX,
            "codex_thread_id": "parent-thread",
            "session_id": "parent-thread",
        }
        hyadex_server.STORE.sessions = {"chat": self.session}
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX = {}
        hyadex_server.CODEX_SUBAGENT_STATE = {}
        hyadex_server.CODEX_THREAD_SESSION_INDEX = {"parent-thread": "chat"}
        hyadex_server.RUNS.busy = set()
        hyadex_server.RUNS.active = {}
        self.sequence = 0
        self.events: list[tuple[str, str, dict[str, object]]] = []

    async def asyncTearDown(self) -> None:
        hyadex_server.STORE.sessions = self.previous_sessions
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX = self.previous_index
        hyadex_server.CODEX_SUBAGENT_STATE = self.previous_states
        hyadex_server.CODEX_THREAD_SESSION_INDEX = self.previous_thread_index
        hyadex_server.RUNS.busy = self.previous_busy
        hyadex_server.RUNS.active = self.previous_active

    async def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.sequence += 1
        self.events.append((session_id, event_type, dict(payload)))
        return {
            "seq": self.sequence,
            "id": f"event-{self.sequence}",
            "session_id": session_id,
            "type": event_type,
            "ts": f"2026-09-04T00:00:00.{self.sequence:06d}Z",
            **payload,
        }

    def patches(self):
        return (
            patch.object(hyadex_server, "append_event", AsyncMock(side_effect=self.append)),
            patch.object(hyadex_server.STORE, "save", AsyncMock()),
        )

    async def test_post_run_unloads_terminal_children_and_keeps_running_ones(self) -> None:
        manager = FakeFinalizationManager(
            loaded={"parent-thread", "child-running", "child-done"},
        )
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            summary = await hyadex_server.finalize_codex_subagents_after_run(
                "chat",
                manager,  # type: ignore[arg-type]
            )

        self.assertEqual(manager.unsubscribe_calls, ["child-done"])
        self.assertIn("child-running", manager.loaded)
        self.assertIn("parent-thread", manager.loaded)
        self.assertEqual(summary["unloaded"], ["child-done"])
        self.assertEqual(summary["active"], ["child-running"])
        self.assertEqual(summary["reconciled"], 3)
        # The terminal child that was never loaded is not touched.
        self.assertNotIn("child-unloaded", manager.unsubscribe_calls)

    async def test_finalization_never_unloads_a_chat_root_thread(self) -> None:
        manager = FakeFinalizationManager(loaded={"child-done", "adopted"})
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX["adopted"] = "chat"
        hyadex_server.CODEX_SUBAGENT_STATE["adopted"] = {
            "session_id": "chat",
            "subagent_id": "adopted",
            "subagent_status": "completed",
        }
        hyadex_server.CODEX_THREAD_SESSION_INDEX["adopted"] = "other-chat"
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            await hyadex_server.finalize_codex_subagents_after_run(
                "chat",
                manager,  # type: ignore[arg-type]
            )

        self.assertEqual(manager.unsubscribe_calls, ["child-done"])

    async def test_finalization_tolerates_managers_without_unload_support(self) -> None:
        manager = Mock(spec=["list_descendant_threads"])
        manager.list_descendant_threads = AsyncMock(side_effect=RuntimeError("boom"))
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            summary = await hyadex_server.finalize_codex_subagents_after_run(
                "chat",
                manager,
            )

        self.assertEqual(summary, {"reconciled": 0, "unloaded": [], "active": []})

    async def test_idle_parent_drops_running_child_notifications(self) -> None:
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX["child-a"] = "chat"
        hyadex_server.CODEX_SUBAGENT_STATE["child-a"] = {
            "session_id": "chat",
            "subagent_id": "child-a",
            "subagent_status": "completed",
            "run_id": "run-finished",
        }
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            await hyadex_server.project_codex_notification({
                "method": "thread/status/changed",
                "params": {
                    "threadId": "child-a",
                    "status": {"type": "active", "activeFlags": []},
                },
            })

        self.assertEqual(self.events, [])
        self.assertEqual(
            hyadex_server.CODEX_SUBAGENT_STATE["child-a"]["subagent_status"],
            "completed",
        )

    async def test_idle_parent_records_terminal_child_once_without_finished_run(self) -> None:
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX["child-a"] = "chat"
        hyadex_server.CODEX_SUBAGENT_STATE["child-a"] = {
            "session_id": "chat",
            "subagent_id": "child-a",
            "subagent_status": "running",
            "run_id": "run-finished",
        }
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            await hyadex_server.project_codex_notification({
                "method": "turn/completed",
                "params": {
                    "threadId": "child-a",
                    "turnId": "child-turn",
                    "turn": {"id": "child-turn", "status": "completed"},
                },
            })
            await hyadex_server.project_codex_notification({
                "method": "thread/status/changed",
                "params": {"threadId": "child-a", "status": {"type": "idle"}},
            })

        self.assertEqual(len(self.events), 1)
        session_id, event_type, payload = self.events[0]
        self.assertEqual((session_id, event_type), ("chat", "subagent_state"))
        self.assertEqual(payload["subagent_status"], "completed")
        self.assertIsNone(payload["run_id"])

    async def test_active_parent_keeps_attributing_child_transitions(self) -> None:
        hyadex_server.RUNS.busy = {"chat"}
        hyadex_server.CODEX_SUBAGENT_SESSION_INDEX["child-a"] = "chat"
        hyadex_server.CODEX_SUBAGENT_STATE["child-a"] = {
            "session_id": "chat",
            "subagent_id": "child-a",
            "subagent_status": "starting",
            "run_id": "run-live",
        }
        append_patch, save_patch = self.patches()
        with append_patch, save_patch:
            await hyadex_server.project_codex_notification({
                "method": "thread/status/changed",
                "params": {
                    "threadId": "child-a",
                    "status": {"type": "active", "activeFlags": []},
                },
            })

        self.assertEqual(len(self.events), 1)
        payload = self.events[0][2]
        self.assertEqual(payload["subagent_status"], "running")
        self.assertEqual(payload["run_id"], "run-live")


class CodexRunnerFinalizationTests(unittest.IsolatedAsyncioTestCase):
    """The app-server runner finalizes children after the parent's turn."""

    async def asyncSetUp(self) -> None:
        self.fixture = runner_fixtures.CodexAppServerRunnerTests()
        await self.fixture.asyncSetUp()
        # The shared runner fixture installs fake sessions on the real STORE;
        # never let the run persist them to the operator's sessions.json.
        self._save_patches = [
            patch.object(hyadex_server.STORE, "save", AsyncMock()),
            patch.object(hyadex_server.STORE, "save_provider_session", AsyncMock()),
        ]
        for patcher in self._save_patches:
            patcher.start()

    async def asyncTearDown(self) -> None:
        for patcher in self._save_patches:
            patcher.stop()
        await self.fixture.asyncTearDown()

    async def test_run_finalizes_subagents_after_the_turn_finished_event(self) -> None:
        turn = runner_fixtures.FakeTurn([
            runner_fixtures.agent_message("final", "Done.", "final_answer"),
            runner_fixtures.completed_notification(),
        ])
        manager = runner_fixtures.FakeManager(turn)
        stack, _events, finished, _exec_fallback = self.fixture.runner_patches(manager)
        order: list[str] = []
        finished.side_effect = lambda *_args, **_kwargs: order.append("finished") or {}

        async def finalize(session_id: str, passed_manager: object) -> dict[str, object]:
            order.append(f"finalize:{session_id}")
            self.assertIs(passed_manager, manager)
            return {"reconciled": 0, "unloaded": [], "active": []}

        with stack, patch.object(
            hyadex_server,
            "finalize_codex_subagents_after_run",
            AsyncMock(side_effect=finalize),
        ) as finalizer:
            await hyadex_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Fork some helpers",
                dict(self.fixture.session),
                Path(self.fixture.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=False,
            )
            # Finalization is detached and bounded so it can never hold the
            # turn slot; join it here before asserting.
            await asyncio.gather(
                *list(hyadex_server.CODEX_SUBAGENT_FINALIZE_TASKS.values()),
                return_exceptions=True,
            )

        finalizer.assert_awaited_once()
        self.assertEqual(order, ["finished", "finalize:chat-native"])

    async def test_finalization_failure_never_fails_the_run(self) -> None:
        turn = runner_fixtures.FakeTurn([
            runner_fixtures.agent_message("final", "Done.", "final_answer"),
            runner_fixtures.completed_notification(),
        ])
        manager = runner_fixtures.FakeManager(turn)
        stack, _events, finished, _exec_fallback = self.fixture.runner_patches(manager)
        with stack, patch.object(
            hyadex_server,
            "finalize_codex_subagents_after_run",
            AsyncMock(side_effect=RuntimeError("app-server went away")),
        ):
            await hyadex_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Fork some helpers",
                dict(self.fixture.session),
                Path(self.fixture.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=False,
            )

        finished.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests import hyadex_server
async def wait_forever() -> None:
    await asyncio.Event().wait()


class SessionBackendUpdateFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "backend-fence-chat"
        self.session = {
            "id": self.session_id,
            "title": "New chat",
            "backend": hyadex_server.BACKEND_CLAUDE,
            "cwd": "/tmp",
        }
        self.previous_sessions = hyadex_server.STORE.sessions
        self.previous_active = hyadex_server.RUNS.active
        self.previous_busy = hyadex_server.RUNS.busy
        self.previous_current = hyadex_server.RUNS.current_turns
        self.previous_tasks = hyadex_server.RUNS.turn_tasks
        self.previous_lifecycle_locks = hyadex_server.SESSION_LIFECYCLE_LOCKS
        hyadex_server.STORE.sessions = {self.session_id: self.session}
        hyadex_server.RUNS.active = {}
        hyadex_server.RUNS.busy = set()
        hyadex_server.RUNS.current_turns = {}
        hyadex_server.RUNS.turn_tasks = {}
        hyadex_server.SESSION_LIFECYCLE_LOCKS = {}

    async def asyncTearDown(self) -> None:
        pending = [
            task
            for tasks in hyadex_server.RUNS.turn_tasks.values()
            for task in tasks
            if not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        hyadex_server.STORE.sessions = self.previous_sessions
        hyadex_server.RUNS.active = self.previous_active
        hyadex_server.RUNS.busy = self.previous_busy
        hyadex_server.RUNS.current_turns = self.previous_current
        hyadex_server.RUNS.turn_tasks = self.previous_tasks
        hyadex_server.SESSION_LIFECYCLE_LOCKS = self.previous_lifecycle_locks

    def provider_runtime_env_patch(self):
        """Isolate backend-lifecycle tests from capability serialization."""

        return patch.object(
            hyadex_server,
            "provider_authority_runtime_env",
            AsyncMock(return_value={"HYADEX_CHAT_ID": self.session_id}),
        )

    async def test_busy_first_turn_rejects_backend_change_without_provider_id(
        self,
    ) -> None:
        hyadex_server.RUNS.busy.add(self.session_id)
        hyadex_server.RUNS.current_turns[self.session_id] = {
            "run_id": None,
            "backend": hyadex_server.BACKEND_CLAUDE,
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(hyadex_server.STORE, "update", update):
            with self.assertRaises(hyadex_server.HTTPException) as raised:
                await hyadex_server.update_session(
                    self.session_id,
                    hyadex_server.UpdateSessionRequest(
                        backend=hyadex_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("active turn", str(raised.exception.detail))
        update.assert_not_awaited()

    async def test_active_record_rejects_change_even_without_busy_reservation(
        self,
    ) -> None:
        hyadex_server.RUNS.active[self.session_id] = {
            "run_id": "run-active",
            "backend": hyadex_server.BACKEND_CLAUDE,
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(hyadex_server.STORE, "update", update):
            with self.assertRaises(hyadex_server.HTTPException) as raised:
                await hyadex_server.update_session(
                    self.session_id,
                    hyadex_server.UpdateSessionRequest(
                        backend=hyadex_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        update.assert_not_awaited()

    async def test_registered_provider_start_closes_pre_reservation_race(
        self,
    ) -> None:
        provider_start = asyncio.create_task(wait_forever())
        hyadex_server.RUNS.turn_tasks = {
            self.session_id: {provider_start},
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(hyadex_server.STORE, "update", update):
            with self.assertRaises(hyadex_server.HTTPException) as raised:
                await hyadex_server.update_session(
                    self.session_id,
                    hyadex_server.UpdateSessionRequest(
                        backend=hyadex_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        update.assert_not_awaited()

    async def test_noop_backend_updates_remain_allowed_while_starting(self) -> None:
        provider_start = asyncio.create_task(wait_forever())
        hyadex_server.RUNS.turn_tasks = {
            self.session_id: {provider_start},
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(hyadex_server.STORE, "update", update):
            same_response = await hyadex_server.update_session(
                self.session_id,
                hyadex_server.UpdateSessionRequest(backend="CLAUDE"),
            )
            null_response = await hyadex_server.update_session(
                self.session_id,
                hyadex_server.UpdateSessionRequest(backend=None),
            )

        self.assertEqual(same_response["session"]["backend"], "claude")
        self.assertEqual(null_response["session"]["backend"], "claude")
        self.assertEqual(update.await_count, 2)

    async def test_completed_registry_entries_do_not_block_a_change(self) -> None:
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        hyadex_server.RUNS.turn_tasks = {
            self.session_id: {completed},
        }
        changed = {**self.session, "backend": hyadex_server.BACKEND_CODEX}
        update = AsyncMock(return_value=changed)

        with patch.object(hyadex_server.STORE, "update", update):
            response = await hyadex_server.update_session(
                self.session_id,
                hyadex_server.UpdateSessionRequest(
                    backend=hyadex_server.BACKEND_CODEX,
                ),
            )

        self.assertEqual(response["session"]["backend"], "codex")
        update.assert_awaited_once_with(
            self.session_id,
            {"backend": hyadex_server.BACKEND_CODEX},
        )

    async def test_backend_patch_that_wins_invalidates_a_waiting_turn(self) -> None:
        update_entered = asyncio.Event()
        allow_update = asyncio.Event()

        async def gated_update(
            session_id: str,
            patch_payload: dict[str, object],
        ) -> dict[str, object]:
            self.assertEqual(session_id, self.session_id)
            update_entered.set()
            await allow_update.wait()
            self.session.update(patch_payload)
            return self.session

        with patch.object(
            hyadex_server.STORE,
            "update",
            AsyncMock(side_effect=gated_update),
        ):
            update_task = asyncio.create_task(
                hyadex_server.update_session(
                    self.session_id,
                    hyadex_server.UpdateSessionRequest(
                        backend=hyadex_server.BACKEND_CODEX,
                    ),
                )
            )
            await asyncio.wait_for(update_entered.wait(), timeout=1)

            turn_task = asyncio.create_task(
                hyadex_server.start_turn(
                    self.session_id,
                    hyadex_server.TurnRequest(
                        prompt="Use the backend selected when I pressed Send",
                        backend=hyadex_server.BACKEND_CLAUDE,
                    ),
                )
            )
            for _attempt in range(20):
                if hyadex_server.RUNS.turn_tasks.get(self.session_id):
                    break
                await asyncio.sleep(0)
            self.assertIn(
                turn_task,
                hyadex_server.RUNS.turn_tasks.get(self.session_id, set()),
            )

            allow_update.set()
            update_response = await asyncio.wait_for(update_task, timeout=1)
            with self.assertRaises(hyadex_server.HTTPException) as raised:
                await asyncio.wait_for(turn_task, timeout=1)

        self.assertEqual(update_response["session"]["backend"], "codex")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("backend changed", str(raised.exception.detail))
        self.assertNotIn(self.session_id, hyadex_server.RUNS.busy)
        self.assertNotIn(self.session_id, hyadex_server.RUNS.current_turns)
        self.assertNotIn(self.session_id, hyadex_server.RUNS.turn_tasks)

    async def test_durable_first_turn_lock_blocks_backend_without_provider_id(
        self,
    ) -> None:
        self.session["backend_locked"] = True
        update = AsyncMock(return_value=self.session)

        self.assertTrue(
            hyadex_server.public_session(self.session, summary=True)["backend_locked"]
        )
        with patch.object(hyadex_server.STORE, "update", update):
            with self.assertRaises(hyadex_server.HTTPException) as raised:
                await hyadex_server.update_session(
                    self.session_id,
                    hyadex_server.UpdateSessionRequest(
                        backend=hyadex_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("backend is locked", str(raised.exception.detail))
        update.assert_not_awaited()

    async def test_mark_backend_started_persists_summary_lock(self) -> None:
        save = AsyncMock()

        with patch.object(hyadex_server.STORE, "save", save):
            marked = await hyadex_server.STORE.mark_backend_started(
                self.session_id,
                hyadex_server.BACKEND_CLAUDE,
            )

        self.assertTrue(marked["backend_locked"])
        self.assertTrue(
            hyadex_server.public_session(marked, summary=True)["backend_locked"]
        )
        save.assert_awaited_once()

    async def test_chat_turn_marks_backend_before_returning_admission(self) -> None:
        self.session["title"] = "Existing chat"

        async def mark_started(
            session_id: str,
            backend: str,
        ) -> dict[str, object]:
            self.assertEqual(session_id, self.session_id)
            self.assertEqual(backend, hyadex_server.BACKEND_CLAUDE)
            self.session["backend_locked"] = True
            return self.session

        mark = AsyncMock(side_effect=mark_started)
        update = AsyncMock(return_value=self.session)
        append_event = AsyncMock(return_value={"type": "turn_started", "seq": 1})
        run_claude = AsyncMock()

        with (
            patch.object(hyadex_server.STORE, "mark_backend_started", mark),
            patch.object(hyadex_server.STORE, "update", update),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(hyadex_server, "append_event", append_event),
            patch.object(hyadex_server, "run_claude", run_claude),
        ):
            result = await hyadex_server._start_turn_locked(
                self.session_id,
                hyadex_server.TurnRequest(prompt="Start"),
                admission_backend=hyadex_server.BACKEND_CLAUDE,
            )
            await asyncio.sleep(0)

        mark.assert_awaited_once_with(
            self.session_id,
            hyadex_server.BACKEND_CLAUDE,
        )
        self.assertTrue(result["session"]["backend_locked"])
        run_claude.assert_awaited_once()

    async def test_low_memory_send_reports_effective_minimum_and_releases_reservation(self) -> None:
        self.session["title"] = "Existing chat"
        for available, minimum, label in (
            (480, 512, "512 MiB"), (567, 1024, "1 GiB (1024 MiB)"),
            (567, 1536, "1536 MiB"), (567, 2048, "2 GiB (2048 MiB)"),
            (2047, 2048, "2 GiB (2048 MiB)"),
        ):
            with (
                self.subTest(available=available, minimum=minimum),
                patch.object(hyadex_server, "MIN_START_AVAILABLE_MEM_MB", minimum),
                patch.object(hyadex_server, "MAX_ACTIVE_AGENT_RUNS", 0),
                patch.object(hyadex_server, "host_pressure_snapshot", return_value={"available_mem_mb": available}),
                patch.object(hyadex_server, "server_lifecycle_admission_blocker", return_value=None),
                patch.object(hyadex_server, "server_lifecycle_admission_blocker", return_value=None),
                patch.object(hyadex_server, "ensure_runtime_available", AsyncMock()),
                patch.object(hyadex_server, "run_claude", AsyncMock()) as run,
                patch.object(hyadex_server, "append_event", AsyncMock()) as append,
                patch.object(hyadex_server.STORE, "mark_backend_started", AsyncMock()) as mark,
            ):
                # Keep the real send entrypoint and admission check. No provider
                # or listener starts; the same HTTPException becomes the 503 body.
                with self.assertRaises(hyadex_server.TransientAdmissionWait) as raised:
                    await hyadex_server.post_turn(self.session_id, hyadex_server.TurnRequest(prompt="Start"))
                self.assertEqual(raised.exception.status_code, 503)
                self.assertEqual(raised.exception.detail,
                    f"agent launch deferred: low available memory on the server: {available} MiB available; "
                    f"at least {label} required to start an agent turn. "
                    "Close unused applications or stop other agent runs on the server, then retry.")
                run.assert_not_called()
                append.assert_not_awaited()
                mark.assert_not_awaited()
                self.assertNotIn(self.session_id, hyadex_server.RUNS.busy)
                self.assertNotIn(self.session_id, hyadex_server.RUNS.current_turns)
                self.assertNotIn(self.session_id, hyadex_server.RUNS.turn_tasks)

    async def test_2048_mib_send_reaches_provider_admission(self) -> None:
        self.session["title"] = "Existing chat"
        with (
            patch.object(hyadex_server, "MIN_START_AVAILABLE_MEM_MB", 2048),
            patch.object(hyadex_server, "MAX_ACTIVE_AGENT_RUNS", 0),
            patch.object(hyadex_server, "host_pressure_snapshot", return_value={"available_mem_mb": 2048}),
            patch.object(hyadex_server, "server_lifecycle_admission_blocker", return_value=None),
            patch.object(hyadex_server, "server_lifecycle_admission_blocker", return_value=None),
            patch.object(hyadex_server, "ensure_runtime_available", AsyncMock()),
            patch.object(hyadex_server.STORE, "mark_backend_started", AsyncMock(return_value=self.session)) as mark,
            patch.object(hyadex_server.STORE, "update", AsyncMock(return_value=self.session)),
            patch.object(hyadex_server, "build_turn_provider_prompt", return_value="provider prompt"),
            patch.object(hyadex_server, "append_event", AsyncMock(return_value={"type": "turn_started", "seq": 1})),
            patch.object(hyadex_server, "run_claude", AsyncMock()) as run,
        ):
            result = await hyadex_server.post_turn(self.session_id, hyadex_server.TurnRequest(prompt="Start"))
            await asyncio.sleep(0)
            self.assertEqual(result["session"]["id"], self.session_id)
            mark.assert_awaited_once()
            run.assert_awaited_once()

    async def test_queued_turn_start_is_durable_before_provider_task_creation(
        self,
    ) -> None:
        self.session["title"] = "Existing chat"
        order: list[str] = []
        provider_release = asyncio.Event()

        async def append_durable(
            session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            self.assertEqual(session_id, self.session_id)
            self.assertEqual(event_type, "turn_started")
            self.assertEqual(payload["queued_id"], "queued-durable")
            order.append("durable")
            return {"type": event_type, "seq": 1, **payload}

        async def provider() -> None:
            await provider_release.wait()

        def launch_provider(*args: object, **kwargs: object) -> object:
            order.append("provider")
            return provider()

        with (
            patch.object(
                hyadex_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server.STORE,
                "update",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(return_value={}),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(
                hyadex_server,
                "append_durable_event",
                AsyncMock(side_effect=append_durable),
            ) as durable,
            patch.object(hyadex_server, "run_claude", new=launch_provider),
            patch.object(
                hyadex_server,
                "issue_provider_authority",
                AsyncMock(return_value=hyadex_server.STATE_DIR / "test-authority.json"),
            ),
            self.provider_runtime_env_patch(),
            patch.object(
                hyadex_server,
                "scrub_tmux_global_secret_environment",
                return_value=None,
            ),
        ):
            result = await hyadex_server._start_turn_locked(
                self.session_id,
                hyadex_server.TurnRequest(prompt="Queued start"),
                queue_if_busy=False,
                queued_id="queued-durable",
                admission_backend=hyadex_server.BACKEND_CLAUDE,
            )
            try:
                self.assertEqual(order, ["durable", "provider"])
                self.assertEqual(result["event"]["queued_id"], "queued-durable")
                durable.assert_awaited_once()
            finally:
                provider_release.set()
                await asyncio.gather(
                    *list(hyadex_server.RUNS.turn_tasks.get(self.session_id, set())),
                    return_exceptions=True,
                )

    async def test_postcommit_title_failure_preserves_live_run_ownership(
        self,
    ) -> None:
        provider_release = asyncio.Event()
        run_metadata: dict[str, dict[str, object]] = {}
        revoke = AsyncMock()

        async def provider() -> None:
            await provider_release.wait()

        with (
            patch.object(hyadex_server.RUNS, "run_metadata", run_metadata),
            patch.object(
                hyadex_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server.STORE,
                "update",
                AsyncMock(side_effect=OSError("title save failed")),
            ),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(return_value={}),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(
                hyadex_server,
                "append_event",
                AsyncMock(return_value={"type": "turn_started", "seq": 1}),
            ),
            patch.object(
                hyadex_server,
                "run_claude",
                new=lambda *args, **kwargs: provider(),
            ),
            patch.object(
                hyadex_server,
                "issue_provider_authority",
                AsyncMock(return_value=hyadex_server.STATE_DIR / "test-authority.json"),
            ),
            self.provider_runtime_env_patch(),
            patch.object(hyadex_server, "revoke_provider_authority", revoke),
            patch.object(
                hyadex_server,
                "scrub_tmux_global_secret_environment",
                return_value=None,
            ),
            patch.object(hyadex_server.logger, "exception"),
        ):
            result = await hyadex_server._start_turn_locked(
                self.session_id,
                hyadex_server.TurnRequest(prompt="Live start"),
                queue_if_busy=False,
                admission_backend=hyadex_server.BACKEND_CLAUDE,
            )
            try:
                run_id = result["run_id"]
                self.assertIn(self.session_id, hyadex_server.RUNS.busy)
                self.assertEqual(
                    hyadex_server.RUNS.current_turns[self.session_id]["run_id"],
                    run_id,
                )
                self.assertIn(run_id, run_metadata)
                revoke.assert_not_awaited()
            finally:
                provider_release.set()
                await asyncio.gather(
                    *list(hyadex_server.RUNS.turn_tasks.get(self.session_id, set())),
                    return_exceptions=True,
                )

    async def test_precommit_launch_failure_cleans_only_its_owned_run(self) -> None:
        run_metadata: dict[str, dict[str, object]] = {
            "run-replacement": {"purpose": "replacement"}
        }
        revoked_run_ids: list[str] = []

        def fail_launch(*args: object, **kwargs: object) -> object:
            raise RuntimeError("provider launch failed")

        async def revoke_and_replace(run_id: str) -> None:
            revoked_run_ids.append(run_id)
            async with hyadex_server.RUNS.active_lock:
                hyadex_server.RUNS.current_turns[self.session_id] = {
                    "run_id": "run-replacement",
                    "_server_restart_admission_id": "replacement-admission",
                }
                hyadex_server.RUNS.active[self.session_id] = {
                    "run_id": "run-replacement",
                }
                hyadex_server.RUNS.busy.add(self.session_id)

        with (
            patch.object(hyadex_server.RUNS, "run_metadata", run_metadata),
            patch.object(
                hyadex_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(return_value={}),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(
                hyadex_server,
                "append_event",
                AsyncMock(return_value={"type": "turn_started", "seq": 1}),
            ),
            patch.object(hyadex_server, "run_claude", new=fail_launch),
            patch.object(
                hyadex_server,
                "issue_provider_authority",
                AsyncMock(return_value=hyadex_server.STATE_DIR / "test-authority.json"),
            ),
            self.provider_runtime_env_patch(),
            patch.object(
                hyadex_server,
                "revoke_provider_authority",
                AsyncMock(side_effect=revoke_and_replace),
            ),
            patch.object(
                hyadex_server,
                "scrub_tmux_global_secret_environment",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider launch failed"):
                await hyadex_server._start_turn_locked(
                    self.session_id,
                    hyadex_server.TurnRequest(
                        prompt="Failed start",
                        purpose="admission_regression",
                    ),
                    queue_if_busy=False,
                    admission_backend=hyadex_server.BACKEND_CLAUDE,
                )

        self.assertEqual(len(revoked_run_ids), 1)
        self.assertNotIn(revoked_run_ids[0], run_metadata)
        self.assertEqual(
            run_metadata["run-replacement"],
            {"purpose": "replacement"},
        )
        self.assertEqual(
            hyadex_server.RUNS.current_turns[self.session_id]["run_id"],
            "run-replacement",
        )
        self.assertEqual(
            hyadex_server.RUNS.active[self.session_id]["run_id"],
            "run-replacement",
        )
        self.assertIn(self.session_id, hyadex_server.RUNS.busy)

    async def test_cancel_during_precommit_failure_cleanup_does_not_strand_slot(
        self,
    ) -> None:
        run_metadata: dict[str, dict[str, object]] = {}
        revoke_started = asyncio.Event()
        release_revoke = asyncio.Event()

        def fail_launch(*args: object, **kwargs: object) -> object:
            raise RuntimeError("provider launch failed")

        async def blocked_revoke(_run_id: str) -> None:
            revoke_started.set()
            await release_revoke.wait()

        with (
            patch.object(hyadex_server.RUNS, "run_metadata", run_metadata),
            patch.object(
                hyadex_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(return_value={}),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(
                hyadex_server,
                "append_event",
                AsyncMock(return_value={"type": "turn_started", "seq": 1}),
            ),
            patch.object(hyadex_server, "run_claude", new=fail_launch),
            patch.object(
                hyadex_server,
                "issue_provider_authority",
                AsyncMock(return_value=hyadex_server.STATE_DIR / "test-authority.json"),
            ),
            self.provider_runtime_env_patch(),
            patch.object(
                hyadex_server,
                "revoke_provider_authority",
                AsyncMock(side_effect=blocked_revoke),
            ),
            patch.object(
                hyadex_server,
                "scrub_tmux_global_secret_environment",
                return_value=None,
            ),
        ):
            start = asyncio.create_task(
                hyadex_server._start_turn_locked(
                    self.session_id,
                    hyadex_server.TurnRequest(
                        prompt="Cancelled failed start",
                        purpose="admission_regression",
                    ),
                    queue_if_busy=False,
                    admission_backend=hyadex_server.BACKEND_CLAUDE,
                )
            )
            try:
                # This test asserts cancellation ownership, not startup latency.
                # A busy CI worker can spend several seconds before scheduling
                # the cleanup task; keep the barrier bounded without racing it.
                await asyncio.wait_for(revoke_started.wait(), 10)
                start.cancel()
                await asyncio.sleep(0)
                self.assertFalse(start.done())
                self.assertIn(self.session_id, hyadex_server.RUNS.busy)
                start.cancel()
                await asyncio.sleep(0)
                self.assertFalse(start.done())
                self.assertIn(self.session_id, hyadex_server.RUNS.busy)

                release_revoke.set()
                with self.assertRaises(asyncio.CancelledError):
                    await start
            finally:
                release_revoke.set()
                if not start.done():
                    start.cancel()
                    await asyncio.gather(start, return_exceptions=True)

        self.assertNotIn(self.session_id, hyadex_server.RUNS.busy)
        self.assertNotIn(self.session_id, hyadex_server.RUNS.current_turns)
        self.assertEqual(run_metadata, {})

    async def test_precommit_cleanup_failure_still_releases_owned_slot(
        self,
    ) -> None:
        run_metadata: dict[str, dict[str, object]] = {}

        def fail_launch(*args: object, **kwargs: object) -> object:
            raise RuntimeError("provider launch failed")

        async def fail_revoke(_run_id: str) -> None:
            raise OSError("capability revoke failed")

        with (
            patch.object(hyadex_server.RUNS, "run_metadata", run_metadata),
            patch.object(
                hyadex_server.STORE,
                "mark_backend_started",
                AsyncMock(return_value=self.session),
            ),
            patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                return_value=None,
            ),
            patch.object(
                hyadex_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                hyadex_server,
                "ensure_runtime_available",
                AsyncMock(return_value={}),
            ),
            patch.object(
                hyadex_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(
                hyadex_server,
                "append_event",
                AsyncMock(return_value={"type": "turn_started", "seq": 1}),
            ),
            patch.object(hyadex_server, "run_claude", new=fail_launch),
            patch.object(
                hyadex_server,
                "issue_provider_authority",
                AsyncMock(return_value=hyadex_server.STATE_DIR / "test-authority.json"),
            ),
            self.provider_runtime_env_patch(),
            patch.object(
                hyadex_server,
                "revoke_provider_authority",
                AsyncMock(side_effect=fail_revoke),
            ),
            patch.object(
                hyadex_server,
                "scrub_tmux_global_secret_environment",
                return_value=None,
            ),
        ):
            with self.assertRaisesRegex(OSError, "capability revoke failed") as raised:
                await hyadex_server._start_turn_locked(
                    self.session_id,
                    hyadex_server.TurnRequest(
                        prompt="Failed cleanup",
                        purpose="admission_regression",
                    ),
                    queue_if_busy=False,
                    admission_backend=hyadex_server.BACKEND_CLAUDE,
                )

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertNotIn(self.session_id, hyadex_server.RUNS.busy)
        self.assertNotIn(self.session_id, hyadex_server.RUNS.current_turns)
        self.assertEqual(run_metadata, {})


if __name__ == "__main__":
    unittest.main()

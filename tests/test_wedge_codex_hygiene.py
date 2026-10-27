"""Codex thread hygiene: compaction mirroring, tallies, rollout size, rotation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


class CompactionMirroringTests(unittest.IsolatedAsyncioTestCase):
    """Every automatic compaction of one turn is mirrored, not only the first."""

    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": "thread",
            }
        }

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions

    def test_aliases_omit_turn_alias_when_item_is_known(self) -> None:
        with_item = agent_server.native_codex_compaction_terminal_aliases(
            "thread", "turn-1", "compact-1"
        )
        turn_only = agent_server.native_codex_compaction_terminal_aliases(
            "thread", "turn-1", None
        )
        item_only = agent_server.native_codex_compaction_terminal_aliases(
            "thread", None, "compact-1"
        )
        self.assertEqual(len(with_item), 2)
        self.assertEqual(len(turn_only), 2)
        self.assertEqual(len(item_only), 2)
        # The exact identity differs, but the thread+item alias is shared so a
        # sparse item-only replay still resolves to the same compaction.
        self.assertEqual(with_item[1], item_only[1])
        # The thread+turn alias is never part of a lookup that knows the item.
        self.assertNotIn(turn_only[1], with_item)
        self.assertEqual(
            agent_server.native_codex_compaction_terminal_aliases(
                "thread", None, None
            ),
            (),
        )

    def test_remembered_completion_recognises_sparse_replays_only(self) -> None:
        agent_server.remember_codex_native_compaction_terminal(
            "chat", "thread", "turn-1", "compact-1"
        )
        # Sparse replays of the same compaction (either id missing) are terminal.
        self.assertTrue(
            agent_server.codex_native_compaction_was_terminal(
                "chat", "thread", "turn-1", None
            )
        )
        self.assertTrue(
            agent_server.codex_native_compaction_was_terminal(
                "chat", "thread", None, "compact-1"
            )
        )
        self.assertTrue(
            agent_server.codex_native_compaction_was_terminal(
                "chat", "thread", "turn-other", "compact-1"
            )
        )
        # A later compaction in the same turn carries a new item id and is not
        # a replay, even though the turn alias of the first one is remembered.
        self.assertFalse(
            agent_server.codex_native_compaction_was_terminal(
                "chat", "thread", "turn-1", "compact-2"
            )
        )

    async def test_two_compactions_in_one_turn_are_both_mirrored(self) -> None:
        active = {
            "chat": {
                "run_id": "run-1",
                "backend": agent_server.BACKEND_CODEX,
                "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
                "provider_thread_id": "thread",
                "provider_turn_id": "turn-1",
            }
        }
        events: list[tuple[str, dict[str, object]]] = []

        async def record_event(
            session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            events.append((event_type, payload))
            # Mirror the real append path: the session metadata hook is what
            # remembers native terminal identities and tallies compactions.
            await agent_server.update_session_event_metadata(
                session_id,
                {"type": event_type, "seq": len(events), **payload},
            )
            return {}

        def notification(method: str, item_id: str) -> dict[str, object]:
            return {
                "method": method,
                "params": {
                    "threadId": "thread",
                    "turnId": "turn-1",
                    "item": {"id": item_id, "type": "contextCompaction"},
                },
            }

        with (
            patch.object(agent_server.RUNS, "active", active),
            patch.object(
                agent_server,
                "codex_session_id_for_thread",
                return_value="chat",
            ),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "append_event", side_effect=record_event),
        ):
            for item_id in ("compact-1", "compact-2"):
                await agent_server.project_codex_notification(
                    notification("item/started", item_id)
                )
                await agent_server.project_codex_notification(
                    notification("item/completed", item_id)
                )
            # A late replay of the first completion stays suppressed.
            await agent_server.project_codex_notification(
                notification("item/completed", "compact-1")
            )

        types = [event_type for event_type, _payload in events]
        self.assertEqual(
            types,
            [
                "codex_compaction_started",
                "codex_compaction_completed",
                "codex_compaction_started",
                "codex_compaction_completed",
            ],
        )
        item_ids = [payload["item_id"] for _event_type, payload in events]
        self.assertEqual(item_ids, ["compact-1", "compact-1", "compact-2", "compact-2"])
        self.assertEqual(
            agent_server.STORE.sessions["chat"]["codex_compaction_count"],
            2,
        )


class CompactionCounterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_index = agent_server.CODEX_THREAD_SESSION_INDEX
        agent_server.CODEX_THREAD_SESSION_INDEX = {}
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": "thread-a",
                "session_id": "thread-a",
            }
        }

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.CODEX_THREAD_SESSION_INDEX = self.previous_index

    async def completed(self, compaction_id: str, seq: int) -> None:
        await agent_server.update_session_event_metadata(
            "chat",
            {
                "type": "codex_compaction_completed",
                "seq": seq,
                "compaction_id": compaction_id,
                "thread_id": "thread-a",
                "status": "completed",
            },
        )

    async def test_counter_increments_once_per_terminal_key(self) -> None:
        with patch.object(agent_server.STORE, "save", AsyncMock()):
            await self.completed("native:thread-a:turn-1:c1", 1)
            await self.completed("native:thread-a:turn-1:c2", 2)
            # Re-persisting an already terminal key is a replay, not a new
            # compaction.
            await self.completed("native:thread-a:turn-1:c1", 3)
        self.assertEqual(
            agent_server.STORE.sessions["chat"]["codex_compaction_count"], 2
        )
        self.assertEqual(
            agent_server.codex_compaction_count(agent_server.STORE.sessions["chat"]),
            2,
        )
        self.assertEqual(agent_server.codex_compaction_count({"codex_compaction_count": True}), 0)
        self.assertEqual(agent_server.codex_compaction_count({"codex_compaction_count": "9"}), 0)

    async def test_counter_resets_when_provider_thread_changes(self) -> None:
        agent_server.STORE.sessions["chat"]["codex_compaction_count"] = 7
        agent_server.STORE.sessions["chat"]["_codex_hygiene_warned"] = {
            "thread_id": "thread-a",
            "level": "large",
        }
        with (
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(
                agent_server, "broadcast_provider_runtime_changed", AsyncMock()
            ),
            patch.object(
                agent_server, "ensure_codex_thread_not_pending_fork_cleanup"
            ),
        ):
            # Same thread: the tally is preserved.
            await agent_server.STORE.save_provider_session(
                "chat", "thread-a", agent_server.BACKEND_CODEX
            )
            self.assertEqual(
                agent_server.STORE.sessions["chat"]["codex_compaction_count"], 7
            )
            await agent_server.STORE.save_provider_session(
                "chat", "thread-b", agent_server.BACKEND_CODEX
            )
        session = agent_server.STORE.sessions["chat"]
        self.assertNotIn("codex_compaction_count", session)
        self.assertNotIn("_codex_hygiene_warned", session)
        self.assertTrue(session.get("codex_thread_started_at"))
        self.assertEqual(agent_server.codex_compaction_count(session), 0)


class RolloutHygieneTests(unittest.IsolatedAsyncioTestCase):
    THREAD = "0199c0de-1111-4222-8333-444455556666"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sessions"
        day = self.root / "2026" / "09" / "04"
        day.mkdir(parents=True)
        self.rollout = day / f"rollout-2026-09-04T10-00-00-{self.THREAD}.jsonl"
        head = {
            "timestamp": "2026-09-04T10:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": self.THREAD, "cwd": "/tmp"},
        }
        self.rollout.write_text(json.dumps(head) + "\n" + ("x" * 4096) + "\n")
        # A decoy whose filename matches but whose head names another thread.
        decoy = day / f"rollout-2026-09-04T09-00-00-{self.THREAD}.jsonl"
        decoy.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "someone-else"}})
            + "\n"
        )
        agent_server.CODEX_ROLLOUT_PATH_CACHE.clear()
        agent_server.CODEX_ROLLOUT_PATH_MISSES.clear()

    def tearDown(self) -> None:
        agent_server.CODEX_ROLLOUT_PATH_CACHE.clear()
        agent_server.CODEX_ROLLOUT_PATH_MISSES.clear()
        self.temp.cleanup()

    def test_rollout_path_finds_and_caches_the_verified_file(self) -> None:
        with patch.object(agent_server, "CODEX_SESSIONS_ROOT", self.root):
            found = agent_server.codex_rollout_path(self.THREAD)
            self.assertEqual(found, self.rollout.resolve())
            self.assertIn(self.THREAD, agent_server.CODEX_ROLLOUT_PATH_CACHE)
            self.assertIsNone(agent_server.codex_rollout_path("missing-thread"))
            self.assertIn("missing-thread", agent_server.CODEX_ROLLOUT_PATH_MISSES)
            self.assertIsNone(agent_server.codex_rollout_path("../escape"))
            # A vanished cached file is dropped rather than returned stale.
            self.rollout.unlink()
            self.assertIsNone(agent_server.codex_rollout_path(self.THREAD))
            self.assertNotIn(self.THREAD, agent_server.CODEX_ROLLOUT_PATH_CACHE)

    async def test_hygiene_fields_and_warning_thresholds(self) -> None:
        session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "codex_thread_id": self.THREAD,
            "codex_compaction_count": 3,
            "codex_thread_started_at": "2026-09-04T10:00:00Z",
        }
        with patch.object(agent_server, "CODEX_SESSIONS_ROOT", self.root):
            hygiene = await agent_server.codex_thread_hygiene(session)
        self.assertEqual(hygiene["rollout_path"], str(self.rollout.resolve()))
        self.assertEqual(hygiene["rollout_bytes"], self.rollout.stat().st_size)
        self.assertTrue(hygiene["rollout_mtime"])
        self.assertEqual(hygiene["compaction_count"], 3)
        self.assertEqual(hygiene["thread_started_at"], "2026-09-04T10:00:00Z")
        self.assertIsNone(agent_server.codex_thread_hygiene_warning(hygiene))

        with (
            patch.object(agent_server, "CODEX_ROLLOUT_WARN_BYTES", 1000),
            patch.object(agent_server, "CODEX_ROLLOUT_OVERSIZED_BYTES", 10_000),
            patch.object(agent_server, "CODEX_COMPACTION_WARN_COUNT", 5),
        ):
            self.assertEqual(
                agent_server.codex_thread_hygiene_warning(hygiene), "large"
            )
            self.assertEqual(
                agent_server.codex_thread_hygiene_warning(
                    {**hygiene, "rollout_bytes": 10_000}
                ),
                "oversized",
            )
            self.assertEqual(
                agent_server.codex_thread_hygiene_warning(
                    {"rollout_bytes": None, "compaction_count": 5}
                ),
                "large",
            )
            self.assertIsNone(
                agent_server.codex_thread_hygiene_warning(
                    {"rollout_bytes": 999, "compaction_count": 4}
                )
            )
            public = agent_server.codex_thread_hygiene_public(hygiene)
            self.assertEqual(public["warning"], "large")
            self.assertEqual(
                public["thresholds"],
                {
                    "rollout_warn_bytes": 1000,
                    "rollout_oversized_bytes": 10_000,
                    "compaction_warn_count": 5,
                },
            )

        # Chats without a thread report an inert snapshot without touching disk.
        with patch.object(agent_server, "CODEX_SESSIONS_ROOT", self.root):
            empty = await agent_server.codex_thread_hygiene(
                {"id": "new", "backend": agent_server.BACKEND_CODEX}
            )
        self.assertEqual(
            empty,
            {
                "rollout_path": None,
                "rollout_bytes": None,
                "rollout_mtime": None,
                "compaction_count": 0,
                "thread_started_at": None,
            },
        )

    def test_thresholds_are_env_overridable_and_fail_safe(self) -> None:
        with patch.dict(
            "os.environ",
            {"CODEX_ROLLOUT_WARN_BYTES": "12345", "CODEX_COMPACTION_WARN_COUNT": "junk"},
        ):
            self.assertEqual(
                agent_server.codex_hygiene_int_setting("CODEX_ROLLOUT_WARN_BYTES", 1),
                12345,
            )
            self.assertEqual(
                agent_server.codex_hygiene_int_setting("CODEX_COMPACTION_WARN_COUNT", 100),
                100,
            )
        with patch.dict("os.environ", {"HYADEX_CODEX_ROLLOUT_WARN_BYTES": "777"}):
            self.assertEqual(
                agent_server.codex_hygiene_int_setting("CODEX_ROLLOUT_WARN_BYTES", 1),
                777,
            )

    async def test_runtime_snapshot_includes_thread_hygiene(self) -> None:
        previous_sessions = agent_server.STORE.sessions
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": self.THREAD,
                "codex_compaction_count": 2,
                "codex_rotated_threads": [{"thread_id": "old", "rotated_at": "x"}],
            }
        }
        try:
            with (
                patch.object(agent_server, "CODEX_SESSIONS_ROOT", self.root),
                patch.object(agent_server, "CODEX_APP_SERVER_MANAGER", None),
                patch.object(agent_server, "CODEX_ROLLOUT_WARN_BYTES", 1000),
            ):
                runtime = await agent_server.codex_runtime_snapshot("chat")
        finally:
            agent_server.STORE.sessions = previous_sessions
        hygiene = runtime["thread_hygiene"]
        self.assertEqual(hygiene["warning"], "large")
        self.assertEqual(hygiene["compaction_count"], 2)
        self.assertEqual(hygiene["rollout_bytes"], self.rollout.stat().st_size)
        self.assertEqual(runtime["rotated_threads"], [{"thread_id": "old", "rotated_at": "x"}])

    async def test_turn_start_warns_once_per_level(self) -> None:
        previous_sessions = agent_server.STORE.sessions
        agent_server.STORE.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "codex_thread_id": self.THREAD,
            }
        }
        events: list[tuple[str, dict[str, object]]] = []

        async def record_event(
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            events.append((event_type, payload))
            return {}

        try:
            with (
                patch.object(agent_server, "CODEX_SESSIONS_ROOT", self.root),
                patch.object(agent_server, "CODEX_ROLLOUT_WARN_BYTES", 1000),
                patch.object(agent_server, "CODEX_ROLLOUT_OVERSIZED_BYTES", 1 << 40),
                patch.object(agent_server.STORE, "save", AsyncMock()),
                patch.object(agent_server, "append_event", side_effect=record_event),
            ):
                await agent_server.warn_codex_thread_hygiene("chat", self.THREAD, "run-1")
                await agent_server.warn_codex_thread_hygiene("chat", self.THREAD, "run-2")
                self.assertEqual(len(events), 1)
                event_type, payload = events[0]
                self.assertEqual(event_type, "codex_thread_hygiene")
                self.assertEqual(payload["warning"], "large")
                self.assertEqual(payload["run_id"], "run-1")
                self.assertEqual(payload["thread_id"], self.THREAD)
                self.assertIn("Start fresh thread", str(payload["message"]))
                warned = agent_server.STORE.sessions["chat"]["_codex_hygiene_warned"]
                self.assertEqual(warned["level"], "large")
                self.assertEqual(warned["thread_id"], self.THREAD)

                # Crossing the next threshold warns again, exactly once more.
                with patch.object(agent_server, "CODEX_ROLLOUT_OVERSIZED_BYTES", 1000):
                    await agent_server.warn_codex_thread_hygiene("chat", self.THREAD, "run-3")
                    await agent_server.warn_codex_thread_hygiene("chat", self.THREAD, "run-4")
                self.assertEqual(len(events), 2)
                self.assertEqual(events[1][1]["warning"], "oversized")

                # The scheduled variant runs off the turn's critical path.
                agent_server.STORE.sessions["chat"].pop("_codex_hygiene_warned")
                agent_server.schedule_codex_thread_hygiene_check("chat", self.THREAD, "run-5")
                await agent_server.wait_for_session_tasks(
                    agent_server.CODEX_HYGIENE_TASKS, "chat"
                )
                self.assertEqual(len(events), 3)
                self.assertEqual(events[2][1]["run_id"], "run-5")
        finally:
            agent_server.STORE.sessions = previous_sessions

    async def test_acquire_run_thread_schedules_check_only_for_resumed_threads(
        self,
    ) -> None:
        scheduled: list[tuple[str, str, str | None]] = []
        with (
            patch.object(
                agent_server,
                "ensure_codex_app_server_thread",
                AsyncMock(return_value=("thread-native", "hash")),
            ),
            patch.object(
                agent_server,
                "schedule_codex_thread_hygiene_check",
                side_effect=lambda *args: scheduled.append(args),
            ),
        ):
            await agent_server.acquire_codex_run_thread(
                object(),
                "chat",
                {"backend": agent_server.BACKEND_CODEX},
                "/tmp",
                standalone_provider_context=False,
                expected_run_id="run-new",
            )
            self.assertEqual(scheduled, [])
            await agent_server.acquire_codex_run_thread(
                object(),
                "chat",
                {
                    "backend": agent_server.BACKEND_CODEX,
                    "codex_thread_id": "thread-native",
                },
                "/tmp",
                standalone_provider_context=False,
                expected_run_id="run-resume",
            )
        self.assertEqual(scheduled, [("chat", "thread-native", "run-resume")])


if __name__ == "__main__":
    unittest.main()

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import agent_server


def timestamp(value: str, timezone_name: str = "UTC") -> float:
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone_name)).timestamp()






class JobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_for_session_disables_enabled_jobs_and_emits_updates(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs = {
            "job_interval": {
                "id": "job_interval",
                "session_id": "sess_archived",
                "title": "Interval target",
                "prompt": "Do not include this in events",
                "schedule_kind": "interval",
                "interval_seconds": 60,
                "enabled": True,
                "next_run_at": 120.0,
                "scheduled_run_at": 120.0,
                "run_count": 7,
            },
            "job_cron": {
                "id": "job_cron",
                "session_id": "sess_archived",
                "title": "Cron target",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "enabled": True,
                "next_run_at": 130.0,
                "scheduled_run_at": 130.0,
            },
            "job_rrule": {
                "id": "job_rrule",
                "session_id": "sess_archived",
                "title": "RRULE target",
                "schedule_kind": "rrule",
                "rrule": "FREQ=DAILY;COUNT=3",
                "enabled": True,
                "next_run_at": 140.0,
                "scheduled_run_at": 140.0,
            },
            "job_already_paused": {
                "id": "job_already_paused",
                "session_id": "sess_archived",
                "title": "Already paused",
                "enabled": False,
                "next_run_at": None,
                "scheduled_run_at": None,
            },
            "job_manual_pending": {
                "id": "job_manual_pending",
                "session_id": "sess_archived",
                "title": "Pending manual run",
                "enabled": False,
                "next_run_at": 150.0,
                "scheduled_run_at": 150.0,
                "manual_run_pending": True,
                "manual_run_requested_at": "2026-08-26T00:00:00Z",
            },
            "job_other_chat": {
                "id": "job_other_chat",
                "session_id": "sess_active",
                "title": "Other chat",
                "enabled": True,
                "next_run_at": 180.0,
                "scheduled_run_at": 180.0,
            },
        }
        events = AsyncMock()

        with (
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(agent_server, "append_event", events),
        ):
            paused = await store.pause_for_session("sess_archived")

        self.assertEqual(paused, 4)
        for job_id in (
            "job_interval",
            "job_cron",
            "job_rrule",
            "job_manual_pending",
        ):
            self.assertFalse(store.jobs[job_id]["enabled"])
            self.assertIsNone(store.jobs[job_id]["next_run_at"])
            self.assertIsNone(store.jobs[job_id]["scheduled_run_at"])
        self.assertFalse(store.jobs["job_manual_pending"]["manual_run_pending"])
        self.assertEqual(store.jobs["job_interval"]["interval_seconds"], 60)
        self.assertEqual(store.jobs["job_interval"]["run_count"], 7)
        self.assertEqual(store.jobs["job_cron"]["cron_expression"], "0 9 * * *")
        self.assertEqual(store.jobs["job_rrule"]["rrule"], "FREQ=DAILY;COUNT=3")
        self.assertFalse(store.jobs["job_already_paused"]["enabled"])
        self.assertTrue(store.jobs["job_other_chat"]["enabled"])
        self.assertEqual(store.jobs["job_other_chat"]["next_run_at"], 180.0)
        save.assert_awaited_once()
        self.assertEqual(events.await_count, 4)
        self.assertEqual(
            {call.args[2]["job_id"] for call in events.await_args_list},
            {"job_interval", "job_cron", "job_rrule", "job_manual_pending"},
        )
        for call in events.await_args_list:
            self.assertEqual(call.args[0], "sess_archived")
            self.assertEqual(call.args[1], "job_updated")
            self.assertFalse(call.args[2]["job"]["enabled"])
            self.assertNotIn("prompt", call.args[2]["job"])

        events.reset_mock()
        save.reset_mock()
        self.assertEqual(await store.pause_for_session("sess_archived"), 0)
        save.assert_not_awaited()
        events.assert_not_awaited()

    async def test_pause_for_session_save_failure_stays_safe_in_memory(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_target"] = {
            "id": "job_target",
            "session_id": "sess_archived",
            "title": "Target",
            "enabled": True,
            "next_run_at": 120.0,
            "scheduled_run_at": 120.0,
        }
        events = AsyncMock()

        with (
            patch.object(
                store,
                "save",
                AsyncMock(side_effect=OSError("disk full")),
            ),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                await store.pause_for_session("sess_archived")

        self.assertFalse(store.jobs["job_target"]["enabled"])
        self.assertIsNone(store.jobs["job_target"]["next_run_at"])
        self.assertIsNone(store.jobs["job_target"]["scheduled_run_at"])
        events.assert_not_awaited()

    async def test_load_pauses_enabled_jobs_for_archived_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_archived": {
                    "id": "job_archived",
                    "session_id": "sess_archived",
                    "title": "Archived job",
                    "prompt": "Do not run",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "schedule_start_at": 120.0,
                    "scheduled_run_at": 120.0,
                    "next_run_at": 120.0,
                    "enabled": True,
                    "context_mode": "chat",
                    "run_count": 0,
                },
                "job_active": {
                    "id": "job_active",
                    "session_id": "sess_active",
                    "title": "Active job",
                    "prompt": "Still run",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "schedule_start_at": 180.0,
                    "scheduled_run_at": 180.0,
                    "next_run_at": 180.0,
                    "enabled": True,
                    "context_mode": "chat",
                    "run_count": 0,
                },
            }))
            store = agent_server.JobStore()
            sessions = {
                "sess_archived": {"id": "sess_archived", "archived": True},
                "sess_active": {"id": "sess_active", "archived": False},
            }

            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
                patch.object(agent_server.STORE, "sessions", sessions),
            ):
                await store.load()

            self.assertFalse(store.jobs["job_archived"]["enabled"])
            self.assertIsNone(store.jobs["job_archived"]["next_run_at"])
            self.assertIsNone(store.jobs["job_archived"]["scheduled_run_at"])
            self.assertTrue(store.jobs["job_active"]["enabled"])
            persisted = json.loads(jobs_file.read_text())
            self.assertFalse(persisted["job_archived"]["enabled"])
            self.assertIsNone(persisted["job_archived"]["next_run_at"])
            self.assertIsNone(persisted["job_archived"]["scheduled_run_at"])

    async def test_create_rejects_an_archived_parent_session(self) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_create"
        request = agent_server.CreateJobRequest(
            session_id=session_id,
            title="Should not exist",
            prompt="Do not schedule",
        )

        with patch.object(agent_server.STORE, "sessions", {
            session_id: {"id": session_id, "archived": True},
        }):
            with self.assertRaises(HTTPException) as raised:
                await store.create(request)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("unarchive", str(raised.exception.detail).lower())
        self.assertEqual(store.jobs, {})

    async def test_revocation_revision_fences_stale_dispatch_and_pause(self) -> None:
        store = agent_server.JobStore()
        # A job saved before @Chat targets were retired.
        reference = {
            "session_id": "target",
            "display_title_snapshot": "Target",
            "source_text_start": 0,
            "source_text_end": 7,
            "action": "instruction",
        }
        old_revision = agent_server.new_job_revision()
        store.jobs["job_race"] = {
            "id": "job_race",
            "session_id": "source",
            "title": "Routed job",
            "prompt": "@Target check now",
            "chat_references": [reference],
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "loop": True,
            "enabled": True,
            "next_run_at": 100.0,
            "scheduled_run_at": 100.0,
            "run_count": 0,
            "_revision": old_revision,
        }
        sessions = {
            "source": {"id": "source", "backend": agent_server.BACKEND_CODEX},
            "target": {"id": "target", "backend": agent_server.BACKEND_CODEX},
        }
        with (
            patch.object(agent_server.STORE, "sessions", sessions),
            patch.object(agent_server, "AGENT_TOKEN", "test-token"),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            revoked = await store.update(
                "job_race",
                {"chat_references": []},
            )
            self.assertNotEqual(revoked["_revision"], old_revision)

            with self.assertRaises(agent_server.ScheduledJobRevisionChanged):
                await store.assert_dispatch_revision(
                    "job_race",
                    "source",
                    old_revision,
                )

            paused = await store.pause_for_chat_reference_repair(
                "job_race",
                agent_server.ScheduledJobChatReferenceRepairRequired(
                    status_code=409,
                    detail="stale target",
                ),
                expected_revision=old_revision,
            )

        self.assertFalse(paused)
        self.assertTrue(store.jobs["job_race"]["enabled"])
        self.assertNotIn("chat_references", store.jobs["job_race"])

    async def test_admitted_old_occurrence_does_not_advance_edited_schedule(
        self,
    ) -> None:
        store = agent_server.JobStore()
        old_revision = agent_server.new_job_revision()
        old_occurrence = 100.0
        edited_occurrence = time.time() + 3_600
        store.jobs["job_schedule_edit_race"] = {
            "id": "job_schedule_edit_race",
            "session_id": "source",
            "title": "Old schedule",
            "prompt": "Run the old occurrence",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "loop": True,
            "enabled": True,
            "next_run_at": old_occurrence,
            "scheduled_run_at": old_occurrence,
            "run_count": 0,
            "_revision": old_revision,
        }

        async def admit_then_edit(*_args: object, **_kwargs: object) -> dict[str, str]:
            current = store.jobs["job_schedule_edit_race"]
            current.update({
                "title": "Edited cron schedule",
                "schedule_kind": "cron",
                "interval_seconds": None,
                "cron_expression": "0 * * * *",
                "loop": True,
                "next_run_at": edited_occurrence,
                "scheduled_run_at": edited_occurrence,
                "_revision": agent_server.new_job_revision(),
            })
            return {"run_id": "run_old_occurrence"}

        sessions = {
            "source": {
                "id": "source",
                "backend": agent_server.BACKEND_CODEX,
            },
        }
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", sessions),
            patch.object(agent_server, "start_turn", side_effect=admit_then_edit),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(agent_server, "append_event", events),
        ):
            result = await store.run_job("job_schedule_edit_race")

        self.assertEqual(result["run_id"], "run_old_occurrence")
        current = store.jobs["job_schedule_edit_race"]
        self.assertEqual(current["run_count"], 1)
        self.assertEqual(current["last_scheduled_run_at"], old_occurrence)
        self.assertEqual(current["next_run_at"], edited_occurrence)
        self.assertEqual(current["scheduled_run_at"], edited_occurrence)
        self.assertEqual(current["cron_expression"], "0 * * * *")
        self.assertTrue(current["enabled"])
        self.assertEqual(
            events.await_args.args[2]["job_scheduled_run_at"],
            old_occurrence,
        )

    async def test_restart_recovers_admitted_direct_job_without_redispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "source.jsonl"
            jobs_file = root / "jobs.json"
            revision = agent_server.new_job_revision()
            occurrence = 1_000.0
            reference = {
                "session_id": "target-private-id",
                "display_title_snapshot": "Target",
                "source_text_start": 0,
                "source_text_end": 7,
                "action": "direct_message",
            }
            event_path.write_text(json.dumps({
                "seq": 1,
                "type": "turn_started",
                "run_id": "run_admitted_before_crash",
                "purpose": "scheduled_job",
                "job_id": "job_direct_restart",
                "job_revision": revision,
                "job_scheduled_run_at": occurrence,
                "chat_references": [reference],
                "cross_chat_direct_message_ids": ["handoff_admitted"],
            }) + "\n")
            store = agent_server.JobStore()
            store.jobs["job_direct_restart"] = {
                "id": "job_direct_restart",
                "session_id": "source",
                "title": "Direct check",
                "prompt": "@Target check",
                "chat_references": [reference],
                "schedule_kind": "interval",
                "interval_seconds": 60,
                "timezone": "UTC",
                "schedule_start_at": occurrence,
                "scheduled_run_at": occurrence,
                "next_run_at": occurrence,
                "loop": True,
                "max_runs": None,
                "enabled": True,
                "run_count": 0,
                "_revision": revision,
            }

            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
                patch.object(
                    agent_server,
                    "events_path",
                    return_value=event_path,
                ),
                patch.object(agent_server.time, "time", return_value=1_001.0),
            ):
                self.assertEqual(
                    await store.reconcile_admitted_runs_after_restart(),
                    1,
                )
                current = store.jobs["job_direct_restart"]
                self.assertEqual(current["run_count"], 1)
                self.assertEqual(current["last_scheduled_run_at"], occurrence)
                self.assertEqual(current["next_run_at"], 1_060.0)
                self.assertEqual(current["scheduled_run_at"], 1_060.0)
                self.assertEqual(current["chat_references"], [reference])
                self.assertEqual(
                    await store.reconcile_admitted_runs_after_restart(),
                    0,
                )
                self.assertEqual(current["run_count"], 1)

                run_job = AsyncMock()
                sleep_count = 0

                async def one_scheduler_iteration(_delay: float) -> None:
                    nonlocal sleep_count
                    sleep_count += 1
                    if sleep_count > 1:
                        raise asyncio.CancelledError

                with (
                    patch.object(
                        agent_server.asyncio,
                        "sleep",
                        side_effect=one_scheduler_iteration,
                    ),
                    patch.object(store, "run_job", run_job),
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await store.scheduler_loop()
                run_job.assert_not_awaited()

            persisted = json.loads(jobs_file.read_text())
            self.assertEqual(
                persisted["job_direct_restart"]["run_count"],
                1,
            )

    async def test_restart_recovers_manual_run_without_consuming_future_cron(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            event_path = root / "source.jsonl"
            jobs_file = root / "jobs.json"
            revision = agent_server.new_job_revision()
            manual_occurrence = 1_000.0
            future_occurrence = 2_000.0
            event_path.write_text(json.dumps({
                "seq": 1,
                "type": "turn_started",
                "run_id": "run_manual_admitted_before_crash",
                "purpose": "scheduled_job",
                "job_id": "job_manual_restart",
                "job_revision": revision,
                "job_scheduled_run_at": manual_occurrence,
                "manual_run": True,
            }) + "\n")
            store = agent_server.JobStore()
            store.jobs["job_manual_restart"] = {
                "id": "job_manual_restart",
                "session_id": "source",
                "title": "Manual cron recovery",
                "prompt": "Run one extra check",
                "schedule_kind": "cron",
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
                "schedule_start_at": future_occurrence,
                "scheduled_run_at": future_occurrence,
                "next_run_at": future_occurrence,
                "enabled": True,
                "run_count": 0,
                "manual_run_pending": True,
                "manual_run_requested_at": "2026-08-27T00:00:00Z",
                "_revision": revision,
            }

            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
                patch.object(
                    agent_server,
                    "events_path",
                    return_value=event_path,
                ),
                patch.object(agent_server.time, "time", return_value=1_001.0),
            ):
                self.assertEqual(
                    await store.reconcile_admitted_runs_after_restart(),
                    1,
                )
                current = store.jobs["job_manual_restart"]
                self.assertEqual(current["run_count"], 1)
                self.assertFalse(current["manual_run_pending"])
                self.assertEqual(current["next_run_at"], future_occurrence)
                self.assertEqual(
                    current["scheduled_run_at"],
                    future_occurrence,
                )
                self.assertEqual(
                    await store.reconcile_admitted_runs_after_restart(),
                    0,
                )
                self.assertEqual(current["run_count"], 1)

            persisted = json.loads(jobs_file.read_text())
            self.assertEqual(
                persisted["job_manual_restart"]["next_run_at"],
                future_occurrence,
            )
            self.assertFalse(
                persisted["job_manual_restart"]["manual_run_pending"],
            )

    async def test_restart_recovery_preserves_edited_job_revision_or_occurrence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            event_path = Path(temporary) / "source.jsonl"
            admitted_revision = agent_server.new_job_revision()
            admitted_occurrence = 1_000.0
            event_path.write_text(json.dumps({
                "seq": 1,
                "type": "turn_started",
                "run_id": "run_old_schedule",
                "purpose": "scheduled_job",
                "job_id": "job_edited_restart",
                "job_revision": admitted_revision,
                "job_scheduled_run_at": admitted_occurrence,
            }) + "\n")

            for label, current_revision, current_occurrence in (
                (
                    "revision",
                    agent_server.new_job_revision(),
                    admitted_occurrence,
                ),
                ("occurrence", admitted_revision, 2_000.0),
            ):
                with self.subTest(edit=label):
                    store = agent_server.JobStore()
                    store.jobs["job_edited_restart"] = {
                        "id": "job_edited_restart",
                        "session_id": "source",
                        "title": "Edited schedule",
                        "prompt": "Run edited schedule",
                        "schedule_kind": "interval",
                        "interval_seconds": 60,
                        "timezone": "UTC",
                        "schedule_start_at": current_occurrence,
                        "scheduled_run_at": current_occurrence,
                        "next_run_at": current_occurrence,
                        "loop": True,
                        "enabled": True,
                        "run_count": 0,
                        "_revision": current_revision,
                    }
                    save = AsyncMock()
                    with (
                        patch.object(
                            agent_server,
                            "events_path",
                            return_value=event_path,
                        ),
                        patch.object(store, "save", save),
                    ):
                        self.assertEqual(
                            await store.reconcile_admitted_runs_after_restart(),
                            0,
                        )
                    current = store.jobs["job_edited_restart"]
                    self.assertEqual(current["_revision"], current_revision)
                    self.assertEqual(
                        current["scheduled_run_at"],
                        current_occurrence,
                    )
                    self.assertEqual(current["run_count"], 0)
                    save.assert_not_awaited()

    async def test_invalid_stored_chat_target_is_durably_paused_for_repair(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_repair"] = {
            "id": "job_repair",
            "session_id": "source",
            "title": "Recurring handoff",
            "prompt": "@Target check now",
            "chat_references": [{
                "session_id": "target",
                "display_title_snapshot": "Target",
                "source_text_start": 0,
                "source_text_end": 7,
                "action": "instruction",
            }],
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "loop": True,
            "enabled": True,
            "next_run_at": 100.0,
            "scheduled_run_at": 100.0,
            "run_count": 3,
        }
        sessions = {
            "source": {
                "id": "source",
                "backend": agent_server.BACKEND_CODEX,
            },
            "target": {
                "id": "target",
                "backend": agent_server.BACKEND_CODEX,
                "archived": True,
            },
        }
        events = AsyncMock()
        with tempfile.TemporaryDirectory() as temporary:
            jobs_file = Path(temporary) / "jobs.json"
            with (
                patch.object(agent_server.STORE, "sessions", sessions),
                patch.object(agent_server, "AGENT_TOKEN", "test-token"),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(agent_server, "append_event", events),
                patch.object(
                    agent_server,
                    "start_turn",
                    new_callable=AsyncMock,
                ) as start_turn,
            ):
                with self.assertRaises(
                    agent_server.ScheduledJobChatReferenceRepairRequired
                ):
                    await store.run_job("job_repair")

            paused = store.jobs["job_repair"]
            self.assertFalse(paused["enabled"])
            self.assertIsNone(paused["next_run_at"])
            self.assertIsNone(paused["scheduled_run_at"])
            self.assertEqual(paused["run_count"], 3)
            persisted = json.loads(jobs_file.read_text())
            self.assertFalse(persisted["job_repair"]["enabled"])
            self.assertIsNone(persisted["job_repair"]["next_run_at"])
            self.assertIsNone(persisted["job_repair"]["scheduled_run_at"])

        start_turn.assert_not_awaited()
        events.assert_awaited_once()
        self.assertEqual(events.await_args.args[1], "job_error")
        payload = events.await_args.args[2]
        self.assertEqual(
            payload["error_code"],
            "scheduled_chat_reference_invalid",
        )
        self.assertTrue(payload["repair_required"])
        self.assertFalse(payload["job"]["enabled"])
        self.assertIn("Edit or remove the target", payload["message"])
        self.assertIn("re-enable the job", payload["message"])

    async def test_deleted_and_unsupported_stored_targets_pause_without_launch(
        self,
    ) -> None:
        cases = {
            "deleted": None,
            "unsupported": {
                "id": "target",
                "backend": "legacy-provider",
            },
        }
        for case, target in cases.items():
            with self.subTest(case=case):
                store = agent_server.JobStore()
                store.jobs["job_repair"] = {
                    "id": "job_repair",
                    "session_id": "source",
                    "title": "Recurring handoff",
                    "prompt": "@Target check now",
                    "chat_references": [{
                        "session_id": "target",
                        "display_title_snapshot": "Target",
                        "source_text_start": 0,
                        "source_text_end": 7,
                        "action": "instruction",
                    }],
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "loop": True,
                    "enabled": True,
                    "next_run_at": 100.0,
                    "scheduled_run_at": 100.0,
                    "run_count": 0,
                }
                sessions = {
                    "source": {
                        "id": "source",
                        "backend": agent_server.BACKEND_CODEX,
                    },
                }
                if target is not None:
                    sessions["target"] = target
                with (
                    patch.object(agent_server.STORE, "sessions", sessions),
                    patch.object(agent_server, "AGENT_TOKEN", "test-token"),
                    patch.object(store, "save", new_callable=AsyncMock) as save,
                    patch.object(
                        agent_server,
                        "append_event",
                        new_callable=AsyncMock,
                    ) as events,
                    patch.object(
                        agent_server,
                        "start_turn",
                        new_callable=AsyncMock,
                    ) as start_turn,
                ):
                    with self.assertRaises(
                        agent_server.ScheduledJobChatReferenceRepairRequired
                    ):
                        await store.run_job("job_repair")

                self.assertFalse(store.jobs["job_repair"]["enabled"])
                self.assertIsNone(store.jobs["job_repair"]["next_run_at"])
                save.assert_awaited_once()
                events.assert_awaited_once()
                start_turn.assert_not_awaited()

    async def test_archived_session_job_cannot_be_enabled_but_can_be_edited(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_update"
        store.jobs["job_paused"] = {
            "id": "job_paused",
            "session_id": session_id,
            "title": "Paused",
            "prompt": "Do not run",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": None,
            "next_run_at": None,
            "enabled": False,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        original = dict(store.jobs["job_paused"])
        save = AsyncMock()
        events = AsyncMock()

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(store, "save", save),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(HTTPException) as raised:
                await store.update("job_paused", {"enabled": True})
            self.assertEqual(store.jobs["job_paused"], original)
            save.assert_not_awaited()
            events.assert_not_awaited()

            updated = await store.update(
                "job_paused",
                {"title": "Renamed while paused", "enabled": False},
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("unarchive", str(raised.exception.detail).lower())
        self.assertEqual(updated["title"], "Renamed while paused")
        self.assertFalse(updated["enabled"])
        self.assertIsNone(updated["next_run_at"])
        save.assert_awaited_once()
        events.assert_awaited_once()

    async def test_scheduler_pauses_due_archived_jobs_without_running_or_deferring(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_due"
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Due but archived",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        blocker = AsyncMock(return_value=None)
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(store, "run_job", new_callable=AsyncMock) as run_job,
            patch.object(store, "defer", new_callable=AsyncMock) as defer,
            patch.object(agent_server, "scheduled_job_blocker", blocker),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        self.assertFalse(store.jobs["job_due"]["enabled"])
        self.assertIsNone(store.jobs["job_due"]["next_run_at"])
        self.assertIsNone(store.jobs["job_due"]["scheduled_run_at"])
        blocker.assert_not_awaited()
        run_job.assert_not_awaited()
        defer.assert_not_awaited()
        events.assert_awaited_once()
        self.assertEqual(events.await_args.args[1], "job_updated")

    async def test_scheduler_does_not_dispatch_a_job_paused_during_blocker_check(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_pause_race"
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Due before pause",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def pause_while_checking(
            _session_id: str,
            *,
            manual: bool = False,
        ) -> None:
            self.assertFalse(manual)
            store.jobs["job_due"]["enabled"] = False
            store.jobs["job_due"]["next_run_at"] = None
            store.jobs["job_due"]["scheduled_run_at"] = None
            return None

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                side_effect=pause_while_checking,
            ),
            patch.object(store, "run_job", new_callable=AsyncMock) as run_job,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        run_job.assert_not_awaited()

    async def test_scheduler_does_not_defer_over_a_schedule_edit_during_blocker_check(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_schedule_edit_race"
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Due before edit",
            "prompt": "Do not overwrite the edit",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": 1.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "_revision": original_revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def edit_while_checking(
            _session_id: str,
            *,
            manual: bool = False,
        ) -> str:
            self.assertFalse(manual)
            await store.update("job_due", {"next_run_at": "100"})
            return "chat already has a running turn"

        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                side_effect=edit_while_checking,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(store, "run_job", new_callable=AsyncMock) as run_job,
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertEqual(current["schedule_start_at"], 100.0)
        self.assertEqual(current["next_run_at"], 100.0)
        self.assertEqual(current["scheduled_run_at"], 100.0)
        self.assertNotEqual(current["_revision"], original_revision)
        self.assertNotIn("last_deferred_at", current)
        self.assertNotIn("last_defer_reason", current)
        self.assertNotIn(
            "job_deferred",
            [call.args[1] for call in events.await_args_list],
        )
        run_job.assert_not_awaited()

    async def test_scheduler_defers_due_cron_if_maintenance_fences_during_dispatch(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        schedule_id = "c" * 32
        store = agent_server.JobStore()
        session_id = "sess_update_dispatch_race"
        occurrence = 1.0
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Cron update race",
            "prompt": "Keep this occurrence",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_revision": original_revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        update_reason = agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(agent_server, "JOB_DEFER_EVENT_MIN_SECONDS", 0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(
                store,
                "run_job",
                new_callable=AsyncMock,
                side_effect=agent_server.HTTPException(status_code=503, detail=agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL),
            ) as run_job,
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertTrue(current["enabled"])
        self.assertEqual(current["run_count"], 0)
        self.assertEqual(current["scheduled_run_at"], occurrence)
        self.assertEqual(
            current["next_run_at"],
            2.0 + max(agent_server.JOB_BUSY_RETRY_SECONDS, 5),
        )
        self.assertEqual(current["last_defer_reason"], update_reason)
        self.assertNotIn("_update_park_schedule_id", current)
        self.assertNotEqual(current["_revision"], original_revision)
        run_job.assert_awaited_once_with("job_due")
        save.assert_awaited_once()
        events.assert_awaited_once()
        self.assertEqual(events.await_args.args[1], "job_deferred")

    async def test_scheduler_preserves_one_shot_after_typed_admission_race(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_one_shot_admission_race"
        occurrence = 1.0
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "One-shot maintenance race",
            "prompt": "Run once after maintenance",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": False,
            "max_runs": 1,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_revision": agent_server.new_job_revision(),
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        reason = "wait for provider session maintenance to finish"
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(agent_server, "JOB_DEFER_EVENT_MIN_SECONDS", 0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                store,
                "run_job",
                new_callable=AsyncMock,
                side_effect=agent_server.TransientAdmissionWait(
                    status_code=409,
                    detail=reason,
                ),
            ),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertTrue(current["enabled"])
        self.assertEqual(current["run_count"], 0)
        self.assertEqual(current["scheduled_run_at"], occurrence)
        self.assertEqual(
            current["next_run_at"],
            2.0 + max(agent_server.JOB_BUSY_RETRY_SECONDS, 5),
        )
        self.assertEqual(current["last_defer_reason"], reason)
        self.assertEqual(events.await_args.args[1], "job_deferred")

    def test_scheduler_classifies_transient_turn_admission_as_deferral(self) -> None:
        for status_code, detail in (
            (503, "agent launch deferred: server already has 10 active agent run(s)"),
            (409, "wait for session maintenance to finish"),
        ):
            with self.subTest(status_code=status_code):
                error = agent_server.TransientAdmissionWait(
                    status_code=status_code,
                    detail=detail,
                )
                self.assertEqual(
                    agent_server.scheduled_job_lifecycle_admission_defer_reason(
                        error
                    ),
                    detail,
                )

    def test_scheduler_classifies_explicit_stop_race_as_deferral(self) -> None:
        error = agent_server.TransientAdmissionWait(
            status_code=409,
            detail="session already has a running turn",
        )
        self.assertEqual(
            agent_server.scheduled_job_lifecycle_admission_defer_reason(error),
            "session already has a running turn",
        )

    def test_scheduler_does_not_classify_untyped_conflict_as_deferral(self) -> None:
        error = HTTPException(status_code=409, detail="unrelated conflict")
        self.assertIsNone(
            agent_server.scheduled_job_lifecycle_admission_defer_reason(error)
        )

    async def test_scheduler_maintenance_race_does_not_defer_over_edited_cron_revision(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        schedule_id = "d" * 32
        store = agent_server.JobStore()
        session_id = "sess_update_edit_race"
        occurrence = 1.0
        edited_occurrence = 100.0
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Cron edit wins",
            "prompt": "Do not overwrite this edit",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_revision": original_revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def edit_then_reject(_job_id: str) -> None:
            await store.update(
                "job_due",
                {"next_run_at": str(edited_occurrence)},
            )
            raise agent_server.HTTPException(status_code=503, detail=agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL)

        events = AsyncMock()
        original_defer = store.defer
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(store, "run_job", side_effect=edit_then_reject),
            patch.object(store, "defer", wraps=original_defer) as defer,
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertEqual(current["next_run_at"], edited_occurrence)
        self.assertEqual(current["scheduled_run_at"], edited_occurrence)
        self.assertNotEqual(current["_revision"], original_revision)
        self.assertNotIn("last_deferred_at", current)
        self.assertNotIn("last_defer_reason", current)
        self.assertNotIn("_update_park_schedule_id", current)
        self.assertNotIn("_update_park_occurrence", current)
        self.assertNotIn("_update_park_original_next_run_at", current)
        self.assertNotIn("_update_park_revision", current)
        defer.assert_awaited_once_with(
            "job_due",
            agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL,
            agent_server.JOB_BUSY_RETRY_SECONDS,
            expected_revision=original_revision,
            expected_next_run_at=occurrence,
        )
        save.assert_awaited_once()
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_updated"],
        )

    async def test_scheduler_does_not_treat_unrelated_503_as_update_fence(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_unrelated_503"
        occurrence = 1.0
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Ordinary provider failure",
            "prompt": "Do not misclassify this failure",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_revision": agent_server.new_job_revision(),
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(
                store,
                "run_job",
                new_callable=AsyncMock,
                side_effect=HTTPException(
                    status_code=503,
                    detail="provider is temporarily unavailable",
                ),
            ),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertNotIn("last_deferred_at", current)
        self.assertNotIn("last_defer_reason", current)
        self.assertEqual(current["next_run_at"], 60.0)
        self.assertEqual(current["scheduled_run_at"], 60.0)
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_error"],
        )
        save.assert_awaited_once()

    async def test_scheduler_durably_retries_runtime_unavailable_same_occurrence(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_runtime_retry"
        occurrence = 1.0
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Runtime retry",
            "prompt": "Retry this exact occurrence",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "context_mode": "chat",
            "chat_references": [],
            "team_references": [],
            "manual_run_pending": False,
            "_revision": original_revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        runtime_error = HTTPException(
            status_code=503,
            detail={
                "code": "runtime_unavailable",
                "backend": "codex",
                "message": "Codex runtime is unavailable.",
            },
        )
        events = AsyncMock()
        with tempfile.TemporaryDirectory() as temporary, (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            })
        ), patch.object(agent_server, "JOBS_FILE", Path(temporary) / "jobs.json"), (
            patch.object(agent_server, "ensure_dirs", return_value=None)
        ), patch.object(agent_server.time, "time", return_value=2.0), patch.object(
            agent_server,
            "JOB_DEFER_EVENT_MIN_SECONDS",
            0,
        ), patch.object(
            agent_server.asyncio,
            "sleep",
            side_effect=one_scheduler_iteration,
        ), patch.object(
            agent_server,
            "scheduled_job_blocker",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            store,
            "run_job",
            new_callable=AsyncMock,
            side_effect=runtime_error,
        ), patch.object(agent_server, "append_event", events):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

            current = store.jobs["job_due"]
            self.assertTrue(current["enabled"])
            self.assertEqual(current["run_count"], 0)
            self.assertEqual(current["scheduled_run_at"], occurrence)
            self.assertEqual(
                current["next_run_at"],
                2.0 + max(agent_server.JOB_BUSY_RETRY_SECONDS, 5),
            )
            self.assertEqual(current["_runtime_unavailable_occurrence"], occurrence)
            self.assertEqual(current["_runtime_unavailable_defer_count"], 1)
            self.assertNotEqual(current["_revision"], original_revision)
            persisted = json.loads((Path(temporary) / "jobs.json").read_text())
            self.assertEqual(
                persisted["job_due"]["_runtime_unavailable_defer_count"],
                1,
            )

            restarted = agent_server.JobStore()
            await restarted.load()
            restarted_job = restarted.jobs["job_due"]
            retry_result = await restarted.defer_runtime_unavailable(
                "job_due",
                "Codex runtime is still unavailable.",
                expected_revision=restarted_job["_revision"],
                expected_next_run_at=restarted_job["next_run_at"],
            )
            self.assertEqual(retry_result, "deferred")

        self.assertEqual(
            restarted.jobs["job_due"]["_runtime_unavailable_occurrence"],
            occurrence,
        )
        self.assertEqual(
            restarted.jobs["job_due"]["_runtime_unavailable_defer_count"],
            2,
        )
        self.assertEqual(events.await_args.args[1], "job_deferred")

    async def test_scheduler_runtime_unavailable_exhaustion_advances_occurrence(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_runtime_exhausted"
        occurrence = 1.0
        revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Exhausted runtime retry",
            "prompt": "Advance after bounded retries",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_runtime_unavailable_occurrence": occurrence,
            "_runtime_unavailable_defer_count": (
                agent_server.JOB_RUNTIME_UNAVAILABLE_MAX_DEFERS
            ),
            "_revision": revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        events = AsyncMock()
        with patch.object(agent_server.STORE, "sessions", {
            session_id: {"id": session_id, "archived": False},
        }), patch.object(agent_server.time, "time", return_value=2.0), patch.object(
            agent_server.asyncio,
            "sleep",
            side_effect=one_scheduler_iteration,
        ), patch.object(
            agent_server,
            "scheduled_job_blocker",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(store, "save", new_callable=AsyncMock) as save, patch.object(
            store,
            "run_job",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=503,
                detail={
                    "code": "runtime_unavailable",
                    "message": "Codex runtime is unavailable.",
                },
            ),
        ), patch.object(agent_server, "append_event", events):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertEqual(current["next_run_at"], 60.0)
        self.assertEqual(current["scheduled_run_at"], 60.0)
        self.assertNotIn("_runtime_unavailable_occurrence", current)
        self.assertNotIn("_runtime_unavailable_defer_count", current)
        self.assertEqual([call.args[1] for call in events.await_args_list], ["job_error"])
        save.assert_awaited_once()

    async def test_runtime_unavailable_defer_cas_cannot_overwrite_edit_or_delete(
        self,
    ) -> None:
        store = agent_server.JobStore()
        occurrence = 1.0
        original_revision = agent_server.new_job_revision()
        original = {
            "id": "job_due",
            "session_id": "sess_runtime_cas",
            "title": "Original",
            "prompt": "Do not resurrect stale state",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": occurrence,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": occurrence,
            "scheduled_run_at": occurrence,
            "_revision": original_revision,
        }
        reason = "Codex runtime is unavailable."

        edited = dict(original)
        edited["title"] = "User edit wins"
        edited["_revision"] = agent_server.new_job_revision()
        store.jobs["job_due"] = edited
        with patch.object(store, "save", new_callable=AsyncMock) as save:
            edit_result = await store.defer_runtime_unavailable(
                "job_due",
                reason,
                expected_revision=original_revision,
                expected_next_run_at=occurrence,
            )
        self.assertEqual(edit_result, "stale")
        self.assertEqual(store.jobs["job_due"], edited)
        save.assert_not_awaited()

        store.jobs.pop("job_due")
        with patch.object(store, "save", new_callable=AsyncMock) as save:
            delete_result = await store.defer_runtime_unavailable(
                "job_due",
                reason,
                expected_revision=original_revision,
                expected_next_run_at=occurrence,
            )
        self.assertEqual(delete_result, "stale")
        self.assertNotIn("job_due", store.jobs)
        save.assert_not_awaited()

    async def test_scheduler_generic_failure_cannot_overwrite_concurrent_edit(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_generic_edit_race"
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Edit wins generic failure",
            "prompt": "Keep the edited timing",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": 1.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "_revision": original_revision,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def edit_then_fail(_job_id: str) -> None:
            await store.update("job_due", {"next_run_at": "100"})
            raise RuntimeError("provider failed after user edit")

        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(store, "run_job", side_effect=edit_then_fail),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertEqual(current["next_run_at"], 100.0)
        self.assertEqual(current["scheduled_run_at"], 100.0)
        self.assertNotEqual(current["_revision"], original_revision)
        save.assert_awaited_once()
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_updated"],
        )

    async def test_scheduler_uses_fresh_revision_after_edit_during_blocker(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_blocker_edit_failure"
        original_revision = agent_server.new_job_revision()
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Original title",
            "prompt": "Fail permanently once",
            "schedule_kind": "cron",
            "cron_expression": "* * * * *",
            "timezone": "UTC",
            "schedule_start_at": 1.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "_revision": original_revision,
        }
        sleep_count = 0
        blocker_count = 0

        async def two_scheduler_iterations(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 2:
                raise asyncio.CancelledError

        async def edit_on_first_blocker(
            _session_id: str,
            *,
            manual: bool = False,
        ) -> None:
            nonlocal blocker_count
            self.assertFalse(manual)
            blocker_count += 1
            if blocker_count == 1:
                # Keep the replacement occurrence due. The stale iteration
                # must not dispatch it using the original revision.
                await store.update("job_due", {"title": "Edited title"})
            return None

        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=two_scheduler_iterations,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                side_effect=edit_on_first_blocker,
            ),
            patch.object(store, "save", new_callable=AsyncMock) as save,
            patch.object(
                store,
                "run_job",
                new_callable=AsyncMock,
                side_effect=RuntimeError("permanent provider failure"),
            ) as run_job,
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        current = store.jobs["job_due"]
        self.assertEqual(current["title"], "Edited title")
        self.assertNotEqual(current["_revision"], original_revision)
        self.assertEqual(current["next_run_at"], 60.0)
        self.assertEqual(current["scheduled_run_at"], 60.0)
        run_job.assert_awaited_once_with("job_due")
        self.assertEqual(save.await_count, 2)
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_updated", "job_error"],
        )

    async def test_scheduler_treats_a_late_archive_rejection_as_a_pause(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archive_during_dispatch"
        session = {"id": session_id, "archived": False}
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Archive race",
            "prompt": "Do not retry",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "run_count": 0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def reject_after_archive(_job_id: str) -> None:
            session["archived"] = True
            raise HTTPException(
                status_code=409,
                detail="archived chats cannot start turns",
            )

        pause = AsyncMock(return_value=1)
        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {session_id: session}),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(store, "run_job", side_effect=reject_after_archive),
            patch.object(store, "pause_for_session", pause),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        pause.assert_awaited_once_with(session_id)
        events.assert_not_awaited()
        self.assertEqual(store.jobs["job_due"]["run_count"], 0)

    async def test_run_now_rejects_an_archived_session_and_pauses_its_jobs(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_archived_run"
        store.jobs["job_run"] = {
            "id": "job_run",
            "session_id": session_id,
            "title": "Run now",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
        }

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": True},
            }),
            patch.object(store, "pause_for_session", new_callable=AsyncMock) as pause,
            patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start,
        ):
            with self.assertRaises(HTTPException) as raised:
                await store.request_manual_run("job_run")

        self.assertEqual(raised.exception.status_code, 409)
        pause.assert_awaited_once_with(session_id)
        start.assert_not_awaited()

    async def test_manual_run_is_durably_deferred_while_chat_is_busy(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_busy_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "Busy manual run",
            "prompt": "Run once chat is free",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
        }
        events = AsyncMock()

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value="chat already has a running turn",
            ),
            patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start,
            patch.object(agent_server, "append_event", events),
        ):
            result = await store.request_manual_run("job_manual")
            repeated_result = await store.request_manual_run("job_manual")

        self.assertTrue(result["deferred"])
        self.assertTrue(repeated_result["deferred"])
        self.assertTrue(result["queued"])
        self.assertIsNone(result["run_id"])
        self.assertIn("start automatically", result["message"])
        self.assertTrue(result["job"]["manual_run_pending"])
        self.assertTrue(store.jobs["job_manual"]["manual_run_pending"])
        self.assertTrue(agent_server.public_job(
            store.jobs["job_manual"],
        )["manual_run_pending"])
        start.assert_not_awaited()
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_updated", "job_deferred"],
        )
        self.assertTrue(events.await_args_list[1].args[2]["job"]["manual_run_pending"])



    async def test_startup_rearms_all_legacy_update_parks_but_preserves_user_edits(
        self,
    ) -> None:
        """Update retirement rearms exact old occurrences without overwriting edits."""

        store = agent_server.JobStore()
        live_schedule_id = "1" * 32
        stale_schedule_id = "2" * 32
        live_revision = "job_rev_live_park"
        stale_revision = "job_rev_stale_park"
        edited_revision = "job_rev_user_edit"
        store.jobs = {
            "job_live": {
                "id": "job_live",
                "enabled": True,
                "next_run_at": 62.0,
                "scheduled_run_at": 1.0,
                "_revision": live_revision,
                "_update_park_schedule_id": live_schedule_id,
                "_update_park_occurrence": 1.0,
                "_update_park_original_next_run_at": 1.0,
                "_update_park_revision": live_revision,
            },
            "job_stale": {
                "id": "job_stale",
                "enabled": True,
                "next_run_at": 62.0,
                "scheduled_run_at": 1.0,
                "_revision": stale_revision,
                "_update_park_schedule_id": stale_schedule_id,
                "_update_park_occurrence": 1.0,
                "_update_park_original_next_run_at": 1.0,
                "_update_park_revision": stale_revision,
            },
            # A user edit won after the old scheduler snapshot. Startup may
            # clear obsolete ownership metadata, but must preserve its timing.
            "job_edited": {
                "id": "job_edited",
                "enabled": True,
                "next_run_at": 100.0,
                "scheduled_run_at": 100.0,
                "_revision": edited_revision,
                "_update_park_schedule_id": stale_schedule_id,
                "_update_park_occurrence": 1.0,
                "_update_park_original_next_run_at": 1.0,
                "_update_park_revision": "job_rev_stale_before_edit",
            },
        }

        with patch.object(agent_server.time, "time", return_value=2.0), \
             patch.object(store, "save", new_callable=AsyncMock) as save:
            resumed = await store.resume_legacy_update_parked()

        self.assertEqual(resumed, 2)
        live = store.jobs["job_live"]
        self.assertEqual(live["next_run_at"], 1.0)
        self.assertNotEqual(live["_revision"], live_revision)
        self.assertNotIn("_update_park_schedule_id", live)

        stale = store.jobs["job_stale"]
        self.assertEqual(stale["next_run_at"], 1.0)
        self.assertEqual(stale["scheduled_run_at"], 1.0)
        self.assertNotEqual(stale["_revision"], stale_revision)
        for field in (
            "_update_park_schedule_id",
            "_update_park_occurrence",
            "_update_park_original_next_run_at",
            "_update_park_revision",
        ):
            self.assertNotIn(field, stale)

        edited = store.jobs["job_edited"]
        self.assertEqual(edited["next_run_at"], 100.0)
        self.assertEqual(edited["scheduled_run_at"], 100.0)
        self.assertNotEqual(edited["_revision"], edited_revision)
        self.assertNotIn("_update_park_schedule_id", edited)
        save.assert_awaited_once()

    async def test_maintenance_race_at_start_turn_preserves_job_occurrence(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        schedule_id = "3" * 32
        store = agent_server.JobStore()
        store.jobs["job_race"] = {
            "id": "job_race",
            "session_id": "race-chat",
            "title": "Admission race",
            "prompt": "Yield if the updater wins admission.",
            "schedule_kind": "interval",
            "interval_seconds": 300,
            "schedule_start_at": 1.0,
            "loop": True,
            "max_runs": None,
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "run_count": 0,
            "manual_run_pending": False,
            "_revision": "job_rev_race",
        }
        sleeps = 0

        async def stop_after_iteration(_delay: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        with (
            patch.object(agent_server.STORE, "sessions", {
                "race-chat": {
                    "id": "race-chat",
                    "backend": agent_server.BACKEND_CODEX,
                    "archived": False,
                },
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=stop_after_iteration,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                agent_server,
                "start_turn",
                new_callable=AsyncMock,
                side_effect=agent_server.HTTPException(status_code=503, detail=agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL),
            ) as start,
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        start.assert_awaited_once()
        job = store.jobs["job_race"]
        self.assertTrue(job["enabled"])
        self.assertEqual(job["run_count"], 0)
        self.assertEqual(job["scheduled_run_at"], 1.0)
        self.assertNotIn("_update_park_schedule_id", job)
        self.assertEqual(job["last_defer_reason"], agent_server.EXECUTION_MAINTENANCE_ACTIVE_DETAIL)
        self.assertNotEqual(job["_revision"], "job_rev_race")

    async def test_permanent_manual_run_error_clears_pending_and_emits_error(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_invalid_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "Invalid manual run",
            "prompt": "Cannot be admitted",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
        }
        events = AsyncMock()

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                agent_server,
                "start_turn",
                new_callable=AsyncMock,
                side_effect=HTTPException(
                    status_code=400,
                    detail="invalid scheduled job",
                ),
            ),
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(HTTPException) as raised:
                await store.request_manual_run("job_manual")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(store.jobs["job_manual"]["manual_run_pending"])
        self.assertIn("invalid scheduled job", store.jobs["job_manual"][
            "last_manual_run_error"
        ])
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_updated", "job_error"],
        )

    async def test_scheduler_drops_permanently_invalid_pending_manual_run(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_corrupt_pending_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "Corrupt pending run",
            "prompt": "Cannot run",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
            "manual_run_pending": True,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        events = AsyncMock()
        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                agent_server,
                "start_turn",
                new_callable=AsyncMock,
                side_effect=ValueError("corrupt scheduled job"),
            ) as start,
            patch.object(agent_server, "append_event", events),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        start.assert_awaited_once()
        self.assertFalse(store.jobs["job_manual"]["manual_run_pending"])
        self.assertEqual(
            [call.args[1] for call in events.await_args_list],
            ["job_error"],
        )

    async def test_duplicate_manual_run_taps_coalesce_during_admission(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_duplicate_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "One manual run",
            "prompt": "Run only once",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
        }
        admission_started = asyncio.Event()
        release_admission = asyncio.Event()

        async def delayed_start(*_args, **_kwargs):
            admission_started.set()
            await release_admission.wait()
            return {"run_id": "run_once", "queued": False}

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(agent_server, "start_turn", side_effect=delayed_start) as start,
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            first_request = asyncio.create_task(
                store.request_manual_run("job_manual"),
            )
            await admission_started.wait()
            duplicate_result = await store.request_manual_run("job_manual")
            release_admission.set()
            first_result = await first_request

        self.assertTrue(duplicate_result["deferred"])
        self.assertTrue(duplicate_result["queued"])
        self.assertEqual(first_result["run_id"], "run_once")
        self.assertFalse(first_result["deferred"])
        self.assertEqual(start.await_count, 1)
        self.assertEqual(store.jobs["job_manual"]["run_count"], 1)
        self.assertFalse(store.jobs["job_manual"]["manual_run_pending"])

    async def test_scheduler_dispatches_pending_manual_run_for_disabled_job(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_deferred_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "Deferred manual run",
            "prompt": "Run after blocker clears",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
            "manual_run_pending": True,
            "manual_run_requested_at": "2026-08-26T00:00:00Z",
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=2.0),
            patch.object(
                agent_server.asyncio,
                "sleep",
                side_effect=one_scheduler_iteration,
            ),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                agent_server,
                "start_turn",
                new_callable=AsyncMock,
                return_value={"run_id": "run_deferred", "queued": False},
            ) as start,
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        start.assert_awaited_once()
        self.assertEqual(store.jobs["job_manual"]["run_count"], 1)
        self.assertFalse(store.jobs["job_manual"]["manual_run_pending"])
        self.assertFalse(store.jobs["job_manual"]["enabled"])

    async def test_manual_run_starts_immediately_when_chat_is_idle(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_idle_manual"
        store.jobs["job_manual"] = {
            "id": "job_manual",
            "session_id": session_id,
            "title": "Immediate manual run",
            "prompt": "Run now",
            "enabled": False,
            "next_run_at": None,
            "scheduled_run_at": None,
            "run_count": 0,
        }
        start = AsyncMock(return_value={
            "run_id": "run_immediate",
            "queued": False,
        })
        events = AsyncMock()

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(agent_server, "start_turn", start),
            patch.object(agent_server, "append_event", events),
        ):
            result = await store.request_manual_run("job_manual")

        self.assertEqual(result["run_id"], "run_immediate")
        self.assertFalse(result["deferred"])
        self.assertFalse(result["queued"])
        self.assertFalse(result["manual_run_pending"])
        self.assertFalse(result["job"]["manual_run_pending"])
        self.assertEqual(start.await_args.args[1].purpose, "scheduled_job")
        self.assertTrue(events.await_args.args[2]["manual_run"])

    async def test_early_manual_run_preserves_future_cron_occurrence(
        self,
    ) -> None:
        store = agent_server.JobStore()
        session_id = "sess_future_cron"
        future_occurrence = 2_000.0
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": session_id,
            "title": "Future cron",
            "prompt": "Run an extra check now",
            "schedule_kind": "cron",
            "cron_expression": "0 9 * * *",
            "timezone": "UTC",
            "enabled": True,
            "next_run_at": future_occurrence,
            "scheduled_run_at": future_occurrence,
            "run_count": 0,
        }
        start = AsyncMock(return_value={
            "run_id": "run_early",
            "queued": False,
        })

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=1_000.0),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(agent_server, "start_turn", start),
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            result = await store.request_manual_run("job_cron")

        self.assertFalse(result["deferred"])
        self.assertEqual(store.jobs["job_cron"]["next_run_at"], future_occurrence)
        self.assertEqual(
            store.jobs["job_cron"]["scheduled_run_at"],
            future_occurrence,
        )
        self.assertTrue(store.jobs["job_cron"]["enabled"])
        self.assertEqual(
            start.await_args.args[1].job_scheduled_run_at,
            1_000.0,
        )

    async def test_manual_run_honors_max_runs_limit(self) -> None:
        store = agent_server.JobStore()
        session_id = "sess_manual_limit"
        store.jobs["job_limited"] = {
            "id": "job_limited",
            "session_id": session_id,
            "title": "Limited run",
            "prompt": "Run the final allowed time",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "loop": False,
            "max_runs": 1,
            "enabled": True,
            "next_run_at": 2_000.0,
            "scheduled_run_at": 2_000.0,
            "run_count": 0,
        }

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: {"id": session_id, "archived": False},
            }),
            patch.object(agent_server.time, "time", return_value=1_000.0),
            patch.object(store, "save", new_callable=AsyncMock),
            patch.object(
                agent_server,
                "scheduled_job_blocker",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                agent_server,
                "start_turn",
                new_callable=AsyncMock,
                return_value={"run_id": "run_limited", "queued": False},
            ),
            patch.object(agent_server, "append_event", new_callable=AsyncMock),
        ):
            await store.request_manual_run("job_limited")

        self.assertEqual(store.jobs["job_limited"]["run_count"], 1)
        self.assertFalse(store.jobs["job_limited"]["enabled"])
        self.assertIsNone(store.jobs["job_limited"]["next_run_at"])

    async def test_delete_for_session_restores_jobs_when_persistence_fails(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_retry"] = {
            "id": "job_retry",
            "session_id": "missing-chat",
            "title": "Retry cleanup",
        }
        with patch.object(
            store,
            "save",
            AsyncMock(side_effect=[OSError("disk full"), None]),
        ) as save:
            with self.assertRaisesRegex(OSError, "disk full"):
                await store.delete_for_session("missing-chat")
            self.assertIn("job_retry", store.jobs)

            self.assertEqual(
                await store.delete_for_session("missing-chat"),
                1,
            )

        self.assertNotIn("job_retry", store.jobs)
        self.assertEqual(save.await_count, 2)

    async def test_scheduler_cleanup_failure_does_not_resurrect_missing_session(
        self,
    ) -> None:
        store = agent_server.JobStore()
        store.jobs["job_orphan"] = {
            "id": "job_orphan",
            "session_id": "missing-chat",
            "title": "Orphan",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", root / "jobs.json"),
                patch.object(agent_server.STORE, "sessions", {}),
                patch.object(agent_server.time, "time", return_value=2.0),
                patch.object(
                    agent_server.asyncio,
                    "sleep",
                    side_effect=one_scheduler_iteration,
                ),
                patch.object(
                    store,
                    "save",
                    AsyncMock(side_effect=OSError("disk full")),
                ),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await store.scheduler_loop()

            self.assertFalse((root / "sessions" / "missing-chat").exists())

        self.assertIn("job_orphan", store.jobs)

    async def test_load_migrates_legacy_interval_without_rescheduling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_old": {
                    "id": "job_old",
                    "session_id": "sess_1",
                    "interval_seconds": 60,
                    "loop": True,
                    "enabled": True,
                    "next_run_at": 12345.0,
                    "run_count": 7,
                }
            }))
            store = agent_server.JobStore()
            with patch.object(agent_server, "STATE_DIR", root), patch.object(agent_server, "JOBS_FILE", jobs_file):
                await store.load()

            migrated = store.jobs["job_old"]
            self.assertEqual(migrated["schedule_kind"], "interval")
            self.assertEqual(migrated["timezone"], "UTC")
            self.assertEqual(migrated["next_run_at"], 12345.0)
            self.assertEqual(migrated["scheduled_run_at"], 12345.0)
            self.assertEqual(migrated["run_count"], 7)
            self.assertTrue(migrated["enabled"])

    async def test_explicit_first_cron_run_is_exact_then_returns_to_rule(self) -> None:
        store = agent_server.JobStore()
        now = timestamp("2026-07-21T08:00:00", "America/Los_Angeles")
        first = timestamp("2026-07-21T10:17:00", "America/Los_Angeles")
        agent_server.STORE.sessions["sess_test"] = {"id": "sess_test"}
        request = agent_server.CreateJobRequest(
            session_id="sess_test",
            title="Daily",
            prompt="Check",
            schedule_kind="cron",
            cron_expression="0 9 * * *",
            timezone="America/Los_Angeles",
            first_run_at="2026-07-21T10:17:00",
        )
        try:
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=now):
                job = await store.create(request)
            self.assertEqual(job["next_run_at"], first)

            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=first + 60):
                await store.mark_ran(job["id"])
            expected = timestamp("2026-07-22T09:00:00", "America/Los_Angeles")
            self.assertEqual(store.jobs[job["id"]]["next_run_at"], expected)
        finally:
            agent_server.STORE.sessions.pop("sess_test", None)

    async def test_defer_preserves_canonical_interval_cadence(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_1"] = {
            "id": "job_1",
            "session_id": "sess_1",
            "title": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": 1060.0,
            "scheduled_run_at": 1060.0,
            "next_run_at": 1060.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1065.0):
            await store.defer("job_1", "busy", delay_seconds=300)
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1365.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1060.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 0)

        # Repeated busy checks replace the one retry deadline. They must not
        # enqueue multiple catch-up executions for the missed intervals.
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1100.0):
            await store.defer("job_1", "still busy", delay_seconds=300)
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1400.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1060.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 0)

        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=1405.0):
            await store.mark_ran("job_1")
        self.assertEqual(store.jobs["job_1"]["next_run_at"], 1420.0)
        self.assertEqual(store.jobs["job_1"]["scheduled_run_at"], 1420.0)
        self.assertEqual(store.jobs["job_1"]["run_count"], 1)

    async def test_scoped_update_delete_enforce_ownership_and_emit_events(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_1"] = {
            "id": "job_1",
            "session_id": "sess_owner",
            "title": "Check",
            "prompt": "private prompt",
            "schedule_kind": "interval",
            "interval_seconds": 60,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 1060.0,
            "next_run_at": 1060.0,
            "enabled": True,
            "loop": True,
            "run_count": 0,
        }
        with self.assertRaises(HTTPException):
            await store.update("job_1", {"title": "No"}, expected_session_id="sess_other")
        with self.assertRaises(HTTPException):
            await store.delete("job_1", expected_session_id="sess_other")

        events = AsyncMock()
        with patch.object(store, "save", new_callable=AsyncMock), patch.object(agent_server, "append_event", events):
            await store.update("job_1", {"title": "Updated"}, expected_session_id="sess_owner")
            await store.delete("job_1", expected_session_id="sess_owner")
        self.assertEqual([call.args[1] for call in events.await_args_list], ["job_updated", "job_deleted"])
        self.assertNotIn("prompt", events.await_args_list[0].args[2]["job"])
        self.assertNotIn("prompt", events.await_args_list[1].args[2]["job"])

    async def test_legacy_interval_edit_cannot_convert_a_calendar_job(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-07-21T08:00:00"),
            "scheduled_run_at": timestamp("2026-07-21T09:00:00"),
            "next_run_at": timestamp("2026-07-21T09:00:00"),
            "enabled": True,
            "loop": True,
            "max_runs": 3,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock):
            updated = await store.update("job_cron", {
                "title": "Renamed by a v7 client",
                "interval_seconds": 3600,
                "loop": True,
                "max_runs": None,
            })
        self.assertEqual(updated["title"], "Renamed by a v7 client")
        self.assertEqual(updated["schedule_kind"], "cron")
        self.assertEqual(updated["cron_expression"], "0 9 * * *")
        self.assertIsNone(updated["interval_seconds"])
        self.assertEqual(updated["max_runs"], 3)

    async def test_rejected_schedule_update_is_atomic(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Hourly",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        before = json.loads(json.dumps(store.jobs["job_interval"]))
        with self.assertRaises(HTTPException):
            await store.update("job_interval", {
                "schedule_kind": "cron",
                "interval_seconds": None,
                "cron_expression": "not a cron expression",
            })
        self.assertEqual(store.jobs["job_interval"], before)
        with self.assertRaises(HTTPException):
            await store.update("job_interval", {"interval_seconds": 10**20})
        self.assertEqual(store.jobs["job_interval"], before)

    async def test_metadata_only_interval_edit_does_not_reschedule(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Hourly",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=2000.25):
            updated = await store.update("job_interval", {"title": "Renamed", "timezone": None})
        self.assertEqual(updated["schedule_start_at"], 1000.0)
        self.assertEqual(updated["next_run_at"], 4600.0)

    async def test_explicit_interval_next_run_reanchors_recurring_cadence(self) -> None:
        store = agent_server.JobStore()
        selected_run = 10_000.0
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 86_400,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1_000.0,
            "scheduled_run_at": 2_000.0,
            "next_run_at": 2_000.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=5_000.0):
            updated = await store.update(
                "job_interval",
                {"next_run_at": str(selected_run)},
            )

        self.assertEqual(updated["schedule_start_at"], selected_run)
        self.assertEqual(updated["scheduled_run_at"], selected_run)
        self.assertEqual(updated["next_run_at"], selected_run)

        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=selected_run + 1):
            await store.mark_ran("job_interval")

        self.assertEqual(
            store.jobs["job_interval"]["next_run_at"],
            selected_run + 86_400,
        )

    async def test_explicit_interval_next_run_wins_over_duration_change(self) -> None:
        store = agent_server.JobStore()
        selected_run = 10_000.0
        store.jobs["job_interval"] = {
            "id": "job_interval",
            "session_id": "sess_owner",
            "title": "Hourly",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3_600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1_000.0,
            "scheduled_run_at": 4_600.0,
            "next_run_at": 4_600.0,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=5_000.0):
            updated = await store.update("job_interval", {
                "interval_seconds": 7_200,
                "next_run_at": str(selected_run),
            })

        self.assertEqual(updated["interval_seconds"], 7_200)
        self.assertEqual(updated["schedule_start_at"], selected_run)
        self.assertEqual(updated["next_run_at"], selected_run)

    async def test_explicit_null_recomputes_calendar_next_match(self) -> None:
        store = agent_server.JobStore()
        now = timestamp("2026-07-21T08:15:00")
        stale_override = timestamp("2026-07-21T12:34:00")
        expected = timestamp("2026-07-21T09:00:00")
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-07-01T00:00:00"),
            "scheduled_run_at": stale_override,
            "next_run_at": stale_override,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=now):
            updated = await store.update("job_cron", {"next_run_at": None})

        self.assertEqual(updated["next_run_at"], expected)
        self.assertEqual(updated["scheduled_run_at"], expected)

    async def test_cron_expression_update_recomputes_next_run(self) -> None:
        store = agent_server.JobStore()
        now = timestamp("2026-07-21T08:15:00")
        expected = timestamp("2026-07-21T10:30:00")
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-07-01T00:00:00"),
            "scheduled_run_at": timestamp("2026-07-21T09:00:00"),
            "next_run_at": timestamp("2026-07-21T09:00:00"),
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=now):
            updated = await store.update(
                "job_cron",
                {"cron_expression": "30 10 * * *"},
            )

        self.assertEqual(updated["cron_expression"], "30 10 * * *")
        self.assertEqual(updated["next_run_at"], expected)
        self.assertEqual(updated["scheduled_run_at"], expected)

    async def test_explicit_cron_next_run_remains_a_one_off_override(self) -> None:
        store = agent_server.JobStore()
        now = timestamp("2026-07-21T08:15:00")
        override = timestamp("2026-07-21T12:34:00")
        expected_after_override = timestamp("2026-07-22T09:00:00")
        original_anchor = timestamp("2026-07-01T00:00:00")
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": original_anchor,
            "scheduled_run_at": timestamp("2026-07-21T09:00:00"),
            "next_run_at": timestamp("2026-07-21T09:00:00"),
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=now):
            updated = await store.update(
                "job_cron",
                {"next_run_at": str(override)},
            )

        self.assertEqual(updated["schedule_start_at"], original_anchor)
        self.assertEqual(updated["next_run_at"], override)

        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=override + 1):
            await store.mark_ran("job_cron")

        self.assertEqual(
            store.jobs["job_cron"]["next_run_at"],
            expected_after_override,
        )

    async def test_omitted_next_run_preserves_calendar_override(self) -> None:
        store = agent_server.JobStore()
        override = timestamp("2026-07-21T12:34:00")
        store.jobs["job_cron"] = {
            "id": "job_cron",
            "session_id": "sess_owner",
            "title": "Daily",
            "prompt": "Check",
            "schedule_kind": "cron",
            "interval_seconds": None,
            "cron_expression": "0 9 * * *",
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": timestamp("2026-07-01T00:00:00"),
            "scheduled_run_at": override,
            "next_run_at": override,
            "enabled": True,
            "loop": True,
            "max_runs": None,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=timestamp("2026-07-21T08:15:00")):
            updated = await store.update("job_cron", {"title": "Renamed"})

        self.assertEqual(updated["next_run_at"], override)
        self.assertEqual(updated["scheduled_run_at"], override)

    async def test_schedule_kind_switch_preserves_finite_run_limit(self) -> None:
        store = agent_server.JobStore()
        store.jobs["job_finite"] = {
            "id": "job_finite",
            "session_id": "sess_owner",
            "title": "Finite",
            "prompt": "Check",
            "schedule_kind": "interval",
            "interval_seconds": 3600,
            "cron_expression": None,
            "rrule": None,
            "timezone": "UTC",
            "schedule_start_at": 1000.0,
            "scheduled_run_at": 4600.0,
            "next_run_at": 4600.0,
            "enabled": True,
            "loop": False,
            "max_runs": 3,
            "run_count": 0,
        }
        with patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server.time, "time", return_value=2000.25):
            cron = await store.update("job_finite", {
                "schedule_kind": "cron",
                "interval_seconds": None,
                "cron_expression": "0 9 * * *",
                "timezone": "UTC",
            })
            rrule = await store.update("job_finite", {
                "schedule_kind": "rrule",
                "cron_expression": None,
                "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
                "timezone": "UTC",
            })
        self.assertEqual(cron["max_runs"], 3)
        self.assertEqual(rrule["max_runs"], 3)

    async def test_count_one_rrule_schedules_exactly_one_run(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_count"] = {"id": "sess_count"}
        request = agent_server.CreateJobRequest(
            session_id="sess_count",
            title="Once",
            prompt="Check",
            schedule_kind="rrule",
            rrule="FREQ=DAILY;COUNT=1",
            timezone="UTC",
        )
        try:
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=2000.25):
                job = await store.create(request)
            self.assertEqual(job["next_run_at"], 2001.0)
            with patch.object(store, "save", new_callable=AsyncMock), \
                    patch.object(agent_server.time, "time", return_value=2001.5):
                await store.mark_ran(job["id"])
            self.assertEqual(store.jobs[job["id"]]["run_count"], 1)
            self.assertFalse(store.jobs[job["id"]]["enabled"])
            self.assertIsNone(store.jobs[job["id"]]["next_run_at"])
        finally:
            agent_server.STORE.sessions.pop("sess_count", None)

    async def test_load_defaults_legacy_jobs_to_chat_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_old": {
                    "id": "job_old",
                    "session_id": "sess_1",
                    "title": "Legacy",
                    "prompt": "Check",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "enabled": False,
                    "run_count": 0,
                }
            }))
            store = agent_server.JobStore()
            with (
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
            ):
                await store.load()

            self.assertEqual(store.jobs["job_old"]["context_mode"], "chat")
            persisted = json.loads(jobs_file.read_text())
            self.assertEqual(persisted["job_old"]["context_mode"], "chat")

    async def test_create_and_update_job_context_mode(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_context"] = {"id": "sess_context"}
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                default_job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_context",
                    title="Default",
                    prompt="Check",
                ))
                standalone_job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_context",
                    title="Standalone",
                    prompt="Check",
                    context_mode="standalone",
                ))
                updated = await store.update(
                    standalone_job["id"],
                    {"context_mode": "chat"},
                )

            self.assertEqual(default_job["context_mode"], "chat")
            self.assertEqual(standalone_job["context_mode"], "standalone")
            self.assertEqual(updated["context_mode"], "chat")
        finally:
            agent_server.STORE.sessions.pop("sess_context", None)

    async def test_legacy_create_infers_standalone_for_alternate_backend(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_backend_contract"] = {
            "id": "sess_backend_contract",
            "backend": agent_server.BACKEND_CODEX,
        }
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                inherited = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Inherited",
                    prompt="Check",
                ))
                matching = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Matching",
                    prompt="Check",
                    backend=agent_server.BACKEND_CODEX,
                ))
                explicit_standalone = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                    context_mode="standalone",
                ))
                legacy_standalone = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_contract",
                    title="Legacy independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                ))
                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.create(agent_server.CreateJobRequest(
                        session_id="sess_backend_contract",
                        title="Invalid same-chat backend",
                        prompt="Check",
                        backend=agent_server.BACKEND_CLAUDE,
                        context_mode="chat",
                    ))

            self.assertIsNone(inherited["backend"])
            self.assertEqual(matching["backend"], agent_server.BACKEND_CODEX)
            self.assertEqual(
                explicit_standalone["backend"],
                agent_server.BACKEND_CLAUDE,
            )
            self.assertEqual(explicit_standalone["context_mode"], "standalone")
            self.assertEqual(
                legacy_standalone["backend"],
                agent_server.BACKEND_CLAUDE,
            )
            self.assertEqual(legacy_standalone["context_mode"], "standalone")
            self.assertEqual(len(store.jobs), 4)
        finally:
            agent_server.STORE.sessions.pop("sess_backend_contract", None)

    async def test_context_mode_update_validates_resulting_backend_atomically(self) -> None:
        store = agent_server.JobStore()
        agent_server.STORE.sessions["sess_backend_update"] = {
            "id": "sess_backend_update",
            "backend": agent_server.BACKEND_CODEX,
        }
        try:
            with (
                patch.object(store, "save", new_callable=AsyncMock),
                patch.object(agent_server, "append_event", new_callable=AsyncMock),
            ):
                job = await store.create(agent_server.CreateJobRequest(
                    session_id="sess_backend_update",
                    title="Independent Claude",
                    prompt="Check",
                    backend=agent_server.BACKEND_CLAUDE,
                    context_mode="standalone",
                ))
                before = dict(store.jobs[job["id"]])
                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.update(job["id"], {"context_mode": "chat"})
                self.assertEqual(store.jobs[job["id"]], before)

                same_chat = await store.update(job["id"], {
                    "context_mode": "chat",
                    "backend": agent_server.BACKEND_CODEX,
                })
                self.assertEqual(same_chat["context_mode"], "chat")
                self.assertEqual(same_chat["backend"], agent_server.BACKEND_CODEX)

                legacy_update = await store.update(
                    job["id"],
                    {"backend": agent_server.BACKEND_CLAUDE},
                )
                self.assertEqual(legacy_update["context_mode"], "standalone")
                self.assertEqual(
                    store.jobs[job["id"]]["backend"],
                    agent_server.BACKEND_CLAUDE,
                )

                with self.assertRaisesRegex(
                    HTTPException,
                    "must use the parent chat backend",
                ):
                    await store.update(job["id"], {"context_mode": "chat"})
                self.assertEqual(
                    store.jobs[job["id"]]["context_mode"],
                    "standalone",
                )
        finally:
            agent_server.STORE.sessions.pop("sess_backend_update", None)

    async def test_run_job_forwards_context_mode_and_projects_run_event(self) -> None:
        for stored_mode, expected_mode in (
            (None, "chat"),
            ("standalone", "standalone"),
        ):
            with self.subTest(stored_mode=stored_mode):
                store = agent_server.JobStore()
                job = {
                    "id": "job_context",
                    "session_id": "sess_context",
                    "title": "Context check",
                    "prompt": "Check now",
                    "schedule_kind": "interval",
                    "timezone": "UTC",
                    "enabled": False,
                    "run_count": 0,
                }
                if stored_mode is not None:
                    job["context_mode"] = stored_mode
                store.jobs[job["id"]] = job
                start_turn = AsyncMock(return_value={"run_id": "run_context"})
                events = AsyncMock()
                with (
                    patch.object(agent_server, "start_turn", start_turn),
                    patch.object(store, "mark_ran", new_callable=AsyncMock),
                    patch.object(agent_server, "append_event", events),
                ):
                    result = await store.run_job(job["id"])

                self.assertEqual(result["run_id"], "run_context")
                self.assertEqual(
                    start_turn.await_args.kwargs["provider_context_mode"],
                    expected_mode,
                )
                self.assertFalse(start_turn.await_args.kwargs["queue_if_busy"])
                self.assertEqual(
                    events.await_args.args[2]["context_mode"],
                    expected_mode,
                )

    async def test_load_migrates_and_runs_legacy_alternate_backend_standalone(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs_file = root / "jobs.json"
            jobs_file.write_text(json.dumps({
                "job_legacy_claude": {
                    "id": "job_legacy_claude",
                    "session_id": "sess_legacy_codex",
                    "title": "Legacy Claude job",
                    "prompt": "Check independently",
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "timezone": "UTC",
                    "enabled": False,
                    "backend": agent_server.BACKEND_CLAUDE,
                    "run_count": 0,
                }
            }))
            store = agent_server.JobStore()
            parent = {
                "id": "sess_legacy_codex",
                "backend": agent_server.BACKEND_CODEX,
            }
            start_turn = AsyncMock(return_value={"run_id": "run_legacy"})
            events = AsyncMock()
            with (
                patch.object(agent_server.STORE, "sessions", {
                    "sess_legacy_codex": parent,
                }),
                patch.object(agent_server, "STATE_DIR", root),
                patch.object(agent_server, "JOBS_FILE", jobs_file),
            ):
                await store.load()
                persisted = json.loads(jobs_file.read_text())
                self.assertEqual(
                    persisted["job_legacy_claude"]["context_mode"],
                    "standalone",
                )
                with (
                    patch.object(agent_server, "start_turn", start_turn),
                    patch.object(store, "mark_ran", new_callable=AsyncMock),
                    patch.object(agent_server, "append_event", events),
                ):
                    result = await store.run_job("job_legacy_claude")

            self.assertEqual(result["run_id"], "run_legacy")
            turn_request = start_turn.await_args.args[1]
            self.assertEqual(turn_request.backend, agent_server.BACKEND_CLAUDE)
            self.assertEqual(
                start_turn.await_args.kwargs["provider_context_mode"],
                "standalone",
            )

    async def test_scheduler_supervisor_restarts_after_escaped_iteration_error(
        self,
    ) -> None:
        store = agent_server.JobStore()
        iteration = AsyncMock(
            side_effect=[RuntimeError("one bad iteration"), asyncio.CancelledError()],
        )
        with patch.object(store, "_scheduler_loop_until_failure", iteration):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()
        self.assertEqual(iteration.await_count, 2)

    async def test_scheduler_rechecks_deleted_parent_after_blocker_yields(self) -> None:
        store = agent_server.JobStore()
        session_id = "sess_deleted_during_blocker"
        store.jobs["job_due"] = {
            "id": "job_due",
            "session_id": session_id,
            "title": "Delete race",
            "prompt": "Do not run",
            "enabled": True,
            "next_run_at": 1.0,
            "scheduled_run_at": 1.0,
            "_revision": agent_server.new_job_revision(),
        }
        sleep_count = 0

        async def one_scheduler_iteration(_delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 1:
                raise asyncio.CancelledError

        async def delete_parent_while_checking(
            _session_id: str,
            *,
            manual: bool = False,
        ) -> None:
            self.assertFalse(manual)
            agent_server.STORE.sessions.pop(session_id, None)
            return None

        cleanup = AsyncMock()
        run_job = AsyncMock()
        with patch.object(
            agent_server.STORE,
            "sessions",
            {session_id: {"id": session_id}},
        ), patch.object(
            agent_server.asyncio,
            "sleep",
            side_effect=one_scheduler_iteration,
        ), patch.object(agent_server.time, "time", return_value=2.0), \
             patch.object(
                 agent_server,
                 "scheduled_job_blocker",
                 side_effect=delete_parent_while_checking,
             ), patch.object(store, "delete_for_session", cleanup), \
             patch.object(store, "run_job", run_job):
            with self.assertRaises(asyncio.CancelledError):
                await store.scheduler_loop()

        cleanup.assert_awaited_once_with(session_id)
        run_job.assert_not_awaited()

    async def test_stop_scheduler_cancels_and_joins_owned_task(self) -> None:
        store = agent_server.JobStore()
        started = asyncio.Event()

        async def scheduler() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(scheduler())
        store._scheduler_task = task
        await started.wait()
        await store.stop_scheduler()

        self.assertTrue(task.cancelled())
        self.assertIsNone(store._scheduler_task)


if __name__ == "__main__":
    unittest.main()

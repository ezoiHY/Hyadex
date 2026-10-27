"""Removed cross-chat messaging leaves only deterministic legacy handling."""
from collections import deque
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import agent_server as server
from hyadex import inventory
from hyadex.providers import runtime


LEGACY_REFERENCE = {
    "session_id": "old-target",
    "display_title_snapshot": "Target",
    "source_text_start": 0,
    "source_text_end": 7,
    "action": "instruction",
}


def legacy_delivery_row(**overrides):
    row = {
        "queued_id": "queued-delivery",
        "prompt": "relay prompt",
        "display_prompt": "Incoming cross-chat message",
        "file_ids": [],
        "purpose": "cross_chat_handoff_delivery",
        "cross_chat_envelope_id": "handoff_old",
        "_durable": True,
        "_paused_after_stop": False,
    }
    row.update(overrides)
    return row


class RetiredCrossChatSurfaceTests(unittest.TestCase):
    def test_routes_helpers_and_files_are_gone(self):
        paths = [str(getattr(route, "path", "")) for route in server.app.routes]
        self.assertFalse([path for path in paths if any(name in path for name in (
            "cross-chat", "/inbox", "agent-handoff-routes", "handoff-digest",
            "skip-cross-chat-delivery", "/api/chats/search",
        ))])
        self.assertNotIn("chats", server.PROVIDER_TOOL_HELPERS)
        root = Path(server.__file__).resolve().parent
        for name in ("hyadex_chats.py", "hyadex/store/cross_chat.py", "hyadex/store/mailbox.py"):
            with self.subTest(name=name):
                self.assertFalse((root / name).exists())
                self.assertNotIn(name, inventory.FILES)

    def test_new_requests_reject_chat_routing_instead_of_dropping_it(self):
        for model, fields, field in (
            (server.TurnRequest, {"prompt": "hello"}, "chat_references"),
            (server.UpdateQueuedTurnRequest, {}, "chat_references"),
            (server.CreateJobRequest, {"session_id": "chat", "title": "Job", "prompt": "hello"}, "chat_references"),
            (server.UpdateJobRequest, {}, "chat_references"),
            (server.AgentCreateScopedJobRequest, {"title": "Job", "prompt": "hello"}, "chat_routes"),
            (server.AgentUpdateJobRequest, {}, "chat_routes"),
        ):
            with self.subTest(model=model.__name__, field=field):
                model(**fields, **{field: []})
                with self.assertRaises(ValidationError):
                    model(**fields, **{field: [LEGACY_REFERENCE]})

    def test_retired_runtime_names_are_scrubbed_but_never_issued(self):
        for name in (
            "HYADEX_CROSS_CHAT_MODE",
            "HYADEX_CROSS_CHAT_HANDLE_COUNT",
            "HYADEX_CROSS_CHAT_RESPONSE_EXCHANGE_ID",
            "HYADEX_PROVIDER_FINAL_RESULT_HANDOFF",
        ):
            with self.subTest(name=name):
                self.assertTrue(runtime.is_scrubbed_provider_runtime_env_name(name))
                with self.assertRaises(runtime.ProviderRuntimeContextError):
                    runtime.validate_provider_runtime_env({name: "1"})
        self.assertEqual(
            runtime.validate_provider_runtime_env({"HYADEX_CHAT_ID": "chat"}),
            {"HYADEX_CHAT_ID": "chat"},
        )

    def test_legacy_delivery_queue_events_stay_hidden_from_clients(self):
        row = legacy_delivery_row()
        payload = server.queued_turn_unqueued_payload("chat", row, "discarded")
        self.assertEqual(payload["cross_chat_envelope_id"], "handoff_old")
        self.assertFalse(server.is_client_visible_event({"type": "turn_unqueued", **payload}))
        user_payload = server.queued_turn_unqueued_payload(
            "chat", {"queued_id": "queued-user", "chat_references": [LEGACY_REFERENCE]}, "discarded",
        )
        self.assertTrue(server.is_client_visible_event({"type": "turn_unqueued", **user_payload}))
        self.assertNotIn("chat_references", user_payload)


class LegacyQueueRowTests(unittest.IsolatedAsyncioTestCase):
    def test_reducer_keeps_legacy_fields_so_the_row_can_be_discarded(self):
        session = {"backend": "claude", "model": "opus", "effort": "high"}
        delivery = server.queued_turn_from_event({
            "type": "turn_queued", "queued_id": "queued-delivery", "prompt": "relay",
            "purpose": "cross_chat_handoff_delivery", "cross_chat_envelope_id": "handoff_old",
            "chat_references": [], "cross_chat_exchange_status": False,
        }, session, 1)
        self.assertEqual(delivery["cross_chat_envelope_id"], "handoff_old")
        self.assertNotIn("chat_references", delivery)
        self.assertTrue(server.queued_turn_uses_retired_routing(delivery))

        routed = server.queued_turn_from_event({
            "type": "turn_queued", "queued_id": "queued-routed", "prompt": "@Target hi",
            "chat_references": [LEGACY_REFERENCE],
        }, session, 2)
        self.assertTrue(server.queued_turn_uses_retired_routing(routed))

        user = server.queued_turn_from_event(
            {"type": "turn_queued", "queued_id": "queued-user", "prompt": "hi",
             "provider_cross_chat_route_snapshot": [{"route_id": "route_" + "a" * 32}]},
            session,
            3,
        )
        self.assertFalse(server.queued_turn_uses_retired_routing(user))
        self.assertNotIn("provider_cross_chat_route_snapshot", user)
        self.assertEqual((user["backend"], user["model"], user["effort"]), ("claude", "opus", "high"))

    def test_legacy_edit_events_can_clear_or_keep_legacy_fields(self):
        session = {"backend": "codex"}
        events = [
            {"type": "turn_queued", "queued_id": "queued-routed", "prompt": "@Target hi",
             "chat_references": [LEGACY_REFERENCE]},
            {"type": "turn_queue_updated", "queued_id": "queued-routed", "prompt": "plain",
             "chat_references": []},
        ]
        path = Path(self._testMethodName + ".jsonl")
        with patch.object(server, "events_path", return_value=path), \
                patch.object(server, "queue_checkpoint_seed",
                             return_value=({"offset": 0, "end": 0}, [])), \
                patch.object(server, "save_queue_checkpoint"), \
                patch.object(server, "queued_event_lines",
                             return_value=[server.json.dumps(event).encode() + b"\n" for event in events]), \
                patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "stat", return_value=MagicMock(st_size=0)):
            recovered = server.scan_queued_turns_from_events([("chat", session)])
        row = recovered["chat"][0]
        self.assertNotIn("chat_references", row)
        self.assertFalse(server.queued_turn_uses_retired_routing(row))

    async def test_startup_recovery_discards_legacy_rows_and_keeps_user_rows(self):
        delivery = legacy_delivery_row()
        routed = {"queued_id": "queued-routed", "prompt": "@Target hi", "file_ids": [],
                  "chat_references": [LEGACY_REFERENCE], "_durable": True, "_paused_after_stop": False}
        digest = {"queued_id": "queued-digest", "prompt": "digest", "file_ids": [],
                  "purpose": "handoff_digest", "digest_job_id": "digest_old",
                  "_durable": True, "_paused_after_stop": False}
        user = {"queued_id": "queued-user", "prompt": "Keep me", "file_ids": [],
                "_durable": True, "_paused_after_stop": False}
        durable = AsyncMock(return_value={})
        queued: dict[str, deque] = {}
        with patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "backend": "codex"}}), \
                patch.object(server, "QUEUED_TURNS", queued), \
                patch.object(server, "scan_queued_turns_from_events",
                             return_value={"chat": [delivery, routed, digest, user]}), \
                patch.object(server, "append_durable_event", durable), \
                patch.object(server, "schedule_next_queued_turn") as schedule:
            rebuilt, scheduled = await server._recover_queued_turns_for_sessions([("chat", {})])

        self.assertEqual((rebuilt, scheduled), (1, 1))
        self.assertEqual([row["queued_id"] for row in queued["chat"]], ["queued-user"])
        schedule.assert_called_once_with("chat")
        unqueued = [call.args[2] for call in durable.await_args_list if call.args[1] == "turn_unqueued"]
        self.assertEqual(
            [payload["queued_id"] for payload in unqueued],
            ["queued-delivery", "queued-routed", "queued-digest"],
        )
        # The old delivery row stays hidden like its original turn_queued;
        # the user's own routed message is visibly removed.
        self.assertFalse(server.is_client_visible_event({"type": "turn_unqueued", **unqueued[0]}))
        self.assertTrue(server.is_client_visible_event({"type": "turn_unqueued", **unqueued[1]}))

    async def test_failed_discard_write_still_never_restores_the_row(self):
        queued: dict[str, deque] = {}
        with patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "backend": "codex"}}), \
                patch.object(server, "QUEUED_TURNS", queued), \
                patch.object(server, "scan_queued_turns_from_events",
                             return_value={"chat": [legacy_delivery_row()]}), \
                patch.object(server, "append_durable_event", AsyncMock(side_effect=OSError("disk"))), \
                patch.object(server, "schedule_next_queued_turn") as schedule:
            self.assertEqual(await server._recover_queued_turns_for_sessions([("chat", {})]), (0, 0))
        self.assertNotIn("chat", queued)
        schedule.assert_not_called()

    async def test_promotion_discards_a_legacy_row_without_admission(self):
        for row in (
            legacy_delivery_row(),
            {"queued_id": "queued-routed", "prompt": "@Target hi", "chat_references": [LEGACY_REFERENCE]},
            {"queued_id": "queued-mailbox", "prompt": "wake", "purpose": "chat_mailbox_wake"},
        ):
            with self.subTest(row=row["queued_id"]), \
                    patch.object(server, "QUEUED_TURNS", {"chat": deque([row])}), \
                    patch.object(server, "RUN_NOW_TURNS", {}), \
                    patch.object(server, "CURRENT_TURNS", {}), \
                    patch.object(server.STORE, "sessions", {"chat": {"id": "chat"}}), \
                    patch.object(server, "terminally_discard_queued_turn", AsyncMock()) as discard, \
                    patch.object(server, "_start_turn_locked", AsyncMock()) as start:
                await server._start_next_queued_turn_locked("chat", admission_backend="codex")
                discard.assert_awaited_once()
                self.assertIs(discard.await_args.args[1], row)
                start.assert_not_awaited()

    async def test_retired_purposes_cannot_be_admitted(self):
        for purpose in sorted(server.RETIRED_TURN_PURPOSES):
            with self.subTest(purpose=purpose), \
                    patch.object(server, "wait_for_queue_recovery_admission", AsyncMock()), \
                    self.assertRaises(HTTPException) as raised:
                await server._start_turn_locked(
                    "chat",
                    server.TurnRequest(prompt="hello", purpose=purpose),
                    queue_if_busy=False,
                )
            self.assertEqual(raised.exception.status_code, 410)

    async def test_user_rows_are_requeued_and_announced_once_on_deferral(self):
        item = {"queued_id": "queued-user", "prompt": "hello", "file_ids": [], "_durable": True}
        for failure in (HTTPException(status_code=409, detail="busy"), RuntimeError("bridge down")):
            queue = {"chat": deque([item])}
            append_event = AsyncMock()
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "backend": "codex"}}), \
                    patch.object(server, "QUEUED_TURNS", queue), \
                    patch.object(server, "RUN_NOW_TURNS", {}), \
                    patch.object(server, "STEERING_SESSIONS", set()), \
                    patch.object(server, "DELETING_SESSIONS", set()), \
                    patch.object(server, "DELETED_SESSION_TOMBSTONES", set()), \
                    patch.object(server, "_start_turn_locked", AsyncMock(side_effect=failure)), \
                    patch.object(server, "append_event", append_event), \
                    patch.object(server, "terminally_discard_queued_turn", AsyncMock()) as discard, \
                    patch.object(server, "schedule_queued_turn_retry", MagicMock()):
                item.pop("_turn_deferred_notified", None)
                await server._start_next_queued_turn_locked("chat", admission_backend=None)
                await server._start_next_queued_turn_locked("chat", admission_backend=None)
            discard.assert_not_awaited()
            self.assertEqual(list(queue["chat"]), [item])
            deferred = [call for call in append_event.await_args_list if call.args[1] == "turn_deferred"]
            self.assertEqual(len(deferred), 1)


class LegacyJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_editing_the_prompt_clears_saved_chat_targets(self):
        store = server.JobStore()
        store.jobs["job"] = {
            "id": "job", "session_id": "chat", "title": "Routed", "prompt": "@Target check",
            "chat_references": [LEGACY_REFERENCE], "schedule_kind": "interval",
            "interval_seconds": 300, "timezone": "UTC", "loop": True, "enabled": False,
            "_revision": server.new_job_revision(),
        }
        with patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "backend": "codex"}}), \
                patch.object(store, "save", AsyncMock()), \
                patch.object(server, "append_event", AsyncMock()):
            renamed = await store.update("job", {"title": "Renamed"})
            self.assertEqual(renamed["chat_references"], [LEGACY_REFERENCE])
            edited = await store.update("job", {"prompt": "Check without a target"})
        self.assertNotIn("chat_references", edited)
        self.assertNotIn("chat_references", store.jobs["job"])

    async def test_new_jobs_never_store_chat_targets(self):
        store = server.JobStore()
        with patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "backend": "codex"}}), \
                patch.object(store, "save", AsyncMock()), \
                patch.object(server, "append_event", AsyncMock()):
            job = await store.create(server.CreateJobRequest(
                session_id="chat", title="Plain", prompt="Run", interval_seconds=300,
            ))
        self.assertNotIn("chat_references", job)


if __name__ == "__main__":
    unittest.main()

"""Queue rows held by Stop: resume route, held-row scheduling, durability and recovery."""
import asyncio
from collections import OrderedDict, deque
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from tests import hyadex_server
from tests.runtime_state import patch_server_multiple

SESSION_ID = "chat-1"


def queued(queued_id: str, prompt: str) -> dict[str, object]:
    return {
        "queued_id": queued_id,
        "prompt": prompt,
        "file_ids": [],
        "backend": "codex",
        "_durable": True,
        "_paused_after_stop": False,
    }


def queued_event(seq: int, queued_id: str, prompt: str) -> dict[str, object]:
    return {
        "seq": seq,
        "type": "turn_queued",
        "queued_id": queued_id,
        "prompt": prompt,
        "request_prompt": prompt,
        "file_ids": [],
    }


def rebuild_from(events: list[dict[str, object]]) -> list[dict[str, object]]:
    hyadex_server.RUNS.queued.pop(SESSION_ID, None)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "events.jsonl"
        path.write_text("".join(json.dumps({
            "id": f"event-{index}",
            "session_id": SESSION_ID,
            "ts": "2026-10-28T00:00:00Z",
            **event,
            "seq": index,
        }) + "\n" for index, event in enumerate(events, start=1)))
        with patch.object(hyadex_server, "events_path", return_value=path):
            hyadex_server.rebuild_queued_turns_from_events()
    return list(hyadex_server.RUNS.queued.get(SESSION_ID) or ())


class HeldQueueFixture(unittest.IsolatedAsyncioTestCase):
    """An idle Codex chat with two durable queued rows; providers never start."""

    async def asyncSetUp(self) -> None:
        self.session = {"id": SESSION_ID, "title": "Chat", "backend": hyadex_server.BACKEND_CODEX}
        self.events: list[tuple[str, dict[str, object]]] = []
        self.fail_event_types: set[str] = set()

        async def record_durable_event(
            _session_id: str,
            event_type: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            if event_type in self.fail_event_types:
                raise OSError("fixture storage failure")
            self.events.append((event_type, dict(payload)))
            return {"type": event_type, **payload}

        self.state = ExitStack()
        self.state.enter_context(patch.object(hyadex_server.STORE, "sessions", {SESSION_ID: self.session}))
        self.state.enter_context(patch_server_multiple(
            hyadex_server,
            ACTIVE={},
            BUSY_SESSIONS=set(),
            CURRENT_TURNS={},
            STOP_REQUESTS=set(),
            STOPPED_RUNS=set(),
            EXPLICIT_STOP_OPERATIONS={},
            DETACHED_STOP_TASKS_BY_SESSION={},
            SESSION_TURN_TASKS={},
            CODEX_NATIVE_ACTION_TASKS={},
            QUEUED_TURNS={SESSION_ID: deque([
                queued("queued-first", "First follow-up"),
                queued("queued-second", "Second follow-up"),
            ])},
            RUN_NOW_TURNS={},
            RUN_NOW_REQUESTS={},
            STEERING_SESSIONS=set(),
            STEERING_WAIT_TASKS={},
            QUEUE_START_TASKS={},
            SESSION_LIFECYCLE_LOCKS={},
            CODEX_APP_SERVER_MANAGER=None,
            append_durable_event=AsyncMock(side_effect=record_durable_event),
            cancel_codex_interactions=AsyncMock(),
            cancel_claude_interactions=AsyncMock(),
            settle_idle_codex_goal_for_stop=AsyncMock(return_value={}),
            server_lifecycle_admission_blocker=lambda: None,
        ))
        self.state.enter_context(patch.object(hyadex_server, "RUN_NOW_COMPLETED_RESULTS", OrderedDict()))
        self.schedule = self.state.enter_context(patch.object(hyadex_server, "schedule_next_queued_turn"))
        self.real_start_turn_locked = hyadex_server._start_turn_locked
        self.start = self.state.enter_context(
            patch.object(hyadex_server, "_start_turn_locked", new_callable=AsyncMock)
        )

    async def asyncTearDown(self) -> None:
        self.state.close()

    def queue(self) -> list[dict[str, object]]:
        return list(hyadex_server.RUNS.queued.get(SESSION_ID) or ())

    def event_types(self) -> list[str]:
        return [event_type for event_type, _payload in self.events]

    def held(self) -> list[tuple[str, bool]]:
        return [(str(item["queued_id"]), bool(item["_paused_after_stop"])) for item in self.queue()]

    async def stop(self) -> None:
        result = await hyadex_server.stop_turn_endpoint(SESSION_ID)
        self.assertTrue(result["ok"])
        self.assertFalse(hyadex_server.stop_cleanup_in_progress(SESSION_ID))
        self.schedule.reset_mock()

    async def start_queued(self) -> str | None:
        """Run the scheduler once; return the queued id it started, if any."""
        self.start.reset_mock()
        await hyadex_server.start_next_queued_turn(SESSION_ID)
        if not self.start.await_count:
            return None
        return self.start.await_args.kwargs["queued_id"]


class QueueResumeTests(HeldQueueFixture):
    async def test_stop_holds_queue_and_resume_releases_it_in_order(self) -> None:
        await self.stop()
        self.assertEqual(self.event_types(), ["turn_queue_paused"])
        self.assertEqual(self.events[0][1]["queued_ids"], ["queued-first", "queued-second"])
        self.assertTrue(all(item["_paused_after_stop"] for item in self.queue()))
        await hyadex_server.start_next_queued_turn(SESSION_ID)
        self.start.assert_not_awaited()

        response = await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(response, {
            "ok": True,
            "resumed": ["queued-first", "queued-second"],
            "held": [],
            "remaining": 2,
        })
        self.assertEqual(self.event_types(), ["turn_queue_paused", "turn_queue_resumed"])
        self.assertEqual(self.events[1][1], {
            "queued_ids": ["queued-first", "queued-second"],
            "message": "Queued messages held after Stop were resumed.",
        })
        self.assertFalse(any(item["_paused_after_stop"] for item in self.queue()))
        rows = await hyadex_server.queued_turns_snapshot(SESSION_ID)
        self.assertEqual([(row["paused"], row["pause_reason"]) for row in rows], [(False, None)] * 2)
        # The idle chat continues with the head of the queue.
        self.schedule.assert_called_once_with(SESSION_ID)
        await hyadex_server.start_next_queued_turn(SESSION_ID)
        self.start.assert_awaited_once()
        self.assertEqual(self.start.await_args.kwargs["queued_id"], "queued-first")
        self.assertEqual([item["queued_id"] for item in self.queue()], ["queued-second"])

        # The events this server wrote rebuild an unheld queue after restart.
        recovered = rebuild_from([
            queued_event(1, "queued-first", "First follow-up"),
            queued_event(2, "queued-second", "Second follow-up"),
            *({"type": event_type, **payload} for event_type, payload in self.events),
        ])
        self.assertEqual(
            [(item["queued_id"], item["_paused_after_stop"]) for item in recovered],
            [("queued-first", False), ("queued-second", False)],
        )

    async def test_delivery_fenced_item_stays_held_without_blocking_the_rest(self) -> None:
        await self.stop()
        fenced = self.queue()[0]
        fenced["_native_delivery_fenced"] = True

        response = await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(response["resumed"], ["queued-second"])
        self.assertEqual(response["held"], ["queued-first"])
        self.assertEqual(self.events[-1], ("turn_queue_resumed", {
            "queued_ids": ["queued-second"],
            "message": "Queued messages held after Stop were resumed.",
        }))
        self.assertTrue(fenced["_paused_after_stop"])
        rows = await hyadex_server.queued_turns_snapshot(SESSION_ID)
        self.assertEqual(
            [(row["queued_id"], row["paused"], row["pause_reason"]) for row in rows],
            [("queued-first", True, "delivery_uncertain"), ("queued-second", False, None)],
        )
        # The fenced row keeps its place; the released row runs past it.
        self.schedule.assert_called_once_with(SESSION_ID)
        self.assertEqual(await self.start_queued(), "queued-second")
        self.assertEqual(self.held(), [("queued-first", True)])

    async def test_failed_durable_write_keeps_every_hold(self) -> None:
        await self.stop()
        self.fail_event_types.add("turn_queue_resumed")

        with self.assertRaises(HTTPException) as raised:
            await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("remain held", str(raised.exception.detail))
        self.assertEqual(self.event_types(), ["turn_queue_paused"])
        self.assertTrue(all(item["_paused_after_stop"] for item in self.queue()))
        self.schedule.assert_not_called()
        await hyadex_server.start_next_queued_turn(SESSION_ID)
        self.start.assert_not_awaited()

    async def test_resume_after_unsaved_stop_hold_restores_restart_durability(self) -> None:
        self.fail_event_types.add("turn_queue_paused")
        await self.stop()
        self.assertTrue(all(item["_paused_after_stop"] for item in self.queue()))
        self.assertTrue(all(item["_durable"] is False for item in self.queue()))
        self.fail_event_types.clear()

        response = await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(response["resumed"], ["queued-first", "queued-second"])
        self.assertTrue(all(
            item["_durable"] is True and item["_paused_after_stop"] is False
            for item in self.queue()
        ))
        async with hyadex_server.RUNS.queue_lock:
            self.assertEqual(hyadex_server.restart_blocking_queued_turn_count_locked(), 0)
        self.schedule.assert_called_once_with(SESSION_ID)

    async def test_resume_while_a_turn_runs_releases_without_starting(self) -> None:
        await self.stop()
        hyadex_server.RUNS.busy.add(SESSION_ID)

        response = await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(response["resumed"], ["queued-first", "queued-second"])
        self.assertFalse(any(item["_paused_after_stop"] for item in self.queue()))
        # The running turn schedules its successor when it finishes.
        self.schedule.assert_not_called()

    async def test_resume_with_nothing_held_is_an_idempotent_no_op(self) -> None:
        await self.stop()
        first = await hyadex_server.post_resume_queue(SESSION_ID)
        second = await hyadex_server.post_resume_queue(SESSION_ID)

        self.assertEqual(first["resumed"], ["queued-first", "queued-second"])
        self.assertEqual(second, {"ok": True, "resumed": [], "held": [], "remaining": 2})
        self.assertEqual(self.event_types(), ["turn_queue_paused", "turn_queue_resumed"])

        hyadex_server.RUNS.queued.pop(SESSION_ID)
        empty = await hyadex_server.post_resume_queue(SESSION_ID)
        self.assertEqual(empty, {"ok": True, "resumed": [], "held": [], "remaining": 0})

    async def test_unknown_session_is_404(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await hyadex_server.post_resume_queue("missing-chat")
        self.assertEqual(raised.exception.status_code, 404)

    async def test_fenced_states_refuse_without_releasing(self) -> None:
        await self.stop()
        release = asyncio.Event()
        stop_operation = asyncio.create_task(release.wait())
        force_send = asyncio.create_task(release.wait())
        cases = {
            "stop_cleanup": (409, lambda: hyadex_server.RUNS.explicit_stops.__setitem__(SESSION_ID, stop_operation),
                             lambda: hyadex_server.RUNS.explicit_stops.pop(SESSION_ID)),
            "force_send": (409, lambda: hyadex_server.RUNS.run_now_requests.__setitem__(
                SESSION_ID, ("queued-first", force_send)),
                lambda: hyadex_server.RUNS.run_now_requests.pop(SESSION_ID)),
            "archived": (409, lambda: self.session.__setitem__("archived", True),
                         lambda: self.session.pop("archived")),
            "deleting": (409, lambda: hyadex_server.DELETING_SESSIONS.add(SESSION_ID),
                         lambda: hyadex_server.DELETING_SESSIONS.discard(SESSION_ID)),
            "retired_runtime": (410, lambda: self.session.__setitem__("backend", "gemini"),
                                lambda: self.session.__setitem__("backend", hyadex_server.BACKEND_CODEX)),
        }
        try:
            for name, (status, enter, leave) in cases.items():
                with self.subTest(fence=name):
                    enter()
                    try:
                        with self.assertRaises(HTTPException) as raised:
                            await hyadex_server.post_resume_queue(SESSION_ID)
                    finally:
                        leave()
                    self.assertEqual(raised.exception.status_code, status)
                    self.assertEqual(self.event_types(), ["turn_queue_paused"])
                    self.assertTrue(all(item["_paused_after_stop"] for item in self.queue()))
            with patch.object(
                hyadex_server,
                "server_lifecycle_admission_blocker",
                lambda: "server restart in progress",
            ):
                with self.assertRaises(HTTPException) as raised:
                    await hyadex_server.post_resume_queue(SESSION_ID)
            self.assertEqual(raised.exception.status_code, 503)
            self.assertTrue(all(item["_paused_after_stop"] for item in self.queue()))
        finally:
            release.set()
            await asyncio.gather(stop_operation, force_send)
        self.schedule.assert_not_called()

    async def test_route_is_a_user_route_and_advertised(self) -> None:
        path = f"/api/sessions/{SESSION_ID}/queue/resume"
        self.assertFalse(hyadex_server.is_agent_helper_route("POST", path))
        methods = {
            method
            for route in hyadex_server.app.routes
            if getattr(route, "path", None) == "/api/sessions/{session_id}/queue/resume"
            for method in getattr(route, "methods", set())
        }
        self.assertEqual(methods, {"POST"})
        auth = hyadex_server.route_auth_for_scope(hyadex_server.app, "POST", path)
        self.assertEqual(auth.kind, "user")
        health = await hyadex_server.health()
        self.assertEqual(health["capabilities"]["queue_resume_v1"]["version"], 1)
        self.assertTrue(health["capabilities"]["queue_resume_v1"]["available"])


class HeldQueueRowsTests(HeldQueueFixture):
    """After Stop, held rows go back to the user and never block new work."""

    LAUNCH_GATE = "fixture stops before provider launch"

    async def post_message(self, prompt: str) -> dict[str, object]:
        """POST a user turn through real admission, stopping before launch.

        The provider launch check runs only after admission reserved the
        chat's slot, so reaching it proves the message started at once.
        """
        observed: dict[str, object] = {}

        async def launch_gate(*, ignore_session_id: str | None = None) -> str:
            current = hyadex_server.RUNS.current_turns.get(SESSION_ID) or {}
            observed.update({
                "reserved": SESSION_ID in hyadex_server.RUNS.busy,
                "prompt": current.get("prompt"),
                "queued_id": current.get("queued_id"),
            })
            return self.LAUNCH_GATE

        with (
            patch.object(hyadex_server, "_start_turn_locked", self.real_start_turn_locked),
            patch.object(hyadex_server, "turn_start_blocker", launch_gate),
            patch.object(hyadex_server, "prepare_codex_login_turn", AsyncMock()),
        ):
            try:
                return await hyadex_server.post_turn(
                    SESSION_ID,
                    hyadex_server.TurnRequest(prompt=prompt),
                )
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 503)
                self.assertIn(self.LAUNCH_GATE, str(exc.detail))
                return {"started": True, **observed}

    async def test_idle_send_after_stop_starts_now_and_leaves_held_rows(self) -> None:
        await self.stop()

        result = await self.post_message("Do this instead")

        self.assertEqual(result, {
            "started": True,
            "reserved": True,
            "prompt": "Do this instead",
            "queued_id": None,
        })
        self.assertNotIn("turn_queued", self.event_types())
        # The held rows stay held, in place, for the user to act on.
        self.assertEqual(self.held(), [("queued-first", True), ("queued-second", True)])

    async def test_send_behind_runnable_rows_still_queues_in_order(self) -> None:
        # Without a Stop the queue is runnable, so a new message waits its turn.
        result = await self.post_message("Third follow-up")

        self.assertTrue(result["queued"])
        self.assertEqual(result["position"], 3)

    async def test_busy_send_runs_after_current_turn_while_held_rows_stay(self) -> None:
        await self.stop()
        hyadex_server.RUNS.busy.add(SESSION_ID)

        result = await self.post_message("Third follow-up")

        self.assertTrue(result["queued"])
        self.assertEqual(result["position"], 3)
        third = str(result["queued_id"])
        hyadex_server.RUNS.busy.discard(SESSION_ID)
        # When the running turn ends the scheduler skips the held rows.
        self.assertEqual(await self.start_queued(), third)
        self.assertEqual(self.held(), [("queued-first", True), ("queued-second", True)])
        self.assertIsNone(await self.start_queued())

        # Resuming afterwards still runs the held rows in their order.
        response = await hyadex_server.post_resume_queue(SESSION_ID)
        self.assertEqual(response["resumed"], ["queued-first", "queued-second"])
        self.assertEqual(await self.start_queued(), "queued-first")
        self.assertEqual(await self.start_queued(), "queued-second")
        self.assertEqual(self.queue(), [])

    async def test_runnable_rows_keep_their_order_around_held_rows(self) -> None:
        hyadex_server.RUNS.queued[SESSION_ID] = deque([
            {**queued("held-1", "Held one"), "_paused_after_stop": True},
            queued("run-1", "Run one"),
            {**queued("held-2", "Held two"), "_paused_after_stop": True},
            queued("run-2", "Run two"),
        ])
        self.assertTrue(await hyadex_server.reconcile_idle_queue_session(
            SESSION_ID, schedule=True, reason="test",
        ))
        self.schedule.assert_called_once_with(SESSION_ID)

        # A deferred start returns the row to its own place, not the front.
        self.start.side_effect = HTTPException(status_code=409, detail="fixture busy")
        with (
            patch.object(hyadex_server, "schedule_queued_turn_retry"),
            patch.object(hyadex_server, "append_event", new_callable=AsyncMock),
        ):
            self.assertEqual(await self.start_queued(), "run-1")
        self.assertEqual(
            [item["queued_id"] for item in self.queue()],
            ["held-1", "run-1", "held-2", "run-2"],
        )
        self.start.side_effect = None

        self.assertEqual(await self.start_queued(), "run-1")
        self.assertEqual(await self.start_queued(), "run-2")
        self.assertIsNone(await self.start_queued())
        self.assertEqual([item["queued_id"] for item in self.queue()], ["held-1", "held-2"])

    async def test_only_held_rows_schedule_nothing(self) -> None:
        await self.stop()
        self.assertFalse(await hyadex_server.reconcile_idle_queue_session(
            SESSION_ID, schedule=True, reason="test",
        ))
        self.schedule.assert_not_called()
        self.assertIsNone(await self.start_queued())

    async def test_restart_replay_keeps_held_rows_and_continues_the_rest(self) -> None:
        events = [
            queued_event(1, "queued-first", "First follow-up"),
            queued_event(2, "queued-second", "Second follow-up"),
            {"type": "turn_queue_paused", "queued_ids": ["queued-first", "queued-second"]},
            queued_event(4, "queued-third", "Third follow-up"),
        ]
        hyadex_server.RUNS.queued.pop(SESSION_ID, None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps({
                "id": f"event-{index}", "session_id": SESSION_ID,
                "ts": "2026-10-28T00:00:00Z", **event, "seq": index,
            }) + "\n" for index, event in enumerate(events, start=1)))
            with patch.object(hyadex_server, "events_path", return_value=path):
                rebuilt, scheduled = await hyadex_server._recover_queued_turns_for_sessions(
                    [(SESSION_ID, dict(self.session))],
                )

        self.assertEqual((rebuilt, scheduled), (3, 1))
        self.schedule.assert_called_once_with(SESSION_ID)
        self.assertEqual(
            self.held(),
            [("queued-first", True), ("queued-second", True), ("queued-third", False)],
        )
        self.assertEqual(await self.start_queued(), "queued-third")

    async def test_restart_replay_with_only_held_rows_schedules_nothing(self) -> None:
        events = [
            queued_event(1, "queued-first", "First follow-up"),
            {"type": "turn_queue_paused", "queued_ids": ["queued-first"]},
        ]
        hyadex_server.RUNS.queued.pop(SESSION_ID, None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps({
                "id": f"event-{index}", "session_id": SESSION_ID,
                "ts": "2026-10-28T00:00:00Z", **event, "seq": index,
            }) + "\n" for index, event in enumerate(events, start=1)))
            with patch.object(hyadex_server, "events_path", return_value=path):
                rebuilt, scheduled = await hyadex_server._recover_queued_turns_for_sessions(
                    [(SESSION_ID, dict(self.session))],
                )

        self.assertEqual((rebuilt, scheduled), (1, 0))
        self.schedule.assert_not_called()

    def test_waiting_work_queries(self) -> None:
        runs = hyadex_server.RunState()
        runs.queued[SESSION_ID] = deque([{**queued("held", "Held"), "_paused_after_stop": True}])
        self.assertTrue(runs.has_waiting_work(SESSION_ID))
        self.assertFalse(runs.has_runnable_waiting_work(SESSION_ID))
        runs.queued[SESSION_ID].append({**queued("fenced", "Fenced"), "_native_delivery_fenced": True})
        self.assertFalse(runs.has_runnable_waiting_work(SESSION_ID))
        runs.queued[SESSION_ID].append(queued("runnable", "Runnable"))
        self.assertTrue(runs.has_runnable_waiting_work(SESSION_ID))
        self.assertEqual(hyadex_server.first_runnable_queued_index(runs.queued[SESSION_ID]), 2)
        runs.queued.clear()
        runs.steering.add(SESSION_ID)
        self.assertTrue(runs.has_runnable_waiting_work(SESSION_ID))


class QueueResumeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ExitStack()
        self.state.enter_context(patch.object(
            hyadex_server.STORE,
            "sessions",
            {SESSION_ID: {"id": SESSION_ID, "backend": "codex"}},
        ))
        self.state.enter_context(patch_server_multiple(hyadex_server, QUEUED_TURNS={}))

    def tearDown(self) -> None:
        self.state.close()

    def test_replayed_resume_releases_stop_holds(self) -> None:
        recovered = rebuild_from([
            queued_event(1, "queued-first", "First"),
            queued_event(2, "queued-second", "Second"),
            {"type": "turn_queue_paused", "queued_ids": ["queued-first", "queued-second"]},
            {"type": "turn_queue_resumed", "queued_ids": ["queued-first", "queued-second"]},
        ])

        self.assertEqual(
            [(item["queued_id"], item["_paused_after_stop"]) for item in recovered],
            [("queued-first", False), ("queued-second", False)],
        )

    def test_replayed_resume_never_releases_a_delivery_fence(self) -> None:
        recovered = rebuild_from([
            queued_event(1, "queued-first", "First"),
            queued_event(2, "queued-second", "Second"),
            {"type": "turn_queue_paused", "queued_ids": ["queued-first", "queued-second"]},
            {"type": "turn_unqueued", "queued_id": "queued-first", "reason": "native_delivery_fence"},
            {
                **queued_event(0, "queued-first", "First"),
                "type": "turn_queue_delivery_fenced",
                "position": 1,
            },
            {"type": "turn_queue_resumed", "queued_ids": ["queued-first", "queued-second"]},
        ])

        by_id = {item["queued_id"]: item for item in recovered}
        self.assertTrue(by_id["queued-first"]["_paused_after_stop"])
        self.assertTrue(by_id["queued-first"]["_native_delivery_fenced"])
        self.assertFalse(by_id["queued-second"]["_paused_after_stop"])

    def test_a_later_stop_holds_again_after_a_resume(self) -> None:
        recovered = rebuild_from([
            queued_event(1, "queued-first", "First"),
            {"type": "turn_queue_paused", "queued_ids": ["queued-first"]},
            {"type": "turn_queue_resumed", "queued_ids": ["queued-first"]},
            {"type": "turn_queue_paused", "queued_ids": ["queued-first"]},
        ])

        self.assertTrue(recovered[0]["_paused_after_stop"])

    def test_resume_event_is_hidden_from_timeline_and_client_internal(self) -> None:
        self.assertIn("turn_queue_resumed", hyadex_server.TIMELINE_INDEX_HIDDEN_TYPES)
        self.assertIn("turn_queue_resumed", hyadex_server.CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES)


if __name__ == "__main__":
    unittest.main()

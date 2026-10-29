"""Chat consults: who may ask whom, basic and deep answers, approvals, limits and lifetime."""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hyadex.api.consults import create_consult_router
from hyadex.features import consult_engine
from hyadex.features.consult_engine import ChatConsults, ConsultError, same_project, split_confidence


def chats() -> dict[str, dict]:
    return {
        "a": {"id": "a", "title": "Mac app", "backend": "claude", "folder": "Hyadex", "cwd": "/w/app"},
        "b": {"id": "b", "title": "Backend", "backend": "codex", "folder": "Hyadex", "cwd": "/w/server"},
        "c": {"id": "c", "title": "Notes", "backend": "claude", "folder": "General", "cwd": "/w/app"},
        "d": {"id": "d", "title": "Other project", "backend": "codex", "folder": "Elsewhere", "cwd": "/x"},
        "e": {"id": "e", "title": "Old", "backend": "claude", "folder": "Hyadex", "cwd": "/w", "archived": True},
        "f": {"id": "f", "title": "Member", "backend": "claude", "folder": "Hyadex", "cwd": "/w",
              "group_member": {"group_id": "g"}},
        "h": {"id": "h", "title": "Private", "backend": "claude", "folder": "Hyadex", "cwd": "/w",
              "consult_inbound": "off"},
        "i": {"id": "i", "title": "Basic only", "backend": "codex", "folder": "Hyadex", "cwd": "/w",
              "consult_inbound": "basic"},
    }


class Executor:
    """Answers with a canned text; `gate` holds the answer until released."""

    def __init__(self, answer: str = "The port is 7872.\nCONFIDENCE: high"):
        self.answer = answer
        self.calls: list[tuple[str, str, str]] = []
        self.gate: asyncio.Event | None = None
        self.running = 0
        self.most_running = 0

    async def __call__(self, mode, sess, prompt, on_step):
        self.calls.append((mode, sess["id"], prompt))
        self.running += 1
        self.most_running = max(self.most_running, self.running)
        try:
            on_step({"kind": "read", "title": "server.py"})
            if self.gate is not None:
                await self.gate.wait()
            return {"answer": self.answer, "usage": {"input_tokens": 10, "output_tokens": 2}}
        finally:
            self.running -= 1


class ConsultEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = chats()
        self.executor = Executor()
        self.remembered: list[tuple[str, str]] = []
        self.notices: list[tuple[str, dict]] = []

        async def remember(source, target):
            self.remembered.append((source, target))
            self.sessions[source].setdefault("consult_deep_allowed", []).append(target)

        async def notify(session_id, event):
            self.notices.append((session_id, event))

        self.marked: list[dict] = []

        async def record_question(record):
            self.marked.append(record)

        self.engine = ChatConsults(None, sessions=lambda: self.sessions, execute=self.executor,
                                   remember_deep_allowed=remember, notify=notify, record_question=record_question)

    async def asyncTearDown(self):
        await self.engine.close()

    async def finished(self, consult_id: str) -> dict:
        return await self.engine.wait(consult_id, source_id="a", timeout=5)

    async def test_project_scope_and_inbound_policy(self):
        titles = {chat["title"]: chat["modes"] for chat in self.engine.targets("a")}
        # Same project folder, or no project but the same working directory.
        self.assertEqual(set(titles), {"Backend", "Notes", "Basic only"})
        self.assertEqual(titles["Basic only"], ["basic"])
        self.assertEqual(titles["Backend"], ["basic", "deep"])
        self.assertTrue(same_project({"folder": "General", "cwd": "/w/app/"}, {"cwd": "/w/app"}))
        self.assertFalse(same_project({"folder": "One", "cwd": "/w/1"}, {"folder": "Two", "cwd": "/w/2"}))

    async def test_basic_answer_comes_back_with_its_confidence(self):
        self.executor.answer = "It listens on 7872, I think.\n\nCONFIDENCE: low"
        started = await self.engine.start(source_id="a", run_id="run1", target="backend", mode="basic",
                                          question="Which port?", lease=True)
        self.assertEqual(started["target"]["id"], "b")
        record = await self.finished(started["id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["answer"], "It listens on 7872, I think.")
        self.assertIs(record["confident"], False)
        self.assertTrue(record["suggest_deep"])
        mode, target, prompt = self.executor.calls[0]
        self.assertEqual((mode, target), ("basic", "b"))
        self.assertIn('Another chat, "Mac app",', prompt)
        self.assertIn("Do not use tools", prompt)
        self.assertTrue(prompt.endswith("Which port?"))
        self.assertEqual(record["steps"][0]["title"], "server.py")
        # Both chats hear about it, so both can show it.
        self.assertEqual({session for session, _ in self.notices}, {"a", "b"})

    async def test_chats_outside_the_project_or_closed_cannot_be_asked(self):
        for reference, status in (("Other project", 404), ("d", 403), ("Old", 404), ("f", 403),
                                  ("Private", 404), ("nobody", 404)):
            with self.subTest(reference=reference):
                with self.assertRaises(ConsultError) as raised:
                    await self.engine.start(source_id="a", target=reference, mode="basic", question="Hi?")
                self.assertEqual(raised.exception.status, status)
        with self.assertRaises(ConsultError) as raised:
            await self.engine.start(source_id="a", target="Basic only", mode="deep", question="Check?")
        self.assertEqual(raised.exception.status, 403)

    async def test_two_chats_with_one_title_must_be_asked_by_id(self):
        self.sessions["c"]["title"] = "Backend"
        with self.assertRaises(ConsultError) as raised:
            await self.engine.start(source_id="a", target="Backend", mode="basic", question="Hi?")
        self.assertEqual(raised.exception.status, 409)
        started = await self.engine.start(source_id="a", target="c", mode="basic", question="Hi?")
        self.assertEqual(started["target"]["id"], "c")

    async def test_deep_waits_for_approval_and_always_skips_the_next_one(self):
        started = await self.engine.start(source_id="a", run_id="run1", target="b", mode="deep",
                                          question="Is the lock held while fsync runs?")
        self.assertEqual(started["status"], "awaiting_approval")
        self.assertEqual(self.executor.calls, [])
        await self.engine.decide(started["id"], source_id="a", decision="always")
        record = await self.finished(started["id"])
        self.assertEqual(record["status"], "completed")
        self.assertEqual(self.remembered, [("a", "b")])
        self.assertIn("read-only copy", self.executor.calls[0][2])
        again = await self.engine.start(source_id="a", run_id="run1", target="b", mode="deep", question="And now?")
        self.assertEqual(again["status"], "queued")

    async def test_denied_and_unanswered_approvals_end_without_running(self):
        denied = await self.engine.start(source_id="a", target="b", mode="deep", question="Look?")
        await self.engine.decide(denied["id"], source_id="a", decision="deny")
        self.assertEqual((await self.finished(denied["id"]))["status"], "denied")
        with mock.patch.object(consult_engine, "APPROVAL_TIMEOUT_SECONDS", 0.05):
            waiting = await self.engine.start(source_id="a", target="b", mode="deep", question="Look again?")
            record = await self.finished(waiting["id"])
        self.assertEqual(record["status"], "expired")
        self.assertEqual(self.executor.calls, [])
        self.sessions["a"]["consult_deep_approval"] = False
        free = await self.engine.start(source_id="a", target="b", mode="deep", question="No approval?")
        self.assertEqual(free["status"], "queued")

    async def test_each_turn_has_a_limit_per_mode(self):
        self.sessions["a"]["consult_deep_approval"] = False
        for number in range(consult_engine.TURN_LIMITS["basic"]):
            await self.engine.start(source_id="a", run_id="run1", target="b", mode="basic", question=f"Q{number}?")
        with self.assertRaises(ConsultError) as raised:
            await self.engine.start(source_id="a", run_id="run1", target="b", mode="basic", question="One more?")
        self.assertEqual(raised.exception.status, 429)
        # Another turn starts afresh.
        await self.engine.start(source_id="a", run_id="run2", target="b", mode="basic", question="Next turn?")

    async def test_one_deep_fork_per_chat_at_a_time(self):
        self.sessions["a"]["consult_deep_approval"] = False
        self.executor.gate = asyncio.Event()
        first = await self.engine.start(source_id="a", target="b", mode="deep", question="One?")
        second = await self.engine.start(source_id="a", target="b", mode="deep", question="Two?")
        await asyncio.sleep(0.05)
        self.assertEqual(self.executor.most_running, 1)
        self.assertEqual(self.engine.records[second["id"]]["status"], "queued")
        self.executor.gate.set()
        for consult in (first, second):
            self.assertEqual((await self.finished(consult["id"]))["status"], "completed")
        self.assertEqual(self.executor.most_running, 1)

    async def test_a_turn_that_stops_waiting_or_ends_cancels_its_consults(self):
        self.executor.gate = asyncio.Event()
        with mock.patch.object(consult_engine, "LEASE_SECONDS", 0.05), \
                mock.patch.object(consult_engine, "LEASE_CHECK_SECONDS", 0.02):
            abandoned = await self.engine.start(source_id="a", run_id="run1", target="b", mode="basic",
                                                question="Anyone?", lease=True)
            await asyncio.sleep(0.3)
        record = (await self.engine.wait(abandoned["id"], source_id="a"))
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("stopped waiting", record["error"]["message"])
        stopped = await self.engine.start(source_id="a", run_id="run2", target="b", mode="basic", question="Still?")
        self.engine.run_ended("a", "run2")
        record = await self.finished(stopped["id"])
        self.assertEqual(record["status"], "cancelled")
        self.assertIn("turn ended", record["error"]["message"])

    async def test_slow_or_failing_answers_end_the_consult_only(self):
        self.executor.gate = asyncio.Event()
        with mock.patch.dict(consult_engine.RUN_TIMEOUTS, {"basic": 0.05}):
            slow = await self.engine.start(source_id="a", target="b", mode="basic", question="Slow?")
            self.assertEqual((await self.finished(slow["id"]))["status"], "failed")

        async def broken(mode, sess, prompt, on_step):
            raise ConsultError(409, "the asked chat has not started its conversation yet")

        self.engine.execute = broken
        failed = await self.engine.start(source_id="a", target="b", mode="basic", question="Broken?")
        record = await self.finished(failed["id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("not started", record["error"]["message"])

    async def test_the_user_can_ask_any_chat_directly(self):
        started = await self.engine.start(source_id=None, target="d", mode="deep", question="What did you change?")
        self.assertEqual(started["status"], "queued")
        self.assertEqual(started["source"], {"kind": "user"})
        await asyncio.wait_for(self.engine.finished.get(started["id"], asyncio.Event()).wait(), 5)
        listed = self.engine.list("d")
        self.assertEqual(listed["incoming"][0]["status"], "completed")
        self.assertIn("The user asks you", self.executor.calls[0][2])

    async def test_the_user_asks_from_a_chat_without_approval_or_turn_limits(self):
        started = await self.engine.start(source_id="a", target="Backend", mode="deep",
                                          question="What does deploy.md set?", by_user=True)
        # The user asked: no approval, and a mark in the asking chat's timeline.
        self.assertEqual(started["status"], "queued")
        self.assertTrue(started["source"]["by_user"])
        self.assertEqual([record["id"] for record in self.marked], [started["id"]])
        record = await self.finished(started["id"])
        self.assertEqual(record["status"], "completed")
        self.assertIn('The user, writing in another chat ("Mac app"),', self.executor.calls[0][2])
        # Scope still applies.
        with self.assertRaises(ConsultError):
            await self.engine.start(source_id="a", target="Other project", mode="basic", question="Hi?", by_user=True)

    async def test_lists_and_deleting_a_chat(self):
        started = await self.engine.start(source_id="a", target="b", mode="basic", question="Hi?")
        await self.finished(started["id"])
        self.assertEqual([item["id"] for item in self.engine.list("a")["outgoing"]], [started["id"]])
        self.assertEqual([item["id"] for item in self.engine.list("b")["incoming"]], [started["id"]])
        await self.engine.delete_session("b")
        self.assertEqual(self.engine.list("a")["outgoing"], [])

    async def test_restart_marks_unfinished_consults_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "consults.sqlite3"
            first = ChatConsults(path, sessions=lambda: self.sessions, execute=self.executor)
            self.executor.gate = asyncio.Event()
            started = await first.start(source_id="a", target="b", mode="basic", question="Hi?")
            # The process dies without closing: the row stays "running".
            first.tasks[started["id"]].cancel()
            first.records[started["id"]]["status"] = "running"
            first._save(first.records[started["id"]])
            second = ChatConsults(path, sessions=lambda: self.sessions, execute=self.executor)
            self.assertEqual(second.recover(), 1)
            self.assertEqual(second.list("a")["outgoing"][0]["status"], "interrupted")
            await first.close()
            await second.close()

    def test_confidence_line(self):
        self.assertEqual(split_confidence("Yes.\nCONFIDENCE: high"), ("Yes.", True))
        self.assertEqual(split_confidence("Maybe.\n**Confidence: LOW**\n\n"), ("Maybe.", False))
        self.assertEqual(split_confidence("No line here."), ("No line here.", None))


class ConsultRouteTests(unittest.TestCase):
    def setUp(self):
        self.sessions = chats()
        self.granted: list[tuple[str, str]] = []

        async def authorize(request, *, action, session_id):
            self.granted.append((action, session_id))
            if request.headers.get("X-Hyadex-Provider-Capability") != "ok":
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="provider capability is invalid")
            return {"source_run_id": "run1"}

        self.engine = ChatConsults(None, sessions=lambda: self.sessions, execute=Executor())
        app = FastAPI()
        app.include_router(create_consult_router(self.engine, authorize_agent=authorize))
        # One event loop for every request, so a consult outlives the request that started it.
        self.client = TestClient(app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.helper = {"X-Hyadex-Provider-Capability": "ok"}

    def test_helper_routes_need_the_turns_capability(self):
        response = self.client.get("/api/agent/sessions/a/consult-targets")
        self.assertEqual(response.status_code, 403)
        response = self.client.get("/api/agent/sessions/a/consult-targets", headers=self.helper)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Backend", [chat["title"] for chat in response.json()["chats"]])
        self.assertEqual(self.granted[-1], ("ask", "a"))

    def test_helper_asks_and_waits_for_the_answer(self):
        response = self.client.post("/api/agent/sessions/a/consults", headers=self.helper,
                                    json={"chat": "Backend", "question": "Which port?", "request_id": "ask_12345678"})
        self.assertEqual(response.status_code, 200)
        consult = response.json()["consult"]
        self.assertEqual(consult["source_run_id"], "run1")
        response = self.client.get(f"/api/agent/sessions/a/consults/{consult['id']}?wait=5", headers=self.helper)
        self.assertEqual(response.json()["consult"]["status"], "completed")
        listed = self.client.get("/api/sessions/b/consults").json()
        self.assertEqual(listed["incoming"][0]["answer"], "The port is 7872.")
        response = self.client.post("/api/agent/sessions/a/consults", headers=self.helper,
                                    json={"chat": "Other project", "question": "Hi?"})
        self.assertEqual(response.status_code, 404)

    def test_the_user_asks_another_chat_from_this_one(self):
        targets = self.client.get("/api/sessions/a/consult-targets").json()["chats"]
        self.assertIn("Backend", [chat["title"] for chat in targets])
        consult = self.client.post("/api/sessions/a/ask", json={"chat": "Backend", "mode": "deep",
                                                               "question": "Which port?"}).json()["consult"]
        self.assertEqual(consult["status"], "queued")
        state = self.client.get(f"/api/agent/sessions/a/consults/{consult['id']}?wait=5", headers=self.helper)
        self.assertEqual(state.json()["consult"]["status"], "completed")

    def test_approval_and_cancel_routes(self):
        consult = self.client.post("/api/agent/sessions/a/consults", headers=self.helper,
                                   json={"chat": "b", "mode": "deep", "question": "Check?"}).json()["consult"]
        self.assertEqual(consult["status"], "awaiting_approval")
        response = self.client.post(f"/api/sessions/b/consults/{consult['id']}/decision", json={"decision": "allow"})
        self.assertEqual(response.status_code, 404)  # only the asking chat approves
        response = self.client.post(f"/api/sessions/a/consults/{consult['id']}/decision", json={"decision": "deny"})
        self.assertEqual(response.status_code, 200)
        state = self.client.get(f"/api/agent/sessions/a/consults/{consult['id']}?wait=5", headers=self.helper)
        self.assertEqual(state.json()["consult"]["status"], "denied")


if __name__ == "__main__":
    unittest.main()

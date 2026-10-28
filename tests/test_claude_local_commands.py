"""Claude local slash commands (``/compact``) live and in imported history.

Records mirror what Claude Code 2.1 writes for ``/compact``: an ``isMeta``
``<local-command-caveat>`` row, the ``<command-name>`` row as its child with
the same prompt id, then the output as the command row's child (a
``<local-command-stdout>`` user row, or a ``local_command`` system row with
``commandRun``). Native Claude Code shows these as the typed command and its
output.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import hyadex_server
from hyadex.providers.claude import history_repair as repair
from hyadex.providers.claude.native_goals import ClaudeGoalHistoryNormalizer
from hyadex.providers.claude.sdk import _parse_claude_sdk_message


PROVIDER = "ea7bd8b3-4f4f-4184-987b-d04682e044d3"
PROMPT = "830b4a05-1111-4111-8111-111111111111"
TIME = "2026-10-27T17:18:24.284Z"
WRAPPER = (
    "<command-name>/compact</command-name>\n"
    "            <command-message>compact</command-message>\n"
    "            <command-args>{args}</command-args>"
)
STDOUT = "<local-command-stdout>Compacted </local-command-stdout>"


def uid(number: int) -> str:
    return f"12345678-1234-4234-8234-{number:012d}"


def row(number: int, parent: int | None, **fields) -> dict:
    return {
        "uuid": uid(number), "parentUuid": uid(parent) if parent else None,
        "sessionId": PROVIDER, "timestamp": TIME, "isSidechain": False,
        "cwd": "/private/tmp/work", **fields,
    }


def user(number: int, parent: int | None, content, **fields) -> dict:
    return row(number, parent, **{"type": "user", "promptId": PROMPT,
                                  "message": {"role": "user", "content": content}, **fields})


def assistant(number: int, parent: int | None, text: str) -> dict:
    return row(number, parent, type="assistant", message={
        "role": "assistant", "model": "claude", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
    })


def compact_records(args: str = "") -> list[dict]:
    return [
        user(1, None, "Remember the word kiwi", promptId="0" * 8 + PROMPT[8:]),
        assistant(2, 1, "Noted."),
        row(3, 2, type="system", subtype="compact_boundary", content="Conversation compacted",
            compactMetadata={"trigger": "manual", "preTokens": 24202}),
        user(4, 3, "This session is being continued from a previous conversation.",
             isCompactSummary=True, isVisibleInTranscriptOnly=True),
        user(5, 4, "<local-command-caveat>The command below was run directly in Claude Code."
                   "</local-command-caveat>", isMeta=True),
        user(6, 5, WRAPPER.format(args=args)),
        user(7, 6, STDOUT),
    ]


def failed_compact_records() -> list[dict]:
    return [
        user(5, None, "<local-command-caveat>The command below was run directly in Claude Code."
                      "</local-command-caveat>", isMeta=True),
        user(6, 5, WRAPPER.format(args="")),
        row(7, 6, type="system", subtype="local_command", level="info", isMeta=False,
            commandRun={"command": "compact", "args": ""},
            content="<local-command-stderr>Error: No messages to compact</local-command-stderr>"),
    ]


def parse(records: list[dict]) -> list[tuple[str, str]]:
    items = hyadex_server.parse_claude_history_events(records, None)
    return [(item["kind"], item["text"]) for item in items]


def encode(rows: list[dict]) -> bytes:
    return b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in rows)


class ClaudeLocalCommandLiveProjectionTests(unittest.IsolatedAsyncioTestCase):
    """Real Agent SDK message classes, parsed from the raw records Claude sends."""

    async def project(self, raw_messages: list[dict]) -> tuple[list[tuple[str, dict]], list, set]:
        append_event = AsyncMock(return_value={})
        outputs: list = []
        native_runs: set = set()
        results = []
        with patch.object(hyadex_server, "append_event", append_event), patch.object(
            hyadex_server, "mark_provider_context_usage_unavailable", AsyncMock(),
        ), patch.object(hyadex_server, "mark_provider_turn_ready", AsyncMock()):
            for raw in raw_messages:
                message = _parse_claude_sdk_message(raw)
                results.append(await hyadex_server.project_claude_sdk_message(
                    "chat-1", "run-1", message, text_parts=[], current_tools={},
                    changed_paths=set(), local_command_outputs=outputs,
                    native_output_run_ids=native_runs,
                ))
        events = [(call.args[1], call.args[2]) for call in append_event.await_args_list]
        return events, results, native_runs

    async def test_sdk_compact_stream_projects_compaction_and_output(self) -> None:
        events, results, native_runs = await self.project([
            {"type": "system", "subtype": "compact_boundary", "session_id": PROVIDER,
             "uuid": uid(3), "compact_metadata": {"trigger": "manual", "pre_tokens": 13103,
                                                  "post_tokens": 2821, "duration_ms": 4389}},
            {"type": "user", "message": {"role": "user", "content": "This session is being continued."},
             "session_id": PROVIDER, "parent_tool_use_id": None, "uuid": uid(4),
             "isReplay": False, "isSynthetic": True},
            {"type": "user", "message": {"role": "user", "content": STDOUT},
             "session_id": PROVIDER, "parent_tool_use_id": None, "uuid": uid(7), "isReplay": True},
            {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 4416,
             "duration_api_ms": 0, "num_turns": 0, "session_id": PROVIDER, "result": "",
             "local_command": "compact", "uuid": uid(8)},
        ])

        self.assertEqual(events, [
            ("claude_compaction_completed", {
                "run_id": "run-1", "backend": "claude", "status": "completed",
                "trigger": "manual", "pre_tokens": 13103, "post_tokens": 2821,
                "provider_message_id": uid(3), "message": "Context compaction completed.",
            }),
            ("assistant_text", {
                "run_id": "run-1", "text": "Compacted", "backend": "claude",
                "provider_message_id": uid(7), "local_command": "compact",
                "local_command_stream": "stdout",
            }),
        ])
        self.assertEqual(results[-1]["local_command"], "compact")
        self.assertEqual(native_runs, {"run-1"})

    async def test_a_late_result_of_an_earlier_command_is_marked_detached(self) -> None:
        """Clearing a goal mid-turn can deliver its result in a later /compact turn."""
        goal_stdout = "<local-command-stdout>Goal cleared: tests pass</local-command-stdout>"
        with patch.dict(hyadex_server.RUNS.current_turns,
                        {"chat-1": {"run_id": "run-1", "prompt": "/compact"}}):
            events, _results, native_runs = await self.project([
                {"type": "user", "message": {"role": "user", "content": goal_stdout},
                 "session_id": PROVIDER, "parent_tool_use_id": None, "uuid": uid(11), "isReplay": True},
                {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10,
                 "duration_api_ms": 0, "num_turns": 0, "session_id": PROVIDER, "result": "",
                 "local_command": "goal", "uuid": uid(12)},
                {"type": "user", "message": {"role": "user", "content": STDOUT},
                 "session_id": PROVIDER, "parent_tool_use_id": None, "uuid": uid(13), "isReplay": True},
                {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 10,
                 "duration_api_ms": 0, "num_turns": 0, "session_id": PROVIDER, "result": "",
                 "local_command": "compact", "uuid": uid(14)},
            ])
        texts = [(payload["text"], payload.get("local_command_detached", False))
                 for kind, payload in events if kind == "assistant_text"]
        self.assertEqual(texts, [("Goal cleared: tests pass", True), ("Compacted", False)])
        self.assertEqual(native_runs, {"run-1"})

    async def test_sdk_synthetic_local_command_message_keeps_its_stream(self) -> None:
        events, _results, native_runs = await self.project([{
            "type": "assistant", "parent_tool_use_id": None, "session_id": PROVIDER,
            "uuid": uid(7), "local_command_run": {"command": "compact", "args": ""},
            "local_command_outcome": {"kind": "failed"},
            "local_command_source": "<local-command-stderr>Error: No messages to compact</local-command-stderr>",
            "message": {"id": "message-1", "model": "<synthetic>", "role": "assistant",
                        "stop_reason": "end_turn", "content": [
                            {"type": "text", "text": "Error: No messages to compact"}]},
        }])

        self.assertEqual(events, [("assistant_text", {
            "run_id": "run-1", "text": "Error: No messages to compact", "backend": "claude",
            "provider_message_id": uid(7), "local_command": "compact",
            "local_command_stream": "stderr",
        })])
        self.assertEqual(native_runs, {"run-1"})


    async def test_a_redelivered_synthetic_output_of_another_command_is_detached(self) -> None:
        """Seen live: a /compact turn re-delivered "Goal set" and "Goal cleared" outputs."""
        with patch.dict(hyadex_server.RUNS.current_turns,
                        {"chat-1": {"run_id": "run-1", "prompt": "/compact"}}):
            events, _results, native_runs = await self.project([{
                "type": "assistant", "parent_tool_use_id": None, "session_id": PROVIDER,
                "uuid": uid(21), "local_command_run": {"command": "goal", "args": "clear"},
                "message": {"id": "message-2", "model": "<synthetic>", "role": "assistant",
                            "stop_reason": "end_turn", "content": [
                                {"type": "text", "text": "Goal cleared: tests pass"}]},
            }])
        self.assertEqual(events, [("assistant_text", {
            "run_id": "run-1", "text": "Goal cleared: tests pass", "backend": "claude",
            "provider_message_id": uid(21), "local_command": "goal", "local_command_detached": True,
        })])
        # Another command's output is not this turn's answer.
        self.assertEqual(native_runs, set())


class ClaudeLocalCommandHistoryImportTests(unittest.TestCase):
    def test_compact_command_and_output_import_as_native_shows_them(self) -> None:
        self.assertEqual(parse(compact_records()), [
            ("user", "Remember the word kiwi"),
            ("assistant", "Noted."),
            ("user", "/compact"),
            ("assistant", "Compacted"),
        ])

    def test_command_arguments_follow_the_command(self) -> None:
        self.assertIn(("user", "/compact keep the test plan"),
                      parse(compact_records(args="  keep the test plan ")))

    def test_failed_command_output_from_the_system_record(self) -> None:
        self.assertEqual(parse(failed_compact_records()), [
            ("user", "/compact"),
            ("assistant", "Error: No messages to compact"),
        ])

    def test_text_without_the_native_record_chain_stays_as_typed(self) -> None:
        wrapper = WRAPPER.format(args="")
        cases = {
            "wrapper typed after an answer": [assistant(2, None, "Hi"), user(6, 2, wrapper)],
            "output typed after an answer": [assistant(2, None, "Hi"), user(7, 2, STDOUT)],
            "caveat from another prompt": [
                user(5, None, "<local-command-caveat>x</local-command-caveat>", isMeta=True),
                user(6, 5, wrapper, promptId="99999999-9999-4999-8999-999999999999"),
            ],
            "caveat from another session": [
                {**user(5, None, "<local-command-caveat>x</local-command-caveat>", isMeta=True),
                 "sessionId": "another-session"},
                user(6, 5, wrapper),
            ],
        }
        for name, records in cases.items():
            with self.subTest(name):
                kept = [text for kind, text in parse(records) if kind == "user"]
                self.assertEqual(kept, [records[-1]["message"]["content"]])

    def test_output_needs_its_command_and_matching_metadata(self) -> None:
        records = failed_compact_records()
        records[-1]["commandRun"] = {"command": "context", "args": ""}
        self.assertEqual(parse(records), [("user", "/compact")])
        records = compact_records()
        records[-1]["promptId"] = "99999999-9999-4999-8999-999999999999"
        self.assertEqual(parse(records)[-1], ("user", STDOUT))

    def test_goal_commands_keep_their_own_attachment_proof(self) -> None:
        normalizer = ClaudeGoalHistoryNormalizer()
        caveat = user(5, None, "<local-command-caveat>x</local-command-caveat>", isMeta=True)
        goal = user(6, 5, WRAPPER.replace("compact", "goal").format(args="clear"))
        normalizer.consume(caveat)
        self.assertEqual(normalizer.consume(goal)["message"]["content"], "/goal clear")

    def test_incremental_sync_links_output_to_a_command_before_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / f"{PROVIDER}.jsonl"
            records = compact_records()
            path.write_bytes(encode(records))
            start = len(encode(records[:-1]))
            info = path.stat()
            items, offset, _digest, blocked = hyadex_server.parse_provider_history_delta(
                path, "claude", start, info.st_size, limit=None,
                expected_stat={field: int(getattr(info, field))
                               for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns")},
                previous_last_item_digest="",
            )

        self.assertEqual([(item["kind"], item["text"]) for item in items], [("assistant", "Compacted")])
        self.assertEqual(items[0]["provider_origin"]["event_id"], uid(7))
        self.assertEqual(offset, info.st_size)
        self.assertFalse(blocked)

    def test_a_command_hyadex_ran_live_is_not_imported_again(self) -> None:
        common = {"session_id": "chat-1", "backend": "claude", "run_id": "run-live"}
        live = [
            {**common, "seq": 1, "type": "turn_started", "prompt": "Remember the word kiwi", "run_id": "run-a"},
            {**common, "seq": 2, "type": "provider_session", "provider_session_id": PROVIDER, "run_id": "run-a"},
            {**common, "seq": 3, "type": "assistant_text", "text": "Noted.", "run_id": "run-a",
             "provider_message_id": uid(2)},
            {**common, "seq": 4, "type": "turn_finished", "exit_code": 0, "result_text": "Noted.", "run_id": "run-a"},
            {**common, "seq": 5, "type": "turn_started", "prompt": "/compact"},
            {**common, "seq": 6, "type": "provider_session", "provider_session_id": PROVIDER},
            {**common, "seq": 7, "type": "claude_compaction_completed", "trigger": "manual"},
            {**common, "seq": 8, "type": "assistant_text", "text": "Compacted",
             "provider_message_id": uid(7), "local_command": "compact"},
            {**common, "seq": 9, "type": "turn_finished", "exit_code": 0, "result_text": ""},
        ]
        items = hyadex_server.parse_claude_history_events(compact_records(), None)
        with tempfile.TemporaryDirectory() as temporary:
            events = Path(temporary) / "events.jsonl"
            events.write_bytes(encode(live))
            with patch.object(hyadex_server, "events_path", return_value=events):
                fresh = hyadex_server.unsynced_history_items("chat-1", items, timeline_through_seq=9)
                appended = hyadex_server.unsynced_history_items(
                    "chat-1", items + [{"kind": "user", "text": "Typed in the terminal"}],
                    timeline_through_seq=9,
                )
                # A later sync consumes the live turn's credits in order.
                cursor_fresh, consumed = hyadex_server.reconcile_cursor_history_items(
                    "chat-1", items[2:], timeline_after_seq=4, timeline_through_seq=9,
                )

        self.assertEqual(fresh, [])
        self.assertEqual([item["text"] for item in appended], ["Typed in the terminal"])
        self.assertEqual([(item["kind"], item["text"]) for item in items[2:]],
                         [("user", "/compact"), ("assistant", "Compacted")])
        self.assertEqual((cursor_fresh, consumed), ([], 9))


class ClaudeLocalCommandImportedRowRepairTests(unittest.TestCase):
    """Rows an older import stored as raw command XML user messages."""

    LIVE_TS = "2026-10-27T17:18:24Z"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / f"{PROVIDER}.jsonl"
        self.events = self.root / "events.jsonl"
        self.cache = repair.ClaudeMetadataRepairCache()

    @staticmethod
    def normalize(event: dict) -> str | None:
        # The old importer's text: the record content, unchanged.
        content = (event.get("message") or {}).get("content")
        return content.strip() if isinstance(content, str) else None

    def imported_rows(self, records: list[dict], *, live: list[dict] = ()) -> tuple[dict, dict]:
        self.source.write_bytes(encode(records))
        info = self.source.stat()
        through = encode(records)
        common = {"session_id": "chat-1", "backend": "claude", "run_id": "import_old", "imported": True}

        def origin(record: dict) -> dict:
            return {"provider": "claude", "event_id": record["uuid"],
                    "session_id": PROVIDER, "timestamp": record["timestamp"]}

        command = {**common, "seq": 21, "type": "turn_started", "provider_history_sanitized": True,
                   "prompt": records[-2]["message"]["content"], "provider_origin": origin(records[-2])}
        output = {**common, "seq": 22, "type": "turn_started", "provider_history_sanitized": True,
                  "prompt": records[-1]["message"]["content"], "provider_origin": origin(records[-1])}
        rows = [*live, {
            **common, "seq": 20, "type": "history_imported", "provider_session_id": PROVIDER,
            "source_path": str(self.source),
            "_history_sync_checkpoint": {
                "version": 1, "previous_present": False, "previous_source_offset": 0,
                "previous_source_digest": "",
                "cursor": {
                    "version": 1, "backend": "claude", "provider_session_id": PROVIDER,
                    "source_path": str(self.source), "source_dev": info.st_dev, "source_ino": info.st_ino,
                    "source_offset": len(through), "source_digest": hashlib.sha256(through).hexdigest(),
                },
            },
        }, command, output, {**common, "seq": 23, "type": "turn_finished"}]
        self.events.write_bytes(encode(rows))
        self.cache.prepare("chat-1", PROVIDER, self.events, self.root, self.normalize)
        return command, output

    def test_old_raw_rows_show_the_command_and_its_output(self) -> None:
        command, output = self.imported_rows(compact_records())

        self.assertEqual(self.cache.project_event("chat-1", command)["prompt"], "/compact")
        projected = self.cache.project_event("chat-1", output)
        self.assertEqual(projected["type"], "assistant_text")
        self.assertEqual(projected["text"], "Compacted")
        self.assertNotIn("prompt", projected)
        self.assertEqual(projected["seq"], 22)
        self.assertEqual(projected["provider_origin"]["event_id"], uid(7))
        self.assertFalse(self.cache.is_hidden("chat-1", output))

    def test_old_rows_of_a_live_compact_turn_are_hidden(self) -> None:
        common = {"session_id": "chat-1", "backend": "claude", "run_id": "run-live"}
        live = [
            {**common, "seq": 1, "type": "turn_started", "prompt": "/compact", "ts": self.LIVE_TS},
            {**common, "seq": 2, "type": "turn_finished", "exit_code": 1, "ts": self.LIVE_TS,
             "provider_session_id": PROVIDER},
        ]
        command, output = self.imported_rows(compact_records(), live=live)

        self.assertTrue(self.cache.is_hidden("chat-1", command))
        self.assertTrue(self.cache.is_hidden("chat-1", output))

    def test_pasted_lookalike_rows_are_not_rewritten(self) -> None:
        records = [assistant(2, None, "Hi"), user(6, 2, WRAPPER.format(args="")), user(7, 6, STDOUT)]
        command, output = self.imported_rows(records)

        self.assertIsNone(self.cache.project_event("chat-1", command))
        self.assertIsNone(self.cache.project_event("chat-1", output))
        self.assertFalse(self.cache.is_hidden("chat-1", command))


GOAL_WRAPPER = (
    "<command-name>/goal</command-name>\n"
    "            <command-message>goal</command-message>\n"
    "            <command-args>clear</command-args>"
)
GOAL_CLEARED = "Goal cleared: The user has typed the word banana in this chat."


def caveat(number: int, parent: int | None, prompt_id: str) -> dict:
    return user(number, parent, "<local-command-caveat>The command below was run directly "
                                "in Claude Code.</local-command-caveat>",
                isMeta=True, promptId=prompt_id)


def prompt_id(number: int) -> str:
    return f"9abcdef0-0000-4000-8000-{number:012d}"


def goal_clear_records() -> list[dict]:
    """A turn, then a /goal clear sent while it ran (its own native records)."""
    return [
        user(31, None, "Reply with exactly kiwi", promptId=prompt_id(1)),
        assistant(32, 31, "kiwi"),
        caveat(33, 32, prompt_id(2)),
        user(34, 33, GOAL_WRAPPER, promptId=prompt_id(2)),
        row(35, 34, type="system", subtype="local_command", level="info", isMeta=False,
            commandRun={"command": "goal", "args": "clear"},
            content=f"<local-command-stdout>{GOAL_CLEARED}</local-command-stdout>"),
    ]


def live_compact_turn_records() -> list[dict]:
    """``/compact`` (boundary, summary, caveat, command, stdout), then a turn."""
    return [
        row(41, 35, type="system", subtype="compact_boundary", content="Conversation compacted",
            compactMetadata={"trigger": "manual", "preTokens": 24202}),
        user(42, 41, "This session is being continued from a previous conversation.",
             isCompactSummary=True, isVisibleInTranscriptOnly=True, promptId=prompt_id(3)),
        caveat(43, 42, prompt_id(3)),
        user(44, 43, WRAPPER.format(args=""), promptId=prompt_id(3)),
        user(45, 44, STDOUT, promptId=prompt_id(3)),
        user(46, 45, "This is a test drill requested by the user.", promptId=prompt_id(4)),
        assistant(47, 46, "Done. I raised one emergency alert."),
    ]


class ClaudeLiveLocalCommandSyncTests(unittest.IsolatedAsyncioTestCase):
    """The incremental history sync after a live ``/compact`` turn.

    Reproduces a real chat: a ``/goal clear`` sent mid-turn was imported by
    one sync, Claude then re-delivered that goal output inside the next
    ``/compact`` turn (before "Compacted"), and the following sync imported
    "Compacted" and everything after it again.
    """

    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ledger = self.root / "events.jsonl"
        self.transcript = self.root / f"{PROVIDER}.jsonl"
        self.events: list[dict] = []
        self.batches: list[list[tuple[str, dict]]] = []

    def write_ledger(self) -> None:
        self.ledger.write_bytes(encode(self.events))

    def live(self, event_type: str, run_id: str, **payload) -> None:
        self.events.append({
            "seq": len(self.events) + 1, "type": event_type, "session_id": "chat-compact",
            "backend": "claude", "run_id": run_id, **payload,
        })

    def live_turn(self, run_id: str, prompt: str, outputs: list[dict]) -> None:
        self.live("turn_started", run_id, prompt=prompt)
        for output in outputs:
            event_type = output.pop("type", "assistant_text")
            self.live(event_type, run_id, **output)
        self.live("provider_session", run_id, provider_session_id=PROVIDER)
        self.live("turn_finished", run_id, provider_session_id=PROVIDER, exit_code=0,
                  result_text="")

    async def sync(self, session: dict) -> dict:
        async def append(_session_id, specs):
            specs = list(specs)
            self.batches.append(specs)
            committed = []
            for event_type, payload in specs:
                event = {"seq": len(self.events) + 1, "type": event_type, **payload}
                self.events.append(event)
                committed.append(event)
            self.write_ledger()
            return committed

        def last_seq(_path):
            self.write_ledger()
            return len(self.events)

        with patch.object(hyadex_server, "events_path", return_value=self.ledger), patch.object(
            hyadex_server, "provider_history_path", return_value=self.transcript,
        ), patch.object(hyadex_server, "last_event_seq_from_file", side_effect=last_seq), patch.object(
            hyadex_server, "append_durable_event_batch", side_effect=append,
        ), patch.object(hyadex_server.STORE, "save", AsyncMock()):
            return await hyadex_server.sync_provider_history(dict(session))

    def imported_messages(self, batch: list[tuple[str, dict]]) -> list[tuple[str, str]]:
        return [
            (event_type, payload.get("prompt") if event_type == "turn_started" else payload.get("text"))
            for event_type, payload in batch
            if event_type in {"turn_started", "assistant_text"} and not payload.get("metadata_only")
        ]

    async def test_live_compact_turn_is_not_imported_by_the_next_sync(self) -> None:
        session = {"id": "chat-compact", "backend": "claude", "claude_session_id": PROVIDER,
                   "cwd": str(self.root)}
        live_sessions = {"chat-compact": dict(session)}
        with patch.object(hyadex_server.STORE, "sessions", live_sessions):
            self.live_turn("run-kiwi", "Reply with exactly kiwi", [
                {"text": "kiwi", "provider_message_id": uid(32)},
            ])
            self.transcript.write_bytes(encode(goal_clear_records()))
            first = await self.sync(live_sessions["chat-compact"])
            # The mid-turn /goal clear never had a live turn: it is imported.
            self.assertEqual(first["imported"], 2)
            self.assertEqual(self.imported_messages(self.batches[-1]), [
                ("turn_started", "/goal clear"), ("assistant_text", GOAL_CLEARED),
            ])
            self.assertEqual(
                self.batches[-1][2][1]["provider_origin"]["event_id"], uid(35),
            )

            # The live /compact turn, as recorded in the real chat (seq 224-230).
            self.live_turn("run-compact", "/compact", [
                {"type": "claude_compaction_completed", "status": "completed",
                 "trigger": "manual", "provider_message_id": uid(41),
                 "message": "Context compaction completed."},
                {"text": GOAL_CLEARED, "provider_message_id": uid(35),
                 "local_command": "goal", "local_command_stream": "stdout",
                 "local_command_detached": True},
                {"text": "Compacted", "provider_message_id": uid(45),
                 "local_command": "compact", "local_command_stream": "stdout"},
            ])
            self.live_turn("run-drill", "This is a test drill requested by the user.", [
                {"text": "Done. I raised one emergency alert.", "provider_message_id": uid(47)},
            ])
            with self.transcript.open("ab") as stream:
                stream.write(encode(live_compact_turn_records()))
            batches_before = len(self.batches)
            second = await self.sync(live_sessions["chat-compact"])
            unchanged = await self.sync(live_sessions["chat-compact"])

        new_batches = self.batches[batches_before:]
        self.assertEqual(
            [message for batch in new_batches for message in self.imported_messages(batch)], [],
        )
        self.assertEqual(second["imported"], 0)
        self.assertEqual(unchanged["imported"], 0)

    async def test_goal_output_redelivered_before_the_sync_is_matched_by_its_id(self) -> None:
        """Without a sync in between, the goal output is in the same delta."""
        session = {"id": "chat-compact", "backend": "claude", "claude_session_id": PROVIDER,
                   "cwd": str(self.root)}
        live_sessions = {"chat-compact": dict(session)}
        with patch.object(hyadex_server.STORE, "sessions", live_sessions):
            self.live_turn("run-before", "Hello", [
                {"text": "Hi.", "provider_message_id": uid(30)},
            ])
            self.transcript.write_bytes(encode([
                user(29, None, "Hello", promptId=prompt_id(0)), assistant(30, 29, "Hi."),
            ]))
            first = await self.sync(live_sessions["chat-compact"])
            self.assertEqual(first["imported"], 0)

            records = goal_clear_records()
            records[0]["parentUuid"] = uid(30)
            self.live_turn("run-kiwi", "Reply with exactly kiwi", [
                {"text": "kiwi", "provider_message_id": uid(32)},
            ])
            self.live_turn("run-compact", "/compact", [
                {"text": GOAL_CLEARED, "provider_message_id": uid(35),
                 "local_command": "goal", "local_command_detached": True},
                {"text": "Compacted", "provider_message_id": uid(45), "local_command": "compact"},
            ])
            self.live_turn("run-drill", "This is a test drill requested by the user.", [
                {"text": "Done. I raised one emergency alert.", "provider_message_id": uid(47)},
            ])
            with self.transcript.open("ab") as stream:
                stream.write(encode(records + live_compact_turn_records()))
            batches_before = len(self.batches)
            second = await self.sync(live_sessions["chat-compact"])

        imported = [message for batch in self.batches[batches_before:]
                    for message in self.imported_messages(batch)]
        # Only the mid-turn /goal clear itself had no live turn of its own.
        self.assertEqual(imported, [("turn_started", "/goal clear")])
        self.assertEqual(second["imported"], 1)


if __name__ == "__main__":
    unittest.main()

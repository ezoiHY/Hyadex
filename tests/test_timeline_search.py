import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import hyadex_server
from tests.test_provider_history_sync import legacy_provider_authority_block


class TimelineSearchForkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = hyadex_server.STATE_DIR
        self.previous_search_db = hyadex_server.HISTORY_SEARCH_DB
        hyadex_server.STATE_DIR = Path(self.temporary.name)
        hyadex_server.HISTORY_SEARCH_DB = hyadex_server.STATE_DIR / "history_search.sqlite3"
        hyadex_server.FORK_INTERNAL_RUN_CACHE.clear()
        hyadex_server.FORK_INTERNAL_RUN_LOCKS.clear()
        self.session_id = "legacy-fork"
        path = hyadex_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            self.event(1, "turn_started", run_id="digest-run", purpose="handoff_digest", forked=True, prompt="Generate private digest"),
            self.event(2, "reasoning_summary", run_id="digest-run", forked=True, text="Private digest reasoning"),
            self.event(3, "assistant_text", run_id="digest-run", forked=True, text="Private digest body"),
            self.event(4, "turn_started", run_id="normal-run", forked=True, prompt="Retained searchable question"),
            self.event(5, "assistant_text", run_id="normal-run", forked=True, text="Retained searchable answer"),
            self.event(
                6,
                "turn_started",
                run_id="handoff-run",
                purpose="cross_chat_handoff_delivery",
                cross_chat_envelope_id="handoff-hidden",
                prompt="Hidden relay needle",
            ),
        ]
        path.write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        hyadex_server.FORK_INTERNAL_RUN_CACHE.clear()
        hyadex_server.FORK_INTERNAL_RUN_LOCKS.clear()
        hyadex_server.STATE_DIR = self.previous_state_dir
        hyadex_server.HISTORY_SEARCH_DB = self.previous_search_db
        self.temporary.cleanup()

    def test_search_excludes_the_entire_copied_digest_run(self) -> None:
        connection = hyadex_server.history_search_connection()
        try:
            indexed_sessions, indexed_events = hyadex_server.sync_history_search_index(
                connection,
                {self.session_id},
                {self.session_id},
            )
        finally:
            connection.close()

        self.assertEqual((indexed_sessions, indexed_events), (1, 2))
        self.assertEqual(hyadex_server.search_timeline_index(self.session_id, "private"), {
            "session_id": self.session_id,
            "query": "private",
            "results": [],
        })
        retained = hyadex_server.search_timeline_index(self.session_id, "searchable")
        self.assertEqual([result["seq"] for result in retained["results"]], [5, 4])
        self.assertEqual(hyadex_server.search_timeline_index(self.session_id, "relay"), {
            "session_id": self.session_id,
            "query": "relay",
            "results": [],
        })

    def test_search_index_version_change_removes_legacy_rows_before_rebuild(self) -> None:
        connection = hyadex_server.history_search_connection()
        connection.execute(
            "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) VALUES (?, ?, ?, ?, ?, ?)",
            ("stale private digest", self.session_id, "legacy-event", 1, None, "assistant"),
        )
        connection.execute(
            "INSERT INTO history_search_state(session_id, inode, offset, mtime_ns) VALUES (?, ?, ?, ?)",
            (self.session_id, 1, 1, 1),
        )
        connection.execute(
            "UPDATE history_search_meta SET value = '4' WHERE key = 'index_version'"
        )
        connection.commit()
        connection.close()

        migrated = hyadex_server.history_search_connection()
        try:
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM history_search").fetchone()[0], 0)
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM history_search_state").fetchone()[0], 0)
            self.assertEqual(
                migrated.execute(
                    "SELECT value FROM history_search_meta WHERE key = 'index_version'"
                ).fetchone()[0],
                hyadex_server.HISTORY_SEARCH_INDEX_VERSION,
            )
        finally:
            migrated.close()

    def test_search_sanitizes_only_structural_provider_import_records(self) -> None:
        authority = legacy_provider_authority_block(run_id="run_search_sanitizer", nonce="0123456789abcdef0123456789abcdef", chat_id=self.session_id)
        generated_notice = (
            "<task-notification>\n"
            "<task-id>task_generated</task-id>\n"
            "<tool-use-id>toolu_task_generated</tool-use-id>\n"
            "<status>completed</status>\n"
            "<summary>secretgeneratedneedle</summary>\n"
            "</task-notification>"
        )
        human_notice = generated_notice.replace(
            "secretgeneratedneedle",
            "legitimatepastedneedle",
        )
        native_notice = generated_notice.replace(
            "secretgeneratedneedle",
            "nativepastedneedle",
        )
        events = [
            self.event(
                1,
                "turn_started",
                run_id="import_search_clean",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                prompt="visiblecleanneedle" + authority,
            ),
            self.event(
                2,
                "turn_started",
                run_id="import_search_notice",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                prompt=generated_notice,
            ),
            self.event(
                3,
                "turn_started",
                run_id="import_human_paste",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                provider_history_sanitized=True,
                prompt=human_notice,
            ),
            self.event(
                4,
                "turn_started",
                run_id="import_authority_only",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                prompt=authority.strip(),
            ),
            self.event(
                5,
                "turn_started",
                run_id="import_native_lookalike",
                backend=hyadex_server.BACKEND_CLAUDE,
                prompt=native_notice,
            ),
        ]
        hyadex_server.events_path(self.session_id).write_text(
            "".join(
                json.dumps(event, separators=(",", ":")) + "\n"
                for event in events
            ),
            encoding="utf-8",
        )

        connection = hyadex_server.history_search_connection()
        try:
            indexed_sessions, indexed_events = (
                hyadex_server.sync_history_search_index(
                    connection,
                    {self.session_id},
                    {self.session_id},
                )
            )
        finally:
            connection.close()

        self.assertEqual((indexed_sessions, indexed_events), (1, 3))
        cleaned = hyadex_server.search_timeline_index(
            self.session_id,
            "visiblecleanneedle",
        )
        self.assertEqual(len(cleaned["results"]), 1)
        self.assertEqual(cleaned["results"][0]["snippet"], "visiblecleanneedle")
        self.assertEqual(
            hyadex_server.search_timeline_index(
                self.session_id,
                "secretgeneratedneedle",
            )["results"],
            [],
        )
        preserved = hyadex_server.search_timeline_index(
            self.session_id,
            "legitimatepastedneedle",
        )
        self.assertEqual([result["seq"] for result in preserved["results"]], [3])
        native = hyadex_server.search_timeline_index(
            self.session_id,
            "nativepastedneedle",
        )
        self.assertEqual([result["seq"] for result in native["results"]], [5])
        self.assertEqual(
            hyadex_server.search_timeline_index(
                self.session_id,
                "Hyadex",
            )["results"],
            [],
        )

    def test_version_migration_rebuild_excludes_imported_claude_task_notification(self) -> None:
        task_notification = (
            "<task-notification>\n"
            "<task-id>workflow-42</task-id>\n"
            "<tool-use-id>toolu_workflow_42</tool-use-id>\n"
            "<status>stopped</status>\n"
            "<summary>internal control needle</summary>\n"
            "</task-notification>"
        )
        # Migration must use the same complete provider fingerprint as every
        # other history projection. An incomplete lookalike can be user text.
        partial_notice = task_notification.replace(
            "<tool-use-id>toolu_workflow_42</tool-use-id>\n", ""
        ).replace("internal control needle", "partialpastedneedle")
        path = hyadex_server.events_path(self.session_id)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.event(
                7,
                "turn_started",
                run_id="import_legacy_task",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                prompt=task_notification,
            ), separators=(",", ":")) + "\n")
            stream.write(json.dumps(self.event(
                8,
                "assistant_text",
                run_id="normal-run",
                backend=hyadex_server.BACKEND_CLAUDE,
                text="visible migration needle",
            ), separators=(",", ":")) + "\n")
            stream.write(json.dumps(self.event(
                9,
                "turn_started",
                run_id="import_legacy_lookalike",
                backend=hyadex_server.BACKEND_CLAUDE,
                imported=True,
                prompt=partial_notice,
            ), separators=(",", ":")) + "\n")

        connection = hyadex_server.history_search_connection()
        connection.execute(
            "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                task_notification,
                self.session_id,
                "legacy-task-notification",
                7,
                None,
                "user",
            ),
        )
        connection.execute(
            "INSERT INTO history_search_state(session_id, inode, offset, mtime_ns) "
            "VALUES (?, ?, ?, ?)",
            (self.session_id, 1, path.stat().st_size, 1),
        )
        connection.execute(
            "UPDATE history_search_meta SET value = '4' WHERE key = 'index_version'"
        )
        connection.commit()
        connection.close()

        migrated = hyadex_server.history_search_connection()
        try:
            hyadex_server.sync_history_search_index(
                migrated,
                {self.session_id},
                {self.session_id},
            )
        finally:
            migrated.close()

        self.assertEqual(
            hyadex_server.search_timeline_index(self.session_id, "control needle")[
                "results"
            ],
            [],
        )
        self.assertEqual(
            [
                result["seq"]
                for result in hyadex_server.search_timeline_index(
                    self.session_id,
                    "migration needle",
                )["results"]
            ],
            [8],
        )
        self.assertEqual(
            [
                result["seq"]
                for result in hyadex_server.search_timeline_index(
                    self.session_id, "partialpastedneedle",
                )["results"]
            ],
            [9],
        )

    def test_initializing_fork_is_not_eligible_for_history_indexing(self) -> None:
        staged_id = "staged-fork"
        staged_path = hyadex_server.events_path(staged_id)
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(
            json.dumps({
                "id": "staged-event",
                "session_id": staged_id,
                "seq": 1,
                "type": "assistant_text",
                "ts": "2026-07-21T01:00:00Z",
                "text": "staged-only needle",
            }, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        sessions = {
            self.session_id: {
                "id": self.session_id,
                "archived": False,
            },
            staged_id: {
                "id": staged_id,
                "archived": False,
                "_fork_initializing": True,
            },
        }

        with patch.object(hyadex_server.STORE, "sessions", sessions):
            active_ids = hyadex_server.active_history_search_session_ids()
            connection = hyadex_server.history_search_connection()
            try:
                hyadex_server.sync_history_search_index(
                    connection,
                    {self.session_id, staged_id},
                    active_ids,
                )
                indexed_session_ids = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT session_id FROM history_search"
                    ).fetchall()
                }
            finally:
                connection.close()

        self.assertEqual(active_ids, {self.session_id})
        self.assertEqual(indexed_session_ids, {self.session_id})

    def test_queries_hide_stale_rows_from_initializing_fork(self) -> None:
        staged_id = "staged-fork"
        removed_id = "removed-staged-fork"
        connection = hyadex_server.history_search_connection()
        try:
            connection.executemany(
                "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        "sharedneedle visible",
                        self.session_id,
                        "visible-event",
                        10,
                        "2026-07-21T01:00:00Z",
                        "assistant",
                    ),
                    (
                        "sharedneedle hidden",
                        staged_id,
                        "hidden-event",
                        20,
                        "2026-07-21T02:00:00Z",
                        "assistant",
                    ),
                    (
                        "sharedneedle removed",
                        removed_id,
                        "removed-event",
                        30,
                        "2026-07-21T03:00:00Z",
                        "assistant",
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO history_search_sessions(session_id, active) VALUES (?, 1)",
                [(self.session_id,), (staged_id,), (removed_id,)],
            )
            connection.commit()
        finally:
            connection.close()

        sessions = {
            self.session_id: {"id": self.session_id},
            staged_id: {"id": staged_id, "_fork_initializing": True},
        }
        with patch.object(hyadex_server.STORE, "sessions", sessions):
            global_results = hyadex_server.search_all_timelines("sharedneedle")
            staged_results = hyadex_server.search_timeline_index(
                staged_id,
                "sharedneedle",
            )

        self.assertEqual(
            [result["session_id"] for result in global_results["results"]],
            [self.session_id],
        )
        self.assertEqual(staged_results["results"], [])

    def event(self, seq: int, event_type: str, **fields: object) -> dict[str, object]:
        return {
            "id": f"event-{seq}",
            "session_id": self.session_id,
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-21T00:00:{seq:02d}Z",
            **fields,
        }


class TimelineSearchCJKTests(unittest.TestCase):
    session_id = "cjk-chat"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = hyadex_server.STATE_DIR
        self.previous_search_db = hyadex_server.HISTORY_SEARCH_DB
        hyadex_server.STATE_DIR = Path(self.temporary.name)
        hyadex_server.HISTORY_SEARCH_DB = hyadex_server.STATE_DIR / "history_search.sqlite3"
        hyadex_server.FORK_INTERNAL_RUN_CACHE.clear()
        hyadex_server.FORK_INTERNAL_RUN_LOCKS.clear()
        texts = [
            ("turn_started", "prompt", "请修复登录页面，然后运行测试"),
            ("assistant_text", "text", "我们在讨论中文搜索的问题"),
            ("assistant_text", "text", "テストを実行しました"),
            ("assistant_text", "text", "검색을 수정했습니다"),
            ("assistant_text", "text", "Ran pytest then 运行test测试 again"),
            ("reasoning_summary", "text", "Plain English reasoning about the login page"),
        ]
        path = hyadex_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            json.dumps({
                "id": f"event-{seq}",
                "session_id": self.session_id,
                "seq": seq,
                "type": event_type,
                "ts": f"2026-07-21T00:00:{seq:02d}Z",
                "run_id": "run-1",
                field: text,
            }, ensure_ascii=False, separators=(",", ":")) + "\n"
            for seq, (event_type, field, text) in enumerate(texts, start=1)
        ), encoding="utf-8")
        connection = hyadex_server.history_search_connection()
        try:
            hyadex_server.sync_history_search_index(connection, {self.session_id}, {self.session_id})
        finally:
            connection.close()

    def tearDown(self) -> None:
        hyadex_server.FORK_INTERNAL_RUN_CACHE.clear()
        hyadex_server.FORK_INTERNAL_RUN_LOCKS.clear()
        hyadex_server.STATE_DIR = self.previous_state_dir
        hyadex_server.HISTORY_SEARCH_DB = self.previous_search_db
        self.temporary.cleanup()

    def seqs(self, query: str) -> list[int]:
        return [result["seq"] for result in hyadex_server.search_timeline_index(self.session_id, query)["results"]]

    def test_words_inside_chinese_japanese_and_korean_text_match(self) -> None:
        self.assertEqual(self.seqs("测试"), [5, 1])
        self.assertEqual(self.seqs("中文"), [2])
        self.assertEqual(self.seqs("登录页面"), [1])
        self.assertEqual(self.seqs("実行"), [3])
        self.assertEqual(self.seqs("テスト"), [3])
        self.assertEqual(self.seqs("색을"), [4])
        self.assertEqual(self.seqs("行test"), [5])
        self.assertEqual(self.seqs("测试 登录"), [1])
        self.assertEqual(self.seqs('"运行 测试"'), [1])
        self.assertEqual(self.seqs("测验"), [])

    def test_latin_prefix_search_is_unchanged(self) -> None:
        self.assertEqual(self.seqs("login"), [6])
        self.assertEqual(self.seqs("reason"), [6])
        self.assertEqual(self.seqs("pytest"), [5])
        self.assertEqual(self.seqs("test"), [5])

    def test_snippets_show_the_original_text(self) -> None:
        results = hyadex_server.search_timeline_index(self.session_id, "测试")["results"]
        self.assertEqual(
            [result["snippet"] for result in results],
            ["Ran pytest then 运行test测试 again", "请修复登录页面，然后运行测试"],
        )
        with patch.object(hyadex_server.STORE, "sessions", {self.session_id: {"id": self.session_id}}):
            global_results = hyadex_server.search_all_timelines("中文")["results"]
        self.assertEqual([result["snippet"] for result in global_results], ["我们在讨论中文搜索的问题"])

    def test_punctuation_only_terms_are_ignored(self) -> None:
        self.assertEqual(self.seqs("login -"), [6])
        self.assertEqual(self.seqs("测试 ，"), [5, 1])
        self.assertEqual(hyadex_server.search_timeline_index(self.session_id, "- …")["results"], [])
        with patch.object(hyadex_server.STORE, "sessions", {self.session_id: {"id": self.session_id}}):
            self.assertEqual(hyadex_server.search_all_timelines("-")["results"], [])


if __name__ == "__main__":
    unittest.main()

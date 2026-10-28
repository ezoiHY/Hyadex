"""Job run outcomes, a chat's provider-reported token totals, and read-only workspace files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from hyadex.features import files as session_files

from hyadex.features.job_outcomes import apply_outcome, JobRunTracker
from hyadex.providers.token_totals import (
    add_codex_thread_total_locked,
    add_tokens_locked,
    claude_usage_tokens,
)


class JobRunTrackerTests(unittest.TestCase):
    def started(self, tracker: JobRunTracker, run_id: str = "run_1", job_id: str = "job_1") -> None:
        tracker.observe({"type": "turn_started", "run_id": run_id, "purpose": "scheduled_job", "job_id": job_id})

    def test_successful_turn_is_completed(self) -> None:
        tracker = JobRunTracker()
        self.started(tracker)
        outcome = tracker.observe({"type": "turn_finished", "run_id": "run_1", "exit_code": 0,
                                   "purpose": "scheduled_job", "job_id": "job_1"})
        self.assertEqual((outcome.job_id, outcome.status, outcome.error), ("job_1", "completed", None))
        self.assertEqual(tracker.finished("run_1"), outcome)

    def test_error_event_marks_the_run_failed_with_its_message(self) -> None:
        tracker = JobRunTracker()
        self.started(tracker)
        self.assertIsNone(tracker.observe({"type": "error", "run_id": "run_1", "message": "API overloaded"}))
        outcome = tracker.observe({"type": "turn_finished", "run_id": "run_1", "exit_code": 1})
        self.assertEqual((outcome.status, outcome.error), ("failed", "API overloaded"))

    def test_nonzero_exit_without_an_error_event_is_failed(self) -> None:
        tracker = JobRunTracker()
        self.started(tracker)
        self.assertEqual(tracker.observe({"type": "turn_finished", "run_id": "run_1", "exit_code": 2}).status,
                         "failed")

    def test_stop_is_matched_by_run_id_alone(self) -> None:
        tracker = JobRunTracker()
        self.started(tracker)
        tracker.observe({"type": "error", "run_id": "run_1", "message": "interrupted"})
        outcome = tracker.observe({"type": "turn_stopped", "run_id": "run_1"})
        self.assertEqual((outcome.status, outcome.error), ("stopped", None))

    def test_ordinary_turns_are_ignored(self) -> None:
        tracker = JobRunTracker()
        tracker.observe({"type": "turn_started", "run_id": "run_2"})
        self.assertIsNone(tracker.observe({"type": "turn_finished", "run_id": "run_2", "exit_code": 1}))

    def test_tracking_is_bounded(self) -> None:
        tracker = JobRunTracker(limit=2)
        for index in range(5):
            self.started(tracker, f"run_{index}")
            tracker.observe({"type": "turn_finished", "run_id": f"run_{index}", "exit_code": 0})
        self.assertIsNone(tracker.finished("run_0"))
        self.assertIsNotNone(tracker.finished("run_4"))

    def test_apply_outcome_replaces_the_previous_result(self) -> None:
        job: dict = {}
        apply_outcome(job, "failed", run_id="run_1", error="boom", at="t1")
        self.assertEqual((job["last_run_status"], job["last_run_error"], job["last_run_finished_at"]),
                         ("failed", "boom", "t1"))
        apply_outcome(job, "running", run_id="run_2", error=None, at="t2")
        self.assertEqual(job, {"last_run_status": "running", "last_run_id": "run_2"})
        apply_outcome(job, "dispatched", run_id=None, error=None, at="t3")
        self.assertEqual(job, {"last_run_status": "dispatched", "last_run_finished_at": "t3"})


class TokenTotalsTests(unittest.TestCase):
    def test_claude_turn_usage_counts_cached_and_created_input(self) -> None:
        tokens = claude_usage_tokens({"input_tokens": 2, "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 900, "output_tokens": 40})
        self.assertEqual(tokens, {"input_tokens": 1002, "cached_input_tokens": 900,
                                  "output_tokens": 40, "total_tokens": 1042})
        self.assertIsNone(claude_usage_tokens(None))
        self.assertIsNone(claude_usage_tokens({"input_tokens": 0, "output_tokens": 0}))

    def test_claude_turns_accumulate(self) -> None:
        session: dict = {}
        for _ in range(2):
            add_tokens_locked(session, claude_usage_tokens({"input_tokens": 10, "output_tokens": 5}))
        self.assertEqual(session["token_totals"]["total_tokens"], 30)
        self.assertEqual(session["token_totals"]["output_tokens"], 10)

    def test_codex_cumulative_samples_add_only_their_growth(self) -> None:
        session: dict = {}
        sample = {"inputTokens": 1000, "cachedInputTokens": 800, "outputTokens": 50, "totalTokens": 1050}
        add_codex_thread_total_locked(session, "thread_a", sample)
        add_codex_thread_total_locked(session, "thread_a", sample)  # repeated sample adds nothing
        add_codex_thread_total_locked(session, "thread_a",
                                      {"inputTokens": 3000, "cachedInputTokens": 2500, "outputTokens": 90,
                                       "totalTokens": 3090})
        self.assertEqual(session["token_totals"]["total_tokens"], 3090)
        add_codex_thread_total_locked(session, "thread_b", {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12})
        self.assertEqual(session["token_totals"]["total_tokens"], 3102)
        self.assertEqual(session["token_totals"]["cached_input_tokens"], 2500)

    def test_codex_thread_that_restarts_its_count_is_added_again(self) -> None:
        session: dict = {}
        add_codex_thread_total_locked(session, "t", {"totalTokens": 500, "inputTokens": 450, "outputTokens": 50})
        add_codex_thread_total_locked(session, "t", {"totalTokens": 100, "inputTokens": 90, "outputTokens": 10})
        self.assertEqual(session["token_totals"]["total_tokens"], 600)


class WorkspaceFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name).resolve()
        self.repo = base / "repo"
        self.cwd = self.repo / "app"
        self.cwd.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        (self.repo / "README.md").write_text("root")
        (self.cwd / "main.py").write_text("print(1)")
        (base / "secret.txt").write_text("no")
        (self.cwd / "escape").symlink_to(base / "secret.txt")
        sessions = {"sess_1": {"id": "sess_1", "cwd": str(self.cwd)}}
        self.store = patch.object(session_files.STORE, "sessions", sessions)
        self.store.start()

    def tearDown(self) -> None:
        self.store.stop()
        self.temporary.cleanup()

    def test_files_in_the_folder_and_its_repository_resolve(self) -> None:
        self.assertEqual(session_files.session_workspace_file("sess_1", str(self.cwd / "main.py")),
                         self.cwd / "main.py")
        # Diff paths are relative to the repository root.
        self.assertEqual(session_files.session_workspace_file("sess_1", "app/main.py"), self.cwd / "main.py")
        self.assertEqual(session_files.session_workspace_file("sess_1", "README.md"), self.repo / "README.md")

    def test_files_outside_or_behind_a_symlink_are_refused(self) -> None:
        for path, status in (("../secret.txt", 403), ("app/escape", 403), ("missing.py", 404), ("app", 404)):
            with self.subTest(path=path), self.assertRaises(HTTPException) as caught:
                session_files.session_workspace_file("sess_1", path)
            self.assertEqual(caught.exception.status_code, status)
        with self.assertRaises(HTTPException) as caught:
            session_files.session_workspace_file("sess_other", "README.md")
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

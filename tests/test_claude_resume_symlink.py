"""Claude resume when the chat folder is reached through a symlink.

Claude resolves symlinks before it records a transcript's ``cwd`` and names
its project directory (on macOS ``/tmp/x`` is stored as ``/private/tmp/x``).
These tests build a real symlinked folder so they hold on Linux as well.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from tests import hyadex_server


PROVIDER = "2e878353-f3e6-4a05-99f1-e54b6c963236"


def claude_cli_project_name(path: str) -> str:
    """The project directory name current Claude versions choose."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class ClaudeSymlinkedCwdResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.temporary.name))
        real_parent = self.root / "private" / "tmp"
        self.real = real_parent / "hyadex.features-e2e"
        self.real.mkdir(parents=True)
        self.other = real_parent / "another-workspace"
        self.other.mkdir()
        link_parent = self.root / "tmp"
        os.symlink(real_parent, link_parent)
        # The chat keeps the path the user picked; Claude records the real one.
        self.cwd = str(link_parent / self.real.name)
        self.projects = self.root / "claude-projects"
        self.projects.mkdir()
        self.projects_patch = patch.object(
            hyadex_server, "CLAUDE_PROJECTS_ROOT", self.projects,
        )
        self.projects_patch.start()

    def tearDown(self) -> None:
        self.projects_patch.stop()
        self.temporary.cleanup()

    def transcript(self, directory: str, recorded_cwd: str) -> Path:
        path = self.projects / directory / f"{PROVIDER}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "type": "user", "sessionId": PROVIDER, "cwd": recorded_cwd,
            "message": {"role": "user", "content": "Remember the word kiwi"},
        }) + "\n")
        return path

    def session(self, saved_cwd: str | None = None) -> dict:
        return {
            "id": "chat-symlink",
            "backend": hyadex_server.BACKEND_CLAUDE,
            "cwd": self.cwd,
            "session_id": PROVIDER,
            "claude_session_id": PROVIDER,
            "claude_session_cwd": self.cwd if saved_cwd is None else saved_cwd,
        }

    def test_canonical_cwd_resolves_symlinks(self) -> None:
        self.assertNotEqual(self.cwd, str(self.real))
        self.assertEqual(hyadex_server.canonical_claude_cwd(self.cwd), str(self.real))
        self.assertTrue(hyadex_server.claude_cwds_match(self.cwd, str(self.real)))
        self.assertFalse(hyadex_server.claude_cwds_match(self.cwd, str(self.other)))
        self.assertFalse(hyadex_server.claude_cwds_match(self.cwd, ""))

    def test_resumes_the_transcript_claude_recorded_under_the_real_folder(self) -> None:
        transcript = self.transcript(claude_cli_project_name(str(self.real)), str(self.real))

        self.assertTrue(hyadex_server.claude_transcript_matches_cwd(transcript, self.cwd))
        self.assertEqual(
            hyadex_server.resolve_claude_resume_provider(self.session(), self.cwd),
            (PROVIDER, None),
        )

    def test_resume_file_lookup_checks_the_resolved_project_directory(self) -> None:
        transcript = self.transcript(str(self.real).replace("/", "-"), str(self.real))

        self.assertEqual(
            hyadex_server.claude_resume_file_for_cwd(PROVIDER, self.cwd), transcript,
        )
        self.assertEqual(
            hyadex_server.claude_project_dirs_for_cwd(self.cwd),
            [
                self.projects / self.cwd.replace("/", "-"),
                self.projects / str(self.real).replace("/", "-"),
            ],
        )
        self.assertEqual(
            hyadex_server.resolve_claude_resume_provider(self.session(), self.cwd),
            (PROVIDER, None),
        )

    def test_saved_folder_through_either_path_still_resumes(self) -> None:
        self.transcript(claude_cli_project_name(str(self.real)), str(self.real))
        for saved_cwd, cwd in ((str(self.real), self.cwd), (self.cwd, str(self.real))):
            with self.subTest(saved_cwd=saved_cwd, cwd=cwd):
                self.assertEqual(
                    hyadex_server.resolve_claude_resume_provider(
                        self.session(saved_cwd), cwd,
                    ),
                    (PROVIDER, None),
                )

    def test_session_from_a_genuinely_different_folder_is_never_resumed(self) -> None:
        transcript = self.transcript(claude_cli_project_name(str(self.other)), str(self.other))

        self.assertFalse(hyadex_server.claude_transcript_matches_cwd(transcript, self.cwd))
        resolved, warning = hyadex_server.resolve_claude_resume_provider(self.session(), self.cwd)
        self.assertIsNone(resolved)
        self.assertIn("is not available for cwd", warning or "")

        resolved, warning = hyadex_server.resolve_claude_resume_provider(
            self.session(str(self.other)), self.cwd,
        )
        self.assertIsNone(resolved)
        self.assertIn("was created in", warning or "")

    def test_symlink_to_another_folder_does_not_match_this_folder(self) -> None:
        alias = self.root / "alias"
        os.symlink(self.other, alias)
        transcript = self.transcript(claude_cli_project_name(str(self.real)), str(self.real))

        self.assertFalse(hyadex_server.claude_transcript_matches_cwd(transcript, str(alias)))
        resolved, _warning = hyadex_server.resolve_claude_resume_provider(
            self.session(str(alias)), str(alias),
        )
        self.assertIsNone(resolved)

    def test_fork_validation_accepts_the_symlinked_folder(self) -> None:
        self.transcript(claude_cli_project_name(str(self.real)), str(self.real))

        self.assertEqual(
            hyadex_server.validated_claude_fork_provider_id(
                self.session(), "chat-symlink", self.cwd,
            ),
            PROVIDER,
        )


if __name__ == "__main__":
    unittest.main()

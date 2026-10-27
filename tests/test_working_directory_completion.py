"""Working-directory suggestions for the new-chat directory picker."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from tests import hyadex_server
class WorkingDirectoryCompletionTests(unittest.TestCase):
    def test_working_directory_completion_lists_only_matching_server_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Alpha").mkdir()
            (root / "alpine").mkdir()
            (root / ".archive").mkdir()
            (root / "alpha.txt").write_text("not a directory")

            exact = hyadex_server.complete_working_directory_sync(str(root), 24)
            partial = hyadex_server.complete_working_directory_sync(str(root / "al"), 24)

        self.assertTrue(exact["exists"])
        self.assertEqual([item["name"] for item in exact["suggestions"]], ["Alpha", "alpine"])
        self.assertFalse(partial["exists"])
        self.assertEqual([item["name"] for item in partial["suggestions"]], ["Alpha", "alpine"])
        self.assertTrue(all(item["path"].endswith(os.sep) for item in partial["suggestions"]))

    def test_working_directory_completion_uses_server_default_for_relative_input_and_bounds_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").mkdir()
            (root / "only").mkdir()
            with patch.object(hyadex_server, "DEFAULT_CWD", str(root)):
                result = hyadex_server.complete_working_directory_sync("on", 1)

        self.assertEqual([item["name"] for item in result["suggestions"]], ["one"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["base_path"], str(root))

    def test_working_directory_completion_reports_missing_parents_and_rejects_nul(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = hyadex_server.complete_working_directory_sync(
                str(Path(temporary) / "missing" / "child"),
                24,
            )
        self.assertFalse(result["exists"])
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["message"], "Parent directory not found.")
        with self.assertRaises(HTTPException) as raised:
            hyadex_server.complete_working_directory_sync("bad\x00path", 24)
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

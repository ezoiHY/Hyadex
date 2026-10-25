"""Exercise real source validation/staging only, never services or dependencies."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

from scripts import package_release


ROOT = Path(__file__).resolve().parents[1]


class InstallerSourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="installer-source-cache-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.stage = self.root / "stage"
        self.source.mkdir()
        self.stage.mkdir()
        for name in package_release.FILES:
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text("synthetic release file\n")
        installer = (ROOT / "install.sh").read_text()
        start = installer.index("RELEASE_FILES=(")
        end = installer.index("\ncurrent_release_binding()", start)
        validation = installer[start:end]
        copy_start = installer.index('for name in "${RELEASE_FILES[@]}"; do', end)
        copy_end = installer.index('echo "[2/7]', copy_start)
        self.script = (
            "set -euo pipefail\nACTIVATE_PREPARED=\nRECOVER_ONLY=false\nPREPARE_ONLY=false\n"
            f"SOURCE_DIR={shlex.quote(str(self.source))}\n"
            f"STAGE_DIR={shlex.quote(str(self.stage))}\n"
            + validation + "\n" + installer[copy_start:copy_end]
        )

    def run_stage(self):
        return subprocess.run(["/bin/bash", "-c", self.script], capture_output=True, text=True, timeout=10)

    def test_cache_and_retired_payload_are_not_shipped_or_modified(self):
        extras = ("__pycache__/agent_server.pyc", "agent_server.pyc", "hyadex_team_hub/private.txt")
        for name in extras:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"must remain outside the release")
        before = {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        result = self.run_stage()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p.relative_to(self.stage).as_posix() for p in self.stage.rglob("*") if p.is_file()}, set(package_release.FILES))
        self.assertEqual(before, {str(p.relative_to(self.source)): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    def test_linked_required_source_is_rejected(self):
        path = self.source / "agent_server.py"
        path.unlink()
        path.symlink_to(self.source / "VERSION")
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular release file", result.stderr)
        self.assertEqual(list(self.stage.iterdir()), [])

    def test_linked_payload_directory_is_rejected(self):
        import shutil
        shutil.rmtree(self.source / "npm")
        (self.source / "npm").symlink_to(self.stage, target_is_directory=True)
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a real directory", result.stderr)

    def test_special_required_source_is_rejected_without_opening_it(self):
        path = self.source / "agent_server.py"
        path.unlink()
        os.mkfifo(path)
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a regular release file", result.stderr)

    def test_bytecode_does_not_substitute_for_required_source(self):
        (self.source / "agent_server.py").unlink()
        (self.source / "agent_server.pyc").write_bytes(b"bytecode is not source")
        result = self.run_stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent_server.py is missing", result.stderr)
        self.assertEqual(list(self.stage.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

"""Package imports remain inert; HTTP adapters do not own process-wide state."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


class PackageBoundariesTests(unittest.TestCase):
    def test_domain_and_router_imports_do_not_load_server_or_create_state(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [sys.executable, '-B', '-c', '''
from pathlib import Path
import sys
from hyadex import groups, autopilot, installation, inventory
from hyadex.app import ApplicationBindings, create_app
from hyadex.store import cross_chat, mailbox, transcripts
from hyadex.jobs import schedules, context
from hyadex.providers import claude, codex, runtime
from hyadex.routes.groups import create_group_router
from hyadex.routes.autopilot import create_autopilot_router
assert 'agent_server' not in sys.modules
assert list(Path.home().iterdir()) == []
assert not hasattr(groups, 'create_group_router')
assert not hasattr(autopilot, 'create_autopilot_router')
'''], cwd=root, env={**os.environ, 'HOME': home}, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

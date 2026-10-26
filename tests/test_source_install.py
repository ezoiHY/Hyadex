"""Manual source-install boundaries and durable native publication."""
from dataclasses import replace
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import execution_install as files
import execution_manage as manage
from hyadex import installation
from tests import test_execution_install as install_tests
from tests import test_execution_manage as manage_tests


class SourceInstallTests(unittest.TestCase):
    def setUp(self):
        self.fixture = install_tests.ExecutionInstallTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_full_install_publishes_and_rolls_back_both_services_and_exact_config(self):
        f = self.fixture
        f.migrate()
        config = f.config / 'env'
        original = b'# retained comment\nHYADEX_AGENT_TOKEN=unchanged\nCUSTOM_SETTING="spaces stay"\n'
        files._atomic_write(config, original)
        layout = replace(f.layout, worker_release=f.new, gateway_release=f.new, port=17851,
                         environment={'HYADEX_AGENT_TOKEN': 'unchanged', 'CUSTOM_SETTING': 'spaces stay'})
        files.stage(layout, scope='install', prior_services=f.absent)
        files.publish(f.root)
        self.assertIn(b'# retained comment\n', config.read_bytes())
        self.assertEqual(installation.read_environment(config), layout.environment)
        self.assertEqual(f.layout.current.resolve(), f.new)
        self.assertEqual(files.pending_worker_operation(f.root, f.new), files.maintenance_operation_id(files._load(f.root)[0]))
        files.rollback(f.root)
        files.finish(f.root)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(f.layout.current.resolve(), f.old)
        self.assertEqual(files.installed_layout(f.root), f.layout)

    def test_retired_update_journals_block_startup_and_release_retention(self):
        f = self.fixture
        f.migrate()
        for name in files.RETIRED_ACTIVATION_NAMES:
            with self.subTest(name=name):
                marker = f.root / name
                marker.mkdir()
                try:
                    with self.assertRaisesRegex(RuntimeError, "retired in-app update"):
                        files.pending_worker_operation(f.root, f.old)
                    with self.assertRaisesRegex(RuntimeError, "retired in-app update"):
                        files.retained_releases(f.root)
                    self.assertTrue(marker.is_dir())
                finally:
                    marker.rmdir()

    def test_interrupted_new_install_config_restores_absence_and_retains_candidate(self):
        f = self.fixture
        files.stage(f.layout, scope='install', prior_services=f.absent)
        files.publish(f.root)
        self.assertTrue((f.config / 'env').exists())
        files.rollback(f.root)
        files.finish(f.root)
        self.assertFalse((f.config / 'env').exists())
        self.assertFalse(f.layout.current.exists())
        self.assertTrue(f.old.is_dir())

    def test_changed_configuration_blocks_recovery_without_overwriting_user_edit(self):
        f = self.fixture
        files.stage(f.layout, scope='install', prior_services=f.absent)
        files.publish(f.root)
        files._atomic_write(f.config / 'env', b'USER_CHANGED=this\n')
        with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
            files.rollback(f.root)
        self.assertEqual((f.config / 'env').read_bytes(), b'USER_CHANGED=this\n')

    def test_completed_journal_does_not_retain_old_or_new_credentials(self):
        f = self.fixture
        files._atomic_write(f.config / 'env', b'HYADEX_AGENT_TOKEN=old-secret\n')
        layout = replace(f.layout, environment={'HYADEX_AGENT_TOKEN': 'new-secret'})
        files.stage(layout, scope='install', prior_services=f.absent)
        files.publish(f.root)
        files.commit(f.root, f.receipt(layout))
        files.finish(f.root)
        receipt = next(f.root.glob('.execution-completed-*/manifest.json')).read_bytes()
        self.assertNotIn(b'old-secret', receipt)
        self.assertNotIn(b'new-secret', receipt)
        self.assertNotIn(b'environment', receipt)

    def test_named_layouts_use_disjoint_service_identities_on_both_platforms(self):
        for platform in ('Darwin', 'Linux'):
            layouts = [replace(self.fixture.layout, platform=platform,
                               environment={'HYADEX_SERVER_INSTANCE': name}) for name in ('default', 'work', 'other')]
            paths = {layout.service_path(role) for layout in layouts for role in ('worker', 'gateway')}
            self.assertEqual(len(paths), 6)
            for layout in layouts:
                for role in ('worker', 'gateway'):
                    data = files.render_service(layout, role)
                    if platform == 'Darwin':
                        self.assertEqual(plistlib.loads(data)['Label'], layout.service_label(role))
                    elif role == 'gateway':
                        self.assertIn('Wants=' + layout.service_unit('worker'), data.decode())

    def test_existing_service_binding_cannot_move_to_another_name(self):
        f = self.fixture
        f.migrate()
        changed = replace(f.layout, environment={'HYADEX_SERVER_INSTANCE': 'other'})
        with self.assertRaisesRegex(ValueError, 'native service identity'):
            files.stage(changed, scope='install', prior_services=f.absent)
        self.assertFalse(f.layout.transaction_dir.exists())

    def test_environment_roundtrip_is_literal_and_rejects_duplicate_keys(self):
        path = self.fixture.config / 'env'
        values = {'A': "spaces 'quotes' $HOME $(no-execution)", 'EMPTY': ''}
        layout = replace(self.fixture.layout, environment=values)
        files._atomic_write(path, files.render_environment(layout))
        self.assertEqual(installation.read_environment(path), values)
        files._atomic_write(path, b'A=one\nA=two\n')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            installation.read_environment(path)

    def test_dependency_environment_cannot_inherit_server_or_uv_selectors(self):
        with patch.dict(os.environ, {'HYADEX_AGENT_TOKEN': 'secret', 'ANTHROPIC_API_KEY': 'secret',
                                    'UV_PROJECT_ENVIRONMENT': '/wrong', 'PYTHONPATH': '/wrong'}):
            environment = installation.staging_environment(self.fixture.home)
        self.assertFalse({'HYADEX_AGENT_TOKEN', 'ANTHROPIC_API_KEY', 'UV_PROJECT_ENVIRONMENT', 'PYTHONPATH'} & environment.keys())

    def test_failed_dependency_stage_is_removed_and_empty_scaffold_can_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            root, config, state = [home / name for name in ('runtime', 'config', 'state')]
            for path in (root, config, state):
                path.mkdir(mode=0o700)
            uv = home / 'uv-fixture'
            uv.write_text('#!/bin/sh\nexit 73\n')
            uv.chmod(0o700)
            with self.assertRaises(subprocess.CalledProcessError):
                installation.stage_source(installation.ROOT, root, '1.0.0', str(uv))
            self.assertEqual(list((root / 'releases').iterdir()), [])
            installation.require_fresh_scaffold(root, config, state)
            (state / 'sessions.json').write_text('preserve')
            with self.assertRaisesRegex(RuntimeError, 'existing runtime, configuration or state'):
                installation.require_fresh_scaffold(root, config, state)
            self.assertEqual((state / 'sessions.json').read_text(), 'preserve')

    def test_dependency_timeout_stops_its_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            pid_file = home / 'child.pid'
            code = "import subprocess, pathlib, sys; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); p.wait()"
            with self.assertRaises(subprocess.TimeoutExpired):
                installation.run_stage([sys.executable, '-c', code, str(pid_file)],
                                       environment=installation.staging_environment(home), timeout=1)
            child = int(pid_file.read_text())
            result = subprocess.run(['ps', '-o', 'stat=', '-p', str(child)], capture_output=True, text=True)
            self.assertTrue(result.returncode != 0 or not result.stdout.strip() or result.stdout.strip().startswith('Z'), result.stdout)

    def test_live_incumbent_is_rejected_before_any_service_action(self):
        services = Mock()
        services.snapshot.return_value = {'worker': {'state': 'running'}, 'gateway': {'state': 'stopped'}}
        with self.assertRaisesRegex(RuntimeError, 'stop the selected instance'):
            installation.assert_stopped(services)
        self.assertEqual(services.method_calls, [unittest.mock.call.snapshot()])

    def test_legacy_pending_evidence_is_never_resumed_or_changed(self):
        path = self.fixture.root / '.activation-transaction'
        path.mkdir()
        (path / 'receipt').write_bytes(b'opaque legacy data')
        with self.assertRaisesRegex(RuntimeError, 'previous installer'):
            installation.refuse_legacy_operation(self.fixture.root)
        self.assertEqual((path / 'receipt').read_bytes(), b'opaque legacy data')

    def test_old_default_plist_without_state_variable_can_be_migrated(self):
        f = self.fixture
        layout = replace(f.layout, platform='Darwin', state_root=f.home / '.hyadex')
        layout.state_root.mkdir(mode=0o700)
        path = layout.service_path('worker')
        files._atomic_write(path, plistlib.dumps({'Label': layout.service_label('worker'),
            'ProgramArguments': [str(f.root / 'current/.venv/bin/python'), str(f.root / 'current/agent_server.py'), 'serve']}))
        installation.verify_existing_services(layout)
        with self.assertRaisesRegex(RuntimeError, 'state binding'):
            installation.verify_existing_services(replace(layout, state_root=f.state))

    def test_legacy_state_selectors_are_preserved_or_rejected_before_native_access(self):
        f = self.fixture
        custom = f.home / 'existing-state'
        with patch.dict(os.environ, {'HYADEX_SERVER_STATE_DIR': str(custom)}, clear=True):
            self.assertEqual(installation.roots('default', f.home)[2], custom)
        with patch.dict(os.environ, {'HYADEX_SERVER_STATE_DIR': str(custom), 'HYADEX_STATE_DIR': str(f.state)}, clear=True):
            with self.assertRaisesRegex(ValueError, 'conflicting state'):
                installation.roots('default', f.home)
        config = f.home / '.config/hyadex-server/env'
        files._atomic_write(config, f'HYADEX_SERVER_STATE_DIR={custom}\n'.encode())
        args = installation.argparse.Namespace(instance='default', show_token=False, recover_only=False)
        with patch.dict(os.environ, {'HOME': str(f.home)}, clear=True), patch.object(installation, 'NativeServices') as native:
            with self.assertRaisesRegex(ValueError, 'saved state path differs'):
                installation.install(args)
            native.assert_not_called()
        config.unlink()
        (f.home / '.zenithbot-agent').mkdir()
        with patch.dict(os.environ, {'HOME': str(f.home)}, clear=True), patch.object(installation, 'NativeServices') as native:
            with self.assertRaisesRegex(ValueError, 'legacy state exists'):
                installation.install(args)
            native.assert_not_called()

    def test_help_retired_flags_and_token_read_do_not_create_installation(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            env = installation.staging_environment(home)
            script = [sys.executable, '-B', str(root / 'hyadex/installation.py')]
            for args, status in ((['--help'], 0), (['--prepare-only'], 2), (['--team-hub-host'], 2), (['--show-token'], 1)):
                result = subprocess.run([*script, *args], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(list(home.iterdir()), [])
            config = home / '.config/hyadex-server'
            config.mkdir(parents=True)
            files._atomic_write(config / 'env', b'HYADEX_AGENT_TOKEN=fixture-token\n')
            result = subprocess.run([*script, '--show-token'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'fixture-token\n')
            self.assertFalse((home / '.local').exists())
            self.assertFalse((home / '.hyadex').exists())


class SourceInstallControllerTests(unittest.TestCase):
    def test_instance_controls_refuse_pending_publication_and_busy_worker(self):
        fixture = manage_tests.ExecutionManageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        files.stage(fixture.layout, scope='install', prior_services=fixture.services.snapshot())
        with patch.object(manage, 'NativeServices') as native:
            with self.assertRaisesRegex(RuntimeError, 'recovery is pending'):
                manage.control_services(fixture.layout, 'start')
            native.assert_not_called()
        files.rollback(fixture.fixture.root)
        files.finish(fixture.fixture.root)
        fixture.control.busy = True
        with patch.object(manage, 'NativeServices', return_value=fixture.services), patch.object(manage, 'WorkerControl', return_value=fixture.control):
            with self.assertRaises(RuntimeError):
                manage.control_services(fixture.layout, 'stop')
        self.assertFalse(any(event[0] == 'stop' for event in fixture.events))

    def test_failed_full_install_restores_exact_environment_and_previous_components(self):
        fixture = manage_tests.ExecutionManageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = fixture.fixture.config / 'env'
        files._atomic_write(config, b'HYADEX_AGENT_TOKEN=preserved\n# comment\n')
        before = config.read_bytes()
        layout = replace(fixture.layout, worker_release=fixture.fixture.new, gateway_release=fixture.fixture.new,
                         environment={'HYADEX_AGENT_TOKEN': 'preserved', 'NEW_OPTION': 'candidate'})
        fixture.control.bad_release = fixture.fixture.new
        with self.assertRaises(RuntimeError):
            fixture.controller(layout).apply('install')
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(files.installed_layout(fixture.fixture.root), fixture.layout)
        self.assertEqual(fixture.services.worker_release, fixture.fixture.old)
        self.assertEqual(fixture.services.gateway_release, fixture.fixture.old)

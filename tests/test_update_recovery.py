"""Exact interrupted activation recovery; no installed native jobs are touched."""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from fastapi import HTTPException
import activation_transaction as activation
from tests import test_execution_activation_transaction as fixtures
import update_recovery as recovery
import update_runner as updates


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionActivationTransactionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.item = self.fixture.layout(split=True)
        self.update_id = "a" * 32
        self.status = {"phase": "installing", "update_id": self.update_id,
            "target_version": self.item.release_version, "track": "stable"}
        self.status["_activation_recovery"] = recovery.activation_intent(
            root=self.item.root, candidate=self.item.candidate_source,
            version=self.item.release_version, api_contract=28, update_id=self.update_id,
            server_identity="server-fixture")
        self.transaction = self.fixture.begin(self.item, extra=("--execution-api-contract", "28"))
        self.status_path = self.item.state / "server-update.json"
        updates.atomic_json(self.status_path, self.status)

    def context(self):
        return recovery.recovery_context(self.item.root, self.status, server_identity="server-fixture")

    def args(self):
        return argparse.Namespace(status_file=str(self.status_path), update_id=self.update_id,
            recovery_transaction=self.transaction, expected_version=self.item.release_version,
            expected_server_identity="server-fixture", port=17850, bind="127.0.0.1")

    def test_exact_candidate_is_bound_before_and_after_publication(self):
        self.assertEqual(self.context()["transaction_id"], self.transaction)
        self.fixture.driver.activate_to_linked(self.item, self.transaction)
        self.assertEqual(self.context()["candidate_release"]["inode"],
                         self.item.release_dir.stat().st_ino)

    def test_identity_and_version_changes_cannot_resume_another_transaction(self):
        for key, value in (("server_identity", "another-server"), ("update_id", "b" * 32),
                           ("version", "99.0.0"), ("api_contract", 29), ("format", True)):
            with self.subTest(key=key), patch.dict(self.status["_activation_recovery"], {key: value}):
                with self.assertRaises(RuntimeError):
                    self.context()
        with patch.dict(self.status["_activation_recovery"]["candidate_binding"], {"inode": 1}):
            with self.assertRaisesRegex(RuntimeError, "candidate identity"):
                self.context()

    def test_reader_does_not_clean_an_installers_live_manifest_temporary(self):
        temporary = self.item.root / ".activation-transaction" / ".manifest-active.tmp"
        temporary.write_text("owned writer still publishing")
        temporary.chmod(0o600)
        self.context()
        self.assertTrue(temporary.exists())

    def test_recovery_never_downloads_or_reinstalls_and_requires_post_commit_health(self):
        with patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer") as installer, \
                patch.object(updates, "download_npm_archive") as download, \
                patch.object(updates, "assert_post_update_identity") as health, \
                patch.object(recovery, "journal_present", return_value=False):
            recovery.run_recovery(self.args())
        command = installer.call_args.args[0]
        self.assertIn("--recover-only", command)
        self.assertEqual(command[command.index("--expected-activation-id") + 1], self.transaction)
        self.assertNotIn("--activate-prepared", command)
        download.assert_not_called()
        health.assert_called_once()
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "complete")

    def test_replaced_journal_or_update_refuses_before_installer(self):
        for field, value in (("recovery_transaction", "activation-" + "b" * 24),
                             ("update_id", "b" * 32)):
            args = self.args()
            setattr(args, field, value)
            with self.subTest(field=field), \
                    patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                    patch.object(updates, "run_installer") as installer:
                with self.assertRaises(RuntimeError):
                    recovery.run_recovery(args)
                installer.assert_not_called()

    def test_verified_rollback_is_failed_retryable_not_false_success(self):
        with patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer", side_effect=updates.InstallerRolledBack()), \
                patch.object(recovery, "journal_present", return_value=False), \
                patch.object(updates, "assert_post_update_identity") as health:
            recovery.run_recovery(self.args())
        health.assert_not_called()
        result = json.loads(self.status_path.read_text())
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["error_code"], "server_update_rolled_back")
        self.assertTrue(result["retryable"])

    def test_installer_zero_with_retained_journal_never_reports_complete(self):
        with patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer"), \
                self.assertRaisesRegex(RuntimeError, "without finalizing"):
            recovery.run_recovery(self.args())
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "installing")

    def test_real_child_exit75_only_means_rollback_in_recovery_mode(self):
        script = self.item.base / "rollback-fixture.sh"
        script.write_text("#!/bin/sh\nexit 75\n")
        script.chmod(0o700)
        kwargs = dict(cwd=script.parent, status_path=self.status_path,
            log_path=self.item.base / "installer.log", version=self.item.release_version,
            expected_update_id=self.update_id)
        with self.assertRaises(updates.InstallerRolledBack):
            updates.run_installer([str(script)], accepted_returncodes=(0, 75), **kwargs)
        with self.assertRaisesRegex(RuntimeError, "installer failed"):
            updates.run_installer([str(script)], **kwargs)

    def test_interrupted_installer_keeps_legacy_admission_drained_for_recovery(self):
        argv = ["update_runner.py", "--status-file", str(self.status_path),
            "--public-key", str(self.item.base / "unused-key"), "--port", "17850", "--bind", "127.0.0.1",
            "--expected-version", self.item.release_version, "--expected-server-identity", "server-fixture",
            "--update-id", self.update_id]
        with patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(sys, "argv", argv), \
                patch.object(updates, "run_update", side_effect=RuntimeError("interrupted fixture")):
            self.assertEqual(updates.main(), 1)
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "installing")
        self.assertEqual(status["error_code"], "server_update_recovery_pending")
        with patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(sys, "argv", argv + ["--recover-only", "--recovery-transaction", self.transaction]), \
                patch.object(recovery, "run_recovery", side_effect=RuntimeError("failed fixture recovery")):
            self.assertEqual(updates.main(), 1)
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "installing")
        self.assertEqual(status["error_code"], "server_update_recovery_failed")
        self.assertTrue(status["retryable"])
        self.assertTrue((self.item.root / ".activation-transaction").is_dir())

    def test_installer_preflight_failure_without_journal_recovers_its_old_handoff(self):
        self.status["_execution_handoff"] = {"owned": True}
        argv = ["update_runner.py", "--status-file", str(self.status_path),
            "--public-key", str(self.item.base / "unused-key"), "--port", "17850", "--bind", "127.0.0.1",
            "--expected-version", self.item.release_version, "--expected-server-identity", "server-fixture",
            "--update-id", self.update_id, "--execution-handoff-file", str(self.item.base / "handoff.json")]
        for error in (None, RuntimeError("fixture callback unreachable")):
            updates.atomic_json(self.status_path, self.status)
            with self.subTest(error=bool(error)), \
                    patch.dict(os.environ, {"HYADEX_SERVER_INSTALL_DIR": str(self.item.root)}), \
                    patch.object(sys, "argv", argv), patch.object(recovery, "journal_present", return_value=False), \
                    patch.object(updates, "run_update", side_effect=RuntimeError("preflight refused")), \
                    patch("update_handoff.release_existing_handoff", side_effect=error) as release:
                self.assertEqual(updates.main(), 1)
            release.assert_called_once()
            value = json.loads(self.status_path.read_text())
            self.assertEqual(value["phase"], "failed")
            self.assertEqual(value.get("error_code"), "server_update_handoff_release_failed" if error else None)
            self.assertTrue(value["retryable"])





if __name__ == "__main__":
    unittest.main()

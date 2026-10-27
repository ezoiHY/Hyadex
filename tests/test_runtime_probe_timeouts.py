"""Passive Claude checks and bounded, non-poisoning catalog refreshes."""

import asyncio
import json
import subprocess
import sys
import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from tests import hyadex_server
def completed(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


class RuntimeProbeTimeoutTests(unittest.TestCase):
    def setUp(self):
        with hyadex_server.RUNTIME_DIAGNOSTICS_LOCK:
            hyadex_server.RUNTIME_DIAGNOSTICS.clear()
            hyadex_server.RUNTIME_DIAGNOSTIC_GENERATIONS.clear()
        token = hyadex_server.RUNTIME_CATALOG_DEADLINE.set(None)
        self.addCleanup(hyadex_server.RUNTIME_CATALOG_DEADLINE.reset, token)
        self.enterContext(patch.object(hyadex_server, "RUNTIME_CATALOG_TIMEOUT_SECONDS", 6.0))
        self.enterContext(patch.object(hyadex_server, "runner_env", return_value={}))

    def test_claude_check_only_runs_version_and_leaves_authentication_unknown(self):
        def run(cmd, **kwargs):
            self.assertEqual(cmd[1:], ["--version"])
            self.assertEqual(kwargs["timeout"], 6.0)
            return completed(cmd, "2.1.283 (Claude Code)")

        with patch.object(hyadex_server.shutil, "which", return_value="/test/claude"), patch.object(
            hyadex_server.subprocess, "run", side_effect=run,
        ):
            result = hyadex_server.probe_runtime("claude")
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["installed"])
        self.assertIsNone(result["authenticated"])
        self.assertIsNone(result["action"])

    def test_claude_catalog_refresh_never_starts_a_metadata_or_auth_process(self):
        from hyadex.providers.claude.model_catalog import clear_native_models
        clear_native_models()
        allowed = (["--help"], ["--effort", "ultracode", "--version"])
        def run(cmd, **kwargs):
            self.assertIn(cmd[1:], allowed)
            return completed(cmd, "--effort <level> (low, medium, high)")
        with patch.object(hyadex_server.subprocess, "Popen") as popen, patch.object(
            hyadex_server.subprocess, "run", side_effect=run,
        ), patch.object(hyadex_server, "discover_claude_provider_models", return_value=([], "unavailable")):
            result = hyadex_server.parse_claude_help_catalog()
        popen.assert_not_called()
        self.assertTrue(result["models"])
        self.assertIn("fallback", result["model_source"])

    def test_authentication_failure_invalidates_model_cache(self):
        with patch("hyadex.providers.claude.model_catalog.clear_native_models") as clear:
            hyadex_server.record_runtime_failure("claude", "OAuth session expired and could not be refreshed")
        clear.assert_called_once_with()

    def test_auth_timeout_is_explicit_unknown_auth_and_does_not_leak_output(self):
        error = subprocess.TimeoutExpired(
            ["codex", "login", "status"], 6,
            output=b"private-account@example.com", stderr=b"secret-token",
        )
        with patch.object(hyadex_server.shutil, "which", return_value="/test/claude"), patch.object(
            hyadex_server, "runtime_command", side_effect=[completed([], "2.1.277"), error],
        ), self.assertLogs(hyadex_server.logger, level="WARNING") as logs:
            result = hyadex_server.probe_runtime("codex")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["authenticated"])
        self.assertTrue(result["installed"])
        self.assertIn("authentication check timed out", result["message"])
        self.assertNotIn("auth login", result["action"])
        exposed = json.dumps(result) + str(logs.output)
        self.assertNotIn("private-account", exposed)
        self.assertNotIn("secret-token", exposed)

    def test_codex_explicit_signed_out_still_blocks(self):
        with patch.object(hyadex_server.shutil, "which", return_value="/test/claude"), patch.object(
            hyadex_server, "runtime_command", side_effect=[
                completed([], "codex-cli"), completed([], 'Not logged in', 1),
            ],
        ):
            result = hyadex_server.probe_runtime("codex")
        self.assertEqual(result["status"], "unauthenticated")
        self.assertFalse(result["available"])
        self.assertFalse(result["authenticated"])
        self.assertIn("codex login", result["action"])

    def test_version_help_and_other_auth_keep_six_second_limit(self):
        with patch.object(hyadex_server.subprocess, "run", return_value=completed([])) as run:
            for cmd in (["claude", "--version"], ["codex", "login", "status"], ["agent", "status"]):
                hyadex_server.runtime_command(cmd)
                self.assertEqual(run.call_args.kwargs["timeout"], 6.0)
            hyadex_server.run_catalog_command(["claude", "--help"])
            self.assertEqual(run.call_args.kwargs["timeout"], 6.0)
            hyadex_server.claude_supports_effort("ultracode")
            self.assertEqual(run.call_args.kwargs["timeout"], 6.0)

    def test_forced_claude_recheck_never_starts_auth_even_after_cached_failure(self):
        hyadex_server.record_runtime_failure("claude", "Not logged in")
        with patch.object(
            hyadex_server.shutil, "which", return_value="/test/claude",
        ), patch.object(hyadex_server.subprocess, "run", return_value=completed([], "2.1.283")) as run:
            result = hyadex_server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result["status"], "unauthenticated")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/test/claude", "--version"])

    def test_remaining_catalog_budget_clamps_each_command(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(102.0)
        with patch.object(hyadex_server.time, "monotonic", return_value=100.0), patch.object(
            hyadex_server.subprocess, "run", return_value=completed([]),
        ) as run:
            hyadex_server.runtime_command(["claude", "auth", "status"], timeout_seconds=15)
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
            hyadex_server.run_catalog_command(["claude", "--help"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)
            hyadex_server.claude_supports_effort("ultracode")
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_exhausted_budget_starts_no_subprocess(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(100.0)
        with patch.object(hyadex_server.time, "monotonic", return_value=100.0), patch.object(
            hyadex_server.subprocess, "run",
        ) as run, self.assertRaises(hyadex_server.RuntimeCatalogBudgetExpired):
            hyadex_server.runtime_command(["claude", "--version"])
        run.assert_not_called()

    def test_budget_expiry_does_not_poison_or_freshen_cached_diagnostic(self):
        previous = hyadex_server.runtime_diagnostic_payload(
            "claude", "ready", installed=True, authenticated=True, version="2.1.277",
        )
        previous.update(checked_at="2026-09-18T01:00:00Z", checked_at_epoch=0.0)
        hyadex_server.store_runtime_diagnostic(previous)
        with patch.object(hyadex_server, "probe_runtime", side_effect=hyadex_server.RuntimeCatalogBudgetExpired):
            result = hyadex_server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result, previous)
        self.assertEqual(hyadex_server.RUNTIME_DIAGNOSTICS["claude"], previous)

    def test_unchecked_provider_remains_unknown_and_uncached(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(0.0)
        with patch.object(hyadex_server, "probe_runtime") as probe, patch.object(
            hyadex_server, "runtime_executable",
        ) as resolve:
            result = hyadex_server.runtime_diagnostic("claude", force=True)
        probe.assert_not_called()
        resolve.assert_not_called()
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["installed"])
        self.assertIsNone(result["authenticated"])
        self.assertIn("deadline", result["message"])
        self.assertNotIn("claude", hyadex_server.RUNTIME_DIAGNOSTICS)

    def test_completed_signed_out_result_is_not_discarded_at_budget_boundary(self):
        clock = [100.0]
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(101.0)
        hyadex_server.store_runtime_diagnostic(hyadex_server.runtime_diagnostic_payload(
            "claude", "ready", installed=True, authenticated=True,
        ))

        def probe(backend):
            clock[0] = 101.0
            return hyadex_server.runtime_diagnostic_payload(
                backend, "unauthenticated", installed=True, authenticated=False,
            )

        with patch.object(hyadex_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            hyadex_server, "probe_runtime", side_effect=probe,
        ):
            result = hyadex_server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result["status"], "unauthenticated")
        self.assertEqual(hyadex_server.RUNTIME_DIAGNOSTICS["claude"]["status"], "unauthenticated")


    def test_budget_exhaustion_is_not_a_normal_auth_timeout(self):
        clock = [100.0]
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(102.0)

        def run(cmd, **kwargs):
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch.object(hyadex_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            hyadex_server.subprocess, "run", side_effect=run,
        ), self.assertRaises(hyadex_server.RuntimeCatalogBudgetExpired):
            hyadex_server.runtime_command(["claude", "auth", "status"], timeout_seconds=15)

    def test_models_api_cannot_outlive_remaining_budget(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(100.25)
        with patch.dict(hyadex_server.os.environ, {"ANTHROPIC_API_KEY": "test-not-a-real-key"}), patch.object(
            hyadex_server.shutil, "which", return_value="/test/curl",
        ), patch.object(hyadex_server.time, "monotonic", return_value=100.0), patch.object(
            hyadex_server.subprocess, "run", return_value=completed([], b'{"data":[]}'),
        ) as run:
            _, status = hyadex_server.discover_claude_provider_models()
        self.assertEqual(status, "success")
        self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--max-time") + 1], "0.25")

    def test_native_models_read_cache_without_allocating_a_process_budget(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(102.0)
        with patch.object(hyadex_server.time, "monotonic", return_value=100.0), patch(
            "hyadex.providers.claude.model_catalog.cached_native_models", return_value=[],
        ) as probe:
            _, status = hyadex_server.discover_claude_native_models()
        self.assertEqual(status, "success")
        self.assertNotIn("timeout", probe.call_args.kwargs)

    def test_native_models_expired_budget_starts_no_process(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(100.0)
        with patch.object(hyadex_server.time, "monotonic", return_value=100.0), patch(
            "hyadex.providers.claude.model_catalog.cached_native_models", return_value=None,
        ), patch("subprocess.Popen") as probe:
            models, status = hyadex_server.discover_claude_native_models()
        probe.assert_not_called()
        self.assertEqual((models, status), ([], "unavailable"))

    def test_models_api_respects_callers_smaller_candidate_budget(self):
        with patch.dict(hyadex_server.os.environ, {"ANTHROPIC_API_KEY": "synthetic-key"}), patch.object(
            hyadex_server.shutil, "which", return_value="/test/curl",
        ), patch.object(hyadex_server.subprocess, "run", return_value=completed([], b'{"data":[]}')) as run:
            _, status = hyadex_server.discover_claude_provider_models(timeout_seconds=2.0)
        self.assertEqual(status, "success")
        self.assertEqual(run.call_args.kwargs["timeout"], 3.0)  # One second to reap curl.
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--max-time") + 1], "2")

    def test_catalog_deadline_restored_on_exception_and_not_shared_between_threads(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(123.0)
        seen = []
        thread = threading.Thread(target=lambda: seen.append(hyadex_server.RUNTIME_CATALOG_DEADLINE.get()))
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(seen, [None])
        with patch.object(hyadex_server, "discover_runtime_catalog_within_budget", side_effect=ValueError("test")):
            with self.assertRaises(ValueError):
                hyadex_server.discover_runtime_catalog()
        self.assertEqual(hyadex_server.RUNTIME_CATALOG_DEADLINE.get(), 123.0)

    def test_full_catalog_slow_claude_and_stalled_peers_share_25_second_budget(self):
        clock = [100.0]
        started = []

        class InlinePool:
            """Deterministic worst-case budget without a racing virtual clock."""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def submit(self, fn, *args, **kwargs):
                future = Future()
                future.set_result(fn(*args, **kwargs))
                return future

        def run(cmd, **kwargs):
            self.assertLess(clock[0], 125.0)
            started.append((cmd, kwargs["timeout"]))
            if cmd[0] == "/test/claude" and cmd[1:] == ["--version"]:
                clock[0] += 0.1
                return completed(cmd, "2.1.277")
            if cmd[0] == "/test/claude" and cmd[1:] == ["auth", "status", "--json"]:
                self.fail("catalog must not invoke Claude authentication commands")
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch.object(hyadex_server.time, "monotonic", side_effect=lambda: clock[0]), patch.object(
            hyadex_server.shutil, "which", side_effect=lambda cmd, **kw: "/test/" + cmd,
        ), patch.object(hyadex_server, "CLAUDE_BIN", "claude"), patch.object(
            hyadex_server.subprocess, "run", side_effect=run,
        ), patch.object(hyadex_server, "codex_user_config_defaults", return_value=("", "", "")), patch.dict(
            hyadex_server.os.environ, {"ANTHROPIC_API_KEY": ""},
        ), patch.object(
            hyadex_server, "ThreadPoolExecutor", return_value=InlinePool(),
        ):
            result = hyadex_server.discover_runtime_catalog(force_runtime_probe=True)
        self.assertLessEqual(clock[0] - 100, 25.0)
        self.assertTrue(started)
        self.assertEqual(set(result["backends"]), hyadex_server.VALID_BACKENDS)
        self.assertEqual(result["backends"]["claude"]["diagnostic"]["status"], "unknown")
        self.assertNotIn("opencode", hyadex_server.RUNTIME_DIAGNOSTICS)
        self.assertTrue(result["backends"]["claude"]["models"])
        self.assertIsNone(hyadex_server.RUNTIME_CATALOG_DEADLINE.get())

    def test_provider_checks_start_concurrently_and_inherit_refresh_deadline(self):
        hyadex_server.RUNTIME_CATALOG_DEADLINE.set(123.0)
        barrier = threading.Barrier(len(hyadex_server.VALID_BACKENDS))
        seen = {}
        lock = threading.Lock()

        def probe(backend, *, force_runtime_probe):
            with lock:
                seen[backend] = (force_runtime_probe, hyadex_server.RUNTIME_CATALOG_DEADLINE.get())
            # A sequential implementation would time out before reaching the
            # other providers, rather than letting the whole group proceed.
            barrier.wait(timeout=5)
            return {"backend": backend, "status": "ready"}

        with patch.object(hyadex_server, "discover_runtime_backend_catalog", side_effect=probe):
            results = hyadex_server.discover_runtime_catalog_within_budget(force_runtime_probe=True)
        self.assertEqual(set(results["backends"]), hyadex_server.VALID_BACKENDS)
        self.assertEqual(seen, {backend: (True, 123.0) for backend in hyadex_server.VALID_BACKENDS})
        self.assertEqual(hyadex_server.RUNTIME_CATALOG_DEADLINE.get(), 123.0)

    def test_slow_peer_diagnostic_cannot_starve_healthy_codex_models(self):
        slow_started = threading.Event()
        models_finished = threading.Event()
        static = {"models": [], "efforts": []}

        def diagnostic(backend, *, force):
            if backend == "claude":
                slow_started.set()
                self.assertTrue(models_finished.wait(timeout=5))
                return {"backend": backend, "status": "error"}
            return {"backend": backend, "status": "ready", "_executable": "/test/" + backend}

        def codex_models(**_kwargs):
            self.assertTrue(slow_started.wait(timeout=5))
            models_finished.set()
            return {"models": [{"value": "codex/test", "label": "Test"}], "efforts": []}

        with patch.object(hyadex_server, "runtime_diagnostic", side_effect=diagnostic), patch.object(
            hyadex_server, "parse_claude_help_catalog", return_value=dict(static),
        ), patch.object(
            hyadex_server, "discover_codex_catalog", side_effect=codex_models,
        ):
            result = hyadex_server.discover_runtime_catalog(force_runtime_probe=True)
        self.assertEqual(result["backends"]["codex"]["models"][0]["value"], "codex/test")
        self.assertTrue(result["backends"]["codex"]["available"])

    def test_http_catalog_route_propagates_budget_to_worker_and_returns_json(self):
        seen = []

        def discover(**kwargs):
            seen.append((kwargs, hyadex_server.RUNTIME_CATALOG_DEADLINE.get()))
            return {"backends": {}, "generated_at": "test"}

        with patch.object(hyadex_server, "discover_runtime_catalog_within_budget", side_effect=discover):
            result = asyncio.run(hyadex_server.runtime_catalog(refresh=True))
        self.assertEqual(json.loads(json.dumps(result))["backends"], {})
        self.assertEqual(seen[0][0], {"force_runtime_probe": True})
        self.assertIsInstance(seen[0][1], float)
        self.assertIsNone(hyadex_server.RUNTIME_CATALOG_DEADLINE.get())

    def test_real_subprocess_timeout_kills_and_reaps_child(self):
        # Exercise real subprocess cleanup, without a provider or credentials.
        with self.assertRaises(subprocess.TimeoutExpired):
            hyadex_server.runtime_command(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_seconds=0.1,
            )


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


def completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class RuntimeDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.native_models = self.enterContext(patch.object(
            agent_server, "discover_claude_native_models", return_value=([], "unavailable"),
        ))
        with agent_server.RUNTIME_DIAGNOSTICS_LOCK:
            agent_server.RUNTIME_DIAGNOSTICS.clear()
            agent_server.RUNTIME_DIAGNOSTIC_GENERATIONS.clear()

    def test_codex_login_hint_expires_cached_denial_without_claiming_authentication(self):
        agent_server.record_runtime_failure("codex", "Not logged in")
        agent_server.expire_codex_login_diagnostic()
        expired = agent_server.RUNTIME_DIAGNOSTICS["codex"]
        self.assertEqual(expired["status"], "unauthenticated")
        self.assertEqual(expired["checked_at_epoch"], 0)
        fresh = agent_server.runtime_diagnostic_payload("codex", "ready", installed=True, authenticated=True)
        with patch.object(agent_server, "probe_runtime", return_value=fresh) as probe:
            self.assertEqual(agent_server.runtime_diagnostic("codex")["status"], "ready")
        probe.assert_called_once_with("codex")

    def test_codex_login_hint_fences_an_older_probe_and_leaves_recheck_due(self):
        agent_server.record_runtime_success("codex")
        def older_probe(_backend):
            agent_server.expire_codex_login_diagnostic()
            return agent_server.runtime_diagnostic_payload("codex", "unauthenticated", installed=True, authenticated=False)
        with patch.object(agent_server, "probe_runtime", side_effect=older_probe):
            result = agent_server.runtime_diagnostic("codex", force=True)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["checked_at_epoch"], 0)

    def test_missing_runtime_is_explicit(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value=None):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["available"])
        self.assertIn("Install Claude Code", diagnostic["action"])

    def test_broken_tmux_is_reported_unavailable_but_optional(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/tmux"), patch.object(
            agent_server.subprocess,
            "run",
            return_value=completed(["/usr/local/bin/tmux", "-V"], returncode=127),
        ) as run:
            capability = agent_server.tmux_capability()

        self.assertFalse(capability["available"])
        self.assertFalse(capability["required"])
        self.assertIn("failed its version check", capability["message"])
        run.assert_called_once()

    def test_failed_tmux_probe_is_cached_for_health_polling(self) -> None:
        agent_server.TMUX_PROBE_CACHE.update({
            "path": None,
            "available": False,
            "checked_at": 0.0,
        })
        with patch.object(
            agent_server.shutil,
            "which",
            return_value="/usr/local/bin/tmux",
        ), patch.object(
            agent_server.subprocess,
            "run",
            side_effect=OSError("cannot execute tmux"),
        ) as run:
            self.assertIsNone(agent_server.working_tmux_bin(use_cache=True))
            self.assertIsNone(agent_server.working_tmux_bin(use_cache=True))

        run.assert_called_once()

    def test_missing_tmux_is_reported_unavailable_but_optional(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value=None), patch.object(
            agent_server.subprocess,
            "run",
        ) as run:
            capability = agent_server.tmux_capability()

        self.assertFalse(capability["available"])
        self.assertFalse(capability["required"])
        self.assertIn("rest of HyadexServer works without it", capability["message"])
        run.assert_not_called()

    def test_working_tmux_is_reported_available(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/tmux"), patch.object(
            agent_server.subprocess,
            "run",
            return_value=completed(["/usr/local/bin/tmux", "-V"]),
        ):
            capability = agent_server.tmux_capability()

        self.assertTrue(capability["available"])
        self.assertFalse(capability["required"])

    def test_claude_probe_does_not_read_account_identity(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/claude"), patch.object(
            agent_server, "runtime_command",
            return_value=completed(["claude", "--version"], stdout="2.3.4 (Claude Code)\n"),
        ) as command:
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        command.assert_called_once_with(["/usr/local/bin/claude", "--version"])
        self.assertEqual(diagnostic["status"], "unknown")
        self.assertEqual(diagnostic["version"], "2.3.4 (Claude Code)")
        self.assertIsNone(diagnostic["authenticated"])
        self.assertIsNone(diagnostic["action"])

    def test_codex_auth_failure_is_actionable(self) -> None:
        responses = [
            completed(["codex", "--version"], stdout="codex-cli 1.2.3\n"),
            completed(["codex", "login", "status"], returncode=1, stderr="Not logged in"),
        ]
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/codex"), patch.object(
            agent_server, "runtime_command", side_effect=responses
        ):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CODEX)
        self.assertEqual(diagnostic["status"], "unauthenticated")
        self.assertIn("codex login", diagnostic["action"])










    def test_stale_probe_cannot_overwrite_newer_runtime_failure(self) -> None:
        probe_started = threading.Event()
        release_probe = threading.Event()
        result: dict[str, dict] = {}

        def slow_probe(_backend: str) -> dict:
            probe_started.set()
            self.assertTrue(release_probe.wait(timeout=5))
            return agent_server.runtime_diagnostic_payload(
                "codex",
                "ready",
                installed=True,
                authenticated=True,
                executable="/bin/agent",
            )

        def refresh() -> None:
            result["diagnostic"] = agent_server.runtime_diagnostic(
                "codex",
                force=True,
            )

        with patch.object(agent_server, "probe_runtime", side_effect=slow_probe):
            thread = threading.Thread(target=refresh)
            thread.start()
            self.assertTrue(probe_started.wait(timeout=5))
            agent_server.record_runtime_failure(
                "codex",
                "Codex authentication failed.",
                executable="/bin/agent",
                auth_failure=True,
            )
            release_probe.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result["diagnostic"]["status"], "unauthenticated")
        self.assertEqual(
            agent_server.RUNTIME_DIAGNOSTICS["codex"][
                "status"
            ],
            "unauthenticated",
        )





    def test_codex_model_effort_validation_accepts_supported_ultra(self) -> None:
        self.assertEqual(
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "gpt-5.6-sol",
                "ultra",
                strict=True,
            ),
            "ultra",
        )

    def test_codex_model_effort_validation_rejects_known_bad_pair(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "gpt-5.6-luna",
                "ultra",
                strict=True,
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("gpt-5.6-luna", str(raised.exception.detail))
        self.assertIn("does not support effort ultra", str(raised.exception.detail))

    def test_codex_model_effort_validation_leaves_custom_models_provider_owned(self) -> None:
        self.assertEqual(
            agent_server.normalize_runtime_effort_for_model(
                agent_server.BACKEND_CODEX,
                "custom-provider-model",
                "ultra",
                strict=True,
            ),
            "ultra",
        )

    def test_codex_catalog_preserves_model_scoped_efforts(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "ultra"},
                    ],
                },
                {
                    "slug": "gpt-5.6-luna",
                    "display_name": "GPT-5.6-Luna",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "max"},
                    ],
                },
            ]
        }
        with patch.object(
            agent_server,
            "run_catalog_command",
            return_value=json.dumps(payload),
        ), patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("gpt-5.6-sol", "medium", "priority"),
        ):
            catalog = agent_server.discover_codex_catalog()

        self.assertEqual(
            [option["value"] for option in catalog["model_efforts"]["gpt-5.6-sol"]],
            ["medium", "ultra"],
        )
        self.assertEqual(
            [option["value"] for option in catalog["model_efforts"]["gpt-5.6-luna"]],
            ["medium", "max"],
        )

    def test_codex_catalog_default_matches_runtime_fallback(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [{"effort": "ultra"}],
                },
                {
                    "slug": agent_server.CODEX_DEFAULT_MODEL,
                    "display_name": "Runtime fallback",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "xhigh",
                    "supported_reasoning_levels": [{"effort": "xhigh"}],
                },
            ]
        }
        with patch.object(
            agent_server,
            "run_catalog_command",
            return_value=json.dumps(payload),
        ), patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("", "", ""),
        ):
            catalog = agent_server.discover_codex_catalog()

        self.assertEqual(catalog["default_model"], agent_server.CODEX_DEFAULT_MODEL)

    def test_codex_catalog_default_stays_truthful_when_runtime_fallback_is_not_discovered(self) -> None:
        payload = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_in_api": True,
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": [
                        {"effort": "medium"},
                        {"effort": "ultra"},
                    ],
                },
            ]
        }
        session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "model": None,
            "effort": None,
        }
        with patch.object(
            agent_server,
            "run_catalog_command",
            return_value=json.dumps(payload),
        ), patch.object(
            agent_server,
            "codex_user_config_defaults",
            return_value=("", "", ""),
        ), patch.object(
            agent_server,
            "CODEX_DEFAULT_MODEL",
            "gpt-5.5",
        ), patch.object(
            agent_server,
            "CODEX_DEFAULT_EFFORT",
            "xhigh",
        ):
            catalog = agent_server.discover_codex_catalog()
            runtime_model, runtime_effort, runtime_service_tier = (
                agent_server.codex_runtime_settings(session)
            )
            command = agent_server.build_codex_cmd(
                "chat",
                session,
                "probe",
                Path("/tmp/current.json"),
            )

        self.assertEqual(catalog["default_model"], runtime_model)
        self.assertEqual(catalog["default_effort"], runtime_effort)
        self.assertEqual(catalog["default_service_tier"] or "", runtime_service_tier)
        self.assertEqual(
            catalog["models"][0]["label"],
            f"Server default ({agent_server.title_model_label(runtime_model)})",
        )
        self.assertEqual(catalog["models"][0]["value"], "")
        self.assertIn(
            "gpt-5.6-sol",
            [option["value"] for option in catalog["models"]],
        )
        self.assertEqual(
            [
                option["value"]
                for option in catalog["model_efforts"][runtime_model]
            ],
            ["low", "medium", "high", "xhigh"],
        )
        self.assertEqual(command[command.index("--model") + 1], runtime_model)
        self.assertIn(f"model_reasoning_effort={runtime_effort}", command)

    def test_transient_provider_failure_keeps_cli_ready(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CLAUDE,
            "ready",
            installed=True,
            authenticated=True,
            version="2.3.4",
        ))
        agent_server.record_runtime_failure(agent_server.BACKEND_CLAUDE, "529 overloaded")
        snapshot = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CLAUDE]
        self.assertEqual(snapshot["status"], "ready")
        self.assertIsNotNone(snapshot["last_error"])
        self.assertNotIn("checked_at_epoch", snapshot)

    def test_provider_thread_not_found_does_not_mark_cli_missing(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "ready",
            installed=True,
            authenticated=True,
            version="1.2.3",
        ))
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            "No conversation found with session ID: external-thread",
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "ready")
        self.assertTrue(diagnostic["installed"])

    def test_spawn_failure_marks_cli_missing(self) -> None:
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            FileNotFoundError(2, "No such file or directory", "codex"),
            spawn_failure=True,
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["installed"])

    def test_claude_catalog_uses_native_versions_without_changing_alias_values(self) -> None:
        self.native_models.return_value = ([
            {"value": "opus[1m]", "label": "Opus 5.5 (1M context)"},
            {"value": "sonnet", "label": "Sonnet 5"},
        ], "success")
        with patch.object(agent_server, "run_catalog_command", return_value="--effort <level> (low, high)"), patch.object(
            agent_server, "claude_supports_effort", return_value=False,
        ), patch.object(agent_server, "discover_claude_provider_models", return_value=([], "unavailable")) as api, patch.dict(
            agent_server.os.environ, {"CLAUDE_MODEL": "sonnet"},
        ):
            result = agent_server.parse_claude_help_catalog()
        self.assertEqual(result["models"], [
            {"value": "", "label": "Server default (Sonnet 5)"},
            {"value": "opus[1m]", "label": "Opus 5.5 (1M context)"},
            {"value": "sonnet", "label": "Sonnet 5"},
        ])
        self.assertEqual(result["model_source"], "Cached Claude SDK initialize")
        self.assertEqual(result["default_model"], "sonnet")
        api.assert_called_once_with(timeout_seconds=2.0)

    def test_claude_native_empty_list_does_not_reintroduce_restricted_models(self) -> None:
        self.native_models.return_value = ([], "success")
        with patch.object(agent_server, "run_catalog_command", return_value="--model <model> 'opus'"), patch.object(
            agent_server, "claude_supports_effort", return_value=False,
        ), patch.object(agent_server, "discover_claude_provider_models", return_value=([], "unavailable")) as api:
            result = agent_server.parse_claude_help_catalog()
        self.assertEqual([option["value"] for option in result["models"]], [""])
        self.assertEqual(result["model_source"], "Cached Claude SDK initialize")
        api.assert_called_once_with(timeout_seconds=2.0)

    def test_claude_api_models_do_not_expand_cached_native_permissions(self) -> None:
        self.native_models.return_value = ([
            {"value": "opus", "label": "Opus 5.5"},
            {"value": "claude-opus-5", "label": "Opus 5"},
        ], "success")
        with patch.object(agent_server, "run_catalog_command", return_value=""), patch.object(
            agent_server, "claude_supports_effort", return_value=False,
        ), patch.object(agent_server, "discover_claude_provider_models", return_value=([
            {"value": "claude-opus-5", "label": "Opus 5"},
            {"value": "claude-org-blocked", "label": "Blocked"},
        ], "success")):
            result = agent_server.parse_claude_help_catalog()
        self.native_models.assert_called_once_with()
        self.assertIn({"value": "claude-opus-5", "label": "Opus 5"}, result["models"])
        self.assertNotIn("claude-org-blocked", [row["value"] for row in result["models"]])

    def test_claude_native_labels_survive_help_failure(self) -> None:
        self.native_models.return_value = ([{"value": "opus", "label": "Opus 6.1"}], "success")
        with patch.object(agent_server, "run_catalog_command", side_effect=RuntimeError), patch.object(
            agent_server, "claude_supports_effort", return_value=False,
        ):
            result = agent_server.parse_claude_help_catalog()
        self.assertIn({"value": "opus", "label": "Opus 6.1"}, result["models"])
        self.assertNotIn("current fallback", result["model_source"])

    def test_claude_catalog_parses_wrapped_effort_levels(self) -> None:
        help_text = """\
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --exclude-dynamic-system-prompt-sections
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=False
        ), patch.object(
            agent_server,
            "discover_claude_provider_models",
            return_value=([], "unavailable"),
        ):
            catalog = agent_server.parse_claude_help_catalog()
        self.assertEqual(
            [option["value"] for option in catalog["efforts"]],
            ["", "low", "medium", "high", "xhigh", "max"],
        )

    def test_claude_catalog_advertises_supported_ultracode_effort(self) -> None:
        help_text = """\
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=True
        ), patch.object(
            agent_server,
            "discover_claude_provider_models",
            return_value=([], "unavailable"),
        ):
            catalog = agent_server.parse_claude_help_catalog()
        self.assertEqual(
            [option["value"] for option in catalog["efforts"]],
            ["", "low", "medium", "high", "xhigh", "max", "ultracode"],
        )

    def test_claude_help_model_parser_keeps_aliases_and_full_name_example(self) -> None:
        help_text = """\
  --model <model>                       Provide an alias for the latest model
                                        (e.g. 'fable', 'opus', or 'sonnet') or
                                        a model's full name (e.g.
                                        'claude-fable-5-1').
  -n, --name <name>                     Set a display name
"""
        self.assertEqual(
            [
                option["value"]
                for option in agent_server.parse_claude_help_model_options(help_text)
            ],
            ["fable", "opus", "sonnet", "claude-fable-5-1"],
        )

    def test_claude_catalog_fallback_includes_current_official_models(self) -> None:
        help_text = """\
  --model <model>                       Provide an alias for the latest model
                                        (e.g. 'fable', 'opus', or 'sonnet')
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=False
        ), patch.object(
            agent_server,
            "discover_claude_provider_models",
            return_value=([], "unavailable"),
        ):
            catalog = agent_server.parse_claude_help_catalog()

        by_value = {option["value"]: option for option in catalog["models"]}
        self.assertIn("fable", by_value)
        self.assertIn("opus", by_value)
        self.assertIn("sonnet", by_value)
        self.assertEqual(by_value["claude-fable-5-1"]["label"], "Fable 5.1")
        self.assertEqual(by_value["claude-mythos-5-1"]["availability"], "limited")
        self.assertIn("claude-opus-5", by_value)
        self.assertIn("claude-sonnet-5", by_value)
        self.assertIn("current fallback", catalog["model_source"])

    def test_claude_catalog_uses_account_scoped_models_without_extra_pins(self) -> None:
        provider_options = [
            agent_server.runtime_option("claude-account-model-7", "Account Model 7")
        ]
        help_text = """\
  --model <model>                       Provide an alias for the latest model
                                        (e.g. 'fable', 'opus', or 'sonnet') or
                                        a model's full name (e.g.
                                        'claude-fable-5' or
                                        'anthropic.claude-fable-5')
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=False
        ), patch.object(
            agent_server,
            "discover_claude_provider_models",
            return_value=(provider_options, "success"),
        ):
            catalog = agent_server.parse_claude_help_catalog()

        values = [option["value"] for option in catalog["models"]]
        self.assertIn("fable", values)
        self.assertIn("claude-account-model-7", values)
        self.assertNotIn("claude-fable-5-1", values)
        self.assertNotIn("claude-fable-5", values)
        self.assertNotIn("anthropic.claude-fable-5", values)
        self.assertNotIn("claude-opus-4-8[1m]", values)
        self.assertIn("Anthropic Models API", catalog["model_source"])
        self.assertNotIn("current fallback", catalog["model_source"])

    def test_claude_catalog_treats_successful_empty_provider_list_as_authoritative(self) -> None:
        help_text = """\
  --model <model>                       Provide an alias for the latest model
                                        (e.g. 'fable', 'opus', or 'sonnet') or
                                        a full name (e.g. 'claude-fable-5')
"""
        with patch.object(agent_server, "run_catalog_command", return_value=help_text), patch.object(
            agent_server, "claude_supports_effort", return_value=False
        ), patch.object(
            agent_server,
            "discover_claude_provider_models",
            return_value=([], "success"),
        ):
            catalog = agent_server.parse_claude_help_catalog()

        values = [option["value"] for option in catalog["models"]]
        self.assertIn("fable", values)
        self.assertIn("opus", values)
        self.assertIn("sonnet", values)
        self.assertNotIn("claude-fable-5", values)
        self.assertNotIn("claude-fable-5-1", values)
        self.assertNotIn("claude-opus-4-8[1m]", values)
        self.assertIn("Anthropic Models API", catalog["model_source"])
        self.assertNotIn("current fallback", catalog["model_source"])

    def test_claude_models_api_discovery_is_account_scoped_and_bounded(self) -> None:
        payload = json.dumps({
            "data": [
                {"id": "claude-fable-5-1", "display_name": "Claude Fable 5.1"},
                {"id": "not valid whitespace", "display_name": "Invalid"},
            ]
        }).encode("utf-8")
        captured: dict[str, object] = {}

        def run_curl(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            captured["headers"] = kwargs["input"].decode("utf-8")
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=payload,
                stderr=b"",
            )

        with patch.dict(
            agent_server.os.environ,
            {
                "ANTHROPIC_API_KEY": "test-secret-never-log",
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            },
        ), patch.object(
            agent_server.shutil,
            "which",
            return_value="/usr/bin/curl",
        ), patch.object(
            agent_server.subprocess,
            "run",
            side_effect=run_curl,
        ):
            options, status = agent_server.discover_claude_provider_models()

        self.assertEqual(status, "success")
        self.assertEqual(
            options,
            [{"value": "claude-fable-5-1", "label": "Claude Fable 5.1"}],
        )
        args = captured["args"]
        kwargs = captured["kwargs"]
        self.assertEqual(args[1], "--disable")
        self.assertEqual(args[-1], "https://api.anthropic.com/v1/models?limit=1000")
        self.assertNotIn("--noproxy", args)
        self.assertIn("--max-time", args)
        self.assertIn("--max-filesize", args)
        self.assertEqual(args[args.index("--header") + 1], "@-")
        self.assertNotIn("test-secret-never-log", " ".join(args))
        self.assertIn("x-api-key: test-secret-never-log", captured["headers"])
        for secret_name in agent_server.CLAUDE_PROVIDER_SECRET_ENV_NAMES:
            self.assertNotIn(secret_name, kwargs["env"])
        self.assertLessEqual(kwargs["timeout"], 7.0)

    def test_claude_models_api_is_not_contacted_without_api_key(self) -> None:
        with patch.dict(agent_server.os.environ, {"ANTHROPIC_API_KEY": ""}), patch.object(
            agent_server.shutil,
            "which",
        ) as which:
            self.assertEqual(
                agent_server.discover_claude_provider_models(),
                ([], "unavailable"),
            )
        which.assert_not_called()

    def test_claude_models_api_rejects_non_loopback_plain_http(self) -> None:
        with patch.dict(
            agent_server.os.environ,
            {
                "ANTHROPIC_API_KEY": "test-secret-never-log",
                "ANTHROPIC_BASE_URL": "http://models.example.test",
            },
        ), patch.object(agent_server.shutil, "which") as which, patch.object(
            agent_server.subprocess,
            "run",
        ) as run:
            self.assertEqual(
                agent_server.discover_claude_provider_models(),
                ([], "failed"),
            )
        which.assert_not_called()
        run.assert_not_called()

    def test_claude_models_api_allows_loopback_plain_http(self) -> None:
        result = subprocess.CompletedProcess(
            args=["curl"],
            returncode=0,
            stdout=b'{"data": []}',
            stderr=b"",
        )
        with patch.dict(
            agent_server.os.environ,
            {
                "ANTHROPIC_API_KEY": "test-secret-never-log",
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8099/v1",
                "ALL_PROXY": "http://proxy.example.test:8080",
                "HTTP_PROXY": "http://proxy.example.test:8080",
            },
        ), patch.object(
            agent_server.shutil,
            "which",
            return_value="/usr/bin/curl",
        ), patch.object(
            agent_server.subprocess,
            "run",
            return_value=result,
        ) as run:
            self.assertEqual(
                agent_server.discover_claude_provider_models(),
                ([], "success"),
            )
        self.assertEqual(
            run.call_args.args[0][-1],
            "http://127.0.0.1:8099/v1/models?limit=1000",
        )
        args = run.call_args.args[0]
        self.assertEqual(args[1], "--disable")
        self.assertEqual(args[args.index("--noproxy") + 1], "*")

    def test_claude_models_api_trickle_has_hard_process_deadline(self) -> None:
        with patch.dict(
            agent_server.os.environ,
            {
                "ANTHROPIC_API_KEY": "test-secret-never-log",
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            },
        ), patch.object(
            agent_server.shutil,
            "which",
            return_value="/usr/bin/curl",
        ), patch.object(
            agent_server.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("curl", 7),
        ) as run:
            self.assertEqual(
                agent_server.discover_claude_provider_models(),
                ([], "failed"),
            )

        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertIn("--max-time", args)
        self.assertLessEqual(float(args[args.index("--max-time") + 1]), 6.0)
        self.assertLessEqual(kwargs["timeout"], 7.0)
        self.assertNotIn("test-secret-never-log", " ".join(args))

    def test_claude_effort_probe_rejects_unknown_values(self) -> None:
        warning = "Warning: Unknown --effort value 'ultracode' - ignoring it and using the default effort."
        with patch.object(agent_server.subprocess, "run", return_value=completed([], stderr=warning)):
            self.assertFalse(agent_server.claude_supports_effort("ultracode"))

    def test_claude_effort_probe_accepts_silent_values(self) -> None:
        with patch.object(agent_server.subprocess, "run", return_value=completed([], stdout="2.1.207 (Claude Code)\n")):
            self.assertTrue(agent_server.claude_supports_effort("ultracode"))





class RuntimePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_runtime_fails_before_launch(self) -> None:
        diagnostic = agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "unauthenticated",
            installed=True,
            authenticated=False,
        )
        with patch.object(agent_server, "runtime_diagnostic", return_value=diagnostic):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.ensure_runtime_available(agent_server.BACKEND_CODEX)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "runtime_unavailable")
        self.assertEqual(raised.exception.detail["backend"], "codex")





class SessionRuntimeValidationTests(unittest.IsolatedAsyncioTestCase):

    async def test_update_rejects_incompatible_effort_without_mutating_session(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-luna",
                "effort": "medium",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            with self.assertRaises(HTTPException):
                await store.update("chat", {"effort": "ultra"})

        self.assertEqual(store.sessions["chat"]["effort"], "medium")

    async def test_model_only_change_clears_existing_incompatible_effort(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "ultra",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            updated = await store.update("chat", {"model": "gpt-5.6-luna"})

        self.assertEqual(updated["model"], "gpt-5.6-luna")
        self.assertIsNone(updated["effort"])

    async def test_explicit_incompatible_model_effort_pair_is_rejected(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "ultra",
                "folder": "General",
            }
        }
        with patch.object(store, "save", AsyncMock()):
            with self.assertRaises(HTTPException):
                await store.update(
                    "chat",
                    {"model": "gpt-5.6-luna", "effort": "ultra"},
                )

        self.assertEqual(store.sessions["chat"]["model"], "gpt-5.6-sol")
        self.assertEqual(store.sessions["chat"]["effort"], "ultra")

    async def test_supported_ultra_pair_is_persisted(self) -> None:
        store = agent_server.SessionStore()
        store.sessions = {
            "chat": {
                "id": "chat",
                "backend": agent_server.BACKEND_CODEX,
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "folder": "General",
            }
        }
        save = AsyncMock()
        with patch.object(store, "save", save):
            updated = await store.update("chat", {"effort": "ultra"})

        self.assertEqual(updated["effort"], "ultra")
        save.assert_awaited_once()


class NativeModelDiscoveryIntegrationTests(unittest.TestCase):
    def test_native_cache_uses_runtime_environment_without_a_process(self):
        with patch.object(agent_server, "runner_env", return_value={"SAFE": "1"}), patch.object(
            agent_server, "RUNTIME_CATALOG_TIMEOUT_SECONDS", 2.0,
        ), patch(
            "hyadex.providers.claude.model_catalog.cached_native_models",
            return_value=[{"value": "opus", "label": "Opus 5.5"}],
        ) as probe:
            models, status = agent_server.discover_claude_native_models()
        self.assertEqual(status, "success")
        self.assertEqual(models[0]["label"], "Opus 5.5")
        probe.assert_called_once_with(agent_server.CLAUDE_BIN, env={"SAFE": "1"})

    def test_native_probe_failure_does_not_leak_or_poison_readiness(self):
        ready = {"status": "ready"}
        with patch.dict(agent_server.RUNTIME_DIAGNOSTICS, {"claude": ready}, clear=True), patch(
            "hyadex.providers.claude.model_catalog.cached_native_models", side_effect=RuntimeError("private@example.com secret"),
        ), self.assertLogs(agent_server.logger, level="DEBUG") as logs:
            self.assertEqual(agent_server.discover_claude_native_models(), ([], "unavailable"))
            self.assertEqual(agent_server.RUNTIME_DIAGNOSTICS["claude"], ready)
        self.assertNotIn("private", str(logs.output))
        self.assertNotIn("secret", str(logs.output))


if __name__ == "__main__":
    unittest.main()

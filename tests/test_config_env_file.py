"""Config-file environment loading for provider processes.

install.sh writes ~/.config/hyadex-server/env and deliberately preserves
operator-added lines in it, and the Linux systemd unit loads it via
`EnvironmentFile`. launchd has no equivalent, so on macOS those lines were
written, preserved across upgrades, and then silently ignored - a company
provider's COMPANY_API_KEY never reached the CLI and the turn failed with
"Missing environment variable". These cover the server-side loader that
closes that platform gap.
"""

import os
import tempfile
import unittest
from pathlib import Path

import agent_server


class ParseConfigEnvFileTests(unittest.TestCase):
    def test_new_installer_environment_matches_actual_runtime_parser(self) -> None:
        from types import SimpleNamespace
        from hyadex.ops.execution.install import render_environment
        values = {'HYADEX_SERVER_NAME': "Harry's Mac", 'PROVIDER_KEY': 'quotes " and \\ and $HOME', 'UNICODE': '中文', 'EMPTY': ''}
        self.assertEqual(agent_server.parse_config_env_file(render_environment(SimpleNamespace(environment=values)).decode()), values)

    def test_parses_the_installer_written_shape(self) -> None:
        parsed = agent_server.parse_config_env_file(
            "HYADEX_AGENT_PORT=7870\n"
            "COMPANY_API_KEY=sk-abc123\n"
        )
        self.assertEqual(
            parsed,
            {"HYADEX_AGENT_PORT": "7870", "COMPANY_API_KEY": "sk-abc123"},
        )

    def test_ignores_comments_blanks_and_export_prefixes(self) -> None:
        parsed = agent_server.parse_config_env_file(
            "\n"
            "# a comment\n"
            "   \n"
            "export COMPANY_API_KEY=sk-abc123\n"
        )
        self.assertEqual(parsed, {"COMPANY_API_KEY": "sk-abc123"})

    def test_strips_matched_surrounding_quotes_only(self) -> None:
        parsed = agent_server.parse_config_env_file(
            'A="quoted"\n'
            "B='single'\n"
            'C="unbalanced\n'
            "D=plain=value=with=equals\n"
        )
        self.assertEqual(parsed["A"], "quoted")
        self.assertEqual(parsed["B"], "single")
        self.assertEqual(parsed["C"], '"unbalanced')
        self.assertEqual(parsed["D"], "plain=value=with=equals")

    def test_decodes_installer_json_quoted_server_names(self) -> None:
        parsed = agent_server.parse_config_env_file(
            'HYADEX_SERVER_NAME="Studio \\"M1\\""\n'
        )
        self.assertEqual(parsed["HYADEX_SERVER_NAME"], 'Studio "M1"')

    def test_malformed_lines_are_skipped_not_raised(self) -> None:
        # This runs during import; one stray hand-edited line must never
        # stop the server from starting.
        parsed = agent_server.parse_config_env_file(
            "no-equals-sign\n"
            "1INVALID_NAME=x\n"
            "has space=x\n"
            "GOOD=1\n"
        )
        self.assertEqual(parsed, {"GOOD": "1"})


class ServerDisplayNameTests(unittest.TestCase):
    def test_normalizes_configured_name_and_uses_fallback_when_empty(self) -> None:
        self.assertEqual(
            agent_server.canonical_server_display_name("  Zen's Studio  "),
            "Zen's Studio",
        )
        self.assertEqual(
            agent_server.canonical_server_display_name("   ", fallback="Sonic"),
            "Sonic",
        )

    def test_enforces_secure_peer_utf8_byte_limit_before_runtime_start(self) -> None:
        self.assertEqual(
            agent_server.canonical_server_display_name("界" * 53),
            "界" * 53,
        )
        for value in ("界" * 54, "S" * 161, "Sonic\nInjected"):
            with self.subTest(value=value), self.assertRaisesRegex(
                RuntimeError,
                "at most 160 UTF-8 bytes",
            ):
                agent_server.canonical_server_display_name(value)


class LoadConfigEnvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "env"
        self.touched: list[str] = []

    def _cleanup(self, *names: str) -> None:
        for name in names:
            self.addCleanup(os.environ.pop, name, None)

    def test_applies_variables_the_process_does_not_have(self) -> None:
        self._cleanup("COMPANY_API_KEY")
        os.environ.pop("COMPANY_API_KEY", None)
        self.path.write_text("COMPANY_API_KEY=sk-from-file\n", encoding="utf-8")

        applied = agent_server.load_config_env_file(self.path)

        self.assertEqual(applied, ["COMPANY_API_KEY"])
        self.assertEqual(os.environ["COMPANY_API_KEY"], "sk-from-file")

    def test_never_overrides_a_variable_the_service_manager_set(self) -> None:
        # The launchd plist / systemd unit injects the real token, port and
        # PATH. A stale copy in the file must not be able to replace them,
        # or a leftover value would break an otherwise healthy install.
        self._cleanup("HYADEX_AGENT_TOKEN")
        os.environ["HYADEX_AGENT_TOKEN"] = "live-token"
        self.path.write_text(
            "HYADEX_AGENT_TOKEN=stale-token\nCOMPANY_API_KEY=sk\n",
            encoding="utf-8",
        )
        self._cleanup("COMPANY_API_KEY")
        os.environ.pop("COMPANY_API_KEY", None)

        applied = agent_server.load_config_env_file(self.path)

        self.assertEqual(os.environ["HYADEX_AGENT_TOKEN"], "live-token")
        self.assertNotIn("HYADEX_AGENT_TOKEN", applied)
        self.assertEqual(applied, ["COMPANY_API_KEY"])

    def test_live_mutable_role_and_name_override_stale_launchd_values(self) -> None:
        self._cleanup("HYADEX_SERVER_NAME", "HYADEX_TEAM_HUB_MODE")
        os.environ["HYADEX_SERVER_NAME"] = "Old name"
        os.environ["HYADEX_TEAM_HUB_MODE"] = "disabled"
        self.path.write_text(
            'HYADEX_SERVER_NAME="Studio"\n'
            "HYADEX_TEAM_HUB_MODE=host\n",
            encoding="utf-8",
        )

        applied = agent_server.load_config_env_file(self.path)

        self.assertEqual(os.environ["HYADEX_SERVER_NAME"], "Studio")
        self.assertEqual(os.environ["HYADEX_TEAM_HUB_MODE"], "host")
        self.assertEqual(
            applied,
            ["HYADEX_SERVER_NAME", "HYADEX_TEAM_HUB_MODE"],
        )

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(
            agent_server.load_config_env_file(self.path / "nope" / "env"), []
        )

    def test_unreadable_file_is_not_an_error(self) -> None:
        # A directory where the file is expected must not crash startup.
        directory = Path(self.tempdir.name) / "as-a-directory"
        directory.mkdir()
        self.assertEqual(agent_server.load_config_env_file(directory), [])


class ProviderEnvironmentTests(unittest.TestCase):
    def test_loaded_variable_reaches_the_provider_environment(self) -> None:
        # The whole point: runner_env() is what provider CLIs inherit, so a
        # config-file variable has to be visible there.
        self.addCleanup(os.environ.pop, "COMPANY_API_KEY", None)
        os.environ["COMPANY_API_KEY"] = "sk-from-file"
        self.assertEqual(
            agent_server.runner_env().get("COMPANY_API_KEY"), "sk-from-file"
        )

    def test_hyadex_secrets_are_still_stripped(self) -> None:
        # Loading the file must not become a way to smuggle the server's own
        # bearer token into provider-controlled processes.
        self.addCleanup(os.environ.pop, "HYADEX_AGENT_TOKEN", None)
        os.environ["HYADEX_AGENT_TOKEN"] = "live-token"
        self.assertNotIn("HYADEX_AGENT_TOKEN", agent_server.runner_env())


if __name__ == "__main__":
    unittest.main()

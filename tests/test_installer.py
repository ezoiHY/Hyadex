import hashlib
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
import re
import tomllib
from pathlib import Path

from scripts import package_release


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
UNINSTALLER = ROOT / "uninstall.sh"
DEPLOYER = ROOT / "deploy.sh"
PACKAGER = ROOT / "scripts" / "package_release.py"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "server-ci.yml"


class InstallerContractTests(unittest.TestCase):


    def test_forced_source_build_has_an_exact_closed_backend_constraint_set(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        uv_configuration = project["tool"]["uv"]
        expected_constraints = [
            "hatchling==1.32.0",
            "packaging==26.3",
            "pathspec==1.1.1",
            "pluggy==1.6.0",
            "tomli==2.4.1; python_version < '3.11'",
            "tomlkit==0.15.1",
            "trove-classifiers==2026.6.1.19",
        ]

        self.assertEqual(
            uv_configuration["no-binary-package"],
            ["claude-agent-sdk"],
        )
        self.assertEqual(
            uv_configuration["build-constraint-dependencies"],
            expected_constraints,
        )

        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        locked_constraints = lock["manifest"]["build-constraints"]
        self.assertEqual(
            locked_constraints,
            [
                {
                    "name": requirement.split("==", 1)[0],
                    "specifier": f"=={requirement.split('==', 1)[1].split(';', 1)[0]}",
                    **(
                        {"marker": "python_full_version < '3.11'"}
                        if requirement.startswith("tomli==")
                        else {}
                    ),
                }
                for requirement in expected_constraints
            ],
        )

    def test_cryptography_lock_is_patched_and_matches_supported_mac_architecture(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(
            "cryptography>=50.0.1,<51",
            project["project"]["dependencies"],
        )
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        packages = [
            package
            for package in lock["package"]
            if package.get("name") == "cryptography"
        ]
        self.assertEqual(len(packages), 1)
        version = tuple(int(part) for part in packages[0]["version"].split("."))
        self.assertGreaterEqual(version, (50, 0, 1))
        macos_wheels = [
            wheel["url"]
            for wheel in packages[0].get("wheels", [])
            if "-macosx_" in wheel["url"]
        ]
        self.assertTrue(macos_wheels)
        self.assertTrue(all("_arm64.whl" in url for url in macos_wheels))
        self.assertTrue(
            any("cp39-abi3-macosx_11_0_arm64.whl" in url for url in macos_wheels)
        )

    def test_shell_syntax_is_valid(self):
        for script in (INSTALLER, UNINSTALLER):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


    def test_uninstall_removes_runtime_and_config_but_preserves_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )
            peer_identity = state_root / "secure-peers" / "client" / "identity.key"
            peer_identity.parent.mkdir(parents=True)
            peer_identity.write_text("preserve-secure-peer-identity\n")

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(install_root.exists())
            self.assertFalse(config_root.exists())
            self.assertFalse(service_file.exists())
            self.assertTrue((state_root / "sessions.json").is_file())
            self.assertEqual(peer_identity.read_text(), "preserve-secure-peer-identity\n")
            self.assertIn("Preserved chat history", result.stdout)
            self.assertIn("secure-peer credentials", result.stdout)
            self.assertNotIn("\033[", result.stdout)
            self.assertTrue(result.stdout.endswith("\n\nSuccessful!\n"))
            self.assertEqual(result.stdout.count("Successful!"), 1)

    def test_uninstall_purge_state_requires_interactive_exact_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes", "--purge-state"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to purge state without an interactive terminal", result.stderr)
            self.assertIn("--yes never bypasses", result.stderr)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )

    def test_internal_name_release_flag_cannot_target_default_or_standalone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, _, install_root, config_root, state_root, service_file, environment = self.fake_linux_uninstall_environment(root)
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)
            for args in (["--yes", "--managed-release-name"], ["--managed-instance", "default", "--yes", "--managed-release-name"]):
                with self.subTest(args=args):
                    result = subprocess.run(["/bin/bash", str(UNINSTALLER), *args], env=environment, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("only for a confirmed named-instance", result.stderr)
                    self.assertEqual(self.snapshot_trees(install_root, config_root, state_root, service_file), before)

    def test_uninstall_refuses_broad_and_overlapping_managed_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            home.mkdir()
            fake_bin.mkdir()
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            sentinel = home / "do-not-delete"
            sentinel.write_text("preserved\n")
            base_environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "HYADEX_STATE_DIR": str(root / "state"),
            }

            broad_result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env={**base_environment, "HYADEX_SERVER_INSTALL_DIR": str(home)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(broad_result.returncode, 2)
            self.assertIn("Refusing unsafe HYADEX_SERVER_INSTALL_DIR target", broad_result.stderr)
            self.assertEqual(sentinel.read_text(), "preserved\n")

            install_root = root / "managed"
            config_root = install_root / "config"
            install_root.mkdir()
            config_root.mkdir()
            overlap_sentinel = install_root / "do-not-delete"
            overlap_sentinel.write_text("preserved\n")
            overlap_result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env={
                    **base_environment,
                    "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                    "HYADEX_SERVER_CONFIG_DIR": str(config_root),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(overlap_result.returncode, 2)
            self.assertIn("Refusing overlapping install, configuration, and state roots", overlap_result.stderr)
            self.assertEqual(overlap_sentinel.read_text(), "preserved\n")

    def test_uninstall_refuses_active_install_lock_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root)
            )
            lock = install_root / ".install-lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n")
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install/update is active", result.stderr)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )

    def test_uninstall_stop_failure_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, config_root, state_root, service_file, environment = (
                self.fake_linux_uninstall_environment(root, stop_failure=True)
            )
            before = self.snapshot_trees(install_root, config_root, state_root, service_file)

            result = subprocess.run(
                ["/bin/bash", str(UNINSTALLER), "--yes"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not stop hyadex-server.service; no files were removed", result.stderr)
            self.assertNotIn("Successful!", result.stdout)
            self.assertEqual(
                self.snapshot_trees(install_root, config_root, state_root, service_file),
                before,
            )


    def fake_linux_uninstall_environment(self, root: Path, *, stop_failure: bool = False):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        config_root = root / "config"
        state_root = root / "state"
        home.mkdir()
        fake_bin.mkdir()
        install_root.mkdir()
        config_root.mkdir()
        state_root.mkdir()
        (install_root / "runtime-marker").write_text("preserved runtime\n")
        self.write_private_file(config_root / "env",
            "HYADEX_AGENT_TOKEN=preserved_token_abcdefghijklmnopqrstuvwxyz0123456789\n"
        )
        (state_root / "sessions.json").write_text('{"keep": true}\n')
        service_file = home / ".config" / "systemd" / "user" / "hyadex-server.service"
        service_file.parent.mkdir(parents=True)
        service_file.write_text("[Service]\nExecStart=/keep/running\n")
        self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
        if stop_failure:
            systemctl_source = """#!/bin/sh
case "${2:-}" in
  show-environment) exit 0 ;;
  disable) exit 1 ;;
  is-active) exit 0 ;;
esac
exit 0
"""
        else:
            systemctl_source = """#!/bin/sh
case "${2:-}" in
  show-environment) exit 0 ;;
  is-active) exit 1 ;;
esac
exit 0
"""
        self.write_executable(fake_bin / "systemctl", systemctl_source)
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HYADEX_SERVER_INSTALL_DIR": str(install_root),
            "HYADEX_SERVER_CONFIG_DIR": str(config_root),
            "HYADEX_STATE_DIR": str(state_root),
        }
        return home, fake_bin, install_root, config_root, state_root, service_file, environment


    @staticmethod
    def release_version() -> str:
        return (ROOT / "VERSION").read_text().strip()


    def prepare_preflight_path(self, fake_bin: Path, *, os_name: str, commands: tuple[str, ...]):
        self.write_executable(fake_bin / "uname", f"#!/bin/sh\necho {os_name}\n")
        for name in ("dirname", "sed", "tail", "tr"):
            resolved = shutil.which(name)
            if resolved is None:
                self.fail(f"test host is missing required utility {name}")
            (fake_bin / name).symlink_to(resolved)
        for name in commands:
            self.write_executable(fake_bin / name, "#!/bin/sh\nexit 0\n")

    @staticmethod
    def snapshot_trees(*roots: Path):
        snapshot = {}
        for root in roots:
            if not root.exists() and not root.is_symlink():
                snapshot[str(root)] = ("missing", None)
                continue
            for path in (root, *sorted(root.rglob("*"))):
                if path.is_symlink():
                    value = ("symlink", os.readlink(path))
                elif path.is_file():
                    value = ("file", path.read_bytes())
                else:
                    value = ("directory", None)
                snapshot[str(path)] = value
        return snapshot

    @staticmethod
    def write_private_file(path: Path, source: str) -> None:
        path.write_text(source)
        path.chmod(0o600)


    @staticmethod
    def write_executable(path: Path, source: str):
        shebang, separator, body = source.partition("\n")
        authoritative_systemctl_default = (
            "true" if path.name == "systemctl" and body.strip() == "exit 0" else "false"
        )
        if path.name == "uv" and shebang.startswith("#!"):
            source = (
                shebang
                + separator
                + "if [ \"${1:-}\" = \"--version\" ]; then "
                + "printf 'uv 0.10.10\\n'; exit 0; fi\n"
                + "fixture_uv_project=''\n"
                + "fixture_uv_want_project=false\n"
                + "for fixture_uv_argument in \"$@\"; do\n"
                + "  if [ \"$fixture_uv_want_project\" = true ]; then\n"
                + "    fixture_uv_project=\"$fixture_uv_argument\"\n"
                + "    fixture_uv_want_project=false\n"
                + "  elif [ \"$fixture_uv_argument\" = --project ]; then\n"
                + "    fixture_uv_want_project=true\n"
                + "  fi\n"
                + "done\n"
                + "fixture_uv_environment=${UV_PROJECT_ENVIRONMENT:-"
                + "${fixture_uv_project:+$fixture_uv_project/.venv}}\n"
                + body
                + "\nfixture_uv_status=$?\n"
                + "if [ \"$fixture_uv_status\" -eq 0 ] "
                + "&& [ -n \"$fixture_uv_environment\" ] "
                + "&& [ -x \"$fixture_uv_environment/bin/python\" ]; then\n"
                + "  mv \"$fixture_uv_environment/bin/python\" "
                + "\"$fixture_uv_environment/bin/python.fixture-original\"\n"
                + "  cat > \"$fixture_uv_environment/bin/python\" <<'PYTHON'\n"
                + "#!/bin/sh\n"
                + "original_python=\"${0%/*}/python.fixture-original\"\n"
                + "if [ \"${1:-}\" = - ] && [ \"$#\" -eq 8 ] "
                + "&& { [ \"${2:-}\" = Linux ] || [ \"${2:-}\" = Darwin ]; } "
                + "&& [ \"${6:-}\" != \"${6#/}\" ]; then\n"
                + "  fixture_health_host=$4\n"
                + "  case \"$fixture_health_host\" in *:*) "
                + "fixture_health_host=\"[$fixture_health_host]\" ;; esac\n"
                + "  if [ \"$7\" = secure-peer ]; then\n"
                + "    exec curl --output \"$8\" -H "
                + "'X-Hyadex-Token: fixture-token' "
                + "\"http://$fixture_health_host:$5$6\"\n"
                + "  fi\n"
                + "  exec curl --output \"$8\" "
                + "\"http://$fixture_health_host:$5$6\"\n"
                + "fi\n"
                + "exec \"$original_python\" \"$@\"\n"
                + "PYTHON\n"
                + "  chmod 755 \"$fixture_uv_environment/bin/python\"\n"
                + "fi\n"
                + "exit \"$fixture_uv_status\"\n"
            )
        elif path.name == "systemctl" and shebang.startswith("#!"):
            source = (
                shebang
                + separator
                + "if [ -n \"${FAKE_SERVER_IDENTITY:-}\" ] "
                + "&& [ -n \"${HYADEX_STATE_DIR:-}\" ] "
                + "&& [ ! -e \"$HYADEX_STATE_DIR/server-identity\" ]; then\n"
                + "  mkdir -p \"$HYADEX_STATE_DIR\"\n"
                + "  chmod 700 \"$HYADEX_STATE_DIR\"\n"
                + "  printf '%s\\n' \"$FAKE_SERVER_IDENTITY\" "
                + "> \"$HYADEX_STATE_DIR/server-identity\"\n"
                + "  chmod 600 \"$HYADEX_STATE_DIR/server-identity\"\n"
                + "fi\n"
                + f"if [ \"${{FAKE_AUTHORITATIVE_SYSTEMCTL:-{authoritative_systemctl_default}}}\" = true ]; then\n"
                + "  fixture_systemd_unit=''\n"
                + "  for fixture_systemd_argument in \"$@\"; do\n"
                + "    case \"$fixture_systemd_argument\" in *.service) "
                + "fixture_systemd_unit=$fixture_systemd_argument ;; esac\n"
                + "  done\n"
                + "  fixture_systemd_config=$HOME/.config/systemd/user/$fixture_systemd_unit\n"
                + "  fixture_systemd_last_action=''\n"
                + "  if [ -n \"${FAKE_EVENT_LOG:-}\" ] "
                + "&& [ -f \"$FAKE_EVENT_LOG\" ] "
                + "&& [ -n \"$fixture_systemd_unit\" ]; then\n"
                + "    fixture_systemd_last_action=$(grep -F "
                + "\" $fixture_systemd_unit\" \"$FAKE_EVENT_LOG\" "
                + "2>/dev/null | tail -n 1 || true)\n"
                + "  fi\n"
                + "  case \"$*\" in\n"
                + "    *' show '*--property=LoadState*--value*)\n"
                + "      if [ -f \"$fixture_systemd_config\" ]; then "
                + "printf 'loaded\\n'; else printf 'not-found\\n'; fi\n"
                + "      exit 0 ;;\n"
                + "    *' show '*--property=ActiveState*--value*)\n"
                + "      if [ ! -f \"$fixture_systemd_config\" ]; then "
                + "printf 'inactive\\n'; "
                + "elif case \"$fixture_systemd_last_action\" in "
                + "*' stop '*|*' disable --now '*|*' kill '*) true ;; "
                + "*) false ;; esac; then printf 'inactive\\n'; "
                + "else printf 'active\\n'; fi\n"
                + "      exit 0 ;;\n"
                + "    *' is-enabled '*)\n"
                + "      if case \"$fixture_systemd_last_action\" in "
                + "*' disable '*|*' disable --now '*) true ;; "
                + "*) false ;; esac; then printf 'disabled\\n'; exit 1; fi\n"
                + "      if [ -f \"$fixture_systemd_config\" ]; then "
                + "printf 'enabled\\n'; exit 0; fi\n"
                + "      printf 'not-found\\n'; exit 1 ;;\n"
                + "  esac\n"
                + "fi\n"
                + f"if [ {authoritative_systemctl_default} = true ] "
                + "&& [ -n \"${FAKE_EVENT_LOG:-}\" ]; then\n"
                + "  printf 'systemctl:%s\\n' \"$*\" >> \"$FAKE_EVENT_LOG\"\n"
                + "fi\n"
                + "case \"$*\" in\n"
                + "  *' show '*--property=MainPID*--value*)\n"
                + f"    if [ \"${{FAKE_AUTHORITATIVE_SYSTEMCTL:-{authoritative_systemctl_default}}}\" = true ] "
                + "&& [ ! -f \"$fixture_systemd_config\" ]; then "
                + "printf '0\\n'; else printf '%s\\n' \"${FAKE_SERVICE_MAIN_PID:-4242}\"; fi\n"
                + "    exit 0 ;;\n"
                + "esac\n"
                + body
            )
        elif path.name == "launchctl" and shebang.startswith("#!"):
            source = (
                shebang
                + separator
                + "if [ -n \"${FAKE_SERVER_IDENTITY:-}\" ] "
                + "&& [ -n \"${HYADEX_STATE_DIR:-}\" ] "
                + "&& [ ! -e \"$HYADEX_STATE_DIR/server-identity\" ]; then\n"
                + "  mkdir -p \"$HYADEX_STATE_DIR\"\n"
                + "  chmod 700 \"$HYADEX_STATE_DIR\"\n"
                + "  printf '%s\\n' \"$FAKE_SERVER_IDENTITY\" "
                + "> \"$HYADEX_STATE_DIR/server-identity\"\n"
                + "  chmod 600 \"$HYADEX_STATE_DIR/server-identity\"\n"
                + "fi\n"
                + "if [ \"${1:-}\" = print ] "
                + "&& [ -n \"${FAKE_LAUNCHCTL_STATE:-}\" ] "
                + "&& [ \"$(cat \"$FAKE_LAUNCHCTL_STATE\" 2>/dev/null)\" = loaded ]; then\n"
                + "  case \"${2:-}\" in gui/*/com.hyadex.server) "
                + "printf 'pid = %s;\\n' \"${FAKE_SERVICE_MAIN_PID:-4242}\"; exit 0 ;; esac\n"
                + "fi\n"
                + body
            )
        elif path.name == "python" and shebang.startswith("#!"):
            source = (
                shebang
                + separator
                + "if [ \"${1:-}\" = - ] && [ \"$#\" -eq 8 ] "
                + "&& { [ \"${2:-}\" = Linux ] || [ \"${2:-}\" = Darwin ]; } "
                + "&& [ \"${6:-}\" != \"${6#/}\" ]; then\n"
                + "  fixture_health_host=$4\n"
                + "  case \"$fixture_health_host\" in *:*) "
                + "fixture_health_host=\"[$fixture_health_host]\" ;; esac\n"
                + "  if [ \"$7\" = secure-peer ]; then\n"
                + "    exec curl --output \"$8\" -H "
                + "'X-Hyadex-Token: fixture-token' "
                + "\"http://$fixture_health_host:$5$6\"\n"
                + "  fi\n"
                + "  exec curl --output \"$8\" "
                + "\"http://$fixture_health_host:$5$6\"\n"
                + "fi\n"
                + body
            )
        path.write_text(source)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()

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
    def assert_smoke_check_imports(self, source, expected_modules):
        checks = []
        for command in re.findall(r"-c '([^'\n]+)'", source):
            checks.append({
                module.strip()
                for statement in re.findall(r"(?:^|;)\s*import\s+([\w., \t]+)", command)
                for module in statement.split(",")
            })
        self.assertTrue(
            any(set(expected_modules) <= modules for modules in checks),
            f"No runtime smoke check imports all required modules: {sorted(expected_modules)}",
        )

    def test_top_level_release_symlinks_are_rejected_by_packager_and_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            source.mkdir()
            for name in package_release.FILES:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "install.sh":
                    shutil.copy2(INSTALLER, path)
                elif name == "VERSION":
                    path.write_text(self.release_version() + "\n")
                else:
                    path.write_text(f"release file {name}\n")
            linked_target = root / "linked-runtime.py"
            linked_target.write_text("linked runtime\n")
            (source / "agent_server.py").unlink()
            (source / "agent_server.py").symlink_to(linked_target)

            with self.assertRaisesRegex(SystemExit, "linked release files"):
                package_release.validate_release_files(source)

            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "HYADEX_SERVER_INSTALL_DIR": str(root / "install"),
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "HYADEX_STATE_DIR": str(root / "state"),
            }
            (root / "home").mkdir()
            result = subprocess.run(
                ["/bin/bash", str(source / "install.sh"), "--execution-mode", "legacy"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("agent_server.py", result.stderr)
            self.assertIn("not a regular release file", result.stderr)
            self.assertFalse((root / "install").exists())

            (source / "agent_server.py").unlink()
            (source / "agent_server.py").write_text("release file\n")
            for name in ("npm/hyadex", "npm"):
                with self.subTest(linked_directory=name):
                    directory = source / name
                    external = root / "external-qr"
                    directory.rename(external)
                    directory.symlink_to(external, target_is_directory=True)
                    with self.assertRaisesRegex(SystemExit, "linked release files"):
                        package_release.validate_release_files(source)
                    result = subprocess.run(
                        ["/bin/bash", str(source / "install.sh"), "--execution-mode", "legacy"],
                        env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("not a real directory", result.stderr)
                    self.assertFalse((root / "install").exists())
                    directory.unlink()
                    external.rename(directory)


    def test_runtime_includes_and_verifies_websocket_support(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        dependencies = project["project"]["dependencies"]
        self.assertIn("claude-agent-sdk==0.2.130", dependencies)
        self.assertEqual(
            project["tool"]["uv"]["no-binary-package"],
            ["claude-agent-sdk"],
        )
        self.assertTrue(any(item.startswith("websockets") for item in dependencies))
        self.assertTrue(any(item.startswith("croniter") for item in dependencies))
        self.assertTrue(any(item.startswith("cryptography") for item in dependencies))
        self.assertTrue(any(item.startswith("python-dateutil") for item in dependencies))
        self.assertTrue(any(item.startswith("tzdata") for item in dependencies))
        self.assertIn("-c 'import websockets'", INSTALLER.read_text())
        self.assertIn("import claude_agent_sdk", INSTALLER.read_text())
        self.assertIn("import croniter, dateutil", INSTALLER.read_text())
        self.assert_smoke_check_imports(
            INSTALLER.read_text(),
            {"codex_auth", "codex_provider", "side_questions", "codex_side_question",
             "claude_side_question", "cursor_agent_client", "cursor_provider_mcp",
             "cursor_process_guard"},
        )
        self.assertIn('version("claude-agent-sdk")', INSTALLER.read_text())
        self.assertIn(
            'raise SystemExit(0 if sdk_version == "0.2.130"',
            INSTALLER.read_text(),
        )

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
        for script in (INSTALLER, UNINSTALLER, DEPLOYER):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_bounded_systemd_kill_uses_the_systemd_249_option_name(self):
        source = INSTALLER.read_text()
        self.assertIn("--kill-who=all", source)
        self.assertNotIn("--kill-whom", source)
        self.assertIn('"$SERVICE_NAME.service"', source)

    def test_managed_update_private_environment_is_consumed_and_cli_overrides_it(self):
        source = INSTALLER.read_text()
        captured = source.index(
            'MANAGED_UPDATE_ID="${HYADEX_MANAGED_UPDATE_ID:-}"'
        )
        scrubbed = source.index(
            "unset HYADEX_MANAGED_UPDATE_ID HYADEX_EXPECTED_SERVICE_CGROUP"
        )
        argument_parsing = source.index("while (($#)); do")
        self.assertLess(captured, scrubbed)
        self.assertLess(scrubbed, argument_parsing)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            install_root = root / "install"
            environment = {
                **os.environ,
                "HOME": str(home),
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "HYADEX_STATE_DIR": str(root / "state"),
                "HYADEX_MANAGED_UPDATE_ID": "invalid managed update id",
                "HYADEX_EXPECTED_SERVICE_CGROUP": "/valid/test.service",
            }
            rejected = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--show-token"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(rejected.returncode, 2)
            self.assertIn("Managed update ID is invalid", rejected.stderr)
            self.assertFalse(install_root.exists())

            environment["HYADEX_EXPECTED_SERVICE_CGROUP"] = "invalid cgroup"
            overridden = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--managed-update-id",
                    "update_test_cli_override",
                    "--expected-service-cgroup",
                    "/valid/test.service",
                    "--show-token",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(overridden.returncode, 1)
            self.assertIn("No HyadexServer access token found", overridden.stderr)
            self.assertNotIn("invalid", overridden.stderr.lower())
            self.assertFalse(install_root.exists())

    def test_linux_managed_update_rejects_installer_inside_service_cgroup(self):
        cgroup_file = Path("/proc/self/cgroup")
        if not cgroup_file.is_file():
            self.skipTest("Linux /proc cgroup fixture is unavailable")
        cgroup_paths = [
            line.split(":", 2)[2].strip()
            for line in cgroup_file.read_text().splitlines()
            if len(line.split(":", 2)) == 3 and line.split(":", 2)[2].strip()
        ]
        service_cgroup = next(
            (path for path in cgroup_paths if path != "/"),
            None,
        )
        if service_cgroup is None:
            self.skipTest("fixture process is in the root cgroup")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _home, fake_bin, install_root, environment = (
                self.fake_linux_preinstall_environment(root)
            )
            environment.update({
                "HYADEX_MANAGED_UPDATE_ID": "update_test_isolation",
                "HYADEX_EXPECTED_SERVICE_CGROUP": service_cgroup,
            })
            before = self.snapshot_trees(install_root)
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--non-interactive",
                    "--expected-server-identity",
                    "server_test_identity_12345678",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            after = self.snapshot_trees(install_root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("managed updater isolation", result.stderr)
        self.assertEqual(after, before)

    def test_linux_managed_update_force_stops_exact_unit_then_starts(self):
        if not Path("/proc/self/cgroup").is_file():
            self.skipTest("Linux /proc cgroup fixture is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, _install_root, environment = (
                self.fake_linux_preinstall_environment(root)
            )
            self.write_running_systemd_service(home)
            events = root / "systemctl.log"
            stopped = root / "stopped"
            count = root / "show-count"
            self.write_exact_health_uv(fake_bin)
            self.write_json_health_curl(fake_bin)
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "systemctl",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
  "--user show-environment") exit 0 ;;
  "--user show hyadex-server.service --property=LoadState --value") echo loaded; exit 0 ;;
  "--user is-enabled hyadex-server.service") echo enabled; exit 0 ;;
  "--user show zenithbot-agent.service --property=LoadState --value") echo not-found; exit 0 ;;
  "--user show zenithbot-agent.service --property=ActiveState --value") echo inactive; exit 0 ;;
  "--user is-enabled zenithbot-agent.service") echo not-found; exit 1 ;;
  "--user show hyadex-server.service --property=ActiveState --value")
    current="$(cat "$FAKE_SHOW_COUNT" 2>/dev/null || echo 0)"
    current=$((current + 1))
    printf '%s\n' "$current" > "$FAKE_SHOW_COUNT"
    if [ -e "$FAKE_STOPPED_SENTINEL" ]; then echo inactive
    elif [ "$current" -eq 1 ]; then echo active
    else echo deactivating
    fi
    exit 0
    ;;
  "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service")
    : > "$FAKE_STOPPED_SENTINEL"
    exit 0
    ;;
esac
exit 0
""",
            )
            server_identity = "server_test_identity_12345678"
            environment.update({
                "FAKE_AUTHORITATIVE_SYSTEMCTL": "false",
                "FAKE_SYSTEMCTL_LOG": str(events),
                "FAKE_STOPPED_SENTINEL": str(stopped),
                "FAKE_SHOW_COUNT": str(count),
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": server_identity,
                "FAKE_TEAM_HUB_ID": "",
                "REAL_PYTHON": sys.executable,
            })
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--port",
                    "47850",
                    "--non-interactive",
                    "--expected-server-identity",
                    server_identity,
                    "--managed-update-id",
                    "update_test_bounded_restart",
                    "--expected-service-cgroup",
                    "/hyadex-server-test.slice/hyadex-server.service",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            lines = events.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        kill = lines.index(
            "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service"
        )
        start = lines.index("--user start --no-block hyadex-server.service")
        self.assertLess(kill, start)
        self.assertFalse(any("pkill" in line or "killall" in line for line in lines))

    def test_linux_managed_update_reprobes_before_force_kill(self):
        if not Path("/proc/self/cgroup").is_file():
            self.skipTest("Linux /proc cgroup fixture is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, _install_root, environment = (
                self.fake_linux_preinstall_environment(root)
            )
            self.write_running_systemd_service(home)
            events = root / "systemctl.log"
            count = root / "show-count"
            self.write_exact_health_uv(fake_bin)
            self.write_json_health_curl(fake_bin)
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "systemctl",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
case "$*" in
  "--user show-environment") exit 0 ;;
  "--user show hyadex-server.service --property=LoadState --value") echo loaded; exit 0 ;;
  "--user is-enabled hyadex-server.service") echo enabled; exit 0 ;;
  "--user show zenithbot-agent.service --property=LoadState --value") echo not-found; exit 0 ;;
  "--user show zenithbot-agent.service --property=ActiveState --value") echo inactive; exit 0 ;;
  "--user is-enabled zenithbot-agent.service") echo not-found; exit 1 ;;
  "--user show hyadex-server.service --property=ActiveState --value")
    current="$(cat "$FAKE_SHOW_COUNT" 2>/dev/null || echo 0)"
    current=$((current + 1))
    printf '%s\n' "$current" > "$FAKE_SHOW_COUNT"
    if [ "$current" -eq 1 ]; then echo active
    elif [ "$current" -gt 50 ]; then echo inactive
    else echo deactivating
    fi
    exit 0
    ;;
  "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service")
    exit 99
    ;;
esac
exit 0
""",
            )
            server_identity = "server_test_identity_12345678"
            environment.update({
                "FAKE_AUTHORITATIVE_SYSTEMCTL": "false",
                "FAKE_SYSTEMCTL_LOG": str(events),
                "FAKE_SHOW_COUNT": str(count),
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": server_identity,
                "FAKE_TEAM_HUB_ID": "",
                "REAL_PYTHON": sys.executable,
            })
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--port",
                    "47850",
                    "--non-interactive",
                    "--expected-server-identity",
                    server_identity,
                    "--managed-update-id",
                    "update_test_stop_race",
                    "--expected-service-cgroup",
                    "/hyadex-server-test.slice/hyadex-server.service",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            lines = events.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service",
            lines,
        )
        self.assertIn("--user start --no-block hyadex-server.service", lines)

    def test_bounded_systemd_restart_shell_fixture_survives_stubborn_descendant(self):
        source = INSTALLER.read_text()
        start = source.index("systemd_service_active_state() {")
        end = source.index("\nrestart_service() {", start)
        functions = source[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            events = root / "events"
            stopped = root / "stopped"
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "systemctl",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_EVENT_LOG"
case "$*" in
  "--user show hyadex-server.service --property=ActiveState --value")
    if [ -e "$FAKE_STOPPED_SENTINEL" ]; then echo inactive; else echo deactivating; fi ;;
  "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service")
    : > "$FAKE_STOPPED_SENTINEL" ;;
esac
exit 0
""",
            )
            harness = root / "harness.sh"
            harness.write_text(
                "set -euo pipefail\n"
                "SERVICE_NAME=hyadex-server\n"
                "SYSTEMD_MANAGED_STOP_ATTEMPTS=2\n"
                "SYSTEMD_MANAGED_STOP_DELAY=0\n"
                "SYSTEMD_MANAGED_KILL_ATTEMPTS=2\n"
                + functions
                + "\nrestart_managed_systemd_service_bounded\n"
            )
            result = subprocess.run(
                ["/bin/bash", str(harness)],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "FAKE_EVENT_LOG": str(events),
                    "FAKE_STOPPED_SENTINEL": str(stopped),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            stubborn_lines = events.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service",
            stubborn_lines,
        )
        self.assertEqual(stubborn_lines[-1], "--user start --no-block hyadex-server.service")

    def test_bounded_systemd_restart_shell_fixture_closes_poll_kill_race(self):
        source = INSTALLER.read_text()
        start = source.index("systemd_service_active_state() {")
        end = source.index("\nrestart_service() {", start)
        functions = source[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            events = root / "events"
            count = root / "count"
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                fake_bin / "systemctl",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_EVENT_LOG"
case "$*" in
  "--user show hyadex-server.service --property=ActiveState --value")
    current="$(cat "$FAKE_SHOW_COUNT" 2>/dev/null || echo 0)"
    current=$((current + 1))
    printf '%s\n' "$current" > "$FAKE_SHOW_COUNT"
    if [ "$current" -gt 2 ]; then echo inactive; else echo deactivating; fi ;;
  "--user kill --kill-who=all --signal=SIGKILL hyadex-server.service") exit 99 ;;
esac
exit 0
""",
            )
            harness = root / "harness.sh"
            harness.write_text(
                "set -euo pipefail\n"
                "SERVICE_NAME=hyadex-server\n"
                "SYSTEMD_MANAGED_STOP_ATTEMPTS=2\n"
                "SYSTEMD_MANAGED_STOP_DELAY=0\n"
                "SYSTEMD_MANAGED_KILL_ATTEMPTS=2\n"
                + functions
                + "\nrestart_managed_systemd_service_bounded\n"
            )
            result = subprocess.run(
                ["/bin/bash", str(harness)],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "FAKE_EVENT_LOG": str(events),
                    "FAKE_SHOW_COUNT": str(count),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            race_lines = events.read_text().splitlines()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(" kill " in f" {line} " for line in race_lines))
        self.assertEqual(race_lines[-1], "--user start --no-block hyadex-server.service")

    def test_real_systemd_new_and_first_upgrade_restarts_survive_stubborn_descendant(self):
        if sys.platform != "linux":
            self.skipTest("real systemd acceptance requires Linux")
        if os.environ.get("HYADEX_RUN_SYSTEMD_ACCEPTANCE") != "1":
            self.skipTest("set HYADEX_RUN_SYSTEMD_ACCEPTANCE=1 to run")
        if shutil.which("systemctl") is None:
            self.skipTest("systemctl is unavailable")
        probe = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest("systemd user manager is unavailable")

        source = INSTALLER.read_text()
        start = source.index("systemd_service_active_state() {")
        end = source.index("\nrestart_service() {", start)
        functions = source[start:end]
        unit_base = (
            f"hyadex-server-update-acceptance-{os.getpid()}-{time.monotonic_ns()}"
        )
        self.assertRegex(unit_base, r"^hyadex-server-update-acceptance-[0-9]+-[0-9]+$")
        unit_name = f"{unit_base}.service"

        def systemctl(*arguments: str, timeout: float = 10) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["systemctl", "--user", *arguments],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        def tasks_current() -> int:
            result = systemctl(
                "show", unit_name, "--property=TasksCurrent", "--value"
            )
            try:
                return int(result.stdout.strip()) if result.returncode == 0 else 0
            except ValueError:
                return 0

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stubborn = root / "stubborn.py"
            stubborn.write_text(
                "import os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "if os.fork() == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "while True:\n"
                "    time.sleep(1)\n"
            )
            unit_file = root / unit_name
            unit_file.write_text(
                "[Unit]\nDescription=HyadexServer bounded restart acceptance fixture\n"
                "[Service]\nType=simple\n"
                f"ExecStart={sys.executable} {stubborn}\n"
                "KillMode=control-group\nTimeoutStopSec=90s\n"
            )
            harness = root / "harness.sh"
            harness.write_text(
                "set -euo pipefail\n"
                f"SERVICE_NAME={unit_base}\n"
                "SYSTEMD_MANAGED_STOP_ATTEMPTS=50\n"
                "SYSTEMD_MANAGED_STOP_DELAY=0.1\n"
                "SYSTEMD_MANAGED_KILL_ATTEMPTS=20\n"
                + functions
                + "\nrestart_managed_systemd_service_bounded\n"
                + "printf 'updater-survived\\n'\n"
            )

            linked = systemctl("--runtime", "link", str(unit_file))
            if linked.returncode != 0:
                self.skipTest(f"could not link transient user unit: {linked.stderr.strip()}")
            try:
                self.assertEqual(systemctl("daemon-reload").returncode, 0)
                started = systemctl("start", unit_name)
                self.assertEqual(started.returncode, 0, started.stderr)
                deadline = time.monotonic() + 10
                while tasks_current() < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(tasks_current(), 2)

                before = time.monotonic()
                result = subprocess.run(
                    ["/bin/bash", str(harness)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                elapsed = time.monotonic() - before

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("updater-survived", result.stdout)
                self.assertLess(elapsed, 20)
                deadline = time.monotonic() + 10
                while tasks_current() < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(tasks_current(), 2)

                # Adoption from 0.1.25 runs its legacy synchronous restart
                # after this installer has written/reloaded the new unit. The
                # 10-second unit timeout is therefore the first-upgrade bound,
                # even though the old runner cannot select the new helper.
                self.assertIn("TimeoutStopSec=10s", source)
                unit_file.write_text(
                    unit_file.read_text().replace(
                        "TimeoutStopSec=90s",
                        "TimeoutStopSec=10s",
                    )
                )
                self.assertEqual(systemctl("daemon-reload").returncode, 0)
                before = time.monotonic()
                legacy_restart = systemctl("restart", unit_name, timeout=20)
                legacy_elapsed = time.monotonic() - before
                self.assertEqual(
                    legacy_restart.returncode,
                    0,
                    legacy_restart.stderr,
                )
                self.assertLess(legacy_elapsed, 20)
                deadline = time.monotonic() + 10
                while tasks_current() < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(tasks_current(), 2)
            finally:
                systemctl("stop", "--no-block", unit_name)
                systemctl(
                    "kill",
                    "--kill-who=all",
                    "--signal=SIGKILL",
                    unit_name,
                )
                systemctl("disable", "--runtime", unit_name)
                systemctl("reset-failed", unit_name)
                systemctl("daemon-reload")

    def test_direct_deploy_includes_scheduler_runtime(self):
        source = DEPLOYER.read_text()
        self.assertIn('"$SCRIPT_DIR/hyadex_jobs.py"', source)
        self.assertIn('"$SCRIPT_DIR/hyadex_chats.py"', source)
        self.assertIn('"$SCRIPT_DIR/hyadex_emergency.py"', source)
        self.assertIn('"$SCRIPT_DIR/hyadex_publish.py"', source)
        self.assertIn('"$SCRIPT_DIR/provider_commands.py"', source)
        self.assertIn('"$SCRIPT_DIR/provider_usage.py"', source)
        self.assertIn('"$SCRIPT_DIR/hyadex/providers/claude.py"', source)
        self.assertIn('"$SCRIPT_DIR/hyadex/providers/codex.py"', source)
        self.assertIn('"$SCRIPT_DIR/cursor_agent_client.py"', source)
        self.assertIn('"$SCRIPT_DIR/cursor_provider_mcp.py"', source)
        self.assert_smoke_check_imports(
            source, {"claude_agent_sdk", "croniter", "cryptography", "dateutil", "tzdata"},
        )
        self.assertIn(r'version(\"claude-agent-sdk\")', source)
        self.assertIn(r'raise SystemExit(0 if sdk_version == \"0.2.130\"', source)
        self.assertIn("'claude-agent-sdk==0.2.130'", source)
        self.assertIn("--no-binary claude-agent-sdk", source)
        self.assertIn("python-dateutil>=2.9,<3", source)
        self.assertIn("'$REMOTE_SERVER_DIR/hyadex/providers/claude.py'", source)
        self.assertIn("'$REMOTE_SERVER_DIR/hyadex/providers/codex.py'", source)
        self.assertIn("'$REMOTE_SERVER_DIR/cursor_agent_client.py'", source)
        self.assertIn("'$REMOTE_SERVER_DIR/provider_commands.py'", source)
        self.assertIn("'$REMOTE_SERVER_DIR/provider_usage.py'", source)
        self.assert_smoke_check_imports(
            source,
            {"codex_auth", "codex_provider", "side_questions", "codex_side_question",
             "claude_side_question", "claude_agent_sdk",
             "cursor_agent_client", "cursor_process_guard",
             "provider_commands"},
        )
        self.assertNotIn("EXPECTED_CLIENT_BINDING", source)





    def run_direct_deploy_unpaired(
        self,
        *,
        status_payload,
        secure_state_available=True,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            ssh_log = root / "ssh.log"
            copy_log = root / "copy.log"
            health_count = root / "health-count"
            self.write_executable(
                fake_bin / "ssh",
                """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

command = " ".join(sys.argv[1:])
with open(os.environ["FAKE_SSH_LOG"], "a", encoding="utf-8") as stream:
    stream.write(command + "\\n")
if "team_hub_host.py" in command:
    print("present", end="")
    raise SystemExit(0)
if "/api/admin/secure-peers/v1/status" in command:
    expected_header = (
        "X-Hyadex-Token: "
        "deploy_token_abcdefghijklmnopqrstuvwxyz0123456789"
    )
    if expected_header not in command or "Authorization:" in command:
        raise SystemExit(91)
    print(os.environ["FAKE_STATUS_PAYLOAD"])
    raise SystemExit(0)
if "/api/health" in command:
    counter = pathlib.Path(os.environ["FAKE_HEALTH_COUNT"])
    if counter.exists():
        version = pathlib.Path(os.environ["FAKE_RELEASE_VERSION"]).read_text().strip()
    else:
        counter.touch()
        version = "0.1.25-beta.1"
    secure_state_available = os.environ["FAKE_SECURE_STATE_AVAILABLE"] == "true"
    print(json.dumps({
        "ok": True,
        "server_version": version,
        "server_identity": "server_deploy_unpaired_12345678",
        "capabilities": {
            "team_hub_v1": {
                "available": False,
                "designated_host": False,
                "version": 1,
                "base_path": None,
                "hub_id": None,
                "host_server_identity": None,
                "transport": None,
                "hub_url": None,
                "routes": [],
            },
            "secure_peer_v1": {
                "available": True,
                "state_available": secure_state_available,
                "state_error_code": (
                    None
                    if secure_state_available
                    else "secure_peer_state_unavailable"
                ),
                "required": False,
                "version": 1,
                "control_path": "/api/admin/secure-peers/v1/status",
                "proxy_prefix": "/api/team-hub-secure",
            },
        },
    }, separators=(",", ":")))
raise SystemExit(0)
""",
            )
            for name in ("scp", "rsync"):
                self.write_executable(
                    fake_bin / name,
                    "#!/bin/sh\nprintf '%s:%s\\n' \"$0\" \"$*\" >> \"$FAKE_COPY_LOG\"\n",
                )
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "HYADEX_REMOTE_HOST": "fake-host",
                "HYADEX_AGENT_TOKEN": "deploy_token_abcdefghijklmnopqrstuvwxyz0123456789",
                "HYADEX_HEALTH_ATTEMPTS": "1",
                "FAKE_SSH_LOG": str(ssh_log),
                "FAKE_COPY_LOG": str(copy_log),
                "FAKE_HEALTH_COUNT": str(health_count),
                "FAKE_RELEASE_VERSION": str(ROOT / "VERSION"),
                "FAKE_STATUS_PAYLOAD": status_payload,
                "FAKE_SECURE_STATE_AVAILABLE": (
                    "true" if secure_state_available else "false"
                ),
            }

            result = subprocess.run(
                ["/bin/bash", str(DEPLOYER)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            return (
                result,
                copy_log.is_file(),
                ssh_log.read_text().splitlines(),
            )

    def test_direct_deploy_accepts_authenticated_unpaired_disabled_target(self):
        status_payload = json.dumps(
            {
                "version": 2,
                "server_identity": "server_deploy_unpaired_12345678",
                "active_connection_id": None,
                "pairings": [],
            },
            separators=(",", ":"),
        )
        result, copied, ssh_lines = self.run_direct_deploy_unpaired(
            status_payload=status_payload,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(copied)
        self.assertTrue(any("systemctl --user restart" in line for line in ssh_lines))







    def test_direct_deploy_rejects_post_restart_server_identity_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            ssh_log = root / "ssh.log"
            copy_log = root / "copy.log"
            health_count = root / "health-count"
            self.write_executable(
                fake_bin / "ssh",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SSH_LOG"
case "$*" in
  *"team_hub_host.py"*) printf 'present'; exit 0 ;;
  *"/api/admin/secure-peers/v1/status"*)
    case "$*" in
      *"X-Hyadex-Token: deploy_token_abcdefghijklmnopqrstuvwxyz0123456789"*) ;;
      *) exit 91 ;;
    esac
    case "$*" in *"Authorization:"*) exit 92 ;; esac
    printf '%s\n' '{"version":2,"server_identity":"server_deploy_before_12345678","active_connection_id":null,"pairings":[]}'
    exit 0
    ;;
  *"/api/health"*)
    if [ ! -f "$FAKE_HEALTH_COUNT" ]; then
      : > "$FAKE_HEALTH_COUNT"
      identity="server_deploy_before_12345678"
      version="0.1.25-beta.1"
    else
      identity="server_deploy_after_wrong_12345678"
      version="$(cat "$FAKE_RELEASE_VERSION")"
    fi
    printf '{"ok":true,"server_version":"%s","server_identity":"%s","capabilities":{"team_hub_v1":{"available":false,"designated_host":false,"version":1,"base_path":null,"hub_id":null,"host_server_identity":null,"transport":null,"hub_url":null,"routes":[]},"secure_peer_v1":{"available":true,"state_available":true,"state_error_code":null,"required":false,"version":1,"control_path":"/api/admin/secure-peers/v1/status","proxy_prefix":"/api/team-hub-secure"}}}\n' "$version" "$identity"
    exit 0
    ;;
esac
exit 0
""",
            )
            for name in ("scp", "rsync"):
                self.write_executable(
                    fake_bin / name,
                    "#!/bin/sh\nprintf '%s:%s\\n' \"$0\" \"$*\" >> \"$FAKE_COPY_LOG\"\n",
                )
            self.write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "HYADEX_REMOTE_HOST": "fake-host",
                "HYADEX_AGENT_TOKEN": "deploy_token_abcdefghijklmnopqrstuvwxyz0123456789",
                "HYADEX_HEALTH_ATTEMPTS": "1",
                "FAKE_SSH_LOG": str(ssh_log),
                "FAKE_COPY_LOG": str(copy_log),
                "FAKE_HEALTH_COUNT": str(health_count),
                "FAKE_RELEASE_VERSION": str(ROOT / "VERSION"),
            }

            result = subprocess.run(
                ["/bin/bash", str(DEPLOYER)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not prove exact version", result.stderr)
            self.assertTrue(copy_log.is_file())
            self.assertTrue(
                any("systemctl --user restart" in line for line in ssh_log.read_text().splitlines())
            )


    def test_installer_and_release_archive_include_runtime_tools_and_uninstaller(self):
        source = INSTALLER.read_text()
        required = re.search(r"(?m)^RELEASE_FILES=\(([^)]*)\)$", source).group(1).split()
        self.assertEqual(set(required), set(package_release.FILES))
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, str(PACKAGER), "--output", temporary],
                                    cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            version = (ROOT / "VERSION").read_text().strip()
            prefix = f"hyadex-server-{version}/"
            with tarfile.open(Path(temporary) / f"hyadex-server-{version}.tar.gz") as archive:
                files = {member.name[len(prefix):] for member in archive.getmembers() if member.isfile()}
            self.assertEqual(files, set(package_release.FILES))
            self.assertTrue({"agent_server.py", "install.sh", "uninstall.sh", "LICENSE", "NOTICE"} <= files)

    def test_installer_preserves_state_and_emits_private_result(self):
        source = INSTALLER.read_text()
        self.assertIn('STATE_ROOT="$HOME/.hyadex"', source)
        self.assertIn('HYADEX_STATE_DIR=$STATE_ROOT', source)
        self.assertIn('LEGACY_STATE_ROOT="$HOME/.zenithbot-agent"', source)
        self.assertIn('mv "$LEGACY_STATE_ROOT" "$STATE_ROOT"', source)
        self.assertIn("HYADEX_SETUP_RESULT=", source)
        self.assertIn("HYADEX_AGENT_TOKEN", source)
        self.assertNotIn("HYADEX_SERVER_ADMIN_TOKEN", source)
        self.assertIn('RELEASES_ROOT="$INSTALL_ROOT/releases"', source)
        self.assertIn('PREVIOUS_LINK="$INSTALL_ROOT/previous"', source)
        self.assertIn('REPLACED_DIR="$RELEASES_ROOT/$RELEASE_VERSION-replaced-', source)
        self.assertIn("ManagedOOMPreference=avoid", source)
        self.assertLess(
            source.index('OLD_TARGET=""'),
            source.index('activate_transaction_files "$STAGE_DIR"'),
        )
        self.assertIn("activation_transaction begin", source)
        self.assertIn("rolling back", source)
        self.assertIn("--team-hub-host", source)
        self.assertIn("--team-hub-tailscale-serve-url", source)
        self.assertIn("--team-hub-direct-ip-url", source)
        self.assertIn("--reactivate-team-hub-host", source)
        self.assertIn("--no-team-hub-host", source)
        self.assertIn("HYADEX_TEAM_HUB_MODE=$TEAM_HUB_MODE", source)
        self.assertIn("HYADEX_TEAM_HUB_TRANSPORT=$TEAM_HUB_TRANSPORT", source)
        self.assertIn("HYADEX_TEAM_HUB_URL=$TEAM_HUB_URL", source)
        self.assertIn("<key>HYADEX_TEAM_HUB_MODE</key>", source)
        self.assertIn("<key>HYADEX_TEAM_HUB_TRANSPORT</key>", source)
        self.assertIn("<key>HYADEX_TEAM_HUB_URL</key>", source)
        self.assertNotIn("HYADEX_AGENT_TLS_CERTFILE", source)
        self.assertNotIn("HYADEX_AGENT_TLS_KEYFILE", source)
        self.assertNotIn('echo "  ${COLOR_BOLD}Access token', source)
        self.assertIsNone(re.search(r"(?m)^\s*sudo\b", source))

    def test_server_name_rejects_controls_and_overlong_values(self):
        for label, invalid in (
            ("control", "Sonic\nInjected"),
            ("overlong", "S" * 161),
            ("overlong-multibyte", "界" * 60),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                _home, fake_bin, install_root, environment = (
                    self.fake_linux_preinstall_environment(root)
                )
                self.write_fake_uv(fake_bin)
                environment["HYADEX_SERVER_NAME"] = invalid

                result = subprocess.run(
                    ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(
                    "empty or at most 160 UTF-8 bytes and contain no control characters",
                    result.stderr,
                )
                self.assertTrue(
                    (install_root / "current" / "runtime-marker").is_file()
                )

    def test_help_does_not_modify_the_machine(self):
        result = subprocess.run(
            ["bash", str(INSTALLER), "--execution-mode", "legacy", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installs or updates HyadexServer", result.stdout)










    def test_show_token_reads_current_token_without_mutating_configured_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, install_root, config_root, state_root, environment = (
                self.show_token_environment(root)
            )
            token = "current_hyadex_token_abcdefghijklmnopqrstuvwxyz"
            self.write_private_file(config_root / "env",
                f"HYADEX_AGENT_TOKEN={token}\n"
                "PRESERVED_SETTING=unchanged\n"
            )
            (install_root / "release-marker").write_text("keep runtime\n")
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            before = self.snapshot_trees(home, install_root, config_root, state_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--show-token"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"{token}\n")
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                self.snapshot_trees(home, install_root, config_root, state_root),
                before,
            )

    def test_show_token_reads_legacy_token_names_and_sources(self):
        scenarios = (
            ("canonical_env_zenithdock_name", "ZENITHDOCK_AGENT_TOKEN", "config"),
            ("canonical_env_zenithbot_name", "ZENITHBOT_AGENT_TOKEN", "config"),
            ("legacy_zenithbot_env", "ZENITHDOCK_AGENT_TOKEN", "legacy_env"),
            ("legacy_systemd_service", "ZENITHDOCK_AGENT_TOKEN", "legacy_service"),
        )
        for scenario, variable_name, source in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                home, _install_root, config_root, _state_root, environment = (
                    self.show_token_environment(root)
                )
                token = f"{scenario}_token_abcdefghijklmnopqrstuvwxyz"
                if source == "config":
                    self.write_private_file(config_root / "env", f"{variable_name}={token}\n")
                elif source == "legacy_env":
                    legacy_env = home / "Zenithbot" / ".env"
                    legacy_env.parent.mkdir(parents=True)
                    legacy_env.write_text(f"{variable_name}={token}\n")
                else:
                    legacy_service = (
                        home
                        / ".config"
                        / "systemd"
                        / "user"
                        / "zenithbot-agent.service"
                    )
                    legacy_service.parent.mkdir(parents=True)
                    legacy_service.write_text(
                        "[Service]\n"
                        f'Environment="{variable_name}={token}"\n'
                    )

                result = subprocess.run(
                    ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--show-token"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, f"{token}\n")
                self.assertEqual(result.stderr, "")

    def test_show_token_fails_without_a_token_and_does_not_mutate_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, install_root, config_root, state_root, environment = (
                self.show_token_environment(root)
            )
            (install_root / "release-marker").write_text("keep runtime\n")
            (config_root / "settings").write_text("keep config\n")
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            before = self.snapshot_trees(home, install_root, config_root, state_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--show-token"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIn("No HyadexServer access token found", result.stderr)
            self.assertIn(str(config_root / "env"), result.stderr)
            self.assertEqual(
                self.snapshot_trees(home, install_root, config_root, state_root),
                before,
            )

    def test_show_token_rejects_unsafe_or_unbounded_configuration_sources(self):
        for source_type in ("symlink", "hardlink", "fifo", "oversized"):
            with self.subTest(source_type=source_type), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                _home, _install_root, config_root, _state_root, environment = (
                    self.show_token_environment(root)
                )
                env_path = config_root / "env"
                victim = root / "victim"
                self.write_private_file(
                    victim,
                    "HYADEX_AGENT_TOKEN=must_not_be_disclosed_abcdefghijklmnopqrstuvwxyz\n",
                )
                if source_type == "symlink":
                    env_path.symlink_to(victim)
                elif source_type == "hardlink":
                    os.link(victim, env_path)
                elif source_type == "fifo":
                    os.mkfifo(env_path, 0o600)
                else:
                    with env_path.open("wb") as output:
                        output.truncate(16 * 1024 * 1024 + 1)
                    os.chmod(env_path, 0o600)

                result = subprocess.run(
                    ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--show-token"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("must_not_be_disclosed", result.stdout)

    def test_same_version_reinstall_keeps_the_replaced_runtime_for_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            fake_bin.mkdir()
            home.mkdir()
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_json_health_curl(fake_bin)
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "HYADEX_STATE_DIR": str(root / "state"),
                "REAL_PYTHON": sys.executable,
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": "e5" * 12,
                "FAKE_TEAM_HUB_MODE": "disabled",
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            }

            # These lifecycle fixtures advertise disabled Hub health; inherited
            # host settings must not silently enable a real candidate Hub.
            for key in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
                "HYADEX_TEAM_HUB_DIRECT_IP_URL",
            ):
                environment.pop(key, None)
            environment["FAKE_TEAM_HUB_MODE"] = "disabled"

            for attempt in range(2):
                result = subprocess.run(
                    ["bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if attempt == 0:
                    (install_root / "current" / "old-runtime-marker").write_text("old\n")

            previous = install_root / "previous"
            current = install_root / "current"
            self.assertTrue(previous.is_symlink())
            self.assertTrue((previous / "old-runtime-marker").is_file())
            self.assertFalse((current / "old-runtime-marker").exists())
            self.assertNotEqual(previous.resolve(), current.resolve())

    def test_dependency_sync_ignores_an_inherited_external_uv_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            external_environment = root / "unrelated-project-venv"
            sync_target_log = root / "uv-sync-target"
            fake_bin.mkdir()
            home.mkdir()
            external_environment.mkdir()
            sentinel = external_environment / "do-not-touch"
            sentinel.write_text("unrelated workspace\n")
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_json_health_curl(fake_bin)
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
if env | grep -Eq '^(HYADEX_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|HYADEX_PUBLISH_TOKEN|HYADEX_PROVIDER_AUTHORITY_FILE)='; then
  exit 90
fi
target="${UV_PROJECT_ENVIRONMENT:-$project/.venv}"
printf '%s\n' "$target" > "${0%/*}/../uv-sync-target"
mkdir -p "$target/bin"
cat > "$target/bin/python" <<'PYTHON'
#!/bin/sh
if env | grep -Eq '^(HYADEX_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|HYADEX_PUBLISH_TOKEN|HYADEX_PROVIDER_AUTHORITY_FILE)='; then
  exit 91
fi
if [ "${1:-}" = "-" ]; then
  exec "$REAL_PYTHON" "$@"
fi
case "$*" in
  *"-m activation_transaction "*|*"/activation_transaction.py "*) exec "$REAL_PYTHON" "$@" ;;
esac
exit 0
PYTHON
chmod 755 "$target/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "HYADEX_STATE_DIR": str(root / "state"),
                "FAKE_UV_SYNC_TARGET_LOG": str(sync_target_log),
                "UV_PROJECT_ENVIRONMENT": str(external_environment),
                "VIRTUAL_ENV": str(external_environment),
                "HYADEX_AGENT_TOKEN": "secret_agent_token_abcdefghijklmnopqrstuvwxyz",
                "ZENITHDOCK_AGENT_TOKEN": "secret_zenith_token_abcdefghijklmnopqrstuvwxyz",
                "ZENITHBOT_AGENT_TOKEN": "secret_legacy_token_abcdefghijklmnopqrstuvwxyz",
                "HYADEX_PUBLISH_TOKEN": "secret_publish_token_abcdefghijklmnopqrstuvwxyz",
                "HYADEX_PROVIDER_AUTHORITY_FILE": "/tmp/private-authority-file.json",
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": "server_dependency_sync_12345678",
                "REAL_PYTHON": sys.executable,
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            }
            for name in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
            ):
                environment.pop(name, None)

            result = subprocess.run(
                ["bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            self.assertEqual(sentinel.read_text(), "unrelated workspace\n")
            self.assertEqual(list(external_environment.iterdir()), [sentinel])
            sync_target = Path(sync_target_log.read_text().strip())
            self.assertEqual(sync_target.name, ".venv")
            self.assertEqual(sync_target.parent.parent, install_root / "releases")
            self.assertTrue((install_root / "current" / ".venv" / "bin" / "python").is_file())

    def test_missing_uv_is_not_bootstrapped_by_a_downloaded_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            config_root = root / "config"
            state_root = root / "state"
            bootstrap_clean = root / "uv-bootstrap-clean"
            uv_installer_source = root / "downloaded-uv-installer.sh"
            uv_binary_source = root / "fake-uv"
            fake_bin.mkdir()
            home.mkdir()
            self.write_unavailable_uv(fake_bin)
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(
                uv_binary_source,
                """#!/bin/sh
if env | grep -Eq '^(HYADEX_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|HYADEX_PUBLISH_TOKEN|HYADEX_PROVIDER_AUTHORITY_FILE)='; then
  exit 93
fi
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
cat > "$project/.venv/bin/python" <<'PYTHON'
#!/bin/sh
if env | grep -Eq '^(HYADEX_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|HYADEX_PUBLISH_TOKEN|HYADEX_PROVIDER_AUTHORITY_FILE)='; then
  exit 94
fi
if [ "${1:-}" = "-" ]; then exec "$REAL_PYTHON" "$@"; fi
case "$*" in
  *"-m activation_transaction "*|*"/activation_transaction.py "*) exec "$REAL_PYTHON" "$@" ;;
esac
exit 0
PYTHON
chmod 755 "$project/.venv/bin/python"
""",
            )
            self.write_executable(
                uv_installer_source,
                """#!/bin/sh
if env | grep -Eq '^(HYADEX_AGENT_TOKEN|ZENITHDOCK_AGENT_TOKEN|ZENITHBOT_AGENT_TOKEN|HYADEX_PUBLISH_TOKEN|HYADEX_PROVIDER_AUTHORITY_FILE)='; then
  exit 92
fi
: > "$FAKE_UV_BOOTSTRAP_CLEAN"
mkdir -p "$HOME/.local/bin"
cp "$FAKE_UV_BINARY_SOURCE" "$HOME/.local/bin/uv"
chmod 755 "$HOME/.local/bin/uv"
""",
            )
            self.write_executable(
                fake_bin / "curl",
                f"""#!/bin/sh
case "$*" in *--version*) exit 0 ;; esac
output=""
want_output="false"
for argument in "$@"; do
  if [ "$want_output" = "true" ]; then output="$argument"; want_output="false"; continue; fi
  if [ "$argument" = "--output" ]; then want_output="true"; fi
done
case "$*" in
  *astral.sh*) cp "$FAKE_UV_INSTALLER_SOURCE" "$output" ;;
  *) cat > "$output" <<'JSON'
{{"ok":true,"server_version":"{self.release_version()}","server_identity":"server_uv_bootstrap_12345678","capabilities":{{"team_hub_v1":{{"available":false,"designated_host":false,"version":1,"base_path":null,"hub_id":null,"host_server_identity":null,"transport":null,"hub_url":null}},"secure_peer_v1":{{"available":true,"state_available":true,"state_error_code":null,"required":false,"version":1,"control_path":"/api/admin/secure-peers/v1/status","proxy_prefix":"/api/team-hub-secure"}}}}}}
JSON
  ;;
esac
""",
            )
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(config_root),
                "HYADEX_STATE_DIR": str(state_root),
                "HYADEX_AGENT_TOKEN": "secret_agent_token_abcdefghijklmnopqrstuvwxyz",
                "ZENITHDOCK_AGENT_TOKEN": "secret_zenith_token_abcdefghijklmnopqrstuvwxyz",
                "ZENITHBOT_AGENT_TOKEN": "secret_legacy_token_abcdefghijklmnopqrstuvwxyz",
                "HYADEX_PUBLISH_TOKEN": "secret_publish_token_abcdefghijklmnopqrstuvwxyz",
                "HYADEX_PROVIDER_AUTHORITY_FILE": "/tmp/private-authority-file.json",
                "FAKE_UV_INSTALLER_SOURCE": str(uv_installer_source),
                "FAKE_UV_BINARY_SOURCE": str(uv_binary_source),
                "FAKE_UV_BOOTSTRAP_CLEAN": str(bootstrap_clean),
                "REAL_PYTHON": sys.executable,
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            }
            for name in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
            ):
                environment.pop(name, None)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unavailable prerequisite: uv", result.stderr)
            self.assertIn("Install the trusted uv package manager", result.stderr)
            self.assertFalse(bootstrap_clean.exists())
            self.assertFalse((home / ".local" / "bin" / "uv").exists())

    def test_dependency_sync_rejects_a_missing_staged_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", "#!/bin/sh\nexit 0\n")

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not create the isolated release runtime", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_default_legacy_state_is_moved_and_linked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home.mkdir()
            legacy = home / ".zenithbot-agent"
            legacy.mkdir()
            (legacy / "sessions.json").write_text('{"kept": true}\n')
            legacy_runtime = home / "Zenithbot"
            legacy_runtime.mkdir()
            preserved_token = "legacy_token_abcdefghijklmnopqrstuvwxyz012345"
            (legacy_runtime / ".env").write_text(
                f"ZENITHDOCK_AGENT_TOKEN={preserved_token}\n"
                "NOTION_TOKEN=keep-private-runtime-setting\n"
                f"PATH={fake_bin}:/legacy/runtime/bin:/usr/bin:/bin\n"
            )
            legacy_service_dir = home / ".config" / "systemd" / "user"
            legacy_service_dir.mkdir(parents=True)
            (legacy_service_dir / "zenithbot-agent.service").write_text(
                "[Service]\nEnvironmentFile=%h/Zenithbot/.env\n"
            )
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_json_health_curl(fake_bin)
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "HYADEX_SERVER_INSTALL_DIR": str(root / "install"),
                "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                "REAL_PYTHON": sys.executable,
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": "d4" * 12,
                "FAKE_TEAM_HUB_MODE": "disabled",
                "FAKE_EVENT_LOG": str(root / "systemctl-events.log"),
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            }
            for name in ("HYADEX_STATE_DIR", "HYADEX_SERVER_STATE_DIR", "ZENITHBOT_AGENT_DIR"):
                environment.pop(name, None)

            # These lifecycle fixtures advertise disabled Hub health; inherited
            # host settings must not silently enable a real candidate Hub.
            for key in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
                "HYADEX_TEAM_HUB_DIRECT_IP_URL",
            ):
                environment.pop(key, None)
            environment["FAKE_TEAM_HUB_MODE"] = "disabled"

            result = subprocess.run(
                ["bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            canonical = home / ".hyadex"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.resolve(), canonical.resolve())
            self.assertEqual((canonical / "sessions.json").read_text(), '{"kept": true}\n')
            installed_env = (root / "config" / "env").read_text()
            self.assertIn(f"HYADEX_AGENT_TOKEN={preserved_token}\n", installed_env)
            self.assertIn("NOTION_TOKEN=keep-private-runtime-setting\n", installed_env)
            self.assertIn("/legacy/runtime/bin", installed_env)
            self.assertNotIn("ZENITHDOCK_AGENT_TOKEN=", installed_env)
            self.assertIn(f'"access_token":"{preserved_token}"', result.stdout)

    def test_broken_tmux_is_optional_and_installation_still_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_running_systemd_service(home)
            config_root = root / "config"
            state_root = root / "state"
            config_root.mkdir()
            state_root.mkdir()
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            token = "preserved_token_abcdefghijklmnopqrstuvwxyz0123456789"
            self.write_private_file(config_root / "env", f"HYADEX_AGENT_TOKEN={token}\n")
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 127\n")
            self.write_json_health_curl(fake_bin)
            self.write_fake_uv(fake_bin)
            environment.update(
                {
                    "FAKE_HEALTH_VERSION": self.release_version(),
                    "FAKE_SERVER_IDENTITY": "server_optional_tmux_12345678",
                    "FAKE_TEAM_HUB_ID": "hub_optional_tmux_12345678",
                    "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                }
            )

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Optional prerequisite unavailable: tmux", result.stderr)
            self.assertIn("sudo apt install tmux", result.stderr)
            self.assertIn("everything else about HyadexServer works without it", result.stderr)
            self.assertTrue((install_root / "current" / "agent_server.py").is_file())
            self.assertEqual((state_root / "sessions.json").read_text(), '{"keep": true}\n')
            human_output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith("HYADEX_SETUP_RESULT=")
            )
            self.assertNotIn(token, human_output)
            self.assertNotIn("Access token", result.stdout)
            self.assertNotIn(token, result.stdout)
            self.assertNotIn("HYADEX_SETUP_RESULT=", result.stdout)

    def test_preflight_reports_every_missing_platform_prerequisite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            home.mkdir()
            self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux",))
            self.write_unavailable_uv(fake_bin)
            self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 1\n")
            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": str(fake_bin),
                    "HYADEX_SERVER_INSTALL_DIR": str(root / "install"),
                    "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                    "HYADEX_STATE_DIR": str(root / "state"),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Optional prerequisite unavailable: tmux", result.stderr)
            self.assertIn("Unavailable prerequisite: uv", result.stderr)
            self.assertIn("Unavailable prerequisite: systemctl --user session", result.stderr)
            final_error = result.stderr.strip().splitlines()[-1]
            self.assertNotIn("tmux", final_error)
            self.assertIn("Missing prerequisites: uv, systemctl --user session.", final_error)
            self.assertFalse((root / "install").exists())
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "state").exists())

    def test_preflight_rejects_intel_macos_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            source = root / "source"
            fake_bin.mkdir()
            home.mkdir()
            source.mkdir()
            for name in package_release.FILES:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, source / name)
            for directory, names in package_release.DIRECTORY_FILES.items():
                for name in names:
                    destination = source / directory / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ROOT / directory / name, destination)
            self.prepare_preflight_path(
                fake_bin,
                os_name="Darwin",
                commands=("curl", "launchctl", "tmux"),
            )
            self.write_executable(
                fake_bin / "uname",
                "#!/bin/sh\nif [ \"${1:-}\" = \"-m\" ]; then echo x86_64; else echo Darwin; fi\n",
            )
            result = subprocess.run(
                ["/bin/bash", str(source / "install.sh"), "--execution-mode", "legacy", "--non-interactive"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": str(fake_bin),
                    "HYADEX_SERVER_INSTALL_DIR": str(root / "install"),
                    "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
                    "HYADEX_STATE_DIR": str(root / "state"),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported macOS architecture: x86_64", result.stderr)
            self.assertIn("Apple silicon (arm64)", result.stderr)
            self.assertIn("no state, release, configuration, or service changes", result.stderr)
            self.assertFalse((root / "install").exists())
            self.assertFalse((root / "config").exists())
            self.assertFalse((root / "state").exists())

    def test_rosetta_macos_reexecs_native_arm64_before_parsing_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            arch_log = root / "arch-arguments.log"
            fake_bin.mkdir()
            home.mkdir()
            self.write_executable(
                fake_bin / "uname",
                """#!/bin/sh
if [ "${1:-}" = "-s" ]; then
  echo Darwin
elif [ "${1:-}" = "-m" ]; then
  if [ "${FAKE_NATIVE_ARCH:-}" = "1" ]; then echo arm64; else echo x86_64; fi
else
  echo Darwin
fi
""",
            )
            self.write_executable(
                fake_bin / "sysctl",
                """#!/bin/sh
if [ "${1:-}" = "-n" ] && [ "${2:-}" = "hw.optional.arm64" ]; then
  echo 1
  exit 0
fi
exit 1
""",
            )
            self.write_executable(
                fake_bin / "arch",
                """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_ARCH_LOG"
[ "${1:-}" = "-arm64" ] || exit 97
[ "${2:-}" = "/bin/bash" ] || exit 98
shift 2
FAKE_NATIVE_ARCH=1 exec /bin/bash "$@"
""",
            )

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--help"],
                env={
                    **os.environ,
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "FAKE_ARCH_LOG": str(arch_log),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Usage: ./install.sh", result.stdout)
            self.assertEqual(
                arch_log.read_text().splitlines(),
                ["-arm64", "/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--help"],
            )

    def test_explicit_port_is_pinned_without_allow_port_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            (install_root / "current").unlink()
            self.write_fake_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            try:
                self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
                environment.update({
                    "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                    "FAKE_HEALTH_VERSION": self.release_version(),
                    "FAKE_SERVER_IDENTITY": "07" * 12,
                })
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(INSTALLER), "--execution-mode", "legacy",
                        "--port",
                        str(port),
                        "--non-interactive",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                listener.close()

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"HyadexServer {self.release_version()} did not become healthy", result.stderr)
            self.assertNotIn("Selecting port", result.stderr)
            self.assertNotIn("HYADEX_SETUP_RESULT=", result.stdout)
            self.assertTrue((install_root / "releases" / "0.0.9" / "runtime-marker").is_file())

    def test_explicit_port_fallback_is_opt_in_and_persists_selected_port(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            (install_root / "current").unlink()
            config_root = root / "config"
            config_root.mkdir()
            token = "preserved_token_abcdefghijklmnopqrstuvwxyz0123456789"
            self.write_private_file(config_root / "env", f"HYADEX_AGENT_TOKEN={token}\n")
            self.write_fake_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            try:
                self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
                environment.update({
                    "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                    "FAKE_HEALTH_VERSION": self.release_version(),
                    "FAKE_SERVER_IDENTITY": "08" * 12,
                })
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(INSTALLER), "--execution-mode", "legacy",
                        "--port",
                        str(port),
                        "--allow-port-fallback",
                        "--non-interactive",
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                listener.close()

            selected_port = port + 1
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                f"Selecting port {selected_port} instead",
                result.stderr,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            installed_env = (config_root / "env").read_text()
            self.assertIn(f"HYADEX_AGENT_PORT={selected_port}\n", installed_env)
            self.assertNotIn(f"HYADEX_AGENT_PORT={port}\n", installed_env)
            service = (home / ".config" / "systemd" / "user" / "hyadex-server.service").read_text()
            self.assertIn(f"--port {selected_port}", service)
            self.assertIn("ManagedOOMPreference=avoid", service)
            self.assertIn("TimeoutStopSec=10s", service)
            result_line = next(
                line
                for line in result.stdout.splitlines()
                if line.startswith("HYADEX_SETUP_RESULT=")
            )
            payload = json.loads(result_line.removeprefix("HYADEX_SETUP_RESULT="))
            self.assertTrue(payload["server_url"].endswith(f":{selected_port}"))
            self.assertEqual(payload["access_token"], token)
            human_output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith("HYADEX_SETUP_RESULT=")
            )
            self.assertNotIn(token, human_output)
            self.assertNotIn("Access token", result.stdout)

    def test_newly_started_unhealthy_server_rolls_back_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_exact_health_uv(fake_bin)
            listener, port = self.adjacent_port_listener()
            listener.close()
            self.write_health_curl(fake_bin, conflict_port=port, healthy_port=port + 1)
            environment.update({
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                "REAL_PYTHON": sys.executable,
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive", "--port", str(port), "--allow-port-fallback"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Selecting port", result.stderr)
            self.assertNotIn("Your new service is up!", result.stdout)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())







    def test_managed_disabled_install_does_not_print_the_agent_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _home, fake_bin, _install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_running_systemd_service(root / "home")
            self.write_exact_health_uv(fake_bin)
            self.write_json_health_curl(fake_bin)
            server_identity = "server_disabled_update_12345678"
            token = "preserved_token_abcdefghijklmnopqrstuvwxyz0123456789"
            config_root = root / "config"
            config_root.mkdir()
            self.write_private_file(config_root / "env",
                f"HYADEX_AGENT_TOKEN={token}\n"
                "HYADEX_AGENT_PORT=17850\n"
                "HYADEX_TEAM_HUB_MODE=disabled\n"
            )
            environment.update(
                {
                    "FAKE_HEALTH_VERSION": self.release_version(),
                    "FAKE_SERVER_IDENTITY": server_identity,
                    "FAKE_TEAM_HUB_MODE": "disabled",
                    "REAL_PYTHON": sys.executable,
                    "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                }
            )

            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--non-interactive",
                    "--port",
                    "17850",
                    "--expected-server-identity",
                    server_identity,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("HYADEX_SETUP_RESULT", result.stdout)
            self.assertNotIn(token, result.stdout)










    def test_rendering_systemd_service_does_not_run_restart_from_comment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, _install_root, environment = (
                self.fake_linux_preinstall_environment(root)
            )
            self.write_running_systemd_service(home)
            self.write_exact_health_uv(fake_bin)
            self.write_json_health_curl(fake_bin)
            server_identity = "server_service_render_12345678"
            config_root = root / "config"
            config_root.mkdir()
            self.write_private_file(config_root / "env",
                "HYADEX_AGENT_TOKEN=preserved_token_abcdefghijklmnopqrstuvwxyz0123456789\n"
                "HYADEX_AGENT_PORT=17850\n"
                "HYADEX_TEAM_HUB_MODE=disabled\n"
            )
            systemctl_log = root / "systemctl.log"
            bare_restart_sentinel = root / "bare-systemctl-restart"
            self.write_executable(
                fake_bin / "systemctl",
                """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [ "$#" -eq 1 ] && [ "$1" = "restart" ]; then
  : > "$FAKE_BARE_SYSTEMCTL_RESTART"
  exit 97
fi
exit 0
""",
            )
            environment.update(
                {
                    "FAKE_HEALTH_VERSION": self.release_version(),
                    "FAKE_SERVER_IDENTITY": server_identity,
                    "FAKE_TEAM_HUB_MODE": "disabled",
                    "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                    "FAKE_BARE_SYSTEMCTL_RESTART": str(bare_restart_sentinel),
                    "REAL_PYTHON": sys.executable,
                    "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                }
            )

            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--non-interactive",
                    "--port",
                    "17850",
                    "--expected-server-identity",
                    server_identity,
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            service = (
                home / ".config" / "systemd" / "user" / "hyadex-server.service"
            ).read_text()
            calls = systemctl_log.read_text().splitlines()
            self.assertFalse(
                bare_restart_sentinel.exists(),
                f"rendering the service ran a bare systemctl restart; calls: {calls}",
            )
            self.assertNotIn("restart", calls)
            self.assertIn("synchronous systemctl restart", service)




























    def test_missing_uv_does_not_attempt_a_curl_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_unavailable_uv(fake_bin)
            curl_log = root / "curl.log"
            self.write_executable(fake_bin / "curl", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
printf '%s\n' "$*" >> "$FAKE_CURL_LOG"
echo 'curl: simulated network timeout' >&2
exit 28
""")
            self.write_executable(fake_bin / "wget", "#!/bin/sh\nexit 127\n")
            environment.update({
                "FAKE_CURL_LOG": str(curl_log),
                "HYADEX_SERVER_DOWNLOAD_CONNECT_TIMEOUT_SECONDS": "1",
                "HYADEX_SERVER_DOWNLOAD_TIMEOUT_SECONDS": "2",
                "HYADEX_SERVER_DOWNLOAD_RETRIES": "2",
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(curl_log.exists())
            self.assertIn("Unavailable prerequisite: uv", result.stderr)
            self.assertIn("Install the trusted uv package manager", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_missing_uv_does_not_attempt_a_wget_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_unavailable_uv(fake_bin)
            wget_log = root / "wget.log"
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 127\n")
            self.write_executable(fake_bin / "wget", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
printf '%s\n' "$*" >> "$FAKE_WGET_LOG"
echo 'wget: simulated network timeout' >&2
exit 4
""")
            environment.update({
                "FAKE_WGET_LOG": str(wget_log),
                "HYADEX_SERVER_DOWNLOAD_TIMEOUT_SECONDS": "2",
                "HYADEX_SERVER_DOWNLOAD_RETRIES": "2",
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(wget_log.exists())
            self.assertIn("Unavailable prerequisite: uv", result.stderr)
            self.assertIn("Install the trusted uv package manager", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())

    def test_dependency_sync_streams_progress_and_times_out_before_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            descendant_marker = root / "uv-descendant-survived"
            self.write_executable(fake_bin / "curl", """#!/bin/sh
for argument in "$@"; do
  [ "$argument" != "--version" ] || exit 0
done
exit 22
""")
            self.write_executable(fake_bin / "uv", f"""#!/bin/sh
echo 'FAKE_UV_VISIBLE_PROGRESS'
(
  /bin/sleep 2
  echo survived > "{descendant_marker}"
) &
wait
""")
            environment.update({
                "HYADEX_SERVER_DEPENDENCY_TIMEOUT_SECONDS": "1",
                "HYADEX_SERVER_INSTALL_HEARTBEAT_SECONDS": "1",
                "FAKE_DESCENDANT_MARKER": str(descendant_marker),
            })

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            # If timeout cleanup killed only the uv parent, its worker would
            # outlive setup and create this marker two seconds after launch.
            time.sleep(1.5)

            self.assertEqual(result.returncode, 124, result.stderr)
            self.assertIn("FAKE_UV_VISIBLE_PROGRESS", result.stdout)
            self.assertIn("Still working on dependency resolution", result.stdout)
            self.assertIn("dependency resolution timed out after 1s.", result.stderr)
            self.assertIn("Review the uv output above.", result.stderr)
            self.assertIn("the active release was not changed.", result.stderr)
            self.assertTrue((install_root / "current" / "runtime-marker").is_file())
            self.assertEqual(list((install_root / "releases").glob(".staging-*")), [])
            self.assertFalse(descendant_marker.exists())
            self.assertFalse((install_root / ".install-lock").exists())

    def test_active_install_lock_rejects_a_second_installer_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_linux_preinstall_environment(root)
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_fake_uv(fake_bin)
            lock = install_root / ".install-lock"
            lock.mkdir(mode=0o700)
            pid_file = lock / "pid"
            pid_file.write_text(f"{os.getpid()}\n")
            pid_file.chmod(0o600)
            before = self.snapshot_trees(install_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Another HyadexServer installation is already running", result.stderr)
            self.assertIn("cancel it from Hyadex before retrying", result.stderr)
            self.assertEqual(self.snapshot_trees(install_root), before)

    def test_unavailable_systemd_user_domain_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            config_root = root / "config"
            state_root = root / "state"
            fake_bin.mkdir()
            home.mkdir()
            release = install_root / "releases" / "0.0.9"
            release.mkdir(parents=True)
            (release / "runtime-marker").write_text("keep runtime\n")
            (install_root / "current").symlink_to(release, target_is_directory=True)
            config_root.mkdir()
            self.write_private_file(config_root / "env", "HYADEX_AGENT_TOKEN=keep-token\n")
            state_root.mkdir()
            (state_root / "sessions.json").write_text('{"keep": true}\n')
            self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux", "curl"))
            self.write_executable(fake_bin / "systemctl", """#!/bin/sh
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then
  echo 'Failed to connect to bus: No medium found' >&2
  exit 1
fi
exit 97
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": str(fake_bin),
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(config_root),
                "HYADEX_STATE_DIR": str(state_root),
            }
            before = self.snapshot_trees(install_root, config_root, state_root)

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            final_error = result.stderr.strip().splitlines()[-1]
            self.assertIn("systemctl --user session", final_error)
            self.assertIn("systemctl --user show-environment", final_error)
            self.assertIn("no state, release, configuration, or service changes were made.", final_error)
            self.assertEqual(self.snapshot_trees(install_root, config_root, state_root), before)

    def test_saved_custom_path_is_used_for_prerequisite_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            bootstrap_bin = root / "bootstrap-bin"
            custom_bin = root / "custom-bin"
            install_root = root / "install"
            config_root = root / "config"
            bootstrap_bin.mkdir()
            custom_bin.mkdir()
            home.mkdir()
            config_root.mkdir()
            self.prepare_preflight_path(bootstrap_bin, os_name="Linux", commands=())
            expected_tmux = custom_bin / "tmux"
            systemctl_log = root / "systemctl.log"
            self.write_private_file(config_root / "env", f"PATH={custom_bin}\n")
            self.write_executable(expected_tmux, "#!/bin/sh\nexit 0\n")
            self.write_json_health_curl(custom_bin)
            self.write_executable(custom_bin / "systemctl", """#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_SYSTEMCTL_LOG"
if [ "$1" = "--user" ] && [ "$2" = "show-environment" ]; then
  [ "$(command -v tmux)" = "$EXPECTED_TMUX" ] || exit 91
fi
exit 0
""")
            self.write_executable(custom_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": str(bootstrap_bin),
                "HYADEX_SERVER_INSTALL_DIR": str(install_root),
                "HYADEX_SERVER_CONFIG_DIR": str(config_root),
                "HYADEX_STATE_DIR": str(root / "state"),
                "FAKE_AUTHORITATIVE_SYSTEMCTL": "true",
                "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                "EXPECTED_TMUX": str(expected_tmux),
                "REAL_PYTHON": sys.executable,
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": "f6" * 12,
                "FAKE_TEAM_HUB_MODE": "disabled",
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            }

            # These lifecycle fixtures advertise disabled Hub health; inherited
            # host settings must not silently enable a real candidate Hub.
            for key in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
                "HYADEX_TEAM_HUB_DIRECT_IP_URL",
            ):
                environment.pop(key, None)
            environment["FAKE_TEAM_HUB_MODE"] = "disabled"

            result = subprocess.run(
                ["/bin/bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            installed_path = next(
                line.removeprefix("PATH=")
                for line in (config_root / "env").read_text().splitlines()
                if line.startswith("PATH=")
            )
            self.assertEqual(installed_path.split(":", 1)[0], str(custom_bin))
            self.assertIn("--user show-environment", systemctl_log.read_text().splitlines())

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

    def test_darwin_restart_waits_for_bootout_and_retries_transient_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_darwin_environment(root)
            self.write_json_health_curl(fake_bin)
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            (launch_agents / "com.hyadex.server.plist").write_text(
                "OLD_SERVICE_CONFIG\n"
            )
            server_identity = "0123456789abcdef01234567"
            state_root = root / "state"
            state_root.mkdir()
            self.write_private_file(
                state_root / "server-identity",
                server_identity + "\n",
            )
            launchctl_state = root / "launchctl-state"
            launchctl_log = root / "launchctl.log"
            transient_done = root / "transient-done"
            server_name = "Sonic & </string><key>Injected</key>"
            launchctl_state.write_text("loaded\n")
            self.write_executable(fake_bin / "launchctl", """#!/bin/sh
command="$1"
case "$command" in
  print-disabled)
    printf '{ }\n'
    exit 0
    ;;
  print)
    state="$(cat "$FAKE_LAUNCHCTL_STATE")"
    printf 'print:%s\n' "$state" >> "$FAKE_LAUNCHCTL_LOG"
    case "$state" in
      loaded) exit 0 ;;
      removing:*)
        remaining="${state#removing:}"
        if [ "$remaining" -gt 0 ]; then
          printf 'removing:%s\n' "$((remaining - 1))" > "$FAKE_LAUNCHCTL_STATE"
          exit 0
        fi
        printf 'absent\n' > "$FAKE_LAUNCHCTL_STATE"
        echo 'Could not find service' >&2
        exit 3
        ;;
      *) echo 'Could not find service' >&2; exit 3 ;;
    esac
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    printf 'removing:2\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
  enable|disable)
    printf '%s\n' "$command" >> "$FAKE_LAUNCHCTL_LOG"
    exit 0
    ;;
  bootstrap)
    printf 'bootstrap\n' >> "$FAKE_LAUNCHCTL_LOG"
    if [ ! -f "$FAKE_TRANSIENT_DONE" ]; then
      : > "$FAKE_TRANSIENT_DONE"
      echo 'Bootstrap failed: 5: Input/output error' >&2
      echo 'Try re-running the command as root for richer errors.' >&2
      exit 5
    fi
    printf 'loaded\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
esac
exit 2
""")
            environment.update({
                "FAKE_LAUNCHCTL_STATE": str(launchctl_state),
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                "FAKE_TRANSIENT_DONE": str(transient_done),
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": server_identity,
                "FAKE_TEAM_HUB_ID": "",
                "FAKE_TEAM_HUB_MODE": "disabled",
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
                "HYADEX_SERVER_NAME": server_name,
            })

            result = subprocess.run(
                [
                    "bash",
                    str(INSTALLER), "--execution-mode", "legacy",
                    "--port",
                    "17850",
                    "--non-interactive",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = launchctl_log.read_text().splitlines()
            self.assertLess(calls.index("bootout"), calls.index("print:removing:2"))
            self.assertLess(calls.index("print:removing:0"), calls.index("bootstrap"))
            self.assertEqual(calls.count("bootstrap"), 2)
            self.assertEqual(launchctl_state.read_text().strip(), "loaded")
            self.assertTrue((install_root / "current" / "agent_server.py").is_file())
            plist_path = (
                home
                / "Library"
                / "LaunchAgents"
                / "com.hyadex.server.plist"
            )
            plist = plist_path.read_text()
            self.assertNotIn("<key>HYADEX_TEAM_HUB_MODE</key><string>host</string>", plist)
            launchd = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(
                launchd["EnvironmentVariables"]["HYADEX_SERVER_NAME"],
                server_name,
            )
            self.assertNotIn("Injected", launchd)
            self.assertIn("&lt;/string&gt;&lt;key&gt;Injected&lt;/key&gt;", plist)

    def test_darwin_restart_allows_more_than_five_seconds_for_bootout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_darwin_environment(root)
            self.write_json_health_curl(fake_bin)
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            (launch_agents / "com.hyadex.server.plist").write_text(
                "OLD_SERVICE_CONFIG\n"
            )
            launchctl_state = root / "launchctl-state"
            launchctl_log = root / "launchctl.log"
            launchctl_state.write_text("loaded\n")
            self.write_executable(fake_bin / "launchctl", """#!/bin/sh
command="$1"
case "$command" in
  print-disabled)
    printf '{ }\n'
    exit 0
    ;;
  print)
    state="$(cat "$FAKE_LAUNCHCTL_STATE")"
    printf 'print:%s\n' "$state" >> "$FAKE_LAUNCHCTL_LOG"
    case "$state" in
      loaded) exit 0 ;;
      removing:*)
        remaining="${state#removing:}"
        if [ "$remaining" -gt 0 ]; then
          printf 'removing:%s\n' "$((remaining - 1))" > "$FAKE_LAUNCHCTL_STATE"
          exit 0
        fi
        printf 'absent\n' > "$FAKE_LAUNCHCTL_STATE"
        echo 'Could not find service' >&2
        exit 3
        ;;
      *) echo 'Could not find service' >&2; exit 3 ;;
    esac
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    # The former 50 x 0.1 s budget failed before this ordinary slow shutdown.
    printf 'removing:75\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
  enable|disable)
    printf '%s\n' "$command" >> "$FAKE_LAUNCHCTL_LOG"
    exit 0
    ;;
  bootstrap)
    printf 'bootstrap\n' >> "$FAKE_LAUNCHCTL_LOG"
    printf 'loaded\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
esac
exit 2
""")
            environment.update({
                "FAKE_LAUNCHCTL_STATE": str(launchctl_state),
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                "FAKE_HEALTH_VERSION": self.release_version(),
                "FAKE_SERVER_IDENTITY": "server_darwin_slow_bootout_12345678",
                "FAKE_TEAM_HUB_ID": "",
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            })

            # These lifecycle fixtures advertise disabled Hub health; inherited
            # host settings must not silently enable a real candidate Hub.
            for key in (
                "HYADEX_TEAM_HUB_MODE",
                "HYADEX_TEAM_HUB_TRANSPORT",
                "HYADEX_TEAM_HUB_URL",
                "HYADEX_TEAM_HUB_DIRECT_IP_URL",
            ):
                environment.pop(key, None)
            environment["FAKE_TEAM_HUB_MODE"] = "disabled"

            result = subprocess.run(
                ["bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = launchctl_log.read_text().splitlines()
            self.assertGreater(calls.count("print:removing:75"), 0)
            self.assertIn("print:removing:50", calls)
            self.assertLess(calls.index("print:removing:0"), calls.index("bootstrap"))
            self.assertEqual(launchctl_state.read_text().strip(), "loaded")
            self.assertTrue((install_root / "current" / "agent_server.py").is_file())

    def test_darwin_nontransient_bootstrap_failure_restores_previous_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home, fake_bin, install_root, environment = self.fake_darwin_environment(root)
            self.write_json_health_curl(fake_bin)
            old_runtime = install_root / "current"
            old_runtime.mkdir(parents=True)
            (old_runtime / "old-runtime-marker").write_text("preserved\n")
            (old_runtime / "VERSION").write_text("0.1.25-beta.1\n")
            old_python = old_runtime / ".venv" / "bin" / "python"
            old_python.parent.mkdir(parents=True)
            self.write_executable(old_python, "#!/bin/sh\nexit 0\n")
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            plist = launch_agents / "com.hyadex.server.plist"
            plist.write_text("OLD_SERVICE_CONFIG\n")
            launchctl_state = root / "launchctl-state"
            launchctl_log = root / "launchctl.log"
            launchctl_state.write_text("loaded\n")
            self.write_executable(fake_bin / "launchctl", """#!/bin/sh
command="$1"
case "$command" in
  print-disabled)
    printf '{ }\n'
    exit 0
    ;;
  print)
    state="$(cat "$FAKE_LAUNCHCTL_STATE")"
    printf 'print:%s\n' "$state" >> "$FAKE_LAUNCHCTL_LOG"
    if [ "$state" = "loaded" ]; then
      exit 0
    fi
    echo 'Could not find service' >&2
    exit 3
    ;;
  bootout)
    printf 'bootout\n' >> "$FAKE_LAUNCHCTL_LOG"
    printf 'absent\n' > "$FAKE_LAUNCHCTL_STATE"
    exit 0
    ;;
  enable|disable)
    printf '%s\n' "$command" >> "$FAKE_LAUNCHCTL_LOG"
    exit 0
    ;;
  bootstrap)
    if grep -q OLD_SERVICE_CONFIG "$3"; then
      printf 'bootstrap:old\n' >> "$FAKE_LAUNCHCTL_LOG"
      printf 'loaded\n' > "$FAKE_LAUNCHCTL_STATE"
      exit 0
    fi
    printf 'bootstrap:new\n' >> "$FAKE_LAUNCHCTL_LOG"
    echo 'Bootstrap failed: 78: Invalid property list' >&2
    exit 78
    ;;
esac
exit 2
""")
            environment.update({
                "FAKE_LAUNCHCTL_STATE": str(launchctl_state),
                "FAKE_LAUNCHCTL_LOG": str(launchctl_log),
                "FAKE_EVENT_LOG": str(launchctl_log),
                "FAKE_HEALTH_VERSION": "0.1.25-beta.1",
                "FAKE_HEALTH_VERSION_AFTER_RESTORE": "0.1.25-beta.1",
                "FAKE_SERVER_IDENTITY": "server_darwin_rollback_12345678",
                "FAKE_TEAM_HUB_MODE": "disabled",
                "HYADEX_SERVER_HEALTH_CHECK_ATTEMPTS": "1",
            })

            result = subprocess.run(
                ["bash", str(INSTALLER), "--execution-mode", "legacy", "--port", "17850", "--non-interactive"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("The previous release and service were restored.", result.stderr)
            calls = launchctl_log.read_text().splitlines()
            self.assertEqual(calls.count("bootstrap:new"), 1)
            self.assertEqual(calls.count("bootstrap:old"), 1)
            self.assertTrue((install_root / "current" / "old-runtime-marker").is_file())
            self.assertEqual(plist.read_text(), "OLD_SERVICE_CONFIG\n")
            self.assertEqual(launchctl_state.read_text().strip(), "loaded")

    def fake_darwin_environment(self, root: Path):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        temporary = root / "tmp"
        fake_bin.mkdir()
        home.mkdir()
        temporary.mkdir()
        self.write_executable(
            fake_bin / "uname",
            "#!/bin/sh\nif [ \"${1:-}\" = \"-m\" ]; then echo arm64; else echo Darwin; fi\n",
        )
        self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
        self.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
        self.write_executable(fake_bin / "sleep", "#!/bin/sh\nprintf 'sleep:%s\\n' \"$1\" >> \"$FAKE_LAUNCHCTL_LOG\"\n")
        self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "TMPDIR": str(temporary),
            "HYADEX_SERVER_INSTALL_DIR": str(install_root),
            "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
            "HYADEX_STATE_DIR": str(root / "state"),
            "FAKE_LAUNCHCTL_LOG": str(root / "launchctl.log"),
            "REAL_PYTHON": sys.executable,
        }
        environment.pop("HYADEX_SERVER_NAME", None)
        return home, fake_bin, install_root, environment

    @staticmethod
    def show_token_environment(root: Path):
        home = root / "home"
        install_root = root / "install"
        config_root = root / "config"
        state_root = root / "state"
        for directory in (home, install_root, config_root, state_root):
            directory.mkdir()
        environment = {
            **os.environ,
            "HOME": str(home),
            "HYADEX_SERVER_INSTALL_DIR": str(install_root),
            "HYADEX_SERVER_CONFIG_DIR": str(config_root),
            "HYADEX_STATE_DIR": str(state_root),
        }
        return home, install_root, config_root, state_root, environment

    def fake_linux_preinstall_environment(self, root: Path):
        home = root / "home"
        fake_bin = root / "bin"
        install_root = root / "install"
        home.mkdir()
        fake_bin.mkdir()
        self.prepare_preflight_path(fake_bin, os_name="Linux", commands=("tmux", "systemctl"))
        old_release = install_root / "releases" / "0.0.9"
        old_release.mkdir(parents=True)
        (old_release / "runtime-marker").write_text("preserved\n")
        (old_release / "VERSION").write_text("0.0.9\n")
        (install_root / "current").symlink_to(old_release, target_is_directory=True)
        environment = {
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HYADEX_SERVER_INSTALL_DIR": str(install_root),
            "HYADEX_SERVER_CONFIG_DIR": str(root / "config"),
            "HYADEX_STATE_DIR": str(root / "state"),
            "FAKE_AUTHORITATIVE_SYSTEMCTL": "true",
            "REAL_PYTHON": sys.executable,
        }
        for key in (
            "HYADEX_SERVER_NAME",
            "HYADEX_TEAM_HUB_MODE",
            "HYADEX_TEAM_HUB_TRANSPORT",
            "HYADEX_TEAM_HUB_URL",
            "HYADEX_TEAM_HUB_DIRECT_IP_URL",
        ):
            environment.pop(key, None)
        return home, fake_bin, install_root, environment

    @staticmethod
    def write_running_systemd_service(home: Path) -> Path:
        service_file = home / ".config" / "systemd" / "user" / "hyadex-server.service"
        service_file.parent.mkdir(parents=True, exist_ok=True)
        service_file.write_text("[Service]\nExecStart=/old/release/server\n")
        return service_file


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

    def write_fake_uv(self, fake_bin: Path):
        self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexec "$REAL_PYTHON" "$@"\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")

    def write_exact_health_uv(self, fake_bin: Path):
        self.write_executable(
            fake_bin / "uv",
            """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
fixture_mutate_config_path_file="${0%/*}/../mutate-config-path"
fixture_mutate_config_sentinel="${0%/*}/../config-mutated"
if [ -f "$fixture_mutate_config_path_file" ] && [ ! -e "$fixture_mutate_config_sentinel" ]; then
  fixture_mutate_config_path="$(cat "$fixture_mutate_config_path_file")"
  printf 'HYADEX_TEAM_HUB_MODE=disabled\nHYADEX_TEAM_HUB_TRANSPORT=loopback\nHYADEX_TEAM_HUB_URL=\n' > "$fixture_mutate_config_path"
  : > "$fixture_mutate_config_sentinel"
fi
mkdir -p "$project/.venv/bin"
cat > "$project/.venv/bin/python" <<'PYTHON'
#!/bin/sh
if [ "${1:-}" = "-" ]; then
  exec "$REAL_PYTHON" "$@"
fi
case "$*" in
  *"-m activation_transaction "*|*"/activation_transaction.py "*)
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli begin-host-cold-handoff"*|*"-m hyadex_team_hub.cli clear-host-cold-handoff"*|*"-m hyadex_team_hub.cli adopt-host-reactivation-preflight"*|*"-m hyadex_team_hub.cli abort-host-reactivation-preflight"*|*"-m hyadex_team_hub.cli verify-server-identity"*)
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli publish-fenced-start-authority"*|*"-m hyadex_team_hub.cli clear-fenced-start-authority"*)
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    case "$*" in
      *"--expected-reason host-reactivation"*) exec "$REAL_PYTHON" "$@" ;;
    esac
    exit 0
    ;;
  *"-m hyadex_team_hub.cli rebase-snapshot"*)
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    printf 'rebase:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exit 0
    ;;
  *"-m hyadex_team_hub.cli prepare-host-reactivation"*)
    printf 'prepare-reactivation:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli verify-host-reactivation-snapshot"*)
    printf 'verify-reactivation:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli restore-host-reactivation-snapshot"*)
    printf 'restore-reactivation:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exec "$REAL_PYTHON" "$@"
    ;;
  *"maintenance_fence_matches_control"*)
    printf 'verify-fence\n' >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    [ "${FAKE_FENCE_VERIFY_FAILURE:-false}" != "true" ] || exit $?
    exec "$REAL_PYTHON" -c 'import os, sys; value = os.lstat(sys.argv[1] + "/maintenance-fence.json"); print(value.st_dev); print(value.st_ino)' "$3"
    ;;
  *"clear_maintenance_fence_control"*)
    printf 'clear-fence\n' >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    [ "${FAKE_CLEAR_FAILURE:-false}" != "true" ] || exit $?
    if [ "${FAKE_SIGNAL_AFTER_CLEAR:-false}" = "true" ]; then
      kill -TERM "$PPID"
    fi
    rm -f -- "$3/maintenance-fence.json"
    exit 0
    ;;
  *"-m hyadex_team_hub.cli verify-snapshot"*)
    printf 'verify-snapshot:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    [ "${FAKE_CANDIDATE_SNAPSHOT_VERIFY_FAILURE:-${FAKE_SNAPSHOT_VERIFY_FAILURE:-false}}" != "true" ]
    exit $?
    ;;
  *"-m hyadex_team_hub.cli restore-snapshot"*)
    printf 'restore:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    [ "${FAKE_RESTORE_FAILURE:-false}" != "true" ] || exit $?
    if [ "${FAKE_SIGNAL_AFTER_RESTORE:-false}" = "true" ]; then
      kill -TERM "$PPID"
    fi
    exit 0
    ;;
  *"-m hyadex_team_hub.cli confirm-restored-host-reactivation-snapshot"*)
    printf 'confirm-restore:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli confirm-restored-snapshot"*)
    printf 'confirm-restore:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    [ "${FAKE_RESTORE_FAILURE:-false}" != "true" ] || exit $?
    exit 0
    ;;
  *"-m hyadex_team_hub.cli acknowledge-restored-host-reactivation-snapshot"*)
    printf 'acknowledge-restore:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    exec "$REAL_PYTHON" "$@"
    ;;
  *"-m hyadex_team_hub.cli acknowledge-restored-snapshot"*)
    printf 'acknowledge-restore:%s\n' "$*" >> "$FAKE_EVENT_LOG"
    if [ "${FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL:-false}" = "true" ]; then
      exec "$REAL_PYTHON" "$@"
    fi
    exit 0
    ;;
esac
exit 0
PYTHON
chmod 755 "$project/.venv/bin/python"
""",
        )

    def write_json_health_curl(self, fake_bin: Path):
        self.write_executable(
            fake_bin / "curl",
            """#!/bin/sh
output=""
want_output="false"
want_header="false"
peer_authorized="false"
request_path=""
for argument in "$@"; do
  if [ "$want_output" = "true" ]; then
    output="$argument"
    want_output="false"
    continue
  fi
  if [ "$want_header" = "true" ]; then
    case "$argument" in
      "X-Hyadex-Token: "*) peer_authorized="true" ;;
    esac
    want_header="false"
    continue
  fi
  case "$argument" in
    --version) exit 0 ;;
    --output) want_output="true" ;;
    -H|--header) want_header="true" ;;
    http://127.0.0.1:*) request_path="$argument" ;;
  esac
done
if [ -n "$output" ]; then
  health_version="$FAKE_HEALTH_VERSION"
  health_server_identity="$FAKE_SERVER_IDENTITY"
  health_hub_id="$FAKE_TEAM_HUB_ID"
  health_team_hub_mode="${FAKE_TEAM_HUB_MODE:-disabled}"
  health_connection_id="${FAKE_TEAM_HUB_CONNECTION_ID:-11111111-2222-4333-8444-555555555555}"
  health_host_server_identity="${FAKE_TEAM_HUB_HOST_SERVER_IDENTITY:-host_server_test_12345678}"
  health_secure_state_available="${FAKE_SECURE_PEER_STATE_AVAILABLE:-true}"
  omit_secure_peer_capability="${FAKE_OMIT_SECURE_PEER_CAPABILITY:-false}"
  peer_active="${FAKE_SECURE_PEER_ACTIVE:-}"
  peer_pairing_id="${FAKE_SECURE_PEER_PAIRING_ID:-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee}"
  peer_team_id="${FAKE_SECURE_PEER_TEAM_ID:-team_client_12345678}"
  peer_host_ca_fingerprint="${FAKE_SECURE_PEER_HOST_CA_FINGERPRINT:-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
  peer_public_key_fingerprint="${FAKE_SECURE_PEER_PUBLIC_KEY_FINGERPRINT:-sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}"
  peer_certificate_fingerprint="${FAKE_SECURE_PEER_CERTIFICATE_FINGERPRINT:-sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc}"
  peer_transcript_hash="${FAKE_SECURE_PEER_TRANSCRIPT_HASH:-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd}"
  peer_requested_scopes="${FAKE_SECURE_PEER_REQUESTED_SCOPES:-[\\\"teamspace.read\\\",\\\"teamspace.write\\\"]}"
  peer_granted_scopes="${FAKE_SECURE_PEER_GRANTED_SCOPES:-[\\\"teamspace.read\\\",\\\"teamspace.write\\\"]}"
  peer_transport_state="${FAKE_SECURE_PEER_TRANSPORT_STATE:-online}"
  peer_status_version="${FAKE_SECURE_PEER_STATUS_VERSION:-2}"
  peer_extra_approved="${FAKE_SECURE_PEER_EXTRA_APPROVED:-false}"
  peer_active_connection="${FAKE_SECURE_PEER_ACTIVE_CONNECTION:-true}"
  peer_pending="${FAKE_SECURE_PEER_PENDING:-false}"
  if [ -n "$health_server_identity" ] \
    && [ -n "${HYADEX_STATE_DIR:-}" ] \
    && [ ! -e "$HYADEX_STATE_DIR/server-identity" ]; then
    mkdir -p "$HYADEX_STATE_DIR"
    chmod 700 "$HYADEX_STATE_DIR"
    printf '%s\n' "$health_server_identity" > "$HYADEX_STATE_DIR/server-identity"
    chmod 600 "$HYADEX_STATE_DIR/server-identity"
  fi
  after_restore="false"
  if [ -n "${FAKE_EVENT_LOG:-}" ]; then
    case "$request_path" in
      */api/admin/secure-peers/v1/status) printf 'peer-status\n' >> "$FAKE_EVENT_LOG" ;;
      *) printf 'health\n' >> "$FAKE_EVENT_LOG" ;;
    esac
    restart_count="$(grep -c '^systemctl:--user restart hyadex-server.service$' "$FAKE_EVENT_LOG" || true)"
    start_count="$(grep -c '^systemctl:--user start hyadex-server.service$' "$FAKE_EVENT_LOG" || true)"
    if grep -q '^restore:' "$FAKE_EVENT_LOG" \
      || grep -q '^restore-reactivation:' "$FAKE_EVENT_LOG" \
      || grep -q '^launchctl:bootstrap:old$' "$FAKE_EVENT_LOG" \
      || grep -q '^bootstrap:old$' "$FAKE_EVENT_LOG" \
      || { [ "$restart_count" -ge 1 ] && [ "$start_count" -ge 1 ]; } \
      || [ "$restart_count" -ge 2 ]; then
      after_restore="true"
      health_version="${FAKE_HEALTH_VERSION_AFTER_RESTORE:-$health_version}"
      health_server_identity="${FAKE_SERVER_IDENTITY_AFTER_RESTORE:-$health_server_identity}"
      health_hub_id="${FAKE_TEAM_HUB_ID_AFTER_RESTORE:-$health_hub_id}"
      health_secure_state_available="${FAKE_SECURE_PEER_STATE_AVAILABLE_AFTER_RESTORE:-$health_secure_state_available}"
      health_team_hub_mode="${FAKE_TEAM_HUB_MODE_AFTER_RESTORE:-$health_team_hub_mode}"
      health_connection_id="${FAKE_TEAM_HUB_CONNECTION_ID_AFTER_RESTORE:-$health_connection_id}"
      health_host_server_identity="${FAKE_TEAM_HUB_HOST_SERVER_IDENTITY_AFTER_RESTORE:-$health_host_server_identity}"
      omit_secure_peer_capability="${FAKE_OMIT_SECURE_PEER_CAPABILITY_AFTER_RESTORE:-$omit_secure_peer_capability}"
      peer_active="${FAKE_SECURE_PEER_ACTIVE_AFTER_RESTORE:-$peer_active}"
      peer_pairing_id="${FAKE_SECURE_PEER_PAIRING_ID_AFTER_RESTORE:-$peer_pairing_id}"
      peer_team_id="${FAKE_SECURE_PEER_TEAM_ID_AFTER_RESTORE:-$peer_team_id}"
      peer_host_ca_fingerprint="${FAKE_SECURE_PEER_HOST_CA_FINGERPRINT_AFTER_RESTORE:-$peer_host_ca_fingerprint}"
      peer_public_key_fingerprint="${FAKE_SECURE_PEER_PUBLIC_KEY_FINGERPRINT_AFTER_RESTORE:-$peer_public_key_fingerprint}"
      peer_certificate_fingerprint="${FAKE_SECURE_PEER_CERTIFICATE_FINGERPRINT_AFTER_RESTORE:-$peer_certificate_fingerprint}"
      peer_transcript_hash="${FAKE_SECURE_PEER_TRANSCRIPT_HASH_AFTER_RESTORE:-$peer_transcript_hash}"
      peer_requested_scopes="${FAKE_SECURE_PEER_REQUESTED_SCOPES_AFTER_RESTORE:-$peer_requested_scopes}"
      peer_granted_scopes="${FAKE_SECURE_PEER_GRANTED_SCOPES_AFTER_RESTORE:-$peer_granted_scopes}"
      peer_transport_state="${FAKE_SECURE_PEER_TRANSPORT_STATE_AFTER_RESTORE:-$peer_transport_state}"
      peer_status_version="${FAKE_SECURE_PEER_STATUS_VERSION_AFTER_RESTORE:-$peer_status_version}"
      peer_extra_approved="${FAKE_SECURE_PEER_EXTRA_APPROVED_AFTER_RESTORE:-$peer_extra_approved}"
      peer_active_connection="${FAKE_SECURE_PEER_ACTIVE_CONNECTION_AFTER_RESTORE:-$peer_active_connection}"
    elif grep -q '^systemctl:--user restart hyadex-server.service$' "$FAKE_EVENT_LOG" \
      || grep -q '^launchctl:bootstrap:candidate$' "$FAKE_EVENT_LOG" \
      || grep -q '^bootstrap:new$' "$FAKE_EVENT_LOG"; then
      health_team_hub_mode="${FAKE_TEAM_HUB_MODE_AFTER_RESTART:-$health_team_hub_mode}"
      health_connection_id="${FAKE_TEAM_HUB_CONNECTION_ID_AFTER_RESTART:-$health_connection_id}"
      health_hub_id="${FAKE_TEAM_HUB_ID_AFTER_RESTART:-$health_hub_id}"
      health_host_server_identity="${FAKE_TEAM_HUB_HOST_SERVER_IDENTITY_AFTER_RESTART:-$health_host_server_identity}"
      health_secure_state_available="${FAKE_SECURE_PEER_STATE_AVAILABLE_AFTER_RESTART:-$health_secure_state_available}"
      omit_secure_peer_capability="${FAKE_OMIT_SECURE_PEER_CAPABILITY_AFTER_RESTART:-$omit_secure_peer_capability}"
      peer_active="${FAKE_SECURE_PEER_ACTIVE_AFTER_RESTART:-$peer_active}"
      peer_pairing_id="${FAKE_SECURE_PEER_PAIRING_ID_AFTER_RESTART:-$peer_pairing_id}"
      peer_team_id="${FAKE_SECURE_PEER_TEAM_ID_AFTER_RESTART:-$peer_team_id}"
      peer_host_ca_fingerprint="${FAKE_SECURE_PEER_HOST_CA_FINGERPRINT_AFTER_RESTART:-$peer_host_ca_fingerprint}"
      peer_public_key_fingerprint="${FAKE_SECURE_PEER_PUBLIC_KEY_FINGERPRINT_AFTER_RESTART:-$peer_public_key_fingerprint}"
      peer_certificate_fingerprint="${FAKE_SECURE_PEER_CERTIFICATE_FINGERPRINT_AFTER_RESTART:-$peer_certificate_fingerprint}"
      peer_transcript_hash="${FAKE_SECURE_PEER_TRANSCRIPT_HASH_AFTER_RESTART:-$peer_transcript_hash}"
      peer_requested_scopes="${FAKE_SECURE_PEER_REQUESTED_SCOPES_AFTER_RESTART:-$peer_requested_scopes}"
      peer_granted_scopes="${FAKE_SECURE_PEER_GRANTED_SCOPES_AFTER_RESTART:-$peer_granted_scopes}"
      peer_transport_state="${FAKE_SECURE_PEER_TRANSPORT_STATE_AFTER_RESTART:-$peer_transport_state}"
      peer_status_version="${FAKE_SECURE_PEER_STATUS_VERSION_AFTER_RESTART:-$peer_status_version}"
      peer_extra_approved="${FAKE_SECURE_PEER_EXTRA_APPROVED_AFTER_RESTART:-$peer_extra_approved}"
      peer_active_connection="${FAKE_SECURE_PEER_ACTIVE_CONNECTION_AFTER_RESTART:-$peer_active_connection}"
    else
      health_team_hub_mode="${FAKE_TEAM_HUB_MODE_BEFORE_RESTART:-disabled}"
      health_connection_id="${FAKE_TEAM_HUB_CONNECTION_ID_BEFORE_RESTART:-$health_connection_id}"
      health_hub_id="${FAKE_TEAM_HUB_ID_BEFORE_RESTART:-$health_hub_id}"
      health_host_server_identity="${FAKE_TEAM_HUB_HOST_SERVER_IDENTITY_BEFORE_RESTART:-$health_host_server_identity}"
      health_secure_state_available="${FAKE_SECURE_PEER_STATE_AVAILABLE_BEFORE_RESTART:-$health_secure_state_available}"
      omit_secure_peer_capability="${FAKE_OMIT_SECURE_PEER_CAPABILITY_BEFORE_RESTART:-$omit_secure_peer_capability}"
      peer_active="${FAKE_SECURE_PEER_ACTIVE_BEFORE_RESTART:-$peer_active}"
      peer_pairing_id="${FAKE_SECURE_PEER_PAIRING_ID_BEFORE_RESTART:-$peer_pairing_id}"
      peer_team_id="${FAKE_SECURE_PEER_TEAM_ID_BEFORE_RESTART:-$peer_team_id}"
      peer_host_ca_fingerprint="${FAKE_SECURE_PEER_HOST_CA_FINGERPRINT_BEFORE_RESTART:-$peer_host_ca_fingerprint}"
      peer_public_key_fingerprint="${FAKE_SECURE_PEER_PUBLIC_KEY_FINGERPRINT_BEFORE_RESTART:-$peer_public_key_fingerprint}"
      peer_certificate_fingerprint="${FAKE_SECURE_PEER_CERTIFICATE_FINGERPRINT_BEFORE_RESTART:-$peer_certificate_fingerprint}"
      peer_transcript_hash="${FAKE_SECURE_PEER_TRANSCRIPT_HASH_BEFORE_RESTART:-$peer_transcript_hash}"
      peer_requested_scopes="${FAKE_SECURE_PEER_REQUESTED_SCOPES_BEFORE_RESTART:-$peer_requested_scopes}"
      peer_granted_scopes="${FAKE_SECURE_PEER_GRANTED_SCOPES_BEFORE_RESTART:-$peer_granted_scopes}"
      peer_transport_state="${FAKE_SECURE_PEER_TRANSPORT_STATE_BEFORE_RESTART:-$peer_transport_state}"
      peer_status_version="${FAKE_SECURE_PEER_STATUS_VERSION_BEFORE_RESTART:-$peer_status_version}"
      peer_extra_approved="${FAKE_SECURE_PEER_EXTRA_APPROVED_BEFORE_RESTART:-$peer_extra_approved}"
      peer_active_connection="${FAKE_SECURE_PEER_ACTIVE_CONNECTION_BEFORE_RESTART:-$peer_active_connection}"
    fi
  fi
  if [ -z "$peer_active" ]; then
    if [ "$health_team_hub_mode" = "client" ]; then
      peer_active="true"
    else
      peer_active="false"
    fi
  fi
  case "$request_path" in
    */api/admin/secure-peers/v1/status)
      [ "$peer_authorized" = "true" ] || exit 96
      if [ "$peer_active" = "true" ]; then
        if [ "$peer_active_connection" = "true" ]; then
          peer_active_connection_json="\\\"$health_connection_id\\\""
          peer_pairing_status="connected"
          peer_effective_transport_state="$peer_transport_state"
          peer_base_path_json="\\\"/api/team-hub-secure/$health_connection_id\\\""
        elif [ "$peer_active_connection" = "false" ]; then
          peer_active_connection_json="null"
          peer_pairing_status="approved"
          peer_effective_transport_state="disconnected"
          peer_base_path_json="null"
        else
          exit 91
        fi
        peer_extra_json=""
        case "$peer_extra_approved:$peer_status_version" in
          true:1)
            peer_extra_json=',{"id":"bbbbbbbb-cccc-4ddd-8eee-ffffffffffff","direction":"outgoing","status":"approved","peer_server_identity":"server_secondary_host_12345678","host_server_identity":"server_secondary_host_12345678","hub_id":"hub_secondary_12345678","team_id":"team_secondary_12345678","connection_id":"66666666-7777-4888-8999-aaaaaaaaaaaa","host_ca_fingerprint":"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","peer_public_key_fingerprint":"sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","certificate_fingerprint":"sha256:1111111111111111111111111111111111111111111111111111111111111111","transcript_hash":"2222222222222222222222222222222222222222222222222222222222222222","requested_scopes":["teamspace.read"],"granted_scopes":["teamspace.read"],"local_proxy_base_path":null}'
            ;;
          true:2)
            peer_extra_json=',{"id":"bbbbbbbb-cccc-4ddd-8eee-ffffffffffff","direction":"outgoing","status":"approved","trust_state":"approved","transport_state":"disconnected","peer_server_identity":"server_secondary_host_12345678","host_server_identity":"server_secondary_host_12345678","hub_id":"hub_secondary_12345678","team_id":"team_secondary_12345678","connection_id":"66666666-7777-4888-8999-aaaaaaaaaaaa","host_ca_fingerprint":"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","peer_public_key_fingerprint":"sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","certificate_fingerprint":"sha256:1111111111111111111111111111111111111111111111111111111111111111","transcript_hash":"2222222222222222222222222222222222222222222222222222222222222222","requested_scopes":["teamspace.read"],"granted_scopes":["teamspace.read"],"local_proxy_base_path":null}'
            ;;
          false:1|false:2) ;;
          *) exit 92 ;;
        esac
        peer_pending_json=""
        case "$peer_pending:$peer_status_version" in
          true:1)
            peer_pending_json=',{"id":"cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa","direction":"outgoing","status":"pending_approval","connection_id":"77777777-8888-4999-8aaa-bbbbbbbbbbbb"}'
            ;;
          true:2)
            peer_pending_json=',{"id":"cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa","direction":"outgoing","status":"pending_approval","trust_state":"pending","transport_state":"disconnected","connection_id":"77777777-8888-4999-8aaa-bbbbbbbbbbbb"}'
            ;;
          false:1|false:2) ;;
          *) exit 90 ;;
        esac
        if [ "$peer_status_version" = "1" ]; then
          cat > "$output" <<EOF
{"version":1,"server_identity":"$health_server_identity","server_instance_id":"instance_test_12345678","active_connection_id":$peer_active_connection_json,"host":{"available":false,"enabled":false},"pairings":[{"id":"$peer_pairing_id","direction":"outgoing","status":"$peer_pairing_status","peer_server_identity":"$health_host_server_identity","host_server_identity":"$health_host_server_identity","hub_id":"$health_hub_id","team_id":"$peer_team_id","connection_id":"$health_connection_id","host_ca_fingerprint":"$peer_host_ca_fingerprint","peer_public_key_fingerprint":"$peer_public_key_fingerprint","certificate_fingerprint":"$peer_certificate_fingerprint","transcript_hash":"$peer_transcript_hash","requested_scopes":$peer_requested_scopes,"granted_scopes":$peer_granted_scopes,"local_proxy_base_path":$peer_base_path_json}$peer_extra_json$peer_pending_json],"remote_routes":[],"published_routes":[],"remote_route_delivery_available":false,"connection_error":null}
EOF
        elif [ "$peer_status_version" = "2" ]; then
          cat > "$output" <<EOF
{"version":2,"heartbeat_interval_seconds":30,"lease_seconds":90,"server_identity":"$health_server_identity","server_instance_id":"instance_test_12345678","active_connection_id":$peer_active_connection_json,"host":{"available":false,"enabled":false,"listen_port":7851,"advertised_host":null,"advertised_hosts":[],"ca_fingerprint":null,"pairing_link":null,"certificate_expires_at":null,"error":null,"error_code":null,"action":null},"pairings":[{"id":"$peer_pairing_id","direction":"outgoing","status":"$peer_pairing_status","trust_state":"approved","transport_state":"$peer_effective_transport_state","peer_server_identity":"$health_host_server_identity","host_server_identity":"$health_host_server_identity","hub_id":"$health_hub_id","team_id":"$peer_team_id","connection_id":"$health_connection_id","host_ca_fingerprint":"$peer_host_ca_fingerprint","peer_public_key_fingerprint":"$peer_public_key_fingerprint","certificate_fingerprint":"$peer_certificate_fingerprint","transcript_hash":"$peer_transcript_hash","requested_scopes":$peer_requested_scopes,"granted_scopes":$peer_granted_scopes,"local_proxy_base_path":$peer_base_path_json}$peer_extra_json$peer_pending_json],"remote_routes":[],"published_routes":[],"remote_route_delivery_available":false,"connection_error":null,"delivery_error":null}
EOF
        else
          exit 93
        fi
      elif [ "$peer_active" = "false" ]; then
        if [ "$health_team_hub_mode" = "host" ]; then
          peer_host_json='{"available":true,"enabled":true,"listen_port":7851,"advertised_host":null,"advertised_hosts":[],"ca_fingerprint":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","pairing_link":null,"certificate_expires_at":null,"error":null,"error_code":null,"action":null}'
        else
          peer_host_json='{"available":false,"enabled":false,"listen_port":7851,"advertised_host":null,"advertised_hosts":[],"ca_fingerprint":null,"pairing_link":null,"certificate_expires_at":null,"error":null,"error_code":null,"action":null}'
        fi
        cat > "$output" <<EOF
{"version":$peer_status_version,"heartbeat_interval_seconds":30,"lease_seconds":90,"server_identity":"$health_server_identity","server_instance_id":"instance_test_12345678","active_connection_id":null,"host":$peer_host_json,"pairings":[],"remote_routes":[],"published_routes":[],"remote_route_delivery_available":false,"connection_error":null,"delivery_error":null}
EOF
      else
        exit 95
      fi
      exit 0
      ;;
  esac
  case "$health_secure_state_available" in
    true) health_secure_state_error_code="null" ;;
    false) health_secure_state_error_code='"secure_peer_state_unavailable"' ;;
    *) exit 97 ;;
  esac
  if [ "$omit_secure_peer_capability" = "true" ]; then
    health_secure_capability=""
  elif [ "$omit_secure_peer_capability" = "false" ]; then
    health_secure_capability=',"secure_peer_v1":{"available":true,"state_available":'"$health_secure_state_available"',"state_error_code":'"$health_secure_state_error_code"',"required":false,"version":1,"control_path":"/api/admin/secure-peers/v1/status","proxy_prefix":"/api/team-hub-secure"}'
  else
    exit 94
  fi
  if [ "$health_team_hub_mode" = "host" ]; then
    health_transport="${FAKE_TEAM_HUB_TRANSPORT:-loopback}"
    health_url="${FAKE_TEAM_HUB_URL:-}"
    health_direct_ip_url="${FAKE_TEAM_HUB_DIRECT_IP_URL:-}"
    if [ "$after_restore" = "true" ]; then
      health_transport="${FAKE_TEAM_HUB_TRANSPORT_AFTER_RESTORE:-$health_transport}"
      health_url="${FAKE_TEAM_HUB_URL_AFTER_RESTORE:-$health_url}"
      health_direct_ip_url="${FAKE_TEAM_HUB_DIRECT_IP_URL_AFTER_RESTORE:-$health_direct_ip_url}"
    fi
    if [ -n "$health_url" ]; then
      health_hub_url="$(printf '"%s"' "$health_url")"
    else
      health_hub_url="null"
    fi
    health_routes="[{\\\"transport\\\":\\\"$health_transport\\\",\\\"hub_url\\\":$health_hub_url}]"
    if [ -n "$health_direct_ip_url" ] && [ "$health_transport" != "direct_ip" ]; then
      health_routes="[{\\\"transport\\\":\\\"$health_transport\\\",\\\"hub_url\\\":$health_hub_url},{\\\"transport\\\":\\\"direct_ip\\\",\\\"hub_url\\\":\\\"$health_direct_ip_url\\\"}]"
    fi
    cat > "$output" <<EOF
{"ok":true,"server_version":"$health_version","server_identity":"$health_server_identity","capabilities":{"team_hub_v1":{"available":true,"designated_host":true,"version":1,"base_path":"/api/team-hub","hub_id":"$health_hub_id","host_server_identity":"$health_server_identity","transport":"$health_transport","hub_url":$health_hub_url,"routes":$health_routes}$health_secure_capability}}
EOF
  elif [ "$health_team_hub_mode" = "failed_host" ]; then
    health_transport="${FAKE_TEAM_HUB_TRANSPORT:-loopback}"
    health_url="${FAKE_TEAM_HUB_URL:-}"
    if [ -n "$health_url" ]; then
      health_hub_url="$(printf '"%s"' "$health_url")"
    else
      health_hub_url="null"
    fi
    cat > "$output" <<EOF
{"ok":true,"server_version":"$health_version","server_identity":"$health_server_identity","capabilities":{"team_hub_v1":{"available":false,"designated_host":true,"version":1,"base_path":"/api/team-hub","hub_id":null,"host_server_identity":"$health_server_identity","transport":"$health_transport","hub_url":$health_hub_url,"routes":[],"startup_failure_reason":"database unavailable"}$health_secure_capability}}
EOF
  elif [ "$health_team_hub_mode" = "client" ]; then
    health_base_path="/api/team-hub-secure/$health_connection_id"
    cat > "$output" <<EOF
{"ok":true,"server_version":"$health_version","server_identity":"$health_server_identity","capabilities":{"team_hub_v1":{"available":true,"designated_host":false,"version":1,"base_path":"$health_base_path","hub_id":"$health_hub_id","host_server_identity":"$health_host_server_identity","connection_id":"$health_connection_id","transport":"secure_peer","hub_url":null,"routes":[{"transport":"secure_peer","hub_url":null,"base_path":"$health_base_path","connection_id":"$health_connection_id","host_server_identity":"$health_host_server_identity","hub_id":"$health_hub_id"}]}$health_secure_capability}}
EOF
  else
    cat > "$output" <<EOF
{"ok":true,"server_version":"$health_version","server_identity":"$health_server_identity","capabilities":{"team_hub_v1":{"available":false,"designated_host":false,"version":1,"base_path":null,"hub_id":null,"host_server_identity":null,"transport":null,"hub_url":null,"routes":[]}$health_secure_capability}}
EOF
  fi
fi
exit 0
""",
        )






    def write_health_curl(self, fake_bin: Path, *, conflict_port: int, healthy_port: int):
        self.write_executable(fake_bin / "curl", f"""#!/bin/sh
output=""
want_output="false"
request_path=""
for argument in "$@"; do
  if [ "$want_output" = "true" ]; then
    output="$argument"
    want_output="false"
    continue
  fi
  case "$argument" in
    --version) exit 0 ;;
    --output) want_output="true" ;;
    http://127.0.0.1:*) request_path="$argument" ;;
  esac
done
case "$request_path" in
  *":{conflict_port}/api/health") exit 22 ;;
  *":{healthy_port}/api/admin/secure-peers/v1/status")
    if [ -n "$output" ]; then
      cat > "$output" <<EOF
{{"version":2,"heartbeat_interval_seconds":30,"lease_seconds":90,"server_identity":"$FAKE_SERVER_IDENTITY","server_instance_id":"instance_port_fixture_12345678","active_connection_id":null,"host":{{"available":false,"enabled":false,"listen_port":7851,"advertised_host":null,"advertised_hosts":[],"ca_fingerprint":null,"pairing_link":null,"certificate_expires_at":null,"error":null,"error_code":null,"action":null}},"pairings":[],"remote_routes":[],"published_routes":[],"remote_route_delivery_available":false,"connection_error":null,"delivery_error":null}}
EOF
    fi
    exit 0
    ;;
  *":{healthy_port}/api/health")
    if [ -n "$output" ]; then
      cat > "$output" <<EOF
{{"ok":true,"server_version":"$FAKE_HEALTH_VERSION","server_identity":"$FAKE_SERVER_IDENTITY","capabilities":{{"team_hub_v1":{{"available":false,"designated_host":false,"version":1,"base_path":null,"hub_id":null,"host_server_identity":null,"transport":null,"hub_url":null,"routes":[]}},"secure_peer_v1":{{"available":true,"state_available":true,"state_error_code":null,"required":false,"version":1,"control_path":"/api/admin/secure-peers/v1/status","proxy_prefix":"/api/team-hub-secure"}}}}}}
EOF
    fi
    exit 0
    ;;
esac
exit 0
""")

    @staticmethod
    def adjacent_port_listener() -> tuple[socket.socket, int]:
        for _attempt in range(100):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            if port >= 65535:
                listener.close()
                continue
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind(("127.0.0.1", port + 1))
            except OSError:
                listener.close()
                continue
            finally:
                probe.close()
            listener.listen(8)
            return listener, port
        raise AssertionError("could not reserve a listener with a free adjacent port")

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
    def write_unavailable_uv(directory: Path) -> None:
        # The installer appends standard package-manager paths, so a restricted
        # PATH alone does not mask a host uv (e.g. Homebrew). Do not use
        # write_executable here: it makes uv --version succeed for runtime stubs.
        path = directory / "uv"
        path.write_text("#!/bin/sh\nexit 127\n")
        path.chmod(0o755)

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

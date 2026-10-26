"""Manual source installation into isolated, owned native user services.

The installer stages dependencies before takeover and uses the execution
controller's journal, startup hold, authenticated health and rollback. It never
downloads releases, operates on a running incumbent, or deletes retained data.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import ipaddress
import os
from pathlib import Path
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import execution_install as files
from execution_manage import ActivationController, InstallationLock, NativeServices
from hyadex.inventory import FILES
from server_instances import Instance, check_path, exclusive_lock, instance_name


def read_environment(path: Path) -> dict[str, str]:
    try:
        raw, _mode = files._read_file(path, private=True)
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in raw.decode().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values:
            raise ValueError("invalid or duplicate configuration environment key")
        words = shlex.split(value, comments=False)
        if len(words) > 1:
            raise ValueError("configuration values must be single literal strings")
        values[key] = words[0] if words else ""
    return values


def roots(name: str, home: Path) -> tuple[Path, Path, Path]:
    item = Instance(name, home)
    defaults = (item.runtime, item.config, item.state)
    names = ("HYADEX_SERVER_INSTALL_DIR", "HYADEX_SERVER_CONFIG_DIR", "HYADEX_STATE_DIR")
    result = tuple(Path(os.environ.get(key, str(default))) for key, default in zip(names, defaults))
    state_override = os.environ.get('HYADEX_STATE_DIR') or os.environ.get('HYADEX_SERVER_STATE_DIR') or os.environ.get('ZENITHBOT_AGENT_DIR')
    if state_override:
        result = (*result[:2], Path(state_override))
    for key in ('HYADEX_STATE_DIR', 'HYADEX_SERVER_STATE_DIR', 'ZENITHBOT_AGENT_DIR'):
        if os.environ.get(key) and Path(os.environ[key]) != result[2]:
            raise ValueError('conflicting state directory selectors')
    if name != "default" and result != defaults:
        raise ValueError("named instance roots must match their isolated bindings")
    for path in result:
        files._path(path)
        check_path(path, home)
        if path == home or path in home.parents:
            raise ValueError("installation roots cannot contain HOME")
    for i, first in enumerate(result):
        for second in result[i + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError("installation roots must not overlap")
    return result


def refuse_legacy_operation(root: Path) -> None:
    for name in (".activation-transaction", ".execution-uninstall.json"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise RuntimeError("finish the previous installer/uninstaller operation with its retained runtime before installing; existing evidence was preserved")


def validate_source(source: Path) -> str:
    for name in FILES:
        path = source / name
        if any((source / part).is_symlink() for part in (Path(name), *Path(name).parents) if part != Path('.')):
            raise ValueError(f"linked runtime source is not allowed: {name}")
        if not path.is_file():
            raise ValueError(f"missing runtime source: {name}")
    version = (source / "VERSION").read_text().strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError("invalid source version")
    return version


def staging_environment(home: Path) -> dict[str, str]:
    # Dependency tools/import checks cannot borrow the caller's server runtime,
    # provider credentials or alternate uv project/lock settings.
    result = {key: value for key, value in os.environ.items() if key in {
        "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"}}
    result.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1", UV_PYTHON_DOWNLOADS="automatic")
    return result


def run_stage(command: list[str], *, environment: dict[str, str], timeout: float,
              cwd: Path | None = None) -> None:
    """Keep dependency descendants owned through timeout or interruption."""
    process = subprocess.Popen(command, env=environment, cwd=cwd, stdin=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        status = process.wait(timeout=timeout)
        if status:
            raise subprocess.CalledProcessError(status, command)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        raise


def stage_source(source: Path, root: Path, version: str, uv: str) -> Path:
    files._ensure_directory(root / "releases")
    target = root / "releases" / (version + "-" + secrets.token_hex(6))
    target.mkdir(mode=0o700)
    try:
        for name in FILES:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source / name, destination, follow_symlinks=False)
            destination.chmod(0o700 if name.endswith('.sh') else 0o600)
        with tempfile.TemporaryDirectory(prefix="hyadex-install-check-") as scratch:
            environment = staging_environment(Path(scratch))
            run_stage([uv, "sync", "--locked", "--python", str(Path(sys.executable).resolve()), "--project", str(target)],
                      environment=environment, timeout=1200)
            python = target / ".venv/bin/python"
            if not python.is_file() or not os.access(python, os.X_OK):
                raise RuntimeError("dependency synchronization did not create a runnable interpreter")
            code = "import pathlib; [compile(p.read_bytes(), str(p), 'exec') for p in pathlib.Path('.').rglob('*.py') if '.venv' not in p.parts]; import agent_server, execution_service, websockets, claude_agent_sdk"
            run_stage([str(python), "-B", "-c", code], cwd=target,
                      environment=environment, timeout=90)
        return target
    except BaseException:
        # Only this invocation's unpublished directory is removable.
        shutil.rmtree(target)
        raise


def assert_stopped(services: NativeServices) -> None:
    if any(item["state"] not in {"absent", "stopped"} for item in services.snapshot().values()):
        raise RuntimeError("stop the selected instance before installing; no running service was changed")


def require_fresh_scaffold(root: Path, config: Path, state: Path, *, locked: bool = False) -> None:
    """A failed dependency stage may leave only empty, owned scaffolding."""
    allowed = {root: {'releases', '.install-lock'} if locked else {'releases'},
               config: set(), state: {'.server-process.lock', 'admin'}}
    for directory, names in allowed.items():
        if not directory.exists() and not directory.is_symlink():
            continue
        files._owned_directory(directory)
        if set(p.name for p in directory.iterdir()) - names:
            raise RuntimeError("fresh installation refuses existing runtime, configuration or state")
    releases = root / 'releases'
    if releases.exists() or releases.is_symlink():
        files._owned_directory(releases)
        if any(releases.iterdir()):
            raise RuntimeError("fresh installation refuses retained runtimes")
    lock = state / '.server-process.lock'
    if lock.exists() or lock.is_symlink():
        if files._read_file(lock, private=True)[0]:
            raise RuntimeError("unexpected state lock contents")
    admin = state / 'admin'
    if admin.exists() or admin.is_symlink():
        files._owned_directory(admin)
        if any(admin.iterdir()):
            raise RuntimeError('fresh installation refuses saved administration state')


def verify_existing_services(layout: files.ExecutionLayout) -> None:
    """Do not replace a same-named native job belonging to another runtime."""
    if layout.manifest_path.exists() or layout.manifest_path.is_symlink():
        old = files.installed_layout(layout.install_root)
        for role in ("worker", "gateway"):
            if files._read_file(old.service_path(role))[0] != files.render_service(old, role):
                raise RuntimeError("installed service configuration differs from its owned layout")
        return
    gateway = layout.service_path("gateway")
    if gateway.exists() or gateway.is_symlink():
        raise RuntimeError("gateway service has no owned execution layout")
    worker = layout.service_path("worker")
    if not worker.exists() and not worker.is_symlink():
        return
    data, _mode = files._read_file(worker)
    current = layout.install_root / "current"
    if layout.platform == "Darwin":
        import plistlib
        job = plistlib.loads(data)
        if (job.get("Label") != layout.service_label("worker") or
                job.get("ProgramArguments", [])[:2] != [str(current / '.venv/bin/python'), str(current / 'agent_server.py')]):
            raise RuntimeError("legacy service does not belong to the selected installation")
        env = job.get("EnvironmentVariables", {})
        selected_state = env.get("HYADEX_STATE_DIR", env.get("HYADEX_SERVER_STATE_DIR"))
        if selected_state is None and layout.environment.get('HYADEX_SERVER_INSTANCE', 'default') == 'default':
            selected_state = str(layout.home / '.hyadex')
        if selected_state != str(layout.state_root):
            raise RuntimeError("legacy service state binding does not match")
    else:
        lines = data.decode().splitlines()
        if (f"EnvironmentFile={layout.config_root / 'env'}" not in lines or not any(
                line.startswith(f"ExecStart={current / '.venv/bin/python'} {current / 'agent_server.py'} serve ") for line in lines)):
            raise RuntimeError("legacy service does not belong to the selected installation")


def install(args: argparse.Namespace, *, source: Path = ROOT) -> None:
    home = Path.home().resolve()
    root, config, state = roots(args.instance, home)
    values = read_environment(config / "env")
    previous_environment = dict(values)
    if args.show_token:
        token = values.get("HYADEX_AGENT_TOKEN")
        if not token:
            raise RuntimeError("no saved Hyadex access token")
        print(token)
        return
    refuse_legacy_operation(root)
    for key in ('HYADEX_STATE_DIR', 'HYADEX_SERVER_STATE_DIR', 'ZENITHBOT_AGENT_DIR'):
        if values.get(key) and Path(values[key]) != state:
            raise ValueError("saved state path differs from the selected path; set HYADEX_STATE_DIR explicitly to the existing directory")
    legacy_state = home / '.zenithbot-agent'
    if args.instance == 'default' and state == home / '.hyadex' and not state.exists() and (legacy_state.exists() or legacy_state.is_symlink()):
        raise ValueError("legacy state exists; select its real directory explicitly with HYADEX_STATE_DIR before installing")
    if args.recover_only:
        with InstallationLock(root):
            transaction, layout = files._load(root)
            if (layout.home, layout.config_root, layout.state_root) != (home, config, state):
                raise ValueError("pending operation belongs to another instance")
            if (transaction['scope'] == 'migration' and transaction['phase'] != 'committed'
                    and not transaction['before']['layout']['exists']
                    and transaction['prior_services']['worker']['state'] == 'running'):
                raise RuntimeError('recover this older live migration with its retained previous installer')
            ActivationController(layout).recover()
        print("Recovered the selected installation; retained state was preserved.")
        return
    version = validate_source(source)
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("install uv before running source installation")
    operating_system = platform.system()
    if operating_system not in {"Darwin", "Linux"} or (operating_system == "Darwin" and platform.machine() != "arm64"):
        raise RuntimeError("source installation supports Apple silicon macOS and Linux user services")
    port = args.port or int(values.get("HYADEX_AGENT_PORT", "7870"))
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    bind = args.bind or values.get("HYADEX_AGENT_BIND", "0.0.0.0")
    ipaddress.ip_address(bind)
    if args.instance != "default" and args.port is None and "HYADEX_AGENT_PORT" not in values:
        raise ValueError("a new named instance requires --port")
    expected = {"HYADEX_SERVER_INSTANCE": args.instance, "HYADEX_SERVER_INSTALL_DIR": str(root),
                "HYADEX_SERVER_CONFIG_DIR": str(config), "HYADEX_STATE_DIR": str(state)}
    for key, value in expected.items():
        if key in values and values[key] != value:
            raise ValueError("saved configuration belongs to another instance")
    values.update(expected)
    values.update(HYADEX_SERVER_STATE_DIR=str(state), HYADEX_AGENT_BIND=bind, HYADEX_AGENT_PORT=str(port))
    values.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    values.setdefault("HOME", str(home))
    if 'HYADEX_AGENT_TOKEN' not in values:
        legacy_tokens = {values[key] for key in ('ZENITHDOCK_AGENT_TOKEN', 'ZENITHBOT_AGENT_TOKEN', 'AGENT_TOKEN') if values.get(key)}
        if len(legacy_tokens) > 1:
            raise ValueError("saved token aliases conflict")
        values['HYADEX_AGENT_TOKEN'] = next(iter(legacy_tokens)) if legacy_tokens else secrets.token_hex(32)
    if not values["HYADEX_AGENT_TOKEN"]:
        raise ValueError("saved access token is empty")
    if args.server_name is not None:
        if not args.server_name.strip() or len(args.server_name.encode()) > 160 or any(ord(c) < 32 for c in args.server_name):
            raise ValueError("invalid server name")
        values["HYADEX_SERVER_NAME"] = args.server_name
    elif args.instance != "default":
        values.setdefault("HYADEX_SERVER_NAME", args.instance)
    service_dir = home / ("Library/LaunchAgents" if operating_system == "Darwin" else ".config/systemd/user")
    # Refuse occupied/native targets before creating configuration or state.
    probe = files.ExecutionLayout(root, config, state, home, operating_system, root / 'unused', root / 'unused', bind, port, values)
    services = NativeServices(probe)
    assert_stopped(services)
    verify_existing_services(probe)
    if args.fresh_install_only:
        if any(probe.service_path(role).exists() or probe.service_path(role).is_symlink() for role in ('worker', 'gateway')):
            raise RuntimeError("fresh installation refuses existing services")
        require_fresh_scaffold(root, config, state)
    for directory in (root, config, state, service_dir):
        files._ensure_directory(directory)
    with InstallationLock(root), ExitStack() as stack:
        refuse_legacy_operation(root)
        from execution_recovery import require_retired_owners
        require_retired_owners(root)
        if read_environment(config / 'env') != previous_environment:
            raise RuntimeError("configuration changed during preflight; retry without overwriting it")
        if args.fresh_install_only:
            require_fresh_scaffold(root, config, state, locked=True)
        if (root / files.TRANSACTION_NAME).exists() or (root / files.TRANSACTION_NAME).is_symlink():
            raise RuntimeError("installation recovery is pending; run --recover-only first")
        assert_stopped(services)
        verify_existing_services(probe)
        stack.enter_context(exclusive_lock(state / ".server-process.lock"))
        candidate = stage_source(source, root, version, uv)
        layout = replace(probe, worker_release=candidate, gateway_release=candidate)
        # Recheck native ownership before handing the state lock to the worker.
        assert_stopped(services)
        verify_existing_services(layout)
        try:
            identity = files._read_file(state / 'server-identity', private=True)[0].decode().strip()
        except FileNotFoundError:
            identity = None
        if identity is not None and not re.fullmatch(r'[A-Za-z0-9_.:-]{8,240}', identity):
            raise ValueError("invalid preserved server identity")
        stack.close()
        ActivationController(layout).apply("install", expected_server_identity=identity)
    print(f"Installed Hyadex {version} ({args.instance}) on port {port}.")
    print(f"Use ./install.sh --instance {args.instance} --show-token to view its access token.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', type=instance_name, default=os.environ.get('HYADEX_SERVER_INSTANCE', 'default'))
    parser.add_argument('--port', type=int)
    parser.add_argument('--bind')
    parser.add_argument('--server-name')
    parser.add_argument('--show-token', action='store_true')
    parser.add_argument('--fresh-install-only', action='store_true')
    parser.add_argument('--recover-only', action='store_true')
    parser.add_argument('--non-interactive', action='store_true', help='accepted for unattended invocation; installation has no prompts')
    parser.add_argument('--no-port-fallback', action='store_true', help='ports are always exact')
    args = parser.parse_args(argv)
    os.umask(0o077)
    def interrupted(number, _frame):
        raise SystemExit(128 + number)
    for number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, interrupted)
    try:
        install(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

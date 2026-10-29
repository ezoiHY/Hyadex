"""Run an isolated Hyadex development server in the foreground.

    .venv/bin/python scripts/dev_server.py [--instance dev] [--port 7871] [--bind 127.0.0.1] [--exit-with-parent]

The server uses a named instance, so its state, configuration and tmux
sessions are separate from any installed default server (and from other
products on the same machine). Inherited agent/server environment variables
are dropped so the development server never borrows another server's
credentials. The access token is created once and kept in the instance's
configuration directory; this script prints where it is, not its value.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INHERITED_PREFIXES = ("HYADEX_", "AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHDOCK_", "ZENITHBOT_",
                      "CLAUDE_CODE_", "CLAUDECODE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--instance", default="dev")
    parser.add_argument("--port", type=int, default=7871)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="use 0.0.0.0 to accept LAN/Tailscale connections")
    parser.add_argument("--exit-with-parent", action="store_true",
                        help="shut down when the launching process exits (the Mac app's own backend)")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", args.instance) or args.instance == "default":
        parser.error("choose a lowercase instance name other than 'default'")

    home = Path.home()
    paths = {
        "HYADEX_SERVER_INSTALL_DIR": home / ".local/share/hyadex-server-instances" / args.instance,
        "HYADEX_SERVER_CONFIG_DIR": home / ".config/hyadex-server-instances" / args.instance,
        "HYADEX_STATE_DIR": home / ".hyadex-instances" / args.instance,
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = paths["HYADEX_SERVER_CONFIG_DIR"] / "dev-token"
    if not token_file.exists():
        descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    token = token_file.read_text().strip()

    env = {key: value for key, value in os.environ.items() if not key.startswith(INHERITED_PREFIXES)}
    env.update({key: str(value) for key, value in paths.items()})
    env.update({"HYADEX_SERVER_INSTANCE": args.instance, "HYADEX_AGENT_TOKEN": token,
                "PYTHONDONTWRITEBYTECODE": "1"})
    if args.exit_with_parent:
        # execve keeps this process id, so the server's parent stays the launcher.
        env["HYADEX_EXIT_WITH_PARENT_PID"] = str(os.getppid())
    print(f"Hyadex dev server '{args.instance}' on http://{args.bind}:{args.port}", flush=True)
    print(f"State: {paths['HYADEX_STATE_DIR']}", flush=True)
    print(f"Token: {token_file} (send it as 'Authorization: Bearer <token>')", flush=True)
    python = ROOT / ".venv/bin/python"
    if not python.exists():
        print("Run `uv sync --python 3.13` first.", file=sys.stderr)
        return 1
    os.chdir(ROOT)
    os.execve(str(python), [str(python), "agent_server.py", "serve", "--bind", args.bind, "--port", str(args.port)], env)


if __name__ == "__main__":
    raise SystemExit(main())

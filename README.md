# Hyadex

**A cluster of coding agents.** Hyadex is a self-hosted server that runs
Claude Code and Codex on a machine you control and lets them work together
with you in **group discussions**, alongside ordinary chats, scheduled jobs,
goals, terminals and file previews. Clients connect over your LAN or Tailscale.

> **Status:** first backend version. The Mac and Android clients are being
> rewritten; until then, use the HTTP/WebSocket API (see [API](#api)).
> Hyadex is derived from the AgentsDock server; see
> [History and license](#history-and-license).

## What it does

- **Native agents.** Each chat runs the real Claude Code (Agent SDK) or Codex
  (app-server) on your server, with that agent's own login, MCP servers,
  skills and project configuration.
- **Group discussions.** You, `@claude` and `@codex` share one timeline. Only
  mentioned agents run, each receives just the messages it has not seen, and
  only final replies reach the group, each with a zero-token receipt of the
  files it changed. Agents hand work to each other by mention, within a
  per-message hop budget. See [Group discussions](docs/GROUP_DISCUSSIONS.md).
- **Autopilot.** Give a goal: Claude plans, Codex implements task by task, and
  a reviewer checks each step against acceptance criteria. Your test command
  verifies every step inside the Codex sandbox, and only verified, reviewed
  trees are committed. Choose per run whether you approve the plan, every step
  or nothing, whether it works in an isolated worktree branch (one commit per
  step) or in place, and who reviews. See [Autopilot](docs/AUTOPILOT.md).
- **Scheduled jobs and goals** for chats and groups.
- **Persistent chats**: durable queues, restart recovery, history import from
  local Claude/Codex sessions, forks and side questions.
- **Workspace tools**: file browsing and previews, Git status/diffs, and
  persistent terminals (tmux).
- **Several servers per machine**: named instances with separate state.

Public snapshot links and interactive browser sharing are no longer supported.
Existing chat history and sharing data are retained.

Same-server chat routes remain available. Team Hub, Team Network mail and
secure-peer pairing have been removed; existing network state is preserved.

Claude Code and Codex are the supported runtimes. Cursor, Gemini and OpenCode
support have been removed; their saved chats
remain readable and can be organized. Execution and forks reject retired
providers, and their saved jobs remain paused.

## Requirements

- Apple silicon macOS with a desktop login, or Linux with `systemctl --user`.
- [`uv`](https://docs.astral.sh/uv/) (it installs Python 3.13) and `git`.
- `tmux` for persistent terminals (`brew install tmux`).
- Claude Code and/or Codex installed **and signed in** on the server machine.

## Run a development server

```bash
git clone https://github.com/ezoiHY/Hyadex.git
cd Hyadex
uv sync --locked --python 3.13
.venv/bin/python scripts/dev_server.py                  # http://127.0.0.1:7871
.venv/bin/python scripts/dev_server.py --bind 0.0.0.0   # reachable on LAN/Tailscale
```

The development server is a named instance (`dev`) with its own state in
`~/.hyadex-instances/dev`, so it never touches an installed server. It
prints the path of its access token file.

## Install as a service

```bash
./install.sh
```

The installer creates the default server as a user service
(`com.hyadex.server` on macOS, `hyadex-server.service` on Linux) on port
**7870** and prints the URL and token. `./install.sh --show-token` prints the
token again. Named instances keep separate state and ports; see
[named instances](docs/NAMED_INSTANCES_TESTING.md).

In-app updates have been removed.
For an existing installation, stop the selected service and rerun the source
installer; see [manual updates](docs/SERVER_REFERENCE.md#updating-hyadexserver).

## Connect

| Client location | Server URL |
| --- | --- |
| Same computer | `http://127.0.0.1:7870` |
| Same Wi-Fi/LAN | `http://<server LAN address>:7870` |
| Anywhere else | `http://<server Tailscale address>:7870`, with both devices on your tailnet |

A server bound to `127.0.0.1` cannot accept other devices. Keep the token
private and never expose the port directly to the public internet.

## Configuration

Settings are environment variables. The installed service also reads
`~/.config/hyadex-server/env`.

| Variable | Meaning |
| --- | --- |
| `HYADEX_AGENT_TOKEN` | Access token; required for any non-local use |
| `HYADEX_AGENT_PORT`, `HYADEX_AGENT_BIND` | Listening port (default 7870) and address (default `0.0.0.0`) |
| `HYADEX_AGENT_CWD` | Default working directory for new chats |
| `HYADEX_ENABLED_BACKENDS` | Backends new chats may use (default `claude,codex`) |

See the [server reference](docs/SERVER_REFERENCE.md) for the full list and
[memory admission](docs/MEMORY_ADMISSION.md) for launch thresholds.

## API

- REST under `/api/...` with `Authorization: Bearer <token>`. FastAPI serves the
  schema at `/openapi.json`.
- A non-loopback listener requires a nonempty `HYADEX_AGENT_TOKEN`. HTTP
  credentials belong in `Authorization` or `x-hyadex-token`, never `?token=`.
  WebSockets also accept the `hyadex-token.<base64url-token>` subprotocol.
  Cross-origin browser access to the server API is disabled; native clients
  and same-origin requests do not need CORS.
- Chat timelines stream over `WS /api/sessions/{id}/events?after=<seq>`;
  group timelines over `WS /api/groups/{id}/events/stream?after=<seq>`.
- Groups: [docs/GROUP_DISCUSSIONS.md](docs/GROUP_DISCUSSIONS.md). Autopilot:
  [docs/AUTOPILOT.md](docs/AUTOPILOT.md).
  Everything else: [docs/SERVER_REFERENCE.md](docs/SERVER_REFERENCE.md).

```bash
TOKEN=...; API=http://127.0.0.1:7871
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Auth refactor","cwd":"/path/to/repo","participants":[{"backend":"claude","role":"reviewer"},{"backend":"codex","role":"implementer"}]}' \
  $API/api/groups
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"@codex add input validation, then ask @claude to review"}' \
  $API/api/groups/<group_id>/messages
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"goal":"Add input validation to the signup form","cwd":"/path/to/repo","verify_command":"npm test"}' \
  $API/api/autopilot/runs     # then POST /api/autopilot/runs/<run_id>/approve once the plan is ready
```

## Development and tests

See [CONTRIBUTING.md](CONTRIBUTING.md) and the rules in [AGENTS.md](AGENTS.md).

## History and license

Hyadex began from the server in
[ZhengyiLuo/AgentsDock](https://github.com/ZhengyiLuo/AgentsDock) (Apache
License 2.0) and is developed independently, with its own history.

Hyadex is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution of the original work and third-party
components.

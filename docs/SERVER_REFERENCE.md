# Hyadex server reference

**HyadexServer is the self-hosted execution backend for
[Hyadex](https://github.com/ezoiHY/Hyadex).** Hyadex provides
the polished desktop and mobile chat experience; HyadexServer runs on the
machine that owns your workspaces, Claude Code installation, and Codex CLI.
Together they provide persistent agent chats without routing private project
files through a third-party chat service.

HyadexServer exposes an authenticated HTTP/WebSocket API and streams normalized
Claude/Codex events, files, videos, uploads, scheduled jobs, group discussions,
Autopilot runs and persistent tmux terminals to Hyadex clients.

```text
Hyadex (Mac, iPhone, iPad, Linux)
        |
        | private HTTP/WebSocket connection
        v
HyadexServer (your workstation or server)
        |
        +-- Claude Code CLI
        +-- Codex CLI
        +-- local workspaces, files, jobs, and tmux sessions
```

This directory is the maintained server inside the Hyadex repository. It
remains independently installable: run server commands from this directory;
building the desktop or mobile app is unnecessary. The standalone HyadexServer
repository and its signed downloads remain available during the update migration.
Keep local chat state, uploaded files, tokens, compiled caches, private hostnames,
and personal machine paths outside tracked source.

## What It Gives Hyadex

- Creates and resumes chat sessions for Claude and Codex CLI backends.
- Streams live agent events over WebSocket while preserving a JSONL event
  history on disk.
- Accepts file uploads and serves generated artifacts, including videos.
- Supports queued turns, stop requests, chat forking, and creating a chat that
  resumes an existing Claude or Codex conversation with its history.
- Recovers oversized Codex provider threads by rolling the same chat onto a
  fresh thread with bounded recent memory when remote compaction fails.
- Runs recurring/loop jobs per chat with host load/memory guardrails.
  Archiving a chat pauses every interval, cron, and RRULE job attached to it;
  unarchiving leaves those jobs paused until the user explicitly enables them.
- Gives each live provider turn a private, chat-scoped authority for the jobs,
  publish, emergency and notify helpers, and rotates it with the logical run
  during native steering. Failed steering restores the predecessor only when
  rejection is proven safe; stopped, uncertain, and restarted runs fail closed.
- Provides optional tmux-pane inspection for active work.
- Hosts one persistent interactive tmux terminal per chat. Clients attach over
  an authenticated PTY WebSocket; disconnecting a client does not stop the
  tmux session, its panes, or processes. Structured actions create, select,
  split, and close individual windows while guarding the final persistent
  window from accidental destruction. Archiving a chat kills this owned tmux
  session and prevents it from being recreated until the chat is unarchived.
- Gives every Claude and Codex turn the owning chat's tmux session name through
  `HYADEX_TMUX_SESSION` and prompt context. Agents can inspect the current
  pane with `tmux capture-pane` when it is relevant, but are instructed not to
  type into, resize, or destroy the interactive terminal without an explicit
  user request.
- Discovers available runtime models/efforts from the installed CLI tools when
  possible.
- Reports Claude Code and Codex installation, authentication, version, and
  latest-run health separately from basic server connectivity. New turns are
  rejected with an actionable error before timeline activity when their
  selected runtime is unavailable.

## Requirements

- Linux or Apple silicon (arm64) macOS host. The trusted `uv` runtime below
  provisions the release's isolated Python 3.10+ environment. Intel macOS is
  unsupported because the patched cryptography runtime no longer publishes or
  supports x86_64 macOS wheels; `install.sh` rejects that architecture before
  changing state, releases, configuration, or services.
- A trusted, preinstalled `uv` on `PATH` is required for the isolated runtime.
  On macOS, install it with `brew install uv`; on Linux, use your trusted OS or
  package-management environment. The installer never downloads and executes
  a mutable bootstrap script.
- Claude CLI and/or Codex CLI installed and authenticated on the agent host.
- Optional: `tmux`, for the persistent chat terminal and tmux-pane inspection.
  Everything else (chats, turns, jobs, files)
  works without it. Install it separately with your normal package manager.
- Tailscale on the agent host and each client device if you want to use the
  server from another Mac, iPhone, or iPad.
- Optional: a user-level `systemd` service on Linux.

### Retired providers

Cursor, Gemini and OpenCode no longer appear in the runtime catalog and cannot be
re-enabled with `HYADEX_ENABLED_BACKENDS`. Their saved chat history remains
readable and can be renamed, pinned, moved or archived. New turns, runtime
changes, forks and native imports reject the removed backend. Existing jobs
are persistently disabled with `provider_removed`. Native profile, credential
and history files are retained; no replacement provider is selected.

### Removed custom API endpoints

Custom Codex providers and per-chat Claude API connections have been removed,
with their admin routes and the `codex_provider_v1` and
`provider_connections_v1` capabilities. Chats always run with the native Claude
Code and Codex logins of the server's user. A chat saved against a removed
custom endpoint keeps readable history and can still be renamed, moved or
archived, but new turns, forks and runtime changes return 410 instead of
continuing on the native login. The saved endpoint settings and API keys in
`admin/codex-provider/` and `admin/provider-connections/` under the state
directory are left on disk; delete them if you no longer need them.

### Optional: tmux

tmux is not required to run HyadexServer, chat with agents, or use jobs,
files, or search. It backs the persistent interactive chat terminal and live
tmux-pane inspection. `GET /api/health` reports
`capabilities.tmux.available` so clients can grey those features out without
tmux rather than failing.

If tmux is missing, `install.sh` proceeds anyway and prints the install
command for your platform. On macOS with Homebrew installed and an
interactive terminal, it asks first whether to run `brew install tmux` for
you. Install tmux at any time and rerun `install.sh` to pick it up; no state,
release, or service is affected.

## Hyadex

Hyadex is the companion client for this server. It provides multiple chats
and folders, queues and scheduled jobs, rich Markdown/code rendering, inline
media, downloads and drag-out, code review, search, notifications, and
persistent per-chat terminals.

Get the client and current installation instructions from
[hyadex.net](https://github.com/ezoiHY/Hyadex). The macOS desktop app is available
as a Developer ID-signed and Apple-notarized build; Apple-platform test
builds are also distributed through TestFlight.

## Guided Setup

The native clients are being rewritten. Install from the source checkout as
the user who will run Claude Code or Codex:

```bash
git clone https://github.com/ezoiHY/Hyadex.git
cd Hyadex
./install.sh
```

The source installer requires `uv`, Python 3.13 (selected by the shell entrypoint)
and a usable launchd GUI domain or systemd user manager. It never installs system
tools or uses `sudo`. Install `tmux` separately for persistent terminals.

Both default and named instances use separate worker and gateway services.
Existing services must be stopped before installation. A unique retained runtime
is staged and import-checked before service/configuration publication. The native
controller journals the environment, both service files, layout and current link,
then verifies process-owned authenticated health. A failed candidate restores the
previous files and service state. Dependencies have a bounded timeout and their
owned process group is terminated on interruption. Chat data and the existing
token are preserved; old runtime directories are retained.

Ports are exact: `--port` never selects another port. `--bind` accepts a literal
IP address. Retired Team Hub, signed preparation and automatic port-fallback flags
are rejected. Old Zenith state directories are not migrated or followed.
`--fresh-install-only` refuses an existing installation or saved chat data, but
allows empty scaffolding from an earlier failed dependency stage.

Read the token with `./install.sh --show-token`, then connect to
`http://<server-address>:7870` (or the selected port). For source updates, finish
or stop active work, stop the selected instance and rerun its installer.

### Supported communication

Agents collaborate through [group discussions](GROUP_DISCUSSIONS.md) and
[Autopilot](AUTOPILOT.md). Messaging between ordinary chats (handoffs, `@Chat`
routes and the chat mailbox) has been removed; see
[Removed cross-chat messaging](#removed-cross-chat-messaging). Team Hub, Team
Network mail, shared team skills, and secure-peer server pairing were removed
earlier. These routes and capabilities are no longer advertised. Old saved Team
and `@Chat` references are rejected or paused for repair rather than executed
as plain prompts. Existing Team Hub and secure-peer state is not deleted by
running this version.

Public snapshot and interactive browser sharing are also removed. Old viewer,
media and sharing-management URLs no longer resolve; sharing cookies and tokens
do not authorize native APIs. Existing sharing databases, registered files and
chat history remain on disk. Previously accepted turns retain their attribution;
new turn requests carrying sharing attribution are rejected with 422.
Native side questions and chat file access remain available.

## Manual Onboarding

1. Clone this repo on the machine that will run the agents.

```bash
git clone https://github.com/ezoiHY/Hyadex.git
cd HyadexServer
```

2. Create a Python environment.

```bash
uv venv
uv sync --frozen
```

3. Install and authenticate the backend CLI tools you want to use.

HyadexServer does not bundle Claude or Codex. It shells out to the CLI tools
that are already installed on the agent host. Install the official Claude CLI
and/or Codex CLI, sign in or configure credentials for each, then verify the
commands work in the same shell/user that will run the server:

```bash
command -v claude
claude --version

command -v codex
codex --version
```

If you only want one backend, install only that backend and set
`HYADEX_BACKEND` accordingly.

4. Start the server locally.

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7870
```

5. Check health from the server machine.

```bash
curl http://127.0.0.1:7870/api/health
```

6. Connect Hyadex.

Open Hyadex, enter the server URL printed by the installer, and paste its
access token. The desktop app can also run the installer for you during
first-run setup.

For a client on the same machine, use:

```text
http://127.0.0.1:7870
```

For another Mac, iPhone, or iPad, use Tailscale. Install Tailscale on the agent
host and client device, confirm both devices are in the same tailnet, then use
the server's Tailscale IP:

```text
http://<tailscale-ip>:7870
```

Do not expose port `7870` directly to the public internet. Use Tailscale or
another private network, and set `HYADEX_AGENT_TOKEN` for shared-token
access control.

## Run Locally

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7870
```

Health check:

```bash
curl http://127.0.0.1:7870/api/health
```

The default state directory is `~/.hyadex`. Override it when you want
state somewhere else:

```bash
HYADEX_STATE_DIR=/path/to/state \
uv run python agent_server.py serve --bind 0.0.0.0 --port 7870
```

## Security

Set `HYADEX_AGENT_TOKEN` to require a shared bearer token for HTTP calls,
uploads, file/video fetches, and WebSocket streams.

```bash
export HYADEX_AGENT_TOKEN='replace-with-a-long-random-token'
uv run python agent_server.py serve --bind 0.0.0.0 --port 7870
```

Clients should send either:

```http
Authorization: Bearer replace-with-a-long-random-token
```

or `X-Hyadex-Token`. The legacy `X-ZenithDock-Token` header remains
accepted for existing clients.
Leave the variable unset only for trusted local development.

`install.sh` prints the generated token once, at the end of setup. To see it
again later without reinstalling anything:

```bash
./install.sh --show-token
```

This only reads the existing configuration and exits; it makes no changes.

## Managed server restart

Authenticated clients can discover restart support through the additive
`capabilities.server_restart` v1 health capability and the top-level
`server_instance_id`. Restart is available only when HyadexServer proves that
the running process belongs to the supported launchd or systemd user service
and is executing from the installer's resolved `current` release.

```text
GET  /api/admin/restart
POST /api/admin/restart
```

Both endpoints require the access token in an authorization header; URL token
parameters and browser-originated requests are rejected. POST accepts a small
JSON body containing a UUID `request_id`, the exact
`expected_server_identity`, the exact `expected_server_instance_id`, and
`confirmed: true`. It returns `202` before signaling the managed process.
Replaying the same request ID is idempotent; another pending or recently
completed request is rejected.

A cooperative restart is fail-closed while worker maintenance, an active turn,
provisional queue write, provider background task, lifecycle operation, or
HTTP mutation is in flight. Durable queued turns remain queued for recovery
after relaunch. Unmanaged processes cannot use this control.

### Forced (emergency) restart

Adding `force: true` and `force_confirmed: true` to the POST body requests an
emergency restart. It exists for the case where the server is wedged, so it
never refuses or waits on server state beyond authentication, the managed
service proof, and the exact `expected_server_identity` /
`expected_server_instance_id` pair:

- Cooldowns, a pending or stale restart record, worker maintenance,
  safety-critical work (Codex maintenance, session deletions, in-flight HTTP
  mutations, goal reconfiguration) are
  overridden and recorded in `forced_audit` instead of rejected.
- `expected_blocker_revision` is optional. A stale or omitted revision is
  audited (`blockers_changed_after_confirmation`, `blocker_revision_omitted`)
  rather than refused, because a wedged server may be unable to serve a fresh
  blocker snapshot at all.
- Every admission lock and probe on the forced path is bounded by a short
  timeout. If one cannot be obtained the audit snapshot is marked
  `snapshot_degraded` and the restart still proceeds. `GET /api/admin/restart`
  and `/api/health` use the same bounded snapshot so they cannot hang.
- The restart journal is best effort: if it cannot be written the restart
  still proceeds.
- SIGTERM is sent from a dedicated thread after the `202` response, and a
  hard-kill watchdog sends SIGKILL a few seconds later if graceful shutdown
  has not finished, so the user service always relaunches the server.

Forced restarts interrupt active agent turns and can leave provider children
to be reaped by the relaunched server; use the cooperative restart when the
server is healthy.

Requests containing the retired `expected_update_schedule_id` field are
rejected. They cannot be reinterpreted as a generic emergency restart.

## Remote Access With Tailscale

Remote access is expected to go through Tailscale. This keeps the server
reachable from phones, tablets, and laptops without publishing the raw agent
port on the internet. It's optional: everything else in this README works
without it, so `install.sh` only prints a reminder with the download link
at the end of a successful run when Tailscale isn't already on the host —
it never blocks setup or is installed automatically.

On the agent host:

```bash
tailscale status
tailscale ip -4
```

On the client device, make sure Tailscale is connected to the same account or
tailnet, then set the Hyadex server URL to:

```text
http://<tailscale-ip>:7870
```

If the browser can open `/api/health` but the app cannot connect, check:

- the client is also connected to Tailscale
- the URL includes the correct port
- the same `HYADEX_AGENT_TOKEN` is configured in the app
- the server is bound to `0.0.0.0` or the Tailscale interface, not only
  `127.0.0.1`

## Updating HyadexServer

For an existing installation, finish or explicitly stop its active turns,
then stop the selected instance before rerunning the source installer. The
instance command stops both worker and gateway. Its token and chat state are preserved:

```bash
git pull --ff-only
./instances.sh stop default
./install.sh
```

For a named instance, use its name in the stop command and
`./install.sh --instance NAME`. Do not stop another instance as part of an update.

On Linux, inspect the installed service with:

```bash
systemctl --user status hyadex-server.service --no-pager -l
journalctl --user -u hyadex-server.service -f
```

On macOS, default services are `com.hyadex.server` and `com.hyadex.gateway`;
named services append `.NAME` to each label. On Linux, named units are
`hyadex-server-NAME.service` and `hyadex-server-NAME-gateway.service`. Native
stdout/stderr logs live under the selected state directory in `execution/logs/`.

From a source checkout, `./instances.sh logs [NAME]` (`default` when omitted)
accepts `--lines N` and `--follow`. It selects the installed service layout,
including both gateway and worker logs for default and named installations.

### Retired in-app updates

The `/api/admin/update` routes, update capabilities and update status health
fields have been removed. `HYADEX_MANAGED_UPDATES` no longer enables them.
Old update receipts, logs and preparations remain on disk without being read or
resumed by the server. An old pending or active update cannot block new work.
At startup, jobs parked by older update reservations are made due again only
when their saved revision and occurrence still match; later user edits keep
their schedule. Normal restart and execution-worker maintenance remain guarded
by active runs, provisional queue writes, provider work and HTTP mutations.

An interrupted source installation with an `.execution-transaction` journal can
be recovered with `./install.sh --instance NAME --recover-only`. Recovery uses
the recorded owned layout and restores the previous stopped services, or finishes
an already committed installation. Do not delete journals or maintenance leases.

Older live-migration journals, `.activation-transaction` and pending uninstall evidence must be recovered
with the previous retained installer/uninstaller before using this source
installer. They are never silently adopted, resumed or discarded. The old helper
modules have been deleted: installation, worker startup and uninstall refuse to
proceed while such a journal is present, and the server does not launch update
recovery on startup.

This retirement raised the public API contract to 29 (it is now 30). Old update
health fields and schedule-bound restart fields are absent; the remaining
restart audit reports execution-worker maintenance. This retirement does not
publish or install a new release.

## Uninstalling HyadexServer

```bash
./uninstall.sh
```

This stops and removes the user service, the versioned release runtime, and
generated configuration (including the access token). It prompts before
making changes unless `--yes` is passed. Chat history, jobs, files, and
terminals under the state directory (`~/.hyadex` by default) are kept by
default, so a later `./install.sh` picks the same ordinary HyadexServer history
back up. Existing Team Hub state is preserved but cannot be reactivated by this
version.
Passing `--purge-state` permanently deletes that too, but always requires an
interactive exact-path confirmation that `--yes` cannot bypass. Before any
change, the uninstaller rejects root, home, broad system/user directories,
path traversal, and overlapping install/configuration/state roots. Like
`install.sh`, it never invokes a package manager or `sudo`. Persistent chat
terminal tmux sessions (named `hx_*`) are left running; list them with `tmux
ls` and remove them yourself if you no longer need them.

## Remote source installation

The direct `deploy.sh` helper has been removed. Check out the desired source on
the target machine and use its local installer with the selected instance stopped.
Changing this checkout does not publish or deploy anything automatically.

## Linux services

`install.sh` writes and manages the worker and gateway user units itself; there
is no separate unit template. Check them with:

```bash
systemctl --user status hyadex-server.service hyadex-server-gateway.service --no-pager -l
curl -H 'Authorization: Bearer replace-with-a-long-random-token' \
  http://127.0.0.1:7870/api/health
```

## Useful Configuration

Most settings are environment variables. New configurations should use
`HYADEX_*`. Historical `ZENITHBOT_*` and `ZENITHDOCK_AGENT_TOKEN` names are
accepted only as compatibility aliases so existing installations can migrate
without losing chat state:

| Variable | Purpose | Default |
|---|---|---|
| `HYADEX_STATE_DIR` | Persistent session/job/file state directory | `~/.hyadex` |
| `HYADEX_AGENT_CWD` | Default working directory for new sessions | user home |
| `HYADEX_AGENT_BIND` | Bind address | `0.0.0.0` |
| `HYADEX_AGENT_PORT` | Port | `7870` |
| `HYADEX_AGENT_TOKEN` | Shared bearer token | unset |
| `HYADEX_SERVER_INSTALL_DIR` | Versioned server runtime root | `~/.local/share/hyadex-server` |
| `HYADEX_BACKEND` | Default backend, `claude` or `codex` | `claude` |
| `CLAUDE_BIN` | Claude Code executable name/path | `claude` |
| `HYADEX_CLAUDE_TRANSPORT` | Interactive Claude transport: `auto`, `agent-sdk`, or `print` | `auto` |
| `HYADEX_CLAUDE_SDK_IDLE_TTL_SECONDS` | Idle per-chat SDK client retention | `300` |
| `HYADEX_CLAUDE_SDK_MAX_LOADED_CHATS` | Maximum retained per-chat SDK clients | `4` |
| `CODEX_BIN` | Codex executable name/path | `codex` |
| `HYADEX_RUNTIME_CATALOG_TIMEOUT_SECONDS` | Per-command CLI version/help/model probe timeout | `6` |
| `HYADEX_RUNTIME_DIAGNOSTIC_TTL_SECONDS` | Cache lifetime for safe CLI version/auth probes | `60` |
| `CLAUDE_PROJECTS_ROOT` | Claude history search root | `~/.claude/projects` |
| `CODEX_SESSIONS_ROOT` | Codex history search root | `~/.codex/sessions` |
| `HYADEX_JOB_MAX_ACTIVE_RUNS` | Scheduled-job concurrency cap (`0` disables this dedicated cap) | `0` |
| `HYADEX_MAX_ACTIVE_AGENT_RUNS` | Optional server-wide concurrency cap for chat, cron and goal runs (`0` means no count limit) | `0` |
| `HYADEX_JOB_MIN_AVAILABLE_MEM_MB` | Additional job launch memory guardrail; the normal turn floor also applies | `1536` |
| `HYADEX_MIN_START_AVAILABLE_MEM_MB` | Interactive launch memory guardrail | `2048` |
| `HYADEX_CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS` | Maximum time for each isolated Git worktree snapshot | `15` |

The `HYADEX_HANDOFF_DIGEST_*` and `HYADEX_WORKSPACE_TEXT_MAX_BYTES` settings
belonged to removed features and are ignored.

Runtime catalog refreshes share a 25-second CLI/network-probe budget so they
can finish before the desktop and mobile clients' 30-second request timeout.
Provider health checks run concurrently so a slow provider cannot prevent the
others from being checked.
Claude checks only its installation and CLI capabilities; it does not run an
authentication-status command. A timeout does not establish that a user is signed out.
If the refresh budget is exhausted, unfinished checks keep any prior diagnostic
with its original timestamp; providers without a prior result are reported as
unknown, not missing or unauthenticated. Model discovery uses its existing
fallbacks, and incomplete checks are not added to the diagnostic cache.

### Codex subagent concurrency

Server chat admission and a Codex thread's spawned-agent slots are separate.
HyadexServer does not inject a default subagent count. Codex's supported setting
is `agents.max_concurrent_threads_per_session`; `agents.max_threads` is its
legacy alias. Unset means **Codex chooses its default**, not unlimited. See the
[official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Native clients can read or change the server default with
`GET` / `PUT /api/admin/codex/subagents`. The PUT body is
`{"max_concurrent_threads_per_session": 12}`; use `null` to remove the override.
The field is required and accepts only a positive, losslessly represented JSON
integer (at most `9007199254740991`) or `null`, never strings, booleans, or zero.
Both endpoints require exactly one native token header and reject browser,
cookie, bearer, and URL-token credentials. PUT uses bounded JSON framing.

Responses include `configurable`, `scope: "server"`, the configured nullable
value, `provider_config_key`, and `applies_to: "new_or_reloaded_threads"`.
Legacy `exec` transport reports `configurable: false` with
`reason: "unsupported_transport"`; it cannot silently save an ineffective setting.

The choice persists in `admin/codex-settings.json` without replacing other
settings. It is passed as a real dotted-key override when native threads start,
resume unloaded, or fork. An already-loaded thread keeps its current limit until
Codex loads it again, for example after it is unloaded to make room for other
chats or after a server restart; the per-chat provider reload route has been
removed. No current turn, goal, child agent, or provider process is interrupted
by saving this setting. Explicit chat overrides take precedence. No polling or
background configuration refresh is added.

## Side Questions

Native clients can ask Codex or Claude a temporary side question using the
provider's conversation context, including completed tool results, without
changing the main turn, queue, goals or chat history. Codex uses an ephemeral
fork; Claude uses its native side-question control on the existing connection.
Saved settings for a later Claude turn do not reconfigure that connection when
asking a side question. See [Native side conversations](../docs/NATIVE_SIDE_CHAT.md)
for follow-ups, cancellation and context behavior.

## Chat Consults

A Work chat's agent can ask another chat in its project a question with the
`ask` helper (provider tool helper `ask`, CLI `hyadex/cli/ask.py`):
`ask list` names the chats it may ask, and `ask TITLE` (or chat id), with the
question on stdin or as `--question`, asks one. Servers advertise
`chat_consult_v1` (`modes`, `inbound`, `turn_limits`).

- **Basic** answers from the asked chat's existing context without tools:
  Claude's native `/btw` on that chat, or a Codex fork with its shell tool
  switched off.
- **Deep** (`--deep`) sends a read-only fork of the asked chat to investigate
  its folder: a Claude fork (`fork_session`) offered only Read, Grep and Glob,
  with no MCP servers and its transcript removed afterwards, or a Codex fork
  in a read-only sandbox that never asks for approval. Its steps (files read,
  searches, commands) are recorded.

Neither touches the asked chat's transcript, queue or turn admission, and no
answer can ask another chat in turn. Who may ask whom:

- The asking and asked chats share a project (`folder`), or, outside
  projects, a working directory. Archived chats and group members neither ask
  nor are asked.
- `consult_inbound` on the asked chat: `off`, `basic` or `deep` (default).
- A deep question needs the user's approval unless the asking chat sets
  `consult_deep_approval: false` or lists the asked chat in
  `consult_deep_allowed` ("always allow" on an approval). An approval waits
  10 minutes.
- Each turn may ask 5 basic and 2 deep questions; one deep fork runs per
  asked chat at a time. Basic answers time out after 2 minutes, deep after 5.
  The `ask` helper call may take up to 16.5 minutes, so the Codex MCP tool
  timeout is raised to match.

The ledger is `chat_consults.sqlite3` in the state directory (200 consults per
chat). A consult ends with its asking turn (`turn_finished`/`turn_stopped`) or
when the helper stops polling for 45 seconds; consults running at a restart
end as `interrupted`. Every change sends `chat_consult_updated` (an
invalidation frame) to both chats' sockets.

Routes: `POST /api/agent/sessions/{id}/consults`,
`GET /api/agent/sessions/{id}/consults/{consult_id}?wait=` and
`GET /api/agent/sessions/{id}/consult-targets` for the helper (the run's
provider capability, action `ask`); `GET /api/sessions/{id}/consults`
(`outgoing`, `incoming`), `POST .../consults` (the user asks this chat,
`mode` basic or deep), `GET .../consult-targets` and `POST .../ask`
(`chat`, `mode`, `question`: the user asks another chat from this one, as
with `@` in a client's composer; no approval or turn limit, the source
records `by_user`, and the asking chat's log gets a `chat_consult_asked`
event for its timeline, never seen by its agent),
`POST .../consults/{consult_id}/decision`
(`allow`, `always`, `deny`), `POST .../consults/{consult_id}/cancel` and
`GET /api/consults/active` for clients.

## Claude Agent SDK Transport

Interactive desktop clients can opt individual Claude chats into a persistent,
per-chat `ClaudeSDKClient`. This enables native steering plus approval and
question cards without sharing one Claude process across chats. The server
advertises the exact `claude_sdk_interactive_v1` capability before the desktop
app opts in.

Set `HYADEX_CLAUDE_TRANSPORT` to `auto` (default), `agent-sdk`, or `print`.
`print` always uses the compatible `claude -p` path. Clients that do not send
the exact capability—including older iOS builds and scheduled jobs—also remain
on `claude -p`, regardless of the server's interactive transport setting.

Idle SDK clients are retained for at most five minutes by default, with up to
four chat processes loaded. Active chats are never evicted to enforce the idle
limit.

In-app Claude MCP management and its `mcp_management` control feature have
been removed; MCP servers come from Claude Code's own configuration.
`HYADEX_CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS` (default `15`) keeps its historical
name and still bounds native SDK control requests such as the provider-command
inventory read and goal clearing.

## Backend CLI Notes

The backend selection in Hyadex only chooses which CLI the server invokes.
The model, effort, authentication, provider-side session storage, and available
commands still come from the installed CLI tools and their local configuration.

Recommended checks before connecting clients:

```bash
# Claude backend
command -v claude
claude --version

# Codex backend
command -v codex
codex --version

```

Run these as the same Unix user that owns the systemd service. If the CLI works
in your login shell but fails under systemd, check the service `PATH`, virtual
environment, and any provider-specific auth/config files.

### Runtime diagnostics

API contract v9 exposes privacy-safe runtime status in two places:

```text
GET /api/health
GET /api/runtime/catalog?refresh=true
```

The health response includes cached `runtimes` entries. The catalog endpoint's
`refresh=true` query forces a fresh runtime probe. Each backend
reports `ready`, `missing`, `unauthenticated`, `unknown`, or probe `error`, plus an
actionable recovery instruction. It never returns account identity, auth
output, or tokens.

HyadexServer has no in-app Codex sign-in management: the account and API-key
routes, the manual login reload and the `codex_auth_v1` capability have been
removed. Sign in on the server with `codex login`; new turns pick up a changed
file-backed login automatically. Catalog refresh can observe such a change, but
an unknown native login revision never retires a working Codex process. See
[Codex login handoff](CODEX_LOGIN_HANDOFF.md) for ownership and audit behavior.

Claude startup, automatic refresh, manual **Recheck CLIs**, and turn admission
never execute `claude auth status`. Short-lived auth-status commands can start
OAuth renewal and exit before persisting the replacement credential (see
[upstream report #95822](https://github.com/anthropics/claude-code/issues/95822)).
Authentication readiness is recorded from real native Claude requests. Before
the first request, an installed Claude reports `unknown` with `authenticated: null`;
the desktop client can still start a chat. Updated desktop clients keep this
passive authentication state in Settings rather than warning in the composer
before a send. A cached backend-wide login failure is not evidence that a
different chat has failed; the composer shows its own latest run error.
Installation checks retain
the last native authentication result and its original timestamp rather than
claiming to have checked the account again. A successful request clears the
previous authentication error. After an external `claude auth login`, retry the
message; rechecking the installation alone does not prove a successful login.
The former `HYADEX_CLAUDE_AUTH_PROBE_TIMEOUT_SECONDS` setting is no longer used.

This removes standalone authentication monitoring, not every native Claude
process started by the catalog. [Native model discovery](CLAUDE_MODEL_DISCOVERY.md)
still initializes disposable SDK processes using the native authentication
environment. Its behavior during token renewal and process termination has not
been validated by the passive-readiness tests. Do not treat a successful model
list as authentication evidence or this change as a guarantee against all
native login failures.

A new prompt performs installation preflight before reserving real agent work. If
the selected CLI is unavailable, the endpoint returns a structured
`503 runtime_unavailable` response. Failures after a healthy launch remain a
`last_error` on a ready runtime so model overloads, bad thread IDs, and ordinary
provider failures are not mislabeled as missing installations.
Claude permits a native retry when authentication is unknown or previously
failed, so an external login can take effect without a server restart. Missing
or broken executables still block admission. Other providers keep their own
existing readiness checks.

## Scheduled job outcomes

`last_run_at` records when a run was admitted. How the latest run ended is kept
on the job record as `last_run_status`:

- `running` while its turn is in progress;
- `completed`, `failed` or `stopped` once the turn ends;
- `dispatched` for a group job, whose run is a message handed to the members.

`failed` also covers a run that could not start, from the scheduler or
**Run now**. Alongside the status the record has `last_run_error` (the turn's
first error, or why it could not start), `last_run_id` and
`last_run_finished_at`. Jobs that have not run since this was added have no
`last_run_status`. The full history per run stays available from the timeline
(`GET .../jobs/{job_id}/runs` for agents).

## Chat token totals

Each chat record (list and detail) carries `token_totals` once a turn has
reported usage: `total_tokens`, `input_tokens` (including cached input),
`cached_input_tokens`, `output_tokens` and `updated_at`. These are the
providers' own counts summed over every turn: Claude's per-turn result usage
(input, cache creation and cache reads) and the growth of each Codex thread's
cumulative usage. Turns from before this field existed are not counted. Context
usage (`context_usage_snapshot`) is separate: it is how full the window is now.

## Agent-managed scheduled jobs

Every Claude and Codex turn receives a compact snapshot of up to 25 jobs
belonging to that chat. The snapshot contains only job ID, title, enabled
state, schedule, IANA timezone, and next-run time; job prompts are omitted.
The helper's `list` command returns the complete current set. Agents are
instructed to change jobs only after an explicit scheduling request.

The installed `hyadex/cli/jobs.py` helper is the authoritative interface from
an agent turn. Every live turn receives a private, run-scoped authority file
and exact helper command in its provider-authority block. The helper accepts
that file and the bound chat ID explicitly, then uses server-enforced
agent-only routes for `list`, `get`, `runs`, `create`, `update`, and `delete`.
It deliberately provides no run-now command. The main server bearer is never
inherited by a provider process. For example, using the literal authority path
and chat ID shown in the current turn:

```bash
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn list
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn get JOB_ID
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn runs JOB_ID --limit 20
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn create --title "Daily status" \
  --prompt "Summarize the current project status." \
  --interval-seconds 86400 --loop
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn create --title "Weekday status" \
  --prompt "Summarize the current project status." \
  --cron "0 9 * * MON-FRI" --timezone America/Los_Angeles
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn update JOB_ID \
  --rrule "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=0;BYSECOND=0" \
  --timezone Europe/London
"$HYADEX_JOBS_CLI" --authority-file /path/from/turn.json --chat-id sess_from_turn delete JOB_ID
```

API contract v13 includes the durable per-chat `provider_jobs_access` setting.
Its default `full` mode exposes the existing helper surface, `read_only`
permits only `list`, `get`, and `runs`, and `blocked` permits no agent Jobs
calls. Capability issuance records the mode at turn start and every agent Jobs
route also checks the live session setting, so a human can tighten access while
a provider turn is running. Restoring broader access beyond the turn's issued
ceiling takes effect on the next turn. Authenticated app/human Jobs endpoints
are unaffected. Clients can detect the contract through
`capabilities.provider_jobs_access_control_v1` in `/api/health`.

Cron accepts Vixie five-field expressions, aliases such as `@daily`, and
seconds-first six- or seven-field expressions (the seventh field is year).
Hashed `H` fields are stable per job; nondeterministic `R` fields are rejected.
RRULE accepts one RFC 5545 recurrence-rule property with an optional `RRULE:`
prefix. Explicit first-run timestamps run exactly once even when off-rule;
subsequent runs return to the calendar schedule. Missed occurrences are
skipped, retries do not move the canonical schedule, nonexistent DST times are
skipped, and ambiguous fall-back times run once.

Interval schedules are capped at ten years. RRULE `COUNT` is capped at 10,000,
and leap-second `BYSECOND=60` is rejected because the runtime clock cannot
represent second 60.

Agent-created jobs use the same job store as the desktop and mobile clients,
so they immediately appear in the Jobs panel. The helper calls the agent-only
routes; authenticated human clients use `/api/jobs` instead:

- `GET/POST /api/agent/sessions/{session_id}/jobs`
- `PATCH/DELETE /api/agent/sessions/{session_id}/jobs/{job_id}`
- `GET /api/agent/sessions/{session_id}/jobs/{job_id}/runs`

Jobs saved by older servers with `@Chat` or Team targets are paused for repair
(HTTP 409) when they fire, rather than running the prompt without its targets.
Replacing the prompt or sending an empty `chat_references` clears saved `@Chat`
targets. New and updated jobs accept `chat_references` (and the helper's
`chat_routes`) only as an empty list.

## Removed cross-chat messaging

Agent-to-agent messaging between ordinary chats has been removed: handoffs,
request/reply exchanges and their legs, live waits, async routes, `@Chat`
references and route grants, the chat mailbox and its wakes, handoff digests,
and the `chats` helper. Their routes, chat search, the inbox and route
management endpoints, and the `cross_chat_handoffs_v1` capability are no longer
registered. Use [group discussions](GROUP_DISCUSSIONS.md) or
[Autopilot](AUTOPILOT.md) for multi-agent work, and [chat consults](#chat-consults)
to ask another chat a question. The per-run provider authority used by the
jobs, publish, emergency and notify helpers is unchanged.

Saved data from older servers is handled deterministically:

- Queued cross-chat deliveries, mailbox wakes and queued messages carrying
  `@Chat` routing are discarded by queue recovery or promotion with a durable
  `turn_unqueued` event; editing such a row returns 400.
- `chat_references` on turns and queue edits accepts only an empty list.
- `cross_chat.sqlite3`, `handoff_digest_jobs.json` and the route keys in
  `sessions.json` stay on disk and are not read.
- Old cross-chat timeline events remain hidden, as before.

This removal raised the API contract to 30.

## Working-directory completion

Hyadex can complete New Chat working-directory paths against the active
HyadexServer host (including remote hosts) when
`capabilities.working_directory_completion` is advertised:

```text
GET /api/working-directories/complete?path=/srv/pro&limit=24
```

The authenticated endpoint performs one bounded, shallow scan and returns
directories only. Older clients and servers continue to use the ordinary path
field without a global API compatibility failure.

## Chat files without workspace browsing

The chat-scoped workspace explorer and editor, its Git status, diff, conflict
and action endpoints, and the `workspace_files` capability have been removed;
the `HYADEX_WORKSPACE_TEXT_MAX_BYTES` setting is ignored. Agents still work in
the chat's `cwd`. Clients show their results through
[per-turn code review](#per-turn-code-review), the chat's registered files
(`GET`/`POST /api/sessions/{session_id}/files` and
`GET /api/sessions/{session_id}/files/{file_id}`), and
`GET /api/sessions/{session_id}/links/file?target=...`, which resolves a file
link in a message to exactly one of that chat's registered files.

`GET /api/sessions/{session_id}/workspace/file?path=...` returns a read-only
copy of one file the chat works on, so a client on another machine can open a
changed file. `path` is absolute, or relative to the Git work tree containing
the chat's folder (as a turn's diff names it). Only regular files inside the
chat's folder or that work tree are served, after resolving symlinks (403
otherwise), up to 50 MB. There is still no listing, editing or Git control.

## Resuming and importing provider history

There is no server-side list of local Claude or Codex sessions and no bulk or
manual history import. To continue an existing native conversation, create a
chat with its provider ID (`provider_session_id`, `claude_session_id` or
`codex_thread_id`) on `POST /api/sessions`. Its history is imported unless
`import_history` is `false`. A conversation already used by another installed
HyadexServer instance on this machine is rejected with 409.

Opening a chat catches its timeline up with messages added to the provider
transcript outside Hyadex. The sync anchors on the newest timeline
messages, skips the parse when the transcript file is unchanged, refuses to
import a transcript that matches nothing on a populated timeline, and closes
every import batch with a `turn_finished` marked `imported: true`. Imported
turns never set a chat's active run, so replayed history can never make a
stopped chat look busy.

## Whole-History Search

`GET /api/search?q=<query>&limit=<chat-count>` searches user, assistant, error,
job, reasoning-summary, and file text across every chat. Quoted phrases remain
phrases; unquoted terms use prefix matching for responsive type-ahead search.

The first request incrementally builds `history_search.sqlite3` inside the
agent state directory. Each transcript stores its indexed byte offset, so later
requests ingest only newly appended JSONL records. The index is persistent and
safe across server restarts; a replaced or truncated transcript is rebuilt
automatically. Indexing runs in a worker thread and does not block agent turns.

## Per-Turn Code Review

For Git worktrees, the server snapshots the repository immediately before and
after each agent turn through an isolated temporary index. This captures the
complete turn-specific textual patch without modifying the user's real index
or folding pre-existing dirty changes into the review.

The append-only timeline stores only a compact `code_diff` event with file and
line-count metadata. Clients fetch the complete patch on demand:

```text
GET /api/sessions/{session_id}/diffs/{run_id}
```

The endpoint uses the same bearer-token authentication as the rest of the API.
Binary changes remain compact Git binary markers rather than being copied into
the event log.

## Agent notification levels

An authorized live provider turn can call the `notify` helper with
`send --level normal|important|emergency --message TEXT`. The helper binds the
request to that exact server, chat, and run. The agent chooses the level;
clients do not infer it from message wording. Normal and important notices
become durable `agent_notification` timeline events. Emergency uses the
existing acknowledged emergency-alert lifecycle. All three advance the
session's unread cursor, and `unread_notification_level` in session summaries
retains the highest level until the chat is marked read. The contract is
advertised as `capabilities.agent_notification_levels_v1`.

System notifications are still scheduled by connected clients. This helper
does not provide APNs or FCM delivery to a terminated mobile app.

## Dismissing emergency alerts

`POST /api/sessions/{session_id}/emergency/acknowledge` accepts exactly one
of these fields:

- `expected_alert_id`: acknowledge that one alert (existing behavior).
- `through_alert_id`: acknowledge every still-active alert raised through
  that alert's durable position, including the boundary alert itself.

Clients discover the batch option through
`capabilities.agent_emergency_alerts_v1.acknowledge_through_alert_id`. Capture
the displayed alert ID when the user clicks; alerts raised after that boundary
remain active. The boundary remains valid after another device acknowledges
it, and repeating the request is harmless. An unknown boundary returns 409;
a missing chat returns 404; both fields or neither field returns 422.

Batch responses contain `session`, `acknowledged`, and `acknowledged_count`.
The server persists ordinary acknowledgement events together and broadcasts
the resulting summary. Reopening the chat or connecting another device sees
the same result. Dismissal retains alert history and does not stop running work.

## Public API Sketch

The server exposes JSON endpoints under `/api`; FastAPI serves the complete
schema at `/openapi.json`. Paths below that begin with `.../` are relative to
`/api/sessions/{session_id}`.

- Health and runtime: `GET /api/health`, `GET /api/runtime/catalog`,
  `GET /api/runtime/usage?backend=codex&session_id=<chat>`,
  `GET /api/connection-addresses`
- Chats: `GET`/`POST /api/sessions`, `GET`/`PATCH`/`DELETE /api/sessions/{session_id}`,
  `POST .../turns`, `POST .../stop`, `POST .../fork`, `POST .../read`,
  `POST .../unread`, `GET .../timeline-index`, `GET .../search`,
  `GET .../subagents`, `GET .../provider-commands`, `GET .../runs/{run_id}/trace`
- Live events: `WS .../events?after=<seq>`, `WS /api/emergency-alerts/events`
- Queue: `PATCH`/`DELETE .../queue/{queued_id}`, `POST .../queue/{queued_id}/move`,
  `POST .../queue/{queued_id}/run-now`, `POST .../queue/resume`
- Files and diffs: `GET`/`POST .../files`, `GET .../files/{file_id}`,
  `GET .../links/file`, `GET .../workspace/file?path=…`, `GET .../diffs/{run_id}`
- Claude controls: `GET .../claude/runtime`, `PUT`/`DELETE .../claude/goal`,
  `POST .../claude/context-usage/refresh`,
  `POST .../claude/interactions/{interaction_id}/resolve`
- Codex controls: `GET .../codex/runtime`, `GET`/`PUT`/`DELETE .../codex/goal`,
  `POST .../codex/compact`, `POST .../codex/interactions/{interaction_id}/resolve`
- Side questions: `GET`/`POST .../side-chat`,
  `DELETE .../side-chat/requests/{request_id}`, `DELETE .../side-chat/{side_chat_id}`,
  `POST .../side-questions`, `DELETE .../side-questions/{request_id}`
- Chat consults: `GET`/`POST .../consults`, `GET .../consult-targets`, `POST .../ask`,
  `POST .../consults/{consult_id}/decision`,
  `POST .../consults/{consult_id}/cancel`, `GET /api/consults/active`
- Terminal and tmux: `GET`/`DELETE .../terminal`, `POST .../terminal/open`,
  `POST .../terminal/input`, `POST .../terminal/resize`, `POST .../terminal/action`,
  `GET .../terminal/windows`, `WS .../terminal/ws`, `GET .../tmux`,
  `GET .../tmux/capture`
- Emergency alerts: `POST .../emergency/acknowledge`
- Jobs: `GET`/`POST /api/jobs`, `PATCH`/`DELETE /api/jobs/{job_id}`,
  `POST /api/jobs/{job_id}/run`
- Search and paths: `GET /api/search`, `GET /api/working-directories/complete`
- Groups and Autopilot: `/api/groups...` and `/api/autopilot/runs...`; see
  [group discussions](GROUP_DISCUSSIONS.md) and [Autopilot](AUTOPILOT.md)
- Administration: `GET`/`PUT /api/admin/codex/goals`,
  `GET`/`PUT /api/admin/codex/subagents`, `GET`/`POST /api/admin/restart`
- Agent helpers, authorized by a live run's provider authority rather than the
  client token: `/api/agent/sessions/{session_id}/jobs...`,
  `POST /api/agent/sessions/{session_id}/artifacts`,
  `POST /api/agent/sessions/{session_id}/emergency-alerts`,
  `POST /api/agent/sessions/{session_id}/notifications` and
  `POST /api/agent/provider-tools/mcp`

Endpoints that no client used have been removed together with their health
capabilities: local CLI session listing and bulk import, manual history import,
maintenance and duplicate pruning, timeline pins (`pinned_items`), port tunnels
(`port_forwarding_v1`), process listings, host diagnostics, the Codex load,
rotate, rollback, review, shell-command, background-terminal and
permission-profile controls, Claude MCP management, provider reload, empty-chat
dismissal and sidebar ordering, the session-scoped copies of the jobs routes
(use `/api/jobs` or the agent routes), and the unscoped and per-event file
lookups (use `.../files/{file_id}`). `local_session_import_v1`,
`workspace_files`, `codex_auth_v1`, `codex_provider_v1`,
`provider_connections_v1` and `cross_chat_handoffs_v1` are no longer
advertised. `codex_controls` and `claude_controls` no longer list the removed
controls, and `scheduled_jobs` no longer lists chat-reference or mention
features.

The event stream is append-only JSONL on disk. `GET /api/sessions/{session_id}`
pages it (`after`, `before`, `limit`), and the events WebSocket replays from
`after` before streaming live events. Large clients should page history
instead of loading every event at once.

Servers advertising `event_stream_liveness_v1` (available, version 1) support
`liveness=true` on `/api/sessions/{session_id}/events`. After complete catch-up
and live registration, the socket sends
`{"type":"event_stream_status","version":1,"session_id":"…","phase":"ready"}`.
Idle sockets then receive the same control shape with `phase:"heartbeat"`
every 15 seconds. These opt-in frames are ephemeral: they have no event `seq`,
never change history, and do not invoke an agent or scan its transcript.
Clients must not advance their durable cursor from a control frame. A quiet
model is not a broken connection. Desktop uses a 45-second no-progress deadline
and resumes after its last successfully accepted event; it never resends a turn
to repair a disconnected stream. Older clients and servers retain their existing
protocol, without imposing heartbeat expectations on servers lacking this capability.

Two more opt-in lanes on the same socket carry output while the provider is
still writing it. Neither has an event `seq` or changes history.

- `reasoning_stream=true` (and `reasoning_text=true` for provider plaintext)
  sends `reasoning_summary_stream` snapshots of thinking in progress: Codex
  reasoning, and Claude's thinking from partial SDK messages.
- `live_text_stream_v1` servers accept `text_stream=true` and send
  `assistant_text_stream` snapshots,
  `{"type":"assistant_text_stream","revision":…,"items":[{"run_id","item_id","text","after_seq","backend",…}]}`:
  every answer still being written (a Codex agent message, a Claude text
  block). `after_seq` is the last durable event when it began.

Each snapshot replaces the previous one, and a socket receives the current one
right after catch-up. The finished answer arrives as the durable
`assistant_text` (Codex commentary as `reasoning_summary`), carrying the same
`item_id` for Codex; its live item then disappears from the next snapshot. A run
that ends early (Stop, an interrupt, a failure) keeps the text shown so far as
an `assistant_text` with `partial: true` and `text_after_seq`; after a normal
completion an unfinished answer stays transient. Unfinished thinking is kept as
a `reasoning_summary` with `partial: true`, as Codex reasoning already was.

The subagents endpoint folds Claude local-agent lifecycle records into bounded
`subagent_state` snapshots without returning provider prompts, raw events,
tool-result output, commands, or output-file paths.

A Claude subagent's own `assistant_text`, `reasoning_summary`, `tool_started`
and `tool_finished` events carry `parent_tool_id`: the id of the Agent/Task
tool call that runs it. Clients fold them under that tool, as Claude Code
does; its text is not the main agent's answer. Session lists report `running`
(true from a turn's `turn_started` until its `turn_finished` or
`turn_stopped`), so a client need not infer it from the latest event type.

### Synchronized side conversations

Native authenticated clients with the `side_questions.sync` capability use
`GET /api/sessions/{session_id}/side-chat` for saved history and `POST` on the
same path to submit an answer request. Acceptance returns immediately; the server
owns the answer even if the requesting app closes. Socket notifications invalidate
history after changes, and reconnecting reads authoritative state without polling.

`DELETE /api/sessions/{session_id}/side-chat/requests/{request_id}` stops that
request. `DELETE /api/sessions/{session_id}/side-chat/{side_chat_id}` clears the
side conversation. Devices using the same native server credential and main chat
share the history. Side content never becomes main-chat transcript content.
Existing transient side-question routes remain available to older clients.

### Provider account usage

Authenticated native clients can read provider-reported allowance separately
from a chat's context usage. Codex uses its existing app-server account read;
subsequent reads use the observed snapshot, and `refresh=true` requests a fresh
native snapshot. Native rate-limit events update it without polling. API-key
accounts do not expose a ChatGPT allowance.

Claude answers from Claude Code's own `/usage` data: a short-lived Agent SDK
connection sends the native `get_usage` control request without a prompt,
settings, hooks or the local transcript scan, and only plan windows are kept,
titled as Claude Code titles them. Answers are cached for two minutes and
refreshed after a native rate-limit event; windows observed through rate-limit
events are the fallback. Missing percentages stay unknown; no model request is
made to obtain usage. Responses include observation times, reset times when reported, and
Codex credit balance when supplied. Credits have no inferred currency. Caches
are scoped to the provider process and invalidated when its account or
generation changes. A `provider_usage_changed` socket notification tells the
native client to reread; account data never becomes a transcript event.

### Native provider commands

Authenticated clients can call
`GET /api/sessions/{session_id}/provider-commands?refresh=false` to retrieve a
bounded, sanitized inventory for that chat's backend and working directory.
Codex returns native skills from app-server `skills/list`; Claude returns the
commands reported by the Claude Agent SDK. Retired providers report unsupported
metadata and an empty inventory.

Clients select an entry by sending only its opaque `id` and inventory
`revision` in the optional turn field `skill_selection`. They never receive or
submit a provider filesystem path. HyadexServer re-discovers and revalidates
the selection against the session, backend, working directory, and current
provider inventory immediately before execution. A valid Codex selection uses
Codex's structured skill input, while a valid Claude selection invokes the
provider-reported command through the SDK. Unknown or unselected leading-slash
text remains an ordinary literal message. Its content is sent unchanged;
Claude receives a native per-message transport flag instead of an added
instruction. Selected commands and ordinary `@file` input keep their native
behavior, and normal approval policy is unchanged.

Opaque IDs and revisions are scoped to the current HyadexServer process so
native filesystem paths cannot be inferred from them. After a server restart,
clients must fetch a fresh inventory. A previously queued selection is
visibly removed as stale during promotion, and later queue entries continue in
FIFO order.

## Repository Hygiene

Before publishing:

```bash
python3 -m compileall -q agent_server.py execution_service.py execution_uninstall.py server_instances.py hyadex
rg -n 'private-host|/home/<name>|/Users/<name>|token-value' .
```

Do not commit:

- `~/.hyadex` state (and the legacy `~/.zenithbot-agent` compatibility link)
- uploads or generated artifacts
- `.env` files or access tokens
- machine-specific hostnames, IP addresses, or user home paths
- compiled Python caches


### Empty-chat cleanup

The desktop empty-chat dismissal and discard routes, and the sidebar ordering
route, have been removed. Dismissals recorded by older desktops still hide the
unchanged empty row when `GET /api/sessions` receives the same
`placeholder_client_id`; a later content or settings change makes it visible
again. Deleting a chat uses `DELETE /api/sessions/{session_id}`.

### Chat readiness after restart

`GET /api/health` keeps transport health (`ok`) available for diagnostics and
recovery controls. `chat_readiness` separately reports `ready`, `recovering`, or
`failed`, with a boolean `ready` and a bounded, non-private message. Queue
restoration must succeed before new turns can be admitted. A failed restoration
remains closed to protect durable message ordering; restarting the managed
server retries the restoration. Repeated failures require server log diagnosis,
not repeated message submissions. A completed recovery task's own timeout is a
failure, not a still-running scan.

Emergency acknowledgement reads lifecycle metadata without retaining unrelated
message/media bodies, and scans outside the global session-store lock. Exact
alert IDs, durable acknowledgement receipts and cancellation-safe projection
remain unchanged.

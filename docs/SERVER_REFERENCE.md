# Hyadex server reference

**HyadexServer is the self-hosted execution backend for
[Hyadex](https://github.com/ezoiHY/Hyadex).** Hyadex provides
the polished desktop and mobile chat experience; HyadexServer runs on the
machine that owns your workspaces, Claude Code installation, and Codex CLI.
Together they provide persistent agent chats without routing private project
files through a third-party chat service.

HyadexServer exposes an authenticated HTTP/WebSocket API and streams normalized
Claude/Codex events, files, videos, uploads, scheduled jobs, process inspection,
and persistent tmux terminals to Hyadex clients.

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
- Supports queued turns, stop requests, chat forking, context digests, and rough
  history import from provider sessions.
- Recovers oversized Codex provider threads by rolling the same chat onto a
  fresh thread with bounded recent memory when remote compaction fails.
- Creates handoff digests with an actual LLM summarizer; the raw transcript/file
  pack is only internal source material.
- Runs recurring/loop jobs per chat with host load/memory guardrails.
  Archiving a chat pauses every interval, cron, and RRULE job attached to it;
  unarchiving leaves those jobs paused until the user explicitly enables them.
- Gives each live provider turn a private, chat-scoped Jobs/Publish authority
  and rotates it with the logical run during native steering. Failed steering
  restores the predecessor only when rejection is proven safe; stopped,
  uncertain, and restarted runs fail closed.
- Provides optional live process and tmux-pane inspection for active work.
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

Hyadex provides same-server chat routes and the local chat mailbox. Team Hub,
Team Network mail, shared team skills, and secure-peer server pairing have been
removed. These routes and capabilities are no longer advertised. Old saved
Team references are rejected or paused for repair rather than executed as plain
prompts; local `@Chat` references retain their existing authorization contract.
Existing Team Hub and secure-peer state is not deleted by running this version.

Public snapshot and interactive browser sharing are also removed. Old viewer,
media and sharing-management URLs no longer resolve; sharing cookies and tokens
do not authorize native APIs. Existing sharing databases, registered files and
chat history remain on disk. Previously accepted turns retain their attribution;
new turn requests carrying sharing attribution are rejected with 422.
Native side questions, file access and same-server chat routes remain available.

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
modules remain temporarily for their remaining recovery/uninstall dependencies;
the server does not launch update recovery on startup.

The public API contract is now 29. Old update health fields and schedule-bound
restart fields are absent; the remaining restart audit reports execution-worker
maintenance. This retirement does not publish or install a new release.

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
| `HYADEX_HANDOFF_DIGEST_BACKEND` | LLM backend for context digests, `claude` or `codex` | `claude` |
| `HYADEX_HANDOFF_DIGEST_MODEL` | LLM model for context digests | `sonnet` |
| `HYADEX_HANDOFF_DIGEST_EFFORT` | Optional digest reasoning/effort setting | unset |
| `HYADEX_HANDOFF_DIGEST_TIMEOUT_SECONDS` | Digest summarizer timeout | `180` |
| `HYADEX_HANDOFF_DIGEST_CHARS` | Final digest character cap | `56000` |
| `HYADEX_CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS` | Maximum time for each isolated Git worktree snapshot | `120` |

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
resume unloaded, or fork. Already-loaded chats need **Reload provider** while
idle; no current turn, goal, child agent, or provider process is interrupted by
saving this setting. Explicit chat overrides take precedence. No polling or
background configuration refresh is added.

## Side Questions

Native clients can ask Codex or Claude a temporary side question using the
provider's conversation context, including completed tool results, without
changing the main turn, queue, goals or chat history. Codex uses an ephemeral
fork; Claude uses its native side-question control on the existing connection.
Saved settings for a later Claude turn do not reconfigure that connection when
asking a side question. See [Native side conversations](../docs/NATIVE_SIDE_CHAT.md)
for follow-ups, cancellation and context behavior.

## Context Digests

`POST /api/sessions/{session_id}/digest` creates a real LLM-summarized handoff
for another chat. The server first builds a bounded source packet from recent
events and files, then asks the configured digest backend to summarize it into
a clean Markdown handoff. If the LLM summarizer fails, the endpoint fails
visibly instead of returning the raw source packet as if it were a digest.

By default, the digest summarizer uses Claude Sonnet:

```bash
HYADEX_HANDOFF_DIGEST_BACKEND=claude
HYADEX_HANDOFF_DIGEST_MODEL=sonnet
```

You can switch it to Codex or another installed CLI model, but the relevant CLI
must already be authenticated for the same Unix user that runs the service.

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

Claude-controls capability v3 adds authenticated MCP management without
changing global API contract v13. Clients gate on
`capabilities.claude_controls.features.mcp_management` and use the additive
v1 endpoints:

```text
GET  /api/sessions/{session_id}/claude/mcp
POST /api/sessions/{session_id}/claude/mcp
```

GET lazily connects an idle chat's Agent SDK client and returns an opaque
generation string plus at most 100 exact-name-deduplicated, sorted, allowlisted
server rows; `truncated` marks an incomplete list. POST requires that exact
generation and supports `reconnect`, `reconnect_all`, `enable`, and `disable`.
Both operations reject active/provider-starting turns, are lifecycle-serialized
with worker-maintenance and restart admission, and have a bounded native-control timeout.
Responses never expose MCP commands, environment, headers, configuration URLs,
raw provider errors, or tool metadata. Print transport and SDK-unavailable
hosts return an explicit unavailable snapshot; older servers continue to
return 404. Tune the default 15-second bound with
`HYADEX_CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS`.

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

Catalog refresh is not a request to reload Codex sign-in. An unknown native
login revision must not retire a working Codex process. Explicit sign-in reload
uses the native-admin `POST /api/admin/codex/auth/reload` with `{}`; account GET
advertises `login_reload_available` and the POST reports `login_reload_pending`.
See [Codex login handoff](CODEX_LOGIN_HANDOFF.md) for ownership and audit behavior.

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

## Agent-managed scheduled jobs

Every Claude and Codex turn receives a compact snapshot of up to 25 jobs
belonging to that chat. The snapshot contains only job ID, title, enabled
state, schedule, IANA timezone, and next-run time; job prompts are omitted.
The helper's `list` command returns the complete current set. Agents are
instructed to change jobs only after an explicit scheduling request.

The installed `hyadex_jobs.py` helper is the authoritative interface from
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

## Cross-chat handoffs

### Permanent chat pairs and independent messages (API contract 28)

An accepted explicit structured `@Chat` grants the exact two chats permanent
bidirectional permission. Each later run receives fresh, live-run-bound
credentials and discovers its permitted routes with `chats list` when needed.
The permission persists without another mention; it does not extend to other
chats, forks, or a recipient's other routes.

Clients negotiate `chat_conversation_async_route_v1` using the additive health
capability. For paired routes advertising `mode: async_route_v1`, `send` and
`ask` each send one independent message and return after acceptance. An idle
recipient starts a normal turn; a busy recipient receives a normal queued
message. `respond-current` explicitly sends another message on the exact
reverse pair. Ordinary final answers are never forwarded automatically, and
there is no reply obligation, exchange leg budget, or one-use route permission.
Saved routes and configured-route messages have no count or hourly quota;
message-size bounds remain. Provider discovery uses bounded cursor pages, not
a permission limit. The existing per-message reference input bound and legacy
exchange budgets remain separate from permanent pair permission.

The desktop receives `chat_conversation_message_*` lifecycle events keyed by
the message envelope, shows the sender's card at acceptance, and shows an
incoming queued item until recipient execution starts. Cancel and revoke use
the existing durable delivery ledger and final admission fence. See
[the async route contract](../docs/ASYNC_CHAT_ROUTES.md) for negotiation, event
fields, compatibility, and isolated validation.

### Legacy route-hint exchange contract (capability v13)

An inline structured `@Chat` is an optional target hint. It never forwards the
raw user prompt. On successful ordinary-turn admission, an exact local
single-`@Chat` reference authored by a v2 client with `grant_intent: true`
idempotently creates or refreshes a durable directional source-to-target
grant. Subsequent turns receive only that source chat's current grants; there
is no ambient all-chat authority. An explicit user request to send, ask, tell,
or contact a named chat requires the agent to use the matching helper; passive
mentions remain optional. Every accepted configured-route Send carries one optional terminal
reply path back to its immutable source; it creates no reply obligation, never
automatically relays the target's ordinary final answer, cannot request a
follow-up, and grants no durable reverse route. Ask explicitly requests one
terminal answer over the same exchange-scoped return mechanism. It commits
immediately as a durable two-leg agent message and keeps the source provider
turn attached until that answer arrives or the exchange is explicitly stopped.
The target delivery remains a normal durable turn and waits in its FIFO when
that chat is busy. Each helper invocation observes one bounded HTTP heartbeat
slice and returns either the answer or a resumable `pending=true` receipt. The
provider immediately runs a new foreground `wait` call with the same exact
lease after every pending receipt. Those slices stay below provider shell-tool
caps; they never convert the exchange into a later source-chat turn or impose a
semantic response deadline.

`pending=true` is returned only when the server confirms it is still waiting.
A lost connection instead produces an `ok=false`, `transport_error=true`,
`retryable=true` receipt with the same exchange, inbound leg, and lease; the
CLI prints that receipt and exits nonzero. Retry that exact `wait`, never the
original Ask: the answer may already be saved. Completed-answer delivery does
not wait indefinitely for disconnect-watcher cleanup.

Active agent messaging is strictly same-server. Cross-server inbox messaging,
Team Hub and secure-peer transport are no longer available.

`/chat` is a composer alias for selecting the same structured hint.

Scheduled runs never inherit the source chat's grants. Each job stores its own
exact route selection, authorized by route ID in the job editor/helper flow,
and revalidates its target, revision, and action on every firing. Its prompt
contains the corresponding exact single `@Chat` marker for display and
binding; no `@@` authoring syntax is required. The health surface advertises
cross-chat version 13, `durable_route_grants`, configured-route
`instruction_reply_once`, `configured_route_live_request_reply`,
`configured_route_request_reply_default: live`,
`live_wait_timeout_async_fallback: false`,
`live_wait_restart_async_fallback: true`,
`agent_ambient_local_handoffs: false`,
scheduled Jobs version 5, and global API contract 27. Capability v13 also
advertises the bounded transport heartbeat, no semantic live-wait deadline,
and the explicit asynchronous source-chat recovery used only when a server
restart loses the process-local provider call.

Capability v10 retains this reply-once behavior for existing
configured `instruction` grants as well as newly created ones. The return path
is a property of each accepted delivery, not a new durable target grant: it is
bound to the original source, delivery run, exchange generation, two-leg
budget, and expiry. Revoking or revising the source route still blocks future
deliveries immediately.

Beta-era local `direct_message` and `@@` references remain readable only for
safe migration/recovery. They are quarantined from ordinary authority and can
never mint a durable grant; queued v6 snapshots are narrowed against current
persisted grants and cannot recreate a revoked route. Nonterminal legacy UI
envelopes are failed/cancelled during upgrade before they can be resubmitted.
Existing same-server configured-route Send/Ask effects and final-result
obligations retain their exact authorization and lifecycle checks.

For a current ordinary turn, the provider-authority block exposes only opaque
route IDs. The agent lists the available routes, then sends or asks when the
user explicitly requests contact (and may otherwise decide no contact is
warranted):

```bash
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json list
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  send --route route_opaque --message "Verify the API contract."
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --route route_opaque --message "Which rollout is blocked?"
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  wait --exchange exchange_exact --inbound-leg leg_exact --lease lease_exact
```

The helper never exposes or accepts an inferred chat ID. It accepts only the
opaque IDs in that run's authority snapshot, uses loopback HyadexServer URLs,
disables redirects and proxies, and submits one bounded agent-authored
message. Send creates a correlated two-leg exchange with one optional terminal
reply capability available only to its exact delivery run. If the target does
not deliberately use that capability, its ordinary final stays local and the
exchange closes without sending anything back. Ask creates the same bounded
two-leg exchange and, by default, keeps the provider turn attached to its
exact live lease through bounded foreground `wait` slices until the answer or
a terminal failure. A pending receipt requires another exact `wait` call; it
is not a timeout or permission to finish the turn. A live-request exchange
does not expire merely because its original 24-hour authorization window
elapsed.
Explicit `--async-response` and restart recovery deliver the eventual result
in a later source-chat turn.

### Historical action-specific grants (v1-v2)

API contract v11 added durable, same-server handoffs selected by the user in
the Hyadex composer. API contract v13 upgraded
`capabilities.cross_chat_handoffs_v1` to version 2 and added bounded
request/reply exchanges. These historical structured references authorized
one exact exchange, instruction, or automatic final-result delivery. They are
retained for readable stored records and secure action-specific compatibility;
they are not the current `@Chat` authoring model. Self, archived, deleted,
legacy-transport, and unpaired foreign-server targets fail closed.

When such an action-specific grant is replayed, the provider-authority block
may print a one-use opaque target handle:

```bash
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  send --target OPAQUE_HANDLE --message "Verify the API contract."
```

`OPAQUE_HANDLE` is capability data, never a session ID. Delivery is a normal
durable target turn using the target chat's own provider, permission policy,
queue, and timeline. A server restart reconciles the ledger and lifecycle
outbox without silently duplicating a handoff. The desktop bearer remains
required to inspect or cancel handoffs.

Request/reply grants use the exact exchange ID reserved when the source turn
is admitted. The source agent starts it with `ask`; a recipient may answer or
ask a clarification with the exact `respond` command printed in that delivery
turn's provider-authority block:

```bash
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --target OPAQUE_HANDLE --message "Which rollout is blocked?"
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  respond --exchange exchange_exact --inbound-leg leg_exact \
  --message "Do you mean the desktop or server rollout?" --request-response
```

An exchange is limited to six directed conversational legs (three rounds).
Asynchronous exchanges expire after 72 hours. An attached same-server live
request does not time-expire while its exact source provider run remains its
owner; explicit cancellation, Stop, participant deletion/archive, or a server
restart closes or recovers it. A successful non-empty recipient final automatically
returns one terminal answer when the inbound leg expects a reply and no
explicit response won the one-use CAS.
Failures, stops, expiry, participant deletion/archive, and queue-owner loss are
durable visible outcomes; non-user failures also create one bounded native
status wake for the waiting sender. Exchange turns reuse the existing hidden
`cross_chat_handoff_delivery` purpose so older clients do not expose synthetic
prompts or queue controls.

### Durable directional route management (v8)

The default-empty per-source-chat grant list is the sole ordinary cross-chat
authority ceiling. Inline `@Chat` admission manages it automatically, while
these authenticated endpoints support inspection and explicit administration:

```text
GET    /api/sessions/{source_session_id}/agent-handoff-routes
POST   /api/sessions/{source_session_id}/agent-handoff-routes
PATCH  /api/sessions/{source_session_id}/agent-handoff-routes/{route_id}
DELETE /api/sessions/{source_session_id}/agent-handoff-routes/{route_id}?expected_revision={revision}
```

`PATCH` and `DELETE` use the route revision as a compare-and-swap precondition;
a stale or missing valid route returns HTTP 409 with
`code: route_revision_conflict`, a safe message, and the current route or
`null`. A chat can hold at most 16 routes, aliases and targets are unique,
routes cannot point to their source chat, and forks inherit neither routes nor
their private mutation journal. Only v2 `agent_cross_chat_routes_v2`
submissions carrying exact `grant_intent` provenance may persist an inline
grant. Older v1/v6 turns and recovered queue hints remain one-run legacy data
and never become durable policy.

Grant mutation and turn admission cross two durable files. HyadexServer first
stores a hidden pending route journal, then fsyncs the exact `turn_started` or
`turn_queued` admission ID, and only then exposes the grant. Startup rolls the
exact route revisions back when no matching event exists, or finalizes them
when it does; later edits and revocations are never recreated. Scheduled,
internal, digest, standalone, and cross-chat delivery turns receive no source
grant snapshot. Every helper call intersects its issued snapshot with the
exact live route revision and allowed actions, so removal or policy edits
block future acceptance immediately; an already accepted ledger item remains
visible and cancelable through authenticated desktop APIs.

The turn-scoped helper surface accepts only opaque issued route IDs:

```bash
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json list
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  send --route route_opaque --message "Apply the corresponding mobile change."
"$HYADEX_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --route route_opaque --message "Which mobile behavior must match?"
```

`list` is capability-scoped; there is no provider chat search or arbitrary
target parameter. `Ask` is not transcript access: it creates a normal target
turn containing only the bounded relayed message, then keeps the source
provider turn on the exact live lease through separate foreground `wait`
slices until one terminal answer arrives. `--async-response`
is the explicit compatibility mode that returns immediately and later queues
the answer in the source chat. Configured-route Send and Ask are limited to two
legs; non-live exchanges expire after 24 hours, while an attached live Ask is
owned by its exact source run instead of a wall-clock deadline. A Send reply is
always terminal; Ask also has no follow-up under the configured-route contract.
Route bodies and answers are
limited to 16,000 characters and 64 KiB UTF-8. A live run can accept at most
one effect per route and four route handoffs total; durable source and target
limits are 12 accepted route effects per rolling hour.

Provider projections contain only the opaque route ID, safe alias, sanitized
bounded title, backend, allowed actions, and generic availability. They never
expose target/session IDs, folders, working directories, models, provider IDs,
transcripts, rate counts, or route mutation history. Configured delivery
prompts likewise omit route and ledger identifiers; a returning answer may
include only the target's sanitized, explicitly untrusted display label.

Agent-created jobs use the same job store as the desktop and mobile clients,
so they immediately appear in the Jobs panel. The scoped helper routes are:

- `GET/POST /api/sessions/{session_id}/jobs`
- `PATCH/DELETE /api/sessions/{session_id}/jobs/{job_id}`

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

## Workspace Files

The optional `workspace_files` health capability exposes a chat-scoped text
workspace rooted at that chat's exact `cwd`. It is additive to API contract v9,
so older clients continue to work without a global compatibility failure.

```text
GET /api/sessions/{session_id}/workspace
GET /api/sessions/{session_id}/workspace/entries?path=&offset=0&limit=500
GET /api/sessions/{session_id}/workspace/search?q=app&limit=100
GET /api/sessions/{session_id}/workspace/file?path=src/App.tsx
PUT /api/sessions/{session_id}/workspace/file
POST /api/sessions/{session_id}/workspace/entry
PATCH /api/sessions/{session_id}/workspace/entry
DELETE /api/sessions/{session_id}/workspace/entry?path=src/old.ts&expected_revision=...&recursive=false
```

Reads accept complete UTF-8 regular files up to 32 MiB by default, replacing
the legacy 2 MiB editor ceiling. Writes are atomic,
require the SHA-256 revision returned by the read endpoint, and reject stale
revisions, symlinks, special files, hard links, archived chats, and read-only
targets. Directory traversal is descriptor-relative and fails closed when the
host lacks secure no-follow file APIs. Configure the text limit with
`HYADEX_WORKSPACE_TEXT_MAX_BYTES`; a positive value selects a bounded
transport ceiling, while an explicit zero disables the HyadexServer ceiling.

On macOS, recursive file search from Home (or a parent of Home) skips that
user's `Library`, `Music`, `Pictures`, `Movies`, `Desktop`, `Documents`,
`Downloads` and `.Trash` trees. Opening the file picker or entering a search
term must not incidentally request access to other apps' data. This applies to
both empty searches and Git/fallback discovery. It is a search policy, not a
filesystem permission change: explicitly opening a path, browsing a directory,
or setting a chat's working directory to a project within one of these trees
still works subject to the OS permissions and existing symlink guards. Project
subdirectories merely named `Library` or `Music` are not excluded.

Workspace-files capability v2 adds an opaque `revision` to every explorer and
search entry. Rename accepts `{path, new_name, expected_revision}`, is limited
to the same parent directory, and uses the host's atomic no-replace primitive,
so it never overwrites another entry. Delete requires the current entry
revision. A non-recursive delete removes files, symlinks, or empty
directories; `recursive=true` confirms deletion of a non-empty directory.
Recursive traversal remains descriptor-relative, never follows symlinks, and
rejects mounted filesystems rather than crossing them.

Workspace-files capability v4 adds no-overwrite creation. Post
`{path, kind: "file"}` to create an empty UTF-8 file or
`{path, kind: "directory"}` to create one directory. Creation is
descriptor-relative, rejects symlinked parents and existing destinations, and
is unavailable for archived chats.

## Workspace Git

Native administrator clients can inspect the canonical Git worktree containing
a chat's working directory, including changes outside that directory when the
chat is rooted in a repository subdirectory. Shared and restricted principals
cannot access these endpoints. Requests run on demand, off the event loop.

```text
GET /api/sessions/{session_id}/workspace/git
GET /api/sessions/{session_id}/workspace/git/diff?path=src/app.ts&view=staged
GET /api/sessions/{session_id}/workspace/git/conflict?path=src/app.ts
POST /api/sessions/{session_id}/workspace/git/action
```

Actions are `stage`, `unstage`, `commit`, `resolve`, `continue`, and `abort`.
Every action requires the current status `expected_revision`; stale reviews
return HTTP 409. Abort additionally requires `confirmed: true`. Commit uses
the reviewed staged index while preserving unstaged edits. Resolve accepts
`path` and UTF-8 `content`, saves the text, and stages it. Archived chats remain
read-only. Non-Git workspaces return `workspace_not_git` (HTTP 422).

Commands have a 30-second deadline. Text conflict sides and edited results are
limited to 2 MiB each; large diffs report `truncated`, and oversized conflict
responses fail explicitly. Binary conflicts, executable Git hooks, and files
with custom filters or merge drivers require the terminal; those mechanisms
are never silently bypassed. A rebase that reaches another conflict returns
the new conflicted status so the client can continue reviewing it.
If publishing the resulting index fails, `git_index_recovery_required`
identifies the retained index snapshot and lock. The lock blocks later writes
until the user restores that snapshot using the supplied recovery instructions.

## Imported provider history

The Import Chat list shows local main conversations across projects. It excludes
Claude sidechains and subagent directories, Codex subagent conversations and
the native `archived_sessions` directory, and sessions already imported into
Hyadex (including archived Hyadex chats). Older or stopped main chats,
headless main sessions, and user-created forks remain eligible. These discovery
filters do not delete transcripts or change existing chats or direct session-ID
lookup.

Opening a chat catches its timeline up with messages added to the provider
transcript outside Hyadex. The sync anchors on the newest timeline
messages, skips the parse when the transcript file is unchanged, refuses to
import a transcript that matches nothing on a populated timeline, and closes
every import batch with a `turn_finished` marked `imported: true`. Imported
turns never set a chat's active run, so replayed history can never make a
stopped chat look busy.

Servers before 0.1.26-beta.30 could append the same transcript tail on every
open. To remove those duplicates from a chat's event log:

```text
POST /api/sessions/{session_id}/history/prune-duplicates
{"dry_run": true}
```

The default dry run reports how many events and import runs would be
removed; `{"dry_run": false}` rewrites the log atomically while the chat is
idle, keeping the first occurrence of every message.

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

The server exposes JSON endpoints under `/api`.

- `GET /api/health`
- `GET /api/sessions`
- `GET /api/search`
- `POST /api/sessions`
- `PATCH /api/sessions/{session_id}`
- `GET /api/sessions/{session_id}/events`
- `GET /api/sessions/{session_id}/subagents`
- `GET /api/sessions/{session_id}/provider-commands`
- `POST /api/sessions/{session_id}/prompt`
- `POST /api/sessions/{session_id}/stop`
- `POST /api/sessions/{session_id}/fork`
- `POST /api/sessions/{session_id}/digest`
- `GET /api/sessions/{session_id}/files`
- `GET /api/sessions/{session_id}/diffs/{run_id}`
- `POST /api/sessions/{session_id}/upload`
- `GET /api/jobs`
- `POST /api/jobs`
- `PATCH /api/jobs/{job_id}`
- `DELETE /api/jobs/{job_id}`
- `GET /api/sessions/{session_id}/jobs`
- `POST /api/sessions/{session_id}/jobs`
- `PATCH /api/sessions/{session_id}/jobs/{job_id}`
- `DELETE /api/sessions/{session_id}/jobs/{job_id}`
- `GET /api/cross-chat/handoffs/{envelope_id}`
- `POST /api/cross-chat/handoffs/{envelope_id}/cancel`
- `GET /api/sessions/{session_id}/processes`
- `GET /api/sessions/{session_id}/tmux`
- `GET /api/runtime/catalog`
- `GET /api/runtime/usage?backend=codex&session_id=<chat>`
- `GET /api/admin/codex/subagents`
- `PUT /api/admin/codex/subagents`
- `GET /ws/sessions/{session_id}`

The event stream is append-only JSONL on disk and paged through the events API.
Large clients should page history instead of loading every event at once.

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

The subagents endpoint folds Claude local-agent lifecycle records into bounded
`subagent_state` snapshots without returning provider prompts, raw events,
tool-result output, commands, or output-file paths.

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
native snapshot. Native rate-limit events update it without polling. Custom
endpoints and API-key accounts do not expose a ChatGPT allowance.

Claude answers from Claude Code's own `/usage` data: a short-lived Agent SDK
connection sends the native `get_usage` control request without a prompt,
settings, hooks or the local transcript scan, and only plan windows are kept,
titled as Claude Code titles them. Answers are cached for two minutes and
refreshed after a native rate-limit event; windows observed through rate-limit
events are the fallback. Missing percentages stay unknown; no model request is
made to obtain usage. Responses include observation times, reset times when reported, and
Codex credit balance when supplied. Credits have no inferred currency. Caches
are scoped to the provider connection and invalidated when its account or
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
python3 -m py_compile agent_server.py hyadex_jobs.py hyadex_chats.py hyadex_emergency.py hyadex_notify.py hyadex_publish.py provider_commands.py hyadex/providers/claude.py hyadex/providers/codex.py hyadex/providers/runtime.py
rg -n 'private-host|/home/<name>|/Users/<name>|token-value' .
```

Do not commit:

- `~/.hyadex` state (and the legacy `~/.zenithbot-agent` compatibility link)
- uploads or generated artifacts
- `.env` files or access tokens
- machine-specific hostnames, IP addresses, or user home paths
- compiled Python caches


### Recoverable desktop empty-chat cleanup

New desktops pass a durable random `placeholder_client_id` to `GET /api/sessions`.
A candidate session advertises `empty_chat_cleanup: "client-v1"`; this is a
capability, not evidence that a row is safe to remove. On startup and explicit
sidebar refresh, the desktop checks its unsent drafts, references and uploads,
then calls `POST /api/sessions/{id}/dismiss-empty` with that client identity and
`expected_updated_at`. The server repeats the existing conditional empty-history
and live-resource checks under the session lifecycle lock and admission fence.

The result `{ "dismissed": true }` hides only the unchanged row in that desktop's
list. It persists across restarts without deleting the session or provider
history, and never removes another desktop's offline drafts. Reading with no
client identity still exposes retained rows; subsequent session/content changes
invalidate a dismissal. Legacy blank records are adopted only after the same
proof. Each row accepts at most 256 client identities and rejects overflow.

An unsupported endpoint, stale session or failed proof preserves the chat. The
new client never falls back to DELETE or the legacy `discard-empty` route.
Explicit user deletion retains its existing behavior. Updating only the server
or only an older client does not provide the complete new cleanup workflow.

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

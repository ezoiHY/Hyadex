# Architecture

The server is the `hyadex` package. Root-level Python files are only entry
points that installed services and scripts run by path. Moving a module never
changes a persisted data format, a protocol string or an API contract.

## Layout

| Package | Owns |
| --- | --- |
| `hyadex/core/` | Settings (`config`), paths, the chat store (`sessions`), the event log, sequencing and timeline index, the event bus, turn state (`runs`), the turn engine (`turns`, `turn_start`, `turn_receipts`), the durable queue (`queue`, `queue_recovery`, `queue_admission`, `steering`), Stop and Run now (`stop`, `stop_turn`, `run_now`, `abandoned`), provider history sync and artifacts, HTTP admission (`http`), process inspection and server state (`server`, `wiring`, `state`) |
| `hyadex/providers/` | Provider registry, authority, commands, usage, instructions and shared provider state (`state`) |
| `hyadex/providers/claude/` | The Agent SDK client (`sdk`, `sdk_manager`), the turn runner (`turns`), runtime, catalog, context usage, interactions, goals, history, subagents and side questions |
| `hyadex/providers/codex/` | The app-server client (`app_server`, `app_server_manager`), the turn runners (`turns`, `exec_runner`, `native_turns`), managers, threads, goals, notifications, interactions, tools, forks, history, subagents and side questions |
| `hyadex/features/` | Scheduled jobs, the group engine and its runtime, Autopilot, terminals, files, forks, history import, search, titles, emergency alerts and side questions |
| `hyadex/api/` | Routes, one module per area; `app.py` is the FastAPI factory |
| `hyadex/ops/` | Server lifecycle, source installation, named instances, local session ownership and the gateway/execution service layout (`ops/execution/`) |
| `hyadex/cli/` | The jobs, publish, emergency and notify helpers agents run inside a turn; `HELPER_SCRIPTS` holds their paths |
| `hyadex/main.py` | Request authentication middleware, route mounting, `app` and `main()` |

Root entry points: `agent_server.py` (installed services run
`agent_server.py serve`; uvicorn loads `agent_server:app`), `execution_service.py`,
`execution_uninstall.py` (used by `uninstall.sh`) and `server_instances.py` (used
by `instances.sh`). Each only imports its `main` from the package.
`install.sh` runs `hyadex/ops/installation.py`. `hyadex/inventory.py` lists
every runtime file; installation and packaging share it.

## Dependencies

Modules import each other directly and the import graph has no cycles. Lower
modules never import higher ones; roughly, from the bottom:

1. settings, paths, utilities, logging and shared registries;
2. the chat store, event log and sequencing;
3. provider runtimes, registry and authority;
4. the turn engine, queue and Stop;
5. provider turn runners (`providers/*/turns.py`);
6. features (jobs, groups, Autopilot, terminals, search);
7. routes, then `hyadex/main.py`.

Where a lower module must call a higher one it goes through `WIRING`
(`hyadex/core/wiring.py`), which the higher module fills in when it is
imported: the turn engine starts provider runners through
`WIRING.run_codex`/`WIRING.run_claude` and consults the job store through
`WIRING.jobs`. Importing `hyadex.main` registers both runners.

State that changes while the server runs lives on objects, so every module
sees the current value: `SERVER` (`hyadex.core.server.ServerState`: bind
address, port, shutdown, queue recovery, maintenance hold, provider callback
origin), `RUNS` (`hyadex.core.runs.RunState`: every in-memory turn registry
and its locks) and `PROVIDERS` (`hyadex.providers.state.ProviderState`: the
shared Codex app-server and Claude SDK managers with their epochs). A small
cache that one module rebinds with `global` stays private to that module.

## Boundaries

`tests/test_architecture_boundaries.py` fails if one of these is bypassed.

- **Events.** `append_event` and the durable batch writer write, sequence and
  publish to `EVENTS`. Live sockets subscribe as an ordered `deliver`
  subscriber (awaited under the chat's event lock, so a chat's events arrive
  in sequence); group discussions subscribe as a non-blocking `observe`
  subscriber. The writers do not know their consumers.
- **Sessions.** `STORE.transaction()` is the one way to serialize a
  read-modify-write of the session registry. Its lock is private.
- **Turn state.** `RUNS` owns the turn registries and their two locks
  (`active_lock` for the provider slot, `queue_lock` for the durable queue).
  `holds_turn()` and `has_waiting_work()` answer the checks admission, Stop
  and the queue share.
- **Authentication.** Each route declares how it is admitted with
  `dependencies=route_auth(kind, body_limit)`: `user` (the default bearer),
  `agent_helper`, `provider_mcp`, `native_admin` or `restart`. The middleware
  enforces the declaration of the route a request reaches, from headers,
  before the body is read; it holds no route paths of its own. Every
  `/api/agent/` and `/api/admin/` route must declare one. Mutation admission
  waits for the body only on routes that parse one, including routes of
  included routers.
- **Modules.** Every global a module uses resolves in that module;
  `SERVER_ROOT` is the installation root; a global rebound at run time is not
  reachable through the test namespace.

Route modules declare their own `router`; `hyadex/main.py` appends their
routes to the application router in a fixed order (`mount_routes`), so they
stay plain routes. Group, Autopilot and side-question routers are built by
factories that receive their engine. Importing `hyadex`, its engines or its
routers does not start a server, create state directories or start provider
processes.

## Tests

Tests address the server through `tests/hyadex_server.py`. It resolves every
name to the module that defines it, and `patch.object(hyadex_server, NAME,
value)` replaces the object in every module that holds it, then restores it
everywhere. Tests that patch state use `tests/runtime_state.py`, which routes
the old global names to `SERVER`, `RUNS` and `PROVIDERS`. Tests that compile
single definitions in isolation read `tests/server_source.text()`, the
combined source of the server modules.

## Removed features

Cross-chat messaging is removed: the cross-chat stores, mailbox and the chats
helper no longer exist. The server keeps only the per-run provider authority
(`PROVIDER_AUTHORITIES`) that the jobs, publish, emergency and notify helpers
present; it is issued at turn start, reissued for native steering and revoked
when the run ends. Its files stay in the state directory's
`cross_chat_authority/` folder for compatibility. Queue recovery discards
durable rows that carry retired cross-chat purposes or routing fields with a
durable `turn_unqueued`, and `cross_chat.sqlite3` is no longer opened.

Workspace browsing and editing, custom Codex providers, per-chat Claude API
connections and in-app Codex sign-in management are also removed.
`hyadex/providers/codex/auth.py` only reads the native Codex login: its kind,
for model policy, and a keyed revision of the login files, so that a terminal
`codex login` moves new turns to a fresh app-server. Sessions saved with a
removed custom endpoint are refused at turn, fork and runtime-change admission
with 410.

Cursor, Gemini and OpenCode execution, API binding, skills and runtime routes are
removed. Existing registry rows and timeline files retain their backend and
provider identity. Turn/fork admission rejects those rows before selecting a
replacement runtime. Native-private-field redaction and local ownership labels
still recognize persisted retired-provider records. Retired jobs stay paused
across restarts. Only Claude and Codex are admitted, even when an old environment
configuration names a retired runtime.

Browser snapshot and interactive sharing are removed, including their routers,
renderers, media adapters and live projection state. Old sharing ledgers and
transcripts remain on disk without being opened or migrated. Saved attribution
on already accepted turns is retained.

In-app update orchestration, update routes and update health advertisements are
removed. Runtime admission names the surviving worker-maintenance and restart
fences directly. `hyadex/core/file_store.py` owns the fsynced atomic JSON
primitive used by ordinary state writers. Installation, startup and uninstall
refuse to act while a journal left by the old updater (`.activation-transaction`,
`.activation-recovery`) is present. Saved update evidence is not opened or
resumed. The narrow legacy job-park migration restores exact unchanged due
occurrences and discards obsolete park ownership without overwriting edits.

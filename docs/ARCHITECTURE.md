# Package boundaries

The package migration is incremental. `agent_server.py` still owns session and
provider process state, turn admission and recovery. It supplies the completed
route collection, authorization middleware and lifespan to the application factory. Moving a module does not change its persisted data format.

| Module | Owns | Depends on |
| --- | --- | --- |
| `hyadex/app.py` | FastAPI construction, middleware and lifespan composition | Explicit `ApplicationBindings` |
| `hyadex/store/files.py` | Fsynced atomic JSON replacement | Caller-owned path and value |
| `hyadex/store/transcripts.py` | Bounded JSONL parsing | Caller-owned binary streams |
| `hyadex/jobs/schedules.py` | Interval, cron, RRULE and timezone calculations | Calendar libraries; no runtime state |
| `hyadex/jobs/context.py` | Chat/standalone provider context policy | Explicit default backend and allowed backends |
| `hyadex/providers/runtime.py` | Exact user input, bounded runtime control data and environment scrubbing | No runtime state |
| `hyadex/providers/claude/sdk.py` | Claude SDK actor ownership and lifecycle | Native SDK factory and callbacks; lazy model-catalog/side-question adapters |
| `hyadex/providers/codex/app_server.py` | Multiplexed Codex app-server JSON-RPC transport and CLI configuration overrides | Process factory and explicit callbacks |
| `hyadex/events.py` | Fan-out of committed chat events to live sockets and observers | Subscribers registered by the application |
| `hyadex/core/runs.py` | In-memory turn lifecycle state: provider slot, stop, steering, durable queue | No I/O |
| `hyadex/groups.py` | Group models, timeline, dispatch, recovery | `GroupRuntime` supplied by the application |
| `hyadex/autopilot.py` | Planning, approval, verification, review, worktree checkpoints | Group engine |
| `hyadex/routes/groups.py` | Group HTTP and WebSocket endpoints | Group engine and explicit WebSocket callbacks |
| `hyadex/routes/autopilot.py` | Autopilot HTTP endpoints | Autopilot engine |
| `agent_server.py:HyadexGroupRuntime` | Bridge to ordinary member sessions | Existing queue, native providers, jobs and event storage |

## Boundaries inside `agent_server.py`

The server is still one module, but its parts meet only through these
interfaces; `tests/test_architecture_boundaries.py` fails if one is bypassed.

- **Events.** `append_event` and the durable batch writer write, sequence and
  publish to `EVENTS`. Live sockets subscribe as an ordered `deliver`
  subscriber (awaited under the chat's event lock, so a chat's events arrive
  in sequence); group discussions subscribe as a non-blocking `observe`
  subscriber. The writers do not know their consumers.
- **Sessions.** `STORE.transaction()` is the one way to serialize a
  read-modify-write of the session registry. Its lock is private.
- **Turn state.** `RUNS` (`hyadex.core.runs.RunState`) owns every in-memory turn
  registry and its two locks (`active_lock` for the provider slot,
  `queue_lock` for the durable queue). `holds_turn()` and `has_waiting_work()`
  answer the checks admission, stop and the queue share.
- **Authentication.** Each route declares how it is admitted with
  `dependencies=route_auth(kind, body_limit)`: `user` (the default bearer),
  `agent_helper`, `provider_mcp`, `native_admin` or `restart`. The middleware
  enforces the declaration of the route a request reaches, from headers,
  before the body is read; it holds no route paths of its own. Every
  `/api/agent/` and `/api/admin/` route must declare one.

Importing `hyadex` or its engine/router modules does not import the server,
create state directories, start provider processes, or construct global engines.
The domain modules do not import HTTP routers. Each router factory receives its
engine, so isolated tests can exercise the public endpoints without compiling
fragments of the server source.

Root-level `group_chat.py`, `autopilot.py`, `chat_mailbox.py`,
`transcript_reader.py`, `claude_sdk_client.py` and `codex_app_server.py` are removed. All callers use the
package paths; there are no duplicate implementations or compatibility wrappers.
`hyadex/inventory.py` enumerates every runtime file explicitly. Source
installation and packaging share this inventory and preserve relative paths.
`hyadex/installation.py` owns manual source staging and stopped-service preflight;
`execution_install.py` and `execution_manage.py` own durable publication, native
service operations, startup admission and authenticated health/rollback.

`create_app(ApplicationBindings(...))` does not discover or import the legacy
server. HTTP authorization is mandatory, routes are snapshotted after
registration, and runtime/child-router lifespans keep their nesting and yielded
request state. Direct route metadata remains visible to the native mutation
admission guard. Group and Autopilot tests use this same factory. Production
route handlers still reference legacy process state; constructing a second
factory instance with those same handlers does not isolate that legacy state.

Cross-chat messaging is removed: `hyadex/store/cross_chat.py`,
`hyadex/store/mailbox.py`, `CrossChatBindings` and the `hyadex_chats.py` helper
no longer exist. `agent_server.py` keeps only the per-run provider authority
(`PROVIDER_AUTHORITIES`) that the jobs, publish, emergency and notify helpers
present; it is issued at turn start, reissued for native steering and revoked
when the run ends. Its files stay in the state directory's
`cross_chat_authority/` folder for compatibility. Queue recovery discards
durable rows that carry retired cross-chat purposes or routing fields with a
durable `turn_unqueued`, and `cross_chat.sqlite3` is no longer opened.

Workspace browsing and editing (`workspace_git.py` and the `/workspace` routes),
custom Codex providers (`codex_provider.py`), per-chat Claude API connections
(`provider_connections.py`) and in-app Codex sign-in management are also
removed. `codex_auth.py` now only reads the native Codex login: its kind, for
model policy, and a keyed revision of the login files, so that a terminal
`codex login` moves new turns to a fresh app-server. Sessions saved
with a removed custom endpoint are refused at turn, fork and runtime-change
admission with 410. Some handler fixtures still compile source fragments.

Further migration should separate remaining persistence, turns,
provider adapters and the remaining route families. Process state must move
behind explicit dependencies before those routes can be created as independent
applications. Do not move the monolith behind a wildcard import or duplicate
its mutable globals. Preserve durable queue admission/recovery, event sequence
allocation, WebSocket catch-up and provider-native session continuity throughout.

Cursor, Gemini and OpenCode execution, API binding, skills and runtime routes are
removed. Existing registry rows and timeline files retain their backend and
provider identity. Turn/fork admission rejects those rows before selecting a
replacement runtime. Native-private-field redaction and local ownership labels
still recognize persisted retired-provider records. Retired jobs stay paused
across restarts. Only Claude and Codex are admitted, even when an old environment
configuration names a retired runtime. Pagination and transcript byte cursors
are shared storage concepts and remain unchanged.

Browser snapshot and interactive sharing are removed, including their routers,
renderers, media adapters and live projection state. Old sharing ledgers and
transcripts remain on disk without being opened or migrated. Saved attribution
on already accepted turns is retained. Side questions use the native session
lifecycle guard and owner authentication; they do not depend on sharing.

In-app update orchestration, update routes and update health advertisements are
removed. Runtime admission now names the surviving worker-maintenance and
restart fences directly. `hyadex/store/files.py` owns the same fsynced atomic JSON
primitive used by ordinary state writers. The updater, activation transaction and
their recovery jobs are deleted; installation, startup and uninstall refuse to act
while a journal they left (`.activation-transaction`, `.activation-recovery`) is
present. Saved update evidence is not opened or resumed. The narrow legacy job-park migration restores exact unchanged due
occurrences and discards obsolete park ownership without overwriting edits.

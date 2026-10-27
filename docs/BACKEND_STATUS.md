# Backend checkpoint — 2026-10-27

This checkpoint follows the removal of unused server features at runtime commit
`4880c0d`. The source version remains `1.0.7-beta.11`, with API contract
**30**; this is not a new published release.

## Current scope

| Area | Current behavior |
| --- | --- |
| Providers | Claude Code through its native SDK and Codex through its native app-server, always with the server user's native logins |
| Collaboration | Ordinary chats, group discussions and Autopilot |
| Work management | Durable queues, scheduled jobs, goals, cancellation and restart recovery |
| Workspace | Persistent tmux terminals, chat files and file links, per-turn code diffs, working-directory completion and native session history |
| Installation | Manual source installation; separate worker/gateway user services for default and named instances |
| Authentication | Header or WebSocket-subprotocol credentials; no query-string tokens, no permissive browser CORS |

Team Hub, Team Network/secure-peer networking, Gemini/OpenCode/Cursor execution,
browser sharing, in-app updates and npm packages were removed earlier. This
round also removed the routes no client called (handoff digests, local session
listing and bulk import, manual history maintenance, timeline pins, port
tunnels, process listings, host diagnostics, several Codex and Claude controls,
provider reload, empty-chat dismissal and sidebar ordering), workspace file
browsing and editing with its Git operations, custom API endpoints, in-app
Codex sign-in management and messaging between chats. A `codex login` in the
server user's terminal is still picked up automatically.

Existing stored history and retired-feature evidence are preserved.
Retired-provider chats and chats saved against a removed custom endpoint keep
their identity; they cannot silently run with a different provider or login
(turns, forks and runtime changes return 410), and retired-provider jobs remain
paused. Jobs with saved `@Chat` targets pause for repair, and queued cross-chat
deliveries are discarded with a durable `turn_unqueued`.

The application factory, group/Autopilot engines and routes, transcript
primitives, job calendar/context policy and native provider clients live in
`hyadex/`. `agent_server.py` still owns substantial runtime state and route
logic. This checkpoint does not claim that P2 decomposition is complete.

## Verified use

- At the previous checkpoint (`807214b`), actual isolated server requests and
  native Claude/Codex runs: file edits and receipts, agent handoff, contextual
  follow-up, parallel replies, Stop, queued continuation, a manually triggered
  group job and ordered WebSocket events.
- At the same checkpoint, actual isolated macOS services: fresh source
  installation, rejection while running, same-version replacement, migration
  from a single service, retained token/identity/session data, independent
  named instances, controls and logs. An unhealthy installation candidate
  restored service files, configuration, layout and current link; the prior
  runtime restarted successfully. Uninstall removed only the selected instance
  and preserved its state.
- Full suite at `b565f8d`: **2,947 tests, all passing**. Before the removals
  began, the suite had 4,230 tests with six failures. The difference is the
  tests of the deleted features, which were deleted with them; no tests were
  skipped to reach this count. Three of the six earlier failures were fixed and
  the other three belonged to deleted features. The fast package runner
  (268 tests) also passes. `tests/test_route_inventory.py` fails if a route the
  Mac client, the in-chat helpers or a planned client feature uses stops being
  registered.

Live checks after the removals, on an isolated development instance started
from this source over its existing state directory (chats, groups and jobs
created before the removals), with real Claude Code and Codex:

- the Mac client's end-to-end run: a Claude turn and a Codex turn, each with a
  native approval, an `@codex` group round, live status, usage and context
  readings, and the terminal panel (typing, colours, CJK text, vim, resizing,
  tmux windows, detaching and reattaching);
- a Claude agent calling the `jobs list` provider helper, after which the
  run's authority file was revoked;
- response shapes of the GET routes the Mac client reads, compared with a
  snapshot taken before the removals: only fields of deleted features changed.

One check failed for a reason that predates this work: Claude's context fill
can be missing while its SDK session is not loaded. Native service execution
was exercised on macOS; Linux service-manager execution and the mobile client
have not been accepted. Server/provider checks do not establish client
presentation or thinking-text parity. See [the development log](DEV_LOG.md)
for each change's acceptance scope.

## Use this source

Follow the [README](../README.md#run-a-development-server) for an isolated
development instance or [source service installation](../README.md#install-as-a-service).
For an existing service, stop only the selected instance before rerunning the
installer; see [manual updates](SERVER_REFERENCE.md#updating-hyadexserver).
Keep native Claude/Codex login profiles available to their processes.

Do not remove pending installation journals to bypass recovery. Use the recorded
source recovery path or the previous retained installer, as described in the
server reference. The legacy update, activation and npm packaging modules are
deleted; journals they left on old installations block installation, startup
and uninstall instead of being recovered.

Data left by removed features stays on disk unread: `cross_chat.sqlite3`,
`handoff_digest_jobs.json`, route keys in `sessions.json`, and saved custom
endpoint settings and API keys under the state directory's `admin/` folder.
Delete the endpoint files if you no longer need them.

## Deferred work

- Further runtime/store/route separation, provider-interface consolidation and
  explicit application-state ownership.
- Typed settings, lighter health diagnostics and remaining AST fixture migration.
- Route contract tests beyond the route inventory, and on-disk compatibility
  fixtures.
- Obsolete alias cleanup.
- Linux service and client acceptance.

These are follow-up items, not changes included in this checkpoint. Availability
is **source only**: no release publication, deployment or existing user-service
replacement was performed.

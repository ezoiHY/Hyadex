# Backend checkpoint — 2026-10-26

This checkpoint closes the current cleanup at runtime commit `807214b`.
Further package splitting and deletion are deferred. The source version remains
`1.0.7-beta.11`, with API contract **29**; this is not a new published release.

## Current scope

| Area | Current behavior |
| --- | --- |
| Providers | Claude Code through its native SDK and Codex through its native app-server |
| Collaboration | Ordinary chats, same-server chat handoff/mailbox, group discussions and Autopilot |
| Work management | Durable queues, scheduled jobs, goals, cancellation and restart recovery |
| Workspace | Terminals, files, Git operations and native session history |
| Installation | Manual source installation; separate worker/gateway user services for default and named instances |
| Authentication | Header or WebSocket-subprotocol credentials; no query-string tokens, no permissive browser CORS |

Team Hub, Team Network/secure-peer networking, Gemini/OpenCode/Cursor execution,
browser sharing, in-app updates and npm packages are removed. Existing stored history and
retired-feature evidence are preserved. Retired-provider chats keep their
identity; they cannot silently run with a different provider, and their jobs
remain paused.

The application factory, group/Autopilot engines and routes, cross-chat storage,
transcript primitives, job calendar/context policy and native provider clients
now live in `hyadex/`. `agent_server.py` still owns substantial runtime state and
route logic. This checkpoint does not claim that P2 decomposition is complete.

## Verified use

- Actual isolated server requests and native Claude/Codex runs: file edits and
  receipts, agent handoff, contextual follow-up, parallel replies, Stop, queued
  continuation, a manually triggered group job and ordered WebSocket events.
- Actual isolated macOS services: fresh source installation, rejection while
  running, same-version replacement, migration from a single service, retained
  token/identity/session data, independent named instances, controls and logs.
- An unhealthy installation candidate restored service files, configuration,
  layout and current link; the prior runtime restarted successfully. Uninstall
  removed only the selected instance and preserved its state.
- Full suite at the runtime checkpoint: **4,226 tests, nine skips, six inherited
  failures, no new failures or errors**. All 270 fast package tests and 46 Node
  CLI tests passed, as did the post-review focused checks, Python compilation,
  shell syntax, dependency lock and whitespace checks.

The six retained failures concern two canonical history-path comparisons,
persisted Codex idle-thread loading, two installation/preparation permission
expectations, and npm CLI interpreter discovery. They remain open; the full
suite is not green. See the preceding entries in [the development log](DEV_LOG.md)
for each change's acceptance scope.

Native service execution was exercised on macOS. Linux service-manager execution
and the rewritten desktop/mobile clients have not been accepted. Server/provider
checks do not establish client presentation or thinking-text parity.

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

## Deferred work

- Further runtime/store/route separation, provider-interface consolidation and
  explicit application-state ownership.
- Typed settings, lighter health diagnostics and remaining AST fixture migration.
- Obsolete alias cleanup.
- The six known test failures and Linux/client acceptance.

These are follow-up items, not changes included in this checkpoint. Availability
is **source only**: no release publication, deployment or existing user-service
replacement was performed.

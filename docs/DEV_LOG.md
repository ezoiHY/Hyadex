# Hyadex development log

Newest first. Hyadex started from the AgentsDock server source (see
[NOTICE](../NOTICE)); its own history begins with the first version below.

## 2026-10-26 — Abort verification during process startup

A later fast-package run exposed an existing Autopilot race: Abort could retire
its verification reservation while the OS was still creating the process. The
late process was then left running until its command finished. Registration
now kills a process whose exact reservation is gone or superseded, without
attaching it to another verification.

A deterministic regression gates the real subprocess launch: it times out
before the fix and passes afterward. All 30 Autopilot cases and all 99 fast
package cases pass. Real isolated native acceptance completed a Claude plan
and Codex file edit, then aborted verification through HTTP, confirmed the
verification process exited without completing its command, and repeated Abort
without reviving the run. The disposable workspace was retained. Desktop/mobile
clients were not exercised. The full suite ran 5,330 cases (62 skipped),
with the same nine inherited failures and one concurrent WebSocket stress-test
timeout; that complete stress test passed when rerun serially. Source compile
and diff checks pass.
**Availability:** source only.

## 2026-10-26 — Independent schedule calculation and fast package checks

- Moved interval, cron, RRULE and timezone calculations into
  `hyadex/jobs/schedules.py`. Their algorithms, limits and validation errors
  remain unchanged. Job context policy now receives the caller's current
  backend allowlist and default explicitly through `hyadex/jobs/context.py`.
- Removed the old definitions and calendar imports from the server. Calendar
  tests import the package directly, while integration tests retain the
  scheduler/admission coverage.
- Added an isolated 98-case fast package suite that rejects server-runtime
  imports. Its temporary home and Git/XDG configuration are independent of the
  developer's profile. The existing full suite remains required for integration.
- Updated all runtime inventories and nested-directory validation.

**Validation.** 131 focused cases and the 98-case fast suite pass. The full
isolated suite ran 5,329 cases (62 skipped), retaining the nine inherited
failures with no new failures or errors. All 371 Python sources compile;
lock, shell, inventory and AST-equivalence checks pass. Independent Claude
static review found no blocker; its isolation and directory-guard suggestions
were applied and checked.

Both real native providers completed timer-triggered group jobs on an isolated
server: exactly one attributed message and answer per job, followed by
`max_runs` disabling it. No run-now endpoint or mocked scheduler was used.
Desktop/mobile clients were not exercised. **Availability:** source only.

## 2026-10-26 — Independent cross-chat persistence

- Moved the durable cross-chat ledger into `hyadex/store/cross_chat.py` with
  explicit clocks, authorization validators, instance configuration and
  cancellation joining. Removed its old monolith implementation. SQL,
  transaction boundaries, queue behavior and persisted formats are unchanged.
- Six isolated fixtures now import the production ledger instead of compiling
  class/method fragments. Migration checks run the complete initializer against
  the previous schema; surrounding handler fixtures still use AST extraction.
- Updated all runtime manifests and documented the storage boundary.

**Validation.** 375 focused cases passed. The isolated full suite ran 5,327
cases (62 skipped), with nine inherited failures and one parallel-load timing
failure; all six tests in that timing module passed serially. Package import and
inventory checks pass, all 365 Python sources compile, and lock/shell checks
pass. Normalizing only dependency injection gives an exact AST match with the
previous ledger. Independent Claude static review found no production blocker.

Real HTTP/native-provider acceptance passed Claude send → Codex idle wake,
read and explicit reply; the actual duplicate tool receipt reused the same
message ID and created one inbox item. A busy recipient retained unread mail
until cancellation. Restarting the isolated named server preserved message
identity, bodies, read receipts, reply relationships and the cancellation.
These checks used the real server and native providers; no desktop/mobile
client was exercised. **Availability:** source only.

## 2026-10-26 — Explicit application factory and storage package

- Added `hyadex/app.py:create_app` with required HTTP authorization, a complete
  route collection and an explicit runtime lifespan. Included-router lifecycles
  and request state are retained; importing the factory initializes no runtime.
- The production server now registers routes before constructing its app.
  Request-body admission checks inspect the current request's application,
  retaining the existing body-before-lease protection.
- Moved the mailbox SQL and bounded transcript reader unchanged into
  `hyadex/store/`, removing their old root files and updating every consumer and
  runtime manifest. No stored schema or transcript format changed.
- Converted two suites from AST-extracted handlers to real imported handlers
  and application-factory fixtures; group/Autopilot route tests use the factory.

**Validation.** The final isolated full suite ran 5,327 cases (62 skipped),
with nine inherited failures and no new failures or errors. Focused application,
transport, restart, update and storage checks pass. All 363 Python sources
compile; dependency-lock, shell syntax and inventory checks pass. Production
route metadata and the complete OpenAPI schema match the preceding commit.
Independent Claude review identified old request fixtures and router-lifespan
handling; both were corrected and covered by regressions.

Real isolated server acceptance passed HTTP/WebSocket authorization and
reconnects, the complete mixed Claude/Codex discussion/Stop/queue/job flow,
47 ordered live events, and a fully automatic Autopilot run with a separate
reviewer. Both Claude and Codex also completed timer-triggered group jobs
exactly once, with `max_runs` disabling each job afterward. This completes the
previous timer-triggered provider acceptance gap. Desktop/mobile clients were
not exercised.

**Availability:** source only. Production session/provider globals still belong
to the legacy runtime and are not isolated merely by calling the new factory.

## 2026-10-26 — Package group discussions and Autopilot

- Moved the group and Autopilot engines into `hyadex/` and their HTTP/WebSocket
  adapters into `hyadex/routes/`. Engines receive their existing runtime
  dependencies; imports do not load the server or create state.
- Removed the two old root modules, 13 unreferenced server helpers, two unused constants and obsolete
  Team helper-route exceptions. No duplicate compatibility implementations remain.
- Updated every runtime manifest and installer import check. Nested package
  paths are preserved when staging or copying the runtime.

**Validation.** The isolated full suite ran 5,325 cases (62 skipped), with the
same nine inherited failures and no new failures. The affected functional
checks pass, all 360 Python sources compile, and dependency-lock, shell syntax
and inventory checks pass. AST comparison confirms unchanged engine and route
function bodies after relocation. An independent Claude read-only review found
no blocking issue; its deployment-copy and duplicate-import findings were fixed
and checked with local transport fixtures and focused regressions.

A real isolated named server passed Claude/Codex file editing and handoff,
contextual follow-up, parallel replies, Stop, two queued rounds, a group-job
execution and 47 ordered WebSocket events. A real Autopilot run planned with
Claude, waited for approval, executed with Codex, verified in its sandbox,
reviewed with Claude and committed exactly the reviewed files in a disposable
worktree; the original test repository stayed clean. These checks also complete
the earlier mixed-provider acceptance gap. Timer-triggered jobs and desktop or
mobile clients are not covered by these runs.

**Availability:** source only. Session/provider state and the remaining server
routes have not yet been extracted; see [architecture](ARCHITECTURE.md).

## 2026-10-26 — Remove Team and cross-server networking

- Removed Team Hub, Team mail, shared team skills and secure-peer transport,
  their API routes, provider helpers, startup tasks and release payloads.
- Same-server chat routes and mailbox delivery, group discussions, Autopilot,
  durable queues and event catch-up remain available.
- New requests reject removed network authority. Recovered network queue rows
  are terminally discarded, and saved Team jobs are paused before either chat
  or group dispatch. Existing network storage is preserved.
- Installer and update health checks no longer require removed capabilities;
  obsolete Team installation and recovery requests are rejected explicitly.

**Validation.** The final isolated full suite ran 5,324 cases (62 skipped),
with nine inherited failures and one parallel timing failure that passed the
serial recheck. All 355 remaining Python sources compile, the dependency lock
check passes, and shell syntax and release inventory checks pass. Default and
named installer fixtures, actual worker/gateway startup, same-server routing,
queue recovery, and removed-authority regressions were exercised.

Real isolated server checks passed for token enforcement, HTTP, WebSocket
reconnects, absent network routes/capabilities and native Claude/Codex replies.
A mixed-provider run exercised file editing, handoff, contextual follow-up,
parallel replies and Stop. A separate Codex run passed contextual follow-up and
WebSocket reconnect. Complete repeated mixed-provider and automatic scheduled
provider acceptance remains pending: intermittent native runs stalled, including
in the pre-removal comparison, and later provider availability prevented the
remaining checks. No desktop/mobile client acceptance is claimed.

**Availability:** source only; full end-to-end acceptance remains pending.

## 2026-10-25 — Listener security and scheduled-job admission

- Non-loopback listeners now require a nonempty server token, checked by both
  the CLI and ASGI startup before loading sessions or recovering queued work.
- HTTP and WebSocket authentication no longer accepts `?token=`. Native
  authorization headers and WebSocket token subprotocols remain supported.
- The server API no longer grants cross-origin browser access. Mounted Hub
  transport policies remain separately enforced.
- The scheduled-job memory reserve is now 1536 MiB. The normal 2048 MiB turn
  floor still applies. Health reports the effective floor and the current
  memory/concurrency blocking reason without taking additional run locks.

**Validation.** 110 focused tests passed. The full isolated suite ran 6,339
cases; inherited platform failures remain. Two newly exposed contract/fixture
issues were corrected, and those plus two parallel-run failures passed in an
11-test serial recheck. All tracked Python sources compile and the dependency
lock check passes. The real-process `scripts/check_network_security.py` uses
`scripts/dev_server.py`, fresh state and a random loopback port to verify
startup rejection, HTTP header/query authentication, CORS, WebSocket denial,
authenticated reconnects and health diagnostics. It uses real HTTP/WS, with
no mocked server. Desktop/mobile client acceptance is not claimed.

**Availability:** source only; no installed service or release changed.

## 2026-10-25 — Autopilot

**Feature.** Autopilot runs let one agent plan and another implement, with a
reviewer checking every step ([design](AUTOPILOT.md)). The server drives a
state machine: plan, optional approval, then execute, verify and review for
each task, with revise and re-plan loops bounded by budgets. Agents return
structured JSON only at those points.

**Options.** These are per run:

- `approval`: `plan`, `step` or `none`.
- `isolation`: `worktree` (a branch with one commit per task) or `in_place`.
- `reviewer`: `planner`, `fresh` or `none`.
- `verify_sandbox`: `codex` or `none`.

Each role's agent and permissions can also be overridden.

**Review fixes.** An independent review found two high-severity and eight
medium issues, all fixed before release:

- Verification runs in the Codex sandbox (the executor's boundary) without
  credential variables.
- Approval commits exactly the reviewed tree, snapshotted after verification.
- A failed verification can no longer be approved by a review.
- Server sections and executor reports sit in fences the content cannot close.
- Every autopilot Git command runs with hooks disabled.
- Verification uses an async subprocess with a token: pause/resume never runs
  it twice, and abort kills it.
- A failing transition always ends in `needs_attention`. Resume then acts on
  the recorded reason.
- Re-plans discard rejected worktree edits and keep finished tasks.
- Monorepo subdirectories are preserved.
- There is no group brief, so long goals work.
- Requests are written ahead and recovered by id.
- Run-owned groups reject outside changes.

**Token use.** Every request is now self-contained, and members skip
everything else in the group. In the real run this cut prompt characters by
29% (the executor's first request by 59%).

**Group engine changes.** Runs are built on the group engine, which gained:

- group kinds;
- per-message audiences;
- orchestrator messages;
- event listeners;
- `skip_backlog`;
- an autopilot charter.

`/api/health` advertises `autopilot_v1`.

**Validation.**

- 29 autopilot tests pass, including the real Codex sandbox and Git hook
  suppression.
- The full suite (6,331 tests) has no new failures against the baseline.
- Real runs with Claude Code and Codex on an isolated `dev` instance:
  - With the defaults and sandboxed `python3 -m unittest`, a two-task slugify
    feature went from plan approval through two reviewed commits containing
    only source files. Its tests pass, and the user's tree was untouched.
  - A fully automatic, in-place run with a fresh reviewer completed.

**Not yet exercised with real agents:** step approval, revise/replan loops,
pause/resume and restart recovery (unit tests only).

## 2026-10-25 — First backend version

**Project.** Hyadex is now an independent project. The server source came
from the AgentsDock monorepo's `server/` directory. It is not synced with
upstream and starts with a fresh history. `NOTICE` keeps the original
Apache-2.0 attribution.

**Rename.** Product, modules, CLIs, environment variables (`HYADEX_*`), paths
(`~/.hyadex`, `~/.config/hyadex-server`), service labels (`com.hyadex.server`,
`hyadex-server.service`), wire identifiers and tests now use Hyadex. The
default port is 7870 and tmux sessions use the `hx_`/`hxi_` prefixes, so a
Hyadex server can run beside an AgentsDock server on the same machine.

**Safer defaults.**

- In-app updates are off (`HYADEX_MANAGED_UPDATES`). Previously they downloaded
  upstream releases, which would have replaced Hyadex with AgentsDock.
- Shared chat pages no longer load the bundled analytics script, which
  reported viewers to the upstream project's Mixpanel account.
- New chats are limited to Claude Code and Codex (`HYADEX_ENABLED_BACKENDS`).

**Group discussions.** The user, Claude Code and Codex share one timeline
([design](GROUP_DISCUSSIONS.md)). Each agent runs in a hidden native member
chat. Only mentioned agents run, and each receives only unseen messages.
Replies carry zero-token changed-file receipts. Agents hand off by mention
within a hop budget. The feature also covers parallel and analysis-only
rounds, stop, restart recovery, native goals and scheduled group jobs.
`/api/health` advertises `group_discussions_v1`.

An independent review of the feature led to these fixes before release:

- Queued, force-sent and unqueued member turns are observed on the durable
  event path.
- Dispatches are written ahead and keyed by the turn's client request id, and
  recovery never re-sends or re-records.
- Stop handles turns that start mid-stop and member chats that are gone.
- Cancelled dispatches roll back the member's cursor.
- Torn timeline tails are repaired.
- Agent text cannot forge message blocks.
- Removing a member re-anchors the group's jobs.
- A busy anchor chat no longer defers group jobs.

**Tooling.** `scripts/dev_server.py` runs an isolated development instance. CI
runs on pull requests to `main` and on manual dispatch.

**Validation.**

- Unit and wiring tests: 30 group-engine/route tests and 11 server wiring tests
  pass.
- Full suite, 6,301 tests on macOS with a temporary home: no new failures
  compared with the unmodified upstream server on this machine. The remaining
  failures are pre-existing macOS/environment cases. Load-sensitive tests
  (memory admission, Codex runner timing) pass when rerun serially.
- Real end-to-end run on an isolated `dev` instance with Claude Code 2.1.309 and
  Codex 0.163.0-alpha.11.1, through the HTTP API and WebSocket:
  - file-edit receipt and a Codex-to-Claude hand-off
  - delta-only follow-up dispatch
  - parallel analysis-only round
  - stop
  - a queued message to a busy member
  - manually run group job
  - hidden member chats
  - ordered event stream

**Not yet exercised with real agents:** approval prompts inside a group, group
goals, and jobs firing on their schedule. No client exists for groups yet; the
Mac and Android clients will be rewritten.

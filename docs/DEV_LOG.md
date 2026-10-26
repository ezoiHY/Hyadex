# Hyadex development log

Newest first. Hyadex started from the AgentsDock server source (see
[NOTICE](../NOTICE)); its own history begins with the first version below.

## 2026-10-26 — Retired updater, activation and npm code removed

- Deleted the in-app updater and everything only it used: the signed-release
  runner and its public key, update preparation, handoff and recovery, the
  activation transaction and its durable recovery jobs, and the legacy runner.
  These modules were no longer reachable from the server or the installer.
- Installation, worker startup and uninstall now refuse to proceed while a
  journal from the retired updater (`.activation-transaction`,
  `.activation-recovery`) is present, instead of trying to recover it. Fresh
  Hyadex installations never create these.
- Removed the npm packages (`@hyadex/server`, the `hyadex` CLI) and their
  packaging scripts; installation is from source only. Service logs are read
  with `./instances.sh logs [NAME]`. The phone-pairing QR helper moved to
  `tools/pairing/` and is no longer installed with the server.
- Removed the single-service systemd template; `install.sh` writes the worker
  and gateway units itself.
- No API, stored-state or chat behaviour changed.

## 2026-10-26 — Usage, context and native permissions in the Mac app

- Server: `GET /api/runtime/usage?backend=claude` now answers from Claude
  Code's own `/usage` data. A short-lived Agent SDK connection sends the native
  `get_usage` control request (no prompt, no model request, no settings or
  hooks loaded, no transcript scan) and keeps only the plan windows, with the
  titles and order Claude Code shows ("Current session", "Current week (all
  models)", Sonnet-only for Max and Team, per-model weeks). Answers are cached
  for two minutes and refreshed after a native rate-limit event. Chats on a
  custom provider connection never read the native account. Rate-limit events
  remain the fallback.
- The Mac app shows both providers' plan usage at the bottom of the sidebar
  (the window closest to its limit; the popover lists every window and its
  reset time), and each chat's context-window fill next to the composer
  controls, updated live from the server's context-usage signals.
- Permission choices use the providers' own modes and names: Claude Code's
  Manual, Accept edits, Plan, Auto, Don't Ask and Bypass Permissions; Codex's
  Read Only, Default and Full Access, with other Codex combinations shown as
  "Custom (sandbox, approval)" as Codex does. Each choice explains what it allows.
- Provider marks are drawn as vectors (Claude's spark, Codex's cloud) instead
  of generic symbols. One-agent chats no longer label every reply with the
  agent's name; group discussions still do.
- Agent replies render GitHub-flavoured Markdown (headings, nested and task
  lists, tables, quotes, highlighted code) and TeX math, inline and display,
  laid out natively with the system's math font; no third-party packages are
  added. Links to files open their diff, the local file, or the server's copy.
  Your own messages show as typed, as in the native apps.
- Images and files attached to a turn or published by an agent appear in the
  conversation (thumbnails for images); clicking opens them.
- Verified on an isolated development server with real Claude Code and Codex:
  usage for both accounts, context fill before and after a turn for both
  chats, a Claude approval in Manual mode, a Codex turn in Default mode, and a
  group reply; a live Codex answer with a heading, display and inline math, a
  table, a list and Python code rendered in the app. 30 app tests and the
  provider usage suites pass.

## 2026-10-26 — Native model and effort choices in the Mac app

- The Mac app now offers the models and reasoning efforts the server discovers
  from the native CLIs (`GET /api/runtime/catalog`: `claude --help`, `codex debug
  models`) with their native names, for chats and for each group member. Codex
  shows only the efforts the selected model supports, and an effort the new
  model cannot use falls back to the server default. Group member cards change
  that member's chat settings, which apply from its next turn.
- Group members waiting for an approval now show it above the group composer,
  where it can be allowed or denied; previously the request was only in the
  hidden member chat and the member stayed blocked.
- Chats show a sent message immediately with a live status line (starting,
  thinking, the running command or edited file, elapsed time, and the live
  reasoning summary when the provider streams one). Each turn's thinking, tool
  calls and answered approvals fold into one summary row. Commands show `~`
  instead of the home path, and sidebar previews drop Markdown syntax. Checked
  live: the status line appears within 2 seconds of sending for Claude and Codex.
- Replaced the permanent inspector column with an on-demand changes panel that
  slides over the conversation, and a details popover. The conversation keeps
  its full width, and group members scroll instead of being clipped.
- Server: the Claude catalog labels its default entries "Server default
  (Sonnet)" and "Server default (High)", as Codex already did, instead of
  repeating the name of the explicit option.
- Verified on an isolated development server: a Codex chat switched to
  `gpt-6-astra`/`high` ran its next turn with those values, and a Claude group
  member switched to `haiku`/`low` answered with them. The fast suite (270
  tests) and 17 app tests pass.

## 2026-10-26 — First macOS client

- Added `apps/macos`, a native SwiftUI client built with SwiftPM: a sidebar with
  Work, Group and Autopilot tabs, live chat and group timelines over the event
  streams, inline Claude and Codex approvals, changed files and diffs, queued
  messages, per-chat model/effort/permission controls, Autopilot run controls,
  scheduled jobs, a ⌘K palette, a menu bar extra, and settings that store the
  server token in the keychain. Without a server it shows demo data.
- Verified against an isolated development server with real Claude Code and
  Codex: turns with approvals for both providers, an `@codex` group round, a job
  toggle and a one-task Autopilot run through to its commit. Fifteen unit tests
  cover the event projections with recorded event logs, and a layout stress
  check covers narrow windows.
- Not yet verified: connecting through the Settings form, creating chats or
  groups from their sheets, Stop and queue edits on a live server, reconnection
  after a server restart, and older-history paging (not implemented). Signing
  is ad hoc and there is no release. No server code changed.

## 2026-10-26 — Consolidate the current backend checkpoint

- Stopped further decomposition and deletion at runtime commit `807214b`.
  Added [the backend checkpoint](BACKEND_STATUS.md) with current behavior,
  removed features, verified boundaries, the six open test failures and
  deferred work. README links to it. No runtime behavior changed in this step.
- Rechecked the current source through an isolated named development server
  with actual Claude and Codex: file editing/receipt, handoff, contextual
  follow-up, parallel replies, Stop, busy queue continuation, a manually
  triggered group job and 47 ordered WebSocket events all passed. The fixture
  server was stopped afterward; no installed user service was used.
- The preceding runtime validation remains 4,226 full-suite cases with nine
  skips and six inherited failures, plus 270 passing fast package tests and
  46 passing Node tests. This checkpoint does not claim a fully green suite,
  complete decomposition, Linux service acceptance or desktop/mobile parity.
- Availability: source only. Version and API contract remain `1.0.7-beta.11`
  and 29. No release, deployment or installed-service replacement occurred.

## 2026-10-26 — Manual source installation and shared runtime inventory

- Replaced the large shell installer with a thin entrypoint and
  `hyadex/installation.py`. Removed the direct deployment helper, obsolete
  Hub/update/preparation installer branches and their shell-fragment tests.
  `hyadex/inventory.py` is the single runtime inventory used by installation
  and packaging; package imports stay inert.
- Both default and named instances use separate worker/gateway services.
  Installation refuses a running incumbent, stages and checks a unique runtime,
  and uses the existing native controller for journaled service/configuration
  publication, process-owned health, startup admission and rollback. Saved
  tokens, identity, history and configuration comments are preserved. Completed
  journals retain compact receipts without old environment snapshots.
- Instance controls share installation ownership and seal an idle worker before
  stopping either service. Legacy macOS stop now waits for unloading before
  returning. Ports are exact; retired Hub, preparation and port-fallback options
  reject. npm legacy recovery is retired and fresh install uses the new entrypoint.
- Old state selectors cannot silently select a new empty state directory. Old
  activation/recovery owners must finish through their retained tools. Current
  native installation journals support explicit source recovery; saved evidence
  is not deleted to bypass ownership. Remaining legacy helper dependencies will
  be removed in a separate stage.
- Actual acceptance used temporary homes and uniquely named launchd services:
  fresh installation, refusal while running, same-version replacement, an actual
  single-service-to-split migration, token/identity/session retention, independent
  instance controls and logs. An intentionally unhealthy candidate restored both
  service files, environment, layout and current link; the previous runtime then
  restarted successfully. Quoted display names and retained operator comments
  were also checked through authenticated HTTP. Removing one actual named pair
  preserved its state and left the other fixture unchanged. All fixture services
  were stopped.
- Claude's static review identified environment quoting, old-state selection and
  a stale opt-in Linux test service selector. Those findings and its native
  ownership/credential-retention findings were addressed. The core server and
  Claude/Codex execution implementations were unchanged.
- Validation: the full suite ran 4,226 tests (nine skips), retaining six
  inherited failures and no new failures or errors. Two previous failure cases
  belonged to the retired installer shell branches. All 129 post-review focused cases,
  270 fast package cases and 46 Node CLI cases passed. The broader 244-case
  lifecycle run retained one known platform/umask failure and two skips.
  Python compilation, shell syntax, offline lock and whitespace checks passed.
- Availability: source only. No release, deployment or existing user service was
  changed. Native installation acceptance ran on macOS; Linux manager execution
  and desktop/mobile UI were not exercised. Linux rendering and controller
  contracts remain covered by their separate suites.

## 2026-10-26 — Retire in-app update orchestration

- Removed update endpoints, background scheduling, startup resumption, health
  advertisements and schedule-bound force updates. Old update settings cannot
  re-enable them. The public API contract is 29; the release version is unchanged.
- Removed the npm update commands and their token/signature HTTP transport.
  They reject before reading credentials or contacting a server. Fresh install
  and existing recovery commands remain while the installer is replaced.
- Retained ordinary restart and worker-maintenance admission, durable queues,
  HTTP mutation leases and native provider work checks. Old update receipts,
  logs and preparations remain untouched. Exact legacy parked job occurrences
  resume at startup; later user edits keep their schedule.
- Moved the existing fsynced atomic JSON writer into `hyadex/store/files.py`
  and registered it in every runtime inventory. Server imports no longer load
  the updater. Native execution, event allocation/delivery, group runtime and
  recovery definitions retain their behavior.
- Actual acceptance: two isolated server starts with synthetic old active and
  force-pending update state verified route removal, contract 29 health, native
  auth, rejection of retired restart fields, ordinary mutations, retained
  evidence and exact job recovery. Real Claude/Codex runs covered file edits,
  receipts, handoff, incremental context, parallel replies, Stop, busy queues,
  a manually triggered group job and ordered WebSocket delivery.
- Claude's static review found no loss of non-update protection. Its remaining
  npm entrypoint and installation/recovery documentation findings were addressed.
  Manual source updates currently require stopping the selected service first;
  the installer and its activation dependencies remain for the next stage.
- Validation: 444 focused tests passed (one platform skip), including real
  gateway/worker maintenance acquisition, public rejection and exact release.
  All 257 fast package tests and 51 Node CLI tests passed. The full Python suite
  ran 4,391 tests with 13 skips and the same eight inherited failures, with no
  new failures or errors. Compilation, shell syntax, dependency-lock, runtime
  inventories and whitespace checks passed.
- Availability: source only. No release, deployment or installed service was
  changed. Desktop/mobile UI and native OS service installation were not
  exercised; acceptance used isolated server/provider and worker/gateway processes.

## 2026-10-26 — Remove browser chat sharing

- Removed public snapshot and interactive sharing routes, browser bundles,
  media adapters, projection state and authentication exceptions. Old URLs no
  longer resolve. Retired bearer URLs remain redacted in access logs.
- Existing sharing ledgers, registered files and transcript bytes remain on
  disk. Already accepted turns retain collaborator attribution during queue
  recovery. New native turn requests cannot claim retired sharing attribution.
- Preserved native side questions by retaining and renaming their session
  lifecycle guard. Native file access, same-server chat routes, groups and
  provider event delivery retain their behavior.
- Verification: actual HTTP checks across two isolated server starts confirmed
  route removal, native authentication, rejection of new sharing attribution,
  retained historical attribution and unchanged synthetic ledger/file/history
  bytes. The route-removal and new-attribution regressions failed before the
  corresponding changes. Separate real Claude/Codex checks covered file edits,
  receipts, handoff, incremental context, parallel replies, Stop, busy queues,
  a manually triggered group job and ordered WebSocket delivery.
- Claude's read-only review found no blocking regression; its attribution and
  stale-documentation findings were addressed. Native runtime, group, queue
  and recovery definitions were compared structurally with the previous source.
- Validation: 311 focused tests and 257 fast tests passed. The full suite ran
  4,581 tests with 13 skips and the same eight inherited failures, with no new
  failures or errors. Python compilation, release inventories, shell syntax,
  offline dependency-lock and whitespace checks passed.
- Availability: source only. No deployment, release or installed service was
  changed. Hyadex's rewritten desktop/mobile clients were not exercised; the
  acceptance surface was the actual isolated server and native providers.

## 2026-10-26 — Retire Cursor and admit only Claude/Codex

- Removed Cursor execution, process guard, MCP bridge, CLI probes, API-key
  configuration, optional title generation and local native-history discovery.
  Runtime inventories now contain only the Claude and Codex integrations.
- Removed Cursor health capabilities and the local-discovery opt-in parameter.
  Bulk-import requests containing a Cursor item fail validation; old optional
  Cursor-specific request fields follow the existing unknown-field policy and
  are ignored. No old field or environment setting can enable Cursor execution.
- Stored chats retain their backend, native identity, title and event bytes.
  Metadata edits remain available; execution, import and fork return 410.
  Saved jobs and retired backend overrides remain durably paused. Native
  configuration, credentials and transcripts are retained as user data.
- Internal executable discovery now rejects retired/unknown providers before
  resolving a CLI instead of falling through to Codex. Its regression failed
  before the guard and passes after it. Generic pagination, transcript byte
  cursors and same-server mailbox behavior remain intact.
- Claude static review identified stale installer expectations and a title
  process fixture. Expectations were corrected; the process fixture now tests
  the shared bounded process owner and still verifies update/restart deferral,
  shutdown and descendant cleanup. Separate tests cover the Codex protocol.

**Validation.** The final isolated full suite ran 4,826 cases (13 skipped), with
only the eight inherited failures and no new failures or errors. All 257 fast
package cases and 257 post-review focused cases pass; the earlier broader
focused run passed 634 cases. All 328 Python files compile; dependency-lock,
shell, manifest and diff checks pass. Native runners, event delivery, group
recovery and shared history-cursor functions retain their previous ASTs.

Real isolated HTTP acceptance passed old native/custom history reads, metadata
edits, native/custom import guards, retired execution and fork rejection,
durable job pause and two starts with unchanged event bytes and native files.
Real Claude/Codex acceptance passed file editing and receipt, handoff,
contextual follow-up, parallel replies, Stop, queue continuation, group-job
execution and 47 ordered WebSocket events. Desktop/mobile clients and a live
Cursor runtime were not exercised. **Availability:** source only.

## 2026-10-26 — Retire the OpenCode runtime

- Removed OpenCode execution, stream translation, CLI probing, native title
  reading, skill scanning, custom API configuration and provider-specific
  settings. Removed its live verification scripts and runtime inventory entries.
- Old OpenCode chats retain their backend, native identity and timeline. They
  remain readable and can be organized; runtime changes and execution reject
  the retired backend. Saved jobs, including backend overrides on supported
  parent chats, stay paused across reloads. Identity-only records remain
  protected from automatic empty-chat cleanup.
- Native profiles, credentials and old private per-turn instruction files are
  retained as data. This server no longer reads them to launch OpenCode. No
  replacement provider, transcript migration or native-file deletion is used.
- Independent Claude static review caught a shared native-import/custom-API
  guard removed with an OpenCode branch. It was restored for all remaining
  native identity aliases, with regressions and real HTTP checks. Retired-job
  override coverage and code formatting were also improved.

**Validation.** The final isolated full suite ran 5,036 cases (12 skipped),
with eight inherited failures, no errors, and one parallel-load timing failure;
all six cases in that timeline module passed serially. All 257 fast-package
cases and 237 post-review focused cases pass, including the unchanged Cursor
runner. All 339 Python sources compile; dependency-lock, shell and inventory checks pass.
Eleven native runner/event/group delivery functions retain their previous ASTs.

Real isolated HTTP acceptance passed old native/custom history reads, metadata
edits, execution/import/fork rejection, durable job pause and a server restart
with identical history bytes and identities. Claude/Cursor native-session
imports into a custom API reject before endpoint binding or chat creation.
Real Claude/Codex acceptance passed file editing and receipt, handoff,
contextual follow-up, parallel replies, Stop, queued messages, group-job
execution and 47 ordered WebSocket events. Desktop/mobile clients and a live
OpenCode runtime were not exercised. **Availability:** source only.

## 2026-10-26 — Handle an exhausted Git snapshot budget

Corrected the timeout exception's constructor when the shared snapshot budget
expires between Git commands. Capture now follows its existing timeout cleanup
path instead of raising `TypeError`. A deterministic regression failed before
the correction and passes afterward, checking that no later command starts and
no snapshot objects remain; all ten snapshot-security cases pass.

Real isolated Claude/Codex acceptance passed a file edit and receipt, handoff,
contextual follow-up, parallel replies, Stop, queue continuation, a group job
and ordered WebSocket events. The exact between-command deadline boundary is
covered by the deterministic test, not by timing a live provider. No client UI
was exercised. **Availability:** source only.

## 2026-10-26 — Retire the Gemini runtime

- Removed Gemini ACP/API execution, import, permission, skill and title modules,
  their routes and runtime advertising, and the Gemini-only process-guard mode.
  Runtime inventories no longer ship these modules. Native profiles and stored
  connection credentials are retained without executing or migrating them.
- Existing Gemini chat histories remain readable and can be renamed, moved,
  pinned or archived. Runtime edits, turns, forks and native imports reject the
  retired provider. Saved scheduled/manual-pending jobs are durably paused with
  `provider_removed`; they cannot silently run with another provider.
- Retained native-identity/redaction fields and the existing uncertain-cleanup
  fence. Stop and Delete do not claim cleanup of a removed runtime. Timeline,
  queue and event-storage formats are unchanged.

**Validation.** Focused retirement, session, job, connection and guard checks
pass, as do 258 fast-package cases. The final isolated full suite ran 5,195
cases (15 skipped), retaining eight inherited failures. It also exposed an
existing Git snapshot deadline exception-construction defect, queued for a
separate correction. All 349 Python sources compile; dependency-lock and
shell checks pass. Independent Claude static review identified metadata edits,
old timers and import side effects; these were corrected and covered. Two
older AST fixtures were updated for the new retirement dependencies and their
21 cases pass.

Real isolated HTTP acceptance loaded native/custom saved chats, read unchanged
event bytes, edited metadata, rejected execution/import/fork and job launch,
then restarted the same named instance and verified identity and job-pause
persistence. Native Claude/Codex acceptance passed file edits, handoff,
contextual follow-up, parallel replies, Stop, queued messages, group-job
execution and ordered WebSocket events. Both native providers separately
completed timer-triggered jobs exactly once. Desktop/mobile clients and a live
Gemini runtime were not exercised. **Availability:** source only.

## 2026-10-26 — Native provider package and shared runtime contract

- Moved the Claude SDK actor client and Codex app-server transport unchanged
  into `hyadex/providers/claude.py` and `codex.py`. Removed their old root files
  and updated static/inline imports, mock targets and all runtime inventories.
- Extracted exact-user-text validation, bounded control context, per-turn
  environment validation and secret scrubbing into `hyadex/providers/runtime.py`.
  Legacy secret names remain scrubbed. No provider protocol or control-data
  semantics changed. Claude's internal logger is now `hyadex.providers.claude`.
- Four test fixtures now use the production validator instead of extracted or
  pass-through replacements. Invalid synthetic environment keys were corrected
  in those fixtures and their consumers. The fast suite includes both native
  client contract suites and the standalone runtime contract.

**Validation.** 234 focused cases and 257 fast-package cases pass. The full
isolated suite ran 5,334 cases (62 skipped), with the same nine inherited
failures and no new failures or errors. Follow-up validator/consumer tests pass.
All 374 Python sources compile; dependency-lock, shell and manifest checks
pass. The two moved native client files match their previous bytes, and the
extracted runtime definitions match their previous ASTs. Independent Claude
static review found no blocker; package dependency and compile documentation
were corrected.

A real isolated server passed native Claude/Codex file editing and handoff,
contextual follow-up, parallel replies, Stop, queued messages, group-job
execution and 47 ordered WebSocket events. Real provider-tool mail passed send,
exact-key duplicate receipt, idle read/reply, busy unread cancellation and
server restart persistence. Desktop/mobile clients were not exercised.
**Availability:** source only. Remaining provider orchestration still belongs
to the legacy runtime; this does not claim a completed unified provider API.

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

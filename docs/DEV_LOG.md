# Hyadex development log

Newest first. Hyadex started from the AgentsDock server source (see
[NOTICE](../NOTICE)); its own history begins with the first version below.

## 2026-10-28 — New app icon

- The icon keeps the V of the Hyades and orange Aldebaran, with crisp stars,
  soft glows instead of hard halos, and stars in different colours for agents
  with different gifts, meeting in one star at the vertex. The README shows it
  as the project logo.

## 2026-10-28 — Project README with architecture and screenshots

- The README (and a Chinese version, README.zh-CN.md) now explains what Hyadex
  is, its server and Mac app architecture with a diagram, the UI design, and
  shows eight screenshots in `docs/images/`.
- The snapshot mode can render the sidebar for these screenshots
  (`HYADEX_SNAPSHOT_SIDEBAR=1`); the sample jobs show their results as the app
  does for real ones.

## 2026-10-28 — Mac app: a calmer composer

- The message box is two lines tall with rounder corners, like Claude's and
  Codex's apps. Its bottom row holds only "+", the permission mode (orange when
  it skips safety checks), the context ring (its number shows from 70 %), the
  model and effort with a chevron, and send.
- "+" holds 添加文件…, 侧问 (⇧⌘B) and 设定目标…; in a group also the answer mode,
  只分析, the goal and 添加成员. A non-default answer mode or 只分析 shows as a
  small label beside the members.
- Verification: `HYADEX_E2E_UI=gaps` passed 44 checks, including the side chat
  and goal opened from "+".

## 2026-10-28 — Mac app: larger text and less explanatory copy

- One type scale for the whole app: messages 14 pt, secondary lines 13 pt,
  metadata 12 pt, badges 11 pt (macOS caption styles are 10 pt). Settings ›
  通用 › 字号 adds 大 (about 15 %).
- Explanations moved from the screen into tooltips, or were removed: form
  footers, the slash-menu key hints, the composer placeholder's command hint,
  the queue note beside the send button, folder and cron help, the group
  answer-mode note. Character counts appear only near a limit.
- 高级设置 and 讨论组设置 are one compact form each; the subagent limit is a
  single menu. Chat token usage is one line in ⓘ (in 万), with the breakdown on
  hover. Sidebar tab badges no longer push 「Autopilot」 out of its tab.
- Verification: 88 Swift tests; `HYADEX_E2E_UI=gaps` passed 40 checks against a
  real isolated server; before/after snapshots at 标准 and 大.

## 2026-10-28 — Mac app: real job outcomes, token totals, settings and pasting

- 定时任务 › 上次结果 shows how the last run ended. Before, a scheduled run that
  failed still showed 成功 because only manual-run errors were recorded. The
  server now keeps `last_run_status` (running, completed, failed, stopped, or
  dispatched for group jobs) with the error and finish time, including runs that
  could not start.
- ⓘ shows the tokens a chat has used across its turns. The server sums the
  providers' own counts into `token_totals` (Claude's per-turn usage, the growth
  of each Codex thread's total); earlier turns are not counted.
- 高级设置… edits a chat's system prompt, subagent limit, automatic title and the
  agent's access to scheduled jobs. 讨论组设置… edits the default responder, the
  hand-off limit and the shared brief; ⓘ shows the real values instead of a fixed
  "4 次". In a group `PATCH`, `null` now restores a setting's default instead of
  storing an empty limit.
- 在编辑器中打开 in the changes panel works: the file opens in place when the
  server is on this Mac, or as a read-only copy from
  `GET /api/sessions/{id}/workspace/file` (limited to the chat's folder and its
  Git work tree) when it is remote.
- ⌘V in the composer attaches screenshots and files copied in Finder; text
  pastes as before.
- Form controls in the new sheets carry accessibility labels.
- Verification: the backend suite (3,053 tests) passes; a real
  API pass with Claude Code and Codex checked each new field and route. In the
  app, `HYADEX_E2E_UI=gaps` passed 41 checks through the real controls against
  an isolated server (see the Mac README). 88 Swift tests pass.

## 2026-10-28 — Mac app: start a local backend and remove demo mode

- The welcome screen, server settings and server menu can start a real,
  app-owned local backend and connect without copying a token. It uses a
  separate named instance and an available loopback port; repeated connections
  reuse its process. Quitting the app stops it and preserves chat history.
  Existing installed servers are not adopted or restarted. Source builds use
  their recorded checkout and its prepared Python environment.
- Ordinary startup, failed connections and disconnects now show an empty
  connection screen. Removed simulated replies and offline mutations; sample
  content is limited to explicit development snapshot fixtures. Late results
  from a disconnected or replaced connection cannot populate another server.
- Verification: 82 Swift tests passed, including an isolated real backend and
  startup cancellation/ownership regressions. A separately identified native
  app (0.1.0, debug build 202610281132, based on source 1d8e6381) was exercised
  through its actual controls: local startup, authenticated connection and
  unauthenticated rejection, Settings test, repeated selection, disconnect and
  reconnect, new-chat creation, app exit and restart with that chat retained,
  and retryable missing-source failure without sample content. The app used
  its normal HTTP transport and real isolated state; these boundaries were not
  mocked. Provider turns and remote Keychain connections were not revalidated
  in this change. Availability: local source/package only.
- Package: release-configuration build 202610281141 (0.1.0, source 803bb1e2)
  compiled and passed ad-hoc signature verification. The final package's UI
  recheck remains pending because the native automation connection was
  unavailable; the full UI acceptance above used the debug build.

## 2026-10-28 — Mac app: files, queue, history, alerts, jobs and chat management

- Attachments: the paperclip, or files dropped anywhere on a conversation,
  upload at once with their progress and go with the next message.
- Queued messages can be moved, sent now (Force Send) or, after a Stop, held
  and continued with 继续发送; sends to one chat keep their typed order.
- "加载更早的消息" pages back through long chats; a search hit in older history
  loads back to it.
- Agents' emergency alerts appear as a red banner, a macOS notification and a
  mark on the chat until acknowledged.
- Scheduled jobs for chats and groups can be created, edited and deleted
  (daily, weekdays, weekly, every N minutes/hours/days, once, or cron), with a
  preview of the next run.
- Chats and groups can be renamed, archived (collected under 已归档), restored
  and deleted after a confirmation; chats can be marked unread and forked.
- The sidebar search also searches message content; side questions (`/btw`)
  open in a panel; subagents are listed in the details; the context ring can
  compact the conversation; new chats pick their folder on the server.
- Settings now take effect: ⌘↩ to send, opening each turn's work by default,
  the usage footer, notifications for approvals and finished replies (also
  only-in-background), and the Dock badge. 通知 shows macOS's permission for
  Hyadex and links to System Settings when it is off. ⇧⌘J opens a job for the
  open chat.
- Claude: output that an earlier priority command delivers in a later turn
  (for example clearing a goal mid-turn) is marked `local_command_detached` and
  not shown as that turn's reply; history sync matches Claude output by its
  message id, so a live local-command turn is never imported a second time.

## 2026-10-28 — Claude: symlinked folders, `/compact` and command history

- Claude chats whose folder is reached through a symlink (such as `/tmp` on
  macOS) resume their conversation again. Claude files those sessions under
  the resolved folder, so every turn started a new Claude session and lost
  the context. A session created in a different folder is still never resumed.
- Claude's local commands such as `/compact` no longer end as a failed turn
  ("Claude returned no response"). Their output, "Compacted" or an error such
  as "No messages to compact", is the turn's reply, as in Claude Code. It is an
  `assistant_text` event marked with `local_command` and `local_command_stream`.
- Compaction, from `/compact` or automatic, is recorded as a durable
  `claude_compaction_completed` event (`trigger`, `pre_tokens`, `post_tokens`),
  and the context-usage reading is taken again afterwards.
- Imported Claude history shows a slash command as typed (`/compact`) and its
  output as a reply, instead of the raw `<command-name>` and
  `<local-command-stdout>` markup. History imported earlier is shown the same
  way, and a command that already ran in the chat is not imported again.

## 2026-10-28 — Resume a stopped queue; Claude MCP reconnect-all

- Messages that Stop held no longer block new work, as in the native clients
  after Esc: a message sent to an idle chat starts at once, and one sent while
  a turn runs is queued and runs next. The held messages keep their place
  until the user resumes, force-sends or removes them, and the same holds
  after a restart. They also no longer stop a Codex goal from resuming, keep
  a group member shown as queued, or keep a group's dispatch open. A group
  message that a Stop in the member's own chat holds is withdrawn from that
  chat and ends as stopped for the group, so the round continues.
- Server: `POST /api/sessions/{id}/queue/resume` releases every hold that Stop
  placed on queued messages, so the queue continues in order instead of
  needing Force Send per message. Messages whose provider delivery is
  uncertain stay held and are reported. The release is saved as a
  `turn_queue_resumed` event before it takes effect, so it survives a restart;
  if it cannot be saved, nothing is released. Health advertises
  `queue_resume_v1`.
- Claude MCP "reconnect all" retries only servers that failed to connect.
  Servers waiting for sign-in (such as claude.ai connectors) and disabled
  servers are left alone, and the time allowed grows with the number of
  servers retried, so it no longer times out on profiles with many connectors.

## 2026-10-27 — Goals, commands and MCP servers in the Mac app

- Chats can set the provider's native goal from the composer or with `/goal`:
  Claude Code's completion condition, or Codex's persistent objective with an
  optional token and time budget. A bar above the composer shows its progress,
  with pause/resume (Codex), edit and clear. Groups set a goal on one member.
- Typing `/` lists the chat agent's own commands and skills; a message starting
  with one runs it natively.
- The details panel lists the MCP servers the agent loaded, with their state and
  tool count. Claude servers can be reconnected, disabled and enabled.
- Server: `GET`/`POST /api/sessions/{id}/claude/mcp` is back (it had been removed
  as unused) and `GET /api/sessions/{id}/codex/mcp` is new, reading Codex's
  native `mcpServerStatus/list`. Both return the same display-safe summary: no
  commands, environment, URLs or raw errors.

## 2026-10-27 — Server split into packages

- Internal restructuring with no change to the API, stored data or chat
  behaviour. The server now lives in the `hyadex` package, split by domain
  (`core`, `providers/claude`, `providers/codex`, `features`, `api`, `ops`,
  `cli`; see [ARCHITECTURE](ARCHITECTURE.md)). Installed services start the
  same way: `agent_server.py`, `execution_service.py`, `execution_uninstall.py`
  and `server_instances.py` remain as entry points, and `install.sh`,
  `uninstall.sh` and `instances.sh` are used as before.
- The helper commands agents run inside a turn moved from `hyadex_*.py` to
  `hyadex/cli/` (`jobs.py`, `publish.py`, `emergency.py`, `notify.py`). Agents
  receive the new paths automatically from the next turn.
- Fixed: requests that change a group discussion took their admission lease
  before their body had arrived, so a slow upload could hold up a server
  restart. They now wait for the body like every other change.

## 2026-10-27 — Server internals separated

- Internal restructuring with no change to the API, stored data or chat
  behaviour: chat events reach live clients and group discussions through an
  event bus, turn state lives in one object, and each route declares how it is
  authenticated. Authentication still happens before a request body is read.
- One small difference: if delivering an event to a live client fails, the
  event (already written) is no longer reported as a failed write.

## 2026-10-27 — Claude context fill no longer drops out

- Fixed: a Claude chat's context fill could go blank after a turn and stay
  blank on every turn after. The server reads it from Claude Code once a turn
  ends; the first reading after Claude's process starts takes one to two
  seconds, and the old two-second limit treated a slow one as a failure, cleared
  the reading and restarted the Claude process, so the next turn was slow again.
  The limit is now ten seconds (`HYADEX_CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS`);
  readings normally arrive in milliseconds, so turns do not end later.

## 2026-10-27 — Unused server features removed

The server now carries only what the Mac app, the in-chat helpers (jobs,
publish, emergency, notify) and planned client features use. API contract 30.

- Removed endpoints no client called: handoff digests, listing and bulk
  importing local CLI sessions (resuming a native conversation when creating a
  chat still works), manual history maintenance, timeline pins, port tunnels,
  process listings, host diagnostics, Codex load/rotate/rollback/review/shell and
  background-terminal controls, Claude MCP management, provider reload,
  empty-chat dismissal, sidebar ordering and duplicate file and job routes.
- Removed workspace file browsing and editing, including its Git staging and
  commit routes. Per-turn diffs, chat files, file links and working-directory
  suggestions are unchanged.
- Removed custom API endpoints for Codex and Claude and in-app Codex sign-in
  management. Chats always use the native Claude Code and Codex logins, and a
  `codex login` in the terminal is still picked up for new turns. Chats saved
  against a custom endpoint keep their history and can be renamed or archived,
  but no longer run; saved endpoint files stay on disk for the user to delete.
- Removed messaging between chats (handoffs, @Chat references, the chat
  mailbox and the `chats` helper). Group discussions cover multi-agent work.
  Queued deliveries left from before are discarded visibly, scheduled jobs that
  targeted another chat pause for editing, and old history stays readable.
- The chat terminal (one persistent tmux session per chat) is kept; the Mac
  app now has a terminal panel for it.

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

## 2026-10-27 — Terminal in the Mac app

- Each chat has a terminal panel (⌃` or the toolbar button) docked under the
  conversation. It attaches to the chat's persistent tmux session on the server,
  the same one the agent can inspect, through the existing authenticated PTY
  socket.
- The terminal is a native view with its own emulator (no third-party packages):
  colours including 256-colour and true colour, the alternate screen used by
  full-screen programs, wide CJK characters, input methods, copy and paste
  (bracketed when the program asks for it), and scrolling back through tmux
  history with the trackpad or mouse wheel.
- Tabs show the session's tmux windows; new windows, splits, closing a pane and
  ending the session are in the panel's header. Hiding the panel only detaches:
  commands keep running, and reopening it repaints the current screen.

## 2026-10-26 — Group discussions match chats in the Mac app

- A group reply now shows its author, then the member's work folded into one
  row (thinking, commands, edits, answered approvals, with the time it took),
  then the answer and the files it changed with their diffs. The work and
  diffs come from each member's own chat, matched to the group by the
  dispatch id the server already uses as the member turn's request id; no
  server change was needed.
- A sent message shows at once, and each member still answering has a live
  status line. A member's approval appears inside its turn instead of above
  the composer, and the member shows that it is waiting.
- Members moved from a bar above the conversation into the composer, like a
  chat's model menu: model, effort, native permission mode and context fill
  per member, adding Claude or Codex members and removing them. The answer
  mode and analysis-only sit next to them; dispatch statistics are in ⓘ.
- Verified on an isolated development server with real Claude Code and Codex:
  a Codex member's shell command shown live and folded under its reply, a
  Claude member's file-edit approval shown inline, approved, and its receipt
  opening the real diff, and adding and removing a Codex member. 32 app
  tests pass.

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
  into `hyadex/providers/claude/sdk.py` and `codex.py`. Removed their old root files
  and updated static/inline imports, mock targets and all runtime inventories.
- Extracted exact-user-text validation, bounded control context, per-turn
  environment validation and secret scrubbing into `hyadex/providers/runtime.py`.
  Legacy secret names remain scrubbed. No provider protocol or control-data
  semantics changed. Claude's internal logger is now `hyadex.providers.claude.sdk`.
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

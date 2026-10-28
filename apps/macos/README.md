# Hyadex for macOS

Native SwiftUI client for a Hyadex server: chats, group discussions, Autopilot
runs and scheduled jobs in one window, live over the server's HTTP/WebSocket
API. It is built with SwiftPM and does not need an Xcode project.

> **Status:** first version, source only (no signed release). It runs against a
> real server for the flows listed under [Verified](#verified); see
> [Known gaps](#known-gaps).

## Requirements

- macOS 15 or later (the app adopts the current system design on macOS 26+).
- A Swift 6 toolchain: Xcode, or the Command Line Tools with the macOS SDK.
- For the local server: keep this source checkout and prepare its Python environment
  once with `uv sync --locked --python 3.13` at the repository root.
- Or a Hyadex server you can reach (same Mac, LAN or Tailscale) and its access token.

## Build and run

```sh
cd apps/macos
scripts/build-app.sh      # release build -> dist/Hyadex.app (ad-hoc signed)
scripts/run-dev.sh        # debug build, then open the app
scripts/test.sh           # Swift Testing suites
open dist/Hyadex.app
```

`build-app.sh` assembles the bundle (Info.plist, icon, entitlements) and signs it
ad hoc. It also records the SDK version in the binary: SwiftPM's default build
system otherwise records the deployment target, and macOS then runs the app with
its legacy window chrome.

## Start or connect to a server

The first window is empty until a real server connects. Click **启动本地服务**
to start a loopback-only, app-owned backend and connect automatically; no token
needs copying. The same control is available in Settings → 服务器 and the sidebar
server menu. Repeated clicks reconnect to the same process, and disconnecting
leaves it running. Closing the window also leaves it running; quitting Hyadex
stops this backend and preserves its history. The app asks before quitting if
local work is active or its activity cannot be checked from the current connection.

This source-built app records its checkout path in its bundle and uses that
checkout's `.venv` and `scripts/dev_server.py`. Keep the checkout in place. It
does not install a service or download dependencies. The backend has a private
`mac-app` named instance, with state in `~/.hyadex-instances/mac-app` and its
configuration under `~/.config/hyadex-server-instances/mac-app`; it chooses an
available local port. Existing installed/development servers are not restarted
or adopted. Only one app may own this instance at a time.

Settings → 服务器: enter the address (for example `http://127.0.0.1:7870`, a LAN
address or a Tailscale `100.x` address) and the access token, then 连接. The token
is kept in the login keychain and is sent only in the `Authorization` header. Use
`./install.sh --show-token` on the server to display it. A failed connection or
disconnect returns to the connection screen, with no sample chats or simulated
replies. The production app has no demo mode.

## Using the app

- **Sidebar tabs.** Work (chats and scheduled jobs), Group (group discussions) and
  Autopilot (runs, split into in progress and finished). The `+` next to search
  creates an item of the current tab. Each tab remembers its last selection, and
  an orange dot marks a tab with something waiting for you. ⌃⌘1–3 switch tabs,
  ⌘K jumps anywhere.
- **Conversation.** A sent message appears at once with a live status line
  (starting, thinking with the provider's live reasoning summary when it sends
  one, running a command or editing a file, with elapsed time). Each turn's work
  (thinking, tool calls, answered approvals) folds into one "已处理 12 秒 · 运行 2
  条命令" row that expands to the steps, so answers, changed files and pending
  approvals stay in view. Messages sent while the agent works queue up: each can
  be edited, withdrawn, moved (⋯) or sent now (立即发送: Codex takes it into the
  running turn when it can, otherwise the turn stops and it runs next). After a
  Stop, queued messages are held until 继续发送. Approvals are inline (Allow,
  Always allow for this chat, Deny). Return sends, Shift-Return adds a line, Esc stops.
  The composer's "+" holds 添加文件…, 侧问 and 设定目标… (in a group also the answer
  mode, 只分析 and 添加成员); its bottom row shows the permission mode, the context
  ring, and the model and effort.
  Files go with the next message: "+" › 添加文件…, drop them anywhere on the
  conversation, or ⌘V a screenshot or files copied in Finder; each uploads at
  once and shows its progress. Text, including text that carries a picture of
  itself (Numbers, Word), still pastes as text.
  "加载更早的消息" at the top pages back through long chats.
  Model, effort and permissions are set per chat from the composer, with the
  providers' own names (Claude Code's Manual / Accept edits / Plan / Auto /
  Don't Ask / Bypass Permissions; Codex's Read Only / Default / Full Access).
  A ring next to them shows how full the chat's context window is.
- **Usage.** The bottom of the sidebar shows each provider account's plan usage
  as its CLI reports it; click it for every window and its reset time.
- **Messages.** Agent replies render GitHub-flavoured Markdown (headings,
  nested and task lists, tables, quotes, highlighted code with copy), TeX math
  (`$…$`, `$$…$$`, `\(…\)`, `\[…\]`) and images. Your own messages show as typed.
  Everything is native: the `HyadexRichText` target has its own parsers and
  lays math out with CoreText in the system's STIX Two Math font, with no
  third-party packages.
- **Media.** Images and files attached to a turn or published by an agent show
  in the conversation; click to open them.
- **Groups.** Laid out like a chat. A sent message shows at once; each member
  answering gets its own live status line (thinking, the running command…).
  Each reply shows who answered, its work folded into one row ("已处理 9 秒 ·
  运行 1 条命令"), the answer, and the files it changed with their diffs. A
  member's approval appears inside its turn. The composer holds the members
  (model, effort, permission mode and context per member; add or remove
  members), the answer mode (sequential or parallel) and analysis-only. The
  work comes from each member's own chat, matched to the group by dispatch.
- **Autopilot.** Create a run with its approval, isolation and reviewer options;
  follow the phase, the plan and each task's verification; approve, give
  feedback, pause, resume or abort.
- **Terminal.** ⌃` or the toolbar's terminal button opens the chat's persistent
  terminal under the conversation: one tmux session per chat on the server, in
  the chat's working directory, which the agent can also inspect. It is a real
  terminal (colours, full-screen programs such as vim or htop, input methods,
  copy and paste, scrolling back through tmux history), with tabs for its tmux
  windows, new windows, splits, and closing a pane or the whole session. Hiding
  the panel only detaches; commands keep running, and reopening it repaints the
  current screen. Drag its top edge to change the height.
- **Goals.** + › 设定目标… in the composer (or typing `/goal …`, `/goal clear`) sets the
  provider's native goal: Claude Code's `/goal` completion condition, checked
  after every reply until it holds, or Codex's persistent objective with an
  optional token and time budget. While a goal is set, a bar above the composer
  shows its state (in progress, achieved, paused, budget used up), Claude's check
  count and last reason or Codex's tokens and minutes, with pause/resume (Codex),
  edit and clear. In a group, 目标 sets the goal on one member and announces it;
  the bar follows that member's goal.
- **Commands and skills.** Typing `/` in a chat lists the commands and skills the
  chat's agent found for its folder (Claude Code's commands and skills, Codex's
  skills), filtered as you type; ↑↓ choose, ↩ or ⇥ complete. A message that
  starts with one is sent as that native command or skill.
- **MCP servers.** ⓘ lists the MCP servers the chat's agent loaded from its own
  configuration, with their state and tool count. In Claude chats a server can
  be reconnected, disabled or enabled again, and failed servers retried; servers
  that need a sign-in are signed in with `/mcp` in Claude Code itself. Codex
  servers show their state once the chat's thread is running.
- **Side questions.** + › 侧问 in the composer (⇧⌘B, or typing `/btw …`) opens a
  panel beside the conversation for quick questions that neither interrupt the
  agent nor enter its transcript: Claude Code's native `/btw`, or for Codex an
  answer from a fork of its thread.
- **Search.** The sidebar search filters chat titles as you type and, after a
  pause, searches every chat's messages; a hit opens the chat at that message
  (loading older history if needed) and highlights it.
- **Managing chats.** Right-click a chat or group (or the 会话 menu) to mark it
  unread, fork it (a copy with its history that the agent continues natively,
  ⇧⌘D), rename it (⇧⌘R), archive it (⇧⌘E; archived items collect under 已归档)
  or delete it (⌘⌫, after a confirmation).
- **Scheduled jobs.** + in 定时任务 creates a job for a chat or a group: daily,
  weekdays, weekly, every N minutes/hours/days, once, or a cron expression, in
  this Mac's time zone, with a preview of the next run. Double-click to edit;
  right-click to run now or delete. 上次结果 shows how the last run actually
  ended (成功, 失败 with the reason on hover, 已停止, 运行中, or 已派发 for a group),
  whether it ran on schedule or by hand.
- **Chat settings.** 高级设置… (right-click a chat, or ⓘ) edits the chat's system
  prompt, its subagent limit (or the agent's own setting), the automatic title,
  and whether the agent may create and change scheduled jobs itself, may only
  view them, or may not use them.
- **Group settings.** 讨论组设置… (right-click a group, or ⓘ › 编辑设置…) sets who
  answers a message that mentions nobody, how many agent hand-offs a round
  allows (0–12), and the shared brief every member receives once per version.
- **Folders.** New chats and groups pick their working directory on the server:
  type a path or step through the folders it lists. A Finder picker is offered
  only when the server runs on this Mac.
- **Emergency alerts.** An agent's emergency alert appears as a red banner over
  every conversation and as a macOS notification, with a red mark on its chat,
  until you acknowledge it.
- **Subagents.** ⓘ lists the helper agents a chat's agent started, with their
  state and recent activity.
- **Context.** Click the context ring to compact the conversation now (Codex
  natively; Claude Code with `/compact`).
- **Settings.** 字号 (标准 or 大) scales all text. ⌘↩ to send (↩ then adds a line), opening each turn's work by
  default, and the plan-usage footer apply at once. 通知 shows whether macOS lets
  Hyadex notify (with a shortcut to System Settings when it does not) and
  chooses notifications for approvals and finished replies, only while Hyadex is
  in the background, and the Dock badge (chats waiting for you plus emergencies).
  会话 › 为此聊天新建定时任务… (⇧⌘J) opens a job for the open chat.
- **Changes and details.** There is no permanent side column. A change card, a
  group receipt or a task's "查看 diff" slides a changes panel over the
  conversation (Esc closes it); the toolbar button shows the file count and is
  disabled when nothing changed. ⓘ shows the selection's settings and usage,
  including the tokens a chat has used across its turns (input with its cached
  part, and output). In the changes panel, 在编辑器中打开 opens the file in its
  default app: in place when the server runs on this Mac, otherwise a read-only
  copy downloaded from the server.

## How the client uses the server

- Lists (chats, groups, Autopilot runs, jobs) are polled every 4 seconds.
- The open chat or group is live over its WebSocket event stream and resumes after
  the last applied `seq`. Raw events are projected into timeline items by
  `SessionProjection` and `GroupProjection` (`HyadexCore/EventProjection.swift`).
- Turns are sent with `claude_sdk_interactive_v1` and `codex_interactive_v1`, so
  native approvals appear inline and are answered through
  `/api/sessions/{id}/{claude|codex}/interactions/{id}/resolve`.
- An open Autopilot run is polled; its progress notes come from its group timeline.
- The terminal attaches over `/api/sessions/{id}/terminal/ws` (`hyadex-terminal-v1`):
  raw PTY output in binary frames, keystrokes back as binary frames, and JSON
  frames for resize and tmux history scrolling. tmux windows come from
  `/terminal/windows` and change through `/terminal/action`.
- Emergency alerts come from `/api/emergency-alerts/events` (`hyadex-emergency-v1`:
  a snapshot, then changes) and are acknowledged with
  `POST /api/sessions/{id}/emergency/acknowledge {through_alert_id}`.
- Uploads are `POST /api/sessions/{id}/files` (multipart, streamed from disk) and
  go with the turn as `file_ids`. Older history is `GET /api/sessions/{id}?before=`.
- The queue uses `queue/{id}/run-now`, `queue/{id}/move` and `queue/resume`; sends
  to one chat go out one at a time so the server queues them in typed order.
- Side chats use `/side-chat` with the `X-Hyadex-Token` header only (the server
  rejects a bearer there) and refresh on the `side_chat_updated` frame.
- Search is `/api/search`; fork is `POST /fork`; unread is `POST /unread`;
  subagents are `GET /subagents` (polled while the chat works: Claude reports them
  only there); jobs are `POST/PATCH/DELETE /api/jobs` and `/api/groups/{id}/jobs`;
  folders come from `/api/working-directories/complete`.
- Goals use `/claude/goal` and `/codex/goal` (PUT/DELETE) and
  `PUT /api/groups/{id}/goal`; their state is read from `/claude/runtime` and
  `/codex/runtime` and reread after `claude_goal_changed`, `codex_goal_updated`
  and `codex_goal_cleared` events. Commands come from `/provider-commands`; a
  message starting with one carries its opaque `skill_selection`, and a 409 for a
  changed list reloads it and resends once. MCP state comes from `/claude/mcp`
  (also POST for control) and `/codex/mcp`.
- Chat settings are read from the full chat record and saved with
  `PATCH /api/sessions/{id}`; group settings and the brief with
  `PATCH /api/groups/{id}` (`null` restores a default). A changed file opens
  through `GET .../workspace/file` when the server is remote. Token totals and
  job outcomes come with the polled lists.

## Code layout

| Target | Role |
| --- | --- |
| `HyadexAPI` | REST client, WebSocket event stream, keychain token store, JSON values. |
| `HyadexCore` | `AppModel` (@Observable), local server lifecycle, live controller, event projections and models. |
| `HyadexRichText` | Message rendering: Markdown and TeX parsers, math layout, syntax highlighting. |
| `HyadexTerminal` | Terminal emulator (xterm subset for tmux), its CoreText view and the PTY socket. |
| `HyadexUI` | SwiftUI views and design tokens. |
| `HyadexApp` | Entry point: window, settings, menu bar extra, commands, development checks. |

Chats, groups and runs share one state vocabulary (`ActivityState`), so colours
and words mean the same thing everywhere.

## Development checks

Launch these through LaunchServices (`open -n -W --env …`); a binary started
directly from a shell may not receive windows.

- **Screenshots:** `--env HYADEX_SNAPSHOT_DIR=/tmp/shots` renders each main screen
  to PNG and quits (`HYADEX_SNAPSHOT_ONLY=<name>` selects scenes). System glass
  materials do not render in these images. Only this explicit development path
  loads sample fixtures; ordinary startup and connection failures never do.
  `HYADEX_SNAPSHOT_SIDEBAR=1` draws an opaque copy of the sidebar over its glass
  column and shows the footer as connected; the README screenshots
  (`docs/images/`) are made this way, with a separate `APP_BUNDLE_ID` so a
  running copy of the app is not disturbed.
- **Local startup acceptance:** use a separate `APP_BUNDLE_ID` when building,
  then launch with `HYADEX_LOCAL_SERVER_HOME` set to a temporary directory and
  `HYADEX_LOCAL_SERVER_INSTANCE` set to a unique test name. `HYADEX_LOCAL_SERVER_SOURCE`
  overrides the source checkout; `HYADEX_LOCAL_SERVER_PORT` can fix a test port.
  `HYADEX_BACKGROUND_LAUNCH=1` avoids requesting focus at launch. Bundle IDs
  isolate both app preferences and Keychain service names. Exercise the normal
  startup button without `HYADEX_SERVER_URL` or `HYADEX_TOKEN` overrides.
- **Layout stress:** `HYADEX_SNAPSHOT_ONLY=stress` with `HYADEX_STRESS_WIDTH=900`
  switches tabs and the inspector rapidly in a narrow window.
- **End to end:** with `HYADEX_SERVER_URL` and `HYADEX_TOKEN` pointing at a
  development server, `HYADEX_E2E_DIR=/tmp/e2e` drives a Claude turn and a Codex
  turn with an approval each and an `@codex` group message;
  `HYADEX_E2E_AUTOPILOT=1` adds a job toggle and a one-task Autopilot run (`=only`
  skips the chats). `HYADEX_E2E_TERMINAL=1` (or `=only`) types commands into the
  Codex test chat's terminal and checks output, colours, CJK text, resizing, tmux
  windows and reattaching. It expects chats titled "mac e2e …", a group titled
  "mac e2e group" and a job titled "mac e2e …". Agents run for real and use tokens.
  `HYADEX_E2E_CAPABILITIES`, `HYADEX_E2E_MANAGE` and `HYADEX_E2E_EXTRAS` (each `=only`
  to run alone) use chats titled "features e2e claude" / "features e2e codex":
  goals, commands and MCP; attachments, queue, history, compaction, emergency
  alerts, jobs, folders, rename/archive/delete; unread, search, side chat, fork
  and subagents. `HYADEX_HISTORY_PAGE=10` shortens the first page so paging is
  exercised. A live check shows its window without taking keyboard focus; launch
  it with `open -g` so typing in another app cannot reach the composer.
- **Real controls:** `HYADEX_E2E_UI=only` drives the same features through the
  app's own controls (`HyadexApp/UIDriver.swift`): buttons, rows and popover
  items are pressed through the app's accessibility tree, text is typed into the
  AppKit text view behind each field (`insertText`, where key presses end),
  menu shortcuts go through the main menu's key-equivalent handling, and Return,
  arrows and Esc go to the focused field. It needs no system permission and
  never moves the pointer or takes the keyboard; the window is placed off
  screen. Menus are chosen while they track (as a click would), and a file drop
  goes through the drop region's own drag callbacks. `=cmdreturn`, `=settings`,
  `=menus`/`=drop` and `=picker` run single steps. `=gaps` covers job outcomes,
  token totals, chat and group settings, 在编辑器中打开 and ⌘V attachments. It
  expects chats titled "gaps e2e claude" (with a system prompt and a changed
  `app/main.py`) and "gaps e2e codex", a group "gaps e2e group", and jobs
  "gaps e2e ok", "gaps e2e fails" and "gaps e2e stopped" that last ran that way.

## Verified

Local startup was exercised through the actual app's home button and Settings
against an isolated, real backend: authenticated connection, repeated selection
of the local server, Settings connection test, disconnect/reconnect, creation of
a chat, graceful app exit stopping the owned process, and restarting with that
chat preserved. A missing-source launch showed a retryable error and no sample
content. The 82 Swift tests include process ownership/cancellation and a real
isolated runner; these checks do not repeat provider-turn acceptance below.
The release-configuration package (0.1.0, build 202610281141, source 803bb1e2)
compiled and passed signature verification. Its separate UI recheck is pending
because native automation was unavailable; the local-startup UI checks above
used the debug build.

Against an isolated development server (API contract 29) with real Claude Code
and Codex: sending turns, live updates, approvals for both providers, replies
and diffs, an `@codex` group round, toggling a job, and a one-task Autopilot run
from plan approval to commit. `HYADEX_E2E_CAPABILITIES=only` covers goals, commands
and MCP the same way: MCP lists for both agents and disable/enable/reconnect in
Claude, the command lists and `/` suggestions, a Codex skill turn, a Claude
`/goal` reaching 已达成 and a second one cleared while active, a Codex goal with
budgets completing and another paused and cleared, and a group goal set on and
cleared from its member. `HYADEX_E2E_MANAGE=only` covers an attachment the agent
reads, sends kept in typed order, reordering and Force Send, Stop holding the
queue while a new message still runs and 继续发送 releasing it, paging back to a
chat's first event, Codex and Claude compaction (Claude's context dropped from
33 k to 9 k tokens in the same session), an agent's emergency alert arriving and
being acknowledged, chat and group jobs created, edited and deleted, server
folder completion, and rename/archive/unarchive/delete of a chat and a group.
`HYADEX_E2E_UI=only` repeats the main paths through the real controls (98
checks): a right-click menu and a menu button chosen from, a file dropped on the
conversation that the agent then reads, the paperclip opening the system file
panel, typing and Return/⌘↩ in the composer, slash completion with the
keyboard, the side panel, the goal sheet and bar, compaction from the context
ring, ⇧⌘U/⇧⌘D/⇧⌘J/⇧⌘R/⇧⌘E/⌘⌫/⌘, through the menu, the delete confirmation,
the new-chat sheet's server folders, Esc holding the queue and 继续发送 / 立即发送,
the search field and its hit, the details popover, approving on the card and
acknowledging the emergency banner. `HYADEX_E2E_EXTRAS=only` covers marking unread, content search including a hit
in older history, side questions on both agents (the main conversation
unchanged), forks of both agents that remember the original conversation, and
a Claude subagent listed while it runs. Unit tests cover the event projections
with event logs recorded from that server.

`HYADEX_E2E_UI=gaps` (44 checks, real Claude Code and Codex, isolated server):
the jobs table showing 成功, 失败 with Claude's own reason, and 已停止 for jobs
that completed, failed on an unknown model and were stopped; a chat's token
totals in ⓘ; 高级设置… loading the stored system prompt, a new prompt and the
automatic title saved, Claude then answering in the prompt's language, and an
emptied prompt clearing it; 在编辑器中打开 resolving a changed file through the
diff's repository root, and the server copy a remote client would download; a
clipboard image pasted with ⌘V, uploaded and read by Claude (after its Read
approval), with text still pasting as text; and 讨论组设置… saving responder,
hand-off limit and brief, kept after a list refresh, then restoring the defaults.
Not exercised: the remote-server branch of 在编辑器中打开 through the button (the
test server runs on this Mac; the download itself was checked), and ⌘V through
the Edit menu with the mouse (the shortcut is intercepted; the menu item pastes
text only).

## Known gaps

- Autopilot receipts list changed files without diffs in the inspector.
- Assistant text arrives as whole messages (the server does not stream tokens).
- Math covers the LaTeX agents usually write (fractions, roots, scripts, big
  operators, delimiters, accents, matrices, cases, aligned); an unsupported
  command shows the formula's source instead.
- Not exercised end to end yet: manually connecting through the Settings form
  with a remote token (keychain), creating groups from their sheet, Stop and
  queued-message editing on a live server, and reconnection after an external
  server restart. Managed local startup/reconnection and chat creation are
  verified above.
- Ad-hoc signing only; the keychain may ask again after each rebuild.
- Slash suggestions, attachments and side questions are offered in chats, not in
  group messages (the server's group message has no attachments).
- Search opens a chat at its newest matching message; the server reports one
  hit per chat.
- A subagent's own transcript cannot be opened (the server has no route for it);
  its recent activity lines are shown.
- Not driven by the automated checks: choosing a file inside the system file
  panel (macOS draws it in a separate service that only takes real input) and
  answering the system's notification permission prompt (Settings › 通知 shows
  the current permission and opens System Settings when it is off). A drop is
  delivered through the destination's own drag callbacks, not a pointer drag.

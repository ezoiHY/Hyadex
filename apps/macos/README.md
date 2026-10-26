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
- A Hyadex server you can reach (same Mac, LAN or Tailscale) and its access token.

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

## Connect to a server

Settings → 服务器: enter the address (for example `http://127.0.0.1:7870`, a LAN
address or a Tailscale `100.x` address) and the access token, then 连接. The token
is kept in the login keychain and is sent only in the `Authorization` header. Use
`./install.sh --show-token` on the server to display it. Without a connection
the app shows built-in demo data.

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
  approvals stay in view. Queued messages are editable and withdrawable;
  approvals are inline (Allow, Always allow for this chat, Deny). Return sends, Shift-Return adds a line, Esc stops.
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
- **Groups.** Member status, `@` completion, sequential or parallel answers,
  analysis-only rounds, file receipts and Stop.
- **Autopilot.** Create a run with its approval, isolation and reviewer options;
  follow the phase, the plan and each task's verification; approve, give
  feedback, pause, resume or abort.
- **Changes and details.** There is no permanent side column. A change card, a
  group receipt or a task's "查看 diff" slides a changes panel over the
  conversation (Esc closes it); the toolbar button shows the file count and is
  disabled when nothing changed. ⓘ shows the selection's settings and usage.

## How the client uses the server

- Lists (chats, groups, Autopilot runs, jobs) are polled every 4 seconds.
- The open chat or group is live over its WebSocket event stream and resumes after
  the last applied `seq`. Raw events are projected into timeline items by
  `SessionProjection` and `GroupProjection` (`HyadexCore/EventProjection.swift`).
- Turns are sent with `claude_sdk_interactive_v1` and `codex_interactive_v1`, so
  native approvals appear inline and are answered through
  `/api/sessions/{id}/{claude|codex}/interactions/{id}/resolve`.
- An open Autopilot run is polled; its progress notes come from its group timeline.

## Code layout

| Target | Role |
| --- | --- |
| `HyadexAPI` | REST client, WebSocket event stream, keychain token store, JSON values. |
| `HyadexCore` | `AppModel` (@Observable), live controller, event projections, models, demo data. |
| `HyadexRichText` | Message rendering: Markdown and TeX parsers, math layout, syntax highlighting. |
| `HyadexUI` | SwiftUI views and design tokens. |
| `HyadexApp` | Entry point: window, settings, menu bar extra, commands, development checks. |

Chats, groups and runs share one state vocabulary (`ActivityState`), so colours
and words mean the same thing everywhere.

## Development checks

Launch these through LaunchServices (`open -n -W --env …`); a binary started
directly from a shell may not receive windows.

- **Screenshots:** `--env HYADEX_SNAPSHOT_DIR=/tmp/shots` renders each main screen
  to PNG and quits (`HYADEX_SNAPSHOT_ONLY=<name>` selects scenes). System glass
  materials do not render in these images.
- **Layout stress:** `HYADEX_SNAPSHOT_ONLY=stress` with `HYADEX_STRESS_WIDTH=900`
  switches tabs and the inspector rapidly in a narrow window.
- **End to end:** with `HYADEX_SERVER_URL` and `HYADEX_TOKEN` pointing at a
  development server, `HYADEX_E2E_DIR=/tmp/e2e` drives a Claude turn and a Codex
  turn with an approval each and an `@codex` group message;
  `HYADEX_E2E_AUTOPILOT=1` adds a job toggle and a one-task Autopilot run (`=only`
  skips the chats). It expects chats titled "mac e2e …", a group titled
  "mac e2e group" and a job titled "mac e2e …". Agents run for real and use tokens.

## Verified

Against an isolated development server (API contract 29) with real Claude Code
and Codex: sending turns, live updates, approvals for both providers, replies
and diffs, an `@codex` group round, toggling a job, and a one-task Autopilot run
from plan approval to commit. Unit tests cover the event projections with event
logs recorded from that server.

## Known gaps

- Opening a chat loads its latest 400 events; older history is not paged yet.
- Autopilot and group receipts list changed files without diffs in the inspector.
- Assistant text arrives as whole messages (the server does not stream tokens).
- Math covers the LaTeX agents usually write (fractions, roots, scripts, big
  operators, delimiters, accents, matrices, cases, aligned); an unsupported
  command shows the formula's source instead.
- Not exercised end to end yet: connecting through the Settings form (keychain),
  creating chats or groups from their sheets, Stop and queued-message editing
  on a live server, and reconnection after a server restart.
- Ad-hoc signing only; the keychain may ask again after each rebuild.

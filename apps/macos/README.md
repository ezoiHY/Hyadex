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
- **Conversation.** Messages, thinking, tool calls with output, changed files,
  queued messages (editable, withdrawable) and inline approvals (Allow, Always
  allow for this chat, Deny). Return sends, Shift-Return adds a line, Esc stops.
  Model, effort and permissions are set per chat from the composer.
- **Groups.** Member status, `@` completion, sequential or parallel answers,
  analysis-only rounds, file receipts and Stop.
- **Autopilot.** Create a run with its approval, isolation and reviewer options;
  follow the phase, the plan and each task's verification; approve, give
  feedback, pause, resume or abort.
- **Inspector.** Changed files with diffs, and details of the selection.

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
- Not exercised end to end yet: connecting through the Settings form (keychain),
  creating chats or groups from their sheets, Stop and queued-message editing
  on a live server, and reconnection after a server restart.
- Ad-hoc signing only; the keychain may ask again after each rebuild.

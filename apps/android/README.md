# Hyadex for Android

A phone client for a Hyadex server: your Work chats with Claude Code and
Codex, live, in the Mac app's style. It talks to the same REST and WebSocket
API as the Mac app and keeps nothing but the server address and token.

## Install

```bash
cd apps/android
scripts/build-apk.sh     # dist/Hyadex.apk, about 3.8 MB
adb install -r dist/Hyadex.apk
```

The script needs the Android SDK (Android Studio installs it) and uses Android
Studio's JDK when `JAVA_HOME` is not set. The release APK is signed with the
local debug key, so it installs directly; publishing would need a release key.

## Connect

On the Mac, open Settings → 服务器 and turn on **允许手机连接**: the Mac app's
backend then listens on the LAN and tailnet at port 7870 and shows a QR code.
On the phone, tap **扫码连接** and scan it. The camera is asked for only then;
without it, copy the link and use **粘贴连接链接**, or type the address and
token:

| Where the phone is | Address |
| --- | --- |
| Same Wi-Fi or LAN | `<LAN address>:7870` |
| Anywhere else | `<Tailscale address>:7870`, with the phone on your tailnet |

If the Mac's firewall is on, macOS asks whether Python may accept incoming
connections; allow it, or the phone cannot reach the Mac.

Opening a `hyadex://connect?address=…&token=…` link on the phone fills in the
form the same way. Connecting stays your tap, and a link that arrives while the
app is connected asks before switching servers.

Plain HTTP is allowed because the address is yours to choose (Tailscale already
encrypts its traffic). The token is encrypted with a key that stays in the
Android Keystore. Never expose the server's port to the internet.

## What it does

- **Chats.** Work chats by project, with search, pinned chats, unread and
  running marks, and pull to refresh. Long press to pin, mark unread, rename or
  archive; swipe a chat left to archive it (撤销 undoes it); + starts a chat
  with Claude or Codex in a server folder.
- **Moving around.** A chat slides in over the list. The back gesture shows the
  list behind it as you swipe (predictive back) and springs back if you change
  your mind. When you have scrolled up, a 新消息 / 回到最新 pill takes you to
  the latest.
- **Timeline.** The Mac app's projection of the event log: thinking and tool
  calls fold into one "已处理" row per run of work, answers render as Markdown
  with highlighted code, images open full screen in the app, and older history
  loads as you scroll up.
- **Live.** The open chat streams its events, the provider's live reasoning
  summary and the answer being written. "正在思考 / 正在运行" shows what the
  agent is doing.
- **Composer.** Send text and images (photos go up as JPEG of at most 2560 px),
  stop a turn, withdraw queued messages, and switch the permission preset,
  model and effort in the provider's own terms. After a stop, queued messages
  wait until 继续发送.
- **Approvals and questions.** Commands, file changes, permissions, Codex
  questions and MCP forms are answered in place with the provider's own choices.
- **Other chats' answers.** When the agent asks another chat in its project, a
  card shows the question and the answer; a deep question waits for 允许,
  拒绝 or 这个聊天以后都允许.
- **Connection.** A banner shows while the stream reconnects. A refused token
  or a chat that was archived or deleted elsewhere ends the stream with a note.
- **Away from the app.** While a chat is still working and the app is out of
  sight, a notification says when the reply is done or an approval is waiting;
  tapping it opens the chat.

Not yet: groups, Autopilot, scheduled jobs, side questions, asking another
chat yourself with @, the terminal, and diffs beyond the changed-file list.

## How it is built

One Gradle module (`app`), Kotlin and Jetpack Compose, no dependency injection,
database or navigation library:

| Package | Role |
| --- | --- |
| `api` | OkHttp REST client, the event stream (`hyadex-events-v1`, resumed by `seq`), lenient JSON reads |
| `core` | Ports of the Mac app's `SessionProjection`, live text, interactions and row grouping; no Android types, unit tested on the JVM |
| `app` | `AppViewModel` (the one state holder), the encrypted server store, `RunWatchService` |
| `ui` | Compose screens and the design tokens (`ui/theme`) |

Kept light on the phone:

- Nothing polls and no socket stays open in the background; returning to the
  app resumes the open chat's stream after the last applied `seq`. Rotating
  the phone keeps the stream.
- In the app, the list polls a summary of each chat every 4 seconds (12 while a
  chat is open) and the full records every 15th time.
- A burst of events (a replay, a fast stream) is folded into one rebuild per
  frame, built off the main thread. Long answers are parsed off the main
  thread and cached; tool input and output kept in memory are cut to 4,000
  characters.
- Animations run in the draw phase, and the list stops drawing while a chat
  covers it.
- The notification watch runs only while a chat is working and the app is out
  of sight, polls every 5 seconds, and stops when nothing is busy (at most two hours).
- Images are cached in memory and on disk and decoded at the size shown;
  photos to send are decoded two at a time.
- Release builds are shrunk by R8 (3.8 MB instead of 68 MB).

The projection follows the Mac app (`apps/macos/Sources/HyadexCore`): change
both together, and port their tests (`app/src/test`) with them.

## Development

```bash
./gradlew testDebugUnitTest    # projection and row tests
./gradlew assembleDebug        # app/build/outputs/apk/debug/app-debug.apk
```

Test against an isolated server (`scripts/dev_server.py --instance NAME`), never
an installed one. In the emulator, `adb reverse tcp:PORT tcp:PORT` and the
address `127.0.0.1:PORT` reach a server on this computer.

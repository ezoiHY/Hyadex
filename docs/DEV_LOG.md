# Hyadex development log

Newest first. Hyadex started from the AgentsDock server source (see
[NOTICE](../NOTICE)); its own history begins with the first version below.

## 2026-10-30 — Pair a phone by QR code; Android review fixes, motion and answers from other chats

- **Mac: 允许手机连接.** A switch in Settings → 服务器 restarts the app-owned
  backend on every interface at port 7870 (otherwise it stays on this Mac, on a
  random port) and shows a QR code for each LAN or Tailscale address. The code
  holds `hyadex://connect?address=…&token=…`.
  - If another server already answers on 7870 (an installed one, say), the
    switch says so and the backend keeps running as it was. A restart that
    fails starts the backend again as before.
  - The code always comes from the app's own backend, and switching no longer
    moves you off another server you are using.
  - A fixed port still in TIME_WAIT after a restart is reused.
- **Codex answers from other chats (both apps).** When a Codex chat asked
  another chat, its card showed the raw MCP result; it now shows the answer.
- **Android: pairing.**
  - 扫码连接 scans the Mac's code, and 粘贴连接链接 reads a copied link. Opening
    the link on the phone works too.
  - The form fills in and connecting stays your tap. A link that arrives while
    the app is connected asks before switching servers.
  - The camera is asked for only when you scan. If you refuse, the screen says
    how to allow it or paste the link; the scanner's misleading "camera
    problem" dialog no longer appears.
  - The scanner's prompt sits above the navigation bar; before, it was hidden
    under it.
- **Android: motion.**
  - A chat slides in over the list. The predictive back gesture shrinks it
    toward your finger with the list behind, and it springs back if you cancel.
  - Swipe a chat left to archive it, with 撤销 in the snackbar.
  - A 新消息 / 回到最新 pill appears when you have scrolled up.
  - Rows fade in and move into place, folds open with a spring, and the send
    button and chevrons animate.
  - The splash screen uses the app's background, light or dark (Android 12+).
- **Android: answers from other chats.** When the agent asks another chat, a
  card shows the question and the answer, marked 深度 for a deep one. A deep
  question waits in the timeline for 拒绝, 允许 or 这个聊天以后都允许, and the
  status line says 正在询问.
- **Android: fixes from a review.**
  - Taps on an open chat no longer reach the list underneath.
  - After Stop, queued messages show as paused with 继续发送; loading older
    history no longer shows them twice.
  - Sending while an image is still uploading, or after it failed, keeps the
    text and says why.
  - An approval cannot be sent twice: the card shows your choice at once and
    goes back if sending fails.
  - Connection:
    - A banner shows while the stream reconnects.
    - A refused token or a chat that is gone ends the stream.
    - A chat archived or deleted elsewhere closes with a note.
    - The list says when the server cannot be reached, and settings offer
      重新连接.
  - Rotating the phone keeps the open chat and its stream.
  - A notification or link is not handled again when the app is restored.
  - Notifications:
    - None while notifications are turned off.
    - 回复完成 only when the chat wrote a new reply.
    - Stopped and failed turns say so.
  - Long model names no longer push the send button off the screen.
- **Android: lighter.**
  - The list polls a summary of each chat and fetches the full records only
    every 15th time. It stops drawing while a chat covers it.
  - Running marks and shimmer animate in the draw phase only.
  - Long answers are parsed off the main thread and cached. Tool input and
    output kept in memory are cut to 4,000 characters.
  - Photos are decoded two at a time.
  - The stream reconnects with jittered backoff, which resets only after a
    stable connection.
  - The token is read from the Keystore off the main thread.
  - Touch targets are larger, and insets cover landscape and cutouts.
- Verification:
  - 18 JVM unit tests, including:
    - consult cards with Codex's receipt;
    - a consult settled by Stop;
    - trimmed events;
    - a stop recorded twice;
    - pairing links in the form the Mac writes.
  - 126 Swift tests, including a taken phone port that leaves the backend
    alone.
  - The opt-in live test: with phone access on, the backend answered on this
    Mac's LAN address with the same token; after it was turned off, it did not.
  - The Mac's code, drawn by the same CoreImage code at its size, decodes to
    the exact link with the ZXing version the phone uses.
  - Android 17 emulator against an isolated `android2` instance:
    - a deep question approved from the card, with its answer;
    - Stop with a queued message, then 继续发送;
    - swipe to archive and 撤销 (exactly two requests);
    - the reconnect banner while the server was stopped and restarted;
    - rotation with no new socket;
    - predictive back;
    - pairing by link;
    - the camera refused, then allowed;
    - the R8 release build paired and opened a chat.
- Not verified:
  - A real phone.
  - Scanning a code with a real camera (the emulator's camera cannot show one).
  - The Mac pairing panel on screen: it is built and tested, but no one has
    looked at it.
- Not yet:
  - The server resends a live answer's whole text every 50 ms. An incremental
    frame would cut traffic on long answers.
  - A deep question waiting in a chat that is not open shows only once you open
    that chat.
  - No baseline profile.

## 2026-10-30 — Android app (first version)

- **New client: `apps/android`.** Kotlin and Jetpack Compose in one Gradle
  module, in the Mac app's style (light and dark), built with
  `apps/android/scripts/build-apk.sh` into a 3.5 MB APK.
  - Connect with an address and token; the token is encrypted with an Android
    Keystore key.
  - Work chats by project, with search, pinned, unread and running marks, pull
    to refresh. Long press to pin, mark unread, rename or archive; + creates a
    Claude or Codex chat in a server folder.
  - The timeline ports the Mac app's projection and row grouping: one "已处理"
    row per run of work, Markdown with highlighted code, images opened full
    screen in the app, older history paged in as you scroll up.
  - Live: the open chat's event stream with the reasoning and answer lanes;
    the status line shows thinking or the running tool.
  - Composer: text and images, Stop, queued messages, and the permission
    preset, model and effort in the provider's own terms.
  - Approvals, Codex questions and MCP forms answered in place.
  - While a chat works and the app is out of sight, a notification says when
    the reply is done or an approval waits; tapping it opens the chat.
- **Kept light.**
  - No polling or socket in the background; the stream resumes after the last
    `seq`.
  - Event bursts are folded into one rebuild per frame.
  - The notification watch runs only while a chat is busy, for at most two
    hours.
  - Photos are sent as JPEG of at most 2560 px.
  - Release builds are shrunk by R8.
- **Stopped turns (both apps).** A turn the user stopped shows as stopped,
  with the interrupted command failed. The server records it as `turn_finished`
  with `stopped` (Codex and Claude); before, the Mac app showed no divider and
  marked the command as succeeded. One stop recorded twice shows once. Covered
  by a new Swift test using the server's real event, with 123 Swift tests passing.
- Verification:
  - 14 JVM unit tests, ported from the Mac projection tests plus stop,
    approvals, subagents, live text, the status line, forms and rows.
  - Android 17 emulator against an isolated `android` instance, with real
    Claude and Codex:
    - A Claude file write approved from the card.
    - A Codex command with its live commentary.
    - An uploaded image that Claude described correctly.
    - Stop mid-command.
    - The app sent to the background mid-turn, catching up after `after=seq`.
    - A "done" notification that opened its chat.
    - New chat and rename; history paging (page size 6, four pages); dark mode.
    - The R8 release build (list, history, images, a live turn).
- Not yet:
  - Groups, Autopilot, scheduled jobs, side questions and @ questions.
  - The terminal, full diffs, and QR pairing (the Mac app shows no code yet).
  - No real phone tested.

## 2026-10-29 — Images: Codex generations shown, work rows timed, in-app preview

- **Codex image generation.** A finished `imageGeneration` item is published
  as a chat image. The server uses the saved file under
  `~/.codex/generated_images`, or decodes the base64 result when no file is
  saved. The tool row now shows the saved path, not truncated base64. The Mac
  calls the tool 生成图片.
- **Work rows (Mac).**
  - Each 已处理 row shows its own run's time, from the turn start (first run)
    or its first step, to the next shown item. Before, every run in a chat
    showed the whole turn's time.
  - An image the agent publishes mid-run shows after that run instead of
    splitting it in two.
  - Settled approvals and forms stay inside the run.
- **Claude thinking.** A Claude message whose thinking block never streamed no
  longer completes another message's block. That mismatch left a stray
  "partial" copy of a thought after the answer. Copies already in old logs are
  hidden.
- **In-app preview (Mac).** Clicking an image, video or PDF, or a link to a
  local one, opens Quick Look in the window instead of an outside app. SVG
  counts as an image.
- Verification:
  - New backend and Swift tests; the backend suite and 122 Swift tests pass.
  - Live, isolated `imagefix` instance:
    - Codex "生成一张猪咪的图片": the tool output was the saved path. An
      `artifact_created` image (1.9 MB PNG) came before the answer and showed
      inline, under "已处理 35 秒".
    - A Claude chat drew and published an SVG. Its runs showed 10 s and 19 s
      on their own, with no duplicate thought.
    - Pressing the Codex image opened the in-app preview; Preview.app did not
      start.
- Caveats:
  - Images Codex generated before this change are not added to old chats; the
    files stay in `~/.codex/generated_images`.
  - The original Claude duplicate was not reproduced live; a unit test covers
    the pairing.
  - A transparent SVG keeps its transparency, so dark strokes are hard to see
    in dark mode.

## 2026-10-29 — Ask another chat with @

- **Mac.** `@` in a chat's composer lists the chats in its project (Tab or
  Return completes `@「title」`).
  - A message that opens with a chat's @ asks that chat directly, 基础 or 深度
    (a bar above the box says it is not sent to this chat's agent).
  - The answer arrives in a 你问「…」 card; 转给 <agent> quotes it into the
    composer.
  - An @ later in a message goes to the agent as text; the agent instructions
    now say `@「TITLE」` names a chat it can ask.
- **Server.** `GET /api/sessions/{id}/consult-targets` and
  `POST /api/sessions/{id}/ask` for clients. A question the user asks this way
  needs no approval and counts against no turn. The asking chat's log gets a
  `chat_consult_asked` event for its timeline, and its agent never sees it.
- Verification:
  - New backend and Swift tests; the backend suite and 119 Swift tests pass.
  - Live, isolated `mention` instance:
    - In the app, `@Back` listed "Backend B". The composer's own send button
      (pressed through accessibility) asked it, and the card showed "server.py
      uses 3 workers" after 7 s. The asking Claude chat's log held only the
      mark, and its agent never ran.
    - A mid-message `@「Backend B」` sent to a Claude chat made it ask Backend B
      through the Hyadex tool on its own.
  - Not verified: 转给 by hand.

## 2026-10-29 — Chats ask each other (basic and deep)

- **Chat consults.** A Work chat's agent can ask another chat in its project
  with the new `ask` helper (`ask list`; `ask TITLE` with the question on
  stdin; `--deep`).
  - Basic answers from that chat's context without tools: Claude's `/btw`, or
    a Codex fork with its shell tool off.
  - Deep sends a read-only fork to investigate the folder: Claude with only
    Read, Grep and Glob, no MCP servers, and its transcript removed after;
    Codex in a read-only sandbox that never asks.
  - Neither touches the asked chat's transcript, queue or turns, and an answer
    cannot ask another chat in turn.
  - Defaults: same project (or working directory); a deep question needs your
    approval (or "always" for that chat); per turn 5 basic and 2 deep; one deep
    fork per chat at a time; 2- and 5-minute limits; a 10-minute approval wait.
    A consult ends with its asking turn.
  - New modules: `features/consult_engine.py` (ledger, scope, limits,
    approvals, lifetime), `features/consult_runtime.py` (provider executors),
    `api/consults.py`, `providers/claude/consult.py`, `cli/ask.py`.
  - Codex side-chat fork settings moved into `codex_side_fork_settings`, shared
    by side chats and consults.
- **Mac.** A consult card in the timeline: whom, 基础 or 深度, a deep copy's live
  steps, the answer and its tokens, approval buttons (允许 / 总是允许 / 拒绝) and
  停止. A chat waiting for that approval counts as needing you. The asked
  chat's side panel lists its incoming questions and gains a 深度 switch for
  your own. Sidebar rows show a magnifying glass while a deep copy works.
  Chat settings gain 聊天互问.
- **Claude's Hyadex tool is always loaded.** With many MCP tools configured,
  Claude Code defers them behind tool search. The Hyadex tool's listing now
  carries `anthropic/alwaysLoad`.
- Verification:
  - 17 new backend tests (engine, routes); the backend suite and 117 Swift
    tests pass.
  - Live, isolated `consult` instance, real turns:
    - A Claude chat asked a Codex chat through the Hyadex tool, basic (3
      workers, 5 s) and deep after approval (2 commands, `notes/deploy.md`
      lines 2–3, 13 s).
    - A Codex chat asked a Claude chat, basic (`/btw`, 23 s) and deep after
      "always allow" (Grep, Glob, Read, 14 s).
    - The asked chats' event logs and the project's files were unchanged, and
      the Claude fork's transcript was removed.
    - Stop while an approval was pending cancelled the consult.
    - A deep question asked from the side panel answered in 14 s.
    - The app showed the cards and the side panel from that server.
  - Found on the way:
    - The tool refuses flags that abbreviate identity flags, so `--chat` (a
      prefix of `--chat-id`) became a positional argument.
    - Turns sent without the Mac's interactive capabilities run Claude in print
      mode, where only the helper CLIs exist.
  - Not verified: the full UI live checks; approving from the card by hand.

## 2026-10-29 — Mac: thoughts in view, selectable answers, projects, screenshots

- **Thinking status.** Claude sends its summarized thinking in bursts that
  finish as they arrive, so the live text was gone almost at once. The status
  line now keeps the turn's latest thought on one line (Codex: its newest step
  heading) until the next one or the answer. The provider's mark replaces the
  brain icon: Claude's spark turns, Codex's cloud breathes (still under Reduce
  Motion). Before the first thought arrives (about 5 s for Sonnet), the line
  shows only "正在思考".
- **Selectable answers.** Each paragraph was its own text view, so a drag
  stopped at the paragraph edge, and inline math copied as "￼". Paragraphs,
  headings, lists, quotes and display math now share one AppKit text view per
  run, so a drag selects across them. Copying gives Markdown-like plain text
  (math as `$…$`/`$$…$$`, list markers as `1.`/`-`/`- [x]`). Code blocks,
  tables and images keep their own views. A copy button under each finished
  answer, shown on hover, copies the whole answer as written.
- **Composer pictures.** Image attachments show as square thumbnails (click
  for Quick Look, × on hover, upload progress over the picture). Other files
  keep their name chips.
- **Screenshots.** A system-wide shortcut (default ⌥⇧S; 设置 › 通用 offers
  ⌃⇧S, ⌃⌥⌘S or off) starts the system's area selection. The picture goes into
  the open chat's composer. The composer's "+" menu and the menu bar have
  截图… too, and they move Hyadex aside first. It needs the Screen Recording
  permission; without it, the app asks once and says where to grant it.
- **Title and directory.** The title bar shows the provider's mark (or a
  group's member marks) and the title, without the subtitle. The working
  directory sits above the composer (long paths keep their last folders), with
  在 Finder 中显示, 拷贝路径 and the terminal.
- **Projects.** Work chats are grouped in the sidebar by project, then by
  working directory, the most recent first, and the home folder last.
  新建项目 (sidebar, File menu, or a chat's 移到项目 menu) takes a name and an
  optional directory, whose chats join it. Moving a chat sets its server-side
  `folder`, so other clients see it; the list of projects, empty ones
  included, is kept on this Mac per server. Sections collapse. A section's +
  starts a chat in its directory and project. 设置 › 通用 › 边栏按项目分组
  turns grouping off.
- **Menu bar icon.** It is grey while no server is connected and red for an
  emergency alert. A badge counts items waiting for you, and the icon pulses
  slowly while agents run. The menu also lists emergency alerts and says when
  it is not connected.
- **Unread dot.** A chat watched to the end of its reply got the unread dot
  as soon as you left it: the server marks a chat read when it is opened, and
  what arrived while it stayed open still counted as new. Leaving a chat now
  marks it read, and so does a reply that finishes while Hyadex is in front.
  Until the server's list agrees, the dot stays off. A chat marked unread by
  hand stays unread.
- **Toolbar and composer.** 侧问 and 设定目标 moved from the composer's "+"
  menu to the toolbar, beside the terminal. The goal button also serves a
  group, and it is tinted while a goal is open. "+" keeps 添加文件… and
  截图…. The model and the effort are two menus. The effort menu is hidden
  for a model without levels.
- **Sidebar headings.** Project and pinned headings are larger (13 pt,
  semibold) than the list's small captions.
- Verification:
  - In process against the isolated `features` instance, the toolbar's 侧问
    opened the chat's side panel and 设定目标 opened the goal sheet. The
    composer showed separate 模型 and 推理强度 menus, and "+" no longer
    listed 侧问. The UI live checks now press the toolbar buttons (not run
    in full).
  - Snapshots: the toolbar in a chat and in a group (side chat and terminal
    greyed there), and the larger headings.
  - Live against the isolated `features` instance, `HYADEX_E2E_EXTRAS` checks
    two cases with real Claude turns. A reply watched to the end stays read
    after leaving. A reply that finishes while another chat is open shows the
    dot. With the fix turned off, the first check failed. The run also showed
    the older "kept while the chat is open" check could fail: opening the chat
    marked it read while the test marked it unread. The test now lets the open
    land first.
  - 112 Swift tests pass. New tests cover thought lines, the status fallback,
    project grouping, `folder` mapping, text runs, and copied text. The
    wrap test caught that measuring had used the view's width.
  - In process, on demo data: a synthesized drag across a whole answer copied
    every paragraph, with math as TeX.
  - Live, against the isolated `livefix` instance:
    - A real Sonnet turn with the screenshot's question kept both of its
      thoughts (English, then Chinese) in the status line until the answer
      began.
    - The status item's own image followed the app's states: template while
      idle, red and not template when urgent, and changing frames while a
      chat ran.
  - Not verified: the menu bar and the title mark on screen (this session
    cannot record the screen; the title's glass setting needs macOS 26), and
    the screenshot shortcut, which needs a key press and the Screen Recording
    permission.

## 2026-10-28 — Review fixes: sidebar, sockets, backend, Codex, Claude, groups

- **Mac sidebar.** A chat you left while it worked no longer stays
  "running" or "needs action" until you reopen it, and its finished-reply and
  approval notifications fire. Its projection is dropped with its socket and
  reloaded on reopening, and the polled list drives it meanwhile. Session lists
  now report `running`, set from `turn_started` to its terminal event, instead
  of leaving it to the latest event type.
- **Mac sockets.** Leaving a chat or group closes its event sockets at once.
  `receive()` ignored Task cancellation, so each switch had left a server
  subscription open until the next frame. Sockets accept 16 MiB messages,
  enough for the live snapshots.
- **Mac terminal.** Copying a selection made before the window narrowed no
  longer crashes. Resizing clears the selection.
- **Mac local backend.** The app-owned backend shuts down when the app crashes
  or is force-quit (`dev_server.py --exit-with-parent`, watched by
  `hyadex/ops/parent_watch.py`), so the next launch can take the instance.
- **Codex.**
  - `idle_warning` is written once per quiet spell, not every 5 seconds.
  - The reader pauses while a subscriber's queue is over half full. A
    command's burst of output deltas no longer overflows the queue and aborts
    the turn. A subscriber that stays stuck still fails after 30 seconds.
  - Approvals and questions from a live subagent thread reach the parent
    chat, under its turn's approval policy, instead of being declined.
  - Force Send carries reasoning and answers still being written, and running
    commands, into the run that continues them: they finish once, in full,
    with one card.
- **Claude.**
  - The run's owner is bound into the options its CLI was started with. A
    command list or MCP request during a cold connect could swap the options
    and make every Hyadex tool of that connection fail as stale.
  - A disconnect that outlasts its 2-second bound now finishes in the
    background. Cancelling it skipped the SDK's SIGTERM/SIGKILL and left the
    CLI running.
  - A subagent's events carry `parent_tool_id`. The Mac app folds them into
    the Agent card (its steps, then its result); they are no longer the main
    agent's messages or the sidebar preview.
- **Groups.** When several queued messages for one member are withdrawn
  (group Stop, or Stop in the member's chat), all of them reach it with its
  next message. Before, the first was skipped.
- Verification:
  - Fixture tests: the backend suite (3,080 tests) and 102 Swift tests pass.
    New tests cover each fix, and the Claude cold-connect test fails without
    its fix.
  - Live runtime, isolated `livefix` instance:
    - A backend whose launcher was SIGKILLed shut down and released its
      instance, which then started again.
    - A real Codex command printing 20,000 lines finished normally.
    - A real Claude subagent's tool events carried their Agent call's id.
  - Not exercised live: the sidebar, socket and terminal changes in the
    running app (only their unit tests); Codex subagent approvals and Force
    Send; the slow Claude disconnect; the 30-minute idle warning.

## 2026-10-28 — Answers live, questions answerable, Chinese search

- **Live output.** Claude chats now run the Agent SDK with partial messages:
  thinking streams on the existing reasoning lane, as Codex reasoning does.
  Answers of both providers (Claude text blocks, Codex agent messages,
  including goal continuations) stream on a new opt-in socket lane
  (`text_stream=true` → `assistant_text_stream`, capability
  `live_text_stream_v1`). The Mac app shows them as a streaming message, and
  in a group under the member still answering. The finished block or message
  stays the durable record. A run that ends early keeps the text shown so far
  as an `assistant_text` with `partial: true`; unfinished thinking is kept as
  partial, as before for Codex. On Stop, Claude Code itself emits the truncated
  block, which is kept as is.
- **Mac interactions.** Approvals offer the decisions the provider sent, with
  their exact response bodies. Codex command approvals often offer only
  `cancel` to refuse; the old 拒绝 always sent `decline` and was rejected.
  Exec-policy and network rules are under 更多选项. Legacy Codex approvals
  use their review decisions. Codex permission requests grant what was asked,
  for the turn or the session. Claude's AskUserQuestion and Codex's
  request_user_input open a form: single choice, multiple choice, "其他" free
  text, secret and number fields, and 跳过. MCP elicitations are filled in from
  their schema, or opened as a link. Before, every request was an Allow/Deny
  card, and questions returned HTTP 400.
- **Search.** Chinese, Japanese and Korean words now match inside sentences
  ("测试" finds "…然后运行测试"). Index version 9 separates each CJK
  character with a zero-width space and searches a word as a phrase; the index
  rebuilds once. A search term made only of punctuation no longer empties the
  results.
- Verification:
  - Fixture tests: the backend suite (3,065 tests) passes, and 99 Swift tests
    pass. New tests cover the live lanes, partial retention, each interaction
    kind's response body, and CJK search.
  - Live runtime: an isolated `livefix` instance ran real Claude Code 2.1.309
    and Codex 0.164.0-alpha.11.1. Both streamed answers before their durable
    message and both kept the result after Stop (Codex as a partial answer). A
    real AskUserQuestion was answered with the form's body (multi-select and
    free text). A real Codex command approval offered accept, a rule and cancel.
  - Client: the form, approval and live-answer cards were checked in snapshot
    renders (scenes 24–26). The app's `HYADEX_E2E_UI` run against a live server
    and MCP elicitation with a real MCP server were not exercised.

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

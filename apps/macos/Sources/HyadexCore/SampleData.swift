import Foundation

/// Demo content shown until the app is connected to a real server.
public enum SampleData {
    static func ago(_ minutes: Double, from now: Date) -> Date { now.addingTimeInterval(-minutes * 60) }

    static let reconnectDiff = """
    @@ -88,14 +88,27 @@ final class EventStream {
         private var lastSeq: Int = 0
    -    private var retryDelay: TimeInterval = 1
    +    private var backoff = Backoff(initial: 0.5, maximum: 30)

         func connect() async {
    -        let url = base.appending(path: "events")
    +        // Resume after the last event we applied, never from zero.
    +        var url = base.appending(path: "events")
    +        url.append(queryItems: [URLQueryItem(name: "after", value: String(lastSeq))])
             let socket = session.webSocketTask(with: url, protocols: ["bearer.\\(token)"])
             socket.resume()
    -        await receive(socket)
    +        do {
    +            try await receive(socket)
    +            backoff.reset()
    +        } catch {
    +            let delay = backoff.next()
    +            logger.info("stream closed, retrying in \\(delay)s")
    +            try? await Task.sleep(for: .seconds(delay))
    +            await connect()
    +        }
         }
    """

    static let backoffDiff = """
    @@ -0,0 +1,22 @@
    +import Foundation
    +
    +/// Exponential backoff with full jitter.
    +struct Backoff {
    +    let initial: TimeInterval
    +    let maximum: TimeInterval
    +    private var attempt = 0
    +
    +    init(initial: TimeInterval, maximum: TimeInterval) {
    +        self.initial = initial
    +        self.maximum = maximum
    +    }
    +
    +    mutating func next() -> TimeInterval {
    +        defer { attempt += 1 }
    +        let ceiling = min(maximum, initial * pow(2, Double(attempt)))
    +        return .random(in: 0...ceiling)
    +    }
    +
    +    mutating func reset() { attempt = 0 }
    +}
    """

    static let testDiff = """
    @@ -40,6 +40,18 @@ final class EventStreamTests {
    +    @Test func resumesAfterLastAppliedSeq() async throws {
    +        let server = FakeServer(events: 1...5)
    +        let stream = EventStream(base: server.url, token: "t")
    +        await stream.apply(upTo: 3)
    +        server.dropConnection()
    +        await stream.connect()
    +        #expect(server.requestedAfter == [0, 3])
    +    }
    """

    static let cursorDiff = """
    @@ -212,7 +212,12 @@ class GroupEngine:
             delta = self._delta_for(member, after=member.cursor)
    -        member.cursor = self._last_seq
    +        # Advance only after the dispatch is durably admitted, so a crash
    +        # between the two never skips messages.
    +        dispatch = await self._admit(member, delta)
    +        if dispatch.admitted:
    +            member.cursor = delta.through_seq
    +            await self._save()
    """

    public static func chats(now: Date) -> [ChatSummary] {
        [
            ChatSummary(id: "c1", title: "重构 WebSocket 断线续传", backend: .claude, model: "opus",
                        permission: "Accept edits", cwd: "~/Code/Hyadex/apps/macos",
                        preview: "已改成按 seq 续传，并加了退避和测试。", lastActivity: ago(2, from: now),
                        pinned: true, tokensUsed: 48_210),
            ChatSummary(id: "c2", title: "Android 推送偶发丢失", backend: .codex, model: "",
                        permission: "Default", cwd: "~/Code/hyadex-android",
                        preview: "Codex 请求运行 ./gradlew connectedCheck", lastActivity: ago(1, from: now),
                        state: .needsAction, tokensUsed: 21_904),
            ChatSummary(id: "c3", title: "发布前检查清单", backend: .claude, model: "sonnet",
                        permission: "Plan", cwd: "~/Code/Hyadex",
                        preview: "正在运行完整测试…", lastActivity: ago(0.2, from: now),
                        state: .running, tokensUsed: 9_380),
            ChatSummary(id: "c4", title: "README 中英双语", backend: .claude, model: "sonnet",
                        permission: "Accept edits", cwd: "~/Code/Hyadex",
                        preview: "两份 README 已对齐，等你确认措辞。", lastActivity: ago(46, from: now),
                        unread: true, tokensUsed: 12_044),
            ChatSummary(id: "c5", title: "7871 端口内存占用排查", backend: .codex, model: "",
                        permission: "Read Only", cwd: "~/Code/Hyadex",
                        preview: "峰值来自事件回放时一次性读入整个 jsonl。", lastActivity: ago(60 * 5, from: now),
                        tokensUsed: 33_617),
            ChatSummary(id: "c6", title: "SwiftUI 列表滚动卡顿", backend: .claude, model: "opus",
                        permission: "Accept edits", cwd: "~/Code/playground",
                        preview: "改用 LazyVStack + 稳定 ID 后流畅了。", lastActivity: ago(60 * 26, from: now),
                        tokensUsed: 15_502),
        ]
    }

    public static func chatTimelines(now: Date) -> [String: [TimelineItem]] {
        let claude = Author.agent("claude", .claude)
        let codex = Author.agent("codex", .codex)
        return [
            "c1": [
                .message(id: "c1-1", author: .user,
                         text: "断线重连后时间线会重复显示一遍。帮我把 WebSocket 改成从最后一个 seq 续传，顺便加上退避重试。",
                         time: ago(9, from: now)),
                .thinking(id: "c1-2", seconds: 7, text: """
                重复是因为重连时总是从 after=0 开始回放，而本地已经应用过这些事件。\
                应该记录最后应用的 seq，重连时带上 after=lastSeq。退避用带抖动的指数退避，连上后重置。
                """),
                .tool(id: "c1-3", kind: .read, title: "Sources/HyadexAPI/EventStream.swift", output: "", status: .succeeded, seconds: 0.1),
                .tool(id: "c1-4", kind: .search, title: "lastSeq", output: "3 处匹配，2 个文件", status: .succeeded, seconds: 0.2),
                .tool(id: "c1-5", kind: .edit, title: "Sources/HyadexAPI/EventStream.swift", output: "", status: .succeeded, seconds: 0.1),
                .tool(id: "c1-6", kind: .edit, title: "Sources/HyadexAPI/Backoff.swift", output: "", status: .succeeded, seconds: 0.1),
                .tool(id: "c1-7", kind: .command, title: "./scripts/test.sh --filter EventStream",
                      output: "✔ Test resumesAfterLastAppliedSeq() passed after 0.012 seconds.\n✔ Test run with 6 tests in 1 suite passed after 0.31 seconds.",
                      status: .succeeded, seconds: 18.4),
                .changes(id: "c1-8", set: FileChangeSet(files: [
                    FileChange(path: "Sources/HyadexAPI/EventStream.swift", added: 15, removed: 3, diff: reconnectDiff),
                    FileChange(path: "Sources/HyadexAPI/Backoff.swift", added: 22, removed: 0, diff: backoffDiff),
                    FileChange(path: "Tests/HyadexAPITests/EventStreamTests.swift", added: 8, removed: 0, diff: testDiff),
                ])),
                .message(id: "c1-9", author: claude, text: """
                已改成按 seq 续传，并加了退避和测试。

                **改了什么**
                - 重连时带上 `after=lastSeq`，服务器只回放之后的事件，不会再重复显示。
                - 新增 `Backoff`：0.5 秒起步、最长 30 秒、带随机抖动；连上后重置。
                - 新测试 `resumesAfterLastAppliedSeq` 模拟断线，确认第二次请求的是 `after=3`。

                ```swift
                var url = base.appending(path: "events")
                url.append(queryItems: [URLQueryItem(name: "after", value: String(lastSeq))])
                ```

                6 个测试全部通过。还没处理的一点：`stream_ready` 里的 `through_seq` 目前没用上，可以用它判断回放是否补齐。
                """, time: ago(2, from: now)),
            ],
            "c2": [
                .message(id: "c2-1", author: .user, text: "推送偶尔收不到，尤其是锁屏十几分钟之后。帮我查一下原因。",
                         time: ago(14, from: now)),
                .thinking(id: "c2-2", seconds: 12, text: "先看前台服务和 WorkManager 的约束，再看 Doze 下的网络回调。"),
                .tool(id: "c2-3", kind: .command, title: "rg -n \"setRequiresDeviceIdle|ForegroundService\" app/src",
                      output: "app/src/main/java/dev/hyadex/push/PushWorker.kt:41: .setRequiresDeviceIdle(false)",
                      status: .succeeded, seconds: 0.4),
                .tool(id: "c2-4", kind: .read, title: "app/src/main/java/dev/hyadex/push/PushWorker.kt", output: "", status: .succeeded, seconds: 0.1),
                .message(id: "c2-5", author: codex, text: """
                找到一个可疑点：长连接的心跳是 15 分钟，而 Doze 维护窗口之外网络会被挂起，服务器那边 10 分钟没收到心跳就把连接关了。\
                我想跑一下设备上的连接测试来确认。
                """, time: ago(2, from: now)),
                .approval(id: "c2-6", author: codex, title: "运行命令",
                          command: "./gradlew connectedCheck -Pandroid.testInstrumentationRunnerArguments.class=dev.hyadex.push.HeartbeatTest",
                          reason: "需要访问已连接的测试设备，超出了当前沙箱的权限。", decision: .pending),
            ],
            "c3": [
                .message(id: "c3-1", author: .user, text: "按发布清单把所有检查跑一遍，有问题就停下来告诉我。", time: ago(4, from: now)),
                .tool(id: "c3-2", kind: .command, title: "git status --porcelain", output: "", status: .succeeded, seconds: 0.1),
                .tool(id: "c3-3", kind: .command, title: "uv run python -m unittest discover -q",
                      output: "........................................................", status: .running, seconds: 41),
            ],
            "c4": [
                .message(id: "c4-1", author: .user, text: "把 README 做成中英双语，英文为主，中文放在 README.zh-CN.md。", time: ago(70, from: now)),
                .changes(id: "c4-2", set: FileChangeSet(files: [
                    FileChange(path: "README.md", added: 12, removed: 4, diff: "@@ -1,4 +1,6 @@\n # Hyadex\n+\n+[中文](README.zh-CN.md)\n"),
                    FileChange(path: "README.zh-CN.md", added: 96, removed: 0, diff: "@@ -0,0 +1,3 @@\n+# Hyadex\n+\n+在局域网里统一管理 Claude Code 和 Codex。\n"),
                ])),
                .message(id: "c4-3", author: claude, text: "两份 README 已对齐，等你确认措辞。中文版把“讨论组”和“自动接力”放在了最前面。", time: ago(46, from: now)),
            ],
        ]
    }

    public static func groups(now: Date) -> [GroupSummary] {
        [
            GroupSummary(id: "g1", title: "讨论组游标设计评审", cwd: "~/Code/Hyadex",
                         participants: [
                            Participant(handle: "claude", backend: .claude, role: "架构", model: "opus"),
                            Participant(handle: "codex", backend: .codex, role: "实现", model: "", state: .running),
                         ],
                         defaultResponder: "claude", preview: "@codex 正在补测试…", lastActivity: ago(0.5, from: now),
                         promptChars: 4_812, dispatches: 5),
            GroupSummary(id: "g2", title: "周报整理", cwd: "~/Documents/weekly",
                         participants: [
                            Participant(handle: "claude", backend: .claude, role: "写作", model: "sonnet"),
                         ],
                         defaultResponder: "claude", preview: "第 43 周周报已生成。", lastActivity: ago(60 * 20, from: now),
                         promptChars: 1_204, dispatches: 1),
        ]
    }

    public static func groupTimelines(now: Date) -> [String: [TimelineItem]] {
        let claude = Author.agent("claude", .claude)
        let codex = Author.agent("codex", .codex)
        return [
            "g1": [
                .divider(id: "g1-0", text: "讨论组已创建 · @claude @codex · ~/Code/Hyadex"),
                .message(id: "g1-1", author: .user,
                         text: "@claude @codex 评审一下成员游标的推进时机：现在是派发前就推进，崩溃时会不会漏消息？",
                         time: ago(12, from: now), mentions: ["claude", "codex"]),
                .message(id: "g1-2", author: claude, text: """
                会漏。`member.cursor` 在 `_admit` 之前就推进了，如果进程在两者之间崩溃，恢复后这个成员会跳过这批消息。

                建议：
                1. 先写入 dispatch，再推进游标；
                2. 游标推进到 `delta.through_seq`，而不是 `_last_seq`，避免把派发期间新到的消息也算成已读。

                @codex 你来改一下？顺便补一个“崩溃发生在两者之间”的测试。
                """, time: ago(10, from: now), mentions: ["codex"]),
                .message(id: "g1-3", author: codex, text: "同意。已按这个顺序改好，并在测试里用故障注入模拟了在 `_admit` 之后、保存之前崩溃。",
                         time: ago(6, from: now), receipt: FileChangeSet(files: [
                            FileChange(path: "hyadex/groups.py", added: 6, removed: 1, diff: cursorDiff),
                            FileChange(path: "tests/test_group_chat.py", added: 31, removed: 0,
                                       diff: "@@ -880,0 +881,4 @@\n+    async def test_crash_between_admit_and_save_resends_nothing(self):\n+        engine = self.engine(fail_after=\"admit\")\n+        ...\n"),
                         ])),
                .divider(id: "g1-4", text: "第 1 轮结束 · 3 次派发 · 2,140 字符"),
                .message(id: "g1-5", author: .user, text: "@codex 再把并行模式下的同样情况也覆盖一下。",
                         time: ago(1, from: now), mentions: ["codex"]),
                .tool(id: "g1-6", kind: .command, title: "@codex · python3 -m unittest tests.test_group_chat -q",
                      output: "", status: .running, seconds: 12),
            ],
            "g2": [
                .message(id: "g2-1", author: .user, text: "@claude 根据本周的 git log 整理一份周报。", time: ago(60 * 20 + 5, from: now), mentions: ["claude"]),
                .message(id: "g2-2", author: Author.agent("claude", .claude), text: "第 43 周周报已生成，放在 `weekly/2026-W43.md`。",
                         time: ago(60 * 20, from: now)),
            ],
        ]
    }

    public static func runs(now: Date) -> [AutopilotRun] {
        let verify = "python3 -m unittest -q"
        return [
            AutopilotRun(id: "r1", title: "groups 分页接口", goal: """
            给 GET /api/groups/{id}/events 加上基于 seq 的分页（before / after / limit），\
            客户端向上滚动时按页加载，不再一次性返回整条时间线。
            """, cwd: "~/Code/Hyadex", branch: "hyadex/run-7f3a", status: .verifying,
                         approval: .plan, isolation: .worktree, reviewer: .planner, verifyCommand: verify,
                         planSummary: "分三步：先改存储层的读取，再改路由参数，最后补客户端可用的分页元数据和测试。",
                         tasks: [
                            AutopilotTask(id: "t1", title: "事件存储支持按 seq 反向读取",
                                          detail: "在 events.jsonl 读取器里加 before 参数，从文件尾部按块反向扫描，避免整文件读入内存。",
                                          files: ["hyadex/groups.py"], acceptance: "读取 10 万条事件的最后一页 < 50 ms",
                                          state: .approved, commit: "a41c9e2",
                                          verification: Verification(command: verify, passed: true, summary: "Ran 212 tests · OK", duration: 38),
                                          reviewNote: "实现正确，块大小可以之后再调。",
                                          changes: FileChangeSet(files: [FileChange(path: "hyadex/groups.py", added: 48, removed: 9, diff: cursorDiff)])),
                            AutopilotTask(id: "t2", title: "路由增加 before / limit 参数",
                                          detail: "GET /api/groups/{id}/events 接受 before 与 limit（1–500），响应里带 has_more 和 next_before。",
                                          files: ["hyadex/routes/groups.py", "docs/GROUP_DISCUSSIONS.md"],
                                          acceptance: "参数非法返回 422；文档同步更新",
                                          state: .verifying, revisions: 1,
                                          reviewNote: "第一次提交漏了 limit 上限校验，已要求补上。"),
                            AutopilotTask(id: "t3", title: "补充分页测试",
                                          detail: "覆盖空时间线、恰好一页、跨页、before 超出范围四种情况。",
                                          files: ["tests/test_group_chat.py"], acceptance: "四种情况都有断言"),
                         ],
                         notes: [
                            .message(id: "r1-n1", author: .autopilot, text: "计划已批准，开始执行任务 1。", time: ago(31, from: now)),
                            .message(id: "r1-n2", author: .autopilot, text: "任务 1 验证通过，审查通过，已提交 a41c9e2。", time: ago(19, from: now)),
                            .message(id: "r1-n3", author: .autopilot, text: "任务 2 审查要求修改：补上 limit 上限校验。", time: ago(9, from: now)),
                            .message(id: "r1-n4", author: .autopilot, text: "任务 2 第 2 次提交，正在运行验证。", time: ago(0.5, from: now)),
                         ],
                         lastActivity: ago(0.5, from: now), promptChars: 14_220),
            AutopilotRun(id: "r2", title: "slugify 工具函数", goal: """
            给 text_utils.py 加一个 slugify(text)：转小写，空格、下划线和连字符合并成一个连字符，\
            去掉其他非 a-z0-9 字符，去掉首尾连字符。并补单元测试。
            """, cwd: "~/Code/text-utils", branch: "hyadex/run-2b91", status: .awaitingApproval,
                         approval: .plan, isolation: .worktree, reviewer: .planner, verifyCommand: verify,
                         planSummary: "两个任务：先实现函数，再补测试。两者都只动 text_utils 相关文件。",
                         tasks: [
                            AutopilotTask(id: "t1", title: "实现 slugify", detail: "在 text_utils.py 中用正则实现，保持无第三方依赖。",
                                          files: ["text_utils.py"], acceptance: "slugify(' Hello__World-- ') == 'hello-world'"),
                            AutopilotTask(id: "t2", title: "补充单元测试", detail: "覆盖空串、纯符号、连续分隔符、首尾分隔符、大小写。",
                                          files: ["test_text_utils.py"], acceptance: "python3 -m unittest 通过"),
                         ],
                         notes: [
                            .message(id: "r2-n1", author: .autopilot, text: "Claude 已给出计划，等待你批准。", time: ago(3, from: now)),
                         ],
                         lastActivity: ago(3, from: now), promptChars: 2_310),
            AutopilotRun(id: "r3", title: "移除遗留 Team 代码", goal: "删除 hyadex_team_hub 以及所有 team 路由和测试，保持其余功能不变。",
                         cwd: "~/Code/Hyadex", branch: "hyadex/run-0c55", status: .done,
                         approval: .none, isolation: .worktree, reviewer: .fresh, verifyCommand: verify,
                         planSummary: "一次性删除并修正引用。",
                         tasks: [
                            AutopilotTask(id: "t1", title: "删除 team 模块与路由", detail: "", files: ["hyadex_team_hub/", "agent_server.py"],
                                          acceptance: "全部测试通过", state: .approved, commit: "7589748",
                                          verification: Verification(command: verify, passed: true, summary: "Ran 1,907 tests · OK", duration: 412)),
                         ],
                         lastActivity: ago(60 * 30, from: now), promptChars: 6_904),
        ]
    }

    public static func jobs(now: Date) -> [ScheduledJob] {
        [
            ScheduledJob(id: "j1", title: "每日依赖安全检查", prompt: "检查 uv.lock 和 package-lock.json 里的已知漏洞并汇报。",
                         target: "Claude · 发布前检查清单", schedule: "每天 09:00",
                         nextRun: now.addingTimeInterval(60 * 60 * 14), lastResult: .done, lastRun: ago(60 * 10, from: now), enabled: true),
            ScheduledJob(id: "j2", title: "夜间完整测试", prompt: "@codex 跑完整测试，失败就定位原因。",
                         target: "讨论组 · 讨论组游标设计评审", schedule: "每天 02:30",
                         nextRun: now.addingTimeInterval(60 * 60 * 7), lastResult: .failed, lastRun: ago(60 * 17, from: now), enabled: true),
            ScheduledJob(id: "j3", title: "周报", prompt: "@claude 根据本周 git log 写周报。",
                         target: "讨论组 · 周报整理", schedule: "每周五 18:00",
                         nextRun: now.addingTimeInterval(60 * 60 * 24 * 4), lastResult: .done, lastRun: ago(60 * 24 * 3, from: now), enabled: true),
            ScheduledJob(id: "j4", title: "清理过期 worktree", prompt: "删除 30 天前已合并的自动接力 worktree。",
                         target: "Codex · 7871 端口内存占用排查", schedule: "每月 1 日 03:00",
                         nextRun: nil, lastResult: nil, lastRun: nil, enabled: false),
        ]
    }

    static func usage(chats: [ChatSummary], now: Date) -> ([AgentBackend: ProviderUsage], [String: ContextUsage]) {
        let usage: [AgentBackend: ProviderUsage] = [
            .claude: ProviderUsage(available: true, windows: [
                UsageWindow(id: "five_hour", label: "Current session", usedPercent: 38,
                            resetsAt: now.addingTimeInterval(2.4 * 3600), windowMinutes: 300),
                UsageWindow(id: "seven_day", label: "Current week (all models)", usedPercent: 21,
                            resetsAt: now.addingTimeInterval(3.2 * 86400), windowMinutes: 10080),
            ], plan: "max", observedAt: now),
            .codex: ProviderUsage(available: true, windows: [
                UsageWindow(id: "codex:primary", label: "5h limit", usedPercent: 12,
                            resetsAt: now.addingTimeInterval(3.6 * 3600), windowMinutes: 300),
                UsageWindow(id: "codex:secondary", label: "Weekly limit", usedPercent: 46,
                            resetsAt: now.addingTimeInterval(4.5 * 86400), windowMinutes: 10080),
            ], observedAt: now),
        ]
        var context: [String: ContextUsage] = [:]
        for chat in chats where chat.tokensUsed > 0 {
            let window = chat.backend == .claude ? 200_000 : 258_400
            context[chat.id] = ContextUsage(tokens: chat.tokensUsed, window: window,
                                            percent: Double(chat.tokensUsed) * 100 / Double(window))
        }
        return (usage, context)
    }
}

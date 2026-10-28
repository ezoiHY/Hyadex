import Foundation
import HyadexAPI

/// Permission choices per backend, with the provider's own names: Claude
/// Code's permission modes (as its mode switcher names them) and Codex's
/// permission presets (as its `/permissions` picker names them).
public struct PermissionPreset: Hashable, Sendable {
    public let label: String
    /// One line on what the agent may do without asking.
    public let detail: String
    public let symbol: String
    public let fields: [String: String]

    public static func presets(for backend: AgentBackend) -> [PermissionPreset] {
        switch backend {
        case .claude:
            [
                PermissionPreset(label: "Manual", detail: "编辑文件、运行命令前先问你（遵循 Claude Code 的允许/拒绝规则）",
                                 symbol: "hand.raised", fields: ["claude_permission_mode": "default"]),
                PermissionPreset(label: "Accept edits", detail: "直接改文件，运行命令前仍会问你",
                                 symbol: "pencil.and.outline", fields: ["claude_permission_mode": "acceptEdits"]),
                PermissionPreset(label: "Plan", detail: "只读分析并给出计划，不改任何东西",
                                 symbol: "list.bullet.clipboard", fields: ["claude_permission_mode": "plan"]),
                PermissionPreset(label: "Auto", detail: "由 Claude 的分类器自动批准或拒绝每次工具调用",
                                 symbol: "wand.and.sparkles", fields: ["claude_permission_mode": "auto"]),
                PermissionPreset(label: "Don't Ask", detail: "从不询问；没有预先允许的操作一律拒绝",
                                 symbol: "nosign", fields: ["claude_permission_mode": "dontAsk"]),
                PermissionPreset(label: "Bypass Permissions", detail: "跳过所有权限检查，谨慎使用",
                                 symbol: "exclamationmark.shield", fields: ["claude_permission_mode": "bypassPermissions"]),
            ]
        case .codex:
            [
                PermissionPreset(label: "Read Only", detail: "可读取工作区文件；改文件或联网需要你批准",
                                 symbol: "eye", fields: ["codex_sandbox_mode": "read-only", "codex_approval_policy": "on-request"]),
                PermissionPreset(label: "Default", detail: "可读写工作区、运行命令；联网或改工作区外的文件需要你批准",
                                 symbol: "folder.badge.gearshape",
                                 fields: ["codex_sandbox_mode": "workspace-write", "codex_approval_policy": "on-request"]),
                PermissionPreset(label: "Full Access", detail: "可改工作区外的文件、联网，都不再询问，谨慎使用",
                                 symbol: "exclamationmark.shield",
                                 fields: ["codex_sandbox_mode": "danger-full-access", "codex_approval_policy": "never"]),
            ]
        }
    }

    public static func preset(_ label: String, for backend: AgentBackend) -> PermissionPreset? {
        presets(for: backend).first { $0.label == label }
    }

    static func label(for session: JSON, backend: AgentBackend) -> String {
        switch backend {
        case .claude:
            let mode = session["claude_permission_mode"].string ?? "default"
            return presets(for: .claude).first { $0.fields["claude_permission_mode"] == mode }?.label ?? "Manual"
        case .codex:
            let sandbox = session["codex_sandbox_mode"].string ?? "workspace-write"
            let policy = session["codex_approval_policy"].string ?? "on-request"
            if let preset = presets(for: .codex).first(where: {
                $0.fields["codex_sandbox_mode"] == sandbox && $0.fields["codex_approval_policy"] == policy
            }) { return preset.label }
            // Codex names any other combination the same way.
            return "Custom (\(sandbox), \(policy))"
        }
    }
}

enum LiveMapping {
    /// Event types that only occur while a turn is in progress.
    static let activeEventTypes: Set<String> = [
        "turn_started", "process_started", "provider_session", "tool_started", "tool_finished", "assistant_text",
        "reasoning_summary", "reasoning_text", "claude_interaction_requested", "codex_interaction_requested",
        "claude_interaction_resolved", "codex_interaction_resolved", "codex_thread_status", "code_diff",
        "subagent_state", "claude_goal_changed",
    ]

    static func abbreviate(_ path: String) -> String {
        var path = path
        if path.hasPrefix("/private/") { path.removeFirst("/private".count) }
        let home = NSHomeDirectory()
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }

    static func expand(_ path: String) -> String {
        path.hasPrefix("~") ? NSHomeDirectory() + path.dropFirst() : path
    }

    static func chat(_ json: JSON, previous: ChatSummary?) -> ChatSummary? {
        guard let id = json["id"].string,
              let backend = json["backend"].string.flatMap(AgentBackend.named) else { return nil }
        var state: ActivityState = LiveMapping.activeEventTypes.contains(json["latest_event_type"].string ?? "")
            ? .running : .idle
        if json["claude_needs_user_action"].bool == true || json["codex_needs_user_action"].bool == true {
            state = .needsAction
        }
        let readSeq = json["last_read_agent_event_seq"].int ?? 0
        let agentSeq = json["latest_agent_event_seq"].int ?? 0
        return ChatSummary(
            id: id,
            title: json["title"].text ?? "未命名聊天",
            backend: backend,
            model: json["model"].text ?? "",
            effort: json["effort"].text ?? "",
            permission: PermissionPreset.label(for: json, backend: backend),
            cwd: abbreviate(json["cwd"].string ?? "~"),
            preview: previous?.preview ?? "",
            lastActivity: json["updated_at"].date ?? previous?.lastActivity ?? .distantPast,
            state: state,
            pinned: json["pinned"].bool == true,
            unread: json["manual_unread"].bool == true || agentSeq > readSeq,
            tokensUsed: previous?.tokensUsed ?? 0,
            archived: json["archived"].bool == true
        )
    }

    /// `members` holds member-chat records by session id. Turns run with the member
    /// chat's settings, so those are authoritative over the group's own copy.
    static func group(_ json: JSON, previous: GroupSummary?, members: [String: JSON] = [:]) -> GroupSummary? {
        guard let id = json["id"].string, json["kind"].string != "autopilot" else { return nil }
        let participants = json["participants"].array.compactMap { item -> Participant? in
            guard let handle = item["handle"].string, let backend = item["backend"].string.flatMap(AgentBackend.named) else {
                return nil
            }
            let status = item["status"]
            var state = GroupProjection.state(status["state"].string)
            if status["needs_action"].bool == true { state = .needsAction }
            let sessionID = item["session_id"].string
            let member = sessionID.flatMap { members[$0] }
            return Participant(handle: handle, backend: backend, role: item["role"].text ?? item["label"].text ?? "",
                               model: (member?["model"] ?? item["model"]).text ?? "", state: state,
                               sessionID: sessionID,
                               effort: (member?["effort"] ?? item["effort"]).text ?? "",
                               permission: member.map { PermissionPreset.label(for: $0, backend: backend) })
        }
        let stats = json["stats"]
        var group = GroupSummary(
            id: id,
            title: json["title"].text ?? "讨论组",
            cwd: abbreviate(json["cwd"].string ?? "~"),
            participants: participants,
            defaultResponder: json["settings"]["default_responder"].string ?? participants.first?.handle ?? "claude",
            preview: previous?.preview ?? "",
            lastActivity: json["updated_at"].date ?? .distantPast,
            unread: false,
            promptChars: stats["prompt_chars"].int ?? 0,
            dispatches: stats["dispatches"].int ?? 0,
            goal: GroupGoal.parse(json["goal"])
        )
        group.archived = json["archived"].bool == true
        return group
    }

    static func runStatus(_ raw: String?) -> AutopilotStatus {
        switch raw {
        case "planning": .planning
        case "awaiting_approval": .awaitingApproval
        case "executing": .executing
        case "verifying": .verifying
        case "reviewing": .reviewing
        case "paused": .paused
        case "needs_attention": .needsAttention
        case "done": .done
        case "aborted", "failed": .aborted
        default: .planning
        }
    }

    static func run(_ json: JSON, previous: AutopilotRun?) -> AutopilotRun? {
        guard let id = json["id"].string else { return nil }
        let status = runStatus(json["status"].string)
        let current = json["task_index"].int
        let lastStep = json["last_step"]
        let commits = Dictionary(json["commits"].array.compactMap { item -> (Int, String)? in
            guard let index = item["task_index"].int, let commit = item["commit"].string else { return nil }
            return (index, String(commit.prefix(7)))
        }, uniquingKeysWith: { $1 })
        let planTasks = json["plan"]["tasks"].array
        let tasks: [AutopilotTask] = planTasks.isEmpty && json["task_count"].int != nil
            ? previous?.tasks ?? []
            : planTasks.enumerated().map { offset, task in
                var state: TaskState
                switch task["status"].string {
                case "done": state = .approved
                case "in_progress":
                    switch status {
                    case .verifying: state = .verifying
                    case .reviewing: state = .reviewing
                    case .needsAttention: state = .blocked
                    default: state = .executing
                    }
                default: state = .pending
                }
                var verification: Verification?
                if lastStep["task_index"].int == offset, !lastStep["verification"].isNull {
                    let check = lastStep["verification"]
                    verification = Verification(command: check["command"].string ?? "",
                                                passed: check["passed"].bool == true,
                                                summary: String((check["output_tail"].string ?? "")
                                                    .split(separator: "\n").last ?? ""),
                                                duration: check["seconds"].double ?? 0)
                }
                return AutopilotTask(
                    id: "\(id)-\(offset)",
                    title: task["title"].text ?? "任务 \(offset + 1)",
                    detail: task["detail"].string ?? "",
                    files: task["files"].array.compactMap(\.string),
                    acceptance: task["acceptance"].string
                        ?? task["acceptance"].array.compactMap(\.string).joined(separator: "\n"),
                    state: offset == current && state == .pending ? .executing : state,
                    revisions: max(0, (task["attempts"].int ?? 1) - 1),
                    commit: commits[offset] ?? task["commit"].string.map { String($0.prefix(7)) },
                    verification: verification
                )
            }
        return AutopilotRun(
            id: id,
            title: json["title"].text ?? "自动接力",
            goal: json["goal"].string ?? previous?.goal ?? "",
            cwd: abbreviate(json["cwd"].string ?? "~"),
            branch: json["branch"].string,
            status: status,
            approval: ApprovalOption(rawValue: json["approval"].string ?? "") ?? .plan,
            isolation: json["isolation"].string == "in_place" ? .inPlace : .worktree,
            reviewer: ReviewerOption(rawValue: json["reviewer"].string ?? "") ?? .planner,
            verifyCommand: json["verify_command"].string ?? previous?.verifyCommand ?? "",
            planSummary: json["plan"]["summary"].string ?? previous?.planSummary ?? "",
            tasks: tasks,
            notes: previous?.notes ?? [],
            lastActivity: json["updated_at"].date ?? .distantPast,
            promptChars: previous?.promptChars ?? 0
        )
    }

    static func schedule(_ job: JSON) -> String {
        switch job["schedule_kind"].string {
        case "interval":
            let seconds = job["interval_seconds"].int ?? 0
            if seconds % 86_400 == 0 { return "每 \(seconds / 86_400) 天" }
            if seconds % 3_600 == 0 { return "每 \(seconds / 3_600) 小时" }
            return "每 \(max(1, seconds / 60)) 分钟"
        case "cron": return job["cron_expression"].string ?? "cron"
        case "rrule": return job["rrule"].string ?? "rrule"
        case "once": return "一次"
        default: return job["schedule_kind"].string ?? "—"
        }
    }

    static func scheduleSpec(_ job: JSON) -> JobSchedule? {
        switch job["schedule_kind"].string {
        case "interval":
            guard let seconds = job["interval_seconds"].int else { return nil }
            return .interval(seconds: seconds, repeats: job["loop"].bool ?? true,
                             firstRun: job["next_run_at"].date ?? job["schedule_start_at"].date)
        case "cron": return job["cron_expression"].string.map(JobSchedule.cron)
        case "rrule": return job["rrule"].string.map(JobSchedule.rrule)
        default: return nil
        }
    }

    static func job(_ json: JSON, chatTitles: [String: String]) -> ScheduledJob? {
        guard let id = json["id"].string else { return nil }
        let target: String
        if let group = json["group_id"].string {
            target = "讨论组 · " + (chatTitles[group] ?? group)
        } else if let session = json["session_id"].string {
            target = chatTitles[session] ?? "聊天"
        } else {
            target = "独立运行"
        }
        let failed = json["last_manual_run_error"].text != nil
        let lastRun = json["last_run_at"].date
        return ScheduledJob(
            id: id,
            title: json["title"].text ?? "定时任务",
            prompt: json["prompt"].string ?? "",
            target: target,
            schedule: schedule(json),
            nextRun: json["next_run_at"].date,
            lastResult: lastRun == nil ? nil : (failed ? .failed : .done),
            lastRun: lastRun,
            enabled: json["enabled"].bool != false,
            sessionID: json["session_id"].string,
            groupID: json["group_id"].string,
            spec: scheduleSpec(json)
        )
    }
}

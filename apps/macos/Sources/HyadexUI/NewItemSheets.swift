import HyadexCore
import SwiftUI

/// Shared chrome for creation sheets: title, form, Cancel / primary button.
private struct SheetScaffold<Content: View>: View {
    let title: String
    let primary: String
    let canSubmit: Bool
    let onSubmit: () -> Void
    @ViewBuilder let content: () -> Content
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            Text(title)
                .font(.uiHeadline)
                .padding(.top, 16)
            Form { content() }
                .formStyle(.grouped)
                .scrollContentBackground(.hidden)
            Divider()
            HStack {
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(primary) {
                    onSubmit()
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
            .padding(14)
        }
        .frame(width: 520)
    }
}

struct NewChatSheet: View {
    @Bindable var model: AppModel
    @State private var backend: AgentBackend = .claude
    @State private var modelName = ""
    @State private var cwd = ""
    @State private var permission = "Manual"
    /// Started from a project: the chat goes into it.
    @State private var project: String?

    private var models: [RuntimeOption] { model.catalog(for: backend).models }

    private var permissions: [String] { PermissionPreset.presets(for: backend).map(\.label) }

    var body: some View {
        SheetScaffold(title: project.map { "在“\($0)”中新建聊天" } ?? "新建聊天", primary: "创建", canSubmit: !cwd.isEmpty) {
            model.createChat(backend: backend, model: modelName, cwd: cwd, permission: permission, project: project)
        } content: {
            Picker("代理", selection: $backend) {
                ForEach(AgentBackend.allCases, id: \.self) { Text($0.displayName).tag($0) }
            }
            .pickerStyle(.segmented)
            .onChange(of: backend, initial: true) {
                modelName = ""
                permission = backend == .claude ? "Manual" : "Default"
            }
            Picker("模型", selection: $modelName) {
                ForEach(models) { Text($0.label).tag($0.value) }
            }
            ServerFolderField(model: model, path: $cwd)
                .help("代理会加载这个目录下自己的 MCP 服务器、技能和项目配置")
            Picker("权限", selection: $permission) {
                ForEach(permissions, id: \.self) { Text($0).tag($0) }
            }
        }
        .onAppear {
            if let preset = model.newChatPreset {
                project = preset.project
                if let directory = preset.directory { cwd = directory }
                model.newChatPreset = nil
            }
            if cwd.isEmpty { cwd = model.isLive ? model.defaultCwd : "~/Code/Hyadex" }
        }
    }
}

/// Creates or edits a project: its name, and optionally the directory its chats work in.
struct ProjectSheet: View {
    @Bindable var model: AppModel
    let target: ProjectEditorTarget
    @State private var name = ""
    @State private var directory = ""

    private var editing: String? {
        if case .edit(let name) = target { name } else { nil }
    }

    private var trimmed: String { name.trimmingCharacters(in: .whitespacesAndNewlines) }

    private var taken: Bool {
        trimmed != editing && model.projects.contains { $0.name == trimmed }
    }

    var body: some View {
        SheetScaffold(title: editing == nil ? "新建项目" : "编辑项目", primary: editing == nil ? "创建" : "保存",
                      canSubmit: !trimmed.isEmpty && !taken && trimmed != ProjectGrouping.serverDefault) {
            model.saveProject(ChatProject(name: trimmed, directory: directory.isEmpty ? nil : directory), replacing: editing)
            if case .new(_, let chatID?) = target { model.moveChat(chatID, toProject: trimmed) }
        } content: {
            TextField("名称", text: $name, prompt: Text("例如 Hyadex"))
            if taken {
                Text("已经有同名的项目。").font(.uiMeta).foregroundStyle(.orange)
            }
            ServerFolderField(model: model, path: $directory)
            Text("可以不填。填了之后，在这个目录里工作的聊天会自动归入项目，在项目里新建聊天时也默认用它。")
                .font(.uiMeta)
                .foregroundStyle(.secondary)
        }
        .onAppear {
            switch target {
            case .new(let path, _):
                directory = path ?? ""
                if let path { name = path.split(separator: "/").last.map(String.init) ?? "" }
            case .edit(let current):
                name = current
                directory = model.projects.first { $0.name == current }?.directory ?? ""
            }
        }
    }
}

struct NewGroupSheet: View {
    @Bindable var model: AppModel
    @State private var title = ""
    @State private var cwd = ""
    @State private var includeClaude = true
    @State private var includeCodex = true
    @State private var defaultResponder = "claude"

    var body: some View {
        SheetScaffold(title: "新建讨论组", primary: "创建", canSubmit: includeClaude || includeCodex) {
            var members: [Participant] = []
            if includeClaude { members.append(Participant(handle: "claude", backend: .claude, role: "规划", model: "")) }
            if includeCodex { members.append(Participant(handle: "codex", backend: .codex, role: "实现", model: "")) }
            members.sort { $0.handle == defaultResponder && $1.handle != defaultResponder }
            model.createGroup(title: title, cwd: cwd, participants: members)
        } content: {
            TextField("名称", text: $title, prompt: Text("例如：登录模块重构"))
            ServerFolderField(model: model, path: $cwd)
            Section("成员") {
                Toggle(isOn: $includeClaude) {
                    HStack { AgentAvatar(backend: .claude, size: 18); Text("@claude · Claude Code") }
                }
                Toggle(isOn: $includeCodex) {
                    HStack { AgentAvatar(backend: .codex, size: 18); Text("@codex · Codex") }
                }
                Picker("默认回答者", selection: $defaultResponder) {
                    Text("@claude").tag("claude")
                    Text("@codex").tag("codex")
                }
                .help("消息里没有 @ 任何人时由它回答")
            }
        }
        .onAppear { if cwd.isEmpty { cwd = model.isLive ? model.defaultCwd : "~/Code/Hyadex" } }
    }
}

struct NewAutopilotSheet: View {
    @Bindable var model: AppModel
    @State private var goal = ""
    @State private var cwd = ""
    @State private var approval: ApprovalOption = .plan
    @State private var isolation: IsolationOption = .worktree
    @State private var reviewer: ReviewerOption = .planner
    @State private var verifyCommand = "python3 -m unittest -q"

    var body: some View {
        SheetScaffold(title: "新建自动接力", primary: "开始规划",
                      canSubmit: !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
            model.createRun(goal: goal, cwd: cwd, approval: approval, isolation: isolation,
                            reviewer: reviewer, verifyCommand: verifyCommand)
        } content: {
            Section("目标") {
                TextField("目标", text: $goal, prompt: Text("要完成什么？写清楚范围和验收标准。"), axis: .vertical)
                    .labelsHidden()
                    .lineLimit(4...8)
                ServerFolderField(model: model, path: $cwd)
                HStack(spacing: 6) {
                    AgentAvatar(backend: .claude, size: 16); Text("Claude 规划")
                    Image(systemName: "arrow.right").font(.uiMeta).foregroundStyle(.tertiary)
                    AgentAvatar(backend: .codex, size: 16); Text("Codex 执行")
                }
                .font(.uiSecondary)
                .foregroundStyle(.secondary)
            }
            Section("运行方式") {
                OptionPicker(title: "审批", selection: $approval, options: ApprovalOption.allCases,
                             label: \.label, explanation: \.explanation)
                OptionPicker(title: "隔离", selection: $isolation, options: IsolationOption.allCases,
                             label: \.label, explanation: \.explanation)
                OptionPicker(title: "审查", selection: $reviewer, options: ReviewerOption.allCases,
                             label: \.label, explanation: \.explanation)
            }
            Section("验证") {
                TextField("验证命令", text: $verifyCommand, prompt: Text("每一步完成后运行，例如 npm test"))
                    .labelsHidden()
                    .font(.uiSecondary.monospaced())
                    .help("验证失败的步骤不会通过。命令在 Codex 沙箱里运行：只能写工作目录，不能联网。")
            }
        }
        .onAppear { if cwd.isEmpty { cwd = model.isLive ? model.defaultCwd : "~/Code/Hyadex" } }
    }
}

/// Segmented choice; what the selected option means is in its tooltip.
private struct OptionPicker<Option: Hashable>: View {
    let title: String
    @Binding var selection: Option
    let options: [Option]
    let label: KeyPath<Option, String>
    let explanation: KeyPath<Option, String>

    var body: some View {
        Picker(title, selection: $selection) {
            ForEach(options, id: \.self) { Text($0[keyPath: label]).tag($0) }
        }
        .pickerStyle(.segmented)
        .help(selection[keyPath: explanation])
    }
}

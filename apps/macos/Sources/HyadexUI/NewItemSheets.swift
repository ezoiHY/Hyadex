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
                .font(.headline)
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

    private var models: [RuntimeOption] { model.catalog(for: backend).models }

    private var permissions: [String] { PermissionPreset.presets(for: backend).map(\.label) }

    var body: some View {
        SheetScaffold(title: "新建聊天", primary: "创建", canSubmit: !cwd.isEmpty) {
            model.createChat(backend: backend, model: modelName, cwd: cwd, permission: permission)
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
            Picker("权限", selection: $permission) {
                ForEach(permissions, id: \.self) { Text($0).tag($0) }
            }
            Text("代理会加载自己在该目录下的 MCP 服务器、技能和项目配置。")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .onAppear { if cwd.isEmpty { cwd = model.isLive ? model.defaultCwd : "~/Code/Hyadex" } }
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
                Text("消息里没有 @ 任何人时由默认回答者回答。成员只在被 @ 时才会被唤醒，只收到自己没看过的新消息。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
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
                    Image(systemName: "arrow.right").font(.caption).foregroundStyle(.tertiary)
                    AgentAvatar(backend: .codex, size: 16); Text("Codex 执行")
                }
                .font(.callout)
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
                    .font(.callout.monospaced())
                Text("验证失败的步骤一定不会通过。命令在 Codex 沙箱里运行：只能写工作目录，不能联网。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .onAppear { if cwd.isEmpty { cwd = model.isLive ? model.defaultCwd : "~/Code/Hyadex" } }
    }
}

/// Segmented choice with a one-line explanation of the consequence.
private struct OptionPicker<Option: Hashable>: View {
    let title: String
    @Binding var selection: Option
    let options: [Option]
    let label: KeyPath<Option, String>
    let explanation: KeyPath<Option, String>

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Picker(title, selection: $selection) {
                ForEach(options, id: \.self) { Text($0[keyPath: label]).tag($0) }
            }
            .pickerStyle(.segmented)
            Text(selection[keyPath: explanation])
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

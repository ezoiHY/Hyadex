import HyadexCore
import SwiftUI

/// An agent's questions, or an MCP server's form, answered in place. The
/// agent waits for the answer, so like an approval the card is tinted.
struct FormCard: View {
    let author: Author
    let form: InteractionForm
    let decision: ApprovalDecision
    var showsAuthor = true
    let onReply: (InteractionReply) -> Void

    /// Chosen option values (and "true"/"false" for switches), by field id.
    @State private var chosen: [String: [String]] = [:]
    /// Free text: answers, "other" answers and numbers, by field id.
    @State private var typed: [String: String] = [:]
    @State private var sent = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        if decision == .pending {
            pending
        } else {
            answered
        }
    }

    private var pending: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 7) {
                Image(systemName: "questionmark.bubble.fill").foregroundStyle(.orange)
                Text(showsAuthor ? "@\(author.name) \(form.title)" : form.title).font(.uiHeadline)
                Spacer()
                Text("等待你回答").font(.uiMeta).foregroundStyle(.orange)
            }
            if !form.message.isEmpty {
                Text(form.message).font(.uiBody).textSelection(.enabled)
            }
            ForEach(form.fields) { field in fieldView(field) }
            HStack(spacing: 8) {
                Button(form.kind == .questions ? "跳过" : "拒绝") { send(form.dismissal) }
                    .help(form.kind == .questions ? "不回答，让代理自己决定" : "拒绝提供这些信息")
                Spacer()
                if let url = form.url {
                    Button("打开链接") { openURL(url) }
                    Button("已完成") { send(form.reply([:])) }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                } else {
                    Button("提交") { send(form.reply(answers)) }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                        .keyboardShortcut(.return, modifiers: .command)
                        .help("提交（⌘↩）")
                        .disabled(!form.isComplete(answers))
                }
            }
            .controlSize(.regular)
            .disabled(sent)
        }
        .padding(12)
        .card(tint: .orange)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(showsAuthor ? author.name : "") \(form.title)")
    }

    private var answered: some View {
        HStack(spacing: 6) {
            Image(systemName: icon).foregroundStyle(decision == .denied ? Color.red : Color.green)
            Text(label)
            if case .answered(let summary) = decision, !summary.isEmpty {
                Text(summary).lineLimit(1).truncationMode(.tail).foregroundStyle(.secondary)
            }
        }
        .font(.uiSecondary)
    }

    private var label: String {
        switch decision {
        case .answered: form.kind == .questions ? "已回答" : "已提交"
        case .skipped: "已跳过"
        case .denied: "已拒绝"
        case .allowed: "已完成"
        default: "已处理"
        }
    }

    private var icon: String {
        switch decision {
        case .denied: "xmark.bubble"
        case .skipped: "arrowshape.turn.up.right"
        default: "checkmark.bubble"
        }
    }

    private func send(_ reply: InteractionReply) {
        guard !sent else { return }
        sent = true
        onReply(reply)
    }

    // MARK: Fields

    @ViewBuilder private func fieldView(_ field: FormField) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if !field.header.isEmpty { Tag(text: field.header) }
            if field.input != .toggle, !field.prompt.isEmpty {
                Text(field.prompt + (form.kind == .elicitation && field.required ? " *" : ""))
                    .font(.uiBody.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !field.detail.isEmpty {
                Text(field.detail).font(.uiSecondary).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            switch field.input {
            case .choice(let options, let multiple, let allowsOther):
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(options) { option in
                        OptionRow(option: option, multiple: multiple,
                                  isOn: selection(field).contains(option.value)) { toggle(option.value, in: field, multiple: multiple) }
                    }
                }
                if allowsOther {
                    TextField(multiple ? "其他（可选）" : "其他…", text: other(field, multiple: multiple), axis: .vertical)
                        .lineLimit(1...4)
                        .textFieldStyle(.roundedBorder)
                }
            case .text(let secret):
                if secret {
                    SecureField("回答", text: text(field)).textFieldStyle(.roundedBorder)
                } else {
                    TextField("回答", text: text(field), axis: .vertical)
                        .lineLimit(1...6)
                        .textFieldStyle(.roundedBorder)
                }
            case .number(let integer):
                TextField(integer ? "整数" : "数字", text: text(field))
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 200)
            case .toggle:
                Toggle(isOn: switchValue(field)) { Text(field.prompt).font(.uiBody) }
            }
        }
        .accessibilityElement(children: .contain)
    }

    /// What will be sent for each field.
    private var answers: [String: [String]] {
        Dictionary(uniqueKeysWithValues: form.fields.map { field in
            let text = (typed[field.id] ?? field.initialText).trimmingCharacters(in: .whitespacesAndNewlines)
            switch field.input {
            case .choice(_, let multiple, _):
                if multiple { return (field.id, selection(field) + (text.isEmpty ? [] : [text])) }
                return (field.id, text.isEmpty ? selection(field) : [text])
            case .toggle:
                return (field.id, [switchValue(field).wrappedValue ? "true" : "false"])
            case .text, .number:
                return (field.id, text.isEmpty ? [] : [text])
            }
        })
    }

    private func selection(_ field: FormField) -> [String] {
        chosen[field.id] ?? (field.initialText.isEmpty ? field.initial : [])
    }

    private func toggle(_ value: String, in field: FormField, multiple: Bool) {
        var values = selection(field)
        if multiple {
            if let index = values.firstIndex(of: value) { values.remove(at: index) } else { values.append(value) }
        } else {
            values = values == [value] ? [] : [value]
            // One answer: choosing an option replaces a typed one.
            typed[field.id] = ""
        }
        chosen[field.id] = values
    }

    private func other(_ field: FormField, multiple: Bool) -> Binding<String> {
        Binding {
            typed[field.id] ?? ""
        } set: { value in
            typed[field.id] = value
            // One answer: typing replaces a chosen option.
            if !multiple, !value.isEmpty { chosen[field.id] = [] }
        }
    }

    private func text(_ field: FormField) -> Binding<String> {
        Binding { typed[field.id] ?? field.initialText } set: { typed[field.id] = $0 }
    }

    private func switchValue(_ field: FormField) -> Binding<Bool> {
        Binding {
            (chosen[field.id] ?? field.initial).first == "true"
        } set: { chosen[field.id] = [$0 ? "true" : "false"] }
    }
}

/// One option of a question: a radio button or checkbox with its explanation.
private struct OptionRow: View {
    let option: FormOption
    let multiple: Bool
    let isOn: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: multiple ? (isOn ? "checkmark.square.fill" : "square")
                                           : (isOn ? "largecircle.fill.circle" : "circle"))
                    .foregroundStyle(isOn ? Color.accentColor : Color.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(option.label).font(.uiBody)
                    if !option.detail.isEmpty {
                        Text(option.detail).font(.uiSecondary).foregroundStyle(.secondary)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 6)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.accentColor.opacity(isOn ? 0.1 : 0)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(isOn ? .isSelected : [])
        .accessibilityLabel(option.detail.isEmpty ? option.label : "\(option.label)，\(option.detail)")
    }
}

private extension FormField {
    /// The starting text of a text or number field.
    var initialText: String {
        switch input {
        case .text, .number: initial.first ?? ""
        default: ""
        }
    }
}

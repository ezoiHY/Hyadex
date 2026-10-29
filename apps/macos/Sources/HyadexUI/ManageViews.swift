import HyadexCore
import SwiftUI
import QuickLook
import UniformTypeIdentifiers

// MARK: - Emergency alerts

/// Agents' emergency alerts, pinned over the conversation until acknowledged.
struct EmergencyBanners: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(spacing: 6) {
            ForEach(model.emergencies.prefix(3)) { alert in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: "exclamationmark.octagon.fill")
                        .foregroundStyle(.red)
                        .font(.uiTitle3)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text("紧急提醒").fontWeight(.semibold)
                            Text(alert.chatTitle).foregroundStyle(.secondary)
                            if alert.count > 1 {
                                Text("共 \(alert.count) 条").font(.uiMeta).foregroundStyle(.secondary)
                            }
                            if let raised = alert.raisedAt {
                                Text(raised, format: .relative(presentation: .named))
                                    .font(.uiMeta).foregroundStyle(.tertiary)
                            }
                        }
                        Text(alert.message)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    Button("打开") { model.select(.chat(alert.sessionID)) }
                        .disabled(model.chat(alert.sessionID) == nil)
                    Button("确认") { model.acknowledge(alert) }
                        .buttonStyle(.borderedProminent)
                        .tint(.red)
                }
                .font(.uiSecondary)
                .padding(12)
                // Opaque: an emergency must stay legible over any conversation.
                .card(tint: .red)
                .background(Color(nsColor: .windowBackgroundColor),
                            in: RoundedRectangle(cornerRadius: Theme.cornerRadius, style: .continuous))
                .shadow(color: .black.opacity(0.15), radius: 8, y: 2)
            }
        }
        .frame(maxWidth: Theme.readableWidth)
        .padding(.horizontal, 28)
        .padding(.top, 10)
    }
}

// MARK: - Rename

struct RenameSheet: View {
    @Bindable var model: AppModel
    let target: RenameTarget
    @State private var title = ""
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(isGroup ? "重命名讨论组" : "重命名聊天").font(.uiHeadline)
            TextField("名称", text: $title)
                .textFieldStyle(.roundedBorder)
                .onSubmit(save)
            HStack {
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("保存", action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(18)
        .frame(width: 380)
        .onAppear { title = target.title }
    }

    private var isGroup: Bool { if case .group = target.item { true } else { false } }

    private func save() {
        guard !title.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        model.rename(target.item, to: title)
        dismiss()
    }
}

// MARK: - Jobs

/// Creates or edits a scheduled job: what to send, where, and when.
struct JobSheet: View {
    @Bindable var model: AppModel
    let target: JobEditorTarget
    @Environment(\.dismiss) private var dismiss

    enum Cadence: String, CaseIterable, Identifiable {
        case daily, weekdays, weekly, every, once, cron
        var id: String { rawValue }
        var label: String {
            switch self {
            case .daily: "每天"
            case .weekdays: "工作日"
            case .weekly: "每周"
            case .every: "每隔"
            case .once: "只运行一次"
            case .cron: "cron 表达式"
            }
        }
    }

    enum Unit: Int, CaseIterable, Identifiable {
        case minutes = 60, hours = 3600, days = 86400
        var id: Int { rawValue }
        var label: String { self == .minutes ? "分钟" : self == .hours ? "小时" : "天" }
    }

    @State private var title = ""
    @State private var prompt = ""
    @State private var destination = ""
    @State private var cadence: Cadence = .daily
    @State private var time = Calendar.current.date(bySettingHour: 9, minute: 0, second: 0, of: .now) ?? .now
    @State private var weekday = 2
    @State private var amount = 1
    @State private var unit: Unit = .hours
    @State private var date = Date.now.addingTimeInterval(3600)
    @State private var cron = "0 0 9 * * *"
    @State private var enabled = true

    private var editing: ScheduledJob? {
        if case .edit(let id) = target { model.job(id) } else { nil }
    }

    private var canSubmit: Bool {
        !title.trimmingCharacters(in: .whitespaces).isEmpty && !prompt.trimmingCharacters(in: .whitespaces).isEmpty
            && (editing != nil || !destination.isEmpty) && (cadence != .cron || cron.split(separator: " ").count >= 6)
            && (cadence != .every || amount > 0)
    }

    var body: some View {
        VStack(spacing: 0) {
            Text(editing == nil ? "新建定时任务" : "编辑定时任务")
                .font(.uiHeadline)
                .padding(.top, 16)
            Form {
                TextField("名称", text: $title, prompt: Text("例如：每日依赖更新检查"))
                Section("发送的消息") {
                    TextField("消息", text: $prompt, prompt: Text("到时间时发给代理的内容"), axis: .vertical)
                        .labelsHidden()
                        .lineLimit(3...8)
                }
                if editing == nil {
                    Picker("发到", selection: $destination) {
                        Text("选择…").tag("")
                        Section("聊天") {
                            ForEach(model.chats.filter { !$0.archived }) { chat in
                                Text(chat.title).tag("chat:" + chat.id)
                            }
                        }
                        Section("讨论组") {
                            ForEach(model.groups.filter { !$0.archived }) { group in
                                Text(group.title).tag("group:" + group.id)
                            }
                        }
                    }
                } else if let editing {
                    LabeledContent("发到", value: editing.target)
                }
                Section("时间") {
                    Picker("频率", selection: $cadence) {
                        ForEach(Cadence.allCases) { Text($0.label).tag($0) }
                    }
                    switch cadence {
                    case .daily, .weekdays:
                        DatePicker("时间", selection: $time, displayedComponents: .hourAndMinute)
                    case .weekly:
                        Picker("星期", selection: $weekday) {
                            ForEach(1...7, id: \.self) { day in
                                Text(Calendar.current.weekdaySymbols[day - 1]).tag(day)
                            }
                        }
                        DatePicker("时间", selection: $time, displayedComponents: .hourAndMinute)
                    case .every:
                        HStack {
                            Stepper(value: $amount, in: 1...999) {
                                Text("每 \(amount)")
                            }
                            Picker("", selection: $unit) {
                                ForEach(Unit.allCases) { Text($0.label).tag($0) }
                            }
                            .labelsHidden()
                            .fixedSize()
                        }
                    case .once:
                        DatePicker("运行时间", selection: $date, in: Date.now..., displayedComponents: [.date, .hourAndMinute])
                    case .cron:
                        TextField("cron", text: $cron, prompt: Text("0 30 8 * * 1-5"))
                            .font(.uiSecondary.monospaced())
                            .help("六段：秒 分 时 日 月 星期。0 30 8 * * 1-5 是工作日 8:30")
                    }
                    Text(nextRunText)
                        .font(.uiMeta)
                        .foregroundStyle(.secondary)
                }
                Toggle("启用", isOn: $enabled)
            }
            .formStyle(.grouped)
            .scrollContentBackground(.hidden)
            Divider()
            HStack {
                if let editing {
                    Button("删除", role: .destructive) {
                        model.deleteJob(editing.id)
                        dismiss()
                    }
                }
                Spacer()
                Button("取消", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(editing == nil ? "创建" : "保存") {
                    submit()
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
            .padding(14)
        }
        .frame(width: 520)
        .onAppear(perform: load)
    }

    /// "下次运行：明天 09:00（Asia/Shanghai）", so the user can check the schedule before saving.
    private var nextRunText: String {
        let zone = "（\(TimeZone.current.identifier)）"
        guard let next = nextRun else { return "按这台 Mac 的时区" + zone }
        let day: String
        if Calendar.current.isDateInToday(next) { day = "今天" }
        else if Calendar.current.isDateInTomorrow(next) { day = "明天" }
        else { day = next.formatted(.dateTime.month().day().weekday()) }
        return "下次运行：\(day) \(next.formatted(date: .omitted, time: .shortened))" + zone
    }

    private var nextRun: Date? {
        let calendar = Calendar.current
        let parts = calendar.dateComponents([.hour, .minute], from: time)
        switch cadence {
        case .daily:
            return next(DateComponents(hour: parts.hour, minute: parts.minute))
        case .weekdays:
            var candidate = next(DateComponents(hour: parts.hour, minute: parts.minute))
            while let date = candidate, calendar.isDateInWeekend(date) {
                candidate = calendar.nextDate(after: date, matching: DateComponents(hour: parts.hour, minute: parts.minute),
                                              matchingPolicy: .nextTime)
            }
            return candidate
        case .weekly:
            return next(DateComponents(hour: parts.hour, minute: parts.minute, weekday: weekday))
        case .every:
            return Date.now.addingTimeInterval(Double(amount * unit.rawValue))
        case .once:
            return date
        case .cron:
            return nil
        }
    }

    private func next(_ components: DateComponents) -> Date? {
        Calendar.current.nextDate(after: .now, matching: components, matchingPolicy: .nextTime)
    }

    private var schedule: JobSchedule {
        let parts = Calendar.current.dateComponents([.hour, .minute], from: time)
        let hour = parts.hour ?? 9, minute = parts.minute ?? 0
        switch cadence {
        case .daily: return .cron("0 \(minute) \(hour) * * *")
        case .weekdays: return .cron("0 \(minute) \(hour) * * 1-5")
        // Calendar weekdays start at Sunday = 1; cron's at Sunday = 0.
        case .weekly: return .cron("0 \(minute) \(hour) * * \(weekday - 1)")
        case .every: return .interval(seconds: amount * unit.rawValue, repeats: true, firstRun: nil)
        case .once: return .interval(seconds: max(60, Int(date.timeIntervalSinceNow)), repeats: false, firstRun: date)
        case .cron: return .cron(cron.trimmingCharacters(in: .whitespaces))
        }
    }

    private func submit() {
        if let editing {
            model.updateJob(editing.id, title: title, prompt: prompt, schedule: schedule, enabled: enabled)
            return
        }
        let parts = destination.split(separator: ":", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return }
        model.createJob(title: title, prompt: prompt, target: parts[0] == "group" ? .group(parts[1]) : .chat(parts[1]),
                        schedule: schedule, enabled: enabled)
    }

    private func load() {
        if case .chat(let id)? = model.selection { destination = "chat:" + id }
        guard let editing else { return }
        title = editing.title
        prompt = editing.prompt
        enabled = editing.enabled
        switch editing.spec {
        case .interval(let seconds, let repeats, let firstRun)?:
            if repeats {
                cadence = .every
                unit = seconds % 86_400 == 0 ? .days : seconds % 3_600 == 0 ? .hours : .minutes
                amount = max(1, seconds / unit.rawValue)
            } else {
                cadence = .once
                date = firstRun ?? date
            }
        case .cron(let expression)?:
            cron = expression
            let fields = expression.split(separator: " ").map(String.init)
            if fields.count == 6, fields[0] == "0", let minute = Int(fields[1]), let hour = Int(fields[2]),
               fields[3] == "*", fields[4] == "*",
               let parsed = Calendar.current.date(bySettingHour: hour, minute: minute, second: 0, of: .now) {
                time = parsed
                if fields[5] == "*" {
                    cadence = .daily
                } else if fields[5] == "1-5" {
                    cadence = .weekdays
                } else if let day = Int(fields[5]), (0...6).contains(day) {
                    cadence = .weekly
                    weekday = day + 1
                } else {
                    cadence = .cron
                }
            } else {
                cadence = .cron
            }
        case .rrule?, nil:
            cadence = .cron
        }
    }
}

// MARK: - Server folders

/// A working-directory field that browses the server's folders. A local
/// folder picker is offered only when the server runs on this Mac.
struct ServerFolderField: View {
    @Bindable var model: AppModel
    @Binding var path: String
    @State private var suggestions: [DirectorySuggestion] = []
    @State private var message: String?
    @State private var picking = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            LabeledContent("工作目录") {
                HStack {
                    TextField("", text: $path, prompt: Text("~/Code/project"))
                        .labelsHidden()
                        .font(.uiSecondary.monospaced())
                        .focused($focused)
                    if model.serverIsLocal {
                        Button("选择…") { picking = true }
                    }
                }
            }
            if !suggestions.isEmpty {
                // A short, scannable list: click a folder to go into it.
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if path.contains("/"), Self.parent(of: path) != path {
                            FolderRow(name: "上一级", symbol: "arrow.turn.left.up", secondary: true) {
                                path = Self.parent(of: path)
                            }
                        }
                        ForEach(suggestions) { folder in
                            FolderRow(name: folder.name, symbol: "folder", secondary: false) {
                                path = folder.path
                            }
                            .help(folder.path)
                        }
                    }
                    .padding(4)
                }
                .frame(maxHeight: 150)
                .fixedSize(horizontal: false, vertical: suggestions.count < 5)
                .card()
            }
            if let message {
                Text(message).font(.uiMeta).foregroundStyle(.secondary)
            }
        }
        .task(id: path) {
            try? await Task.sleep(for: .milliseconds(200))
            guard !Task.isCancelled else { return }
            let result = await model.directorySuggestions(path)
            suggestions = Array(result.folders.prefix(24))
            message = result.message
        }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result {
                path = url.path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
            }
        }
    }

    static func parent(of path: String) -> String {
        var trimmed = path
        while trimmed.count > 1, trimmed.hasSuffix("/") { trimmed.removeLast() }
        guard let slash = trimmed.lastIndex(of: "/") else { return path }
        let parent = String(trimmed[...slash])
        return parent.isEmpty ? "/" : parent
    }
}

private struct FolderRow: View {
    let name: String
    let symbol: String
    let secondary: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Image(systemName: symbol)
                    .foregroundStyle(secondary ? Color.secondary : Color.accentColor)
                    .frame(width: 16)
                Text(name)
                    .foregroundStyle(secondary ? .secondary : .primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                if !secondary {
                    Image(systemName: "chevron.right").font(.uiSmall).foregroundStyle(.tertiary)
                }
            }
            .font(.uiSecondary)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: Theme.smallRadius, style: .continuous)
                    .fill(hovering ? Color.accentColor.opacity(0.12) : .clear)
            )
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

// MARK: - Attachments

/// Files added to the composer, with their upload state.
/// The composer's files: pictures as square thumbnails (click to preview),
/// other files as name chips. Each shows its upload state and can be removed.
struct AttachmentStrip: View {
    let attachments: [DraftAttachment]
    let onRemove: (String) -> Void
    @State private var preview: URL?

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .bottom, spacing: 8) {
                ForEach(attachments) { attachment in
                    if let url = attachment.localURL, Self.isImage(url) {
                        ImageThumbnail(attachment: attachment, url: url, failure: failure(attachment),
                                       onOpen: { preview = url }, onRemove: { onRemove(attachment.id) })
                    } else {
                        chip(attachment)
                    }
                }
            }
            // Room for the thumbnails' remove buttons, which sit over their corners.
            .padding(.top, 6)
            .padding(.trailing, 6)
        }
        .quickLookPreview($preview)
    }

    static func isImage(_ url: URL) -> Bool {
        UTType(filenameExtension: url.pathExtension)?.conforms(to: .image) == true
    }

    private func chip(_ attachment: DraftAttachment) -> some View {
        HStack(spacing: 5) {
            switch attachment.state {
            case .uploading: ProgressView().controlSize(.mini)
            case .ready: Image(systemName: "doc").foregroundStyle(.secondary)
            case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            }
            Text(attachment.name).lineLimit(1).truncationMode(.middle)
            if attachment.size > 0 {
                Text(ByteCountFormatter.string(fromByteCount: attachment.size, countStyle: .file))
                    .foregroundStyle(.secondary)
            }
            Button {
                onRemove(attachment.id)
            } label: {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("移除")
        }
        .font(.uiMeta)
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(Capsule().fill(Color.primary.opacity(0.06)))
        .help(failure(attachment) ?? attachment.name)
    }

    private func failure(_ attachment: DraftAttachment) -> String? {
        if case .failed(let message) = attachment.state { "上传失败：\(message)" } else { nil }
    }
}

/// A picture in the composer, cropped to a square.
private struct ImageThumbnail: View {
    let attachment: DraftAttachment
    let url: URL
    let failure: String?
    let onOpen: () -> Void
    let onRemove: () -> Void
    @State private var image: NSImage?
    @State private var hovering = false

    private static let side: CGFloat = 64

    var body: some View {
        Button(action: onOpen) {
            ZStack {
                Color.primary.opacity(0.06)
                if let image {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.high)
                        .aspectRatio(contentMode: .fill)
                } else {
                    Image(systemName: "photo").foregroundStyle(.secondary)
                }
                if attachment.state == .uploading {
                    Color.black.opacity(0.35)
                    ProgressView().controlSize(.small).tint(.white)
                }
            }
            .frame(width: Self.side, height: Self.side)
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(failure == nil ? Color(nsColor: .separatorColor) : Color.orange,
                                  lineWidth: failure == nil ? 0.5 : 1.5)
            )
            .overlay(alignment: .bottomLeading) {
                if failure != nil {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 12))
                        .foregroundStyle(.orange)
                        .padding(4)
                }
            }
        }
        .buttonStyle(.plain)
        .overlay(alignment: .topTrailing) {
            Button(action: onRemove) {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 15))
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, Color.black.opacity(0.65))
            }
            .buttonStyle(.plain)
            .offset(x: 6, y: -6)
            .opacity(hovering ? 1 : 0)
            .help("移除")
            .accessibilityLabel("移除 \(attachment.name)")
        }
        .onHover { hovering = $0 }
        .help(failure ?? "\(attachment.name) · \(ByteCountFormatter.string(fromByteCount: attachment.size, countStyle: .file))")
        .accessibilityLabel("图片 \(attachment.name)")
        .task(id: url) { image = await Self.thumbnail(url, pixels: Self.side * 3) }
    }

    /// A downsampled copy, read off the main thread.
    static func thumbnail(_ url: URL, pixels: CGFloat) async -> NSImage? {
        await Task.detached(priority: .userInitiated) {
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: pixels,
            ]
            guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
            return NSImage(cgImage: cgImage, size: NSSize(width: cgImage.width, height: cgImage.height))
        }.value
    }
}

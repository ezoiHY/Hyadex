import HyadexAPI
import HyadexCore
import SwiftUI
@preconcurrency import UserNotifications

public struct SettingsView: View {
    @Bindable var model: AppModel

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some View {
        TabView {
            Tab("通用", systemImage: "gearshape") { GeneralSettings() }
            Tab("服务器", systemImage: "server.rack") { ServerSettings(model: model) }
            Tab("通知", systemImage: "bell.badge") { NotificationSettings() }
        }
        .frame(width: 560, height: 420)
    }
}

private struct GeneralSettings: View {
    @AppStorage("sendWithCommandReturn") private var sendWithCommandReturn = false
    @AppStorage("expandThinking") private var expandThinking = false
    @AppStorage("showUsage") private var showUsage = true

    var body: some View {
        Form {
            Section("输入") {
                Picker("发送消息", selection: $sendWithCommandReturn) {
                    Text("↩ 发送，⇧↩ 换行").tag(false)
                    Text("⌘↩ 发送，↩ 换行").tag(true)
                }
            }
            Section("时间线") {
                Toggle("默认展开每轮的处理过程", isOn: $expandThinking)
                Toggle("在边栏底部显示套餐用量", isOn: $showUsage)
            }
        }
        .formStyle(.grouped)
    }
}

private struct NotificationSettings: View {
    @AppStorage("notifyApproval") private var notifyApproval = true
    @AppStorage("notifyFinished") private var notifyFinished = true
    @AppStorage("notifyOnlyInBackground") private var notifyOnlyInBackground = true
    @AppStorage("dockBadge") private var dockBadge = true

    @State private var permission: UNAuthorizationStatus?

    var body: some View {
        Form {
            Section {
                LabeledContent("系统通知") {
                    HStack(spacing: 8) {
                        Text(permissionLabel).foregroundStyle(permission == .denied ? Color.orange : .secondary)
                        switch permission {
                        case .notDetermined?:
                            Button("允许通知") {
                                Task {
                                    _ = try? await UNUserNotificationCenter.current()
                                        .requestAuthorization(options: [.alert, .sound])
                                    await refresh()
                                }
                            }
                        case .denied?:
                            Button("打开系统设置") {
                                if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
                                    NSWorkspace.shared.open(url)
                                }
                            }
                        default:
                            EmptyView()
                        }
                    }
                }
            } footer: {
                Text("代理的紧急提醒总会以红色横幅显示在窗口里；允许系统通知后，Hyadex 在后台时也会弹出通知。")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                Toggle("代理需要批准时", isOn: $notifyApproval)
                Toggle("一轮回复完成时", isOn: $notifyFinished)
                Toggle("只在 Hyadex 不在前台时通知", isOn: $notifyOnlyInBackground)
            }
            Section {
                Toggle("在 Dock 图标上显示待处理数量", isOn: $dockBadge)
            }
        }
        .formStyle(.grouped)
        .task { await refresh() }
    }

    private var permissionLabel: String {
        switch permission {
        case .authorized?, .provisional?: "已允许"
        case .denied?: "已关闭"
        case .notDetermined?: "尚未询问"
        default: "—"
        }
    }

    private func refresh() async {
        permission = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }
}

private struct ServerSettings: View {
    @Bindable var model: AppModel
    @State private var selection: ServerEndpoint.ID?
    @State private var token = ""
    @State private var status: String?
    @State private var busy = false

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                List(model.servers, selection: $selection) { server in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(server.name)
                            Text(server.baseURL.absoluteString).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if model.isLive && server.id == model.currentServerID {
                            Circle().fill(.green).frame(width: 7, height: 7).help("已连接")
                        }
                    }
                    .tag(server.id)
                }
                Divider()
                HStack(spacing: 0) {
                    Button {
                        let server = ServerEndpoint(name: "新服务器", baseURL: URL(string: "http://127.0.0.1:7870")!)
                        model.servers.append(server)
                        model.saveServers()
                        selection = server.id
                    } label: { Image(systemName: "plus").frame(width: 24, height: 20) }
                    .help("添加服务器")
                    Button {
                        guard let id = selection else { return }
                        TokenStore.setToken(nil, for: id)
                        model.servers.removeAll { $0.id == id }
                        model.saveServers()
                        selection = model.servers.first?.id
                    } label: { Image(systemName: "minus").frame(width: 24, height: 20) }
                    .disabled(model.servers.count <= 1)
                    .help("删除服务器")
                    Spacer()
                }
                .buttonStyle(.borderless)
                .padding(4)
            }
            .frame(width: 190)
            Divider()
            if let index = model.servers.firstIndex(where: { $0.id == selection }) {
                Form {
                    TextField("名称", text: $model.servers[index].name)
                    TextField("地址", text: Binding(
                        get: { model.servers[index].baseURL.absoluteString },
                        set: { if let url = URL(string: $0), url.scheme != nil { model.servers[index].baseURL = url } }))
                        .font(.callout.monospaced())
                    SecureField("访问令牌", text: $token, prompt: Text("粘贴服务器的访问令牌"))
                    HStack {
                        Button("测试") { test(model.servers[index]) }
                        Button(isConnected(index) ? "重新连接" : "连接") { connect(model.servers[index]) }
                            .buttonStyle(.borderedProminent)
                        if isConnected(index) {
                            Button("断开") { model.disconnect() }
                        }
                        if busy { ProgressView().controlSize(.small) }
                    }
                    .disabled(busy)
                    if let status {
                        Text(status).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    Text("""
                    支持局域网和 Tailscale 地址。令牌只通过 Authorization 头发送，并保存在钥匙串中。\
                    用 `install.sh --show-token` 查看已安装服务器的令牌。
                    """)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .formStyle(.grouped)
                .onChange(of: model.servers[index]) { model.saveServers() }
            } else {
                ContentUnavailableView("选择一个服务器", systemImage: "server.rack")
            }
        }
        .onAppear { selection = selection ?? model.currentServerID }
        .onChange(of: selection, initial: true) {
            status = nil
            token = selection.flatMap(TokenStore.token(for:)) ?? ""
        }
    }

    private func isConnected(_ index: Int) -> Bool {
        model.isLive && model.servers[index].id == model.currentServerID
    }

    private func test(_ server: ServerEndpoint) {
        busy = true
        status = nil
        Task {
            switch await model.testConnection(to: server, token: token) {
            case .success(let health):
                var parts = ["✅ 可以连接"]
                if let version = health.version { parts.append("v\(version)") }
                parts.append(health.supportsGroups ? "讨论组 ✓" : "讨论组 ✗")
                parts.append(health.supportsAutopilot ? "自动接力 ✓" : "自动接力 ✗")
                status = parts.joined(separator: " · ")
            case .failure(let error):
                status = "❌ " + error.localizedDescription
            }
            busy = false
        }
    }

    private func connect(_ server: ServerEndpoint) {
        busy = true
        status = nil
        Task {
            await model.connect(to: server, token: token)
            if case .connected = model.connection {
                status = "✅ 已连接到 \(server.name)"
            } else {
                status = "❌ " + (model.errorMessage ?? "连接失败")
            }
            busy = false
        }
    }
}

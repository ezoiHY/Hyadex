import HyadexAPI
import HyadexCore
import SwiftUI
@preconcurrency import UserNotifications

public struct SettingsView: View {
    @Bindable var model: AppModel
    @AppStorage("settingsTab") private var tab = "general"

    public init(model: AppModel) {
        self.model = model
    }

    public var body: some View {
        TabView(selection: $tab) {
            Tab("通用", systemImage: "gearshape", value: "general") { GeneralSettings() }
            Tab("服务器", systemImage: "server.rack", value: "server") { ServerSettings(model: model) }
            Tab("通知", systemImage: "bell.badge", value: "notifications") { NotificationSettings() }
        }
        .frame(width: 680, height: 520)
    }
}

private struct GeneralSettings: View {
    @AppStorage("sendWithCommandReturn") private var sendWithCommandReturn = false
    @AppStorage(TextSize.key) private var textSize = TextSize.standard
    @AppStorage("expandThinking") private var expandThinking = false
    @AppStorage("showUsage") private var showUsage = true

    var body: some View {
        Form {
            Section("显示") {
                Picker("字号", selection: $textSize) {
                    ForEach(TextSize.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                .fixedSize()
            }
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
                Text("紧急提醒总会在窗口里显示红色横幅")
                    .font(.uiMeta)
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
        VStack(spacing: 0) {
            LocalServerControls(model: model)
                .padding(18)
            Divider()
            serverList
        }
        .onAppear { selection = selection ?? model.currentServerID }
        .onChange(of: model.currentServerID) { selection = model.currentServerID }
        .onChange(of: selection, initial: true) {
            status = nil
            token = selection.flatMap(TokenStore.token(for:)) ?? ""
        }
    }

    private var serverList: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                List(model.servers, selection: $selection) { server in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(server.name)
                            Text(server.baseURL.absoluteString).font(.uiMeta).foregroundStyle(.secondary)
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
                        if id == model.currentServerID { model.disconnect() }
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
                    if model.servers[index].id == model.managedLocalServerID {
                        LabeledContent("名称", value: "这台 Mac")
                        LabeledContent("地址", value: model.servers[index].baseURL.absoluteString)
                            .font(.uiSecondary.monospaced())
                        Text("由 Hyadex 自动管理")
                            .font(.uiMeta).foregroundStyle(.secondary)
                    } else {
                        TextField("名称", text: $model.servers[index].name)
                        TextField("地址", text: Binding(
                            get: { model.servers[index].baseURL.absoluteString },
                            set: { if let url = URL(string: $0), url.scheme != nil { model.servers[index].baseURL = url } }))
                            .font(.uiSecondary.monospaced())
                        SecureField("访问令牌", text: $token, prompt: Text("粘贴服务器的访问令牌"))
                    }
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
                        Text(status).font(.uiSecondary).foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    if model.servers[index].id != model.managedLocalServerID {
                      Text("""
                    支持局域网和 Tailscale 地址。令牌只通过 Authorization 头发送，并保存在钥匙串中。\
                    用 `install.sh --show-token` 查看已安装服务器的令牌。
                    """)
                        .font(.uiMeta)
                        .foregroundStyle(.secondary)
                    }
                }
                .formStyle(.grouped)
                .onChange(of: model.servers[index]) { model.saveServers() }
            } else {
                ContentUnavailableView("选择一个服务器", systemImage: "server.rack")
            }
        }
    }

    private func isConnected(_ index: Int) -> Bool {
        model.isLive && model.servers[index].id == model.currentServerID
    }

    private func test(_ server: ServerEndpoint) {
        busy = true
        status = nil
        let accessToken = token
        Task {
            let result = await model.testConnection(to: server, token: accessToken)
            busy = false
            guard selection == server.id else { return }
            switch result {
            case .success(let health):
                var parts = ["✅ 可以连接"]
                if let version = health.version { parts.append("v\(version)") }
                parts.append(health.supportsGroups ? "讨论组 ✓" : "讨论组 ✗")
                parts.append(health.supportsAutopilot ? "自动接力 ✓" : "自动接力 ✗")
                status = parts.joined(separator: " · ")
            case .failure(let error):
                status = "❌ " + error.localizedDescription
            }
        }
    }

    private func connect(_ server: ServerEndpoint) {
        busy = true
        status = nil
        let accessToken = token
        Task {
            if server.id == model.managedLocalServerID {
                await model.startLocalServer()
            } else {
                await model.connect(to: server, token: accessToken)
            }
            busy = false
            guard selection == server.id else { return }
            if case .connected = model.connection {
                status = "✅ 已连接到 \(server.name)"
            } else {
                status = "❌ " + (model.errorMessage ?? "连接失败")
            }
        }
    }
}

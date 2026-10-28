import HyadexCore
import SwiftUI

/// The local server is a real, app-owned process. Connection failures keep
/// this entry point available rather than replacing server data with samples.
struct ServerWelcomeView: View {
    @Bindable var model: AppModel
    @Environment(\.openSettings) private var openSettings
    @AppStorage("settingsTab") private var settingsTab = "general"

    var body: some View {
        VStack(spacing: 22) {
            Image(systemName: "desktopcomputer")
                .font(.system(size: 42, weight: .light))
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            VStack(spacing: 8) {
                Text("开始使用 Hyadex").font(.title2.bold())
                Text("在这台 Mac 上启动服务，或连接已有服务器。")
                    .foregroundStyle(.secondary)
            }
            LocalServerControls(model: model, welcome: true)
            Button("连接已有服务器…") {
                settingsTab = "server"
                openSettings()
            }
            .buttonStyle(.link)
            if case .offline(let reason) = model.connection {
                Label(reason, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: 480)
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct LocalServerControls: View {
    @Bindable var model: AppModel
    var welcome = false

    private var connecting: Bool { model.connection == .connecting }
    private var busy: Bool { model.localServer.isStarting || model.localServer.state == .stopping || connecting }

    var body: some View {
        VStack(alignment: welcome ? .center : .leading, spacing: 10) {
            if !welcome {
                HStack {
                    Label("这台 Mac 的本地服务", systemImage: "desktopcomputer").font(.headline)
                    Spacer()
                    startButton
                }
            } else {
                startButton
                    .controlSize(.large)
            }
            if busy {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(model.localServer.isStarting || model.localServer.state == .stopping
                         ? model.localServer.statusText : "正在连接服务器…")
                        .font(.callout)
                }
                .accessibilityElement(children: .combine)
            } else if let error = model.localServer.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .textSelection(.enabled)
            } else if !welcome {
                Text(model.localServer.statusText).font(.callout).foregroundStyle(.secondary)
            }
            Text("本地服务随 Hyadex 运行；退出应用时停止，聊天记录会保留。")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(welcome ? .center : .leading)
        }
    }

    private var startButton: some View {
        Button {
            Task { await model.startLocalServer() }
        } label: {
            Label(busy ? "正在准备…" : model.localServer.isRunning ? "连接本地服务" : "启动本地服务", systemImage: "play.fill")
        }
        .buttonStyle(.borderedProminent)
        .disabled(busy)
        .accessibilityIdentifier("startLocalServer")
    }
}

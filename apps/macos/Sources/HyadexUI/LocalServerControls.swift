import CoreImage.CIFilterBuiltins
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
                Text("开始使用 Hyadex").font(.uiTitle2.bold())
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
                    .font(.uiSecondary)
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
                    Label("这台 Mac 的本地服务", systemImage: "desktopcomputer").font(.uiHeadline)
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
                        .font(.uiSecondary)
                }
                .accessibilityElement(children: .combine)
            } else if let error = model.localServer.errorMessage {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.uiSecondary)
                    .foregroundStyle(.orange)
                    .textSelection(.enabled)
            } else if !welcome {
                Text(model.localServer.statusText).font(.uiSecondary).foregroundStyle(.secondary)
            }
            Text("退出 Hyadex 时停止，聊天记录会保留")
                .font(.uiMeta)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(welcome ? .center : .leading)
            if !welcome {
                Toggle(isOn: Binding(get: { model.phoneAccess }, set: { on in Task { await model.setPhoneAccess(on) } })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("允许手机连接")
                        Text("同一 Wi-Fi 或 Tailscale 上的手机可以连接（端口 \(String(LocalServerConfiguration.phonePort))），需要令牌")
                            .font(.uiMeta)
                            .foregroundStyle(.secondary)
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("phoneAccess")
                if model.phoneAccess, model.localServer.isRunning, !busy {
                    PhonePairing(model: model)
                }
            }
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

/// A code the Hyadex Android app scans (or any camera opens) to connect to this
/// Mac: the address and the token, one code per address the phone may use.
private struct PhonePairing: View {
    let model: AppModel
    @State private var links: [(address: String, link: String)] = []
    @State private var selected = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if links.isEmpty {
                Text("没有找到局域网或 Tailscale 地址").font(.uiMeta).foregroundStyle(.secondary)
            } else {
                if links.count > 1 {
                    Picker("地址", selection: $selected) {
                        ForEach(links.indices, id: \.self) { Text(links[$0].address).tag($0) }
                    }
                    .pickerStyle(.menu)
                    .fixedSize()
                }
                if let image = Self.code(links[min(selected, links.count - 1)].link) {
                    Image(nsImage: image)
                        .interpolation(.none)
                        .resizable()
                        .frame(width: 168, height: 168)
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: Theme.smallRadius).fill(.white))
                        .accessibilityLabel("连接二维码")
                }
                Text("在手机上用 Hyadex 扫码连接 \(links[min(selected, links.count - 1)].address)。二维码里有令牌，不要分享给别人。")
                    .font(.uiMeta)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { links = await model.phoneLinks() }
    }

    /// A QR code drawn with sharp modules.
    static func code(_ text: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(text.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)) else { return nil }
        let representation = NSCIImageRep(ciImage: output)
        let image = NSImage(size: representation.size)
        image.addRepresentation(representation)
        return image
    }
}

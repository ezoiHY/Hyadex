import HyadexCore
import HyadexTerminal
import SwiftUI

/// The chat's persistent terminal, docked under the conversation.
///
/// It attaches to the chat's tmux session on the server: closing the panel
/// only detaches, and commands keep running. The agent sees the same session
/// (read-only unless asked), so both can follow one build or server.
struct TerminalDock: View {
    @Bindable var model: AppModel
    let chatID: String
    @State private var dragStart: Double?
    @State private var confirmingKill = false

    var body: some View {
        VStack(spacing: 0) {
            resizeHandle
            header
            Divider()
            content
        }
        .frame(height: model.terminalHeight)
        .task(id: chatID) {
            // tmux windows change from the shell too (new-window, exit).
            while !Task.isCancelled {
                await model.refreshTerminalWindows(chatID)
                try? await Task.sleep(for: .seconds(2))
            }
        }
        .confirmationDialog("结束这个终端会话？", isPresented: $confirmingKill) {
            Button("结束会话", role: .destructive) { model.killTerminal(chatID) }
        } message: {
            Text("会话里正在运行的命令和所有窗口都会被关闭。")
        }
    }

    private var resizeHandle: some View {
        Rectangle()
            .fill(.clear)
            .frame(height: 5)
            .contentShape(Rectangle())
            .onHover { inside in
                if inside { NSCursor.resizeUpDown.push() } else { NSCursor.pop() }
            }
            .gesture(DragGesture(minimumDistance: 1, coordinateSpace: .global)
                .onChanged { value in
                    let start = dragStart ?? model.terminalHeight
                    dragStart = start
                    model.terminalHeight = min(max(start - value.translation.height, 140), 900)
                }
                .onEnded { _ in dragStart = nil })
            .onTapGesture(count: 2) { model.terminalHeight = 260 }
            .accessibilityLabel("调整终端高度")
    }

    private var windows: [TerminalWindow] { model.terminalWindows[chatID] ?? [] }

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "terminal")
                .foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 2) {
                    ForEach(windows) { window in
                        WindowTab(window: window, canClose: windows.count > 1,
                                  select: { model.terminalAction(chatID, "select-window", target: String(window.index)) },
                                  close: { model.terminalAction(chatID, "kill-window", target: String(window.index)) })
                    }
                }
            }
            Button {
                model.terminalAction(chatID, "new-window")
            } label: {
                Image(systemName: "plus")
            }
            .help("新建窗口")
            .disabled(windows.isEmpty)
            Spacer(minLength: 4)
            Menu {
                Button("向右分屏") { model.terminalAction(chatID, "split-right") }
                Button("向下分屏") { model.terminalAction(chatID, "split-down") }
                Divider()
                Button("关闭当前窗格") { model.terminalAction(chatID, "kill-pane") }
            } label: {
                Image(systemName: "rectangle.split.2x1")
            }
            .menuIndicator(.hidden)
            .fixedSize()
            .help("分屏")
            .disabled(windows.isEmpty)
            Menu {
                Button("重新连接") { model.terminalSession(for: chatID)?.reconnect() }
                Divider()
                Button("结束终端会话…", role: .destructive) { confirmingKill = true }
            } label: {
                Image(systemName: "ellipsis")
            }
            .menuIndicator(.hidden)
            .fixedSize()
            .help("更多")
            Button {
                model.toggleTerminal(chatID)
            } label: {
                Image(systemName: "chevron.down")
            }
            .help("隐藏终端（⌃`）")
        }
        .buttonStyle(.borderless)
        .font(.callout)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(.bar)
    }

    @ViewBuilder private var content: some View {
        if let session = model.terminalSession(for: chatID) {
            ZStack {
                TerminalSurface(session: session)
                    .accessibilityLabel("终端")
                if session.phase != .connected {
                    status(session)
                }
            }
            .onAppear { session.connect(columns: 100, rows: 24) }
            .onDisappear { session.disconnect() }
        } else {
            Text("连接服务器后可以使用终端")
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private func status(_ session: TerminalSession) -> some View {
        VStack(spacing: 10) {
            switch session.phase {
            case .idle, .connecting:
                ProgressView().controlSize(.small)
                Text("正在连接终端…").foregroundStyle(.secondary)
            case .ended:
                Text("终端会话已结束").foregroundStyle(.secondary)
                Button("打开新会话") { session.reconnect() }
            case .failed(let message):
                Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Button("重试") { session.reconnect() }
            case .connected:
                EmptyView()
            }
        }
        .padding(16)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: Theme.cornerRadius))
    }
}

private struct WindowTab: View {
    let window: TerminalWindow
    let canClose: Bool
    let select: () -> Void
    let close: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 4) {
            Text("\(window.index):\(window.name)")
                .lineLimit(1)
            if window.panes > 1 {
                Text("\(window.panes)")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
                    .help("\(window.panes) 个窗格")
            }
            if canClose && hovering {
                Button(action: close) {
                    Image(systemName: "xmark").font(.caption2)
                }
                .buttonStyle(.borderless)
                .help("关闭窗口")
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(window.active ? Color.primary.opacity(0.1) : .clear,
                    in: RoundedRectangle(cornerRadius: Theme.smallRadius))
        .contentShape(Rectangle())
        .onTapGesture(perform: select)
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(window.active ? [.isSelected, .isButton] : .isButton)
    }
}

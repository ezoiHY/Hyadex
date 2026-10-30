import Darwin
import Foundation
import HyadexAPI

/// A foreground backend owned by this app, with the runner's standard named
/// instance layout. A different home is useful for isolated acceptance checks.
public struct LocalServerConfiguration: Sendable {
    public var sourceDirectory: URL?
    public var homeDirectory: URL
    public var instanceName: String
    public var port: Int
    /// Phones on the LAN or tailnet may connect: the backend listens on every
    /// interface at a fixed port (unless one was given), so a paired phone keeps working.
    public var phoneAccess: Bool
    public var startupTimeout: TimeInterval
    public var shutdownTimeout: TimeInterval

    public init(sourceDirectory: URL?, homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
                instanceName: String = "mac-app", port: Int = 0, phoneAccess: Bool = false,
                startupTimeout: TimeInterval = 35, shutdownTimeout: TimeInterval = 80) {
        self.sourceDirectory = sourceDirectory.map(Self.canonicalDirectory)
        self.homeDirectory = Self.canonicalDirectory(homeDirectory)
        self.instanceName = instanceName
        self.port = port
        self.phoneAccess = phoneAccess
        self.startupTimeout = startupTimeout
        self.shutdownTimeout = shutdownTimeout
    }

    /// Source builds record their checkout in the app bundle. An explicit
    /// override is authoritative, including an invalid path: never silently run
    /// a different checkout when an acceptance check asked for a particular one.
    public static func current(environment: [String: String] = ProcessInfo.processInfo.environment,
                               bundle: Bundle = .main) -> Self {
        let source: URL?
        if let path = environment["HYADEX_LOCAL_SERVER_SOURCE"] {
            source = path.isEmpty ? nil : URL(fileURLWithPath: path, isDirectory: true)
        } else if let path = bundle.object(forInfoDictionaryKey: "HyadexLocalServerSourcePath") as? String {
            source = URL(fileURLWithPath: path, isDirectory: true)
        } else {
            // `swift run` has no application Info.plist.
            source = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
        }
        let home = environment["HYADEX_LOCAL_SERVER_HOME"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.homeDirectoryForCurrentUser
        return Self(sourceDirectory: source, homeDirectory: home,
                    instanceName: environment["HYADEX_LOCAL_SERVER_INSTANCE"] ?? "mac-app",
                    port: environment["HYADEX_LOCAL_SERVER_PORT"].map { Int($0) ?? -1 } ?? 0,
                    phoneAccess: UserDefaults.standard.bool(forKey: phoneAccessKey))
    }

    /// 设置 › 服务器 › 允许手机连接.
    public static let phoneAccessKey = "localServerPhoneAccess"
    /// The port phones pair with: the server's documented default.
    public static let phonePort = 7870

    var bindAddress: String { phoneAccess ? "0.0.0.0" : "127.0.0.1" }
    var launchPort: Int { port != 0 ? port : phoneAccess ? Self.phonePort : 0 }

    public var stateDirectory: URL { homeDirectory.appending(path: ".hyadex-instances/\(instanceName)") }
    public var configDirectory: URL { homeDirectory.appending(path: ".config/hyadex-server-instances/\(instanceName)") }
    var runtimeDirectory: URL { homeDirectory.appending(path: ".local/share/hyadex-server-instances/\(instanceName)") }
    var tokenFile: URL { configDirectory.appending(path: "dev-token") }
    var ownershipFile: URL { configDirectory.appending(path: "mac-app.json") }

    private static func canonicalDirectory(_ url: URL) -> URL {
        // Foundation deliberately preserves /var and /tmp aliases on macOS;
        // Python Path.resolve() does not. Named-instance validation requires
        // the same canonical spelling on both sides of the process boundary.
        var parent = url.standardizedFileURL
        var missing: [String] = []
        while true {
            if let path = realpath(parent.path, nil) {
                defer { free(path) }
                var canonical = URL(fileURLWithPath: String(cString: path), isDirectory: true)
                for component in missing.reversed() { canonical.append(path: component) }
                return canonical
            }
            let next = parent.deletingLastPathComponent()
            guard next != parent else { break }
            missing.append(parent.lastPathComponent)
            parent = next
        }
        return url.standardizedFileURL
    }
}

public struct LocalServerConnection: Sendable {
    public let endpoint: ServerEndpoint
    public let token: String
}

public enum LocalServerState: Equatable, Sendable {
    case idle, starting, running, stopping, failed(String)
}

public enum LocalServerError: LocalizedError, Sendable {
    case invalidConfiguration
    case missingSource
    case missingPython(String)
    case existingInstance
    case alreadyOwned
    case stopping
    case portUnavailable(Int)
    case phonePortTaken(Int)
    case unsafeFile
    case launchFailed
    case exited(Int32, String)
    case timeout
    case wrongServer

    public var errorDescription: String? {
        switch self {
        case .invalidConfiguration: "本地服务配置无效：请使用独立实例名和 0–65535 范围内的端口。"
        case .missingSource: "找不到本地后端源码。请保留构建应用时的 Hyadex 源码目录，或在设置中连接已安装的服务器。"
        case .missingPython(let path): "本地后端尚未准备好。请在 \(path) 运行 uv sync --locked --python 3.13，然后重试。准备完成后可离线启动。"
        case .existingInstance: "这个本地实例已有其他服务或数据。应用不会接管它；请连接已有服务器，或选择新的本地实例名。"
        case .alreadyOwned: "另一个 Hyadex 应用正在管理这个本地实例。请使用那个窗口，或退出后重试。"
        case .stopping: "本地服务正在停止，请稍后再启动。"
        case .portUnavailable(let port): "本地端口 \(port) 已被占用，未停止任何服务。请使用其他端口后重试。"
        case .phonePortTaken(let port): "端口 \(port) 已被其他服务占用（可能是已安装的 Hyadex 服务），本地服务保持原样。手机可以直接连接那个服务。"
        case .unsafeFile: "本地服务配置不是当前用户的普通私有文件，无法安全启动。请检查所选实例的目录权限。"
        case .launchFailed: "无法启动本地 Python。请重新运行 uv sync --locked --python 3.13 准备后端环境。"
        case .exited(let code, let detail): "本地服务启动失败（退出码 \(code)）。\(detail)"
        case .timeout: "本地服务未在限定时间内就绪，已停止本次启动。请检查后端依赖后重试。"
        case .wrongServer: "本地端口返回了其他服务的身份或认证状态，已停止本次启动。"
        }
    }
}

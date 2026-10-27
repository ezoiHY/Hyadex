// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "HyadexMac",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "Hyadex", targets: ["HyadexApp"]),
    ],
    targets: [
        // Network layer: REST client and wire types. No UI.
        .target(name: "HyadexAPI"),
        // Terminal emulator and its PTY socket: self-contained, no UI framework
        // beyond AppKit for the drawing view.
        .target(name: "HyadexTerminal"),
        // App state and domain logic (@Observable stores, timeline items, sample data).
        .target(name: "HyadexCore", dependencies: ["HyadexAPI", "HyadexTerminal"]),
        // Message rendering: Markdown, TeX math, code highlighting, images.
        // Self-contained: its own parsers, CoreText and the system math font.
        .target(name: "HyadexRichText"),
        // SwiftUI views and design tokens.
        .target(name: "HyadexUI", dependencies: ["HyadexCore", "HyadexRichText", "HyadexTerminal"]),
        // Thin entry point: scenes, menu commands, settings.
        .executableTarget(name: "HyadexApp", dependencies: ["HyadexCore", "HyadexUI"]),
        .testTarget(name: "HyadexRichTextTests", dependencies: ["HyadexRichText"]),
        .testTarget(name: "HyadexTerminalTests", dependencies: ["HyadexTerminal"]),
        .testTarget(name: "HyadexCoreTests", dependencies: ["HyadexCore", "HyadexAPI"], resources: [.copy("Fixtures")]),
    ]
)

import AppKit
import HyadexCore
import SwiftUI

/// The provider's mark, drawn as vectors so it stays sharp at any size:
/// Claude's terracotta spark and Codex's cloud with a terminal prompt.
struct ProviderMark: View {
    let backend: AgentBackend
    var size: CGFloat = 16

    var body: some View {
        Group {
            switch backend {
            case .claude:
                ClaudeSpark().fill(Theme.claudeOrange)
            case .codex:
                ZStack {
                    CodexCloud().fill(LinearGradient(
                        colors: [Color(red: 0.74, green: 0.66, blue: 1.0), Color(red: 0.45, green: 0.52, blue: 1.0),
                                 Color(red: 0.27, green: 0.33, blue: 0.98)],
                        startPoint: .top, endPoint: .bottom))
                    CodexPrompt().stroke(.white, style: StrokeStyle(lineWidth: size * 0.075, lineCap: .round, lineJoin: .round))
                }
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    /// A bitmap of the mark for places that only take images (menu labels).
    @MainActor
    static func image(_ backend: AgentBackend, size: CGFloat = 16) -> NSImage {
        let key = "\(backend.rawValue)-\(size)"
        if let cached = cache[key] { return cached }
        let renderer = ImageRenderer(content: ProviderMark(backend: backend, size: size))
        renderer.scale = 3
        let image = renderer.nsImage ?? NSImage()
        image.size = NSSize(width: size, height: size)
        cache[key] = image
        return image
    }

    @MainActor private static var cache: [String: NSImage] = [:]
}

/// Twelve tapered rays of uneven length around a small hub.
struct ClaudeSpark: Shape {
    /// (angle in degrees, length as a fraction of the radius)
    static let rays: [(Double, Double)] = [
        (6, 0.98), (36, 0.78), (63, 0.96), (90, 0.84), (116, 1.0), (146, 0.8),
        (176, 0.97), (204, 0.76), (232, 0.94), (262, 0.88), (292, 0.98), (322, 0.8),
    ]

    func path(in rect: CGRect) -> Path {
        let radius = min(rect.width, rect.height) / 2
        let center = CGPoint(x: rect.midX, y: rect.midY)
        var path = Path()
        path.addEllipse(in: CGRect(x: center.x - radius * 0.2, y: center.y - radius * 0.2,
                                   width: radius * 0.4, height: radius * 0.4))
        for (degrees, length) in Self.rays {
            let angle = degrees * .pi / 180
            let direction = CGVector(dx: cos(angle), dy: -sin(angle))
            let normal = CGVector(dx: -direction.dy, dy: direction.dx)
            let base = radius * 0.11, tip = radius * 0.065
            let reach = radius * length - tip
            func point(_ along: CGFloat, _ across: CGFloat) -> CGPoint {
                CGPoint(x: center.x + direction.dx * along + normal.dx * across,
                        y: center.y + direction.dy * along + normal.dy * across)
            }
            var ray = Path()
            ray.move(to: point(radius * 0.05, base))
            ray.addLine(to: point(reach, tip))
            ray.addArc(center: point(reach, 0), radius: tip,
                       startAngle: .radians(atan2(normal.dy, normal.dx)),
                       endAngle: .radians(atan2(-normal.dy, -normal.dx)), clockwise: true)
            ray.addLine(to: point(radius * 0.05, -base))
            ray.closeSubpath()
            path.addPath(ray)
        }
        return path
    }
}

/// A scalloped cloud: eight overlapping lobes around a core.
struct CodexCloud: Shape {
    func path(in rect: CGRect) -> Path {
        let side = min(rect.width, rect.height)
        let center = CGPoint(x: rect.midX, y: rect.midY)
        var path = Path()
        let core = side * 0.36
        path.addEllipse(in: CGRect(x: center.x - core, y: center.y - core, width: core * 2, height: core * 2))
        let ring = side * 0.27, lobe = side * 0.2
        for index in 0..<8 {
            let angle = Double(index) * .pi / 4 + .pi / 8
            let lobeCenter = CGPoint(x: center.x + cos(angle) * ring, y: center.y + sin(angle) * ring)
            path.addEllipse(in: CGRect(x: lobeCenter.x - lobe, y: lobeCenter.y - lobe, width: lobe * 2, height: lobe * 2))
        }
        return path
    }
}

/// ">_" in the cloud.
struct CodexPrompt: Shape {
    func path(in rect: CGRect) -> Path {
        let side = min(rect.width, rect.height)
        let origin = CGPoint(x: rect.midX - side / 2, y: rect.midY - side / 2)
        func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: origin.x + x * side, y: origin.y + y * side) }
        var path = Path()
        path.move(to: point(0.32, 0.37))
        path.addLine(to: point(0.43, 0.48))
        path.addLine(to: point(0.32, 0.59))
        path.move(to: point(0.52, 0.6))
        path.addLine(to: point(0.68, 0.6))
        return path
    }
}

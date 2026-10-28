// Renders the Hyadex app icon into an .iconset: the V of the Hyades star
// cluster, its stars in different colours (agents, each with its own gift)
// linked along the two arms and meeting in one star at the vertex. The
// orange star is Aldebaran, beside the Hyades.
// Usage: swift scripts/make-icon.swift Resources/AppIcon.iconset [--png icon.png]
import AppKit

let arguments = Array(CommandLine.arguments.dropFirst())
let output = URL(fileURLWithPath: arguments.first ?? "AppIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func rgb(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> NSColor { NSColor(srgbRed: r, green: g, blue: b, alpha: 1) }

func dot(_ c: NSPoint, _ r: CGFloat, _ color: NSColor) {
    color.setFill()
    NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2)).fill()
}

/// A glow that fades out smoothly, without a visible edge.
func softGlow(_ c: NSPoint, _ r: CGFloat, _ color: NSColor, _ alpha: CGFloat) {
    let stops: [(CGFloat, CGFloat)] = [(0, 1), (0.15, 0.62), (0.32, 0.3), (0.5, 0.12), (0.7, 0.04), (1, 0)]
    let gradient = NSGradient(colors: stops.map { color.withAlphaComponent(alpha * $0.1) },
                              atLocations: stops.map(\.0), colorSpace: .sRGB)!
    gradient.draw(in: NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: r * 2, height: r * 2)),
                  relativeCenterPosition: .zero)
}

/// A four-point star with curved, tapering rays.
func sparkle(_ c: NSPoint, long: CGFloat, waist: CGFloat) -> NSBezierPath {
    let path = NSBezierPath()
    for i in 0..<4 {
        let a = CGFloat(i) * .pi / 2, next = CGFloat(i + 1) * .pi / 2
        if i == 0 { path.move(to: NSPoint(x: c.x + cos(a) * long, y: c.y + sin(a) * long)) }
        let control = NSPoint(x: c.x + cos(a + .pi / 4) * waist, y: c.y + sin(a + .pi / 4) * waist)
        path.curve(to: NSPoint(x: c.x + cos(next) * long, y: c.y + sin(next) * long),
                   controlPoint1: control, controlPoint2: control)
    }
    path.close()
    return path
}

/// A deterministic sequence, so every render is identical.
struct Seeded {
    var state: UInt64
    mutating func next() -> CGFloat {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return CGFloat(state >> 33) / CGFloat(UInt64(1) << 31)
    }
}

func render(size: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let context = NSGraphicsContext.current!.cgContext
    context.clear(CGRect(x: 0, y: 0, width: size, height: size))
    context.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)

    // macOS icon grid: 824×824 body centred in 1024.
    let body = NSRect(x: 100, y: 100, width: 824, height: 824)
    let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: NSColor.black.withAlphaComponent(0.32).cgColor)
    NSColor.black.setFill()
    shape.fill()
    context.restoreGState()
    shape.addClip()
    NSGradient(colors: [rgb(0.20, 0.22, 0.46), rgb(0.035, 0.04, 0.11)])!.draw(in: body, angle: -90)
    softGlow(NSPoint(x: 520, y: 360), 560, rgb(0.45, 0.42, 1.0), 0.35)
    // A faint glass highlight across the top.
    NSGradient(colors: [NSColor.white.withAlphaComponent(0.11), NSColor.white.withAlphaComponent(0)])!
        .draw(in: NSRect(x: 100, y: 640, width: 824, height: 284), angle: -90)
    var random = Seeded(state: 7)
    for _ in 0..<26 {
        let x = 140 + random.next() * 744, y = 140 + random.next() * 744
        dot(NSPoint(x: x, y: y), 1 + random.next() * 1.6, NSColor.white.withAlphaComponent(0.18 + random.next() * 0.3))
    }

    // The V: the vertex, three stars up the left arm, two up the right, and Aldebaran.
    let stars: [NSPoint] = [(512, 330), (452, 450), (400, 560), (342, 680), (575, 455), (632, 575), (700, 700)]
        .map { NSPoint(x: $0.0, y: $0.1) }
    let radii: [CGFloat] = [24, 15, 16, 19, 15, 17, 31]
    let colors: [NSColor] = [
        .white, .white,
        rgb(0.45, 0.90, 0.82),   // teal
        rgb(0.55, 0.65, 1.0),    // blue
        .white,
        rgb(0.78, 0.66, 1.0),    // violet
        rgb(1.0, 0.60, 0.34),    // Aldebaran
    ]
    for arm in [[3, 2, 1, 0], [0, 4, 5, 6]] {
        for k in 0..<(arm.count - 1) {
            let line = NSBezierPath()
            line.move(to: stars[arm[k]])
            line.line(to: stars[arm[k + 1]])
            line.lineWidth = 6.5
            line.lineCapStyle = .round
            NSColor.white.withAlphaComponent(0.32).setStroke()
            line.stroke()
        }
    }
    for i in 1..<stars.count {
        softGlow(stars[i], radii[i] * 4.2, colors[i], 0.7)
        dot(stars[i], radii[i], colors[i])
        dot(stars[i], radii[i] * 0.45, NSColor.white.withAlphaComponent(0.95))
    }
    // Where the arms meet.
    softGlow(stars[0], 110, .white, 0.55)
    NSColor.white.setFill()
    sparkle(stars[0], long: 60, waist: 11).fill()
    dot(stars[0], 13, .white)

    // A thin edge highlight.
    let edge = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 184, yRadius: 184)
    edge.lineWidth = 3
    NSColor.white.withAlphaComponent(0.14).setStroke()
    edge.stroke()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try render(size: base).write(to: output.appending(path: "icon_\(base)x\(base).png"))
    try render(size: base * 2).write(to: output.appending(path: "icon_\(base)x\(base)@2x.png"))
}
// --png FILE also writes a 512 px copy (the README logo).
if let flag = arguments.firstIndex(of: "--png"), arguments.indices.contains(flag + 1) {
    try render(size: 512).write(to: URL(fileURLWithPath: arguments[flag + 1]))
}
print("wrote \(output.path)")

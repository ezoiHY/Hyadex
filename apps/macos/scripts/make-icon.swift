// Renders the Hyadex app icon (the V of the Hyades star cluster) into an .iconset.
// Usage: swift scripts/make-icon.swift Resources/AppIcon.iconset
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "AppIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func render(size: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let scale = CGFloat(size) / 1024
    let context = NSGraphicsContext.current!.cgContext
    context.scaleBy(x: scale, y: scale)

    // macOS icon grid: 824×824 body centred in 1024, continuous corners.
    let body = NSRect(x: 100, y: 100, width: 824, height: 824)
    let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: NSColor.black.withAlphaComponent(0.35).cgColor)
    NSColor.black.setFill()
    shape.fill()
    context.restoreGState()
    shape.addClip()
    NSGradient(colors: [
        NSColor(red: 0.16, green: 0.18, blue: 0.42, alpha: 1),
        NSColor(red: 0.05, green: 0.06, blue: 0.16, alpha: 1),
    ])!.draw(in: body, angle: -70)

    // Faint background stars.
    let background: [(CGFloat, CGFloat, CGFloat)] = [
        (210, 760, 3), (300, 300, 2.5), (820, 820, 3), (760, 230, 2.5), (180, 470, 2), (620, 860, 2),
        (870, 520, 2.5), (420, 190, 2), (560, 300, 2), (250, 610, 2), (700, 700, 2),
    ]
    for (x, y, r) in background {
        NSColor.white.withAlphaComponent(0.35).setFill()
        NSBezierPath(ovalIn: NSRect(x: x - r, y: y - r, width: r * 2, height: r * 2)).fill()
    }

    // The V asterism, with Aldebaran (orange) at the tip of one arm.
    let stars: [(CGFloat, CGFloat, CGFloat, NSColor)] = [
        (512, 330, 30, .white),
        (452, 450, 22, .white), (400, 560, 22, .white), (342, 680, 26, .white),
        (575, 455, 22, .white), (632, 575, 24, .white),
        (700, 700, 38, NSColor(red: 1.0, green: 0.62, blue: 0.36, alpha: 1)),
    ]
    let line = NSBezierPath()
    line.move(to: NSPoint(x: 342, y: 680)); line.line(to: NSPoint(x: 512, y: 330)); line.line(to: NSPoint(x: 700, y: 700))
    line.lineWidth = 6
    NSColor.white.withAlphaComponent(0.18).setStroke()
    line.stroke()
    for (x, y, r, color) in stars {
        let glow = NSGradient(colors: [color.withAlphaComponent(0.55), color.withAlphaComponent(0)])!
        glow.draw(in: NSBezierPath(ovalIn: NSRect(x: x - r * 2.6, y: y - r * 2.6, width: r * 5.2, height: r * 5.2)),
                  relativeCenterPosition: .zero)
        color.setFill()
        NSBezierPath(ovalIn: NSRect(x: x - r / 2, y: y - r / 2, width: r, height: r)).fill()
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try render(size: base).write(to: output.appending(path: "icon_\(base)x\(base).png"))
    try render(size: base * 2).write(to: output.appending(path: "icon_\(base)x\(base)@2x.png"))
}
print("wrote \(output.path)")

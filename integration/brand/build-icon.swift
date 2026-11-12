// Rebuild Handy.icns with: swift integration/brand/build-icon.swift
import AppKit
import Foundation
let base = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let output = FileManager.default.temporaryDirectory.appendingPathComponent("Handy-\(UUID().uuidString).iconset")
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: output) }
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let context = NSGraphicsContext.current!.cgContext
        context.scaleBy(x: CGFloat(pixels)/512, y: CGFloat(pixels)/512)
        context.translateBy(x: 0, y: 512); context.scaleBy(x: 1, y: -1)
        NSColor(srgbRed: 22/255, green: 63/255, blue: 59/255, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: 512, height: 512), xRadius: 112, yRadius: 112).fill()
        let h = NSBezierPath()
        h.move(to: NSPoint(x: 156, y: 116)); h.line(to: NSPoint(x: 218, y: 116)); h.line(to: NSPoint(x: 218, y: 233))
        h.curve(to: NSPoint(x: 276, y: 208), controlPoint1: NSPoint(x: 234, y: 216), controlPoint2: NSPoint(x: 253, y: 208))
        h.curve(to: NSPoint(x: 348, y: 284), controlPoint1: NSPoint(x: 324, y: 208), controlPoint2: NSPoint(x: 348, y: 233))
        h.line(to: NSPoint(x: 348, y: 396)); h.line(to: NSPoint(x: 286, y: 396)); h.line(to: NSPoint(x: 286, y: 291))
        h.curve(to: NSPoint(x: 255, y: 257), controlPoint1: NSPoint(x: 286, y: 268), controlPoint2: NSPoint(x: 276, y: 257))
        h.curve(to: NSPoint(x: 218, y: 298), controlPoint1: NSPoint(x: 230, y: 257), controlPoint2: NSPoint(x: 218, y: 271))
        h.line(to: NSPoint(x: 218, y: 396)); h.line(to: NSPoint(x: 156, y: 396)); h.close()
        NSColor(srgbRed: 248/255, green: 250/255, blue: 249/255, alpha: 1).setFill(); h.fill()
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let name = "icon_\(size)x\(size)" + (scale == 2 ? "@2x" : "") + ".png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name))
    }
}
let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = ["-c", "icns", output.path, "-o", base.appendingPathComponent("Handy.icns").path]
try process.run(); process.waitUntilExit(); exit(process.terminationStatus)

// XCUITest evidence helpers for the NerveCenter simulator verify path
// (T003-06 / AC7). Used by the NervePhoneUITests and NerveWatchUITests harnesses
// to "navigate with a virtual cursor" (real touch events), capture screenshots,
// and dump the accessibility tree of every audited screen.
//
// Artifacts are written to a directory derived from the `NERVE_E2E_DIR`
// environment variable (set on the test run), so the evidence is reproducible
// from a plain `xcodebuild test`.

import Foundation
import XCTest

public enum NerveE2E {
    /// The directory screenshots + accessibility dumps are written to.
    public static var artifactDir: URL {
        let override = ProcessInfo.processInfo.environment["NERVE_E2E_DIR"]
        let base = override ?? (NSTemporaryDirectory() + "/nerve-e2e")
        let url = URL(fileURLWithPath: base, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// Capture the app's current screen to `prefix.png` under the artifact dir.
    @discardableResult
    public static func screenshot(_ app: XCUIApplication, prefix: String) -> URL {
        let url = artifactDir.appendingPathComponent("\(prefix).png")
        let img = app.screenshot()
        let data = img.cgImageRepresentation() ?? Data()
        try? data.write(to: url)
        return url
    }

    /// Recursively collect the app's accessibility tree as label/identifier/value
    /// records (skipping empty structural nodes so the dump stays readable).
    private static func walk(_ element: XCUIElement, depth: Int, _ out: inout [String]) {
        if depth > 24 { return }
        let isAx = element.isAccessibilityElement
        let label = (element.label ?? "")
        let identifier = (element.identifier ?? "")
        let value = (element.value?.description ?? "")
        if isAx && !(label.isEmpty && identifier.isEmpty && value.isEmpty) {
            let esc = { (s: String) in s.replacingOccurrences(of: "\"", with: "\\\"").replacingOccurrences(of: "\n", with: " ") }
            out.append("  {\"label\":\"\(esc(label))\",\"identifier\":\"\(esc(identifier))\",\"value\":\"\(esc(value))\"}")
        }
        for child in element.allChildren {
            walk(child, depth: depth + 1, &out)
        }
    }

    /// Dump the app's accessibility tree as JSON under the artifact dir.
    @discardableResult
    public static func accessibilityDump(_ app: XCUIApplication, prefix: String) -> URL {
        let url = artifactDir.appendingPathComponent("\(prefix).ax.json")
        var rows: [String] = []
        for window in app.windows {
            walk(window, depth: 0, &rows)
        }
        let json = "[\n" + rows.joined(separator: ",\n") + "\n]"
        try? json.write(to: url, atomically: true, encoding: .utf8)
        return url
    }
}

// XCUITest harness for the NerveCenter iPhone app (T003-06 / AC7).
//
// Drives the six §56 tabs with real touch events (the "virtual cursor"),
// capturing a screenshot + accessibility-tree dump for each tab under
// $NERVE_E2E_DIR. Runs against a local backend when reachable; renders
// honest empty states otherwise — either way the evidence is produced.
//
// Usage:
//   NERVE_E2E_DIR=apple/e2e-artifacts xcodebuild test \
//     -project apple/Distribution/Generated/NervePhone.xcodeproj \
//     -scheme NervePhone -destination 'platform=iOS Simulator,name=iPhone 17 Pro' \
//     -only-testing:NervePhoneUITests/NervePhoneUITests

import XCTest

final class NervePhoneUITests: XCTestCase {
    var app: XCUIApplication!

    override func setUpWithError() throws {
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    private func capture(_ tab: String, index: Int) {
        let prefix = String(format: "phone-%02d-%@", index, tab.lowercased())
        NerveE2E.screenshot(app, prefix: prefix)
        NerveE2E.accessibilityDump(app, prefix: prefix)
    }

    private func selectTab(_ label: String) {
        var target: XCUIElement? = app.tabBar.buttons[label]
        if target == nil || !target!.exists {
            target = app.buttons[label]
        }
        if let el = target, el.exists {
            el.tap()
        }
        app.wait(for: .notBusy, timeout: 1.5)
    }

    /// Audit all six §56 tabs, capturing evidence for each.
    func testAuditSixTabs() {
        // Give the async bootstrap (credential load + cache snapshot) a moment.
        app.wait(for: .notBusy, timeout: 4)
        guard app.windows.firstElement?.exists ?? false else {
            XCTFail("App failed to present a window")
            return
        }

        let tabs: [(String, Int)] = [
            ("Overview", 1), ("Goals", 2), ("Incidents", 3),
            ("Agents", 4), ("Systems", 5), ("Ask", 6),
        ]
        for (label, idx) in tabs {
            selectTab(label)
            capture(label, index: idx)
        }
    }

    /// Exercise the Ask tab's input → submit path (phone Ask; spec §40/§55).
    func testAskSubmit() {
        selectTab("Ask")
        app.wait(for: .notBusy, timeout: 1.5)
        if let field = app.textFields.firstMatch, field.exists {
            field.typeText("What needs my attention right now?")
        }
        if let submit = app.buttons.matching(identifier: "ask.submit").firstMatch, submit.exists {
            submit.tap()
        }
        app.wait(for: .notBusy, timeout: 2.0)
        capture("ask-submitted", index: 7)
    }
}

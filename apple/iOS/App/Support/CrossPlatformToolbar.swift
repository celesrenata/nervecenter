import SwiftUI

// MARK: - Cross-platform toolbar/title helpers (macOS host build)
//
// The package cross-compiles the iPhone target for macOS (SPM `swift build`
// on the host), so iOS-only UIKit-bridge modifiers must be gated. These
// shims keep the call sites platform-neutral:
//
// - `navigationBarTitleDisplayMode` is a no-op off iOS (macOS navigation
//   bars have a single display style).
// - `.topBarTrailing` placement is iOS-only; `.trailing` (iOS 14+/macOS 11+)
//   is the portable equivalent and lays out identically on iOS.

extension View {
    /// Inline navigation-bar title. iOS-only; no-op on other platforms.
    @ViewBuilder
    public func inlineNavigationTitle() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.inline)
        #else
        self
        #endif
    }
}

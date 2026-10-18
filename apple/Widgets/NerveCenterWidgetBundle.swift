#if canImport(WidgetKit)
import WidgetKit
import SwiftUI

// MARK: - §62 WidgetKit glue
//
// Three size-specific `StaticConfiguration`s (one per §62 layout) wrapped in
// a single `@main` `WidgetBundle`. Each configuration declares its
// `supportedFamilies` to match the §62 sizes exactly:
//
// - small  → `systemSmall`  (health dot + HEALTHY + agents/incidents)
// - medium → `systemMedium` (top-3 compute rows + agents/incidents)
// - large  → `systemLarge`  (goal progress + compute + incidents + agents)

// MARK: - Small

public struct NerveCenterSmallWidget: Widget {
    public init() {}

    public var body: some WidgetConfiguration {
        StaticConfiguration(kind: "nerv.center.widgets.small", provider: NerveCenterWidgetProvider()) { entry in
            SmallWidgetView(state: entry.state)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Nerve Center — Health")
        .description("System health, agent and incident counts.")
        // watchOS widgets use accessory families; systemSmall/Medium/Large are
        // unavailable on watchOS.
        #if os(watchOS)
        .supportedFamilies([.accessoryCircular])
        #else
        .supportedFamilies([.systemSmall])
        #endif
    }
}

// MARK: - Medium

public struct NerveCenterMediumWidget: Widget {
    public init() {}

    public var body: some WidgetConfiguration {
        StaticConfiguration(kind: "nerv.center.widgets.medium", provider: NerveCenterWidgetProvider()) { entry in
            MediumWidgetView(state: entry.state)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Nerve Center — Compute")
        .description("Compute utilisation plus agent and incident counts.")
        // `systemMedium` is unavailable on watchOS — use accessoryRectangular.
        #if os(watchOS)
        .supportedFamilies([.accessoryRectangular])
        #else
        .supportedFamilies([.systemMedium])
        #endif
    }
}

// MARK: - Large

public struct NerveCenterLargeWidget: Widget {
    public init() {}

    public var body: some WidgetConfiguration {
        StaticConfiguration(kind: "nerv.center.widgets.large", provider: NerveCenterWidgetProvider()) { entry in
            LargeWidgetView(state: entry.state)
                .containerBackground(.clear, for: .widget)
        }
        .configurationDisplayName("Nerve Center — Overview")
        .description("Goal progress, compute state, incidents, and agent activity.")
        // `systemLarge` is unavailable on watchOS — use accessoryCorner.
        #if os(watchOS)
        .supportedFamilies([.accessoryCorner])
        #else
        .supportedFamilies([.systemLarge])
        #endif
    }
}

// MARK: - Bundle (iOS/visionOS/macOS only)

/// The widget bundle. `@main` so the extension (or, on the macOS host, the
/// library target) has a single entry point; the app-intents/display-name
/// metadata lives on each configuration (`configurationDisplayName`).
///
/// Parent-integration notes (do not ship without these):
/// - The app target (or extension Xcode project) must reference this target
///   and host the extension with the `group.nerv.center` App Group
///   entitlement. Note the phone currently uses
///   `group.com.celestium.nervecenter` — align the two before device use.
/// - `NSExtension` / widget-extension Info.plist and `App Group` entitlement
///   belong to the app bundle the parent owns, not this Swift package.
@main
public struct NerveCenterWidgetBundle: WidgetBundle {
    public init() {}

    public var body: some Widget {
        NerveCenterSmallWidget()
        NerveCenterMediumWidget()
        NerveCenterLargeWidget()
    }
}
#endif

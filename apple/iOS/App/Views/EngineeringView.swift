import SwiftUI

import NerveApps
import NerveDesign

// MARK: - Engineering tab (gaps4 D2')

/// The Engineering group (Overview, Goals, Incidents, Agents, Systems)
/// as a tab. The tabs are arranged as a sub-menu of the "Engineering"
/// tab in the main TabView, rather than direct tabs (to meet the prompt2
/// requirement of three top-level segments: Today, Engineering, Ask).
public struct EngineeringView: View {
    @ObservedObject public var store: PhoneDataStore
    @State private var selectedSubTab: PhoneTab = .overview

    public init(store: PhoneDataStore) {
        self.store = store
    }

    public var body: some View {
        TabView(selection: $selectedSubTab) {
            OverviewView(store: store)
                .tabItem {
                    Label("Overview", systemImage: PhoneTab.overview.symbolName)
                }
                .tag(PhoneTab.overview)
                .accessibilityIdentifier("phone.tab.engineering.overview")

            GoalsView(store: store)
                .tabItem {
                    Label("Goals", systemImage: PhoneTab.goals.symbolName)
                }
                .tag(PhoneTab.goals)
                .accessibilityIdentifier("phone.tab.engineering.goals")

            IncidentsView(store: store)
                .tabItem {
                    Label("Incidents", systemImage: PhoneTab.incidents.symbolName)
                }
                .tag(PhoneTab.incidents)
                .accessibilityIdentifier("phone.tab.engineering.incidents")

            AgentsView(store: store)
                .tabItem {
                    Label("Agents", systemImage: PhoneTab.agents.symbolName)
                }
                .tag(PhoneTab.agents)
                .accessibilityIdentifier("phone.tab.engineering.agents")

            SystemsView(store: store)
                .tabItem {
                    Label("Systems", systemImage: PhoneTab.systems.symbolName)
                }
                .tag(PhoneTab.systems)
                .accessibilityIdentifier("phone.tab.engineering.systems")
        }
        .tint(.blue)
        .accessibilityIdentifier("phone.tab.engineering.stack")
    }
}

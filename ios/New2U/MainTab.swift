import SwiftUI

/// What the main screen shows under the summary (WHO-145).
///
/// The raw value is what `@AppStorage("mainTab")` keeps, so the app
/// opens on the last tab. It is only the tab's name, no listening data.
/// A stored "artists" (the tab before it was removed) reads as Tracks.
nonisolated enum MainTab: String, CaseIterable, Identifiable {
    case tracks, albums, singles

    var id: Self { self }

    var title: String {
        switch self {
        case .tracks: return String(localized: "Tracks")
        case .albums: return String(localized: "Albums")
        case .singles: return String(localized: "Singles")
        }
    }

    /// The SF Symbol over the title in the bar.
    var systemImage: String {
        switch self {
        case .tracks: return "music.note"
        case .albums: return "square.stack"
        case .singles: return "record.circle"
        }
    }
}

/// The Tracks | Albums | Singles switch: a floating Liquid Glass
/// capsule at the bottom of the main screen, above the home indicator,
/// like the tab bar of iOS 26 (WHO-145).
///
/// `TimelineView` puts one under each tab's page with
/// `.mainTabBar($tab, page:)`, so each list scrolls under the glass with
/// its own fade, and its last row stops above it.
struct MainTabBar: View {
    @Binding var tab: MainTab
    /// The page this switch is under. Only that page's switch ticks when
    /// it opens, so a change ticks once, not once per page.
    let page: MainTab

    /// The one selection pill, which slides to the tab tapped.
    @Namespace private var selection

    var body: some View {
        HStack(spacing: 0) {
            ForEach(MainTab.allCases) { item in
                button(item)
            }
        }
        // On the row, so the pill slides across the tabs.
        .animation(.snappy, value: tab)
        .padding(Theme.Spacing.xs)
        // Not interactive: a tap moves the pill and nothing else, so the
        // glass behind the tabs never dims or flashes, as in the system
        // tab bar.
        .liquidGlass(in: Capsule(), interactive: false)
        // A soft tick on every switch, as the system tab bar does.
        .sensoryFeedback(.selection, trigger: tab) { _, opened in opened == page }
        // VoiceOver: "Show, tab bar", then "Tracks, selected, button".
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Show")
        .accessibilityAddTraits(.isTabBar)
        .padding(.horizontal, Theme.Spacing.xl)
        .padding(.bottom, Theme.Spacing.xs)
    }

    /// One tab: its symbol over its title. The open one sits on the
    /// pill and takes the accent colour. There is one pill, moved from
    /// tab to tab, never one fading out while another fades in.
    private func button(_ item: MainTab) -> some View {
        let isOpen = item == tab
        return Button {
            tab = item
        } label: {
            VStack(spacing: 2) {
                Image(systemName: item.systemImage)
                    .font(.system(size: 18, weight: .medium))
                    .frame(height: 22)
                Text(item.title)
                    .font(.caption2.weight(.semibold))
                    .lineLimit(1)
            }
            .foregroundStyle(isOpen ? Color.accentColor : Color.primary)
            .frame(maxWidth: .infinity, minHeight: 48)
            .background {
                if isOpen {
                    Capsule()
                        .fill(.fill.tertiary)
                        .matchedGeometryEffect(id: "pill", in: selection)
                }
            }
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        // The bar stays one line high, as the system tab bar does. At
        // the accessibility text sizes a touch and hold shows the tab
        // name large in the middle of the screen instead.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
        .accessibilityShowsLargeContentViewer {
            Label(item.title, systemImage: item.systemImage)
        }
        .accessibilityLabel(item.title)
        .accessibilityAddTraits(isOpen ? .isSelected : [])
    }
}

extension View {
    /// Pins `MainTabBar` to the bottom edge. From iOS 26 on it is a
    /// `safeAreaBar`, which also fades the rows that scroll under the
    /// glass, as under the system tab bar, so the tab names stay easy to
    /// read. Before iOS 26 it is a plain bottom inset.
    func mainTabBar(_ tab: Binding<MainTab>, page: MainTab) -> some View {
        Group {
            if #available(iOS 26, *) {
                safeAreaBar(edge: .bottom, spacing: 0) { MainTabBar(tab: tab, page: page) }
            } else {
                safeAreaInset(edge: .bottom, spacing: 0) { MainTabBar(tab: tab, page: page) }
            }
        }
    }
}

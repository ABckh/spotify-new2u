import SwiftUI

/// Settings (story S13): which time zone the app shows times in, the
/// state of the Spotify connection, and "Sync now" (WHO-118).
///
/// It has no view model of its own. The zone comes from the timeline's
/// last answer (`period.time_zone`), and the Spotify part reads the same
/// `ConnectionViewModel` as the banner, so the two never disagree.
/// "Sync now" uses the timeline's `RefreshViewModel` and its reload.
///
/// The zone is read-only. Contract section 4: v1 has no endpoint to
/// change it, and the "Use current device zone" button (WHO-13 R-10)
/// waits on a CTO decision.
struct SettingsView: View {
    /// The stored display zone, or nil while no answer has echoed it.
    let displayZone: TimeZone?
    let connection: ConnectionViewModel
    /// "Last updated" and the cooldown for "Sync now".
    let refresh: RefreshViewModel
    /// Runs "Sync now". The timeline screen owns it, so the plays reload
    /// after the sync even if this screen has been closed by then.
    let sync: () -> Void
    /// The Spotify history file import (WHO-94): the row above the
    /// privacy entries.
    let historyImport: HistoryImportViewModel
    /// Leaves Settings for the timeline ("Open timeline" after an import).
    let openTimeline: () -> Void
    /// The device's zone. A parameter only so previews can fix it.
    var deviceZone: TimeZone = .current

    var body: some View {
        List {
            timeZoneSection
            spotifySection
            historyImportSection
        }
        .navigationTitle("Settings")
        .navigationBarTitleDisplayMode(.inline)
        // Checks again on every visit. Pushing this screen cancels the
        // timeline's own check, so without this a check that was still
        // running would never finish.
        .task { await loadAll() }
        .refreshable { await loadAll() }
        // Each connection answer carries the poll times for "Last updated".
        .onChange(of: connection.status) {
            if let answer = connection.connection { refresh.show(answer) }
        }
    }

    private func loadAll() async {
        async let connectionLoaded: Void = connection.load()
        async let importLoaded: Void = historyImport.load()
        _ = await (connectionLoaded, importLoaded)
    }

    // MARK: - Time zone

    private var timeZoneSection: some View {
        Section {
            if let displayZone {
                SettingsRow(
                    label: "Times are shown in",
                    value: DisplayFormat.zoneName(displayZone),
                    detail: displayZone.identifier)
                if DisplayFormat.isDifferentZone(displayZone, deviceZone) {
                    SettingsRow(
                        label: "This iPhone is in",
                        value: DisplayFormat.zoneName(deviceZone),
                        detail: deviceZone.identifier)
                }
            } else {
                SettingsRow(label: "Times are shown in", value: Messages.zoneNotKnownYet, detail: nil)
            }
        } header: {
            Text("Time zone")
        } footer: {
            Text(Messages.zoneFooter)
                .foregroundStyle(Theme.secondaryText)
        }
    }

    // MARK: - Spotify

    private var spotifySection: some View {
        Section {
            switch connection.status {
            case .loading:
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView()
                    Text("Checking Spotify…")
                        .foregroundStyle(Theme.secondaryText)
                }
                .frame(minHeight: 44)
            case .loaded(let account):
                SettingsRow(
                    label: "Account",
                    value: account.spotifyDisplayName ?? "Spotify account",
                    detail: nil)
                SettingsRow(
                    label: "Status",
                    value: account.isActive ? "Collecting plays" : "Reconnect needed",
                    detail: nil)
                // Only while connected: a revoked account can't sync, and
                // the "Reconnect needed" row says what to do instead.
                if account.isActive {
                    SpotifySyncRows(viewModel: refresh, sync: sync)
                }
            case .notConnected:
                SettingsRow(label: "Account", value: "Not connected", detail: nil)
            case .failed(let error):
                let text = Messages.failure(error)
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Label(text.title, systemImage: text.systemImage)
                        .font(.body)
                    Text(text.detail)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                }
                Button("Try again") { Task { await connection.load() } }
                    .frame(minHeight: 44)
            }
        } header: {
            Text("Spotify")
        }
    }

    // MARK: - Spotify history file (WHO-103 §3)

    /// Shown until one import succeeds. Hidden after `succeeded`, and
    /// also while the status is unknown (loading or the check failed).
    @ViewBuilder
    private var historyImportSection: some View {
        if let entry = historyImport.settingsEntry {
            Section {
                NavigationLink {
                    HistoryImportView(viewModel: historyImport, openTimeline: openTimeline)
                } label: {
                    VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                        Text(Messages.importRowLabel)
                        Text(Self.subtitle(entry))
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondaryText)
                    }
                    .frame(minHeight: 44)
                }
                NavigationLink(Messages.howToGetTheFile) { HistoryStepsView() }
                    .frame(minHeight: 44)
            }
        }
    }

    static func subtitle(_ entry: HistoryImportViewModel.SettingsEntry) -> String {
        switch entry {
        case .start: return Messages.importRowStart
        case .retry: return Messages.importRowRetry
        case .importing: return Messages.importRowImporting
        }
    }
}

/// One line in Settings: a label on the left, the value on the right,
/// and an optional small line under the value. At accessibility text
/// sizes the value moves under the label so neither gets cut off.
struct SettingsRow: View {
    let label: String
    let value: String
    let detail: String?

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text(label)
                    valueText(alignment: .leading)
                }
            } else {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
                    Text(label)
                    Spacer(minLength: Theme.Spacing.s)
                    valueText(alignment: .trailing)
                }
            }
        }
        .frame(minHeight: 44)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label), \(value)")
    }

    private func valueText(alignment: HorizontalAlignment) -> some View {
        VStack(alignment: alignment, spacing: Theme.Spacing.xs) {
            Text(value)
                .foregroundStyle(Theme.secondaryText)
                .multilineTextAlignment(alignment == .leading ? .leading : .trailing)
            if let detail {
                Text(detail)
                    .font(.footnote)
                    .foregroundStyle(Theme.secondaryText)
            }
        }
    }
}

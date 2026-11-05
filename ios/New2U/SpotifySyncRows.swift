import SwiftUI

/// The sync rows in Settings, under the Spotify account (WHO-118):
/// "Last updated 4 min ago" and the "Sync now" button. They used to be
/// a bar on the main screen (WHO-96); the main screen stays minimal now.
///
/// The button is disabled during the cooldown and shows the wait
/// ("Available in 3:12"), or "Checking Spotify…" while the server polls.
/// All state comes from `RefreshViewModel`.
struct SpotifySyncRows: View {
    let viewModel: RefreshViewModel
    /// Runs the sync. The timeline screen owns it, because it reloads the
    /// timeline, the summary and the connection afterwards.
    let sync: () -> Void

    var body: some View {
        // `SwiftUI.TimelineView` redraws these rows once a second, so the
        // countdown ticks and the button turns back on at zero. The
        // prefix is needed because the app's main screen is also called
        // `TimelineView`.
        SwiftUI.TimelineView(.periodic(from: .now, by: 1)) { context in
            SettingsRow(
                label: "Last updated",
                value: viewModel.lastPolledAt.map { DisplayFormat.lastChecked($0, now: context.date) }
                    ?? Messages.syncNotYet,
                detail: nil)
            button(at: context.date)
            if case .failed(let error) = viewModel.status {
                Text(Messages.refreshFailed(error))
                    .font(.footnote)
                    .foregroundStyle(Theme.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func button(at date: Date) -> some View {
        Button(action: sync) {
            if viewModel.status == .checking {
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView()
                    Text(Messages.refreshChecking)
                }
            } else if let seconds = viewModel.secondsToWait(at: date) {
                Text(Messages.refreshAvailable(inSeconds: seconds))
                    .monospacedDigit()
            } else {
                Text(Messages.syncButton)
            }
        }
        .frame(minHeight: 44)
        .disabled(!viewModel.canRefresh(at: date))
    }
}

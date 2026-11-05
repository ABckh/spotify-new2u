import AuthenticationServices
import SwiftUI

/// A short note under the period bar when the Spotify connection needs
/// the user: revoked, not connected, or ending soon. It says what
/// happened in one sentence and offers to reconnect (design bar).
///
/// Reconnecting is the same sign-in as the connect screen
/// (`SignInViewModel`), sent as a renewal with the current session
/// token. The server keeps the same account and its stored zone, and
/// hands back a new session token. If the user picks another Spotify
/// account, the banner says so and the app stays signed in.
struct ConnectionBanner: View {
    let notice: ConnectionViewModel.Notice
    /// The display zone, for the "reconnect by" date.
    let timeZone: TimeZone
    let signIn: SignInViewModel
    /// Called after a reconnect attempt, to check the connection again.
    let reconnected: () async -> Void

    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    var body: some View {
        let text = shownText
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            Image(systemName: text.systemImage)
                .foregroundStyle(.primary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(text.title)
                    .font(.subheadline.weight(.semibold))
                Text(text.detail)
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
                reconnectButton
                    .padding(.top, Theme.Spacing.xs)
            }
            Spacer(minLength: 0)
        }
        // Pinned above the list like the period bar, so it stops growing
        // at the same size.
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.vertical, Theme.Spacing.m)
        .background(Color(.secondarySystemBackground))
        .overlay(alignment: .bottom) { Divider() }
        .accessibilityElement(children: .contain)
    }

    private var reconnectButton: some View {
        Button {
            Task {
                await signIn.connect(renewing: true, openBrowser: openInBrowser)
                await reconnected()
            }
        } label: {
            if signIn.status == .working {
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView()
                    Text("Waiting for Spotify…")
                }
            } else {
                Text(notice == .notConnected ? "Connect Spotify" : "Reconnect Spotify")
            }
        }
        .buttonStyle(.bordered)
        .frame(minHeight: 44)
        .disabled(signIn.status == .working)
    }

    /// The notice, or the sign-in's own failure after a failed reconnect.
    private var shownText: Messages.Text {
        if case .failed(let failure) = signIn.status { return failure }
        var byDate: String?
        if case .reconnectSoon(let date?) = notice {
            byDate = DisplayFormat.dayAndMonth(date, timeZone: timeZone)
        }
        return Messages.connection(notice, byDate: byDate)
    }

    /// The same system sign-in sheet as `SignInView`.
    private func openInBrowser(_ url: URL) async throws -> URL {
        try await webAuthenticationSession.authenticate(
            using: url,
            callbackURLScheme: SignInCallback.scheme,
            preferredBrowserSession: .ephemeral)
    }
}

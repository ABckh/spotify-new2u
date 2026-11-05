import AuthenticationServices
import SwiftUI

/// The "Connect Spotify" screen, shown while there is no session.
///
/// All the work is in `SignInViewModel`. This file lays out the screen
/// and gives the view model a way to open the Spotify page.
struct SignInView: View {
    @State private var viewModel: SignInViewModel
    /// True when the last session ended because the server said so.
    let sessionExpired: Bool
    /// The "Import my full Spotify history" switch (WHO-103 C2). Off by
    /// default and never remembered. It is not sent to the server.
    @State private var importFullHistory = false
    /// Set from C2 when "Connect Spotify" is tapped. `RootView` keeps it in
    /// memory only, and the signed-in screen opens screen H once.
    @Binding var showsHistoryStepsAfterConnect: Bool

    @Environment(\.webAuthenticationSession) private var webAuthenticationSession

    init(api: APIClient, sessionStore: SessionStore, showsHistoryStepsAfterConnect: Binding<Bool>) {
        _viewModel = State(initialValue: SignInViewModel(api: api, sessionStore: sessionStore))
        sessionExpired = sessionStore.sessionExpired
        _showsHistoryStepsAfterConnect = showsHistoryStepsAfterConnect
    }

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: systemImage)
        } description: {
            Text(detail)
        } actions: {
            // Only on a first connect. "Your session ended" is a returning
            // user, whose history is already on the server.
            if !sessionExpired {
                Toggle(isOn: $importFullHistory) {
                    VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                        Text(Messages.fullHistoryChoiceLabel)
                        Text(Messages.fullHistoryChoiceExplainer)
                            .font(.footnote)
                            .foregroundStyle(Theme.secondaryText)
                    }
                }
                .disabled(viewModel.status == .working)
                .padding(.bottom, Theme.Spacing.s)
            }
            // Plain text, not tappable. It comes before the button in the
            // stack, so VoiceOver reads it first.
            Text(viewModel.importNotice)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button {
                // Read only after a successful connect: until then this
                // screen stays up and the flag does nothing.
                showsHistoryStepsAfterConnect = !sessionExpired && importFullHistory
                Task { await viewModel.connect(openBrowser: openInBrowser) }
            } label: {
                if viewModel.status == .working {
                    HStack {
                        ProgressView()
                        Text("Waiting for Spotify…")
                    }
                } else if case .failed = viewModel.status {
                    Text("Try again")
                } else {
                    Text("Connect Spotify")
                }
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(viewModel.status == .working)
        }
        .onAppear {
            importFullHistory = false
        }
    }

    /// Opens the Spotify page in the system sign-in sheet and waits for
    /// the browser to come back on `new2u://`. "Ephemeral" means the
    /// sheet shares no cookies with Safari and keeps none afterwards.
    private func openInBrowser(_ url: URL) async throws -> URL {
        try await webAuthenticationSession.authenticate(
            using: url,
            callbackURLScheme: SignInCallback.scheme,
            preferredBrowserSession: .ephemeral)
    }

    // MARK: - Text for the current state

    private var title: String {
        if case .failed(let text) = viewModel.status { return text.title }
        return "Connect Spotify"
    }

    private var detail: String {
        if case .failed(let text) = viewModel.status { return text.detail }
        if sessionExpired { return "Your session ended. Connect Spotify again to continue." }
        return "Connect your Spotify account to see what you listened to, day by day."
    }

    private var systemImage: String {
        if case .failed(let text) = viewModel.status { return text.systemImage }
        return "music.note.list"
    }
}

#Preview {
    let store = SessionStore(service: "preview")
    SignInView(
        api: APIClient(baseURL: URL(string: "http://127.0.0.1:8000"), sessionStore: store), sessionStore: store,
        showsHistoryStepsAfterConnect: .constant(false))
}

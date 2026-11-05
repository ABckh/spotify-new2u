import SwiftUI

/// The app's entry point. It builds the shared objects once and hands
/// them to the first screen.
@main
struct New2UApp: App {
    private let sessionStore: SessionStore
    private let baseURL: URL?
    private let api: APIClient

    init() {
        // Hides the screen in the app switcher (WHO-30, WHO-165).
        PrivacyCoverWindow.shared.start()
        #if DEBUG
        if DebugFixtures.activeName != nil {
            // Synthetic data for simulator screenshots. A separate
            // Keychain entry, so the real session is never touched.
            let store = SessionStore(service: "app.new2u.fixtures")
            // `connect` shows the connect screen, so it starts signed out.
            // `connect-expired` shows it as "Your session ended".
            if DebugFixtures.activeName == "connect" {
                store.deleteToken()
            } else if DebugFixtures.activeName == "connect-expired" {
                store.endExpiredSession()
            } else {
                store.saveToken("synthetic-fixture-token")
            }
            let url = URL(string: "http://fixtures.invalid")
            sessionStore = store
            baseURL = url
            api = APIClient(baseURL: url, sessionStore: store, urlSession: DebugFixtures.makeURLSession())
            return
        }
        #endif
        sessionStore = SessionStore()
        baseURL = AppConfig.apiBaseURL()
        api = APIClient(baseURL: baseURL, sessionStore: sessionStore)
    }

    /// `.background` once the app has left the front.
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(baseURL: baseURL, sessionStore: sessionStore, api: api)
                // Covers are kept in memory only, and only while the app
                // is in use (WHO-148 rule 4).
                .onChange(of: scenePhase) { _, phase in
                    if phase == .background {
                        CoverImageLoader.shared.removeAll()
                    }
                }
        }
    }
}

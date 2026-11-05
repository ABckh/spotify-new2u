import SwiftUI

/// The first screen. It decides which screen to show:
/// no server address, connect Spotify, or the timeline.
///
/// It watches `sessionStore.hasToken`, so a 401 from any request (which
/// deletes the token) brings the user back to the connect screen.
struct RootView: View {
    let baseURL: URL?
    let sessionStore: SessionStore
    let api: APIClient
    /// The connect screen's C2 switch was on (WHO-103 §1). In memory only:
    /// never sent to the server and never saved.
    @State private var showsHistorySteps = false

    var body: some View {
        if baseURL == nil {
            ContentUnavailableView {
                Label("Server address not configured", systemImage: "network.slash")
            } description: {
                Text("This build doesn't know where the server is. Install a build with the server address set.")
            }
        } else if !sessionStore.hasToken {
            SignInView(api: api, sessionStore: sessionStore, showsHistoryStepsAfterConnect: $showsHistorySteps)
        } else {
            NavigationStack {
                SignedInView(api: api, sessionStore: sessionStore, showsHistorySteps: $showsHistorySteps)
            }
        }
    }
}

/// Owns the signed-in screens' view models. A new sign-in gets new ones,
/// so nothing from a previous session stays on screen.
struct SignedInView: View {
    let api: APIClient
    @State private var timeline: TimelineViewModel
    @State private var summary: SummaryViewModel
    @State private var albums: AlbumsViewModel
    @State private var singles: AlbumsViewModel
    @State private var newToYou: NewToYouViewModel
    @State private var review: ReviewViewModel
    @State private var ratings: RatingsViewModel
    @State private var search: SearchViewModel
    @State private var connection: ConnectionViewModel
    @State private var refresh: RefreshViewModel
    @State private var signIn: SignInViewModel
    @State private var historyImport: HistoryImportViewModel
    /// True once after a connect with C2 on. Cleared as soon as it is read.
    @Binding var showsHistorySteps: Bool
    @State private var presentsHistorySteps = false

    init(api: APIClient, sessionStore: SessionStore, showsHistorySteps: Binding<Bool>) {
        self.api = api
        _timeline = State(initialValue: TimelineViewModel(api: api))
        _summary = State(initialValue: SummaryViewModel(api: api))
        let albums = AlbumsViewModel(api: api)
        let singles = AlbumsViewModel(api: api, singles: true)
        let newToYou = NewToYouViewModel(api: api)
        let review = ReviewViewModel(api: api)
        let ratings = RatingsViewModel(api: api)
        // A rating changes the badges in every album list and the
        // all-time list.
        let reloadAfterRating: () async -> Void = {
            async let newToYouLoaded: Void = newToYou.load()
            async let albumsLoaded: Void = albums.load()
            async let singlesLoaded: Void = singles.load()
            async let ratingsLoaded: Void = ratings.load()
            _ = await (newToYouLoaded, albumsLoaded, singlesLoaded, ratingsLoaded)
        }
        newToYou.reloadAfterRating = reloadAfterRating
        albums.reloadAfterRating = reloadAfterRating
        singles.reloadAfterRating = reloadAfterRating
        let search = SearchViewModel(api: api)
        search.reloadAfterRating = reloadAfterRating
        // A "Not new" mark changes the "New" tags in "All albums", and
        // what is left to review.
        newToYou.reloadAfterMark = {
            async let albumsLoaded: Void = albums.load()
            async let singlesLoaded: Void = singles.load()
            async let reviewLoaded: Void = review.load()
            _ = await (albumsLoaded, singlesLoaded, reviewLoaded)
        }
        // A finished review marked items: every list leaves them out.
        review.finished = {
            async let newToYouLoaded: Void = newToYou.load()
            async let albumsLoaded: Void = albums.load()
            async let singlesLoaded: Void = singles.load()
            _ = await (newToYouLoaded, albumsLoaded, singlesLoaded)
        }
        _albums = State(initialValue: albums)
        _singles = State(initialValue: singles)
        _newToYou = State(initialValue: newToYou)
        _review = State(initialValue: review)
        _ratings = State(initialValue: ratings)
        _search = State(initialValue: search)
        _connection = State(initialValue: ConnectionViewModel(api: api))
        _refresh = State(initialValue: RefreshViewModel(api: api))
        _signIn = State(initialValue: SignInViewModel(api: api, sessionStore: sessionStore))
        _historyImport = State(initialValue: HistoryImportViewModel(api: api))
        _showsHistorySteps = showsHistorySteps
    }

    var body: some View {
        TimelineView(
            viewModel: timeline, summary: summary, albums: albums, singles: singles, newToYou: newToYou,
            review: review, ratings: ratings, search: search,
            connection: connection,
            refresh: refresh, signIn: signIn, historyImport: historyImport, api: api)
            .task { await openHistoryStepsIfChosen() }
            .sheet(isPresented: $presentsHistorySteps) {
                NavigationStack { HistoryStepsView() }
            }
    }

    /// After a connect with C2 on, opens screen H once, unless this
    /// account already imported its history (WHO-103 §1 rule 3). If the
    /// check fails, H still opens: it is only instructions.
    private func openHistoryStepsIfChosen() async {
        guard showsHistorySteps else { return }
        showsHistorySteps = false
        await historyImport.load()
        if historyImport.latest?.status != "succeeded" {
            presentsHistorySteps = true
        }
    }
}

#Preview("Not configured") {
    let store = SessionStore(service: "preview")
    RootView(baseURL: nil, sessionStore: store, api: APIClient(baseURL: nil, sessionStore: store))
}

#Preview("Not connected") {
    let store = SessionStore(service: "preview")
    let url = URL(string: "http://127.0.0.1:8000")
    RootView(baseURL: url, sessionStore: store, api: APIClient(baseURL: url, sessionStore: store))
}

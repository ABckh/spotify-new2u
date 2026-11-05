import SwiftUI

/// The main screen: the period picker on top, the Tracks | Albums |
/// Singles switch pinned to the bottom (WHO-145), and between them the
/// open tab's list:
///
/// - Tracks: the summary, then the first 10 plays under sticky day
///   headers, newest first, with "Show all" for the rest (WHO-131).
/// - Albums: the summary, the new albums, then all albums.
/// - Singles: the summary and the new singles and EPs.
///
/// All data comes from the view models. This file only lays it out and
/// holds which period and which tab are chosen.
struct TimelineView: View {
    let viewModel: TimelineViewModel
    let summary: SummaryViewModel
    /// Every album played in the period, on the Albums tab (WHO-134).
    let albums: AlbumsViewModel
    /// Every single and EP played in the period, on the Singles tab.
    let singles: AlbumsViewModel
    /// "New to you", on the Albums and Singles tabs (S14).
    let newToYou: NewToYouViewModel
    /// "Did you already know these?", offered above "New to you" in the
    /// first weeks after connecting.
    let review: ReviewViewModel
    /// "Your album ratings", offered on the Albums tab once one is rated.
    let ratings: RatingsViewModel
    /// Search, all time, pushed from the toolbar. Kept here so the last
    /// search is still there when it opens again.
    let search: SearchViewModel
    /// The Spotify connection check, for the banner under the period bar.
    let connection: ConnectionViewModel
    /// "Sync now" and its cooldown, shown in Settings (WHO-118).
    let refresh: RefreshViewModel
    /// Runs "Reconnect Spotify" from the banner.
    let signIn: SignInViewModel
    /// The Spotify history file import, for its row in Settings.
    let historyImport: HistoryImportViewModel
    /// Only for the custom-range sheet, which checks dates with the server.
    let api: APIClient

    /// The chosen period. Changing it reloads every tab and the summary.
    @State private var query = PeriodQuery(kind: .week)
    /// The open tab, kept between launches. First open: Tracks.
    @AppStorage("mainTab") private var tab: MainTab = .tracks
    /// Set while the custom-range sheet is open.
    @State private var customRange: CustomRangeViewModel?
    /// True while Settings is pushed on top of this screen.
    @State private var showsSettings = false
    /// True while Search is pushed on top of this screen.
    @State private var showsSearch = false
    /// The track whose comment editor is open, from a tapped play.
    @State private var trackComment: TrackCommentModel?
    /// True while "Did you already know these?" is open.
    @State private var showsReview = false
    #if DEBUG
    /// The `album…` fixtures push the first new album on launch.
    @State private var showsDebugAlbum = false
    /// `plays-many-all` pushes the plays' "Show all" on launch.
    @State private var showsDebugAllPlays = false
    #endif

    var body: some View {
        tabs
            .safeAreaInset(edge: .top, spacing: 0) {
                VStack(spacing: 0) {
                    PeriodBar(
                        query: $query,
                        period: viewModel.period ?? summary.summary?.period,
                        isLoading: isLoading,
                        editCustomRange: openCustomRange)
                    // Shown in every state, including empty and offline,
                    // so a revoked connection is never hidden.
                    if let notice = connection.notice {
                        ConnectionBanner(
                            notice: notice,
                            timeZone: viewModel.displayTimeZone,
                            signIn: signIn,
                            reconnected: reloadAfterReconnect)
                    }
                }
            }
            .navigationTitle("Listening")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showsSettings = true } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showsSearch = true } label: {
                        Image(systemName: "magnifyingglass")
                    }
                    .accessibilityLabel(Messages.searchTitle)
                }
                // A reload keeps the old rows and shows this small
                // indicator instead of clearing the screen.
                if isReloading {
                    ToolbarItem(placement: .topBarTrailing) {
                        ProgressView().accessibilityLabel("Loading")
                    }
                }
            }
            .navigationDestination(isPresented: $showsSearch) {
                SearchView(viewModel: search, timeZone: viewModel.displayTimeZone)
            }
            .navigationDestination(isPresented: $showsSettings) {
                SettingsView(
                    displayZone: displayZone, connection: connection, refresh: refresh,
                    sync: {
                        // An unstructured Task, so leaving Settings does not
                        // cancel the 20 s wait and the reload after it.
                        Task { await refreshFromSpotify() }
                    },
                    historyImport: historyImport, openTimeline: openTimelineAfterImport)
            }
            .sheet(item: $customRange) { model in
                CustomRangeSheet(viewModel: model) { accepted in query = accepted }
            }
            .sheet(isPresented: $showsReview) {
                ReviewView(viewModel: review, timeZone: viewModel.displayTimeZone)
            }
            .task(id: query) { await loadAll() }
            // Once per sign-in, not on every period change. A new user's
            // first poll lands seconds after this, so wait for it and
            // reload once (WHO-192).
            .task {
                async let ratingsLoaded: Void = ratings.load()
                async let reviewLoaded: Void = review.load()
                await connection.load()
                if await connection.waitForFirstPoll() {
                    // The first poll also reads the library baseline.
                    async let reloaded: Void = review.load()
                    await loadAll()
                    await reloaded
                }
                await reviewLoaded
                await ratingsLoaded
            }
            // Each connection answer carries the import marker for the
            // timeline and the poll times for "Sync now" in Settings.
            .onChange(of: connection.status, initial: true) {
                viewModel.connection = connection.connection
                if let answer = connection.connection { refresh.show(answer) }
            }
            #if DEBUG
            .navigationDestination(isPresented: $showsDebugAlbum) {
                if let album = newToYou.answer?.albums.items.first {
                    AlbumTracksView(album: album, source: newToYou)
                }
            }
            .navigationDestination(isPresented: $showsDebugAllPlays) {
                TimelineAllView(source: viewModel)
            }
            .onAppear {
                if let launchTab = DebugFixtures.launchTab {
                    tab = launchTab
                }
                if DebugFixtures.opensReviewOnLaunch {
                    Task {
                        try? await Task.sleep(for: .seconds(1))
                        showsReview = true
                    }
                }
                if DebugFixtures.opensAllPlaysOnLaunch {
                    Task {
                        try? await Task.sleep(for: .seconds(1))
                        showsDebugAllPlays = true
                    }
                }
                if DebugFixtures.checksCustomRangeOnOpen {
                    openCustomRange()
                    customRange.map(DebugFixtures.reverse)
                }
                if DebugFixtures.opensAlbumOnLaunch {
                    // After "New to you" has answered, so there is an album.
                    Task {
                        try? await Task.sleep(for: .seconds(1.5))
                        showsDebugAlbum = true
                    }
                }
                if DebugFixtures.marksNotNewOnLaunch {
                    // After "New to you" has answered, so there is an album.
                    Task {
                        try? await Task.sleep(for: .seconds(1.5))
                        if let album = newToYou.answer?.albums.items.first {
                            await newToYou.marker.mark(album.notNewMark)
                        }
                    }
                }
                if DebugFixtures.opensSettingsOnLaunch {
                    // After the first answer, so Settings can show its zone.
                    Task {
                        try? await Task.sleep(for: .seconds(1))
                        showsSettings = true
                    }
                }
                if DebugFixtures.opensSearchOnLaunch {
                    search.text = DebugFixtures.searchWordsOnLaunch
                    Task {
                        try? await Task.sleep(for: .seconds(1))
                        showsSearch = true
                    }
                }
            }
            #endif
    }

    /// The stored display zone from whichever answer arrived, or nil
    /// before any has (for example offline at launch).
    private var displayZone: TimeZone? {
        (viewModel.period ?? summary.summary?.period).flatMap { TimeZone(identifier: $0.timeZone) }
    }

    /// True while the open tab's list is on its way.
    private var isLoading: Bool {
        switch tab {
        case .tracks: return viewModel.status == .loading
        case .albums: return albums.status == .loading
        case .singles: return newToYou.status == .loading
        }
    }

    /// True while the open tab reloads with its old rows still on screen,
    /// or while a new user's first poll is on its way.
    private var isReloading: Bool {
        if connection.isWaitingForFirstPoll { return true }
        switch tab {
        case .tracks: return viewModel.status == .loading && !viewModel.days.isEmpty
        case .albums: return albums.status == .loading && albums.answer != nil
        case .singles: return newToYou.status == .loading && newToYou.answer != nil
        }
    }

    // MARK: - Loading

    /// Loads the summary and every tab for `query` side by side, so a
    /// tab switch shows its list at once. Each can fail on its own
    /// without blanking the others.
    private func loadAll() async {
        viewModel.query = query
        summary.query = query
        albums.query = query
        singles.query = query
        newToYou.query = query
        async let timelineLoaded: Void = viewModel.load()
        async let summaryLoaded: Void = summary.load()
        async let albumListsLoaded: Void = loadAlbumLists()
        _ = await (timelineLoaded, summaryLoaded, albumListsLoaded)
    }

    /// "New to you", "All albums" and "All singles & EPs", asked for side
    /// by side but shown top-down: the two full lists wait for "New to
    /// you" above them, so new releases never appear late and push the
    /// list down.
    private func loadAlbumLists() async {
        async let albumsFetched = albums.fetch()
        async let singlesFetched = singles.fetch()
        await newToYou.load()
        let (albumsAnswer, singlesAnswer) = await (albumsFetched, singlesFetched)
        if !Task.isCancelled {
            albums.show(albumsAnswer)
            singles.show(singlesAnswer)
        }
    }

    /// Loads the summary and one tab's lists: pull-to-refresh and
    /// "Try again" reload only what is on screen.
    private func load(_ tab: MainTab) async {
        async let summaryLoaded: Void = summary.load()
        switch tab {
        case .tracks:
            await viewModel.load()
        case .albums, .singles:
            await loadAlbumLists()
        }
        await summaryLoaded
    }

    /// After "Reconnect Spotify": check the connection again, and reload
    /// the plays in case the new session belongs to the account with them.
    private func reloadAfterReconnect() async {
        await connection.load()
        await loadAll()
    }

    /// "Sync now" in Settings and pull-to-refresh. Asks the server to poll
    /// Spotify now; on 202, waits about 20 s, then reloads once.
    private func refreshFromSpotify() async {
        switch await refresh.request() {
        case .accepted:
            await refresh.waitForPoll()
            async let connectionChecked: Void = connection.load()
            await loadAll()
            await connectionChecked
            refresh.finishChecking()
        case .notConnected:
            // The connection answer turns into the "Connect Spotify" banner.
            await connection.load()
        case .coolingDown, .failed:
            break
        }
    }

    /// Pull-to-refresh on any tab.
    private func pullToRefresh(_ tab: MainTab) async {
        // The Spotify request runs on its own, so the spinner does not
        // sit there for the 20 s wait. Meanwhile, reload what the server
        // already has for the summary and this tab.
        Task { await refreshFromSpotify() }
        async let connectionChecked: Void = connection.load()
        await load(tab)
        await connectionChecked
    }

    /// "Open timeline" after a history import: back to this screen, and
    /// reload, so the imported plays and the marker show.
    private func openTimelineAfterImport() {
        showsSettings = false
        Task {
            await connection.load()
            await loadAll()
        }
    }

    private func openCustomRange() {
        customRange = CustomRangeViewModel(api: api, current: viewModel.period)
    }

    // MARK: - Tabs

    /// Every tab stays on screen, and only the open one is visible,
    /// so each keeps its scroll position when you switch.
    private var tabs: some View {
        ZStack {
            page(.tracks) { tracksContent }
            page(.albums) { albumsContent }
            page(.singles) { singlesContent }
        }
    }

    /// One tab's page: hidden, untouchable and silent for VoiceOver
    /// unless it is the open tab.
    ///
    /// Each page has its own switch at the bottom, so its list gets its
    /// own fade under the glass. One switch over the three stacked lists
    /// faded only the first, and every tab change flashed the glass. All
    /// three follow `tab`, so the pill slides in the one on screen.
    private func page(_ pageTab: MainTab, @ViewBuilder content: () -> some View) -> some View {
        let isOpen = tab == pageTab
        return content()
            // The "Not new" Undo banner sits above the switch, never
            // over it (WHO-123, WHO-145).
            .notNewBanner(newToYou.marker)
            .mainTabBar($tab, page: pageTab)
            .opacity(isOpen ? 1 : 0)
            .allowsHitTesting(isOpen)
            .accessibilityHidden(!isOpen)
    }

    /// The summary at the top of every tab.
    private var summaryRow: some View {
        Section {
            SummaryHeader(viewModel: summary) { Task { await summary.load() } }
                .listRowInsets(Theme.rowInsets)
                .listRowSeparator(.hidden)
        }
    }

    // MARK: Tracks

    @ViewBuilder
    private var tracksContent: some View {
        switch viewModel.status {
        case .loading where viewModel.days.isEmpty:
            SkeletonList()
        case .failed(let error):
            failure(error, title: nil, tab: .tracks)
        case .empty(let reason):
            empty(reason: reason)
        default:
            playList
        }
    }

    private var playList: some View {
        // The reader is only for the `plays-many-bottom` screenshot.
        ScrollViewReader { proxy in
            List {
                summaryRow
                // The first 10 plays, with "Show all" (WHO-131).
                PlaysSection(viewModel: viewModel, playCount: summary.summary?.totals.playCount) { event in
                    trackComment = viewModel.trackComment(event)
                }
            }
            .listStyle(.plain)
            .refreshable { await pullToRefresh(.tracks) }
            .sheet(item: $trackComment) { model in
                CommentEditor(name: model.title, viewModel: model)
            }
            #if DEBUG
                .task {
                    guard DebugFixtures.scrollsToPlays else { return }
                    try? await Task.sleep(for: .seconds(1.5))
                    proxy.scrollTo(PlaysSection.showAllID, anchor: .bottom)
                }
            #endif
        }
    }

    // MARK: Albums

    @ViewBuilder
    private var albumsContent: some View {
        if let answer = albums.answer, answer.isEmpty {
            empty(reason: answer.emptyReason)
        } else if case .failed(let error) = albums.status {
            failure(error, title: Messages.failureTitle(error, section: Messages.albumsFailed), tab: .albums)
        } else {
            // While loading, the sections show placeholder rows.
            List {
                summaryRow
                reviewOffer
                ratingsEntry(.albums)
                Section {
                    NewToYouSection(viewModel: newToYou, list: .albums, summary: summary.summary) {
                        Task { await newToYou.load() }
                    }
                }
                Section {
                    AlbumsSection(viewModel: albums) {
                        Task { await albums.load() }
                    }
                }
            }
            .listStyle(.plain)
            .refreshable { await pullToRefresh(.albums) }
        }
    }

    // MARK: Singles

    /// The summary, "New to you" and every single and EP played in the
    /// period; its empty and failed states are those of "New to you".
    @ViewBuilder
    private var singlesContent: some View {
        if let answer = newToYou.answer, answer.isEmpty {
            empty(reason: answer.emptyReason)
        } else {
            List {
                summaryRow
                reviewOffer
                ratingsEntry(.singles)
                Section {
                    NewToYouSection(viewModel: newToYou, list: .singles, summary: summary.summary) {
                        Task { await newToYou.load() }
                    }
                }
                Section {
                    AlbumsSection(viewModel: singles) {
                        Task { await singles.load() }
                    }
                }
            }
            .listStyle(.plain)
            .refreshable { await pullToRefresh(.singles) }
        }
    }

    // MARK: Review offer

    @ViewBuilder
    private var reviewOffer: some View {
        if review.isOffered, let count = review.review?.count {
            Section {
                ReviewOfferRow(count: count) { showsReview = true }
            }
        }
    }

    // MARK: Ratings entry

    /// Opens the ratings of `shelf` only: the albums' on the Albums tab,
    /// the singles' and EPs' on the Singles tab.
    @ViewBuilder
    private func ratingsEntry(_ shelf: RatingsShelf) -> some View {
        if ratings.isOffered(shelf), let answer = ratings.answer {
            Section {
                NavigationLink {
                    RatingsView(viewModel: ratings, shelf: shelf)
                } label: {
                    RatingsEntryRow(ratings: answer, shelf: shelf)
                }
                .listRowInsets(Theme.rowInsets)
            }
        }
    }

    // MARK: Empty and failed, the same on every tab

    /// A failed tab with nothing to show. `title` replaces the error's own
    /// title, for example "Couldn't load albums." (see `Messages.failureTitle`).
    private func failure(_ error: APIError, title: String?, tab: MainTab) -> some View {
        let text = Messages.failure(error)
        return ContentUnavailableView {
            Label(title ?? text.title, systemImage: text.systemImage)
        } description: {
            Text(text.detail)
        } actions: {
            Button("Try again") { Task { await load(tab) } }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
        }
    }

    /// The period has no plays. The period bar stays above this, so the
    /// user still sees which dates are empty and can move to others (R-54).
    private func empty(reason: String?) -> some View {
        let text = Messages.empty(reason: reason)
        return ContentUnavailableView {
            Label(text.title, systemImage: text.systemImage)
        } description: {
            Text(text.detail)
        }
    }
}

/// The sticky header over one day: the date, and that day's total once
/// all of the day's plays have loaded.
struct DayHeader: View {
    let day: DayGroup
    let timeZone: TimeZone

    var body: some View {
        let title = DisplayFormat.dayHeader(localDate: day.localDate, timeZone: timeZone)
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .font(.headline)
                .foregroundStyle(.primary)
            Spacer(minLength: Theme.Spacing.s)
            if let total = day.totalMs {
                Text(DisplayFormat.total(milliseconds: total))
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(Theme.secondaryText)
            }
        }
        .textCase(nil)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenLabel(title: title))
        .accessibilityAddTraits(.isHeader)
    }

    private func spokenLabel(title: String) -> String {
        guard let total = day.totalMs else { return title }
        return "\(title), total \(DisplayFormat.spokenDuration(milliseconds: total))"
    }
}

/// "Collection started 14:32. Earlier plays were imported from Spotify."
/// (T1, WHO-103 §6.6). A plain note between plays, not a
/// warning: no icon and no warning colour. The time only, because the
/// row is always under its own day's header.
struct ImportMarkerRow: View {
    let marker: ImportMarker
    let timeZone: TimeZone

    var body: some View {
        Text(Messages.importMarker(time: DisplayFormat.clockTime(marker.at, timeZone: timeZone)))
            .font(.footnote)
            .foregroundStyle(Theme.secondaryText)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, Theme.Spacing.xs)
    }
}

/// Placeholder rows of the real row height while the first page loads.
struct SkeletonList: View {
    var body: some View {
        List {
            Section {
                SummaryNumbers(summary: SummaryHeader.placeholder)
                    .listRowSeparator(.hidden)
            }
            Section {
                ForEach(0..<8, id: \.self) { _ in
                    PlayRow(event: SkeletonList.placeholderEvent, timeZone: .current)
                }
            } header: {
                Text("Placeholder day").font(.headline).textCase(nil)
            }
        }
        .listStyle(.plain)
        .redacted(reason: .placeholder)
        .disabled(true)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading your plays")
    }

    static let placeholderEvent = TimelineEvent(
        id: "placeholder", source: "spotify", kind: "listen",
        occurredAt: Date(timeIntervalSince1970: 0), occurredAtPrecision: "second",
        localDate: "1970-01-01", durationMs: 222_000, playedMs: nil,
        title: "Placeholder track title", creators: ["Placeholder artist"],
        collection: nil, artworkUrl: nil, itemKey: "placeholder")
}

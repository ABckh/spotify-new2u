import Foundation
import Observation

/// Loads the plays for one period, page by page, for `TimelineView`
/// (the first `previewCount` plays) and `TimelineAllView` (all of them).
///
/// The view reads the properties below and calls `load()` and
/// `loadMoreIfNeeded(after:)`. Everything here is plain `async/await`.
@Observable
final class TimelineViewModel {
    /// What the screen should show in place of the list, if anything.
    enum Status: Equatable {
        /// The first page is on its way. The view shows skeleton rows,
        /// or keeps the previous period's rows if it has any.
        case loading
        /// Rows are on screen.
        case loaded
        /// The period has no plays. `reason` is the contract's `empty_reason`.
        case empty(reason: String?)
        /// The first page failed and there is nothing to show.
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    private(set) var period: ResolvedPeriod?
    private(set) var days: [DayGroup] = []
    /// Set when a later page or a refresh fails while rows are on screen.
    private(set) var bannerError: APIError?

    var query: PeriodQuery

    /// The Spotify connection, set by `TimelineView` once its check has
    /// answered. Only used for the import marker.
    var connection: Connection? {
        didSet {
            if connection != oldValue { regroup() }
        }
    }

    private let api: APIClient
    private var events: [TimelineEvent] = []
    /// The user's earliest stored play, from the first page.
    private var coverageStart: Date?
    private var nextCursor: String?
    private var isLoadingMore = false

    /// How many rows before the end the next page starts loading, so
    /// the user never sees the bottom of the list while it loads.
    static let prefetchDistance = 15
    static let pageSize = 50
    /// How many plays the main screen shows above "Show all" (WHO-131).
    static let previewCount = 10

    init(api: APIClient, query: PeriodQuery = PeriodQuery(kind: .week)) {
        self.api = api
        self.query = query
    }

    /// The display zone for every time and date on this screen.
    var displayTimeZone: TimeZone {
        period.flatMap { TimeZone(identifier: $0.timeZone) } ?? .current
    }

    /// Where to draw "Collection started …" (WHO-90 T1), or nil for no
    /// marker. Shown only when all of these hold:
    /// - the server says the import stored a play from before
    ///   `collection_started_at` (`has_imported_plays` is true). An
    ///   import that found no earlier play shows nothing (WHO-93 F2);
    /// - a stored play is older than `collection_started_at` (the
    ///   earliest play, `coverage_start`, is before it);
    /// - this period contains `collection_started_at`.
    var importMarker: ImportMarker? {
        guard let connection, connection.showsImportMarker,
              let started = connection.collectionStartedAt,
              let coverageStart, coverageStart < started,
              let period, period.start <= started, started < period.end
        else { return nil }
        return ImportMarker(at: started, localDate: DisplayFormat.localDate(started, timeZone: displayTimeZone))
    }

    /// The first `previewCount` plays under their days, for the main
    /// screen. The marker shows only if it falls among them.
    var previewDays: [DayGroup] {
        DayGroup.make(from: Array(events.prefix(Self.previewCount)), hasMorePages: hasMoreThanPreview, marker: importMarker)
    }

    /// True when the period has more plays than the main screen shows,
    /// so it needs "Show all".
    var hasMoreThanPreview: Bool {
        events.count > Self.previewCount || nextCursor != nil
    }

    /// Runs after a track's comment changed here, with the comment (nil
    /// when cleared) and the track. "Show all" uses it to change the
    /// main screen's plays too.
    var trackCommentChanged: (String?, String) -> Void = { _, _ in }

    /// A model for the comment of the track `event` is a play of. Once
    /// saved, every loaded play of the track shows it, without a reload.
    func trackComment(_ event: TimelineEvent) -> TrackCommentModel {
        let model = TrackCommentModel(api: api, itemKey: event.itemKey, title: event.title, comment: event.comment)
        model.changed = { [weak self] comment in
            self?.setComment(comment, itemKey: event.itemKey)
            self?.trackCommentChanged(comment, event.itemKey)
        }
        return model
    }

    /// Shows `comment` on every loaded play of the track `itemKey`.
    func setComment(_ comment: String?, itemKey: String) {
        guard events.contains(where: { $0.itemKey == itemKey }) else { return }
        for index in events.indices where events[index].itemKey == itemKey {
            events[index].comment = comment
        }
        regroup()
    }

    /// A model for "Show all": the same period and connection, loaded
    /// on its own and paged as the user scrolls.
    func makeShowAll() -> TimelineViewModel {
        let all = TimelineViewModel(api: api, query: query)
        all.connection = connection
        all.trackCommentChanged = { [weak self] comment, itemKey in
            self?.setComment(comment, itemKey: itemKey)
        }
        return all
    }

    /// Loads the first page of `query`. The rows already on screen stay
    /// there until the new answer arrives (design bar: never clear to white).
    func load() async {
        status = .loading
        bannerError = nil
        do {
            let page: TimelinePage = try await api.get("/v1/timeline", query: firstPageQuery())
            try Task.checkCancellation()
            show(firstPage: page)
        } catch is CancellationError {
            return
        } catch let error as APIError {
            handle(error, whileLoadingFirstPage: true)
        } catch {
            handle(.badResponse, whileLoadingFirstPage: true)
        }
    }

    /// Call when a row appears. Starts the next page once the user is
    /// within `prefetchDistance` rows of the end.
    func loadMoreIfNeeded(after event: TimelineEvent) async {
        guard let cursor = nextCursor, !isLoadingMore, status == .loaded else { return }
        guard let index = events.firstIndex(where: { $0.id == event.id }),
              index >= events.count - Self.prefetchDistance
        else { return }

        isLoadingMore = true
        defer { isLoadingMore = false }
        do {
            // The same period parameters on every page, plus the cursor
            // (api-contract section 3.1).
            var items = firstPageQuery()
            items.append(URLQueryItem(name: "cursor", value: cursor))
            let page: TimelinePage = try await api.get("/v1/timeline", query: items)
            try Task.checkCancellation()
            events.append(contentsOf: page.events)
            nextCursor = page.nextCursor
            regroup()
            bannerError = nil
        } catch is CancellationError {
            return
        } catch APIError.invalidRequest(code: "invalid_cursor") {
            // The period moved under us (for example "today" crossed
            // midnight). The contract says: reload from the first page.
            await load()
        } catch let error as APIError {
            handle(error, whileLoadingFirstPage: false)
        } catch {
            handle(.badResponse, whileLoadingFirstPage: false)
        }
    }

    // MARK: - Private

    private func firstPageQuery() -> [URLQueryItem] {
        query.queryItems + [URLQueryItem(name: "limit", value: String(Self.pageSize))]
    }

    private func show(firstPage page: TimelinePage) {
        period = page.period
        events = page.events
        coverageStart = page.coverageStart
        nextCursor = page.nextCursor
        regroup()
        status = page.isEmpty || page.events.isEmpty ? .empty(reason: page.emptyReason) : .loaded
    }

    /// Puts the loaded plays, and the import marker if any, under their days.
    private func regroup() {
        days = DayGroup.make(from: events, hasMorePages: nextCursor != nil, marker: importMarker)
    }

    /// A 401 needs nothing here: APIClient has already ended the session,
    /// and RootView swaps this screen for the connect screen.
    private func handle(_ error: APIError, whileLoadingFirstPage: Bool) {
        if days.isEmpty {
            status = .failed(error)
        } else {
            // Keep what is on screen and say what happened above it.
            if whileLoadingFirstPage { status = .loaded }
            bannerError = error
        }
    }
}

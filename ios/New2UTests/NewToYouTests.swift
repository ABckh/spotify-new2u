import XCTest
@testable import New2U

/// "New to you" (S14): decoding, the request, failures and the words.
@MainActor
final class NewToYouTests: XCTestCase {
    private var store: SessionStore!

    /// The example from api-contract section 3.9, with a second album
    /// so the order the server sends is checked too.
    static let contractExample = """
    {"period": \(TimelineFixtures.period),
     "albums": {"count": 4, "items": [
        {"album_id": "4aawyAB9vmqN3uQ7FjRGTy", "name": "Album", "artists": ["Artist"],
         "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
         "tracks_played_in_period": 8, "tracks_listened": 11, "total_tracks": 16, "artwork_url": "https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228"},
        {"album_id": "zzz", "name": null, "artists": [],
         "first_played_at": "2026-10-06T18:00:00.000Z", "first_played_local_date": "2026-10-06",
         "tracks_played_in_period": 1, "tracks_listened": 1, "total_tracks": 1, "artwork_url": null}]},
     "singles": {"count": 2, "items": [
        {"album_id": "EP1", "name": "Four Songs", "artists": ["Artist"],
         "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
         "tracks_played_in_period": 3, "tracks_listened": 3, "total_tracks": 4, "artwork_url": null,
         "rating": 8, "release_type": "ep"}]},
     "tracks": {"count": 9, "items": [
        {"item_key": "id:AAA", "title": "Track", "creators": ["Artist"],
         "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05"}]},
     "coverage_start": "2026-09-10T18:00:00.000Z",
     "coverage_start_local_date": "2026-09-10",
     "period_contains_coverage_start": false,
     "nothing_new": false,
     "is_empty": false,
     "empty_reason": null,
     "collection_gap_overlap": false}
    """

    private func makeAPI() -> APIClient {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        self.store = store
        addTeardownBlock { @MainActor in store.deleteToken() }
        return APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
    }

    func testDecodesTheContractExampleInServerOrder() throws {
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(Self.contractExample.utf8))
        XCTAssertEqual(answer.albums.count, 4)
        // S14.6: the app keeps the server's order and never re-sorts.
        XCTAssertEqual(answer.albums.items.map(\.albumId), ["4aawyAB9vmqN3uQ7FjRGTy", "zzz"])
        XCTAssertNil(answer.albums.items[1].name)
        // Singles and EPs: the album shape, with their release type.
        XCTAssertEqual(answer.singles.count, 2)
        XCTAssertEqual(answer.singles.items.first?.releaseType, .ep)
        XCTAssertEqual(answer.singles.items.first?.rating, 8)
        XCTAssertNil(answer.albums.items[0].releaseType)
        // WHO-115: the deprecated `tracks` list is still sent and ignored.
        XCTAssertFalse(answer.showsNothingNew)
        XCTAssertEqual(answer.coverageStartLocalDate, "2026-09-10")
        XCTAssertFalse(answer.nothingNew)
        XCTAssertEqual(answer.zone.identifier, "Europe/Warsaw")
    }

    func testLoadSendsThePeriodAndAsksForTheFirstTen() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }

        await model.load()

        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.answer?.albums.count, 4)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/new-to-you")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1&all=false")
    }

    /// S14.7: "Show all" asks for the same period with `all=true`.
    func testShowAllAsksForEveryEntryOfTheSamePeriod() async {
        let section = NewToYouViewModel(api: makeAPI(), query: .custom(from: "2026-10-01", to: "2026-10-14"))
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }

        await section.makeShowAll().load()

        XCTAssertEqual(
            StubURLProtocol.lastRequest?.url?.query, "period=custom&from=2026-10-01&to=2026-10-14&all=true")
    }

    /// S14.16: offline drops the old lists, so another period's lists
    /// are never shown.
    func testOfflineDropsTheOldLists() async {
        let model = NewToYouViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertNil(model.answer)
    }

    /// `count` new albums, the first `sent` of them in `items`.
    private static func albums(count: Int, sent: Int) -> String {
        let items = (0..<sent).map { index in
            """
            {"album_id": "album\(index)", "name": "Album \(index)", "artists": ["Artist"],
             "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
             "tracks_played_in_period": 1, "tracks_listened": 1, "total_tracks": 10, "artwork_url": null}
            """
        }
        return """
        {"period": \(TimelineFixtures.period),
         "albums": {"count": \(count), "items": [\(items.joined(separator: ","))]},
         "singles": {"count": 0, "items": []},
         "coverage_start_local_date": "2026-09-10", "period_contains_coverage_start": false,
         "nothing_new": false, "is_empty": false, "empty_reason": null}
        """
    }

    /// WHO-196: with more than the 10 albums the section has, Share asks
    /// for the whole list of the same period first, so the picture holds
    /// up to 25.
    func testShareAsksForTheWholeListWhenThereAreMoreThanTen() async throws {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        // Built here: the stub runs off the main actor.
        let first10 = Self.albums(count: 30, sent: 10), every = Self.albums(count: 30, sent: 30)
        StubURLProtocol.handler = { request in
            let all = request.url?.query?.contains("all=true") ?? false
            return .init(body: all ? every : first10)
        }
        await model.load()

        let shared = await model.shareCard(summary: nil)
        let card = try XCTUnwrap(shared)

        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1&all=true")
        XCTAssertEqual(card.albums.count, 25)
        XCTAssertEqual(card.moreCount, 5)
        // The section keeps its first 10.
        XCTAssertEqual(model.answer?.albums.items.count, 10)
    }

    func testShareUsesTheTenAlreadyLoadedWhenTheWholeListFails() async throws {
        let model = NewToYouViewModel(api: makeAPI())
        let first10 = Self.albums(count: 30, sent: 10)
        StubURLProtocol.handler = { _ in .init(body: first10) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(status: 500, body: "oops") }

        let shared = await model.shareCard(summary: nil)
        let card = try XCTUnwrap(shared)

        XCTAssertEqual(card.albums.count, 10)
        XCTAssertEqual(card.moreCount, 20)
        XCTAssertEqual(model.status, .loaded)
    }

    func testShareAsksForNothingWhenEveryAlbumIsLoaded() async throws {
        let model = NewToYouViewModel(api: makeAPI())
        let seven = Self.albums(count: 7, sent: 7)
        StubURLProtocol.handler = { _ in .init(body: seven) }
        await model.load()
        let before = StubURLProtocol.requestCount

        let shared = await model.shareCard(summary: nil)
        let card = try XCTUnwrap(shared)

        XCTAssertEqual(StubURLProtocol.requestCount, before)
        XCTAssertEqual(card.albums.count, 7)
    }

    /// S14.17: a server failure is this section's own error.
    func testServerErrorIsTheSectionsOwnFailure() async {
        let model = NewToYouViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(status: 500, body: "oops") }
        await model.load()
        XCTAssertEqual(model.status, .failed(.server(status: 500)))
        XCTAssertTrue(store.hasToken)
    }

    func testExpiredSessionEndsTheSession() async {
        let model = NewToYouViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
    }

    // MARK: - Words

    func testV1CarriesTheHistoryStartDateInTheDeviceLocale() throws {
        let warsaw = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        let date = DisplayFormat.longDate(localDate: "2026-09-10", timeZone: warsaw, locale: Locale(identifier: "en_GB"))
        XCTAssertEqual(date, "10 September 2026")
        XCTAssertEqual(
            Messages.newToYouMeaning(historyStart: date),
            "New means first played since 10 September 2026, when your history here begins. This app does not know what you played before that date.")
    }

    func testFirstPlayDateShowsTheYearOnlyOutsideThisYear() throws {
        let warsaw = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        let gb = Locale(identifier: "en_GB")
        let now = try XCTUnwrap(APIClient.parseInstant("2026-10-15T12:00:00.000Z"))
        XCTAssertEqual(DisplayFormat.shortDate(localDate: "2026-10-05", timeZone: warsaw, locale: gb, now: now), "5 Oct")
        XCTAssertEqual(DisplayFormat.shortDate(localDate: "2025-12-31", timeZone: warsaw, locale: gb, now: now), "31 Dec 2025")
    }

    func testHeadingsUseTheFullCount() {
        XCTAssertEqual(Messages.newAlbums(4), "4 new albums")
        XCTAssertEqual(Messages.newAlbums(1), "1 new album")
        XCTAssertEqual(Messages.newAlbums(0), "No new albums")
        XCTAssertEqual(Messages.newSingles(2), "2 new singles & EPs")
        XCTAssertEqual(Messages.newSingles(1), "1 new single or EP")
    }

    /// VoiceOver reads one entry as one sentence (design bar).
    func testAnAlbumEntryIsOneSpokenSentence() throws {
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(Self.contractExample.utf8))
        let row = NewItemRow(album: answer.albums.items[0], timeZone: answer.zone)
        XCTAssertEqual(row.title, "Album")
        XCTAssertEqual(row.detail, "11 of 16 tracks")
        XCTAssertTrue(row.spoken.hasPrefix("Album, by Artist. First played "))
        // WHO-153: the count is over the whole history, not the period.
        XCTAssertTrue(row.spoken.hasSuffix("11 of 16 tracks played."))
    }

    /// WHO-153: the row shows the count the "new" rule uses, not the
    /// period count the server still sends.
    func testAnAlbumRowShowsTracksListenedOfTotal() throws {
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(Self.contractExample.utf8))
        XCTAssertEqual(answer.albums.items[0].tracksListened, 11)
        XCTAssertEqual(answer.albums.items[0].totalTracks, 16)
        XCTAssertEqual(NewItemRow(album: answer.albums.items[1], timeZone: answer.zone).detail, "1 of 1 track")
        XCTAssertEqual(Messages.tracksOf(2, 3), "2 of 3 tracks")
    }

    // MARK: - Nothing new while the server still counts tracks (WHO-116)

    /// A period whose only new item is a track on a known album: the
    /// server says `nothing_new: false` but there is nothing to list.
    func testOnlyNewTracksShowsNothingNew() throws {
        let json = Self.contractExample
            .replacingOccurrences(of: #""albums": {"count": 4"#, with: #""albums": {"count": 0"#)
            .replacingOccurrences(of: #""singles": {"count": 2"#, with: #""singles": {"count": 0"#)
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(json.utf8))
        XCTAssertFalse(answer.nothingNew)
        XCTAssertTrue(answer.showsNothingNew)
    }

    // MARK: - One album's tracks (WHO-115)

    /// The example from api-contract section 3.10, with a second track
    /// so the server's order is checked too.
    static let albumExample = """
    {"period": \(TimelineFixtures.period),
     "album": {"album_id": "4aawyAB9vmqN3uQ7FjRGTy", "name": "Album", "artists": ["Artist"],
               "artwork_url": "https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228"},
     "tracks": [
        {"item_key": "id:BBB", "title": "Zebra", "creators": ["Artist"], "play_count": 3,
         "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
         "is_new": true},
        {"item_key": "id:AAA", "title": "Apple", "creators": [], "play_count": 1,
         "first_played_at": "2025-01-05T18:00:00.000Z", "first_played_local_date": "2025-01-05",
         "is_new": false}]}
    """

    func testAlbumTracksAsksForTheSectionsPeriodAndKeepsTheOrder() async {
        let section = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        StubURLProtocol.handler = { _ in .init(body: Self.albumExample) }

        let model = section.makeAlbumTracks(albumId: "4aawyAB9vmqN3uQ7FjRGTy")
        await model.load()

        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/new-to-you/albums/4aawyAB9vmqN3uQ7FjRGTy/tracks")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1")
        // Never re-sorted: "Zebra" stays before "Apple".
        XCTAssertEqual(model.answer?.tracks.map(\.itemKey), ["id:BBB", "id:AAA"])
        XCTAssertEqual(model.answer?.tracks.map(\.isNew), [true, false])
        XCTAssertEqual(model.answer?.album.name, "Album")
    }

    func testUnknownAlbumDecodesAsAnEmptyList() async throws {
        let json = """
        {"period": \(TimelineFixtures.period),
         "album": {"album_id": "zzz", "name": null, "artists": []}, "tracks": []}
        """
        let answer = try APIClient.makeDecoder().decode(AlbumTracks.self, from: Data(json.utf8))
        XCTAssertNil(answer.album.name)
        XCTAssertTrue(answer.tracks.isEmpty)
    }

    func testAlbumTracksOfflineDropsTheOldList() async {
        let model = AlbumTracksViewModel(api: makeAPI(), albumId: "abc", query: PeriodQuery(kind: .week))
        StubURLProtocol.handler = { _ in .init(body: Self.albumExample) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertNil(model.answer)
    }

    func testAlbumTracksExpiredSessionEndsTheSession() async {
        let model = AlbumTracksViewModel(api: makeAPI(), albumId: "abc", query: PeriodQuery(kind: .week))
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
    }

    func testATrackRowIsOneSpokenSentenceAndSaysNew() throws {
        let answer = try APIClient.makeDecoder().decode(AlbumTracks.self, from: Data(Self.albumExample.utf8))
        XCTAssertEqual(
            AlbumTrackRow(track: answer.tracks[0], showsNew: answer.tracks[0].isNew).spoken,
            "Zebra, by Artist. 3 plays in this period. New to you.")
        XCTAssertEqual(
            AlbumTrackRow(track: answer.tracks[1], showsNew: answer.tracks[1].isNew).spoken,
            "Apple, by Unknown artist. 1 play in this period.")
    }
}

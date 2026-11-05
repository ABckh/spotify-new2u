import XCTest
@testable import New2U

/// "Albums" (WHO-134): decoding, the requests, failures and the words.
@MainActor
final class AlbumsTests: XCTestCase {
    private var store: SessionStore!

    /// The WHO-133 shape: a new and an old album, the second with an
    /// unknown name, `count` larger than the items sent.
    static let example = """
    {"period": \(TimelineFixtures.period),
     "albums": {"count": 14, "items": [
        {"album_id": "zzz", "name": "Zebra", "artists": ["Artist"], "play_count": 9,
         "tracks_played_in_period": 4, "total_ms": 1800000, "is_new": false,
         "artwork_url": "https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228"},
        {"album_id": "aaa", "name": null, "artists": [], "play_count": 1,
         "tracks_played_in_period": 1, "total_ms": 200000, "is_new": true, "artwork_url": null}]},
     "is_empty": false,
     "empty_reason": null,
     "coverage_start": "2026-09-10T18:00:00.000Z",
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

    func testDecodesInServerOrder() throws {
        let answer = try APIClient.makeDecoder().decode(PlayedAlbums.self, from: Data(Self.example.utf8))
        XCTAssertEqual(answer.albums.count, 14)
        // The app keeps the server's order (most played first) and never re-sorts.
        XCTAssertEqual(answer.albums.items.map(\.albumId), ["zzz", "aaa"])
        XCTAssertEqual(answer.albums.items[0].playCount, 9)
        XCTAssertEqual(answer.albums.items[0].totalMs, 1_800_000)
        XCTAssertFalse(answer.albums.items[0].isNew)
        XCTAssertTrue(answer.albums.items[1].isNew)
        XCTAssertNil(answer.albums.items[1].name)
        XCTAssertFalse(answer.isEmpty)
    }

    func testDecodesAnEmptyPeriod() throws {
        let json = """
        {"period": \(TimelineFixtures.period), "albums": {"count": 0, "items": []},
         "is_empty": true, "empty_reason": "no_plays", "coverage_start": null,
         "collection_gap_overlap": false}
        """
        let answer = try APIClient.makeDecoder().decode(PlayedAlbums.self, from: Data(json.utf8))
        XCTAssertTrue(answer.isEmpty)
        XCTAssertEqual(answer.emptyReason, "no_plays")
    }

    func testLoadSendsThePeriodAndAsksForTheFirstTen() async {
        let model = AlbumsViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        StubURLProtocol.handler = { _ in .init(body: Self.example) }

        await model.load()

        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.answer?.albums.items.count, 2)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/albums")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1&all=false")
    }

    func testShowAllAsksForEveryAlbumOfTheSamePeriod() async {
        let section = AlbumsViewModel(api: makeAPI(), query: .custom(from: "2026-10-01", to: "2026-10-14"))
        StubURLProtocol.handler = { _ in .init(body: Self.example) }

        let all = section.makeShowAll()
        await all.load()

        XCTAssertTrue(all.all)
        XCTAssertEqual(
            StubURLProtocol.lastRequest?.url?.query, "period=custom&from=2026-10-01&to=2026-10-14&all=true")
    }

    /// A tapped album asks `/v1/albums/{id}/tracks` for the same period.
    func testAnAlbumOpensItsTracksForTheSamePeriod() async {
        let section = AlbumsViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        StubURLProtocol.handler = { _ in .init(body: NewToYouTests.albumExample) }

        await section.makeAlbumTracks(albumId: "zzz").load()

        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/albums/zzz/tracks")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1")
    }

    /// From "New to you" the album still asks the old path.
    func testNewToYouAlbumKeepsItsPath() async {
        let section = NewToYouViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: NewToYouTests.albumExample) }
        await section.makeAlbumTracks(albumId: "zzz").load()
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/new-to-you/albums/zzz/tracks")
    }

    func testOfflineDropsTheOldAlbums() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.example) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertNil(model.answer)
    }

    func testServerErrorIsTheSectionsOwnFailure() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(status: 500, body: "oops") }
        await model.load()
        XCTAssertEqual(model.status, .failed(.server(status: 500)))
        XCTAssertTrue(store.hasToken)
    }

    func testFetchShowsNothingUntilShown() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.example) }
        let fetched = await model.fetch()
        // The Albums tab holds the albums back until "New to you" is on screen.
        XCTAssertEqual(model.status, .loading)
        XCTAssertNil(model.answer)

        model.show(fetched)
        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.answer?.albums.items.count, 2)
    }

    func testAFailedFetchIsShownAsTheSectionsFailure() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.example) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(status: 500, body: "oops") }
        let fetched = await model.fetch()
        // The old albums stay until the failure is shown.
        XCTAssertNotNil(model.answer)

        model.show(fetched)
        XCTAssertEqual(model.status, .failed(.server(status: 500)))
        XCTAssertNil(model.answer)
    }

    func testACancelledFetchChangesNothing() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.example) }
        await model.load()
        model.show(nil)
        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.answer?.albums.items.count, 2)
    }

    func testExpiredSessionEndsTheSession() async {
        let model = AlbumsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
    }

    // MARK: - Words

    func testCountWords() {
        XCTAssertEqual(Messages.albums(12), "12 albums")
        XCTAssertEqual(Messages.albums(1), "1 album")
        XCTAssertEqual(Messages.albums(0), "No albums")
    }

    func testARowIsOneSpokenSentenceWithTheFallbacks() throws {
        let answer = try APIClient.makeDecoder().decode(PlayedAlbums.self, from: Data(Self.example.utf8))
        XCTAssertEqual(
            PlayedAlbumRow(album: answer.albums.items[0]).spoken,
            "Zebra, by Artist. 9 plays, 30 minutes in this period.")
        XCTAssertEqual(
            PlayedAlbumRow(album: answer.albums.items[1]).spoken,
            "Unknown album, by Unknown artist. 1 play, 3 minutes, 20 seconds in this period. New to you.")
    }
}

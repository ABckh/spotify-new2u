import XCTest
@testable import New2U

/// Album ratings: decoding, rating from the album screen (and its Undo on
/// a failure), the all-time list, and the rating on the share picture.
@MainActor
final class RatingsTests: XCTestCase {
    /// Every request the stub saw, as "METHOD path", with its JSON body.
    nonisolated(unsafe) private static var seen: [(String, NSDictionary?)] = []

    private func makeAPI() -> APIClient {
        StubURLProtocol.reset()
        Self.seen = []
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        addTeardownBlock { @MainActor in store.deleteToken() }
        return APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
    }

    static func albumTracks(rating: String) -> String {
        """
        {"period": \(TimelineFixtures.period),
         "album": {"album_id": "ALB1", "name": "Album", "artists": ["Artist"],
                   "artwork_url": null, "rating": \(rating)},
         "tracks": []}
        """
    }

    static let ratings = """
    {"count": 4, "items": [
       {"album_id": "TOP", "name": "Top", "artists": ["A"], "artwork_url": null,
        "release_type": "album", "rating": 10, "rated_at": "2026-10-28T10:00:00.000Z"},
       {"album_id": "ONE", "name": "One Song", "artists": ["B"], "artwork_url": null,
        "release_type": "single", "rating": 9, "rated_at": "2026-10-28T09:00:00.000Z"},
       {"album_id": "EP", "name": "Four Songs", "artists": ["C"], "artwork_url": null,
        "release_type": "ep", "rating": 7, "rated_at": "2026-10-28T08:00:00.000Z"},
       {"album_id": "MID", "name": null, "artists": [], "artwork_url": null,
        "release_type": "album", "rating": 5, "rated_at": "2026-10-27T10:00:00.000Z"}]}
    """

    /// Answers the album with `rating`, a rating change with `ratingStatus`.
    private func answer(rating: String = "null", ratingStatus: Int = 204) {
        StubURLProtocol.handler = { request in
            let path = request.url!.path
            RatingsTests.seen.append(("\(request.httpMethod!) \(path)", Self.readJSONBody(request)))
            if path.hasSuffix("/rating") { return .init(status: ratingStatus, body: "") }
            if path == "/v1/ratings/albums" { return .init(body: Self.ratings) }
            return .init(body: Self.albumTracks(rating: rating))
        }
    }

    private func albumModel(_ api: APIClient) -> AlbumTracksViewModel {
        AlbumTracksViewModel(api: api, albumId: "ALB1", query: PeriodQuery(kind: .week), basePath: "/v1/albums")
    }

    func testTheRatingIsReadFromTheAlbumAndIsOptionalOnTheWire() async throws {
        let model = albumModel(makeAPI())
        answer(rating: "8")
        await model.load()
        XCTAssertEqual(model.rating, 8)

        // An older server sends no rating at all.
        let json = Self.albumTracks(rating: "null").replacingOccurrences(of: #", "rating": null"#, with: "")
        let decoded = try APIClient.makeDecoder().decode(AlbumTracks.self, from: Data(json.utf8))
        XCTAssertNil(decoded.album.rating)
    }

    func testRatingSendsPutWithTheNumberThenReloadsTheLists() async {
        let model = albumModel(makeAPI())
        var reloads = 0
        model.ratingChanged = { reloads += 1 }
        answer()
        await model.load()
        Self.seen = []

        await model.rate(7)

        XCTAssertEqual(model.rating, 7)
        XCTAssertEqual(Self.seen.map(\.0), ["PUT /v1/albums/ALB1/rating"])
        XCTAssertEqual(Self.seen[0].1, ["rating": 7])
        XCTAssertEqual(reloads, 1)
    }

    func testClearingSendsDelete() async {
        let model = albumModel(makeAPI())
        answer(rating: "8")
        await model.load()
        Self.seen = []

        await model.rate(nil)

        XCTAssertNil(model.rating)
        XCTAssertEqual(Self.seen.map(\.0), ["DELETE /v1/albums/ALB1/rating"])
    }

    func testTheSameRatingAgainSendsNothing() async {
        let model = albumModel(makeAPI())
        answer(rating: "8")
        await model.load()
        Self.seen = []

        await model.rate(8)

        XCTAssertTrue(Self.seen.isEmpty)
    }

    func testAFailedRatingGoesBackAndSaysWhy() async {
        let model = albumModel(makeAPI())
        var reloads = 0
        model.ratingChanged = { reloads += 1 }
        answer(rating: "4", ratingStatus: 500)
        await model.load()

        await model.rate(9)

        XCTAssertEqual(model.rating, 4)
        XCTAssertNotNil(model.ratingFailure)
        XCTAssertEqual(reloads, 0)
    }

    func testTheAllTimeListKeepsTheServersOrderAndAverages() async {
        let model = RatingsViewModel(api: makeAPI())
        answer()
        await model.load()

        XCTAssertTrue(model.isOffered)
        XCTAssertEqual(model.answer?.items.map(\.albumId), ["TOP", "ONE", "EP", "MID"])
        XCTAssertEqual(model.answer?.average, 7.75)
        XCTAssertEqual(
            Messages.ratingsSummary(count: 2, average: 7.5, locale: Locale(identifier: "en_US")),
            "2 rated · average 7.5")
    }

    /// Albums and singles & EPs are two tables; each keeps the server's order.
    func testAlbumsAndSinglesAreRankedApart() async throws {
        let model = RatingsViewModel(api: makeAPI())
        answer()
        await model.load()
        let answer = try XCTUnwrap(model.answer)

        XCTAssertEqual(answer.items(of: .albums).map(\.albumId), ["TOP", "MID"])
        XCTAssertEqual(answer.items(of: .singles).map(\.albumId), ["ONE", "EP"])
        XCTAssertEqual(RatedAlbums.average(answer.items(of: .albums)), 7.5)
    }

    /// Five leagues by score: 10, 9, 8, 6-7, 1-5.
    func testEveryScoreHasOneLeague() {
        XCTAssertEqual(
            (1...10).map { League(rating: $0).rawValue },
            [5, 5, 5, 5, 5, 4, 4, 3, 2, 1])
        XCTAssertEqual(League.allCases.map(\.scores), ["10", "9", "8", "6–7", "1–5"])
    }

    func testLeaguesListEveryLeagueInOrderWithItsReleases() async throws {
        let model = RatingsViewModel(api: makeAPI())
        answer()
        await model.load()
        let items = try XCTUnwrap(model.answer).items

        let leagues = RatedAlbums.leagues(items)

        XCTAssertEqual(leagues.map(\.league), [.one, .two, .three, .four, .five])
        XCTAssertEqual(leagues.map { $0.items.map(\.albumId) }, [["TOP"], ["ONE"], [], ["EP"], ["MID"]])
    }

    func testARatedRowSaysSingleOrEP() async throws {
        let model = RatingsViewModel(api: makeAPI())
        answer()
        await model.load()
        let items = try XCTUnwrap(model.answer).items

        XCTAssertEqual(items.map(\.releaseType.label), [nil, "Single", "EP", nil])
    }

    func testNoRatingsHidesTheEntryRow() async {
        let model = RatingsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: #"{"count": 0, "items": []}"#) }
        await model.load()
        XCTAssertFalse(model.isOffered)
    }

    func testTheSharePictureCarriesEachRatedAlbumsRating() throws {
        let json = """
        {"period": \(TimelineFixtures.period),
         "albums": {"count": 2, "items": [
            {"album_id": "A", "name": "Rated", "artists": ["X"],
             "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
             "tracks_played_in_period": 2, "tracks_listened": 8, "total_tracks": 10,
             "artwork_url": null, "rating": 9},
            {"album_id": "B", "name": "Unrated", "artists": ["Y"],
             "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
             "tracks_played_in_period": 2, "tracks_listened": 8, "total_tracks": 10,
             "artwork_url": null, "rating": null}]},
         "singles": {"count": 0, "items": []},
         "coverage_start_local_date": "2026-09-10", "period_contains_coverage_start": false,
         "nothing_new": false, "is_empty": false, "empty_reason": null}
        """
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(json.utf8))
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer))

        XCTAssertEqual(card.albums.map(\.rating), [9, nil])
        XCTAssertNotNil(ShareCard.png(card, covers: [nil, nil]))
    }

    // MARK: - Helper

    /// URLProtocol gets the body as a stream, not as `httpBody`.
    nonisolated private static func readJSONBody(_ request: URLRequest) -> NSDictionary? {
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return (try? JSONSerialization.jsonObject(with: data)) as? NSDictionary
    }
}

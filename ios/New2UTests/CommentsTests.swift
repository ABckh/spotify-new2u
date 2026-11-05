import XCTest
@testable import New2U

/// Album comments: decoding, writing, editing and clearing one from the
/// album screen (independent of the rating), the server's length rule,
/// and the comments in the all-time list.
@MainActor
final class CommentsTests: XCTestCase {
    /// Every request the stub saw, as "METHOD path?query", with its JSON body.
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

    static func albumTracks(rating: String = "null", comment: String = "null") -> String {
        """
        {"period": \(TimelineFixtures.period),
         "album": {"album_id": "ALB1", "name": "Album", "artists": ["Artist"],
                   "artwork_url": null, "rating": \(rating), "comment": \(comment)},
         "tracks": []}
        """
    }

    static let ratings = """
    {"count": 4, "items": [
       {"album_id": "TOP", "name": "Top", "artists": ["A"], "artwork_url": null,
        "release_type": "album", "rating": 10, "rated_at": "2026-10-28T10:00:00.000Z",
        "comment": "Best of the year", "commented_at": "2026-10-28T11:00:00.000Z"},
       {"album_id": "LOW", "name": "Low", "artists": ["B"], "artwork_url": null,
        "release_type": "album", "rating": 4, "rated_at": "2026-10-28T09:00:00.000Z",
        "comment": null, "commented_at": null},
       {"album_id": "WORDS", "name": "Words", "artists": ["C"], "artwork_url": null,
        "release_type": "album", "rating": null, "rated_at": null,
        "comment": "Not sure yet", "commented_at": "2026-10-29T10:00:00.000Z"},
       {"album_id": "EPW", "name": "EP Words", "artists": ["D"], "artwork_url": null,
        "release_type": "ep", "rating": null, "rated_at": null,
        "comment": "Short and good", "commented_at": "2026-10-27T10:00:00.000Z"}]}
    """

    /// Answers the album with `rating` and `comment`, a comment or rating
    /// change with `changeStatus`, and the all-time list with `ratings`.
    private func answer(rating: String = "null", comment: String = "null", changeStatus: Int = 204) {
        StubURLProtocol.handler = { request in
            let url = request.url!
            // No closure here: this runs off the main actor, and one formed
            // in this @MainActor class would be checked to run on it.
            var target = url.path
            if let query = url.query { target += "?" + query }
            CommentsTests.seen.append(("\(request.httpMethod!) \(target)", Self.readJSONBody(request)))
            if url.path.hasSuffix("/comment") || url.path.hasSuffix("/rating") {
                return .init(status: changeStatus, body: "")
            }
            if url.path == "/v1/ratings/albums" { return .init(body: Self.ratings) }
            return .init(body: Self.albumTracks(rating: rating, comment: comment))
        }
    }

    private func albumModel(_ api: APIClient) -> AlbumTracksViewModel {
        AlbumTracksViewModel(api: api, albumId: "ALB1", query: PeriodQuery(kind: .week), basePath: "/v1/albums")
    }

    // MARK: - The album screen

    func testTheCommentIsReadFromTheAlbumAndIsOptionalOnTheWire() async throws {
        let model = albumModel(makeAPI())
        answer(comment: #""Line one\nline two""#)
        await model.load()
        XCTAssertEqual(model.comment, "Line one\nline two")
        XCTAssertNil(model.rating)

        // An older server sends no comment at all.
        let json = Self.albumTracks().replacingOccurrences(of: #", "comment": null"#, with: "")
        let decoded = try APIClient.makeDecoder().decode(AlbumTracks.self, from: Data(json.utf8))
        XCTAssertNil(decoded.album.comment)
    }

    func testSavingSendsPutWithTheTrimmedTextThenReloadsTheLists() async {
        let model = albumModel(makeAPI())
        var reloads = 0
        model.ratingChanged = { reloads += 1 }
        answer(rating: "8")
        await model.load()
        Self.seen = []

        let saved = await model.saveComment("  Better than I remembered.\n\n")

        XCTAssertTrue(saved)
        XCTAssertEqual(model.comment, "Better than I remembered.")
        XCTAssertEqual(Self.seen.map(\.0), ["PUT /v1/albums/ALB1/comment"])
        XCTAssertEqual(Self.seen[0].1, ["comment": "Better than I remembered."])
        XCTAssertEqual(reloads, 1)
        // The rating is not touched.
        XCTAssertEqual(model.rating, 8)
    }

    func testABlankDraftClearsTheCommentWithDelete() async {
        let model = albumModel(makeAPI())
        answer(rating: "6", comment: #""Old words""#)
        await model.load()
        Self.seen = []

        let saved = await model.saveComment(" \n\t ")

        XCTAssertTrue(saved)
        XCTAssertNil(model.comment)
        XCTAssertEqual(Self.seen.map(\.0), ["DELETE /v1/albums/ALB1/comment"])
        XCTAssertEqual(model.rating, 6)
    }

    func testTheSameTextAgainSendsNothing() async {
        let model = albumModel(makeAPI())
        answer(comment: #""Same""#)
        await model.load()
        Self.seen = []

        let saved = await model.saveComment("Same  ")

        XCTAssertTrue(saved)
        XCTAssertTrue(Self.seen.isEmpty)
    }

    /// The comment changes only once the server has it, so the editor
    /// keeps the draft and says why.
    func testAFailedSaveKeepsTheCommentBeforeAndSaysWhy() async {
        let model = albumModel(makeAPI())
        var reloads = 0
        model.ratingChanged = { reloads += 1 }
        answer(comment: #""Before""#, changeStatus: 500)
        await model.load()

        let saved = await model.saveComment("After")

        XCTAssertFalse(saved)
        XCTAssertEqual(model.comment, "Before")
        XCTAssertNotNil(model.commentFailure)
        XCTAssertFalse(model.isSavingComment)
        XCTAssertEqual(reloads, 0)
        model.dismissCommentFailure()
        XCTAssertNil(model.commentFailure)
    }

    /// Rating and comment are independent: changing or clearing one
    /// sends only its own request and leaves the other as it was.
    func testRatingAndCommentNeverTouchEachOther() async {
        let model = albumModel(makeAPI())
        answer(rating: "7", comment: #""Words""#)
        await model.load()
        Self.seen = []

        await model.rate(nil)
        XCTAssertNil(model.rating)
        XCTAssertEqual(model.comment, "Words")

        await model.rate(9)
        await model.saveComment("")
        XCTAssertEqual(model.rating, 9)
        XCTAssertNil(model.comment)

        XCTAssertEqual(
            Self.seen.map(\.0),
            ["DELETE /v1/albums/ALB1/rating", "PUT /v1/albums/ALB1/rating", "DELETE /v1/albums/ALB1/comment"])
    }

    /// The server counts Unicode scalars, not characters, up to 2000.
    func testTheLengthIsCountedAsTheServerCountsIt() async {
        XCTAssertEqual(AlbumTracksViewModel.commentLength("👍🏽"), 2)
        XCTAssertEqual(AlbumTracksViewModel.commentLength("  abc \n"), 3)
        XCTAssertEqual(AlbumTracksViewModel.commentToSend("a\u{0}b"), "ab")
        XCTAssertNil(AlbumTracksViewModel.commentToSend("\u{0} \n"))

        let model = albumModel(makeAPI())
        answer()
        await model.load()
        Self.seen = []

        let tooLong = await model.saveComment(String(repeating: "x", count: Messages.commentMaxLength + 1))
        XCTAssertFalse(tooLong)
        XCTAssertTrue(Self.seen.isEmpty)

        let longest = String(repeating: "🎵", count: Messages.commentMaxLength)
        let saved = await model.saveComment(longest)
        XCTAssertTrue(saved)
        XCTAssertEqual(model.comment, longest)
    }

    // MARK: - Lists

    func testAlbumRowsCarryTheComment() throws {
        let json = """
        {"period": \(TimelineFixtures.period),
         "albums": {"count": 2, "items": [
            {"album_id": "A", "name": "With words", "artists": ["X"], "play_count": 3,
             "tracks_played_in_period": 2, "total_tracks": 3, "total_ms": 1000, "is_new": false,
             "artwork_url": null, "rating": null, "comment": "Words"},
            {"album_id": "B", "name": "Without", "artists": ["Y"], "play_count": 3,
             "tracks_played_in_period": 2, "total_tracks": 3, "total_ms": 1000, "is_new": false,
             "artwork_url": null, "rating": 5}]},
         "is_empty": false, "empty_reason": null}
        """
        let albums = try APIClient.makeDecoder().decode(PlayedAlbums.self, from: Data(json.utf8)).albums.items

        XCTAssertEqual(albums.map(\.comment), ["Words", nil])
        XCTAssertTrue(PlayedAlbumRow(album: albums[0]).spoken.hasSuffix(" With your comment."))
        XCTAssertFalse(PlayedAlbumRow(album: albums[1]).spoken.contains("comment"))
    }

    func testTheAllTimeListAsksForCommentsAndKeepsTheServersOrder() async throws {
        let model = RatingsViewModel(api: makeAPI())
        answer()
        await model.load()
        let answer = try XCTUnwrap(model.answer)

        XCTAssertEqual(Self.seen.map(\.0), ["GET /v1/ratings/albums?commented=true"])
        XCTAssertTrue(model.isOffered)
        XCTAssertEqual(answer.items.map(\.albumId), ["TOP", "LOW", "WORDS", "EPW"])
        XCTAssertEqual(answer.items.map(\.comment), ["Best of the year", nil, "Not sure yet", "Short and good"])
        XCTAssertEqual(answer.ratedCount, 2)
        XCTAssertEqual(answer.commentCount, 3)
        // The ones with only a comment count for no average and no league.
        XCTAssertEqual(answer.average, 7)
        XCTAssertEqual(
            RatedAlbums.leagues(answer.items).map { $0.items.map(\.albumId) }, [["TOP"], [], [], [], ["LOW"]])
        XCTAssertEqual(RatedAlbums.commentOnly(answer.items(of: .albums)).map(\.albumId), ["WORDS"])
        XCTAssertEqual(RatedAlbums.commentOnly(answer.items(of: .singles)).map(\.albumId), ["EPW"])
    }

    func testAListWithOnlyCommentsIsStillOffered() async throws {
        let model = RatingsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in
            .init(body: """
            {"count": 1, "items": [
               {"album_id": "WORDS", "name": "Words", "artists": [], "artwork_url": null,
                "release_type": "album", "rating": null, "rated_at": null,
                "comment": "Only words", "commented_at": "2026-10-29T10:00:00.000Z"}]}
            """)
        }
        await model.load()
        let answer = try XCTUnwrap(model.answer)

        XCTAssertTrue(model.isOffered)
        XCTAssertNil(answer.average)
        XCTAssertEqual(
            Messages.ratingsSummary(count: answer.ratedCount, average: answer.average, comments: answer.commentCount),
            "1 comment")
    }

    func testTheSummaryCountsRatingsAndComments() {
        let locale = Locale(identifier: "en_US")
        XCTAssertEqual(Messages.ratingsSummary(count: 0, average: nil, locale: locale), "0 rated")
        XCTAssertEqual(
            Messages.ratingsSummary(count: 2, average: 7.5, comments: 3, locale: locale),
            "2 rated · average 7.5 · 3 comments")
        XCTAssertEqual(Messages.ratingsSummary(count: 0, average: nil, comments: 2, locale: locale), "2 comments")
    }

    func testARatedRowReadsTheWholeComment() throws {
        let items = try APIClient.makeDecoder().decode(RatedAlbums.self, from: Data(Self.ratings.utf8)).items

        XCTAssertEqual(
            RatedAlbumRow(album: items[0]).spoken, "Top, by A. Rated 10 out of 10. Your comment: Best of the year")
        XCTAssertEqual(RatedAlbumRow(album: items[2]).spoken, "Words, by C. Your comment: Not sure yet")
        XCTAssertEqual(RatedAlbumRow(album: items[1]).spoken, "Low, by B. Rated 4 out of 10.")
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

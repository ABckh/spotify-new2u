import XCTest
@testable import New2U

/// Track comments, the singles list and the per-tab ratings: what the app
/// sends, and what each screen shows after a change.
@MainActor
final class TrackCommentsTests: XCTestCase {
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

    /// Answers every comment change with `status`, and records it.
    private func answerChanges(status: Int = 204) {
        StubURLProtocol.handler = { request in
            let path = request.url!.absoluteString.replacingOccurrences(of: "http://127.0.0.1:8000", with: "")
            Self.seen.append(("\(request.httpMethod!) \(path)", Self.readJSONBody(request)))
            return .init(status: status, body: "")
        }
    }

    // MARK: The comment of one track

    func testSavingSendsPutWithTheTextAndReportsIt() async {
        let model = TrackCommentModel(api: makeAPI(), itemKey: "id:AAA", title: "Low Tide", comment: nil)
        var reported: [String?] = []
        model.changed = { reported.append($0) }
        answerChanges()

        let saved = await model.saveComment("  The bridge at 2:10.\n")

        XCTAssertTrue(saved)
        XCTAssertEqual(Self.seen.map(\.0), ["PUT /v1/tracks/id:AAA/comment"])
        XCTAssertEqual(Self.seen[0].1, ["comment": "The bridge at 2:10."])
        XCTAssertEqual(model.comment, "The bridge at 2:10.")
        XCTAssertEqual(reported, ["The bridge at 2:10."])
    }

    func testABlankDraftClearsTheCommentWithDelete() async {
        let model = TrackCommentModel(api: makeAPI(), itemKey: "id:AAA", title: "Low Tide", comment: "Old words")
        var reported: [String?] = ["unset"]
        model.changed = { reported = [$0] }
        answerChanges()

        await model.saveComment("   ")

        XCTAssertEqual(Self.seen.map(\.0), ["DELETE /v1/tracks/id%3AAAA/comment"])
        XCTAssertNil(model.comment)
        XCTAssertEqual(reported, [nil])
    }

    func testTheSameTextSendsNothing() async {
        let model = TrackCommentModel(api: makeAPI(), itemKey: "id:AAA", title: "Low Tide", comment: "Same")
        answerChanges()

        let saved = await model.saveComment("Same ")

        XCTAssertTrue(saved)
        XCTAssertTrue(Self.seen.isEmpty)
    }

    func testAFailureKeepsTheOldCommentAndSaysWhy() async {
        let model = TrackCommentModel(api: makeAPI(), itemKey: "id:AAA", title: "Low Tide", comment: "Old words")
        var reported = 0
        model.changed = { _ in reported += 1 }
        answerChanges(status: 500)

        let saved = await model.saveComment("New words")

        XCTAssertFalse(saved)
        XCTAssertEqual(model.comment, "Old words")
        XCTAssertNotNil(model.commentFailure)
        XCTAssertEqual(reported, 0)
        model.dismissCommentFailure()
        XCTAssertNil(model.commentFailure)
    }

    // MARK: Where track comments show

    func testEveryLoadedPlayOfTheTrackShowsTheNewComment() async throws {
        let api = makeAPI()
        let timeline = TimelineViewModel(api: api)
        // Plays a0 and a1 are of tracks id:a0 and id:a1; make both id:a0.
        let page = TimelineFixtures.page(prefix: "a", count: 3, nextCursor: nil)
            .replacingOccurrences(of: "\"item_key\": \"id:a1\"", with: "\"item_key\": \"id:a0\"")
        StubURLProtocol.handler = { _ in .init(body: page) }
        await timeline.load()
        let showAll = timeline.makeShowAll()
        await showAll.load()

        let event = try XCTUnwrap(showAll.days.first?.events.first)
        let model = showAll.trackComment(event)
        answerChanges()
        await model.saveComment("Mine")

        for model in [timeline, showAll] {
            let comments = model.days.flatMap(\.events).map { "\($0.itemKey)=\($0.comment ?? "-")" }
            XCTAssertEqual(comments, ["id:a0=Mine", "id:a0=Mine", "id:a2=-"])
        }
    }

    func testTrackCommentsAreReadFromEveryAnswer() throws {
        let decoder = APIClient.makeDecoder()
        let tracks = try decoder.decode(AlbumTracks.self, from: Data("""
            {"period": \(TimelineFixtures.period),
             "album": {"album_id": "ALB1", "name": "Album", "artists": ["A"], "artwork_url": null},
             "tracks": [
               {"item_key": "id:AAA", "title": "One", "creators": ["A"], "play_count": 2,
                "is_new": false, "comment": "Words"},
               {"item_key": "id:BBB", "title": "Two", "creators": ["A"], "play_count": 1,
                "is_new": true, "comment": null}]}
            """.utf8))
        XCTAssertEqual(tracks.tracks.map(\.comment), ["Words", nil])

        let page = try decoder.decode(
            TimelinePage.self,
            from: Data(TimelineFixtures.page(prefix: "a", count: 1, nextCursor: nil).utf8))
        XCTAssertNil(page.events[0].comment)  // an older server sends none
    }

    func testAnAlbumTrackOpensItsOwnCommentAndReloadsTheAlbumAfterASave() async {
        let api = makeAPI()
        let album = AlbumTracksViewModel(api: api, albumId: "ALB1", query: PeriodQuery(kind: .week), basePath: "/v1/albums")
        var listsReloaded = 0
        album.ratingChanged = { listsReloaded += 1 }
        let track = AlbumTrack(itemKey: "id:AAA", title: "One", creators: ["A"], playCount: 2, isNew: false, comment: "Old")

        let model = album.trackComment(track)
        XCTAssertEqual(model.itemKey, "id:AAA")
        XCTAssertEqual(model.comment, "Old")

        StubURLProtocol.handler = { request in
            if request.httpMethod == "PUT" { return .init(status: 204, body: "") }
            return .init(body: RatingsTests.albumTracks(rating: "null"))
        }
        await model.saveComment("New")

        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/albums/ALB1/tracks")
        XCTAssertEqual(listsReloaded, 1)
    }

    // MARK: All singles & EPs, and the ratings of each tab

    func testTheSinglesListAsksForTheSinglesOfTheSamePeriod() async throws {
        let singles = AlbumsViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1), singles: true)
        let answer = AlbumsTests.example.replacingOccurrences(
            of: "\"is_new\": true", with: "\"is_new\": true, \"release_type\": \"ep\"")
        StubURLProtocol.handler = { _ in .init(body: answer) }

        await singles.load()
        await singles.makeShowAll().load()

        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/singles")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1&all=true")
        XCTAssertTrue(singles.answer?.albums.items.contains { $0.releaseType == .ep } ?? false)
    }

    func testEachTabOffersOnlyItsOwnRatingsWithItsOwnNumbers() async throws {
        let ratings = RatingsViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: RatingsTests.ratings) }
        await ratings.load()
        let answer = try XCTUnwrap(ratings.answer)

        XCTAssertTrue(ratings.isOffered(.albums))
        XCTAssertTrue(ratings.isOffered(.singles))
        // TOP 10 and MID 5 are albums; ONE 9 and EP 7 are singles and EPs.
        XCTAssertEqual(RatedAlbums.average(answer.items(of: .albums)), 7.5)
        XCTAssertEqual(RatedAlbums.average(answer.items(of: .singles)), 8)
        XCTAssertEqual(RatingsShelf.albums.title, "Your album ratings")
        XCTAssertEqual(RatingsShelf.singles.title, "Your singles & EPs ratings")

        let albumsOnly = RatingsTests.ratings
            .replacingOccurrences(of: "\"single\"", with: "\"album\"")
            .replacingOccurrences(of: "\"ep\"", with: "\"album\"")
        StubURLProtocol.handler = { _ in .init(body: albumsOnly) }
        await ratings.load()
        XCTAssertTrue(ratings.isOffered(.albums))
        XCTAssertFalse(ratings.isOffered(.singles))
    }

    // MARK: Helpers

    nonisolated static func readJSONBody(_ request: URLRequest) -> NSDictionary? {
        var data = request.httpBody
        if data == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var bytes = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                bytes.append(buffer, count: count)
            }
            data = bytes
        }
        guard let data, !data.isEmpty else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? NSDictionary
    }
}

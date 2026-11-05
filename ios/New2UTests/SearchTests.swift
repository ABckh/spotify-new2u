import XCTest
@testable import New2U

/// Search: decoding the answer, when the app asks the server and what it
/// sends, the album screen a found album opens, and the marked words.
@MainActor
final class SearchTests: XCTestCase {
    private func makeAPI() -> APIClient {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        addTeardownBlock { @MainActor in store.deleteToken() }
        return APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
    }

    private func makeModel() -> SearchViewModel {
        SearchViewModel(api: makeAPI(), debounce: .zero)
    }

    static let answer = """
    {"albums": {"count": 2, "items": [
       {"album_id": "ALB1", "name": "Harbour Lights", "artists": ["The Quiet Harbour"],
        "artwork_url": null, "release_type": "album", "rating": 9,
        "comment": "Best on a grey morning", "play_count": 42,
        "last_played_at": "2026-10-28T10:00:00.000Z", "last_played_local_date": "2026-10-28"},
       {"album_id": "GONE", "name": null, "artists": [], "artwork_url": null,
        "release_type": "album", "rating": null, "comment": "Words about it",
        "play_count": 0, "last_played_at": null, "last_played_local_date": null}]},
     "tracks": {"count": 1, "items": [
       {"item_key": "id:AAA", "title": "Paper Lanterns", "creators": ["The Quiet Harbour"],
        "album": "Harbour Lights", "album_id": null, "artwork_url": null, "play_count": 18,
        "first_played_at": "2024-03-02T09:00:00.000Z", "last_played_at": "2026-10-28T10:00:00.000Z",
        "last_played_local_date": "2026-10-28"}]}}
    """

    func testTheAnswerIsDecodedWithItsNulls() throws {
        let answer = try APIClient.makeDecoder().decode(SearchAnswer.self, from: Data(Self.answer.utf8))

        XCTAssertEqual(answer.albums.count, 2)
        XCTAssertEqual(answer.albums.items[0].comment, "Best on a grey morning")
        XCTAssertEqual(answer.albums.items[0].rating, 9)
        XCTAssertNil(answer.albums.items[1].name)
        XCTAssertNil(answer.albums.items[1].lastPlayedLocalDate)
        XCTAssertEqual(answer.tracks.items[0].album, "Harbour Lights")
        XCTAssertNil(answer.tracks.items[0].albumId)
        XCTAssertFalse(answer.isEmpty)
    }

    func testTheWordsAreSentAsOneQueryAndKeptForMarking() async throws {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer) }
        model.text = "  harbour \n  GREY "

        await model.search()

        let request = try XCTUnwrap(StubURLProtocol.lastRequest)
        XCTAssertEqual(request.url?.path, "/v1/search")
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query, [URLQueryItem(name: "q", value: "harbour GREY")])
        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.answeredWords, ["harbour", "GREY"])
        XCTAssertEqual(model.answer?.albums.items.map(\.albumId), ["ALB1", "GONE"])
    }

    func testABlankFieldClearsTheAnswerWithoutAsking() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer) }
        model.text = "harbour"
        await model.search()
        let asked = StubURLProtocol.requestCount

        model.text = "   "
        await model.search()

        XCTAssertEqual(StubURLProtocol.requestCount, asked)
        XCTAssertEqual(model.status, .idle)
        XCTAssertNil(model.answer)
        XCTAssertEqual(model.answeredWords, [])
    }

    func testMoreThanTheServerSearchesIsNotSent() async {
        let model = makeModel()
        model.text = Array(repeating: "word", count: 11).joined(separator: " ")
        await model.search()
        XCTAssertEqual(model.status, .tooLong)

        model.text = String(repeating: "x", count: 201)
        await model.search()
        XCTAssertEqual(model.status, .tooLong)
        XCTAssertEqual(StubURLProtocol.requestCount, 0)

        StubURLProtocol.handler = { _ in .init(body: Self.answer) }
        model.text = String(repeating: "x", count: 200)
        await model.search()
        XCTAssertEqual(model.status, .loaded)
    }

    func testASearchCancelledWhileWaitingNeverAsks() async {
        let model = SearchViewModel(api: makeAPI(), debounce: .seconds(5))
        model.text = "harbour"

        let typing = Task { await model.search() }
        typing.cancel()
        await typing.value

        XCTAssertEqual(StubURLProtocol.requestCount, 0)
    }

    func testAFailureSaysSoAndTryAgainAsksForTheSameWords() async throws {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(status: 500, body: "") }
        model.text = "harbour"
        await model.search()
        XCTAssertEqual(model.status, .failed(.server(status: 500)))

        StubURLProtocol.handler = { _ in .init(body: Self.answer) }
        model.text = "harbour lights"
        await model.reload()  // nothing answered yet: nothing to ask again
        XCTAssertEqual(model.status, .failed(.server(status: 500)))

        await model.search()
        StubURLProtocol.handler = { _ in .init(status: 500, body: "") }
        await model.reload()
        XCTAssertEqual(model.status, .failed(.server(status: 500)))
        let query = URLComponents(url: try XCTUnwrap(StubURLProtocol.lastRequest?.url), resolvingAgainstBaseURL: false)
        XCTAssertEqual(query?.queryItems?.first?.value, "harbour lights")
    }

    func testAFoundAlbumOpensTheYearEndingWithItsLastPlay() async {
        let model = makeModel()
        var reloads = 0
        model.reloadAfterRating = { reloads += 1 }

        let tracks = model.makeAlbumTracks(albumId: "ALB1", lastPlayedLocalDate: "2026-10-28")

        XCTAssertEqual(tracks.albumId, "ALB1")
        XCTAssertEqual(tracks.basePath, "/v1/albums")
        XCTAssertEqual(tracks.query, .custom(from: "2025-10-28", to: "2026-10-28"))
        await tracks.ratingChanged()
        XCTAssertEqual(reloads, 1)
    }

    func testTheYearBeforeIsThe366DaysTheServerTakes() {
        XCTAssertEqual(SearchViewModel.yearBefore("2026-10-05"), "2025-10-05")
        // Over a 29 February: still 366 days, both ends included.
        XCTAssertEqual(SearchViewModel.yearBefore("2024-10-05"), "2023-10-06")
        XCTAssertEqual(SearchViewModel.yearBefore("2024-02-29"), "2023-03-01")
        XCTAssertNil(SearchViewModel.yearBefore("not a date"))
    }

    func testEveryPlaceAWordOccursIsMarkedIgnoringCase() {
        let text = "Harbour by the harbour"
        let ranges = SearchHighlight.ranges(of: ["HARBOUR", "the"], in: text)

        XCTAssertEqual(ranges.map { String(text[$0]) }, ["Harbour", "harbour", "the"])
        XCTAssertEqual(SearchHighlight.ranges(of: ["", "zither"], in: text), [])
        let marked = SearchHighlight.marked(text, words: ["by"])
        XCTAssertEqual(String(marked.characters), text)
    }

    func testThePlaysLine() {
        let utc = TimeZone(identifier: "UTC")!
        XCTAssertEqual(Messages.searchPlays(0, lastPlayedLocalDate: nil, timeZone: utc), "No plays")
        XCTAssertTrue(Messages.searchPlays(12, lastPlayedLocalDate: "2024-11-30", timeZone: utc)
            .hasPrefix("12 plays · last "))
        XCTAssertEqual(Messages.searchMore(shown: 50, count: 132), "50 of 132. Add a word to narrow it down.")
    }
}

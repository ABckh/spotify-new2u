import XCTest
@testable import New2U

/// "Not new" (WHO-123): the request, hiding the row at once, putting it
/// back on a failure, and Undo.
@MainActor
final class NotNewTests: XCTestCase {
    private var store: SessionStore!
    /// Every request the stub saw, as "METHOD path". Static, because
    /// the stub answers off the main thread, like `StubURLProtocol.lastRequest`.
    nonisolated(unsafe) private static var seen: [String] = []
    private var seen: [String] {
        get { Self.seen }
        set { Self.seen = newValue }
    }

    private func makeAPI() -> APIClient {
        StubURLProtocol.reset()
        seen = []
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        self.store = store
        addTeardownBlock { @MainActor in store.deleteToken() }
        return APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
    }

    /// Answers the list with the contract example and a mark with 204.
    /// `markStub` replaces the 204, to make the mark fail.
    private func answerWith(markStub: StubURLProtocol.Stub? = nil) {
        StubURLProtocol.handler = { request in
            let path = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.percentEncodedPath
            NotNewTests.seen.append("\(request.httpMethod!) \(path)")
            if path.hasPrefix("/v1/new-to-you/not-new/") {
                return markStub ?? .init(status: 204, body: "")
            }
            return .init(body: NewToYouTests.contractExample)
        }
    }

    private let album = NotNewMark(kind: .album, itemId: "4aawyAB9vmqN3uQ7FjRGTy", name: "Album")

    func testPathPartsArePercentEncodedOneByOne() {
        // "/" in a name must not split it into two path parts.
        XCTAssertEqual(APIClient.encodePathSegment("AC/DC"), "AC%2FDC")
        XCTAssertEqual(APIClient.encodePathSegment("Sigur Rós?#"), "Sigur%20R%C3%B3s%3F%23")
        XCTAssertEqual(APIClient.encodePathSegment("id:AAA"), "id%3AAAA")
    }

    func testMarkSendsPutThenReloadsTheList() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()
        seen = []

        await model.marker.mark(NotNewMark(kind: .track, itemId: "id:AAA", name: "Track"))

        XCTAssertEqual(seen, ["PUT /v1/new-to-you/not-new/track/id%3AAAA", "GET /v1/new-to-you"])
        XCTAssertEqual(StubURLProtocol.lastRequest?.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-token")
        XCTAssertTrue(model.marker.hidden.isEmpty)
        XCTAssertNotNil(model.marker.undoable)
        XCTAssertNil(model.marker.failure)
    }

    func testTheRowIsHiddenAndTheCountLoweredAtOnce() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()

        // Started, not awaited: what shows before the server answers.
        let marking = Task { await model.marker.mark(album) }
        await Task.yield()
        XCTAssertEqual(model.shown?.albums.items.map(\.albumId), ["zzz"])
        XCTAssertEqual(model.shown?.albums.count, 3)
        XCTAssertEqual(model.shown?.singles.count, 2)
        await marking.value
    }

    func testHidingEveryNewItemShowsNothingNew() async throws {
        let answer = try APIClient.makeDecoder().decode(NewToYou.self, from: Data(NewToYouTests.contractExample.utf8))
        let one = NewToYou(
            period: answer.period,
            albums: NewAlbums(count: 1, items: [answer.albums.items[0]]),
            singles: NewAlbums(count: 1, items: answer.singles.items),
            coverageStartLocalDate: nil, periodContainsCoverageStart: false,
            nothingNew: false, isEmpty: false, emptyReason: nil)
        let hidden = one.hiding([answer.albums.items[0].notNewMark, answer.singles.items[0].notNewMark])
        XCTAssertTrue(hidden.showsNothingNew)
    }

    func testAFailedMarkPutsTheRowBackAndSaysWhy() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()
        answerWith(markStub: .init(error: URLError(.notConnectedToInternet)))

        await model.marker.mark(album)

        XCTAssertEqual(model.shown?.albums.items.map(\.albumId), ["4aawyAB9vmqN3uQ7FjRGTy", "zzz"])
        XCTAssertEqual(model.shown?.albums.count, 4)
        XCTAssertNil(model.marker.undoable)
        XCTAssertEqual(
            model.marker.failure,
            "Couldn't mark \u{201C}Album\u{201D} as not new. You're offline. Connect to the internet, then try again.")
        // No reload after a failure.
        XCTAssertEqual(seen.last, "PUT /v1/new-to-you/not-new/album/4aawyAB9vmqN3uQ7FjRGTy")
    }

    func testUndoSendsDeleteAfterThePutAndReloads() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()
        seen = []

        // Undo tapped before the mark has finished: it still goes second.
        let marking = Task { await model.marker.mark(album) }
        await Task.yield()
        await model.marker.undo()
        await marking.value

        XCTAssertEqual(seen, [
            "PUT /v1/new-to-you/not-new/album/4aawyAB9vmqN3uQ7FjRGTy",
            "GET /v1/new-to-you",
            "DELETE /v1/new-to-you/not-new/album/4aawyAB9vmqN3uQ7FjRGTy",
            "GET /v1/new-to-you",
        ])
        XCTAssertNil(model.marker.undoable)
        XCTAssertTrue(model.marker.hidden.isEmpty)
        XCTAssertEqual(model.shown?.albums.count, 4)
    }

    func testAFailedUndoSaysTheMarkStays() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()
        await model.marker.mark(album)
        answerWith(markStub: .init(status: 500, body: ""))

        await model.marker.undo()

        XCTAssertEqual(
            model.marker.failure,
            "Couldn't undo \u{201C}Album\u{201D}. It is still marked as not new. The server couldn't answer. Try again in a moment.")
    }

    func testAMarkWithAnExpiredSessionEndsTheSession() async {
        let model = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        await model.load()
        answerWith(markStub: .init(status: 401, body: #"{"detail": "Unauthorized"}"#))

        await model.marker.mark(album)

        // RootView shows the connect screen once the token is gone.
        XCTAssertFalse(store.hasToken)
        XCTAssertTrue(model.marker.hidden.isEmpty)
    }

    func testAMarkInShowAllReloadsTheSectionToo() async {
        let section = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        answerWith()
        let showAll = section.makeShowAll()
        await showAll.load()
        seen = []

        await showAll.marker.mark(album)

        XCTAssertEqual(seen.filter { $0.hasPrefix("GET") }.count, 2)
        XCTAssertEqual(section.status, .loaded)
    }

    func testASongMarkDropsItsNewTagAtOnce() async {
        let section = NewToYouViewModel(api: makeAPI(), query: PeriodQuery(kind: .week))
        StubURLProtocol.handler = { request in
            if request.httpMethod == "PUT" { return .init(status: 204, body: "") }
            return .init(body: NewToYouTests.albumExample)
        }
        let model = section.makeAlbumTracks(albumId: "4aawyAB9vmqN3uQ7FjRGTy")
        await model.load()
        let zebra = model.answer!.tracks[0]
        XCTAssertTrue(model.showsNew(zebra))

        let marking = Task { await model.marker.mark(zebra.notNewMark) }
        await Task.yield()
        XCTAssertFalse(model.showsNew(zebra))
        await marking.value
        XCTAssertEqual(zebra.notNewMark.pathSegments.last, "id:BBB")
    }
}

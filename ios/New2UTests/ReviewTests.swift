import XCTest
@testable import New2U

/// "Did you already know these?": loading the review, ticking, and
/// finishing it with one batch of marks and "Done".
@MainActor
final class ReviewTests: XCTestCase {
    /// Every request the stub saw, as "METHOD path", with its JSON body.
    /// Static, because the stub answers off the main thread.
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

    static let example = """
    {"open": true, "window_start": "2026-10-04T10:00:00.000Z",
     "window_end": "2026-10-19T09:00:00.000Z", "through": "2026-10-11T12:00:00.123Z",
     "albums": {"count": 1, "items": [
        {"album_id": "4aawyAB9vmqN3uQ7FjRGTy", "name": "Album", "artists": ["Artist"],
         "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
         "tracks_played_in_period": 8, "tracks_listened": 11, "total_tracks": 16, "artwork_url": null}]},
     "singles": {"count": 2, "items": [
        {"album_id": "SINGLE1", "name": "One Song", "artists": ["AC/DC"],
         "first_played_at": "2026-10-06T10:00:00.000Z", "first_played_local_date": "2026-10-06",
         "tracks_played_in_period": 1, "tracks_listened": 1, "total_tracks": 1, "artwork_url": null,
         "release_type": "single"},
        {"album_id": "EP1", "name": "Four Songs", "artists": ["Nova Kett"],
         "first_played_at": "2026-10-07T10:00:00.000Z", "first_played_local_date": "2026-10-07",
         "tracks_played_in_period": 3, "tracks_listened": 3, "total_tracks": 4, "artwork_url": null,
         "release_type": "ep"}]}}
    """

    static let nothingLeft = """
    {"open": true, "window_start": null, "window_end": "2026-10-19T09:00:00.000Z",
     "through": "2026-10-11T12:00:00.123Z",
     "albums": {"count": 0, "items": []}, "singles": {"count": 0, "items": []}}
    """

    /// Answers the review with `review`, the batch with its counts and
    /// "Done" with 204. `failing` answers that path with a 500 instead.
    private func answer(review: String = example, failing: String? = nil) {
        StubURLProtocol.handler = { request in
            let path = request.url!.path
            ReviewTests.seen.append(("\(request.httpMethod!) \(path)", Self.readJSONBody(request)))
            if path == failing { return .init(status: 500, body: "{}") }
            switch path {
            case "/v1/new-to-you/not-new":
                return .init(body: #"{"marked": 2, "not_played": 0}"#)
            case "/v1/me/review/done":
                return .init(status: 204, body: "")
            default:
                return .init(body: review)
            }
        }
    }

    func testTheOfferShowsOnlyWhileSomethingIsLeft() async {
        let model = ReviewViewModel(api: makeAPI())
        answer()
        await model.load()
        XCTAssertTrue(model.isOffered)
        XCTAssertEqual(model.review?.count, 3)

        answer(review: Self.nothingLeft)
        await model.load()
        XCTAssertFalse(model.isOffered)

        answer(review: Self.example.replacingOccurrences(of: #""open": true"#, with: #""open": false"#))
        await model.load()
        XCTAssertFalse(model.isOffered)
    }

    func testAFailedLoadHidesTheOffer() async {
        let model = ReviewViewModel(api: makeAPI())
        answer(failing: "/v1/me/review")
        await model.load()
        XCTAssertFalse(model.isOffered)
        XCTAssertEqual(model.status, .failed(.server(status: 500)))
    }

    func testTickingSendsNothingAndSelectAllTogglesOneKind() async {
        let model = ReviewViewModel(api: makeAPI())
        answer()
        await model.load()
        Self.seen = []

        model.toggleAll(.singles)
        XCTAssertTrue(model.allSelected(.singles))
        XCTAssertFalse(model.allSelected(.albums))
        XCTAssertEqual(model.selected.count, 2)
        model.toggleAll(.singles)
        XCTAssertTrue(model.selected.isEmpty)

        let album = model.review!.albums.items[0].notNewMark
        model.toggle(album)
        XCTAssertTrue(model.isSelected(album))
        model.toggle(album)
        XCTAssertFalse(model.isSelected(album))
        XCTAssertTrue(Self.seen.isEmpty)
    }

    func testFinishMarksTheTickedItemsThenSendsDoneWithTheServersThrough() async {
        let model = ReviewViewModel(api: makeAPI())
        var finishedRuns = 0
        model.finished = { finishedRuns += 1 }
        answer()
        await model.load()
        model.toggle(model.review!.albums.items[0].notNewMark)
        model.toggle(model.review!.singles.items[0].notNewMark)
        Self.seen = []

        let saved = await model.finish()

        XCTAssertTrue(saved)
        XCTAssertEqual(
            Self.seen.map(\.0),
            ["POST /v1/new-to-you/not-new", "POST /v1/me/review/done", "GET /v1/me/review"])
        let items = Self.seen[0].1?["items"] as? [NSDictionary]
        XCTAssertEqual(items, [
            ["kind": "album", "item_id": "4aawyAB9vmqN3uQ7FjRGTy"],
            ["kind": "album", "item_id": "SINGLE1"],
        ])
        XCTAssertEqual(Self.seen[1].1, ["through": "2026-10-11T12:00:00.123Z"])
        XCTAssertTrue(model.selected.isEmpty)
        XCTAssertEqual(finishedRuns, 1)
    }

    func testFinishWithNothingTickedOnlySendsDone() async {
        let model = ReviewViewModel(api: makeAPI())
        answer()
        await model.load()
        Self.seen = []

        let saved = await model.finish()

        XCTAssertTrue(saved)
        XCTAssertEqual(Self.seen.map(\.0), ["POST /v1/me/review/done", "GET /v1/me/review"])
    }

    func testAFailedSaveKeepsTheTicksAndDoesNotFinish() async {
        let model = ReviewViewModel(api: makeAPI())
        var finishedRuns = 0
        model.finished = { finishedRuns += 1 }
        answer(failing: "/v1/me/review/done")
        await model.load()
        let single = model.review!.singles.items[0].notNewMark
        model.toggle(single)

        let saved = await model.finish()

        XCTAssertFalse(saved)
        XCTAssertEqual(model.status, .saveFailed(.server(status: 500)))
        XCTAssertTrue(model.isSelected(single))
        XCTAssertEqual(finishedRuns, 0)
    }

    func testMoreThanTwoHundredTicksGoInSeveralRequests() async throws {
        let singles = (0..<450).map {
            #"{"album_id": "S\#($0)", "name": "Single \#($0)", "artists": ["Artist"], "#
                + #""first_played_at": "2026-10-06T10:00:00.000Z", "first_played_local_date": "2026-10-06", "#
                + #""tracks_played_in_period": 1, "tracks_listened": 1, "total_tracks": 1, "#
                + #""artwork_url": null, "release_type": "single"}"#
        }
        let review = #"{"open": true, "through": "2026-10-11T12:00:00.000Z", "#
            + #""albums": {"count": 0, "items": []}, "#
            + #""singles": {"count": 450, "items": [\#(singles.joined(separator: ","))]}}"#
        let model = ReviewViewModel(api: makeAPI())
        answer(review: review)
        await model.load()
        model.toggleAll(.singles)
        Self.seen = []

        _ = await model.finish()

        let batches = Self.seen.filter { $0.0 == "POST /v1/new-to-you/not-new" }
        XCTAssertEqual(batches.map { ($0.1?["items"] as? [Any])?.count }, [200, 200, 50])
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

import XCTest
@testable import New2U

/// The summary header's data, and the custom-range check.
@MainActor
final class SummaryTests: XCTestCase {
    private var store: SessionStore!

    /// The example from api-contract section 3.2, with W-5's numbers.
    static let contractExample = """
    {"period": \(TimelineFixtures.period),
     "totals": {"play_count": 6, "distinct_tracks": 5, "distinct_artists": 3,
                "total_ms": 1100000, "events_without_duration": 1},
     "top_tracks": [{"item_key": "id:AAA", "title": "Track", "creators": ["Artist"],
                     "play_count": 2, "total_ms": 400000,
                     "artwork_url": "https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228"}],
     "top_artists": [{"name": "Artist", "play_count": 3, "total_ms": 600000}],
     "is_empty": false, "empty_reason": null,
     "coverage_start": "2026-10-01T06:12:45.000Z"}
    """

    @MainActor
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

    func testDecodesTheContractExample() throws {
        let summary = try APIClient.makeDecoder().decode(Summary.self, from: Data(Self.contractExample.utf8))
        XCTAssertEqual(summary.totals.playCount, 6)
        XCTAssertEqual(summary.totals.totalMs, 1_100_000)
        XCTAssertEqual(summary.totals.eventsWithoutDuration, 1)
        XCTAssertEqual(summary.topArtists.first?.name, "Artist")
        XCTAssertEqual(summary.topTracks.first?.itemKey, "id:AAA")
        XCTAssertEqual(summary.period.timeZone, "Europe/Warsaw")
    }

    @MainActor
    func testLoadSendsThePeriodAndAsksForTheTopOne() async {
        let model = SummaryViewModel(api: makeAPI(), query: PeriodQuery(kind: .month, offset: -1))
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }

        await model.load()

        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.summary?.totals.playCount, 6)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/summary")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=month&offset=-1&top=1")
    }

    /// A failure drops the old numbers, so they never sit under another
    /// period's dates.
    @MainActor
    func testFailureDropsTheOldNumbers() async {
        let model = SummaryViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertNil(model.summary)
    }

    @MainActor
    func testExpiredSessionEndsTheSession() async {
        let model = SummaryViewModel(api: makeAPI())
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
    }

    // MARK: - Custom range (R-22, R-23)

    @MainActor
    func testAcceptedRangeReturnsTheQueryInTheDisplayZone() async throws {
        let warsaw = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        // 00:30 on 13 April in Warsaw is still 12 April in UTC.
        let from = try XCTUnwrap(APIClient.parseInstant("2026-04-12T22:30:00.000Z"))
        let to = try XCTUnwrap(APIClient.parseInstant("2026-04-19T10:00:00.000Z"))
        let range = CustomRangeViewModel(api: makeAPI(), from: from, to: to, timeZone: warsaw)
        StubURLProtocol.handler = { _ in .init(body: Self.contractExample) }

        let accepted = await range.check()

        XCTAssertEqual(accepted, .custom(from: "2026-04-13", to: "2026-04-19"))
        XCTAssertNil(range.message)
        XCTAssertEqual(
            StubURLProtocol.lastRequest?.url?.query, "period=custom&from=2026-04-13&to=2026-04-19&top=1")
    }

    /// T-22: on a 422 the sheet keeps the user's dates and says why.
    @MainActor
    func testRefusedRangeKeepsTheDatesAndSaysWhy() async throws {
        let warsaw = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        let from = try XCTUnwrap(APIClient.parseInstant("2026-04-19T10:00:00.000Z"))
        let to = try XCTUnwrap(APIClient.parseInstant("2026-04-13T10:00:00.000Z"))
        let range = CustomRangeViewModel(api: makeAPI(), from: from, to: to, timeZone: warsaw)
        StubURLProtocol.handler = { _ in
            .init(status: 422, body: #"{"detail": {"code": "period_range_reversed", "message": "x"}}"#)
        }

        let accepted = await range.check()

        XCTAssertNil(accepted)
        XCTAssertEqual(range.message, "The end date is before the start date.")
        XCTAssertEqual(range.from, from)
        XCTAssertEqual(range.to, to)
        XCTAssertFalse(range.isChecking)
    }

    @MainActor
    func testOfflineRangeCheckSaysOffline() async throws {
        let range = CustomRangeViewModel(api: makeAPI(), from: Date(), to: Date(), timeZone: .current)
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        let accepted = await range.check()
        XCTAssertNil(accepted)
        XCTAssertEqual(range.message, "You're offline. Connect to the internet, then try again.")
    }
}

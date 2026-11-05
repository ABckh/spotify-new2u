import XCTest
@testable import New2U

final class TimelineViewModelTests: XCTestCase {
    private var store: SessionStore!
    private var model: TimelineViewModel!

    @MainActor
    private func makeModel() {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        self.store = store
        let api = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        model = TimelineViewModel(api: api, query: PeriodQuery(kind: .week, offset: -1))
        addTeardownBlock { @MainActor in store.deleteToken() }
    }

    @MainActor
    func testFirstPageShowsDaysAndSendsPeriod() async throws {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.contractExample) }

        await model.load()

        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.days.map(\.localDate), ["2026-10-11"])
        XCTAssertEqual(model.displayTimeZone.identifier, "Europe/Warsaw")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=week&offset=-1&limit=50")
    }

    @MainActor
    func testEmptyPeriodKeepsTheReason() async {
        makeModel()
        StubURLProtocol.handler = { _ in
            .init(body: TimelineFixtures.page(prefix: "e", count: 0, nextCursor: nil))
        }
        await model.load()
        XCTAssertEqual(model.status, .empty(reason: "no_plays"))
    }

    @MainActor
    func testOfflineWithNothingOnScreenFails() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
    }

    @MainActor
    func testExpiredSessionEndsTheSession() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
        XCTAssertTrue(store.sessionExpired)
    }

    /// A failed refresh keeps the rows and shows a banner instead.
    @MainActor
    func testFailedRefreshKeepsRows() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.contractExample) }
        await model.load()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .loaded)
        XCTAssertEqual(model.days.count, 1)
        XCTAssertEqual(model.bannerError, .offline)
    }

    @MainActor
    func testNextPageSendsCursorAndAppends() async throws {
        makeModel()
        StubURLProtocol.handler = { request in
            let isSecond = request.url?.query?.contains("cursor=c1") ?? false
            return .init(body: isSecond
                ? TimelineFixtures.page(prefix: "b", count: 2, nextCursor: nil)
                : TimelineFixtures.page(prefix: "a", count: 20, nextCursor: "c1"))
        }
        await model.load()
        XCTAssertNil(model.days.last?.totalMs)

        let lastRow = try XCTUnwrap(model.days.last?.events.last)
        await model.loadMoreIfNeeded(after: lastRow)

        XCTAssertEqual(model.days.flatMap(\.events).count, 22)
        XCTAssertEqual(model.days.last?.totalMs, 22 * 60_000)
        XCTAssertEqual(
            StubURLProtocol.lastRequest?.url?.query, "period=week&offset=-1&limit=50&cursor=c1")
    }

    /// Rows far from the end do not start a page request.
    @MainActor
    func testNoPrefetchFarFromTheEnd() async throws {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.page(prefix: "a", count: 40, nextCursor: "c1")) }
        await model.load()
        let firstRow = try XCTUnwrap(model.days.first?.events.first)
        await model.loadMoreIfNeeded(after: firstRow)
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
    }

    /// api-contract 3.1: on invalid_cursor, reload from the first page.
    @MainActor
    func testInvalidCursorReloadsFirstPage() async throws {
        makeModel()
        StubURLProtocol.handler = { request in
            if request.url?.query?.contains("cursor=") ?? false {
                return .init(status: 422, body: #"{"detail": {"code": "invalid_cursor", "message": "x"}}"#)
            }
            return .init(body: TimelineFixtures.page(prefix: "a", count: 5, nextCursor: "c1"))
        }
        await model.load()
        let lastRow = try XCTUnwrap(model.days.last?.events.last)
        await model.loadMoreIfNeeded(after: lastRow)

        XCTAssertEqual(StubURLProtocol.requestCount, 3)
        XCTAssertFalse(StubURLProtocol.lastRequest?.url?.query?.contains("cursor=") ?? true)
        XCTAssertEqual(model.status, .loaded)
        XCTAssertNil(model.bannerError)
    }

    // MARK: - Main screen preview and "Show all" (WHO-131)

    /// Exactly 10 plays: all of them on the main screen, no "Show all".
    @MainActor
    func testTenPlaysShowNoShowAll() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.page(prefix: "a", count: 10, nextCursor: nil)) }
        await model.load()
        XCTAssertEqual(model.previewDays.flatMap(\.events).count, 10)
        XCTAssertFalse(model.hasMoreThanPreview)
        XCTAssertEqual(model.previewDays.last?.totalMs, 10 * 60_000)
    }

    /// 11 plays: the first 10 on the main screen, newest first, and "Show all".
    @MainActor
    func testElevenPlaysShowTenAndShowAll() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.page(prefix: "a", count: 11, nextCursor: nil)) }
        await model.load()
        XCTAssertEqual(model.previewDays.flatMap(\.events).map(\.id), (0..<10).map { "a\($0)" })
        XCTAssertTrue(model.hasMoreThanPreview)
        // The day goes on past the 10th play, so its header has no total yet.
        XCTAssertNil(model.previewDays.last?.totalMs)
    }

    /// Fewer than 10 loaded but another page waiting still means "Show all".
    @MainActor
    func testNextPageMeansShowAll() async {
        makeModel()
        StubURLProtocol.handler = { _ in .init(body: TimelineFixtures.page(prefix: "a", count: 4, nextCursor: "c1")) }
        await model.load()
        XCTAssertEqual(model.previewDays.flatMap(\.events).count, 4)
        XCTAssertTrue(model.hasMoreThanPreview)
    }

    /// The preview spans days in the server's order.
    @MainActor
    func testPreviewKeepsDaysInOrder() async {
        makeModel()
        let body = TimelineFixtures.page(
            days: [("a", 6, "2026-10-11"), ("b", 6, "2026-10-10")], nextCursor: nil)
        StubURLProtocol.handler = { _ in .init(body: body) }
        await model.load()
        XCTAssertEqual(model.previewDays.map(\.localDate), ["2026-10-11", "2026-10-10"])
        XCTAssertEqual(model.previewDays.map(\.events.count), [6, 4])
        XCTAssertEqual(model.previewDays.first?.totalMs, 6 * 60_000)
    }

    /// "Show all" asks for the same period, and its next page sends the cursor.
    @MainActor
    func testShowAllAsksTheSamePeriodAndPages() async throws {
        makeModel()
        StubURLProtocol.handler = { request in
            let isSecond = request.url?.query?.contains("cursor=c1") ?? false
            return .init(body: isSecond
                ? TimelineFixtures.page(prefix: "b", count: 2, nextCursor: nil)
                : TimelineFixtures.page(prefix: "a", count: 20, nextCursor: "c1"))
        }
        let all = model.makeShowAll()
        await all.load()
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/timeline")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=week&offset=-1&limit=50")
        XCTAssertEqual(all.days.flatMap(\.events).count, 20)

        let lastRow = try XCTUnwrap(all.days.last?.events.last)
        await all.loadMoreIfNeeded(after: lastRow)
        XCTAssertEqual(
            StubURLProtocol.lastRequest?.url?.query, "period=week&offset=-1&limit=50&cursor=c1")
        XCTAssertEqual(all.days.flatMap(\.events).count, 22)
        // The main screen's model did not load anything.
        XCTAssertTrue(model.days.isEmpty)
    }
}

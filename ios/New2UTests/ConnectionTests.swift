import XCTest
@testable import New2U

/// The Spotify connection check behind the banner (api-contract 3.6).
@MainActor
final class ConnectionTests: XCTestCase {
    private var store: SessionStore!

    /// The example from api-contract section 3.6.
    /// `lastPolledAt: nil` is a new connection the server has not polled yet.
    nonisolated static func answer(
        status: String = "active", reconnectSoon: Bool = false,
        lastPolledAt: String? = "2026-10-11T11:45:02.123456Z"
    ) -> String {
        """
        {"status": "\(status)", "reauth_reason": null,
         "spotify_account_id": "synthetic", "spotify_display_name": "Synthetic Listener",
         "collection_started_at": "2026-10-01T06:00:00Z",
         "authorized_at": "2026-10-01T06:00:00Z",
         "grant_expires_at": "2027-03-30T06:00:00Z",
         "reconnect_soon": \(reconnectSoon),
         "last_polled_at": \(lastPolledAt.map { "\"\($0)\"" } ?? "null"),
         "poll_interval_seconds": 900}
        """
    }

    private func makeModel() -> ConnectionViewModel {
        let model = ConnectionViewModel(api: makeAPI())
        model.firstPollCheckGap = .zero
        return model
    }

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
        let connection = try APIClient.makeDecoder().decode(Connection.self, from: Data(Self.answer().utf8))
        XCTAssertTrue(connection.isActive)
        XCTAssertEqual(connection.spotifyDisplayName, "Synthetic Listener")
        XCTAssertEqual(connection.grantExpiresAt, APIClient.parseInstant("2027-03-30T06:00:00Z"))
        XCTAssertNotNil(connection.lastPolledAt, "six fractional digits must parse")
    }

    func testActiveConnectionShowsNoBanner() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer()) }
        await model.load()
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/me/connection")
        XCTAssertEqual(StubURLProtocol.lastRequest?.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-token")
        XCTAssertNil(model.notice)
    }

    /// The revoked Spotify token case.
    func testReauthRequiredIsRevoked() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer(status: "reauth_required", reconnectSoon: true)) }
        await model.load()
        XCTAssertEqual(model.notice, .revoked)
    }

    /// A status this app does not know yet must not read as fine.
    func testUnknownStatusIsRevoked() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer(status: "paused")) }
        await model.load()
        XCTAssertEqual(model.notice, .revoked)
    }

    func testReconnectSoonCarriesTheDeadline() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer(reconnectSoon: true)) }
        await model.load()
        XCTAssertEqual(model.notice, .reconnectSoon(by: APIClient.parseInstant("2027-03-30T06:00:00Z")))
    }

    func testNotFoundIsNotConnected() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(status: 404, body: #"{"detail": "Not Found"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .notConnected)
        XCTAssertEqual(model.notice, .notConnected)
    }

    /// Offline: the timeline already says so, so the banner stays hidden.
    func testOfflineShowsNoBanner() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertNil(model.notice)
    }

    func testExpiredSessionEndsTheSession() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(status: 401, body: #"{"detail": "Unauthorized"}"#) }
        await model.load()
        XCTAssertEqual(model.status, .failed(.sessionExpired))
        XCTAssertFalse(store.hasToken)
        XCTAssertTrue(store.sessionExpired)
    }

    // MARK: - First poll after sign-in (WHO-192)

    /// The paths the stub was asked for, in order. The stub's handler runs
    /// on the URL loading thread, so the list sits behind a lock.
    private final class RequestLog: @unchecked Sendable {
        private let lock = NSLock()
        private var paths: [String] = []
        func add(_ path: String) { lock.withLock { paths.append(path) } }
        func count(_ path: String) -> Int { lock.withLock { paths.filter { $0 == path }.count } }
    }

    /// The bug: a new user saw an empty timeline until "Sync now". The
    /// first answer says "not polled yet", the second has a poll time:
    /// the timeline reloads once, and no poll request is sent.
    func testFirstPollReloadsTheTimelineOnceWithoutAPollRequest() async {
        let api = makeAPI()
        let connection = ConnectionViewModel(api: api)
        connection.firstPollCheckGap = .zero
        let timeline = TimelineViewModel(api: api)
        let log = RequestLog()
        let notPolled = Self.answer(lastPolledAt: nil)
        let polled = Self.answer()
        let emptyPage = TimelineFixtures.page(prefix: "e", count: 0, nextCursor: nil)
        let fullPage = TimelineFixtures.page(prefix: "p", count: 3, nextCursor: nil)
        StubURLProtocol.handler = { request in
            let path = request.url?.path ?? ""
            log.add(path)
            switch path {
            case "/v1/me/connection":
                return .init(body: log.count(path) == 1 ? notPolled : polled)
            case "/v1/timeline":
                return .init(body: log.count(path) == 1 ? emptyPage : fullPage)
            default:
                return .init(status: 500)
            }
        }

        // What TimelineView does at sign-in.
        await timeline.load()
        await connection.load()
        XCTAssertEqual(timeline.status, .empty(reason: "no_plays"))
        if await connection.waitForFirstPoll() { await timeline.load() }

        XCTAssertEqual(log.count("/v1/me/connection"), 2)
        XCTAssertEqual(log.count("/v1/timeline"), 2, "one load at sign-in, one reload after the poll")
        XCTAssertEqual(log.count("/v1/me/spotify/poll"), 0)
        XCTAssertEqual(timeline.status, .loaded)
        XCTAssertEqual(timeline.days.flatMap(\.events).count, 3)
        XCTAssertNotNil(connection.connection?.lastPolledAt)
        XCTAssertFalse(connection.isWaitingForFirstPoll)
    }

    /// A connection that was polled before needs no wait and no check.
    func testAlreadyPolledDoesNotWait() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.answer()) }
        await model.load()
        let reload = await model.waitForFirstPoll()
        XCTAssertFalse(reload)
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
    }

    /// The wait is bounded: no poll time after every check means no reload.
    func testNoPollWithinTheLimitGivesUp() async {
        let model = makeModel()
        model.firstPollChecks = 3
        StubURLProtocol.handler = { _ in .init(body: Self.answer(lastPolledAt: nil)) }
        await model.load()
        let reload = await model.waitForFirstPoll()
        XCTAssertFalse(reload)
        XCTAssertEqual(StubURLProtocol.requestCount, 4, "the first load and 3 checks")
        XCTAssertFalse(model.isWaitingForFirstPoll)
    }

    /// A check that fails offline keeps the last answer and tries again.
    func testOfflineCheckTriesAgain() async {
        let model = makeModel()
        let notPolled = Self.answer(lastPolledAt: nil)
        let polled = Self.answer()
        StubURLProtocol.handler = { _ in
            switch StubURLProtocol.requestCount {
            case 1: return .init(body: notPolled)
            case 2: return .init(error: URLError(.notConnectedToInternet))
            default: return .init(body: polled)
            }
        }
        await model.load()
        let reload = await model.waitForFirstPoll()
        XCTAssertTrue(reload)
        XCTAssertEqual(StubURLProtocol.requestCount, 3)
    }

    /// Revoked while waiting: stop, and the banner shows "revoked".
    func testRevokedWhileWaitingStops() async {
        let model = makeModel()
        let notPolled = Self.answer(lastPolledAt: nil)
        let revoked = Self.answer(status: "reauth_required", lastPolledAt: nil)
        StubURLProtocol.handler = { _ in
            .init(body: StubURLProtocol.requestCount == 1 ? notPolled : revoked)
        }
        await model.load()
        let reload = await model.waitForFirstPoll()
        XCTAssertFalse(reload)
        XCTAssertEqual(StubURLProtocol.requestCount, 2)
        XCTAssertEqual(model.notice, .revoked)
    }

    // MARK: - Text

    func testBannerTextNamesTheProblemAndTheNextStep() {
        let revoked = Messages.connection(.revoked, byDate: nil)
        XCTAssertEqual(revoked.title, "Spotify disconnected")
        XCTAssertTrue(revoked.detail.contains("Reconnect Spotify"))

        let soon = Messages.connection(.reconnectSoon(by: nil), byDate: "30 March")
        XCTAssertEqual(soon.detail, "Reconnect Spotify by 30 March to keep collecting your plays.")
    }

    /// The deadline is read in the display zone: 23:30 UTC on 29 March
    /// is already 30 March in Warsaw.
    func testDeadlineDateUsesTheDisplayZone() {
        let date = APIClient.parseInstant("2027-03-29T23:30:00Z")!
        let text = DisplayFormat.dayAndMonth(
            date, timeZone: TimeZone(identifier: "Europe/Warsaw")!, locale: Locale(identifier: "en_GB"))
        XCTAssertEqual(text, "30 March")
    }
}

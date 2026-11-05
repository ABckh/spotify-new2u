import XCTest
@testable import New2U

/// "Refresh from Spotify" and its cooldown (WHO-96, server side WHO-95).
@MainActor
final class RefreshTests: XCTestCase {
    /// A fixed "now" for every test: 12:00:00 UTC.
    private let now = APIClient.parseInstant("2026-10-17T12:00:00Z")!

    private func makeModel() -> RefreshViewModel {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        addTeardownBlock { @MainActor in store.deleteToken() }
        let api = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        let fixedNow = now
        let model = RefreshViewModel(api: api, now: { fixedNow })
        model.pollWait = .zero
        return model
    }

    /// GET /v1/me/connection with the WHO-95 fields.
    private func connection(nextManualPollAt: String?) throws -> Connection {
        let next = nextManualPollAt.map { "\"\($0)\"" } ?? "null"
        let json = """
            {"status": "active", "reauth_reason": null, "spotify_display_name": "Synthetic Listener",
             "grant_expires_at": "2027-03-30T06:00:00Z", "reconnect_soon": false,
             "last_polled_at": "2026-10-17T11:56:00Z",
             "last_poll_attempt_at": "2026-10-17T11:56:00Z",
             "next_manual_poll_at": \(next), "poll_interval_seconds": 900}
            """
        return try APIClient.makeDecoder().decode(Connection.self, from: Data(json.utf8))
    }

    // MARK: - Disabled state from next_manual_poll_at

    func testFutureNextManualPollAtDisablesTheButton() throws {
        let model = makeModel()
        model.show(try connection(nextManualPollAt: "2026-10-17T12:03:12Z"))
        XCTAssertFalse(model.canRefresh(at: now))
        XCTAssertEqual(model.secondsToWait(at: now), 192)
        XCTAssertEqual(Messages.refreshAvailable(inSeconds: 192), "Available in 3:12")
        XCTAssertEqual(model.lastPolledAt, APIClient.parseInstant("2026-10-17T11:56:00Z"))
    }

    func testTheButtonTurnsBackOnAtNextManualPollAt() throws {
        let model = makeModel()
        model.show(try connection(nextManualPollAt: "2026-10-17T12:03:12Z"))
        let later = APIClient.parseInstant("2026-10-17T12:03:12Z")!
        XCTAssertNil(model.secondsToWait(at: later))
        XCTAssertTrue(model.canRefresh(at: later))
    }

    func testNullOrPastNextManualPollAtMeansNow() throws {
        let model = makeModel()
        model.show(try connection(nextManualPollAt: nil))
        XCTAssertTrue(model.canRefresh(at: now))
        model.show(try connection(nextManualPollAt: "2026-10-17T11:00:00Z"))
        XCTAssertTrue(model.canRefresh(at: now))
    }

    /// During the cooldown a tap sends nothing.
    func testNoRequestDuringTheCooldown() async throws {
        let model = makeModel()
        model.show(try connection(nextManualPollAt: "2026-10-17T12:03:12Z"))
        let answer = await model.request()
        XCTAssertEqual(answer, .coolingDown)
        XCTAssertEqual(StubURLProtocol.requestCount, 0)
    }

    // MARK: - Responses

    func test202StartsCheckingAndTakesTheNextAllowedTime() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in
            .init(status: 202, body: #"{"requested_at": "2026-10-17T12:00:00Z", "next_allowed_at": "2026-10-17T12:05:00Z"}"#)
        }
        let answer = await model.request()
        XCTAssertEqual(answer, .accepted)
        XCTAssertEqual(StubURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/me/spotify/poll")
        XCTAssertEqual(StubURLProtocol.lastRequest?.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-token")
        XCTAssertEqual(model.status, .checking)
        XCTAssertFalse(model.canRefresh(at: now))
        XCTAssertEqual(model.secondsToWait(at: now), 300)

        await model.waitForPoll()
        model.finishChecking()
        XCTAssertEqual(model.status, .idle)
    }

    func test429TakesTheWaitFromRetryAfter() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in
            .init(status: 429, headers: ["Retry-After": "192"],
                  body: #"{"detail": {"code": "cooling_down", "message": "Spotify was checked a moment ago."}}"#)
        }
        let answer = await model.request()
        XCTAssertEqual(answer, .coolingDown)
        XCTAssertEqual(model.status, .idle)
        XCTAssertEqual(model.secondsToWait(at: now), 192)
        XCTAssertFalse(model.canRefresh(at: now))
    }

    /// The per-user request limit may answer 429 without Retry-After.
    func test429WithoutRetryAfterWaitsAMinute() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(status: 429, body: #"{"detail": "Too Many Requests"}"#) }
        _ = await model.request()
        XCTAssertEqual(model.secondsToWait(at: now), 60)
    }

    func test409IsNotConnected() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in
            .init(status: 409, body: #"{"detail": {"code": "no_active_connection", "message": "Connect Spotify first."}}"#)
        }
        let answer = await model.request()
        XCTAssertEqual(answer, .notConnected)
        XCTAssertEqual(model.status, .idle)
        XCTAssertTrue(model.canRefresh(at: now))
    }

    func testOfflineShowsTheOfflineLine() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        let answer = await model.request()
        XCTAssertEqual(answer, .failed)
        XCTAssertEqual(model.status, .failed(.offline))
        XCTAssertEqual(Messages.refreshFailed(.offline), "You're offline. Connect to the internet, then try again.")
        XCTAssertTrue(model.canRefresh(at: now), "the user can try again")
    }

    // MARK: - Text

    func testAvailableTextPadsSeconds() {
        XCTAssertEqual(Messages.refreshAvailable(inSeconds: 65), "Available in 1:05")
        XCTAssertEqual(Messages.refreshAvailable(inSeconds: 9), "Available in 0:09")
    }
}

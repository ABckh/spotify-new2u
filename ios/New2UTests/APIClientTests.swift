import XCTest
@testable import New2U

final class APIClientTests: XCTestCase {
    private var store: SessionStore!
    private var client: APIClient!

    private struct Config: Decodable, Equatable {
        let pollIntervalSeconds: Int
    }

    /// Each test gets its own Keychain entry and a client whose network
    /// is StubURLProtocol.
    @MainActor
    private func makeClient() {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        self.store = store
        client = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        addTeardownBlock { @MainActor in store.deleteToken() }
    }

    @MainActor
    func testSendsBearerTokenAndDecodesSnakeCase() async throws {
        makeClient()
        store.saveToken("synthetic-token")
        StubURLProtocol.handler = { _ in .init(body: #"{"poll_interval_seconds": 900}"#) }

        let config: Config = try await client.get("/v1/config")

        XCTAssertEqual(config, Config(pollIntervalSeconds: 900))
        let request = try XCTUnwrap(StubURLProtocol.lastRequest)
        XCTAssertEqual(request.url?.absoluteString, "http://127.0.0.1:8000/v1/config")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-token")
    }

    @MainActor
    func testSendsQueryItems() async throws {
        makeClient()
        store.saveToken("synthetic-token")
        StubURLProtocol.handler = { _ in .init(body: #"{"poll_interval_seconds": 900}"#) }
        let _: Config = try await client.get(
            "/v1/summary", query: [URLQueryItem(name: "period", value: "week"), URLQueryItem(name: "offset", value: "-1")])
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.query, "period=week&offset=-1")
    }

    @MainActor
    func testNoTokenFailsWithoutCallingTheServer() async {
        makeClient()
        await assertThrows(APIError.notSignedIn) { let _: Config = try await self.client.get("/v1/config") }
        XCTAssertEqual(StubURLProtocol.requestCount, 0)
    }

    @MainActor
    func testNoBaseURLIsNotConfigured() async {
        makeClient()
        let unconfigured = APIClient(baseURL: nil, sessionStore: store, urlSession: StubURLProtocol.makeURLSession())
        await assertThrows(APIError.notConfigured) { let _: Config = try await unconfigured.get("/v1/config") }
    }

    @MainActor
    func testUnauthorizedDeletesTheTokenAndReportsExpiredSession() async {
        makeClient()
        store.saveToken("expired-token")
        StubURLProtocol.handler = { _ in .init(status: 401, headers: ["WWW-Authenticate": "Bearer"], body: #"{"detail": "Unauthorized"}"#) }

        await assertThrows(APIError.sessionExpired) { let _: Config = try await self.client.get("/v1/config") }
        XCTAssertNil(store.readToken())
    }

    @MainActor
    func testOfflineIsOneError() async {
        makeClient()
        store.saveToken("synthetic-token")
        for code in [URLError.Code.notConnectedToInternet, .timedOut, .cannotConnectToHost] {
            StubURLProtocol.handler = { _ in .init(error: URLError(code)) }
            await assertThrows(APIError.offline) { let _: Config = try await self.client.get("/v1/config") }
        }
        XCTAssertEqual(store.readToken(), "synthetic-token")
    }

    @MainActor
    func testValidationErrorCarriesTheContractCode() async {
        makeClient()
        store.saveToken("synthetic-token")
        StubURLProtocol.handler = { _ in
            .init(status: 422, body: #"{"detail": {"code": "period_range_reversed", "message": "to is before from"}}"#)
        }
        await assertThrows(APIError.invalidRequest(code: "period_range_reversed")) {
            let _: Config = try await self.client.get("/v1/summary")
        }
    }

    @MainActor
    func testRateLimitReadsRetryAfter() async {
        makeClient()
        store.saveToken("synthetic-token")
        StubURLProtocol.handler = { _ in .init(status: 429, headers: ["Retry-After": "12"], body: #"{"detail": "Too Many Requests"}"#) }
        await assertThrows(APIError.rateLimited(retryAfterSeconds: 12)) {
            let _: Config = try await self.client.get("/v1/timeline")
        }
    }

    @MainActor
    func testBadRequestReadsTheRefusalReason() async {
        makeClient()
        StubURLProtocol.handler = { _ in .init(status: 400, body: #"{"detail": {"reason": "different_account"}}"#) }
        await assertThrows(APIError.signInRefused(reason: "different_account")) {
            let _: Config = try await self.client.post("/auth/spotify/login/x/claim", body: ["a": "b"], signedIn: false)
        }

        StubURLProtocol.handler = { _ in .init(status: 400, body: "not json") }
        await assertThrows(APIError.signInRefused(reason: "unknown")) {
            let _: Config = try await self.client.post("/auth/spotify/login/x/claim", body: ["a": "b"], signedIn: false)
        }
    }

    @MainActor
    func testServerFaultAndUnexpectedBody() async {
        makeClient()
        store.saveToken("synthetic-token")
        StubURLProtocol.handler = { _ in .init(status: 500, body: "boom") }
        await assertThrows(APIError.server(status: 500)) { let _: Config = try await self.client.get("/v1/config") }

        StubURLProtocol.handler = { _ in .init(body: #"{"something": "else"}"#) }
        await assertThrows(APIError.badResponse) { let _: Config = try await self.client.get("/v1/config") }
    }

    @MainActor
    func testPostSendsJSONWithoutTokenWhenSignedOut() async throws {
        makeClient()
        struct Pending: Decodable, Equatable { let status: String }
        StubURLProtocol.handler = { _ in .init(status: 202, body: #"{"status": "pending"}"#) }

        let answer: Pending = try await client.post(
            "/auth/spotify/login", body: ["home_timezone": "Europe/Warsaw"], signedIn: false)

        XCTAssertEqual(answer, Pending(status: "pending"))
        let request = try XCTUnwrap(StubURLProtocol.lastRequest)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
    }

    func testParsesEveryInstantShapeTheServerSends() throws {
        let expected = try XCTUnwrap(APIClient.parseInstant("2026-10-11T08:00:00Z"))
        XCTAssertEqual(APIClient.parseInstant("2026-10-11T08:00:00.000Z"), expected)
        XCTAssertEqual(APIClient.parseInstant("2026-10-11T08:00:00.000000Z"), expected)
        let later = try XCTUnwrap(APIClient.parseInstant("2026-10-11T11:45:02.123456Z"))
        let secondsBetween: TimeInterval = 3 * 3600 + 45 * 60 + 2.123
        XCTAssertEqual(later.timeIntervalSince(expected), secondsBetween, accuracy: 0.0005)
        XCTAssertNil(APIClient.parseInstant("2026-10-11"))
        XCTAssertNil(APIClient.parseInstant("not a date"))
    }

    // MARK: - Refreshing the session

    /// Answers /auth/refresh with `refresh`, and every other request with
    /// 200 for "Bearer new-session" and 401 for anything else.
    @MainActor
    private func stubRefresh(_ refresh: StubURLProtocol.Stub) {
        StubURLProtocol.handler = { request in
            if request.url?.path == "/auth/refresh" { return refresh }
            if request.value(forHTTPHeaderField: "Authorization") == "Bearer new-session" {
                return .init(body: #"{"poll_interval_seconds": 900}"#)
            }
            return .init(status: 401, headers: ["WWW-Authenticate": "Bearer"], body: #"{"detail": "Unauthorized"}"#)
        }
    }

    private let refreshed = StubURLProtocol.Stub(body: """
        {"session_token": "new-session", "expires_at": "2026-10-27T08:00:00Z",
         "refresh_token": "new-refresh", "refresh_expires_at": "2027-01-25T07:00:00Z"}
        """)

    @MainActor
    func testAnExpiredSessionIsRefreshedAndTheRequestSentAgain() async throws {
        makeClient()
        store.saveToken("old-session", refreshToken: "old-refresh")
        stubRefresh(refreshed)

        let config: Config = try await client.get("/v1/config")

        XCTAssertEqual(config, Config(pollIntervalSeconds: 900))
        XCTAssertEqual(store.readToken(), "new-session")
        XCTAssertEqual(store.readRefreshToken(), "new-refresh")
        XCTAssertTrue(store.hasToken)
        // The 401, the refresh, and the same request again.
        XCTAssertEqual(StubURLProtocol.requestCount, 3)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/config")
    }

    @MainActor
    func testTheRefreshSendsTheRefreshTokenAndNoSessionToken() async throws {
        makeClient()
        store.saveToken("old-session", refreshToken: "old-refresh")
        var refreshRequest: URLRequest?
        StubURLProtocol.handler = { [refreshed] request in
            if request.url?.path == "/auth/refresh" {
                refreshRequest = request
                return refreshed
            }
            if request.value(forHTTPHeaderField: "Authorization") == "Bearer new-session" {
                return .init(body: #"{"poll_interval_seconds": 900}"#)
            }
            return .init(status: 401)
        }

        let _: Config = try await client.get("/v1/config")

        let request = try XCTUnwrap(refreshRequest)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        let body = try XCTUnwrap(request.httpBodyStream.map(Self.read) ?? request.httpBody)
        XCTAssertEqual(try JSONDecoder().decode([String: String].self, from: body), ["refresh_token": "old-refresh"])
    }

    @MainActor
    func testARefusedRefreshSignsOut() async {
        makeClient()
        store.saveToken("old-session", refreshToken: "old-refresh")
        stubRefresh(.init(status: 401, body: #"{"detail": "Unauthorized"}"#))

        await assertThrows(APIError.sessionExpired) { let _: Config = try await self.client.get("/v1/config") }

        XCTAssertNil(store.readToken())
        XCTAssertNil(store.readRefreshToken())
        XCTAssertTrue(store.sessionExpired)
    }

    @MainActor
    func testARefreshWhileOfflineKeepsTheTokens() async {
        makeClient()
        store.saveToken("old-session", refreshToken: "old-refresh")
        stubRefresh(.init(error: URLError(.notConnectedToInternet)))

        await assertThrows(APIError.offline) { let _: Config = try await self.client.get("/v1/config") }

        XCTAssertEqual(store.readToken(), "old-session")
        XCTAssertEqual(store.readRefreshToken(), "old-refresh")
    }

    @MainActor
    func testRequestsThatExpireTogetherShareOneRefresh() async throws {
        makeClient()
        store.saveToken("old-session", refreshToken: "old-refresh")
        var refreshes = 0
        StubURLProtocol.handler = { [refreshed] request in
            if request.url?.path == "/auth/refresh" {
                refreshes += 1
                return refreshed
            }
            if request.value(forHTTPHeaderField: "Authorization") == "Bearer new-session" {
                return .init(body: #"{"poll_interval_seconds": 900}"#)
            }
            return .init(status: 401)
        }

        let client = try XCTUnwrap(self.client)
        async let first: Config = client.get("/v1/config")
        async let second: Config = client.get("/v1/summary")
        async let third: Config = client.get("/v1/timeline")
        let answers = try await [first, second, third]

        XCTAssertEqual(answers, Array(repeating: Config(pollIntervalSeconds: 900), count: 3))
        XCTAssertEqual(refreshes, 1, "a refresh token works once, so only one refresh may be sent")
    }

    @MainActor
    func testWithoutARefreshTokenA401SignsOutAsBefore() async {
        makeClient()
        store.saveToken("old-session")
        stubRefresh(refreshed)

        await assertThrows(APIError.sessionExpired) { let _: Config = try await self.client.get("/v1/config") }

        XCTAssertEqual(StubURLProtocol.requestCount, 1, "no refresh without a refresh token")
        XCTAssertNil(store.readToken())
    }

    nonisolated private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }

    // MARK: - Helper

    @MainActor
    private func assertThrows(
        _ expected: APIError,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () async throws -> Void
    ) async {
        do {
            try await body()
            XCTFail("Expected \(expected), but nothing was thrown", file: file, line: line)
        } catch let error as APIError {
            XCTAssertEqual(error, expected, file: file, line: line)
        } catch {
            XCTFail("Expected \(expected), got \(error)", file: file, line: line)
        }
    }
}

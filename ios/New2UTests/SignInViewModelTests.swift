import AuthenticationServices
import XCTest
@testable import New2U

/// Synthetic values only: no real login, secret or token.
final class SignInViewModelTests: XCTestCase {
    private let loginID = "6f1c2b4e-8d3a-4c5f-9e7b-1a2b3c4d5e6f"
    private var store: SessionStore!
    private var viewModel: SignInViewModel!
    /// The JSON bodies the app sent, keyed by request path.
    /// NSDictionary, because the login body mixes strings and a boolean.
    nonisolated(unsafe) private static var sentBodies: [String: NSDictionary] = [:]
    /// The Authorization header the app sent, keyed by request path.
    nonisolated(unsafe) private static var sentAuthorization: [String: String] = [:]

    @MainActor
    private func makeViewModel(claim: StubURLProtocol.Stub? = nil, login: StubURLProtocol.Stub? = nil) {
        StubURLProtocol.reset()
        Self.sentBodies = [:]
        Self.sentAuthorization = [:]
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        self.store = store
        let api = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        viewModel = SignInViewModel(api: api, sessionStore: store)
        addTeardownBlock { @MainActor in store.deleteToken() }

        let loginID = self.loginID
        let claimStub = claim ?? .init(body: #"{"session_token": "synthetic-session", "expires_at": "2026-11-10T08:00:00Z"}"#)
        StubURLProtocol.handler = { request in
            let path = request.url?.path ?? ""
            Self.sentBodies[path] = Self.readJSONBody(request)
            Self.sentAuthorization[path] = request.value(forHTTPHeaderField: "Authorization")
            if path == "/auth/spotify/login" {
                if let login { return login }
                return .init(status: 201, body: """
                    {"login_id": "\(loginID)", "claim_secret": "synthetic-claim-secret",
                     "authorize_url": "https://accounts.spotify.example/authorize?x=1",
                     "expires_at": "2026-10-13T08:10:00Z"}
                    """)
            }
            return claimStub
        }
    }

    private func callback(_ query: String) -> SignInViewModel.OpenBrowser {
        { _ in URL(string: "new2u://signed-in?\(query)")! }
    }

    // MARK: - Success

    @MainActor
    func testConnectedSignInClaimsAndSavesTheToken() async {
        makeViewModel()
        var openedURL: URL?
        await viewModel.connect { url in
            openedURL = url
            return URL(string: "new2u://signed-in?login_id=\(self.loginID)&result=connected&callback_code=synthetic-code")!
        }

        XCTAssertEqual(viewModel.status, .idle)
        XCTAssertEqual(store.readToken(), "synthetic-session")
        XCTAssertTrue(store.hasToken)
        XCTAssertEqual(openedURL?.host, "accounts.spotify.example")
        XCTAssertEqual(
            Self.sentBodies["/auth/spotify/login"],
            ["home_timezone": TimeZone.current.identifier, "import_recent_history": true] as NSDictionary)
        XCTAssertEqual(
            Self.sentBodies["/auth/spotify/login/\(loginID)/claim"],
            ["claim_secret": "synthetic-claim-secret", "callback_code": "synthetic-code"] as NSDictionary)
    }

    // MARK: - The recent-plays import, always on (WHO-92, WHO-114)

    @MainActor
    func testFirstSignInAsksForTheImport() async {
        makeViewModel()
        await viewModel.connect(openBrowser: connected)
        XCTAssertTrue(store.hasToken)
        let body = Self.sentBodies["/auth/spotify/login"]
        XCTAssertEqual(body?["import_recent_history"] as? Bool, true)
        // A JSON boolean, not the number 1 or the string "true".
        XCTAssertTrue(body?["import_recent_history"] is Bool)
        XCTAssertEqual(CFGetTypeID(body?["import_recent_history"] as CFTypeRef), CFBooleanGetTypeID())
    }

    @MainActor
    func testRenewalNeverAsksForTheImport() async {
        makeViewModel()
        store.saveToken("synthetic-old-session")
        await viewModel.connect(renewing: true, openBrowser: connected)
        XCTAssertEqual(Self.sentBodies["/auth/spotify/login"]?["import_recent_history"] as? Bool, false)
    }

    // MARK: - Failures, each with a message and no token

    @MainActor
    func testFailedResultShowsNotConnectedWithoutClaiming() async {
        makeViewModel()
        await viewModel.connect(openBrowser: callback("login_id=\(loginID)&result=failed&callback_code=c"))
        XCTAssertEqual(viewModel.status, .failed(Messages.signInNotConnected))
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
        XCTAssertFalse(store.hasToken)
    }

    @MainActor
    func testCallbackForAnotherSignInIsRefused() async {
        makeViewModel()
        let other = UUID().uuidString.lowercased()
        await viewModel.connect(openBrowser: callback("login_id=\(other)&result=connected&callback_code=c"))
        XCTAssertEqual(viewModel.status, .failed(Messages.signInNotConnected))
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
        XCTAssertFalse(store.hasToken)
    }

    @MainActor
    func testClosingTheSheetIsNotAnError() async {
        makeViewModel()
        await viewModel.connect { _ in throw ASWebAuthenticationSessionError(.canceledLogin) }
        XCTAssertEqual(viewModel.status, .idle)
        XCTAssertFalse(store.hasToken)
    }

    @MainActor
    func testOfflineShowsTheOfflineMessage() async {
        makeViewModel()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await viewModel.connect(openBrowser: callback(""))
        XCTAssertEqual(viewModel.status, .failed(Messages.failure(.offline)))
    }

    @MainActor
    func testClaimAnswers() async {
        let connected = "login_id=\(loginID)&result=connected&callback_code=c"
        let cases: [(StubURLProtocol.Stub, Messages.Text)] = [
            (.init(status: 202, body: #"{"status": "pending"}"#), Messages.signInNotConnected),
            (.init(status: 400, body: #"{"detail": {"reason": "access_denied"}}"#), Messages.signInNotConnected),
            (.init(status: 404, body: #"{"detail": "Not Found"}"#), Messages.signInFailure(.notFound)),
            (.init(status: 429, headers: ["Retry-After": "30"]), Messages.failure(.rateLimited(retryAfterSeconds: 30))),
        ]
        for (stub, expected) in cases {
            makeViewModel(claim: stub)
            await viewModel.connect(openBrowser: callback(connected))
            XCTAssertEqual(viewModel.status, .failed(expected), "status \(stub.status)")
            XCTAssertFalse(store.hasToken)
        }
    }

    // MARK: - Renewal from the reconnect banner (S9.16)

    private var connected: SignInViewModel.OpenBrowser {
        callback("login_id=\(loginID)&result=connected&callback_code=c")
    }

    @MainActor
    func testFirstSignInSendsNoSessionToken() async {
        makeViewModel()
        await viewModel.connect(openBrowser: connected)
        XCTAssertNil(Self.sentAuthorization["/auth/spotify/login"])
        XCTAssertNil(Self.sentAuthorization["/auth/spotify/login/\(loginID)/claim"])
    }

    @MainActor
    func testRenewalSendsTheSessionTokenOnLoginOnly() async {
        makeViewModel()
        store.saveToken("synthetic-old-session")
        await viewModel.connect(renewing: true, openBrowser: connected)
        XCTAssertEqual(Self.sentAuthorization["/auth/spotify/login"], "Bearer synthetic-old-session")
        XCTAssertNil(Self.sentAuthorization["/auth/spotify/login/\(loginID)/claim"])
    }

    @MainActor
    func testSameAccountRenewalSavesTheNewToken() async {
        makeViewModel()
        store.saveToken("synthetic-old-session")
        await viewModel.connect(renewing: true, openBrowser: connected)
        XCTAssertEqual(viewModel.status, .idle)
        XCTAssertEqual(store.readToken(), "synthetic-session")
    }

    @MainActor
    func testDifferentAccountKeepsTheSessionToken() async {
        makeViewModel(claim: .init(status: 400, body: #"{"detail": {"reason": "different_account"}}"#))
        store.saveToken("synthetic-old-session")
        await viewModel.connect(renewing: true, openBrowser: connected)
        XCTAssertEqual(viewModel.status, .failed(Messages.signInWrongAccount))
        XCTAssertEqual(store.readToken(), "synthetic-old-session")
    }

    @MainActor
    func testOtherRefusalOnRenewalShowsNotConnectedAndKeepsTheToken() async {
        for body in [#"{"detail": {"reason": "access_denied"}}"#, #"{"detail": "Bad Request"}"#, ""] {
            makeViewModel(claim: .init(status: 400, body: body))
            store.saveToken("synthetic-old-session")
            await viewModel.connect(renewing: true, openBrowser: connected)
            XCTAssertEqual(viewModel.status, .failed(Messages.signInNotConnected), "body \(body)")
            XCTAssertEqual(store.readToken(), "synthetic-old-session")
        }
    }

    @MainActor
    func testRenewalWithAnExpiredSessionSignsOut() async {
        makeViewModel(login: .init(status: 401, headers: ["WWW-Authenticate": "Bearer"], body: #"{"detail": "Not authenticated"}"#))
        store.saveToken("synthetic-old-session")
        await viewModel.connect(renewing: true, openBrowser: connected)
        XCTAssertEqual(viewModel.status, .failed(Messages.failure(.sessionExpired)))
        XCTAssertFalse(store.hasToken)
        // Never carried on as a fresh sign-in: the browser was not opened.
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
    }

    // MARK: - Import notice (C3 / C3b, WHO-121)

    /// The Security-approved strings, word for word (WHO-119 copy §1,
    /// changed by WHO-120). A wording change must go back to Security.
    @MainActor
    func testImportNoticeStringsAreWordForWord() {
        XCTAssertEqual(
            Messages.connectImportNotice,
            "When you connect, we also import the last 50 plays Spotify still lists, including plays from before you connect. We read your saved albums and songs, followed artists and top lists, so music you already knew doesn't show as new.")
        XCTAssertEqual(
            Messages.reconnectImportNotice,
            "If your data was deleted, or you connect a different Spotify account, we also import the last 50 plays Spotify still lists, including plays from before you connect. We read your saved albums and songs, followed artists and top lists, so music you already knew doesn't show as new.")
    }

    /// Never signed in: the connect screen shows C3.
    @MainActor
    func testImportNoticeIsC3WithoutASession() {
        makeViewModel()
        XCTAssertFalse(store.sessionExpired)
        XCTAssertEqual(viewModel.importNotice, Messages.connectImportNotice)
    }

    /// After a 401 (for example after a delete-all), the connect screen
    /// is "Your session ended". Connecting from there imports, so the
    /// notice stays, as C3b.
    @MainActor
    func testImportNoticeIsC3bAfterTheSessionEnded() {
        makeViewModel()
        store.endExpiredSession()
        XCTAssertEqual(viewModel.importNotice, Messages.reconnectImportNotice)
    }

    /// "Try again" starts the same connect, so a failure keeps the notice.
    @MainActor
    func testImportNoticeStaysAfterAFailedSignIn() async {
        makeViewModel()
        await viewModel.connect(openBrowser: callback("login_id=\(loginID)&result=failed&callback_code=c"))
        XCTAssertEqual(viewModel.status, .failed(Messages.signInNotConnected))
        XCTAssertEqual(viewModel.importNotice, Messages.connectImportNotice)
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

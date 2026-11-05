import Foundation

/// Answers every request with a canned response, so APIClient tests
/// never touch the network. Set `handler` before each test.
final class StubURLProtocol: URLProtocol {
    struct Stub {
        var status: Int = 200
        var headers: [String: String] = [:]
        var body: String = "{}"
        /// Sent instead of `body` when set, for binary answers (covers).
        var data: Data?
        var error: URLError?
        /// Sent as a redirect to this URL (with `status`, e.g. 302).
        var redirectTo: URL?
        /// Never answers, like a dead connection. Only cancelling the
        /// request ends it (counted in `stoppedCount`).
        var neverAnswers = false
    }

    nonisolated(unsafe) static var handler: (URLRequest) -> Stub = { _ in Stub() }
    nonisolated(unsafe) static var lastRequest: URLRequest?
    nonisolated(unsafe) static var requestCount = 0
    /// Every URL asked for, in order. Requests can start on several
    /// threads at once (parallel cover loads), so this one is locked.
    static var requestedURLs: [URL] { lock.withLock { urls } }
    /// How many requests the session stopped. A `neverAnswers` request
    /// is only stopped when it is cancelled.
    static var stoppedCount: Int { lock.withLock { stopped } }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var urls: [URL] = []
    nonisolated(unsafe) private static var stopped = 0

    static func reset() {
        handler = { _ in Stub() }
        lastRequest = nil
        requestCount = 0
        lock.withLock {
            urls = []
            stopped = 0
        }
    }

    static func makeURLSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lastRequest = request
        Self.requestCount += 1
        Self.lock.withLock { Self.urls.append(request.url!) }
        let stub = Self.handler(request)
        if stub.neverAnswers { return }
        if let error = stub.error {
            client?.urlProtocol(self, didFailWithError: error)
            return
        }
        let response = HTTPURLResponse(
            url: request.url!, statusCode: stub.status, httpVersion: "HTTP/1.1", headerFields: stub.headers)!
        if let target = stub.redirectTo {
            // Asks the session's delegate. If it accepts, the session
            // starts a new request; if it refuses, the 3xx is the answer.
            client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: target), redirectResponse: response)
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: stub.data ?? Data(stub.body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {
        Self.lock.withLock { Self.stopped += 1 }
    }
}

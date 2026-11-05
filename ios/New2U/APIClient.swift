import Foundation

/// The one place the app talks to our API.
///
/// It adds the session token, sends the request, and turns the answer
/// into either a decoded value or an `APIError`. It knows nothing about
/// screens. Each screen's view model calls it with plain `async/await`.
///
/// The session token lasts an hour. When a signed-in request answers 401,
/// the client trades the refresh token for a new pair at
/// POST /auth/refresh and sends the request once more, so the user stays
/// signed in. Only one refresh runs at a time: a refresh token works once,
/// and the server ends the session if it sees one twice. The user signs in
/// again only when the refresh itself is refused.
final class APIClient {
    private let baseURL: URL?
    private let sessionStore: SessionStore
    private let urlSession: URLSession
    /// The refresh in progress, shared by every request that got a 401
    /// meanwhile. Main-actor state, like the rest of this class.
    private var refreshInFlight: Task<Bool, Error>?

    /// `urlSession` is only replaced by the unit tests, to answer
    /// requests with fixtures instead of the network.
    init(baseURL: URL?, sessionStore: SessionStore, urlSession: URLSession = APIClient.makeURLSession()) {
        self.baseURL = baseURL
        self.sessionStore = sessionStore
        self.urlSession = urlSession
    }

    /// A session that keeps nothing on disk: no response cache and no
    /// cookies. Listening history must not sit in a cache file.
    static func makeURLSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.timeoutIntervalForRequest = 30
        return URLSession(configuration: configuration)
    }

    /// GET a path such as "/v1/summary" and decode the JSON answer.
    func get<Response: Decodable>(
        _ path: String,
        query: [URLQueryItem] = [],
        signedIn: Bool = true
    ) async throws -> Response {
        let data = try await execute(signedIn: signedIn) {
            try self.makeRequest(method: "GET", path: path, query: query, body: nil, signedIn: signedIn)
        }
        return try decode(data)
    }

    /// POST a JSON body and decode the JSON answer.
    func post<Response: Decodable>(
        _ path: String,
        body: some Encodable,
        signedIn: Bool = true
    ) async throws -> Response {
        let bodyData = try JSONEncoder().encode(body)
        let data = try await execute(signedIn: signedIn) {
            try self.makeRequest(method: "POST", path: path, query: [], body: bodyData, signedIn: signedIn)
        }
        return try decode(data)
    }

    /// POST with no body, such as "/v1/me/spotify/poll", and decode the
    /// JSON answer.
    func post<Response: Decodable>(_ path: String, signedIn: Bool = true) async throws -> Response {
        let data = try await execute(signedIn: signedIn) {
            try self.makeRequest(method: "POST", path: path, query: [], body: nil, signedIn: signedIn)
        }
        return try decode(data)
    }

    /// POST (or PUT) a JSON body to a route that answers 204 with no
    /// JSON, such as "/v1/me/review/done".
    func sendJSON(_ path: String, method: String = "POST", body: some Encodable) async throws {
        let bodyData = try JSONEncoder().encode(body)
        _ = try await execute(signedIn: true) {
            try self.makeRequest(method: method, path: path, query: [], body: bodyData, signedIn: true)
        }
    }

    /// PUT or DELETE with no body, for a route that answers 204 with no
    /// JSON, such as "Not new" (WHO-123).
    ///
    /// The path is given as its parts, and each part is percent-encoded on
    /// its own: an artist name such as "AC/DC" stays one part of the path.
    func send(method: String, pathSegments: [String]) async throws {
        let path = "/" + pathSegments.map(Self.encodePathSegment).joined(separator: "/")
        _ = try await execute(signedIn: true) {
            try self.makeRequest(
                method: method, path: path, query: [], body: nil, signedIn: true, pathIsEncoded: true)
        }
    }

    /// Everything except ASCII letters, digits and "-._~" is
    /// percent-encoded, so "/", "?", "#" and spaces cannot change the path.
    nonisolated static func encodePathSegment(_ segment: String) -> String {
        let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return segment.addingPercentEncoding(withAllowedCharacters: unreserved) ?? segment
    }

    /// POST one file as `multipart/form-data`, with the file in the part
    /// named `file`, and decode the JSON answer. Used for the Spotify
    /// history file (POST /v1/spotify/history-import).
    ///
    /// The body is written to a temporary file and sent from there, so a
    /// 200 MB file is never held in memory. That temporary file is deleted
    /// when the upload ends, whether it worked or not.
    func upload<Response: Decodable>(_ path: String, fileAt fileURL: URL, fileName: String) async throws -> Response {
        let boundary = "New2U-\(UUID().uuidString)"
        let bodyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("upload-\(UUID().uuidString).multipart")
        defer { try? FileManager.default.removeItem(at: bodyURL) }
        try await Self.writeMultipartBody(to: bodyURL, boundary: boundary, fileURL: fileURL, fileName: fileName)

        let data = try await execute(signedIn: true, bodyFile: bodyURL) {
            var request = try self.makeRequest(method: "POST", path: path, query: [], body: nil, signedIn: true)
            request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
            return request
        }
        return try decode(data)
    }

    /// Writes the multipart body: one part named `file`, copied from
    /// `fileURL` 1 MB at a time.
    ///
    /// `@concurrent` runs this away from the main thread, so copying a
    /// large file never freezes the screen.
    @concurrent
    nonisolated static func writeMultipartBody(
        to bodyURL: URL, boundary: String, fileURL: URL, fileName: String
    ) async throws {
        // Locked while the phone is locked, like the rest of our files.
        guard FileManager.default.createFile(
            atPath: bodyURL.path, contents: nil,
            attributes: [.protectionKey: FileProtectionType.complete])
        else { throw CocoaError(.fileWriteUnknown) }

        let output = try FileHandle(forWritingTo: bodyURL)
        defer { try? output.close() }
        let input = try FileHandle(forReadingFrom: fileURL)
        defer { try? input.close() }

        let header = "--\(boundary)\r\n"
            + "Content-Disposition: form-data; name=\"file\"; filename=\"\(fileName)\"\r\n"
            + "Content-Type: application/octet-stream\r\n\r\n"
        try output.write(contentsOf: Data(header.utf8))
        while let chunk = try input.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            try output.write(contentsOf: chunk)
        }
        try output.write(contentsOf: Data("\r\n--\(boundary)--\r\n".utf8))
    }

    // MARK: - Building the request

    private func makeRequest(
        method: String,
        path: String,
        query: [URLQueryItem],
        body: Data?,
        signedIn: Bool,
        pathIsEncoded: Bool = false
    ) throws -> URLRequest {
        guard let baseURL,
              var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        else { throw APIError.notConfigured }

        if pathIsEncoded {
            components.percentEncodedPath = path
        } else {
            components.path = path
        }
        components.queryItems = query.isEmpty ? nil : query
        guard let url = components.url else { throw APIError.notConfigured }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if signedIn {
            guard let token = sessionStore.readToken() else { throw APIError.notSignedIn }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    // MARK: - Staying signed in

    /// A 401 from the server, before deciding whether to refresh.
    private struct Unauthorized: Error {}

    /// Sends the request `build` makes. A signed-in request that answers
    /// 401 is sent once more after a refresh; the request is built again so
    /// it carries the new session token.
    ///
    /// `bodyFile` is set only for `upload`: the body is sent from that file.
    private func execute(
        signedIn: Bool, bodyFile: URL? = nil, _ build: () throws -> URLRequest
    ) async throws -> Data {
        let request = try build()
        do {
            return try await send(request, bodyFile: bodyFile)
        } catch is Unauthorized {
            guard signedIn,
                  try await refreshSession(replacing: request.value(forHTTPHeaderField: "Authorization"))
            else { throw endSession() }
            do {
                return try await send(try build(), bodyFile: bodyFile)
            } catch is Unauthorized {
                throw endSession()
            }
        }
    }

    /// True when a new session token is saved. False means sign in again.
    /// Throws (offline, say) without touching the tokens, so the next
    /// request can try again.
    private func refreshSession(replacing usedAuthorization: String?) async throws -> Bool {
        if let refreshInFlight {
            return try await refreshInFlight.value
        }
        // Another request refreshed while this one was out: use its token.
        if let token = sessionStore.readToken(), "Bearer \(token)" != usedAuthorization {
            return true
        }
        guard let refreshToken = sessionStore.readRefreshToken() else { return false }
        let task = Task { try await self.exchange(refreshToken) }
        refreshInFlight = task
        defer { refreshInFlight = nil }
        return try await task.value
    }

    /// POST /auth/refresh: the refresh token for a new session token and a
    /// new refresh token, which replace both saved ones.
    private func exchange(_ refreshToken: String) async throws -> Bool {
        let body = try JSONEncoder().encode(["refresh_token": refreshToken])
        let request = try makeRequest(method: "POST", path: "/auth/refresh", query: [], body: body, signedIn: false)
        do {
            let tokens: SessionTokens = try decode(try await send(request))
            sessionStore.saveToken(tokens.sessionToken, refreshToken: tokens.refreshToken)
            return true
        } catch is Unauthorized {
            return false
        }
    }

    /// The session is over: drop the tokens and show sign-in.
    private func endSession() -> APIError {
        sessionStore.endExpiredSession()
        return .sessionExpired
    }

    // MARK: - Sending and reading the answer

    /// `bodyFile` is set only for `upload`: the body is sent from that file.
    private func send(_ request: URLRequest, bodyFile: URL? = nil) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            if let bodyFile {
                (data, response) = try await urlSession.upload(for: request, fromFile: bodyFile)
            } else {
                (data, response) = try await urlSession.data(for: request)
            }
        } catch let error as URLError {
            throw Self.mapNetworkError(error)
        }

        guard let http = response as? HTTPURLResponse else { throw APIError.badResponse }
        switch http.statusCode {
        case 200..<300:
            return data
        case 401:
            // `execute` refreshes the session or ends it.
            throw Unauthorized()
        case 400:
            throw APIError.signInRefused(reason: Self.readRefusalReason(data) ?? "unknown")
        case 404:
            throw APIError.notFound
        case 409:
            throw APIError.conflict(code: Self.readErrorCode(data) ?? "unknown")
        case 413, 415, 422:
            throw APIError.invalidRequest(code: Self.readErrorCode(data) ?? "unknown")
        case 429, 503:
            let retryAfter = http.value(forHTTPHeaderField: "Retry-After").flatMap { Int($0) }
            throw APIError.rateLimited(retryAfterSeconds: retryAfter)
        default:
            throw APIError.server(status: http.statusCode)
        }
    }

    private func decode<Response: Decodable>(_ data: Data) throws -> Response {
        do {
            return try APIClient.makeDecoder().decode(Response.self, from: data)
        } catch {
            throw APIError.badResponse
        }
    }

    /// Every network failure becomes `.offline`, so screens show one
    /// message: "You're offline". A cancelled request (the user changed
    /// the period while it was loading) is not an error to show.
    static func mapNetworkError(_ error: URLError) -> Error {
        if error.code == .cancelled { return CancellationError() }
        return APIError.offline
    }

    /// Reads `detail.code` from a 409, 413, 415 or 422 body, if it has one.
    nonisolated private static func readErrorCode(_ data: Data) -> String? {
        struct Body: Decodable {
            struct Detail: Decodable { let code: String }
            let detail: Detail
        }
        return (try? JSONDecoder().decode(Body.self, from: data))?.detail.code
    }

    /// Reads `detail.reason` from a sign-in 400 body, if it has one.
    nonisolated private static func readRefusalReason(_ data: Data) -> String? {
        struct Body: Decodable {
            struct Detail: Decodable { let reason: String }
            let detail: Detail
        }
        return (try? JSONDecoder().decode(Body.self, from: data))?.detail.reason
    }

    // MARK: - JSON decoding

    /// A decoder that reads the contract's snake_case keys and its
    /// RFC 3339 instants.
    nonisolated static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = parseInstant(text) else {
                throw DecodingError.dataCorruptedError(
                    in: container, debugDescription: "Not an RFC 3339 instant: \(text)")
            }
            return date
        }
        return decoder
    }

    /// Parses "2026-10-11T08:00:00Z", "...00.123Z" and "...00.123456Z".
    ///
    /// The server sends 0, 3 or 6 fractional digits depending on the
    /// route (contract section 1). Digits after the third are dropped,
    /// because a Date is only shown to the second.
    nonisolated static func parseInstant(_ text: String) -> Date? {
        var normalized = text
        if let dot = text.firstIndex(of: "."), let zone = text.lastIndex(of: "Z"), dot < zone {
            let digits = text[text.index(after: dot)..<zone]
            let millis = String(digits.prefix(3)).padding(toLength: 3, withPad: "0", startingAt: 0)
            normalized = String(text[..<dot]) + "." + millis + "Z"
        }
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: normalized) { return date }
        let withoutFraction = ISO8601DateFormatter()
        withoutFraction.formatOptions = [.withInternetDateTime]
        return withoutFraction.date(from: normalized)
    }
}

/// 200 from the claim and from POST /auth/refresh: our session token and
/// the refresh token that renews it. Older servers send no refresh token.
struct SessionTokens: Decodable {
    let sessionToken: String
    let refreshToken: String?
}

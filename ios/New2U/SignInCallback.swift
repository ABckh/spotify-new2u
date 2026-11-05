import Foundation

/// What the server sends back when the Spotify sign-in finishes.
///
/// The server redirects the sign-in browser to
/// `new2u://signed-in?login_id=...&result=connected&callback_code=...`.
/// `ASWebAuthenticationSession` catches that URL and hands it to us.
struct SignInCallback: Equatable {
    enum Result: String {
        case connected
        case failed
    }

    /// The scheme the sign-in browser returns to. The only one the app handles.
    static let scheme = "new2u"
    static let host = "signed-in"

    let loginID: UUID
    let result: Result
    let callbackCode: String

    /// Reads a callback URL. Returns nil for anything else: another
    /// scheme or host, a missing or repeated field, an unknown result.
    static func parse(_ url: URL) -> SignInCallback? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              components.scheme?.lowercased() == scheme,
              components.host?.lowercased() == host,
              components.path.isEmpty || components.path == "/",
              let items = components.queryItems
        else { return nil }

        guard let loginID = singleValue("login_id", in: items).flatMap({ UUID(uuidString: $0) }),
              let result = singleValue("result", in: items).flatMap({ Result(rawValue: $0) }),
              let callbackCode = singleValue("callback_code", in: items), !callbackCode.isEmpty
        else { return nil }

        return SignInCallback(loginID: loginID, result: result, callbackCode: callbackCode)
    }

    /// The value of `name` when it appears exactly once.
    private static func singleValue(_ name: String, in items: [URLQueryItem]) -> String? {
        let matches = items.filter { $0.name == name }
        guard matches.count == 1 else { return nil }
        return matches[0].value
    }
}

import Foundation

/// Every way a call to our API can fail, in the terms a screen needs.
///
/// Screens switch over this to pick their message. The cases follow the
/// error table in the WHO-7 `api-contract` document, section 1.1.
enum APIError: Error, Equatable {
    /// The build has no usable server address (see AppConfig).
    case notConfigured
    /// There is no session token in the Keychain. Show sign-in.
    case notSignedIn
    /// 401: the session expired, was logged out, or the account is gone.
    /// The token has already been deleted. Show sign-in.
    case sessionExpired
    /// The device is offline or the server cannot be reached.
    case offline
    /// 404.
    case notFound
    /// 400 `{"detail": {"reason": ...}}` from the sign-in claim: the
    /// sign-in failed. `different_account` means a renewal picked another
    /// Spotify account (api-contract section 2.3). A 400 without a reason
    /// reads as "unknown".
    case signInRefused(reason: String)
    /// 409 with a stable `detail.code`. `no_active_connection` from
    /// POST /v1/me/spotify/poll means Spotify is not connected (WHO-95).
    case conflict(code: String)
    /// 422 with a stable `detail.code` from the read API. Also 413 and 415
    /// from the history file upload (WHO-104), which carry a code too.
    case invalidRequest(code: String)
    /// 429 or 503. Wait this many seconds before trying again.
    case rateLimited(retryAfterSeconds: Int?)
    /// Any other non-2xx status.
    case server(status: Int)
    /// A 2xx answer whose body did not match what we expected.
    case badResponse
}

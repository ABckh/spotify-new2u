import AuthenticationServices
import Foundation
import Observation

/// Runs "Connect Spotify" for `SignInView`.
///
/// 1. POST /auth/spotify/login: the server answers with a login id, a
///    claim secret and the Spotify page to open.
/// 2. The view opens that page in `ASWebAuthenticationSession`. When the
///    user finishes, the browser comes back on `new2u://signed-in?...`.
/// 3. POST /auth/spotify/login/{login_id}/claim with the claim secret and
///    the callback code: the server answers with our session token,
///    which goes into the Keychain. `RootView` then shows the timeline.
///
/// A reconnect from the timeline is a *renewal*: step 1 also sends the
/// current session token, so the server renews Spotify access for this
/// user only (api-contract section 2.1). If the user then picks another
/// Spotify account, the claim answers 400 `different_account` and the
/// current token stays in the Keychain. A first sign-in sends no token.
///
/// Step 1 also sends `import_recent_history` (WHO-89): true on the
/// connect screen, false for a renewal. The import is always on since
/// WHO-114, so there is no switch; the server imports the last 50 plays
/// on a first connect whatever this says, and never on a renewal.
///
/// The claim secret only lives in a local variable for one sign-in.
@Observable
final class SignInViewModel {
    enum Status: Equatable {
        /// The "Connect Spotify" button is ready.
        case idle
        /// A sign-in is running: talking to the server or the browser is open.
        case working
        /// The last sign-in failed. The screen shows this message.
        case failed(Messages.Text)
    }

    /// Opens the Spotify page and returns the URL the browser came back on.
    /// `SignInView` passes `ASWebAuthenticationSession`; tests pass a stub.
    typealias OpenBrowser = (URL) async throws -> URL

    private(set) var status: Status = .idle

    private let api: APIClient
    private let sessionStore: SessionStore

    init(api: APIClient, sessionStore: SessionStore) {
        self.api = api
        self.sessionStore = sessionStore
    }

    /// The plain line above the connect button (C3, or C3b after the
    /// server ended the session). It stays on the failure states too,
    /// because "Try again" starts the same connect.
    var importNotice: String {
        Messages.signInImportNotice(sessionExpired: sessionStore.sessionExpired)
    }

    /// `renewing` is true when the user is already signed in (the
    /// reconnect banner) and false on the connect screen.
    func connect(renewing: Bool = false, openBrowser: OpenBrowser) async {
        guard status != .working else { return }
        status = .working
        do {
            // 1. Start the sign-in. A renewal sends the session token.
            let started: LoginStarted = try await api.post(
                "/auth/spotify/login",
                body: LoginRequest(
                    homeTimezone: TimeZone.current.identifier,
                    importRecentHistory: !renewing),
                signedIn: renewing)
            guard let authorizeURL = URL(string: started.authorizeUrl), authorizeURL.scheme == "https" else {
                throw APIError.badResponse
            }

            // 2. Let the user sign in at Spotify.
            let returnedURL: URL
            do {
                returnedURL = try await openBrowser(authorizeURL)
            } catch ASWebAuthenticationSessionError.canceledLogin {
                // The user closed the sheet. Not an error to show.
                status = .idle
                return
            } catch {
                status = .failed(Messages.signInBrowserFailed)
                return
            }
            guard let callback = SignInCallback.parse(returnedURL),
                  callback.loginID == started.loginId,
                  callback.result == .connected
            else {
                status = .failed(Messages.signInNotConnected)
                return
            }

            // 3. Swap the claim secret and callback code for our session token.
            let claimed: ClaimAnswer = try await api.post(
                "/auth/spotify/login/\(started.loginId.uuidString.lowercased())/claim",
                body: ["claim_secret": started.claimSecret, "callback_code": callback.callbackCode],
                signedIn: false)
            guard let token = claimed.sessionToken, !token.isEmpty else {
                // 202 "pending": the server did not see Spotify finish.
                status = .failed(Messages.signInNotConnected)
                return
            }
            sessionStore.saveToken(token, refreshToken: claimed.refreshToken)
            status = .idle
        } catch let error as APIError {
            // Only a 401 deletes the token (in APIClient). A 400
            // `different_account` leaves the current session in place.
            status = .failed(Messages.signInFailure(error))
        } catch {
            status = .failed(Messages.signInFailure(.badResponse))
        }
    }
}

/// The body of POST /auth/spotify/login.
struct LoginRequest: Encodable {
    let homeTimezone: String
    /// True except for a renewal. Servers before WHO-114 read it on a
    /// first connect; newer ones ignore it and always import.
    let importRecentHistory: Bool

    enum CodingKeys: String, CodingKey {
        case homeTimezone = "home_timezone"
        case importRecentHistory = "import_recent_history"
    }
}

/// 201 from POST /auth/spotify/login.
struct LoginStarted: Decodable {
    let loginId: UUID
    let claimSecret: String
    let authorizeUrl: String
}

/// 200 `{"session_token", "expires_at", "refresh_token", ...}` or 202
/// `{"status": "pending"}` from the claim route. Only the two tokens are
/// used; a server without refresh tokens sends none.
struct ClaimAnswer: Decodable {
    let sessionToken: String?
    let refreshToken: String?
}

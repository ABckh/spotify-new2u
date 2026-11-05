import Foundation
import Observation
import Security

/// Keeps our own session token and refresh token in the Keychain.
///
/// These are the only credentials the app stores. The session token is
/// short-lived (an hour); the refresh token gets a new one from
/// POST /auth/refresh, so the user stays signed in (see `APIClient`).
/// Nothing here holds a Spotify token or touches UserDefaults or a plist.
///
/// `RootView` watches `hasToken`: when the session cannot be refreshed
/// and the tokens are deleted, the app goes back to the connect screen
/// by itself.
@Observable
final class SessionStore {
    /// True while a token is saved. Updated by save and delete.
    private(set) var hasToken = false
    /// True when the last sign-out happened because the server said the
    /// session was no longer valid, so the connect screen can say why.
    private(set) var sessionExpired = false

    private let service: String
    private let account = "session_token"
    private let refreshAccount = "refresh_token"

    /// `service` is only changed by the unit tests, so they never touch
    /// the real app's token.
    init(service: String = "app.new2u.session") {
        self.service = service
        hasToken = readToken() != nil
    }

    /// The saved session token, or nil when the user is signed out.
    func readToken() -> String? {
        read(account)
    }

    /// The saved refresh token, or nil when there is none (signed out, or
    /// a session from a server without refresh tokens).
    func readRefreshToken() -> String? {
        read(refreshAccount)
    }

    /// Saves the session token and its refresh token, replacing the
    /// previous ones. Without a refresh token any old one is removed: every
    /// refresh token works only once, so an old one must never be sent.
    func saveToken(_ token: String, refreshToken: String? = nil) {
        write(token, to: account)
        if let refreshToken {
            write(refreshToken, to: refreshAccount)
        } else {
            remove(refreshAccount)
        }
        hasToken = readToken() != nil
        sessionExpired = false
    }

    /// Removes both tokens. Safe to call when there is none.
    ///
    /// Covers count as listening history, so they go with the session:
    /// every way out of a session (401, sign-out, delete-all) ends here
    /// (WHO-148 rule 4).
    func deleteToken() {
        remove(account)
        remove(refreshAccount)
        hasToken = false
        CoverImageLoader.shared.removeAll()
    }

    /// Removes the tokens because the server refused the session and its
    /// refresh.
    func endExpiredSession() {
        deleteToken()
        sessionExpired = true
    }

    // MARK: - Keychain

    private func read(_ account: String) -> String? {
        var query = baseQuery(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: AnyObject?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func write(_ value: String, to account: String) {
        remove(account)
        var query = baseQuery(account)
        query[kSecValueData as String] = Data(value.utf8)
        // Readable after the first unlock, never synced or restored to
        // another device.
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(query as CFDictionary, nil)
    }

    private func remove(_ account: String) {
        SecItemDelete(baseQuery(account) as CFDictionary)
    }

    private func baseQuery(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

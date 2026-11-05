import Foundation
import Observation

/// Checks the Spotify connection, for `ConnectionBanner`.
///
/// The same shape as the other view models: the view reads the
/// properties below and calls `load()`. Plain `async/await`.
@Observable
final class ConnectionViewModel {
    enum Status: Equatable {
        case loading
        case loaded(Connection)
        /// 404: this account has no Spotify connection.
        case notConnected
        /// The check failed. The timeline shows its own error for the
        /// same cause, so the banner stays hidden.
        case failed(APIError)
    }

    /// What the banner says, or nil when there is nothing to say.
    enum Notice: Equatable {
        /// Spotify stopped letting the server read plays (the revoked
        /// Spotify token case). New plays are not collected.
        case revoked
        /// No Spotify account is connected.
        case notConnected
        /// Still collecting, but Spotify's permission ends soon.
        case reconnectSoon(by: Date?)
    }

    private(set) var status: Status = .loading
    /// True while `waitForFirstPoll()` waits. The timeline shows its
    /// small reload indicator meanwhile.
    private(set) var isWaitingForFirstPoll = false

    /// How often `waitForFirstPoll()` checks again, and how many times.
    /// 12 checks 5 s apart: about a minute. The tests set the gap to zero.
    var firstPollCheckGap: Duration = .seconds(5)
    var firstPollChecks = 12

    private let api: APIClient

    init(api: APIClient) {
        self.api = api
    }

    func load() async {
        status = .loading
        do {
            let answer: Connection = try await api.get("/v1/me/connection")
            try Task.checkCancellation()
            status = .loaded(answer)
        } catch is CancellationError {
            return
        } catch APIError.notFound {
            status = .notConnected
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            status = .failed(error)
        } catch {
            status = .failed(.badResponse)
        }
    }

    /// Right after sign-in the server has not polled Spotify yet, so the
    /// timeline is empty. The server polls a new connection within seconds
    /// and imports the last 50 plays on that first poll (WHO-87, WHO-190).
    ///
    /// If the last answer has no `lastPolledAt`, this checks the connection
    /// again every few seconds, for about a minute, until a poll time
    /// appears. It returns true when one did: the caller reloads once.
    /// It never asks the server to poll, so the "Sync now" cooldown
    /// does not start.
    func waitForFirstPoll() async -> Bool {
        guard let answer = connection, answer.isActive, answer.lastPolledAt == nil else { return false }
        isWaitingForFirstPoll = true
        defer { isWaitingForFirstPoll = false }
        for _ in 0..<firstPollChecks {
            try? await Task.sleep(for: firstPollCheckGap)
            if Task.isCancelled { return false }
            let latest = await check()
            // A pull-to-refresh check is on its way: look again later.
            if status == .loading { continue }
            // Gone (404) or revoked meanwhile: the banner says so, and
            // there is no first poll to wait for.
            guard let current = connection, current.isActive else { return false }
            if latest?.lastPolledAt != nil { return true }
            // Not polled yet, or this check failed (for example a moment
            // offline): try again after the gap.
        }
        return false
    }

    /// One quiet check for `waitForFirstPoll()`. Unlike `load()` it does
    /// not pass through `.loading`, so the banner and the import marker
    /// do not flicker. A failed check keeps the last answer. Returns the
    /// new answer, or nil if the check failed.
    private func check() async -> Connection? {
        do {
            let answer: Connection = try await api.get("/v1/me/connection")
            try Task.checkCancellation()
            status = .loaded(answer)
            return answer
        } catch APIError.notFound {
            status = .notConnected
            return nil
        } catch {
            // A 401 needs nothing here: RootView shows the connect screen.
            return nil
        }
    }

    /// The last answer, or nil while loading or after a failure.
    var connection: Connection? {
        if case .loaded(let connection) = status { return connection }
        return nil
    }

    var notice: Notice? {
        switch status {
        case .loaded(let connection):
            // Anything but "active" means collection has stopped, so an
            // unknown status reads as revoked rather than as fine.
            if !connection.isActive { return .revoked }
            if connection.reconnectSoon { return .reconnectSoon(by: connection.grantExpiresAt) }
            return nil
        case .notConnected:
            return .notConnected
        case .loading, .failed:
            return nil
        }
    }
}

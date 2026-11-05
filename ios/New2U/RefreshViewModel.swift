import Foundation
import Observation

/// "Sync now" in Settings, for `SpotifySyncRows` (WHO-96, WHO-118).
///
/// It asks the server to poll Spotify now (POST /v1/me/spotify/poll) and
/// keeps the cooldown the server reports. It keeps no timer of its own:
/// every time it shows comes from a server answer, so a relaunch starts
/// from what the server says.
///
/// The same shape as the other view models: the view reads the
/// properties below and calls the functions. Plain `async/await`.
@Observable
final class RefreshViewModel {
    enum Status: Equatable {
        case idle
        /// 202: the server will poll Spotify within seconds. "Checking Spotify…"
        case checking
        /// The request failed. The bar shows one line about it.
        case failed(APIError)
    }

    /// What the screen should do after `request()`.
    enum Answer: Equatable {
        /// 202: call `waitForPoll()`, reload, then `finishChecking()`.
        case accepted
        /// 429: nothing to reload; the button now shows the wait.
        case coolingDown
        /// 409: Spotify is not connected. Check the connection again,
        /// so the timeline shows the "Connect Spotify" banner.
        case notConnected
        /// Offline or another error; `status` says which.
        case failed
    }

    private(set) var status: Status = .idle
    /// The last successful poll, from GET /v1/me/connection.
    private(set) var lastPolledAt: Date?
    /// The button is disabled until this time. Nil means available now.
    private(set) var nextAllowedAt: Date?

    /// How long the server needs to start and finish a poll. docs/how-it-works.md
    /// says "reload after about 20 s". The tests set it to zero.
    var pollWait: Duration = .seconds(20)

    private let api: APIClient
    /// The current time. The tests pass a fixed one.
    private let now: () -> Date

    init(api: APIClient, now: @escaping () -> Date = Date.init) {
        self.api = api
        self.now = now
    }

    /// Takes the poll times from a connection answer.
    func show(_ connection: Connection) {
        lastPolledAt = connection.lastPolledAt
        nextAllowedAt = connection.nextManualPollAt
    }

    /// Seconds until the button works again, or nil when it works now.
    func secondsToWait(at date: Date) -> Int? {
        guard let nextAllowedAt, nextAllowedAt > date else { return nil }
        return Int(nextAllowedAt.timeIntervalSince(date).rounded(.up))
    }

    /// True when a tap would be sent: not checking and not cooling down.
    func canRefresh(at date: Date) -> Bool {
        status != .checking && secondsToWait(at: date) == nil
    }

    /// Asks the server to poll Spotify now.
    func request() async -> Answer {
        guard canRefresh(at: now()) else { return .coolingDown }
        status = .checking
        do {
            let answer: PollRequested = try await api.post("/v1/me/spotify/poll")
            nextAllowedAt = answer.nextAllowedAt
            return .accepted
        } catch APIError.rateLimited(let seconds) {
            // The per-user request limit may leave Retry-After out.
            nextAllowedAt = now().addingTimeInterval(TimeInterval(seconds ?? 60))
            status = .idle
            return .coolingDown
        } catch APIError.conflict {
            status = .idle
            return .notConnected
        } catch is CancellationError {
            status = .idle
            return .failed
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            status = .failed(error)
            return .failed
        } catch {
            status = .failed(.badResponse)
            return .failed
        }
    }

    /// Waits while the server polls Spotify. Kept apart from `request()`
    /// so the screen decides what to reload afterwards.
    func waitForPoll() async {
        try? await Task.sleep(for: pollWait)
    }

    /// Ends "Checking Spotify…" after the reload.
    func finishChecking() {
        if status == .checking { status = .idle }
    }
}

/// 202 from POST /v1/me/spotify/poll (WHO-95).
struct PollRequested: Decodable, Equatable {
    let requestedAt: Date
    let nextAllowedAt: Date
}

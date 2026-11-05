import Foundation
import Observation

/// Search, all time: the albums, singles, EPs and songs whose name or
/// artists, or for an album the user's comment, hold every word typed.
/// So a comment finds its album, and an album shows its comment.
///
/// `text` is the search field. `search()` waits a moment after the last
/// keystroke, then asks the server; the view runs it again on every
/// change, which cancels the one before. The last answer stays on screen
/// while the next one loads.
///
/// The answer lives only in memory. Nothing is written to disk (S6.5d).
@Observable
final class SearchViewModel {
    enum Status: Equatable {
        /// Nothing typed yet.
        case idle
        /// The answer is on its way.
        case loading
        case loaded
        /// More than the server searches: over 200 characters or 10 words.
        case tooLong
        /// The request failed. The screen shows the message and a retry.
        case failed(APIError)
    }

    /// The server's bounds on a query.
    static let maxLength = 200
    static let maxWords = 10

    var text = ""
    private(set) var status: Status = .idle
    private(set) var answer: SearchAnswer?
    /// The words `answer` was found for, to highlight them in the rows.
    private(set) var answeredWords: [String] = []

    /// Runs after an album opened from here was rated or got a comment,
    /// so the other lists show it. `SignedInView` sets it.
    var reloadAfterRating: () async -> Void = {}

    private let api: APIClient
    private let debounce: Duration

    init(api: APIClient, debounce: Duration = .milliseconds(300)) {
        self.api = api
        self.debounce = debounce
    }

    /// The words of `text`, split on whitespace.
    static func words(_ text: String) -> [String] {
        text.split(whereSeparator: \.isWhitespace).map(String.init)
    }

    /// Waits `debounce`, then searches for `text`. A blank field clears
    /// the answer without asking the server.
    func search() async {
        let words = Self.words(text)
        if words.isEmpty {
            answer = nil
            answeredWords = []
            status = .idle
            return
        }
        if text.count > Self.maxLength || words.count > Self.maxWords {
            status = .tooLong
            return
        }
        do {
            try await Task.sleep(for: debounce)
        } catch {
            return  // the next keystroke's search runs instead
        }
        await load(words)
    }

    /// Asks again for the words on screen, right away.
    func reload() async {
        guard !answeredWords.isEmpty else { return }
        await load(answeredWords)
    }

    private func load(_ words: [String]) async {
        status = .loading
        do {
            let query = [URLQueryItem(name: "q", value: words.joined(separator: " "))]
            let answer: SearchAnswer = try await api.get("/v1/search", query: query)
            try Task.checkCancellation()
            self.answer = answer
            answeredWords = words
            status = .loaded
        } catch is CancellationError {
            return
        } catch let error as APIError {
            status = .failed(error)
        } catch {
            status = .failed(.badResponse)
        }
    }

    /// A model for one found album's screen. Search is all time, and the
    /// album screen shows one period, so it shows the 366 days that end
    /// with the album's last play, `lastPlayedLocalDate`.
    func makeAlbumTracks(albumId: String, lastPlayedLocalDate: String) -> AlbumTracksViewModel {
        let first = Self.yearBefore(lastPlayedLocalDate) ?? lastPlayedLocalDate
        let model = AlbumTracksViewModel(
            api: api, albumId: albumId, query: .custom(from: first, to: lastPlayedLocalDate),
            basePath: "/v1/albums")
        model.ratingChanged = { [weak self] in
            await self?.reload()
            await self?.reloadAfterRating()
        }
        return model
    }

    /// A model for a found song's comment. Once saved, the search runs
    /// again, so the row shows it.
    func trackComment(_ track: FoundTrack) -> TrackCommentModel {
        let model = TrackCommentModel(api: api, itemKey: track.itemKey, title: track.title, comment: track.comment)
        model.changed = { [weak self] _ in await self?.reload() }
        return model
    }

    /// "2025-10-05" for "2026-10-05": the first day of the 366 days
    /// ending on `localDate`, the longest custom range the server takes.
    static func yearBefore(_ localDate: String) -> String? {
        let utc = TimeZone(identifier: "UTC")!
        guard let date = DisplayFormat.middayOf(localDate: localDate, timeZone: utc) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = utc
        guard let first = calendar.date(byAdding: .day, value: -365, to: date) else { return nil }
        return DisplayFormat.localDate(first, timeZone: utc)
    }
}

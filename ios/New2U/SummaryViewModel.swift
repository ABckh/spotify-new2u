import Foundation
import Observation

/// Loads the totals for one period, for `SummaryHeader`.
///
/// The same shape as `TimelineViewModel`: the view reads the properties
/// below and calls `load()`. Plain `async/await`.
@Observable
final class SummaryViewModel {
    enum Status: Equatable {
        /// The answer is on its way. The header keeps the previous
        /// period's numbers if it has them, or shows placeholders.
        case loading
        case loaded
        /// The request failed. The header shows a one-line note.
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    /// The last answer that arrived. Kept while the next one loads, so
    /// the header never jumps or clears (design bar).
    private(set) var summary: Summary?

    var query: PeriodQuery

    private let api: APIClient

    /// The header shows one top artist and one top track, so that is all
    /// we ask for (WHO-13 R-36 allows 1..50).
    static let topCount = 1

    init(api: APIClient, query: PeriodQuery = PeriodQuery(kind: .week)) {
        self.api = api
        self.query = query
    }

    func load() async {
        status = .loading
        do {
            let items = query.queryItems + [URLQueryItem(name: "top", value: String(Self.topCount))]
            let answer: Summary = try await api.get("/v1/summary", query: items)
            try Task.checkCancellation()
            summary = answer
            status = .loaded
        } catch is CancellationError {
            return
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            // The old numbers go, so they are never shown under a
            // different period's dates.
            summary = nil
            status = .failed(error)
        } catch {
            summary = nil
            status = .failed(.badResponse)
        }
    }
}

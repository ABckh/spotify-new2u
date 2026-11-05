import Foundation
import Observation

/// The custom-range sheet: two dates, and a check with the server
/// before the range is used.
///
/// The server decides whether a range is valid (WHO-13 R-22). If it
/// refuses, the sheet stays open with the user's dates and shows the
/// reason inline; the results behind the sheet are not touched (R-23).
@Observable
final class CustomRangeViewModel: Identifiable {
    /// The dates in the two pickers. They are shown and read in the
    /// display zone, so the date the user picks is the date we send.
    var from: Date
    var to: Date

    /// The reason the last check failed, shown under the pickers.
    private(set) var message: String?
    private(set) var isChecking = false

    let timeZone: TimeZone
    private let api: APIClient

    init(api: APIClient, from: Date, to: Date, timeZone: TimeZone) {
        self.api = api
        self.from = from
        self.to = to
        self.timeZone = timeZone
    }

    /// Opens on the period already on screen, or on today when there is
    /// none yet.
    convenience init(api: APIClient, current period: ResolvedPeriod?) {
        let zone = period.flatMap { TimeZone(identifier: $0.timeZone) } ?? .current
        let from = period.flatMap { DisplayFormat.middayOf(localDate: $0.localStartDate, timeZone: zone) } ?? Date()
        let to = period.flatMap { DisplayFormat.middayOf(localDate: $0.localEndDate, timeZone: zone) } ?? Date()
        self.init(api: api, from: from, to: to, timeZone: zone)
    }

    /// The range as the server reads it: two "YYYY-MM-DD" dates.
    var query: PeriodQuery {
        .custom(
            from: DisplayFormat.localDate(from, timeZone: timeZone),
            to: DisplayFormat.localDate(to, timeZone: timeZone))
    }

    /// Asks the server for this range. Returns the query to show when the
    /// server accepts it, or nil (with `message` set) when it does not.
    func check() async -> PeriodQuery? {
        let query = query
        isChecking = true
        message = nil
        defer { isChecking = false }
        do {
            let items = query.queryItems + [URLQueryItem(name: "top", value: "1")]
            let _: Summary = try await api.get("/v1/summary", query: items)
            return query
        } catch is CancellationError {
            return nil
        } catch let error as APIError {
            message = Messages.customRange(error)
            return nil
        } catch {
            message = Messages.customRange(.badResponse)
            return nil
        }
    }
}

import XCTest
@testable import New2U

/// The period picker's side of WHO-13: what goes on the wire, and how
/// the server's echo is turned into the range text.
@MainActor
final class PeriodTests: XCTestCase {
    private let warsaw = TimeZone(identifier: "Europe/Warsaw")!
    private let british = Locale(identifier: "en_GB")

    func testDayWeekMonthSendPeriodAndOffsetOnly() {
        XCTAssertEqual(query(PeriodQuery(kind: .day)), "period=day&offset=0")
        XCTAssertEqual(query(PeriodQuery(kind: .week, offset: -1)), "period=week&offset=-1")
        XCTAssertEqual(query(PeriodQuery(kind: .month, offset: -12)), "period=month&offset=-12")
    }

    func testCustomSendsBothDatesAndNoOffset() {
        XCTAssertEqual(
            query(.custom(from: "2026-04-13", to: "2026-04-19")),
            "period=custom&from=2026-04-13&to=2026-04-19")
    }

    /// T-26 / R-8: no client clock and no client zone on any request.
    func testNoClientClockOrZoneIsSent() {
        let queries = PeriodQuery.Kind.allCases.map { kind in
            kind == .custom ? PeriodQuery.custom(from: "2026-01-01", to: "2026-01-01") : PeriodQuery(kind: kind)
        }
        for item in queries.flatMap(\.queryItems) {
            XCTAssertTrue(["period", "offset", "from", "to"].contains(item.name), item.name)
        }
    }

    func testPickerOrderIsDayWeekMonthCustom() {
        XCTAssertEqual(PeriodQuery.Kind.allCases, [.day, .week, .month, .custom])
    }

    // MARK: - Range text, only from the echo (R-44, R-45)

    func testWeekRangeText() {
        let text = DisplayFormat.periodRange(period("week", "2026-10-05", "2026-10-11"), locale: british, now: october20)
        // The locale decides the spacing around the dash, so compare
        // with the spaces taken out.
        let compact = text.unicodeScalars.filter { !CharacterSet.whitespaces.contains($0) }
        XCTAssertEqual(String(String.UnicodeScalarView(compact)), "5–11Oct", text)
    }

    func testMonthRangeText() {
        let text = DisplayFormat.periodRange(period("month", "2026-10-01", "2026-10-31"), locale: british, now: october20)
        XCTAssertEqual(text, "October 2026")
    }

    func testDayRangeTextUsesTodayInTheDisplayZone() {
        let text = DisplayFormat.periodRange(period("day", "2026-10-20", "2026-10-20"), locale: british, now: october20)
        XCTAssertEqual(text, "Today")
    }

    func testRangeOutsideThisYearShowsTheYear() {
        let text = DisplayFormat.periodRange(period("custom", "2025-12-29", "2026-01-04"), locale: british, now: october20)
        XCTAssertTrue(text.contains("2025") && text.contains("2026"), text)
    }

    /// A date picked at 00:30 in Warsaw is still that Warsaw date, even
    /// though it is the day before in UTC.
    func testPickedDateIsReadInTheDisplayZone() throws {
        let picked = try XCTUnwrap(APIClient.parseInstant("2026-04-12T22:30:00.000Z"))
        XCTAssertEqual(DisplayFormat.localDate(picked, timeZone: warsaw), "2026-04-13")
    }

    /// The four R-22 texts, word for word.
    func testRefusedRangeMessages() {
        XCTAssertEqual(Messages.customRange(.invalidRequest(code: "period_range_reversed")), "The end date is before the start date.")
        XCTAssertEqual(Messages.customRange(.invalidRequest(code: "period_range_too_long")), "Pick a range of 366 days or less.")
        XCTAssertEqual(Messages.customRange(.invalidRequest(code: "period_invalid_date")), "That date isn't valid.")
        XCTAssertEqual(Messages.customRange(.invalidRequest(code: "period_range_incomplete")), "Pick both a start and an end date.")
    }

    // MARK: - Helpers

    private var october20: Date { APIClient.parseInstant("2026-10-20T10:00:00.000Z")! }

    private func query(_ query: PeriodQuery) -> String? {
        var components = URLComponents()
        components.queryItems = query.queryItems
        return components.query
    }

    private func period(_ type: String, _ first: String, _ last: String) -> ResolvedPeriod {
        ResolvedPeriod(
            type: type, offset: nil, anchor: nil, timeZone: "Europe/Warsaw", weekStart: "monday",
            start: Date(timeIntervalSince1970: 0), end: Date(timeIntervalSince1970: 0),
            localStartDate: first, localEndDate: last, isPartial: false)
    }
}

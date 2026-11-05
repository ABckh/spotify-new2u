import XCTest
@testable import New2U

@MainActor
final class DisplayFormatTests: XCTestCase {
    private let warsaw = TimeZone(identifier: "Europe/Warsaw")!
    private let newYork = TimeZone(identifier: "America/New_York")!
    private let british = Locale(identifier: "en_GB")

    /// WHO-13 R-6, R-7: a play at 00:30 in the display zone sits under
    /// the 12 October header and shows 00:30, even if the phone is in
    /// New York, where it was still 11 October.
    func testPlayNearMidnightMatchesItsDayHeader() throws {
        // 2026-10-11T22:30Z is 00:30 on 12 October in Warsaw (UTC+2).
        let played = try XCTUnwrap(APIClient.parseInstant("2026-10-11T22:30:00.000Z"))
        let now = try XCTUnwrap(APIClient.parseInstant("2026-10-20T10:00:00.000Z"))

        XCTAssertEqual(DisplayFormat.clockTime(played, timeZone: warsaw, locale: british), "00:30")
        XCTAssertEqual(
            DisplayFormat.dayHeader(localDate: "2026-10-12", timeZone: warsaw, locale: british, now: now),
            "Monday 12 October")
        // The device zone would have given a different clock time.
        XCTAssertEqual(DisplayFormat.clockTime(played, timeZone: newYork, locale: british), "18:30")
    }

    func testTodayAndYesterdayAreReadInTheDisplayZone() throws {
        // 23:30 UTC on the 11th is already the 12th in Warsaw.
        let now = try XCTUnwrap(APIClient.parseInstant("2026-10-11T23:30:00.000Z"))
        XCTAssertEqual(DisplayFormat.dayHeader(localDate: "2026-10-12", timeZone: warsaw, locale: british, now: now), "Today")
        XCTAssertEqual(DisplayFormat.dayHeader(localDate: "2026-10-11", timeZone: warsaw, locale: british, now: now), "Yesterday")
    }

    func testClockTimeFollowsLocale() throws {
        let played = try XCTUnwrap(APIClient.parseInstant("2026-10-11T19:14:00.000Z"))
        let text = DisplayFormat.clockTime(played, timeZone: warsaw, locale: Locale(identifier: "en_US"))
        XCTAssertTrue(text.hasPrefix("9:14"), text)
        XCTAssertTrue(text.contains("PM"), text)
    }

    func testDurations() {
        XCTAssertEqual(DisplayFormat.duration(milliseconds: 222_000, locale: british), "3m 42s")
        XCTAssertEqual(DisplayFormat.total(milliseconds: 11_520_000, locale: british), "3h 12m")
        XCTAssertEqual(DisplayFormat.spokenDuration(milliseconds: 222_000, locale: british), "3 minutes, 42 seconds")
    }

    func testBadLocalDateFallsBackToTheRawText() {
        XCTAssertEqual(DisplayFormat.dayHeader(localDate: "not-a-date", timeZone: warsaw), "not-a-date")
    }
}

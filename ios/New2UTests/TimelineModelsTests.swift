import XCTest
@testable import New2U

@MainActor
final class TimelineModelsTests: XCTestCase {
    func testPeriodQueryItems() {
        let query = PeriodQuery(kind: .week, offset: -1)
        XCTAssertEqual(query.queryItems.map(\.description), ["period=week", "offset=-1"])
    }

    /// The example answer from api-contract section 3.1 decodes field by field.
    func testDecodesContractExample() throws {
        let page = try APIClient.makeDecoder().decode(TimelinePage.self, from: Data(TimelineFixtures.contractExample.utf8))
        XCTAssertEqual(page.period.timeZone, "Europe/Warsaw")
        XCTAssertEqual(page.period.localStartDate, "2026-10-05")
        XCTAssertEqual(page.period.localEndDate, "2026-10-11")
        XCTAssertNil(page.period.anchor)
        let event = try XCTUnwrap(page.events.first)
        XCTAssertEqual(event.localDate, "2026-10-11")
        XCTAssertEqual(event.durationMs, 200_000)
        XCTAssertNil(event.playedMs)
        XCTAssertEqual(event.creators, ["Artist A", "Artist B"])
        XCTAssertEqual(page.nextCursor, "opaque string")
        XCTAssertFalse(page.isEmpty)
    }

    func testGroupsByLocalDateKeepingServerOrder() {
        let events = [
            TimelineFixtures.event("a", localDate: "2026-10-12", durationMs: 60_000),
            TimelineFixtures.event("b", localDate: "2026-10-12", durationMs: 120_000),
            TimelineFixtures.event("c", localDate: "2026-10-11", durationMs: 30_000),
        ]
        let days = DayGroup.make(from: events, hasMorePages: false)
        XCTAssertEqual(days.map(\.localDate), ["2026-10-12", "2026-10-11"])
        XCTAssertEqual(days[0].events.map(\.id), ["a", "b"])
        XCTAssertEqual(days[0].totalMs, 180_000)
        XCTAssertEqual(days[1].totalMs, 30_000)
    }

    /// The last day may continue on the next page, so it has no total yet.
    func testLastDayHasNoTotalWhileMorePagesExist() {
        let events = [
            TimelineFixtures.event("a", localDate: "2026-10-12", durationMs: 60_000),
            TimelineFixtures.event("b", localDate: "2026-10-11", durationMs: 60_000),
        ]
        let days = DayGroup.make(from: events, hasMorePages: true)
        XCTAssertEqual(days[0].totalMs, 60_000)
        XCTAssertNil(days[1].totalMs)
    }

    /// A play with no duration adds 0, as the summary does (R-25, R-27).
    func testMissingDurationCountsAsZero() {
        let events = [
            TimelineFixtures.event("a", localDate: "2026-10-12", durationMs: nil),
            TimelineFixtures.event("b", localDate: "2026-10-12", durationMs: 90_000),
        ]
        XCTAssertEqual(DayGroup.make(from: events, hasMorePages: false)[0].totalMs, 90_000)
    }
}

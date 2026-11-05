import XCTest
@testable import New2U

/// The "Collection started …" row on the timeline (WHO-90 T1, WHO-92).
/// Synthetic plays only. The fixture week is 5 to 11 October 2026 in
/// Europe/Warsaw (UTC+2), and live collection starts on 9 October at
/// 14:30 local time (12:30 UTC).
@MainActor
final class ImportMarkerTests: XCTestCase {
    private let collectionStart = Date(timeIntervalSince1970: 1_791_549_000)  // 2026-10-09T12:30:00Z
    private var store: SessionStore!
    private var model: TimelineViewModel!

    // MARK: - Where the row goes (DayGroup.make)

    func testMarkerSitsBetweenPlaysAfterAndBeforeIt() {
        let events = [
            play("live2", at: "2026-10-09T16:00:00Z", localDate: "2026-10-09"),
            play("live1", at: "2026-10-09T12:31:00Z", localDate: "2026-10-09"),
            play("old1", at: "2026-10-09T09:00:00Z", localDate: "2026-10-09"),
            play("old2", at: "2026-10-08T20:00:00Z", localDate: "2026-10-08"),
        ]
        let days = DayGroup.make(from: events, hasMorePages: false, marker: marker)

        XCTAssertEqual(days.map(\.localDate), ["2026-10-09", "2026-10-08"])
        XCTAssertEqual(days[0].markerIndex, 2)
        XCTAssertEqual(days[0].eventsAboveMarker.map(\.id), ["live2", "live1"])
        XCTAssertEqual(days[0].eventsBelowMarker.map(\.id), ["old1"])
        XCTAssertNil(days[1].markerIndex)
        XCTAssertEqual(days[1].eventsAboveMarker.map(\.id), ["old2"])
        // The server's order is kept exactly; the marker only adds a row.
        XCTAssertEqual(days.flatMap(\.events).map(\.id), events.map(\.id))
    }

    func testMarkerGoesByTimeNotByHowThePlayWasCollected() {
        // A play at the very moment collection started counts as live.
        let events = [
            play("same", at: "2026-10-09T12:30:00Z", localDate: "2026-10-09"),
            play("old", at: "2026-10-09T12:29:59Z", localDate: "2026-10-09"),
        ]
        let days = DayGroup.make(from: events, hasMorePages: false, marker: marker)
        XCTAssertEqual(days[0].markerIndex, 1)
    }

    func testMarkerGetsADayOfItsOwnWhenNoPlayLoadedOnItsDate() {
        let events = [
            play("live", at: "2026-10-10T08:00:00Z", localDate: "2026-10-10"),
            play("old", at: "2026-10-07T20:00:00Z", localDate: "2026-10-07"),
        ]
        let days = DayGroup.make(from: events, hasMorePages: false, marker: marker)

        XCTAssertEqual(days.map(\.localDate), ["2026-10-10", "2026-10-09", "2026-10-07"])
        XCTAssertEqual(days[1].markerIndex, 0)
        XCTAssertTrue(days[1].events.isEmpty)
        XCTAssertNil(days[1].totalMs, "a day with no plays shows no total")
    }

    func testMarkerWaitsUntilItsSpotHasLoaded() {
        // Only plays after the marker so far, and more pages to come:
        // the next page could still hold plays from before 14:30 on 9 October.
        let newer = [play("live", at: "2026-10-10T08:00:00Z", localDate: "2026-10-10")]
        let waiting = DayGroup.make(from: newer, hasMorePages: true, marker: marker)
        XCTAssertEqual(waiting.map(\.localDate), ["2026-10-10"])
        XCTAssertNil(waiting[0].markerIndex)

        // Once an older play has loaded, the spot is final.
        let more = newer + [play("old", at: "2026-10-09T09:00:00Z", localDate: "2026-10-09")]
        let placed = DayGroup.make(from: more, hasMorePages: true, marker: marker)
        XCTAssertEqual(placed[1].localDate, "2026-10-09")
        XCTAssertEqual(placed[1].markerIndex, 0)
    }

    func testNoMarkerLeavesDaysUnchanged() {
        let events = [play("a", at: "2026-10-09T16:00:00Z", localDate: "2026-10-09")]
        let days = DayGroup.make(from: events, hasMorePages: false, marker: nil)
        XCTAssertNil(days[0].markerIndex)
        XCTAssertEqual(days[0].eventsAboveMarker.map(\.id), ["a"])
        XCTAssertTrue(days[0].eventsBelowMarker.isEmpty)
    }

    // MARK: - When the row shows (TimelineViewModel.importMarker)

    func testShownWhenPlaysWereImportedBeforeCollectionStarted() async {
        await load(coverageStart: "2026-10-08T20:00:00Z")
        model.connection = connection(imported: true, hasImportedPlays: true)

        XCTAssertEqual(model.importMarker, marker)
        XCTAssertEqual(model.days.first { $0.markerIndex != nil }?.localDate, "2026-10-09")
    }

    func testHiddenWhenTheUserDidNotImport() async {
        await load(coverageStart: "2026-10-08T20:00:00Z")
        model.connection = connection(imported: false, hasImportedPlays: false)

        XCTAssertNil(model.importMarker)
        XCTAssertTrue(model.days.allSatisfy { $0.markerIndex == nil })
    }

    func testHiddenWhenTheImportRanButFoundNoEarlierPlay() async {
        // WHO-93 F2: the import succeeded with 0 earlier plays, so
        // history_imported_at is set but has_imported_plays is false.
        // The flag alone decides, even with an older play in the period.
        await load(coverageStart: "2026-10-08T20:00:00Z")
        model.connection = connection(imported: true, hasImportedPlays: false)

        XCTAssertNil(model.importMarker)
        XCTAssertTrue(model.days.allSatisfy { $0.markerIndex == nil })
    }

    func testHiddenWhenAnOlderServerLeavesTheFlagOut() async {
        await load(coverageStart: "2026-10-08T20:00:00Z")
        model.connection = connection(imported: true, hasImportedPlays: nil)

        XCTAssertNil(model.importMarker)
    }

    func testHiddenWhenNoPlayIsOlderThanCollectionStart() async {
        // The import ran but Spotify had no plays: the earliest stored
        // play is a live one.
        await load(coverageStart: "2026-10-09T12:31:00Z")
        model.connection = connection(imported: true, hasImportedPlays: true)

        XCTAssertNil(model.importMarker)
        XCTAssertTrue(model.days.allSatisfy { $0.markerIndex == nil })
    }

    func testHiddenInAPeriodThatDoesNotContainCollectionStart() async {
        await load(coverageStart: "2026-10-08T20:00:00Z")
        // Collection started a month later, outside the fixture week.
        model.connection = connection(imported: true, hasImportedPlays: true, startedAt: collectionStart.addingTimeInterval(30 * 86_400))

        XCTAssertNil(model.importMarker)
    }

    func testHiddenUntilTheConnectionCheckAnswers() async {
        await load(coverageStart: "2026-10-08T20:00:00Z")
        XCTAssertNil(model.importMarker)
        XCTAssertTrue(model.days.allSatisfy { $0.markerIndex == nil })
    }

    func testMarkerTextIsT1WordForWord() throws {
        let warsaw = try XCTUnwrap(TimeZone(identifier: "Europe/Warsaw"))
        let time = DisplayFormat.clockTime(collectionStart, timeZone: warsaw, locale: Locale(identifier: "en_GB"))
        XCTAssertEqual(
            Messages.importMarker(time: time),
            "Collection started 14:30. Earlier plays were imported from Spotify.")
    }

    func testConnectionDecodesTheImportFields() throws {
        let json = """
        {"status": "active", "reauth_reason": null, "spotify_display_name": "Synthetic",
         "grant_expires_at": "2027-04-01T00:00:00Z", "reconnect_soon": false, "last_polled_at": null,
         "collection_started_at": "2026-10-09T12:30:00Z", "history_imported_at": "2026-10-09T12:30:20Z",
         "has_imported_plays": true}
        """
        let decoded = try APIClient.makeDecoder().decode(Connection.self, from: Data(json.utf8))
        XCTAssertEqual(decoded.collectionStartedAt, collectionStart)
        XCTAssertEqual(decoded.historyImportedAt, collectionStart.addingTimeInterval(20))
        XCTAssertEqual(decoded.hasImportedPlays, true)
        XCTAssertTrue(decoded.showsImportMarker)
    }

    func testConnectionWithoutTheFlagDecodesAsNoMarker() throws {
        let json = """
        {"status": "active", "reauth_reason": null, "spotify_display_name": "Synthetic",
         "grant_expires_at": "2027-04-01T00:00:00Z", "reconnect_soon": false, "last_polled_at": null,
         "collection_started_at": "2026-10-09T12:30:00Z", "history_imported_at": "2026-10-09T12:30:20Z"}
        """
        let decoded = try APIClient.makeDecoder().decode(Connection.self, from: Data(json.utf8))
        XCTAssertNil(decoded.hasImportedPlays)
        XCTAssertFalse(decoded.showsImportMarker)
    }

    // MARK: - Helpers

    private var marker: ImportMarker {
        ImportMarker(at: collectionStart, localDate: "2026-10-09")
    }

    private func play(_ id: String, at instant: String, localDate: String) -> TimelineEvent {
        TimelineEvent(
            id: id, source: "spotify", kind: "listen",
            occurredAt: ISO8601DateFormatter().date(from: instant)!, occurredAtPrecision: "second",
            localDate: localDate, durationMs: 60_000, playedMs: nil,
            title: "Track \(id)", creators: ["Artist"], collection: nil,
            artworkUrl: nil, itemKey: "id:\(id)")
    }

    /// `hasImportedPlays` is the server's flag: nil stands for a server
    /// that leaves the key out.
    private func connection(imported: Bool, hasImportedPlays: Bool?, startedAt: Date? = nil) -> Connection {
        let started = startedAt ?? collectionStart
        return Connection(
            status: "active", reauthReason: nil, spotifyDisplayName: "Synthetic",
            grantExpiresAt: nil, reconnectSoon: false, lastPolledAt: nil, nextManualPollAt: nil,
            collectionStartedAt: started,
            historyImportedAt: imported ? started.addingTimeInterval(20) : nil,
            hasImportedPlays: hasImportedPlays)
    }

    /// Loads one page of the fixture week: two plays after collection
    /// started on 9 October, one before it that day, one on 8 October.
    private func load(coverageStart: String) async {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        self.store = store
        let api = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        model = TimelineViewModel(api: api, query: PeriodQuery(kind: .week, offset: -1))
        addTeardownBlock { @MainActor in store.deleteToken() }

        let plays = [
            ("live2", "2026-10-09T16:00:00.000Z", "2026-10-09"),
            ("live1", "2026-10-09T12:31:00.000Z", "2026-10-09"),
            ("old1", "2026-10-09T09:00:00.000Z", "2026-10-09"),
            ("old2", "2026-10-08T20:00:00.000Z", "2026-10-08"),
        ].map { id, instant, localDate in
            """
            {"id": "\(id)", "source": "spotify", "kind": "listen",
             "occurred_at": "\(instant)", "occurred_at_precision": "second",
             "local_date": "\(localDate)", "duration_ms": 60000, "played_ms": null,
             "title": "Track", "creators": ["Artist"], "collection": null,
             "artwork_url": null, "item_key": "id:\(id)"}
            """
        }
        let body = """
        {"period": \(TimelineFixtures.period), "events": [\(plays.joined(separator: ","))],
         "next_cursor": null, "is_empty": false, "empty_reason": null,
         "coverage_start": "\(coverageStart)"}
        """
        StubURLProtocol.handler = { _ in .init(body: body) }
        await model.load()
        XCTAssertEqual(model.status, .loaded)
    }
}

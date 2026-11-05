import Foundation
@testable import New2U

/// Synthetic timeline data for the tests. No real listening history.
enum TimelineFixtures {
    static func event(_ id: String, localDate: String, durationMs: Int?) -> TimelineEvent {
        TimelineEvent(
            id: id, source: "spotify", kind: "listen",
            occurredAt: Date(timeIntervalSince1970: 0), occurredAtPrecision: "second",
            localDate: localDate, durationMs: durationMs, playedMs: nil,
            title: "Track \(id)", creators: ["Artist"], collection: nil,
            artworkUrl: nil, itemKey: "id:\(id)")
    }

    static let period = """
    {"type": "week", "offset": -1, "anchor": null, "time_zone": "Europe/Warsaw",
     "week_start": "monday", "start": "2026-10-04T22:00:00.000Z", "end": "2026-10-11T22:00:00.000Z",
     "local_start_date": "2026-10-05", "local_end_date": "2026-10-11", "is_partial": false}
    """

    /// The example from api-contract section 3.1.
    static let contractExample = """
    {"period": \(period),
     "events": [{
       "id": "0327f377-9004-47eb-92f4-bd0ccb82c802", "source": "spotify", "kind": "listen",
       "occurred_at": "2026-10-11T08:00:00.000Z", "occurred_at_precision": "second",
       "local_date": "2026-10-11", "duration_ms": 200000, "played_ms": null,
       "title": "Track title", "creators": ["Artist A", "Artist B"], "collection": "Album title",
       "artwork_url": "https://i.scdn.co/image/x", "item_key": "id:4uLU6hMCjMI75M1A2tKUQC"}],
     "next_cursor": "opaque string", "is_empty": false, "empty_reason": null,
     "coverage_start": "2026-10-01T06:12:45.000Z"}
    """

    /// A page of `count` plays on one date, ids "<prefix>0", "<prefix>1", ...
    static func page(prefix: String, count: Int, localDate: String = "2026-10-11", nextCursor: String?) -> String {
        page(days: [(prefix, count, localDate)], nextCursor: nextCursor)
    }

    /// A page with plays on several dates, newest day first: `count` plays
    /// per day, ids "<prefix>0", "<prefix>1", ...
    static func page(days: [(prefix: String, count: Int, localDate: String)], nextCursor: String?) -> String {
        let events = days.flatMap { day in
            (0..<day.count).map { index in
                """
                {"id": "\(day.prefix)\(index)", "source": "spotify", "kind": "listen",
                 "occurred_at": "2026-10-11T08:00:00.000Z", "occurred_at_precision": "second",
                 "local_date": "\(day.localDate)", "duration_ms": 60000, "played_ms": null,
                 "title": "Track", "creators": ["Artist"], "collection": null,
                 "artwork_url": null, "item_key": "id:\(day.prefix)\(index)"}
                """
            }
        }
        let count = events.count
        let cursor = nextCursor.map { "\"\($0)\"" } ?? "null"
        return """
        {"period": \(period), "events": [\(events.joined(separator: ","))],
         "next_cursor": \(cursor), "is_empty": \(count == 0), "empty_reason": \(count == 0 ? "\"no_plays\"" : "null"),
         "coverage_start": "2026-10-01T06:12:45.000Z"}
        """
    }
}

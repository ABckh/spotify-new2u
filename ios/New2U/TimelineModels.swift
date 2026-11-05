import Foundation

/// Which period the user asked for. Turned into query items exactly as
/// the api-contract section 3.0 describes. The app never computes a
/// boundary itself (WHO-13 R-37): it sends `period` and `offset` (or
/// the two dates of a custom range), and the server answers with the
/// resolved dates. No device clock or device zone is ever sent (R-8).
struct PeriodQuery: Equatable {
    /// In the order the period picker shows them (design bar).
    enum Kind: String, CaseIterable {
        case day, week, month, custom
    }

    var kind: Kind
    /// 0 = the period containing today, -1 = the one before, +1 = after.
    /// Not used for `custom`.
    var offset: Int = 0
    /// First and last included date of a custom range, "YYYY-MM-DD".
    /// Only used for `custom`.
    var from: String?
    var to: String?

    /// A custom range of whole local dates, both included (R-19).
    static func custom(from: String, to: String) -> PeriodQuery {
        PeriodQuery(kind: .custom, from: from, to: to)
    }

    var queryItems: [URLQueryItem] {
        if kind == .custom {
            return [
                URLQueryItem(name: "period", value: kind.rawValue),
                URLQueryItem(name: "from", value: from ?? ""),
                URLQueryItem(name: "to", value: to ?? ""),
            ]
        }
        return [
            URLQueryItem(name: "period", value: kind.rawValue),
            URLQueryItem(name: "offset", value: String(offset)),
        ]
    }
}

/// The resolved period the server echoes on every timeline and summary
/// answer (api-contract section 3.0).
struct ResolvedPeriod: Decodable, Equatable {
    let type: String
    let offset: Int?
    let anchor: String?
    /// The stored display zone. Every time and date on screen uses it.
    let timeZone: String
    let weekStart: String
    let start: Date
    /// Exclusive.
    let end: Date
    /// First local date, "YYYY-MM-DD".
    let localStartDate: String
    /// Last included local date, "YYYY-MM-DD".
    let localEndDate: String
    let isPartial: Bool
}

/// One play, exactly as `GET /v1/timeline` sends it (section 3.1).
struct TimelineEvent: Decodable, Equatable, Identifiable {
    let id: String
    let source: String
    let kind: String
    let occurredAt: Date
    /// "second", or "day" when only the date is known (then no time is shown).
    let occurredAtPrecision: String
    /// The local date in the display zone. The timeline groups by this.
    let localDate: String
    let durationMs: Int?
    /// Always nil for Spotify. Never derived.
    let playedMs: Int?
    let title: String
    let creators: [String]
    let collection: String?
    let artworkUrl: String?
    let itemKey: String
    /// The user's own comment on the track, or nil. The same on every
    /// play of the track.
    var comment: String?

    /// What this play adds to a total: played, else duration, else 0
    /// (WHO-13 R-25). The same rule the server uses for the summary.
    var countedMs: Int {
        playedMs ?? durationMs ?? 0
    }
}

/// One page of `GET /v1/timeline`.
struct TimelinePage: Decodable, Equatable {
    let period: ResolvedPeriod
    let events: [TimelineEvent]
    /// Send as `cursor` for the next page. nil = this was the last page.
    let nextCursor: String?
    let isEmpty: Bool
    /// "no_data_yet", "before_coverage", "collection_gap" or "no_plays".
    let emptyReason: String?
    let coverageStart: Date?
}

/// Where the timeline draws "Collection started …" (WHO-90 T1): the
/// moment live collection began, and that moment's local date in the
/// display zone, so the row goes under the right day header.
struct ImportMarker: Equatable {
    let at: Date
    let localDate: String
}

/// The plays of one local date, in the order the server sent them.
struct DayGroup: Identifiable, Equatable {
    let localDate: String
    var events: [TimelineEvent]
    /// The day's total listening time, or nil while some of the day's
    /// plays are still on a page that has not loaded yet.
    var totalMs: Int?
    /// Set on the one day that shows the import marker: how many of
    /// `events` go above it. Nil on every other day.
    var markerIndex: Int?

    var id: String { localDate }

    /// The plays above the marker (played at or after it). All of the
    /// day's plays when the day has no marker.
    var eventsAboveMarker: ArraySlice<TimelineEvent> {
        events[..<(markerIndex ?? events.count)]
    }

    /// The plays below the marker (played before it). Empty when the day
    /// has no marker.
    var eventsBelowMarker: ArraySlice<TimelineEvent> {
        events[(markerIndex ?? events.count)...]
    }

    /// Groups plays under their `local_date` (WHO-13 R-48), keeping the
    /// server's order inside each day and between days.
    ///
    /// The server sends newest first, so a day is complete once a play
    /// from an older day has arrived, or once there are no more pages.
    /// Only complete days get a total, so a header never shows a number
    /// that grows while you scroll.
    ///
    /// `marker`, when set, goes into the day of its local date, between
    /// the plays at or after it and the plays before it. It goes by time,
    /// not by how a play was collected (WHO-90 T1 rule 2). It is placed
    /// only once its spot is final: a play older than it has loaded, or
    /// there are no more pages. If no play loaded on its date, it gets a
    /// day of its own, with no total.
    static func make(from events: [TimelineEvent], hasMorePages: Bool, marker: ImportMarker? = nil) -> [DayGroup] {
        var groups: [DayGroup] = []
        for event in events {
            if let index = groups.firstIndex(where: { $0.localDate == event.localDate }) {
                groups[index].events.append(event)
            } else {
                groups.append(DayGroup(localDate: event.localDate, events: [event], totalMs: nil))
            }
        }
        for index in groups.indices {
            let isLastGroup = index == groups.count - 1
            if !isLastGroup || !hasMorePages {
                groups[index].totalMs = groups[index].events.reduce(0) { $0 + $1.countedMs }
            }
        }

        guard let marker else { return groups }
        let spotIsFinal = !hasMorePages || events.contains { $0.occurredAt < marker.at }
        guard spotIsFinal else { return groups }
        if let index = groups.firstIndex(where: { $0.localDate == marker.localDate }) {
            // The server sends newest first, so the plays at or after the
            // marker are the ones before the first older play.
            let above = groups[index].events.firstIndex { $0.occurredAt < marker.at }
            groups[index].markerIndex = above ?? groups[index].events.count
        } else {
            // Days are newest first: the marker's day goes before the
            // first older day ("YYYY-MM-DD" sorts as text).
            let index = groups.firstIndex { $0.localDate < marker.localDate } ?? groups.count
            groups.insert(DayGroup(localDate: marker.localDate, events: [], totalMs: nil, markerIndex: 0), at: index)
        }
        return groups
    }
}

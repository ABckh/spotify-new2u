import Foundation

/// The answer of `GET /v1/summary`, exactly as api-contract section 3.2
/// describes it. The server does all the counting; the app only shows it.
struct Summary: Decodable, Equatable {
    let period: ResolvedPeriod
    let totals: SummaryTotals
    /// At most `top` entries, never padded (WHO-13 R-36).
    let topTracks: [TopTrack]
    /// A play counts once for each of its artists, so these counts can
    /// add up to more than `totals.playCount`. Never show them as a
    /// share or percentage (R-34).
    let topArtists: [TopArtist]
    let isEmpty: Bool
    let emptyReason: String?
    let coverageStart: Date?
}

struct SummaryTotals: Decodable, Equatable {
    /// Plays in the period. Repeats count every time (R-28).
    let playCount: Int
    let distinctTracks: Int
    let distinctArtists: Int
    /// Full track lengths for Spotify, not measured listening (R-26).
    let totalMs: Int
    /// Plays that added nothing to `totalMs` because no length is known (R-27).
    let eventsWithoutDuration: Int
}

struct TopTrack: Decodable, Equatable {
    let itemKey: String
    let title: String
    let creators: [String]
    let playCount: Int
    let totalMs: Int
    /// The track's album cover on `i.scdn.co`, or nil (WHO-149).
    let artworkUrl: String?
}

struct TopArtist: Decodable, Equatable {
    let name: String
    let playCount: Int
    let totalMs: Int
}

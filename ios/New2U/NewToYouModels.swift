import Foundation

/// The answer of `GET /v1/new-to-you`, exactly as api-contract section
/// 3.9 describes it. The server decides what is new and in which order;
/// the app only shows it, in the order it arrives (S14.6).
struct NewToYou: Decodable, Equatable {
    let period: ResolvedPeriod
    /// Shown first, then the singles and EPs. The server still sends a
    /// deprecated `tracks` list; the app ignores it (WHO-115).
    let albums: NewAlbums
    /// What Spotify calls singles: singles and EPs, each with its
    /// `releaseType`. A single whose songs are all on a new album of the
    /// period is left out by the server, so a song shows once.
    let singles: NewAlbums
    /// The user's earliest stored play, as a "YYYY-MM-DD" date in the
    /// display zone. Shown in V1. nil only when nothing was ever stored.
    let coverageStartLocalDate: String?
    /// True: also show V2 (S14.9).
    let periodContainsCoverageStart: Bool
    /// True: the period has plays but nothing new. Show V3 instead of
    /// the three lists (S14.14).
    let nothingNew: Bool
    /// True: show V3. Also true while the server still counts new
    /// tracks: a period whose only new item is a track on a known album
    /// has `nothing_new: false` with no albums and no singles (WHO-116).
    var showsNothingNew: Bool {
        nothingNew || (albums.count == 0 && singles.count == 0)
    }

    /// True: hide the whole section; the summary shows the empty message (S14.15).
    let isEmpty: Bool
    let emptyReason: String?

    /// The stored display zone, for every date in the section.
    var zone: TimeZone {
        TimeZone(identifier: period.timeZone) ?? .current
    }

    /// This answer without the albums, singles and EPs just marked "Not
    /// new", with the counts lowered to match, while the server is asked
    /// (WHO-123). Song marks do not change these lists.
    func hiding(_ marks: Set<NotNewMark>) -> NewToYou {
        let albumIds = Set(marks.filter { $0.kind == .album }.map(\.itemId))
        return NewToYou(
            period: period,
            albums: albums.hiding(albumIds),
            singles: singles.hiding(albumIds),
            coverageStartLocalDate: coverageStartLocalDate,
            periodContainsCoverageStart: periodContainsCoverageStart,
            nothingNew: nothingNew,
            isEmpty: isEmpty,
            emptyReason: emptyReason)
    }
}

/// `count` is the full number of new items, even when `items` holds only
/// the first 10. The heading uses `count` ("23 new albums").
struct NewAlbums: Decodable, Equatable {
    let count: Int
    let items: [NewAlbum]

    /// Without the albums in `albumIds`, and `count` lowered to match.
    func hiding(_ albumIds: Set<String>) -> NewAlbums {
        let kept = items.filter { !albumIds.contains($0.albumId) }
        return NewAlbums(count: count - (items.count - kept.count), items: kept)
    }
}

/// What a release is. Spotify calls singles and EPs both "single"; the
/// server tells them apart by track count: 1 to 3 is a single, more an EP.
enum ReleaseType: String, Decodable, Equatable {
    case album, single, ep

    /// "Single" or "EP", in front of the artist on a row; nil for an album.
    var label: String? {
        switch self {
        case .album: return nil
        case .single: return String(localized: "Single")
        case .ep: return String(localized: "EP")
        }
    }
}

struct NewAlbum: Decodable, Equatable {
    /// Spotify's album ID. A deluxe edition is a different album (S14.4).
    let albumId: String
    /// May be null on the wire.
    let name: String?
    let artists: [String]
    /// "YYYY-MM-DD" in the display zone. Shown as is, never worked out
    /// on the phone from the instant.
    let firstPlayedLocalDate: String
    /// Distinct tracks of this album played before the period's end, over
    /// the whole history. The "new" rule counts these (WHO-153), so the
    /// row shows this, not the period count the server also sends.
    let tracksListened: Int
    /// The album's track count. Never null on a new album (WHO-155).
    let totalTracks: Int
    /// The album's cover on `i.scdn.co`, or nil (WHO-149).
    let artworkUrl: String?
    /// The user's own rating, 1 to 10, or nil when not rated.
    var rating: Int?
    /// The user's own comment, or nil. Independent of the rating.
    var comment: String?
    /// `single` or `ep` in the singles list; nil (an album) in the albums list.
    var releaseType: ReleaseType?
}

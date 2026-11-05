import Foundation

/// The answer of `GET /v1/albums` (WHO-133): every album played in the
/// period, new and old. `GET /v1/singles` answers the same for the
/// singles and EPs. The server counts and orders them (most played
/// first); the app only shows them, in the order they arrive.
struct PlayedAlbums: Decodable, Equatable {
    let period: ResolvedPeriod
    let albums: PlayedAlbumList
    /// True: hide the whole section; the summary shows the empty message.
    let isEmpty: Bool
    let emptyReason: String?
}

/// `count` is the full number of albums, even when `items` holds only
/// the first 10. "Show all" appears when there are more.
struct PlayedAlbumList: Decodable, Equatable {
    let count: Int
    let items: [PlayedAlbum]
}

struct PlayedAlbum: Decodable, Equatable {
    /// Spotify's album ID, the same ID as in "New to you".
    let albumId: String
    /// May be null on the wire.
    let name: String?
    let artists: [String]
    /// Plays of this album's tracks in the period.
    let playCount: Int
    /// Distinct tracks from this album played in the period.
    let tracksPlayedInPeriod: Int
    let totalMs: Int
    /// True: the album is also new to you in this period. Shown as a
    /// small "New" tag.
    let isNew: Bool
    /// The album's cover on `i.scdn.co`, or nil (WHO-149).
    let artworkUrl: String?
    /// The user's own rating, 1 to 10, or nil when not rated.
    var rating: Int?
    /// The user's own comment, or nil. Independent of the rating.
    var comment: String?
    /// Album, single or EP; nil from a server that does not send it,
    /// which only lists albums.
    var releaseType: ReleaseType?
}

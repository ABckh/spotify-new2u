import Foundation

/// The answer of `GET /v1/new-to-you/albums/{album_id}/tracks`, exactly
/// as api-contract section 3.10 describes it: the tracks of one album
/// the user played in the period. The server decides the order; the app
/// shows it as it arrives and never re-sorts.
struct AlbumTracks: Decodable, Equatable {
    let period: ResolvedPeriod
    let album: AlbumTracksAlbum
    /// The whole list, with no paging. Most played first.
    let tracks: [AlbumTrack]
}

struct AlbumTracksAlbum: Decodable, Equatable {
    let albumId: String
    /// May be null on the wire, for example for an unknown album.
    let name: String?
    let artists: [String]
    /// The album's cover on `i.scdn.co`, or nil (WHO-149).
    let artworkUrl: String?
    /// The user's own rating, 1 to 10, or nil when not rated.
    var rating: Int?
    /// The user's own comment, or nil. Independent of the rating.
    var comment: String?
}

struct AlbumTrack: Decodable, Equatable {
    let itemKey: String
    let title: String
    let creators: [String]
    /// Plays of this track on this album, in the period only.
    let playCount: Int
    /// True: the first play of this track ever is inside the period.
    /// A new album can hold tracks that are not new.
    let isNew: Bool
    /// The user's own comment on the track, or nil.
    var comment: String?
}

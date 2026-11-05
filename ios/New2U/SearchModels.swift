import Foundation

/// The answer of `GET /v1/search`: the user's albums (singles and EPs
/// included) and songs, all time, holding every word searched for. The
/// server decides what matches and in which order (most played first);
/// the app shows it as it arrives.
struct SearchAnswer: Decodable, Equatable {
    let albums: FoundAlbums
    let tracks: FoundTracks

    var isEmpty: Bool { albums.count == 0 && tracks.count == 0 }
}

struct FoundAlbums: Decodable, Equatable {
    /// Every match; `items` holds at most the first 50.
    let count: Int
    let items: [FoundAlbum]
}

struct FoundTracks: Decodable, Equatable {
    /// Every match; `items` holds at most the first 50.
    let count: Int
    let items: [FoundTrack]
}

/// An album, single or EP found by its name, artists or the user's
/// comment on it.
struct FoundAlbum: Decodable, Equatable {
    let albumId: String
    /// nil only for an album found by its comment whose plays are gone.
    let name: String?
    let artists: [String]
    let artworkUrl: String?
    let releaseType: ReleaseType
    let rating: Int?
    let comment: String?
    /// Plays over the whole history: 0 for an album without plays.
    let playCount: Int
    /// "YYYY-MM-DD" in the display zone, nil without plays.
    let lastPlayedLocalDate: String?
}

/// A song found by its title, artists or the album name stored with it.
struct FoundTrack: Decodable, Equatable {
    let itemKey: String
    let title: String
    let creators: [String]
    /// The album name stored with the most recent play.
    let album: String?
    /// The album its plays belong to, when the server knows it: the row
    /// then opens that album.
    let albumId: String?
    let artworkUrl: String?
    /// The user's own comment on the song, or nil. A song is also found
    /// by the words of its comment.
    var comment: String?
    /// Plays over the whole history.
    let playCount: Int
    /// "YYYY-MM-DD" in the display zone.
    let lastPlayedLocalDate: String
}

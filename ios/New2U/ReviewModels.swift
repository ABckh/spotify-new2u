import Foundation

/// The answer of `GET /v1/me/review`: "Did you already know these?",
/// the albums, singles and EPs first played in the first two weeks that
/// are still new. The server decides what is listed, with the same rules as
/// "New to you".
struct Review: Decodable, Equatable {
    /// False once the review is over for good: nothing is shown.
    let open: Bool
    /// Sent back unchanged with "Done", so the next review starts after
    /// what this one showed. Kept as the server's text, never re-encoded.
    let through: String?
    let albums: NewAlbums
    let singles: NewAlbums

    /// Everything still to review.
    var count: Int { albums.count + singles.count }

    /// One of the two lists.
    func list(_ list: NewToYouList) -> NewAlbums {
        switch list {
        case .albums: return albums
        case .singles: return singles
        }
    }
}

/// The body of `POST /v1/new-to-you/not-new`.
struct NotNewBatch: Encodable {
    struct Item: Encodable, Equatable {
        let kind: String
        let itemId: String

        enum CodingKeys: String, CodingKey {
            case kind
            case itemId = "item_id"
        }
    }

    let items: [Item]
}

/// The answer of `POST /v1/new-to-you/not-new`.
struct NotNewBatchAnswer: Decodable, Equatable {
    let marked: Int
    let notPlayed: Int
}

/// The body of `POST /v1/me/review/done`.
struct ReviewDone: Encodable {
    let through: String
}

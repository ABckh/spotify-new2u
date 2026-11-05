import Foundation
import Observation

/// Loads the tracks of one album for `AlbumTracksView`, opened from
/// "New to you" or from "Albums" (WHO-134).
///
/// The same shape as `NewToYouViewModel`: the view reads the properties
/// below and calls `load()`. It asks for the same period as the
/// section it was opened from.
///
/// The answer lives only in memory. Nothing is written to disk (S6.5d).
@Observable
final class AlbumTracksViewModel: CommentSaving {
    enum Status: Equatable {
        /// The answer is on its way.
        case loading
        case loaded
        /// The request failed. The screen shows the message and a retry.
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    private(set) var answer: AlbumTracks?

    let albumId: String
    let query: PeriodQuery
    /// "/v1/new-to-you/albums" or "/v1/albums". Both answer the same.
    let basePath: String

    /// "Not new" on song rows, with its Undo (WHO-123).
    let marker: NotNewMarker

    /// The user's rating of this album, 1 to 10. Changes at once on a tap,
    /// and goes back when the server says no.
    private(set) var rating: Int?
    /// The message of the last failed rating, while it shows.
    private(set) var ratingFailure: String?
    /// The user's own comment on this album, or nil. Independent of the
    /// rating: saving or clearing one never touches the other.
    private(set) var comment: String?
    /// The message of the last failed comment change, while it shows.
    private(set) var commentFailure: String?
    /// True while a comment change is on its way.
    private(set) var isSavingComment = false
    /// Runs after a rating or a comment reached the server, so every list
    /// shows it.
    var ratingChanged: () async -> Void = {}

    private let api: APIClient

    /// `parent` is the "New to you" model the album was opened from. A
    /// song mark reloads it too, so its counts match. nil from "Albums".
    init(
        api: APIClient, albumId: String, query: PeriodQuery,
        basePath: String = "/v1/new-to-you/albums", parent: NewToYouViewModel? = nil
    ) {
        self.api = api
        self.albumId = albumId
        self.query = query
        self.basePath = basePath
        marker = NotNewMarker(api: api)
        marker.refresh = { [weak self, weak parent] in
            await self?.load()
            await parent?.load()
        }
    }

    /// True: show the "New" tag on this song. A song just marked "Not new"
    /// loses it at once, before the server answers.
    func showsNew(_ track: AlbumTrack) -> Bool {
        track.isNew && !marker.hidden.contains { $0.kind == .track && $0.itemId == track.itemKey }
    }

    /// Rates the album, or clears its rating with nil.
    func rate(_ value: Int?) async {
        guard value != rating else { return }
        let previous = rating
        rating = value
        ratingFailure = nil
        do {
            if let value {
                try await api.sendJSON(
                    "/v1/albums/\(albumId)/rating", method: "PUT", body: RatingBody(rating: value))
            } else {
                try await api.send(method: "DELETE", pathSegments: ["v1", "albums", albumId, "rating"])
            }
        } catch {
            rating = previous
            ratingFailure = Messages.ratingFailed((error as? APIError) ?? .badResponse)
            return
        }
        await ratingChanged()
    }

    func dismissRatingFailure() {
        ratingFailure = nil
    }

    /// What `saveComment` sends for `draft`: trimmed, without NUL, or nil
    /// (clear) when nothing is left.
    static func commentToSend(_ draft: String) -> String? {
        let text = draft.replacingOccurrences(of: "\u{0}", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    /// The length the server counts: Unicode scalars, not characters.
    static func commentLength(_ draft: String) -> Int {
        commentToSend(draft)?.unicodeScalars.count ?? 0
    }

    /// Saves `draft` as the comment; a blank draft clears it. True once
    /// the server has it, or when nothing changed. Unlike the rating it
    /// changes only after the server says yes, so a failure keeps the
    /// draft in the editor rather than losing what was typed.
    @discardableResult
    func saveComment(_ draft: String) async -> Bool {
        let text = Self.commentToSend(draft)
        guard text != comment else { return true }
        if let text, text.unicodeScalars.count > Messages.commentMaxLength { return false }
        isSavingComment = true
        commentFailure = nil
        defer { isSavingComment = false }
        do {
            if let text {
                try await api.sendJSON(
                    "/v1/albums/\(albumId)/comment", method: "PUT", body: CommentBody(comment: text))
            } else {
                try await api.send(method: "DELETE", pathSegments: ["v1", "albums", albumId, "comment"])
            }
        } catch {
            commentFailure = Messages.commentFailed((error as? APIError) ?? .badResponse)
            return false
        }
        comment = text
        await ratingChanged()
        return true
    }

    func dismissCommentFailure() {
        commentFailure = nil
    }

    /// A model for one track's comment, from a tapped track. Once saved,
    /// the album is asked again so the track shows it, and the other
    /// lists after it.
    func trackComment(_ track: AlbumTrack) -> TrackCommentModel {
        let model = TrackCommentModel(api: api, itemKey: track.itemKey, title: track.title, comment: track.comment)
        model.changed = { [weak self] _ in
            await self?.load()
            await self?.ratingChanged()
        }
        return model
    }

    func load() async {
        status = .loading
        do {
            // Album IDs are base62 (the server refuses anything else), so
            // they go into the path as they are.
            let answer: AlbumTracks = try await api.get(
                "\(basePath)/\(albumId)/tracks", query: query.queryItems)
            try Task.checkCancellation()
            self.answer = answer
            rating = answer.album.rating
            comment = answer.album.comment
            status = .loaded
        } catch is CancellationError {
            return
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            answer = nil
            status = .failed(error)
        } catch {
            answer = nil
            status = .failed(.badResponse)
        }
    }
}

/// The body of `PUT /v1/albums/{album_id}/rating`.
struct RatingBody: Encodable, Equatable {
    let rating: Int
}

/// The body of `PUT /v1/albums/{album_id}/comment`.
struct CommentBody: Encodable, Equatable {
    let comment: String
}

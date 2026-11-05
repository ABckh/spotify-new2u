import Foundation
import Observation

/// One "Not new" mark: an album (single and EP included) or a song the
/// user already knew before they installed the app (WHO-123).
struct NotNewMark: Hashable {
    enum Kind: String {
        case album, track
    }

    let kind: Kind
    /// Album: Spotify's album ID. Song: its `item_key`.
    let itemId: String
    /// The name shown in the Undo banner.
    let name: String

    /// `/v1/new-to-you/not-new/{kind}/{item_id}`, one entry per part.
    var pathSegments: [String] {
        ["v1", "new-to-you", "not-new", kind.rawValue, itemId]
    }
}

extension NewAlbum {
    var notNewMark: NotNewMark {
        NotNewMark(kind: .album, itemId: albumId, name: name ?? String(localized: "Unknown album"))
    }
}

extension AlbumTrack {
    var notNewMark: NotNewMark {
        NotNewMark(kind: .track, itemId: itemKey, name: title)
    }
}

/// Sends "Not new" marks and their Undo, for one screen.
///
/// The row goes away at once: the screen hides every mark in `hidden`
/// while the request is on its way. When the server says yes, the
/// screen's data is loaded again so the counts are right. When it says
/// no, the row comes back and `failure` holds the message.
///
/// Marks and Undos are sent one after another, in the order the user
/// made them, so an Undo never reaches the server before its mark.
@Observable
final class NotNewMarker {
    /// Marks the screen hides right now.
    private(set) var hidden: Set<NotNewMark> = []
    /// The last mark, while its Undo banner shows.
    private(set) var undoable: NotNewMark?
    /// The message of the last failed mark or Undo, while its banner shows.
    private(set) var failure: String?

    /// Loads the screen's data again. Set by the screen's view model.
    var refresh: () async -> Void = {}

    private let api: APIClient
    /// The last request in line. The next one waits for it.
    private var lastRequest: Task<Void, Never>?

    init(api: APIClient) {
        self.api = api
    }

    func mark(_ mark: NotNewMark) async {
        hidden.insert(mark)
        undoable = mark
        failure = nil
        await inOrder {
            do {
                try await self.api.send(method: "PUT", pathSegments: mark.pathSegments)
            } catch {
                // Put the row back and say why.
                self.hidden.remove(mark)
                if self.undoable == mark { self.undoable = nil }
                self.failure = Messages.notNewFailed(name: mark.name, error: Self.apiError(error))
                return
            }
            await self.refresh()
            // The new answer leaves the item out, so it can stop being hidden.
            self.hidden.remove(mark)
        }
    }

    /// Takes the last mark back.
    func undo() async {
        guard let mark = undoable else { return }
        undoable = nil
        failure = nil
        await inOrder {
            do {
                try await self.api.send(method: "DELETE", pathSegments: mark.pathSegments)
            } catch {
                self.failure = Messages.undoFailed(name: mark.name, error: Self.apiError(error))
                return
            }
            self.hidden.remove(mark)
            await self.refresh()
        }
    }

    /// The Undo banner timed out.
    func dismissUndo() {
        undoable = nil
    }

    func dismissFailure() {
        failure = nil
    }

    /// Runs `work` after every request queued before it.
    private func inOrder(_ work: @escaping () async -> Void) async {
        let previous = lastRequest
        let task = Task {
            await previous?.value
            await work()
        }
        lastRequest = task
        await task.value
    }

    private static func apiError(_ error: Error) -> APIError {
        (error as? APIError) ?? .badResponse
    }
}

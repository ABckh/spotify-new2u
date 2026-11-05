import Foundation
import Observation

/// Loads "New to you" for one period, for `NewToYouSection` (the first
/// 10 of each list) and for `NewToYouAllView` (the whole list).
///
/// The same shape as `SummaryViewModel`: the view reads the properties
/// below and calls `load()`. It is its own request, so when it fails the
/// summary and timeline still show (S14.17).
///
/// The answer lives only in memory. Nothing is written to disk (S6.5d).
@Observable
final class NewToYouViewModel {
    enum Status: Equatable {
        /// The answer is on its way.
        case loading
        case loaded
        /// The request failed. The section shows its own message and retry.
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    /// The last answer that arrived. Kept while the next one loads, like
    /// the summary, and dropped on a failure so lists of another period
    /// are never shown (S14.16).
    private(set) var answer: NewToYou?

    var query: PeriodQuery
    /// false: the first 10 of each list. true: every entry, for "Show all" (S14.7).
    let all: Bool

    /// "Not new" on album and artist rows, with its Undo (WHO-123).
    let marker: NotNewMarker

    /// Runs after an album was rated from an album opened here, so the
    /// lists and the all-time ratings show it. `SignedInView` sets it.
    var reloadAfterRating: () async -> Void = {}

    /// Runs after a "Not new" mark or its Undo reached the server, so the
    /// "New" tags in "All albums" and "All artists" match (WHO-145).
    /// `SignedInView` sets it on the main screen's model.
    var reloadAfterMark: () async -> Void = {}

    /// What the lists show: the answer without the rows just marked
    /// "Not new".
    var shown: NewToYou? {
        answer?.hiding(marker.hidden)
    }

    private let api: APIClient

    /// `parent` is the section's model when this one is for "Show all".
    /// A mark made there reloads the section too, so its counts match.
    init(
        api: APIClient, query: PeriodQuery = PeriodQuery(kind: .week), all: Bool = false,
        parent: NewToYouViewModel? = nil
    ) {
        self.api = api
        self.query = query
        self.all = all
        marker = NotNewMarker(api: api)
        marker.refresh = { [weak self, weak parent] in
            await self?.load()
            await self?.reloadAfterMark()
            await parent?.load()
            await parent?.reloadAfterMark()
        }
    }

    /// A model for the "Show all" screen: the same period, every entry.
    func makeShowAll() -> NewToYouViewModel {
        let showAll = NewToYouViewModel(api: api, query: query, all: true, parent: self)
        showAll.reloadAfterRating = { [weak self, weak showAll] in
            await showAll?.load()
            await self?.reloadAfterRating()
        }
        return showAll
    }

    /// A model for one album's tracks, from a tapped album row (WHO-115).
    func makeAlbumTracks(albumId: String) -> AlbumTracksViewModel {
        let model = AlbumTracksViewModel(api: api, albumId: albumId, query: query, parent: self)
        model.ratingChanged = { [weak self] in await self?.reloadAfterRating() }
        return model
    }

    /// The share picture's content (WHO-196). The section has only the
    /// first 10 albums, so when there are more the whole list is asked
    /// for first, so the picture can hold up to
    /// `ShareCardModel.maxAlbums`. Albums just marked "Not new" are left
    /// out. When that request fails, the albums already here are used.
    func shareCard(summary: Summary?) async -> ShareCardModel? {
        guard let shown else { return nil }
        let albums = shown.albums
        if !all, albums.count > albums.items.count, albums.items.count < ShareCardModel.maxAlbums {
            let items = query.queryItems + [URLQueryItem(name: "all", value: "true")]
            if let full: NewToYou = try? await api.get("/v1/new-to-you", query: items),
               full.period == shown.period {
                return ShareCardModel(newToYou: full.hiding(marker.hidden), summary: summary)
            }
        }
        return ShareCardModel(newToYou: shown, summary: summary)
    }

    func load() async {
        status = .loading
        do {
            let items = query.queryItems + [URLQueryItem(name: "all", value: all ? "true" : "false")]
            let answer: NewToYou = try await api.get("/v1/new-to-you", query: items)
            try Task.checkCancellation()
            self.answer = answer
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

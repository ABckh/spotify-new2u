import Foundation
import Observation

/// Loads every album played in one period, for `AlbumsSection` (the
/// first 10) and for `AlbumsAllView` (the whole list) (WHO-134). With
/// `singles`, every single and EP instead, for the Singles tab.
///
/// The same shape as `NewToYouViewModel`: the view reads the properties
/// below and calls `load()`. It is its own request, so when it fails the
/// summary, "New to you" and the timeline still show.
///
/// The answer lives only in memory. Nothing is written to disk (S6.5d).
@Observable
final class AlbumsViewModel {
    enum Status: Equatable {
        /// The answer is on its way.
        case loading
        case loaded
        /// The request failed. The section shows its own message and retry.
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    /// The last answer that arrived. Kept while the next one loads, and
    /// dropped on a failure so albums of another period are never shown.
    private(set) var answer: PlayedAlbums?

    var query: PeriodQuery
    /// false: the first 10. true: every album, for "Show all".
    let all: Bool
    /// true: the singles and EPs (`/v1/singles`), not the albums.
    let singles: Bool

    /// Runs after an album was rated from an album opened here, so the
    /// lists and the all-time ratings show it. `SignedInView` sets it.
    var reloadAfterRating: () async -> Void = {}

    private let api: APIClient

    init(
        api: APIClient, query: PeriodQuery = PeriodQuery(kind: .week), all: Bool = false,
        singles: Bool = false
    ) {
        self.api = api
        self.query = query
        self.all = all
        self.singles = singles
    }

    /// A model for the "Show all" screen: the same period, every album.
    func makeShowAll() -> AlbumsViewModel {
        let showAll = AlbumsViewModel(api: api, query: query, all: true, singles: singles)
        showAll.reloadAfterRating = { [weak self, weak showAll] in
            await showAll?.load()
            await self?.reloadAfterRating()
        }
        return showAll
    }

    /// A model for one album's tracks, from a tapped album row.
    func makeAlbumTracks(albumId: String) -> AlbumTracksViewModel {
        let model = AlbumTracksViewModel(api: api, albumId: albumId, query: query, basePath: "/v1/albums")
        model.ratingChanged = { [weak self] in await self?.reloadAfterRating() }
        return model
    }

    func load() async {
        show(await fetch())
    }

    /// Asks for the albums without showing them: the Albums tab shows them
    /// with `show(_:)` once "New to you" above them is on screen, so the
    /// list fills from the top and nothing pushes it down. Marks the model
    /// loading. nil when cancelled.
    func fetch() async -> Result<PlayedAlbums, APIError>? {
        status = .loading
        do {
            let items = query.queryItems + [URLQueryItem(name: "all", value: all ? "true" : "false")]
            let answer: PlayedAlbums = try await api.get(singles ? "/v1/singles" : "/v1/albums", query: items)
            try Task.checkCancellation()
            return .success(answer)
        } catch is CancellationError {
            return nil
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            return .failure(error)
        } catch {
            return .failure(.badResponse)
        }
    }

    /// Shows what `fetch()` got; nil (cancelled) changes nothing.
    func show(_ result: Result<PlayedAlbums, APIError>?) {
        switch result {
        case .success(let answer):
            self.answer = answer
            status = .loaded
        case .failure(let error):
            answer = nil
            status = .failed(error)
        case nil:
            return
        }
    }
}

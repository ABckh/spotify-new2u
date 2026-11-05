import Foundation
import Observation

/// "Did you already know these?": the first-two-weeks review.
///
/// The user ticks the albums, singles and EPs they already knew and taps one
/// button. The ticked ones are marked "Not new" in one request (or a few,
/// 200 at a time), then the review is finished, which keeps everything
/// else new. Nothing is sent while the user ticks.
@Observable
final class ReviewViewModel {
    enum Status: Equatable {
        case loading
        case loaded
        /// The marks and "Done" are on their way.
        case saving
        /// Loading failed: the offer stays hidden.
        case failed(APIError)
        /// Saving failed: the sheet stays open with the ticks kept.
        case saveFailed(APIError)
    }

    /// The server takes at most this many marks per request.
    static let batchLimit = 200

    private(set) var status: Status = .loading
    private(set) var review: Review?
    /// What the user ticked as already known.
    private(set) var selected: Set<NotNewMark> = []

    /// Runs after a finished review, so "New to you" and the "All" lists
    /// leave out what was just marked. `SignedInView` sets it.
    var finished: () async -> Void = {}

    private let api: APIClient

    init(api: APIClient) {
        self.api = api
    }

    /// True: show the "Already knew some of these?" row.
    var isOffered: Bool {
        guard status == .loaded || status == .saving, let review else { return false }
        return review.open && review.count > 0
    }

    func load() async {
        if review == nil { status = .loading }
        do {
            let answer: Review = try await api.get("/v1/me/review")
            try Task.checkCancellation()
            review = answer
            // A tick for an item that is gone would be sent for nothing.
            selected.formIntersection(Self.marks(of: answer))
            status = .loaded
        } catch is CancellationError {
            return
        } catch let error as APIError {
            review = nil
            status = .failed(error)
        } catch {
            review = nil
            status = .failed(.badResponse)
        }
    }

    func isSelected(_ mark: NotNewMark) -> Bool {
        selected.contains(mark)
    }

    func toggle(_ mark: NotNewMark) {
        if selected.contains(mark) { selected.remove(mark) } else { selected.insert(mark) }
    }

    /// True when every item of `list` is ticked.
    func allSelected(_ list: NewToYouList) -> Bool {
        let all = marks(list)
        return !all.isEmpty && all.isSubset(of: selected)
    }

    /// Ticks every item of `list`, or unticks them when all already are.
    func toggleAll(_ list: NewToYouList) {
        let all = marks(list)
        if all.isSubset(of: selected) { selected.subtract(all) } else { selected.formUnion(all) }
    }

    /// Marks the ticked items "Not new" and finishes the review. True when
    /// both reached the server; on false nothing is lost and the user can
    /// try again (a mark sent twice is harmless).
    func finish() async -> Bool {
        guard let through = review?.through else { return false }
        status = .saving
        do {
            let items = selected
                .sorted { ($0.kind.rawValue, $0.itemId) < ($1.kind.rawValue, $1.itemId) }
                .map { NotNewBatch.Item(kind: $0.kind.rawValue, itemId: $0.itemId) }
            for start in stride(from: 0, to: items.count, by: Self.batchLimit) {
                let chunk = Array(items[start..<min(start + Self.batchLimit, items.count)])
                let _: NotNewBatchAnswer = try await api.post(
                    "/v1/new-to-you/not-new", body: NotNewBatch(items: chunk))
            }
            try await api.sendJSON("/v1/me/review/done", body: ReviewDone(through: through))
        } catch {
            status = .saveFailed((error as? APIError) ?? .badResponse)
            return false
        }
        selected = []
        await load()
        await finished()
        return true
    }

    private func marks(_ list: NewToYouList) -> Set<NotNewMark> {
        Set(review?.list(list).items.map(\.notNewMark) ?? [])
    }

    private static func marks(of review: Review) -> Set<NotNewMark> {
        Set((review.albums.items + review.singles.items).map(\.notNewMark))
    }
}

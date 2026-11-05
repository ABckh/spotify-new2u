import Foundation
import Observation
import SwiftUI

/// The answer of `GET /v1/ratings/albums?commented=true`: every album,
/// single and EP the user rated, all time, best first, then the ones
/// with only a comment, most recent first. The server orders them; the
/// app keeps the order.
struct RatedAlbums: Decodable, Equatable {
    let count: Int
    let items: [RatedAlbum]

    /// The mean of every rating, or nil with none.
    var average: Double? {
        Self.average(items)
    }

    /// How many are rated: the ones with only a comment are not.
    var ratedCount: Int {
        Self.ratings(items).count
    }

    /// How many have a comment, rated or not.
    var commentCount: Int {
        items.filter { $0.comment != nil }.count
    }

    /// Only the albums, or only the singles and EPs, best first.
    func items(of shelf: RatingsShelf) -> [RatedAlbum] {
        items.filter { ($0.releaseType == .album) == (shelf == .albums) }
    }

    /// `items` in their league, League 1 first; a league with nothing
    /// rated has an empty list. Each keeps the best-first order. A release
    /// with only a comment is in no league (`commentOnly`).
    static func leagues(_ items: [RatedAlbum]) -> [(league: League, items: [RatedAlbum])] {
        League.allCases.map { league in
            (league, items.filter { $0.rating.map(League.init(rating:)) == league })
        }
    }

    /// The releases with a comment and no rating, in the server's order.
    static func commentOnly(_ items: [RatedAlbum]) -> [RatedAlbum] {
        items.filter { $0.rating == nil }
    }

    /// The ratings in `items`, in order; the ones with only a comment have none.
    static func ratings(_ items: [RatedAlbum]) -> [Int] {
        items.compactMap(\.rating)
    }

    /// The mean rating of `items`, the ones with only a comment left out.
    static func average(_ items: [RatedAlbum]) -> Double? {
        let ratings = Self.ratings(items)
        return ratings.isEmpty ? nil : Double(ratings.reduce(0, +)) / Double(ratings.count)
    }
}

struct RatedAlbum: Decodable, Equatable {
    let albumId: String
    let name: String?
    let artists: [String]
    let artworkUrl: String?
    /// Album, single or EP: albums and the rest are ranked apart.
    let releaseType: ReleaseType
    /// nil only for a release listed for its comment alone.
    let rating: Int?
    let ratedAt: Date?
    /// The user's own words, or nil. Independent of the rating.
    var comment: String?
    var commentedAt: Date?
}

/// The two separate rating lists: a single never competes with an
/// album, and each has its own count and average. The Albums tab opens
/// the albums', the Singles tab the singles' and EPs'.
enum RatingsShelf: Hashable, CaseIterable {
    case albums, singles

    /// The entry row's and the screen's title.
    var title: String {
        switch self {
        case .albums: return Messages.ratingsAlbumsTitle
        case .singles: return Messages.ratingsSinglesTitle
        }
    }

    var emptyText: String {
        switch self {
        case .albums: return Messages.ratingsAlbumsEmpty
        case .singles: return Messages.ratingsSinglesEmpty
        }
    }
}

/// Five leagues by score, so the ratings read as tiers: a rating puts a
/// release straight into its league.
enum League: Int, CaseIterable, Identifiable {
    case one = 1, two, three, four, five

    var id: Int { rawValue }

    init(rating: Int) {
        switch rating {
        case 10...: self = .one
        case 9: self = .two
        case 8: self = .three
        case 6...7: self = .four
        default: self = .five
        }
    }

    /// The scores in it, as the heading shows them: "10", "6–7", "1–5".
    var scores: String {
        switch self {
        case .one: return "10"
        case .two: return "9"
        case .three: return "8"
        case .four: return "6–7"
        case .five: return "1–5"
        }
    }
}

/// Loads the all-time ratings and comments for the Albums tab's entry
/// row and the "Your album ratings" screen. Lives only in memory.
@Observable
final class RatingsViewModel {
    enum Status: Equatable {
        case loading
        case loaded
        case failed(APIError)
    }

    private(set) var status: Status = .loading
    private(set) var answer: RatedAlbums?

    private let api: APIClient

    init(api: APIClient) {
        self.api = api
    }

    /// True: show the entry row. Only once something is rated or has a comment.
    var isOffered: Bool {
        (answer?.count ?? 0) > 0
    }

    /// True: show `shelf`'s entry row, once something on it is rated or
    /// has a comment.
    func isOffered(_ shelf: RatingsShelf) -> Bool {
        !(answer?.items(of: shelf).isEmpty ?? true)
    }

    func load() async {
        if answer == nil { status = .loading }
        do {
            // `commented=true`: the releases with only a comment too.
            let answer: RatedAlbums = try await api.get(
                "/v1/ratings/albums", query: [URLQueryItem(name: "commented", value: "true")])
            try Task.checkCancellation()
            self.answer = answer
            status = .loaded
        } catch is CancellationError {
            return
        } catch let error as APIError {
            status = .failed(error)
        } catch {
            status = .failed(.badResponse)
        }
    }
}

/// "8/10" after an album's name, on rows that show a rating.
struct RatingBadge: View {
    let rating: Int

    var body: some View {
        Text(Messages.ratingShort(rating))
            .font(.caption2.weight(.semibold))
            .monospacedDigit()
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(.white)
            .background(Capsule().fill(.tint))
            .fixedSize()
    }
}

/// Ten buttons, 1 to 10, on the album screen. The ones up to the rating
/// are filled. Tapping the current rating clears it, so does "Clear".
/// VoiceOver reads one element whose value goes up and down by swiping.
struct RatingPicker: View {
    let rating: Int?
    let rate: (Int?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack {
                Text(Messages.ratingTitle)
                    .font(.subheadline.weight(.semibold))
                Spacer()
                if rating != nil {
                    Button(Messages.ratingClear) { rate(nil) }
                        .font(.subheadline)
                }
            }
            HStack(spacing: Theme.Spacing.xs) {
                ForEach(1...10, id: \.self) { value in
                    let filled = value <= (rating ?? 0)
                    Button {
                        rate(value == rating ? nil : value)
                    } label: {
                        Text("\(value)")
                            .font(.subheadline.weight(.semibold))
                            .monospacedDigit()
                            .frame(maxWidth: .infinity, minHeight: 36)
                            .foregroundStyle(filled ? AnyShapeStyle(.white) : AnyShapeStyle(.primary))
                            .background(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .fill(filled ? AnyShapeStyle(.tint) : AnyShapeStyle(Theme.placeholderFill)))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Messages.ratingTitle)
        .accessibilityValue(rating.map { "\($0) out of 10" } ?? String(localized: "Not rated"))
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: rate(min(10, (rating ?? 0) + 1))
            case .decrement: rate((rating ?? 1) <= 1 ? nil : (rating ?? 1) - 1)
            @unknown default: break
            }
        }
    }
}

/// The row on the Albums tab that opens "Your album ratings".
/// "Your album ratings" on the Albums tab, "Your singles & EPs ratings"
/// on the Singles tab, each with the count and average of its own shelf
/// only.
struct RatingsEntryRow: View {
    let ratings: RatedAlbums
    let shelf: RatingsShelf

    var body: some View {
        let items = ratings.items(of: shelf)
        HStack(spacing: Theme.Spacing.m) {
            Image(systemName: "star.circle")
                .font(.title3)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(shelf.title)
                    .font(.body.weight(.medium))
                Text(Messages.ratingsSummary(
                    count: RatedAlbums.ratings(items).count, average: RatedAlbums.average(items),
                    comments: items.filter { $0.comment != nil }.count))
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// One shelf's ratings, all time, in five leagues by score: the albums
/// from the Albums tab, the singles and EPs from the Singles tab, never
/// both. A release with a comment and no rating is under "Not rated", last.
struct RatingsView: View {
    let viewModel: RatingsViewModel
    let shelf: RatingsShelf

    init(viewModel: RatingsViewModel, shelf: RatingsShelf = .albums) {
        self.viewModel = viewModel
        self.shelf = shelf
    }

    var body: some View {
        Group {
            if let answer = viewModel.answer {
                let items = answer.items(of: shelf)
                List {
                    Section {
                        Text(Messages.ratingsSummary(
                            count: RatedAlbums.ratings(items).count, average: RatedAlbums.average(items),
                            comments: items.filter { $0.comment != nil }.count))
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondaryText)
                            .listRowInsets(Theme.rowInsets)
                            .listRowSeparator(.hidden)
                    }
                    if !items.isEmpty {
                        ForEach(RatedAlbums.leagues(items), id: \.league) { league, rated in
                            Section {
                                if rated.isEmpty {
                                    Text(Messages.leagueEmpty)
                                        .font(.subheadline)
                                        .foregroundStyle(Theme.secondaryText)
                                        .listRowInsets(Theme.rowInsets)
                                } else {
                                    ForEach(rated, id: \.albumId) { album in
                                        RatedAlbumRow(album: album)
                                            .listRowInsets(Theme.rowInsets)
                                    }
                                }
                            } header: {
                                LeagueHeader(league: league, count: rated.count)
                            }
                        }
                        let commentOnly = RatedAlbums.commentOnly(items)
                        if !commentOnly.isEmpty {
                            Section {
                                ForEach(commentOnly, id: \.albumId) { album in
                                    RatedAlbumRow(album: album)
                                        .listRowInsets(Theme.rowInsets)
                                }
                            } header: {
                                CommentOnlyHeader(count: commentOnly.count)
                            }
                        }
                    }
                }
                .listStyle(.plain)
                .overlay {
                    if items.isEmpty {
                        ContentUnavailableView(
                            shelf.title, systemImage: "star",
                            description: Text(shelf.emptyText))
                    }
                }
            } else if case .failed(let error) = viewModel.status {
                ContentUnavailableView {
                    Label(Messages.ratingsFailed, systemImage: Messages.failure(error).systemImage)
                } description: {
                    Text(Messages.failure(error).detail)
                } actions: {
                    Button("Try again") { Task { await viewModel.load() } }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                ProgressView("Loading")
            }
        }
        .navigationTitle(shelf.title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await viewModel.load() }
        .refreshable { await viewModel.load() }
    }
}

/// "League 1" over "10 · 3", the top of one league's section.
struct LeagueHeader: View {
    let league: League
    let count: Int

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(Messages.leagueTitle(league.rawValue))
                .font(.headline)
                .foregroundStyle(Color.primary)
            Text(league.scores)
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .foregroundStyle(.white)
                .background(Capsule().fill(.tint.opacity(1 - Double(league.rawValue - 1) * 0.15)))
            Spacer()
            Text("\(count)")
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.secondaryText)
        }
        .textCase(nil)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(Messages.leagueTitle(league.rawValue)), scores \(league.scores), \(count)")
        .accessibilityAddTraits(.isHeader)
    }
}

/// "Not rated" over its count, the section of releases with only a comment.
struct CommentOnlyHeader: View {
    let count: Int

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(Messages.ratingsCommentOnly)
                .font(.headline)
                .foregroundStyle(Color.primary)
            Spacer()
            Text("\(count)")
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.secondaryText)
        }
        .textCase(nil)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(Messages.ratingsCommentOnly), \(count)")
        .accessibilityAddTraits(.isHeader)
    }
}

/// One rated release: cover, name over artists, and the rating large on
/// the right. The user's comment, if any, is under them: three lines,
/// all of it after a tap.
struct RatedAlbumRow: View {
    let album: RatedAlbum
    @State private var expanded = false

    var body: some View {
        HStack(alignment: album.comment == nil ? .center : .top, spacing: Theme.Spacing.m) {
            CoverImage(url: album.artworkUrl)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(name)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                Text([album.releaseType.label, artists].compactMap { $0 }.joined(separator: " · "))
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(1)
                if let comment = album.comment {
                    Text(comment)
                        .font(.subheadline)
                        .lineLimit(expanded ? nil : 3)
                        .padding(.top, Theme.Spacing.xs)
                }
            }
            Spacer(minLength: Theme.Spacing.s)
            if let rating = album.rating {
                HStack(alignment: .firstTextBaseline, spacing: 1) {
                    Text("\(rating)")
                        .font(.title2.weight(.bold))
                        .monospacedDigit()
                    Text("/10")
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                }
                .fixedSize()
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if album.comment != nil { expanded.toggle() }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// One sentence, then the whole comment: on this screen it is the point.
    var spoken: String {
        let kind = album.releaseType.label.map { "\($0), " } ?? ""
        let comment = album.comment.map { " Your comment: \($0)" } ?? ""
        return "\(name), \(kind)by \(artists).\(Messages.ratingSpoken(album.rating))\(comment)"
    }

    private var name: String { album.name ?? String(localized: "Unknown album") }

    private var artists: String {
        album.artists.isEmpty ? String(localized: "Unknown artist") : album.artists.joined(separator: ", ")
    }
}

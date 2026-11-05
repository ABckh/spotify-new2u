import Foundation

/// What goes on the "New albums I listened to" share picture (WHO-196): the period,
/// the period's totals from the summary header, and the first new
/// albums with the user's rating of each rated one, nothing else
/// (WHO-197 rules 1 and 2; ratings were added at the owner's request).
///
/// Built by `NewToYouViewModel.shareCard(summary:)` from the whole list
/// without the albums just marked "Not new", and the "+ N more" count
/// is lowered to match (rule 3). The albums keep the order the server
/// sent.
struct ShareCardModel: Equatable, Identifiable {
    /// One album on the picture: its name, first artist and cover.
    struct Album: Equatable {
        let name: String
        let artist: String?
        /// Only used to get the cover from `CoverImageLoader`. Never
        /// drawn as text (rule 2).
        let artworkUrl: String?
        /// The user's rating, drawn on the cover's corner; nil draws none.
        var rating: Int?
    }

    /// The numbers at the top of every tab, as text: "3h 12m", and the
    /// top artist and top track with their play counts.
    struct Stats: Equatable {
        let total: String
        let topArtist: String?
        let topTrack: String?
    }

    /// Each tap on Share makes a new picture, so a new sheet.
    let id = UUID()

    /// At most 25 covers. `ShareCard` picks the grid that fills the
    /// picture with the largest covers (`ShareCard.Grid`).
    static let maxAlbums = 25

    /// The period as the period bar shows it, for example "October 2026".
    let periodLabel: String
    let albums: [Album]
    /// New albums not on the picture: "+ 3 more". 0 hides the line.
    let moreCount: Int
    /// nil when the summary is not loaded, or is for another period than
    /// the albums. Then the picture has no numbers block.
    let stats: Stats?

    /// nil when there is no new album to share, so no Share button shows.
    init?(newToYou: NewToYou, summary: Summary? = nil, locale: Locale = .current, now: Date = Date()) {
        let items = newToYou.albums.items.prefix(Self.maxAlbums)
        guard !newToYou.isEmpty, !items.isEmpty else { return nil }
        periodLabel = DisplayFormat.periodRange(newToYou.period, locale: locale, now: now)
        albums = items.map {
            Album(
                name: $0.name ?? String(localized: "Unknown album"),
                artist: $0.artists.first,
                artworkUrl: $0.artworkUrl,
                rating: $0.rating)
        }
        // `count` is every new album, also any the server did not send.
        moreCount = max(0, newToYou.albums.count - albums.count)
        stats = summary.flatMap { Self.stats($0, period: newToYou.period, locale: locale) }
    }

    /// The summary's numbers, the same text as `SummaryNumbers` shows.
    /// Only when the summary is for exactly the albums' period, so the
    /// picture never mixes two periods while one of them reloads.
    private static func stats(_ summary: Summary, period: ResolvedPeriod, locale: Locale) -> Stats? {
        guard summary.period == period, !summary.isEmpty else { return nil }
        return Stats(
            total: DisplayFormat.total(milliseconds: summary.totals.totalMs, locale: locale),
            topArtist: summary.topArtists.first.map { "\($0.name) · \(Messages.plays($0.playCount))" },
            topTrack: summary.topTracks.first.map { "\($0.title) · \(Messages.plays($0.playCount))" })
    }
}

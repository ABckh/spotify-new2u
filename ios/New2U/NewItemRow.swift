import SwiftUI

/// One entry in a "New to you" list: an album, single or EP (design
/// bar): its cover square, name over artists, date of the first play
/// over a count (S14.5). A single or EP says so before its artists.
struct NewItemRow: View {
    let title: String
    /// Artist names, or nil for none.
    let subtitle: String?
    /// The first play, already formatted.
    let date: String
    /// "6 of 10 tracks".
    let detail: String?
    /// What VoiceOver reads: one sentence for the whole entry.
    let spoken: String
    /// True: the cover square (or its placeholder) leads.
    var showsCover = false
    var artworkUrl: String?
    /// The user's album rating, as a badge after the name.
    var rating: Int?
    /// True: the user has a comment on it, a small mark after the name.
    var hasComment = false

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                stackedLayout
            } else {
                regularLayout
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    private var regularLayout: some View {
        HStack(spacing: Theme.Spacing.m) {
            if showsCover {
                CoverImage(url: artworkUrl)
            }
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                HStack(spacing: Theme.Spacing.xs) {
                    Text(title)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    if let rating {
                        RatingBadge(rating: rating)
                    }
                    if hasComment {
                        CommentMark()
                    }
                }
                if let subtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: Theme.Spacing.s)
            VStack(alignment: .trailing, spacing: Theme.Spacing.xs) {
                Text(date)
                    .font(.subheadline)
                    .monospacedDigit()
                if let detail {
                    Text(detail)
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.secondaryText)
                }
            }
            .lineLimit(1)
            .fixedSize()
        }
    }

    /// At the largest text sizes the date and count move under the name,
    /// and the cover is dropped.
    private var stackedLayout: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text(title)
                .font(.body.weight(.medium))
                .lineLimit(2)
            if let subtitle {
                Text(subtitle)
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(2)
            }
            Text([date, detail].compactMap { $0 }.joined(separator: " · "))
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.secondaryText)
        }
    }
}

// MARK: - One row per kind of entry

extension NewItemRow {
    init(album: NewAlbum, timeZone: TimeZone) {
        let name = album.name ?? String(localized: "Unknown album")
        let artists = Self.names(album.artists)
        let tracks = Messages.tracksOf(album.tracksListened, album.totalTracks)
        // "EP · Nova Kett"; an album shows only its artists.
        let kind = album.releaseType?.label
        self.init(
            title: name,
            subtitle: [kind, artists].compactMap { $0 }.joined(separator: " · "),
            date: DisplayFormat.shortDate(localDate: album.firstPlayedLocalDate, timeZone: timeZone),
            detail: tracks,
            spoken: "\(name), \(kind.map { "\($0), " } ?? "")by \(artists). First played \(Self.spokenDate(album.firstPlayedLocalDate, timeZone)). \(tracks) played.\(Messages.ratingSpoken(album.rating))\(Messages.commentSpoken(album.comment))",
            showsCover: true,
            artworkUrl: album.artworkUrl,
            rating: album.rating,
            hasComment: album.comment != nil)
    }

    private static func names(_ names: [String]) -> String {
        names.isEmpty ? String(localized: "Unknown artist") : names.joined(separator: ", ")
    }

    /// The full date for VoiceOver, so it reads "10 September 2026",
    /// not an abbreviation.
    private static func spokenDate(_ localDate: String, _ timeZone: TimeZone) -> String {
        DisplayFormat.longDate(localDate: localDate, timeZone: timeZone)
    }
}

import SwiftUI

/// One album in "All albums": the cover square, name (with a "New" tag
/// when it is new to you) over artists, and the plays over the
/// listening time in the period on the right.
struct PlayedAlbumRow: View {
    let album: PlayedAlbum

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                // At the largest text sizes the numbers move under the
                // name and the cover is dropped, as in a play row.
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    titleLine(lineLimit: 2)
                    Text(byline)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                        .lineLimit(2)
                    Text(Messages.playsAndTime(plays: album.playCount, totalMs: album.totalMs))
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.secondaryText)
                }
            } else {
                HStack(spacing: Theme.Spacing.m) {
                    CoverImage(url: album.artworkUrl)
                    VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                        titleLine(lineLimit: 1)
                        Text(byline)
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondaryText)
                            .lineLimit(1)
                    }
                    Spacer(minLength: Theme.Spacing.s)
                    PlaysAndTime(plays: album.playCount, totalMs: album.totalMs)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// The same fallback as "New to you" when the name is unknown.
    var name: String {
        album.name ?? String(localized: "Unknown album")
    }

    var artists: String {
        album.artists.isEmpty ? String(localized: "Unknown artist") : album.artists.joined(separator: ", ")
    }

    /// The line under the name: the artists, after "Single" or "EP" for
    /// a single or EP.
    var byline: String {
        [album.releaseType?.label, artists].compactMap { $0 }.joined(separator: " · ")
    }

    /// What VoiceOver reads: one sentence for the whole row.
    var spoken: String {
        let new = album.isNew ? " New to you." : ""
        let time = DisplayFormat.spokenDuration(milliseconds: album.totalMs)
        let kind = album.releaseType?.label.map { "\($0), " } ?? ""
        return "\(name), \(kind)by \(artists). \(Messages.plays(album.playCount)), \(time) in this period.\(new)\(Messages.ratingSpoken(album.rating))\(Messages.commentSpoken(album.comment))"
    }

    private func titleLine(lineLimit: Int) -> some View {
        HStack(spacing: Theme.Spacing.xs) {
            Text(name)
                .font(.body.weight(.medium))
                .lineLimit(lineLimit)
            if let rating = album.rating {
                RatingBadge(rating: rating)
            }
            if album.comment != nil {
                CommentMark()
            }
            if album.isNew {
                NewTag()
            }
        }
    }
}

/// The right-hand column of an album or artist row: plays over the
/// listening time, like the clock time over the length in a play row.
struct PlaysAndTime: View {
    let plays: Int
    let totalMs: Int

    var body: some View {
        VStack(alignment: .trailing, spacing: Theme.Spacing.xs) {
            Text(Messages.plays(plays))
                .font(.subheadline)
                .monospacedDigit()
            Text(DisplayFormat.total(milliseconds: totalMs))
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.secondaryText)
        }
        .lineLimit(1)
        .fixedSize()
    }
}

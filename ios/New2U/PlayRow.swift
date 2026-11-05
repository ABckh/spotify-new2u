import SwiftUI

/// One play in the timeline. Same anatomy in every row (design bar):
/// artwork square, title over artist, clock time over duration.
struct PlayRow: View {
    let event: TimelineEvent
    let timeZone: TimeZone

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                stackedLayout
            } else {
                regularLayout
            }
        }
        // VoiceOver reads the row as one sentence.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenSentence)
    }

    /// The normal row: one fixed height, numbers trailing and aligned.
    private var regularLayout: some View {
        HStack(spacing: Theme.Spacing.m) {
            CoverImage(url: event.artworkUrl)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                HStack(spacing: Theme.Spacing.xs) {
                    Text(event.title)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    if event.comment != nil {
                        CommentMark()
                    }
                }
                Text(artists)
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(1)
            }
            Spacer(minLength: Theme.Spacing.s)
            VStack(alignment: .trailing, spacing: Theme.Spacing.xs) {
                Text(clockTime ?? " ")
                    .font(.subheadline)
                    .monospacedDigit()
                Text(duration)
                    .font(.subheadline)
                    .monospacedDigit()
                    .foregroundStyle(Theme.secondaryText)
            }
            .lineLimit(1)
            .fixedSize()
        }
    }

    /// At the largest text sizes there is no room for two columns, so
    /// the numbers move under the text and the artwork is dropped.
    private var stackedLayout: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            Text(event.title)
                .font(.body.weight(.medium))
                .lineLimit(2)
            Text(artists)
                .font(.subheadline)
                .foregroundStyle(Theme.secondaryText)
                .lineLimit(2)
            Text([clockTime, duration].compactMap { $0 }.joined(separator: " · "))
                .font(.subheadline)
                .monospacedDigit()
                .foregroundStyle(Theme.secondaryText)
        }
    }

    // MARK: - Text

    private var artists: String {
        event.creators.isEmpty ? String(localized: "Unknown artist") : event.creators.joined(separator: ", ")
    }

    /// nil for a date-only play, which has no time of day (WHO-13 R-49).
    private var clockTime: String? {
        guard event.occurredAtPrecision == "second" else { return nil }
        return DisplayFormat.clockTime(event.occurredAt, timeZone: timeZone)
    }

    private var duration: String {
        guard let ms = event.durationMs else { return "–" }
        return DisplayFormat.duration(milliseconds: ms)
    }

    private var spokenSentence: String {
        let length = event.durationMs.map { DisplayFormat.spokenDuration(milliseconds: $0) }
            ?? String(localized: "length unknown")
        let sentence = [event.title, artists, clockTime, length].compactMap { $0 }.joined(separator: ", ")
        return sentence + "." + Messages.commentSpoken(event.comment)
    }
}

/// The neutral square shown where a cover would go: while it loads,
/// when it fails, and when the play has no cover.
///
/// Covers come from Spotify's image server, `i.scdn.co`, through
/// `CoverImageLoader` only (WHO-148 decision). Never use `AsyncImage`
/// for them: it keeps images in a cache file on disk.
struct ArtworkPlaceholder: View {
    var size: CGFloat = Theme.artworkSize

    var body: some View {
        RoundedRectangle(cornerRadius: Self.cornerRadius(size), style: .continuous)
            .fill(Theme.placeholderFill)
            .frame(width: size, height: size)
            .overlay {
                Image(systemName: "music.note")
                    .font(.system(size: size * 0.4))
                    .foregroundStyle(Theme.secondaryText)
            }
            .accessibilityHidden(true)
    }

    /// 6 points on a row's square, a little more on a larger cover.
    static func cornerRadius(_ size: CGFloat) -> CGFloat {
        max(6, size / 12)
    }
}

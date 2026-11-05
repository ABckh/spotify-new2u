import SwiftUI

/// The numbers at the top of the list: total time (the biggest number
/// on screen), then plays, top artist and top track (design bar).
///
/// All data comes from `SummaryViewModel`. This file only lays it out.
struct SummaryHeader: View {
    let viewModel: SummaryViewModel
    let retry: () -> Void

    var body: some View {
        if let summary = viewModel.summary {
            SummaryNumbers(summary: summary)
        } else if case .failed = viewModel.status {
            HStack {
                Text("Couldn't load the totals.")
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                Spacer(minLength: Theme.Spacing.s)
                Button("Try again", action: retry)
                    .font(.subheadline)
                    .frame(minHeight: 44)
            }
        } else {
            // Placeholders of the real size, so the list below does not
            // move when the numbers arrive.
            SummaryNumbers(summary: SummaryHeader.placeholder)
                .redacted(reason: .placeholder)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Loading totals")
        }
    }

    static let placeholder = Summary(
        period: ResolvedPeriod(
            type: "week", offset: 0, anchor: nil, timeZone: "UTC", weekStart: "monday",
            start: Date(timeIntervalSince1970: 0), end: Date(timeIntervalSince1970: 0),
            localStartDate: "1970-01-01", localEndDate: "1970-01-07", isPartial: false),
        totals: SummaryTotals(
            playCount: 42, distinctTracks: 30, distinctArtists: 12,
            totalMs: 11_520_000, eventsWithoutDuration: 0),
        topTracks: [TopTrack(itemKey: "x", title: "Placeholder track", creators: ["Artist"], playCount: 4, totalMs: 0, artworkUrl: nil)],
        topArtists: [TopArtist(name: "Placeholder artist", playCount: 9, totalMs: 0)],
        isEmpty: false, emptyReason: nil, coverageStart: nil)
}

/// The laid-out numbers of one summary.
struct SummaryNumbers: View {
    let summary: Summary

    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showsDisclosure = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            totalBlock
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    ForEach(facts, id: \.label) { fact in
                        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                            label(fact.label)
                            value(fact.value)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            } else {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: Theme.Spacing.l, verticalSpacing: Theme.Spacing.s) {
                    ForEach(facts, id: \.label) { fact in
                        GridRow {
                            label(fact.label)
                            if fact.showsCover {
                                // The top track's small cover. Dropped at
                                // the largest text sizes, as in the rows.
                                HStack(spacing: Theme.Spacing.s) {
                                    CoverImage(url: fact.artworkUrl, size: Theme.smallArtworkSize)
                                    value(fact.value)
                                }
                            } else {
                                value(fact.value)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
        }
        .padding(.vertical, Theme.Spacing.s)
        #if DEBUG
        .task {
            if DebugFixtures.showsDisclosureOnOpen {
                try? await Task.sleep(for: .seconds(1))
                showsDisclosure = true
            }
        }
        #endif
    }

    /// The big number and its info button (WHO-13 R-26: the disclosure is
    /// one tap away, next to the total).
    private var totalBlock: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs) {
                Text(DisplayFormat.total(milliseconds: summary.totals.totalMs))
                    .font(.largeTitle.weight(.semibold))
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .accessibilityLabel(
                        "Total listening time, \(DisplayFormat.spokenDuration(milliseconds: summary.totals.totalMs))")
                Button {
                    showsDisclosure = true
                } label: {
                    Image(systemName: "info.circle")
                        .font(.body)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("How the total is counted")
                .popover(isPresented: $showsDisclosure) {
                    disclosure
                }
            }
            label("total listening time")
                .accessibilityHidden(true)
        }
    }

    private var disclosure: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(Messages.totalDisclosure)
            if summary.totals.eventsWithoutDuration > 0 {
                // WHO-13 R-27: say how many plays added nothing.
                Text("\(Messages.plays(summary.totals.eventsWithoutDuration)) in this period had no known length and added nothing to the total.")
                    .foregroundStyle(Theme.secondaryText)
            }
        }
        .font(.subheadline)
        .fixedSize(horizontal: false, vertical: true)
        .padding(Theme.Spacing.l)
        .frame(idealWidth: 320)
        .presentationCompactAdaptation(.popover)
    }

    /// Plays, top artist, top track. Artist plays are shown as counts,
    /// never as a share of all plays (WHO-13 R-34).
    private var facts: [Fact] {
        var rows = [Fact(label: "plays", value: "\(summary.totals.playCount)")]
        if let artist = summary.topArtists.first {
            rows.append(Fact(label: "top artist", value: "\(artist.name) · \(Messages.plays(artist.playCount))"))
        }
        if let track = summary.topTracks.first {
            rows.append(Fact(
                label: "top track", value: "\(track.title) · \(Messages.plays(track.playCount))",
                showsCover: true, artworkUrl: track.artworkUrl))
        }
        return rows
    }

    /// One labelled number under the total.
    private struct Fact {
        let label: String
        let value: String
        var showsCover = false
        var artworkUrl: String?
    }

    private func label(_ text: String) -> some View {
        Text(text)
            .font(.subheadline)
            .foregroundStyle(Theme.secondaryText)
    }

    private func value(_ text: String) -> some View {
        Text(text)
            .font(.body.weight(.medium))
            .monospacedDigit()
            .lineLimit(typeSize.isAccessibilitySize ? 2 : 1)
    }
}

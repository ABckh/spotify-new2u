import SwiftUI

/// "Plays" on the main screen (WHO-131): the first 10 plays of the
/// period, newest first, under sticky day headers, with "Show all" when
/// there are more. This list does not page; "Show all" does.
///
/// It puts several sections into the main list (the heading, one per
/// day, the link), so it goes straight into the `List`, not into a
/// `Section`. All data comes from `TimelineViewModel`.
struct PlaysSection: View {
    let viewModel: TimelineViewModel
    /// The period's play count from the summary, or nil before it loads.
    let playCount: Int?
    /// Opens the comment editor for a tapped play's track.
    let editComment: (TimelineEvent) -> Void

    /// The "Show all" row's id, for scrolling to it.
    static let showAllID = "plays-show-all"

    var body: some View {
        Section {
            Text("Plays")
                .font(.title3.weight(.semibold))
                .accessibilityAddTraits(.isHeader)
                .padding(.top, Theme.Spacing.l)
                .listRowInsets(Theme.rowInsets)
                .listRowSeparator(.hidden)
            if let error = viewModel.bannerError {
                Text(Messages.banner(error))
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .listRowSeparator(.hidden)
            }
        }
        PlayDaySections(days: viewModel.previewDays, viewModel: viewModel, pages: false, editComment: editComment)
        if viewModel.hasMoreThanPreview {
            Section {
                NavigationLink {
                    TimelineAllView(source: viewModel)
                } label: {
                    Text(playCount.map { "Show all \($0)" } ?? "Show all")
                        .font(.body)
                        .foregroundStyle(.tint)
                }
                .listRowInsets(Theme.rowInsets)
                .id(Self.showAllID)
            }
        }
    }
}

/// The plays of `days`, one section per day under its sticky header,
/// with the import marker inside its day. Used by the main screen and
/// by "Show all".
struct PlayDaySections: View {
    let days: [DayGroup]
    let viewModel: TimelineViewModel
    /// True on "Show all": a row near the end loads the next page.
    let pages: Bool
    /// Opens the comment editor for a tapped play's track. The screen
    /// holds the sheet: a sheet on these rows would be one per row.
    let editComment: (TimelineEvent) -> Void

    var body: some View {
        ForEach(days) { day in
            Section {
                playRows(day.eventsAboveMarker)
                if day.markerIndex != nil, let marker = viewModel.importMarker {
                    ImportMarkerRow(marker: marker, timeZone: viewModel.displayTimeZone)
                        .listRowInsets(Theme.rowInsets)
                }
                playRows(day.eventsBelowMarker)
            } header: {
                DayHeader(day: day, timeZone: viewModel.displayTimeZone)
            }
        }
    }

    private func playRows(_ events: ArraySlice<TimelineEvent>) -> some View {
        ForEach(events) { event in
            // A tap writes or edits the comment on the play's track.
            Button {
                editComment(event)
            } label: {
                PlayRow(event: event, timeZone: viewModel.displayTimeZone)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint(Messages.trackCommentHint(event.comment))
                .listRowInsets(Theme.rowInsets)
                .onAppear {
                    guard pages else { return }
                    // A Task, not .task: scrolling past this row
                    // must not cancel the page request.
                    Task { await viewModel.loadMoreIfNeeded(after: event) }
                }
        }
    }
}

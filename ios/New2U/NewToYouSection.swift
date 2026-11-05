import SwiftUI

/// "New to you" under the summary (S14): the heading and V1, then the
/// new albums (Albums tab) or the new singles and EPs (Singles tab), at
/// most 10, with "Show all" when there are more (WHO-145).
///
/// Every piece is its own list row, so it scrolls with the tab's list.
/// All data comes from `NewToYouViewModel`. This file only lays it out.
struct NewToYouSection: View {
    let viewModel: NewToYouViewModel
    /// Which of the two lists this tab shows.
    let list: NewToYouList
    /// The tab's summary, for the numbers on the share picture.
    let summary: Summary?
    let retry: () -> Void

    @State private var showsMeaning = false
    /// The share picture's content while its sheet is open (WHO-196).
    @State private var shareCard: ShareCardModel?
    /// The whole list is loading for the picture; the button shows a spinner.
    @State private var preparingShare = false

    var body: some View {
        // `shown` leaves out rows just marked "Not new" (WHO-123).
        if let answer = viewModel.shown {
            // S14.15: an empty period hides the section. The summary
            // shows the empty message instead.
            if !answer.isEmpty {
                content(answer)
            }
        } else if case .failed(let error) = viewModel.status {
            heading(meaning: nil, collectionStarted: false)
            failure(error)
        } else {
            heading(meaning: nil, collectionStarted: false)
            placeholder
        }
    }

    // MARK: - Loaded

    @ViewBuilder
    private func content(_ answer: NewToYou) -> some View {
        // Dates are shown in the stored display zone the server echoes.
        let timeZone = answer.zone
        let meaning = answer.coverageStartLocalDate.map {
            Messages.newToYouMeaning(historyStart: DisplayFormat.longDate(localDate: $0, timeZone: timeZone))
        }
        heading(meaning: meaning, collectionStarted: answer.periodContainsCoverageStart)

        if answer.showsNothingNew {
            // S14.14: one sentence, not two empty boxes.
            Text(Messages.newToYouNothingNew)
                .font(.body)
                .row()
        } else {
            switch list {
            case .albums:
                albumsHeading(answer)
                rows(answer.albums, list: .albums, timeZone: timeZone)
            case .singles:
                listHeading(Messages.newSingles(answer.singles.count))
                rows(answer.singles, list: .singles, timeZone: timeZone)
            }
        }
    }

    /// The first 10 of one list, each opening its tracks, with "Show all"
    /// when there are more.
    @ViewBuilder
    private func rows(_ albums: NewAlbums, list: NewToYouList, timeZone: TimeZone) -> some View {
        ForEach(albums.items, id: \.albumId) { album in
            // WHO-115: a tap opens the release's played tracks.
            NavigationLink {
                AlbumTracksView(album: album, source: viewModel)
            } label: {
                NewItemRow(album: album, timeZone: timeZone)
            }
            .row()
            .notNewActions(album.notNewMark, marker: viewModel.marker)
        }
        if albums.count > albums.items.count {
            showAll(list, count: albums.count)
        }
    }

    /// "New to you" with an info button that opens V1 (once the date is
    /// known), then V2 (when collection started in this period).
    private func heading(meaning: String?, collectionStarted: Bool) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs) {
                Text("New to you")
                    .font(.title3.weight(.semibold))
                    .accessibilityAddTraits(.isHeader)
                if let meaning {
                    // WHO-110: V1 is one tap away, like the total's
                    // disclosure, instead of always on screen.
                    Button {
                        showsMeaning = true
                    } label: {
                        Image(systemName: "info.circle")
                            .font(.body)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("What new to you means")
                    .popover(isPresented: $showsMeaning) {
                        Text(meaning)
                            .font(.subheadline)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(Theme.Spacing.l)
                            .frame(idealWidth: 320)
                            .presentationCompactAdaptation(.popover)
                    }
                    #if DEBUG
                    .task {
                        if DebugFixtures.showsNewToYouMeaningOnOpen {
                            try? await Task.sleep(for: .seconds(1))
                            showsMeaning = true
                        }
                    }
                    #endif
                }
            }
            if collectionStarted {
                Text(Messages.newToYouCollectionStarted)
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .padding(.top, Theme.Spacing.l)
        .listRowInsets(Self.insets(top: Theme.Spacing.s, bottom: 0))
        // The list's heading under it belongs to it: no line and no
        // extra space between.
        .listRowSeparator(.hidden, edges: .bottom)
    }

    /// `Theme.rowInsets` with its own top and bottom, for the heading
    /// rows that sit right on top of each other.
    private static func insets(top: CGFloat, bottom: CGFloat) -> EdgeInsets {
        EdgeInsets(top: top, leading: Theme.rowInsets.leading, bottom: bottom, trailing: Theme.rowInsets.trailing)
    }

    /// "2 new singles & EPs", right under "New to you", as the albums
    /// heading sits.
    private func listHeading(_ text: String) -> some View {
        Text(text)
            .font(.headline)
            .accessibilityAddTraits(.isHeader)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .listRowInsets(Self.insets(top: 0, bottom: Theme.Spacing.s))
            .listRowSeparator(.hidden, edges: .top)
    }

    /// "4 new albums" with a Share button when there is at least one
    /// new album (WHO-196). `answer` is `viewModel.shown`, so albums just
    /// marked "Not new" are not on the picture; the tap loads the rest of
    /// the list first when there are more than 10.
    private func albumsHeading(_ answer: NewToYou) -> some View {
        let card = ShareCardModel(newToYou: answer, summary: summary)
        return HStack(alignment: .firstTextBaseline) {
            Text(Messages.newAlbums(answer.albums.count))
                .font(.headline)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: Theme.Spacing.s)
            if let card {
                Button {
                    preparingShare = true
                    Task {
                        shareCard = await viewModel.shareCard(summary: summary) ?? card
                        preparingShare = false
                    }
                } label: {
                    // The spinner sits over the hidden icon, so the row
                    // keeps the icon's size and baseline and never moves.
                    Image(systemName: "square.and.arrow.up")
                        .font(.body)
                        .opacity(preparingShare ? 0 : 1)
                        .overlay {
                            if preparingShare {
                                ProgressView()
                            }
                        }
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderless)
                .disabled(preparingShare)
                .accessibilityLabel("Share new albums")
            }
        }
        .listRowInsets(Self.insets(top: 0, bottom: Theme.Spacing.s))
        .listRowSeparator(.hidden, edges: .top)
        // Closing the sheet sets `shareCard` back to nil.
        .sheet(item: $shareCard) { card in
            SharePreviewSheet(card: card)
        }
        #if DEBUG
        .task {
            if DebugFixtures.opensShareOnLaunch {
                // After the covers on screen and a "Not new" mark are in.
                try? await Task.sleep(for: .seconds(2.5))
                shareCard = await viewModel.shareCard(summary: summary)
            }
        }
        #endif
    }

    /// One tap to the whole list (S14.7).
    private func showAll(_ list: NewToYouList, count: Int) -> some View {
        NavigationLink {
            NewToYouAllView(source: viewModel, list: list)
        } label: {
            Text("Show all \(count)")
                .font(.body)
                .foregroundStyle(.tint)
        }
        .row()
    }

    // MARK: - Loading and failed

    /// The section's own error and retry (S14.16, S14.17). The summary
    /// and the timeline above and below stay as they are.
    private func failure(_ error: APIError) -> some View {
        let text = Messages.failure(error)
        return HStack {
            Text("\(text.title). \(text.detail)")
                .font(.subheadline)
                .foregroundStyle(Theme.secondaryText)
            Spacer(minLength: Theme.Spacing.s)
            Button("Try again", action: retry)
                .font(.subheadline)
                .frame(minHeight: 44)
        }
        .row()
    }

    /// Rows of the real size, so the timeline below does not jump much
    /// when the lists arrive.
    private var placeholder: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            Text(list == .albums ? Messages.newAlbums(3) : Messages.newSingles(3)).font(.headline)
            ForEach(0..<3, id: \.self) { _ in
                NewItemRow(
                    title: "Placeholder name", subtitle: "Placeholder artist",
                    date: "1 Jan", detail: "3 tracks", spoken: "", showsCover: true)
            }
        }
        .redacted(reason: .placeholder)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading what's new to you")
        .row()
    }
}

private extension View {
    /// The main list's row insets.
    func row() -> some View {
        listRowInsets(Theme.rowInsets)
    }
}

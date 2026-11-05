import SwiftUI

/// "All albums" on the Albums tab (WHO-134, WHO-145): every album
/// played in the period, new and old, most played first. At most 10,
/// with "Show all" when there are more. On the Singles tab, "All singles
/// & EPs", the same for the singles and EPs.
///
/// Every piece is its own list row, so it scrolls with the tab's list.
/// All data comes from `AlbumsViewModel`. This file only lays it out.
struct AlbumsSection: View {
    let viewModel: AlbumsViewModel
    let retry: () -> Void

    var body: some View {
        if let answer = viewModel.answer {
            // An empty period hides the section, like "New to you". The
            // summary shows the empty message instead.
            if !answer.isEmpty {
                content(answer)
            }
        } else if case .failed(let error) = viewModel.status {
            heading
            failure(error)
        } else {
            heading
            placeholder
        }
    }

    // MARK: - Loaded

    @ViewBuilder
    private func content(_ answer: PlayedAlbums) -> some View {
        heading
        if answer.albums.items.isEmpty {
            Text(viewModel.singles ? Messages.singlesNone : Messages.albumsNone)
                .font(.body)
                .row()
        } else {
            ForEach(answer.albums.items, id: \.albumId) { album in
                // A tap opens the album's played tracks, as in "New to you".
                NavigationLink {
                    AlbumTracksView(album: album, source: viewModel)
                } label: {
                    PlayedAlbumRow(album: album)
                }
                .row()
            }
            if answer.albums.count > answer.albums.items.count {
                NavigationLink {
                    AlbumsAllView(source: viewModel)
                } label: {
                    Text("Show all \(answer.albums.count)")
                        .font(.body)
                        .foregroundStyle(.tint)
                }
                .row()
            }
        }
    }

    private var heading: some View {
        Text(viewModel.singles ? Messages.allSingles : Messages.allAlbums)
            .font(.title3.weight(.semibold))
            .accessibilityAddTraits(.isHeader)
            .padding(.top, Theme.Spacing.l)
            .row()
    }

    // MARK: - Loading and failed

    /// The section's own error and retry. The summary and "New to you"
    /// stay as they are.
    private func failure(_ error: APIError) -> some View {
        let text = Messages.failure(error)
        return HStack {
            Text("\(viewModel.singles ? Messages.singlesFailed : Messages.albumsFailed) \(text.detail)")
                .font(.subheadline)
                .foregroundStyle(Theme.secondaryText)
            Spacer(minLength: Theme.Spacing.s)
            Button("Try again", action: retry)
                .font(.subheadline)
                .frame(minHeight: 44)
        }
        .row()
    }

    /// Rows of the real size, so the screen below does not jump much
    /// when the albums arrive.
    private var placeholder: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            ForEach(0..<3, id: \.self) { _ in
                PlayedAlbumRow(album: PlayedAlbum(
                    albumId: "placeholder", name: "Placeholder album", artists: ["Placeholder artist"],
                    playCount: 12, tracksPlayedInPeriod: 3, totalMs: 0, isNew: false, artworkUrl: nil))
            }
        }
        .redacted(reason: .placeholder)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Loading albums")
        .row()
    }
}

private extension View {
    /// The main list's row insets.
    func row() -> some View {
        listRowInsets(Theme.rowInsets)
    }
}

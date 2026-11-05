import SwiftUI

/// "Show all": every album played in the period (WHO-134), or every
/// single and EP.
///
/// It asks the server again with `all=true` rather than keeping a full
/// copy around, so nothing beyond what is on screen is held (S6.5d).
struct AlbumsAllView: View {
    @State private var viewModel: AlbumsViewModel

    /// `source` is the section's model; this screen asks for the same period.
    init(source: AlbumsViewModel) {
        _viewModel = State(initialValue: source.makeShowAll())
    }

    var body: some View {
        content
            .navigationTitle(viewModel.answer.map { title($0.albums.count) } ?? title(nil))
            .navigationBarTitleDisplayMode(.inline)
            .task { await viewModel.load() }
    }

    @ViewBuilder
    private var content: some View {
        if let answer = viewModel.answer {
            List {
                ForEach(answer.albums.items, id: \.albumId) { album in
                    // The same tap as in the section.
                    NavigationLink {
                        AlbumTracksView(album: album, source: viewModel)
                    } label: {
                        PlayedAlbumRow(album: album)
                    }
                }
            }
            .listStyle(.plain)
            .overlay {
                if answer.albums.items.isEmpty {
                    ContentUnavailableView(title(0), systemImage: "square.stack")
                }
            }
        } else if case .failed(let error) = viewModel.status {
            let text = Messages.failure(error)
            ContentUnavailableView {
                Label(
                    Messages.failureTitle(error, section: viewModel.singles ? Messages.singlesFailed : Messages.albumsFailed),
                    systemImage: text.systemImage)
            } description: {
                Text(text.detail)
            } actions: {
                Button("Try again") { Task { await viewModel.load() } }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
            }
        } else {
            ProgressView("Loading")
        }
    }

    /// "12 albums", or "12 singles & EPs"; without a count, the kind.
    private func title(_ count: Int?) -> String {
        if viewModel.singles {
            return count.map(Messages.singlesAndEPs) ?? Messages.allSingles
        }
        return count.map(Messages.albums) ?? "Albums"
    }
}

import SwiftUI

/// Which of the two "New to you" lists a "Show all" screen shows.
enum NewToYouList {
    case albums, singles
}

/// "Show all": every new album, or every new single and EP, of the period (S14.7).
///
/// It asks the server again with `all=true` rather than keeping a full
/// copy around, so nothing beyond what is on screen is held (S6.5d).
struct NewToYouAllView: View {
    let list: NewToYouList
    @State private var viewModel: NewToYouViewModel

    /// `source` is the section's model; this screen asks for the same period.
    init(source: NewToYouViewModel, list: NewToYouList) {
        self.list = list
        _viewModel = State(initialValue: source.makeShowAll())
    }

    var body: some View {
        content
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .task { await viewModel.load() }
            .notNewBanner(viewModel.marker)
    }

    @ViewBuilder
    private var content: some View {
        // `shown` leaves out rows just marked "Not new" (WHO-123).
        if let answer = viewModel.shown {
            let timeZone = answer.zone
            List {
                ForEach(items(in: answer).items, id: \.albumId) { album in
                    // WHO-115: the same tap as in the section.
                    NavigationLink {
                        AlbumTracksView(album: album, source: viewModel)
                    } label: {
                        NewItemRow(album: album, timeZone: timeZone)
                    }
                    .notNewActions(album.notNewMark, marker: viewModel.marker)
                }
            }
            .listStyle(.plain)
            .overlay {
                if count(in: answer) == 0 {
                    ContentUnavailableView(heading(count: 0), systemImage: "sparkles")
                }
            }
        } else if case .failed(let error) = viewModel.status {
            let text = Messages.failure(error)
            ContentUnavailableView {
                Label(text.title, systemImage: text.systemImage)
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

    /// "23 new albums" once loaded, "New albums" before.
    private var title: String {
        guard let answer = viewModel.shown else {
            switch list {
            case .albums: return "New albums"
            case .singles: return "New singles & EPs"
            }
        }
        return heading(count: count(in: answer))
    }

    private func items(in answer: NewToYou) -> NewAlbums {
        switch list {
        case .albums: return answer.albums
        case .singles: return answer.singles
        }
    }

    private func count(in answer: NewToYou) -> Int {
        items(in: answer).count
    }

    private func heading(count: Int) -> String {
        switch list {
        case .albums: return Messages.newAlbums(count)
        case .singles: return Messages.newSingles(count)
        }
    }
}

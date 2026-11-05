import SwiftUI

/// "Show all" under "Plays" (WHO-131): every play of the period, newest
/// first, under sticky day headers, with the next page loading before
/// the user reaches the bottom. The same list the main screen had
/// before WHO-131.
///
/// It loads the period again on its own model, so the main screen's
/// model keeps only its first page.
struct TimelineAllView: View {
    @State private var viewModel: TimelineViewModel
    /// The track whose comment editor is open.
    @State private var trackComment: TrackCommentModel?

    /// `source` is the main screen's model; this screen asks for the same period.
    init(source: TimelineViewModel) {
        _viewModel = State(initialValue: source.makeShowAll())
    }

    var body: some View {
        content
            .navigationTitle("Plays")
            .navigationBarTitleDisplayMode(.inline)
            .task { await viewModel.load() }
    }

    @ViewBuilder
    private var content: some View {
        switch viewModel.status {
        case .loading where viewModel.days.isEmpty:
            ProgressView("Loading")
        case .failed(let error):
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
        case .empty(let reason):
            let text = Messages.empty(reason: reason)
            ContentUnavailableView {
                Label(text.title, systemImage: text.systemImage)
            } description: {
                Text(text.detail)
            }
        default:
            List {
                if let error = viewModel.bannerError {
                    Text(Messages.banner(error))
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                        .listRowSeparator(.hidden)
                }
                PlayDaySections(days: viewModel.days, viewModel: viewModel, pages: true) { event in
                    trackComment = viewModel.trackComment(event)
                }
            }
            .listStyle(.plain)
            .sheet(item: $trackComment) { model in
                CommentEditor(name: model.title, viewModel: model)
            }
        }
    }
}

import SwiftUI

/// Search, pushed from the main screen's toolbar: the albums, singles,
/// EPs and songs of the whole history that hold every word typed, by
/// name, artist or, for an album, the user's comment. The words found
/// are marked in each row, so a comment shows why it came up.
///
/// An album opens its screen, where the rating and comment can be
/// changed; a song opens its album when the server knows it.
///
/// All data comes from `SearchViewModel`. This file only lays it out.
struct SearchView: View {
    @Bindable var viewModel: SearchViewModel
    /// The display zone, for the dates of last plays.
    let timeZone: TimeZone
    /// The song whose comment editor is open.
    @State private var trackComment: TrackCommentModel?

    var body: some View {
        content
            .navigationTitle(Messages.searchTitle)
            .navigationBarTitleDisplayMode(.inline)
            .searchable(
                text: $viewModel.text, placement: .navigationBarDrawer(displayMode: .always),
                prompt: Messages.searchPrompt)
            .focusesSearchField()
            .autocorrectionDisabled()
            .task(id: viewModel.text) { await viewModel.search() }
            .sheet(item: $trackComment) { model in
                CommentEditor(name: model.title, viewModel: model)
            }
            .toolbar {
                if viewModel.status == .loading && viewModel.answer != nil {
                    ToolbarItem(placement: .topBarTrailing) {
                        ProgressView().accessibilityLabel("Loading")
                    }
                }
            }
    }

    @ViewBuilder
    private var content: some View {
        switch viewModel.status {
        case .idle:
            ContentUnavailableView {
                Label(Messages.searchTitle, systemImage: "magnifyingglass")
            } description: {
                Text(Messages.searchHint)
            }
        case .tooLong:
            ContentUnavailableView {
                Label(Messages.searchTitle, systemImage: "magnifyingglass")
            } description: {
                Text(Messages.searchTooLong)
            }
        case .failed(let error):
            let text = Messages.failure(error)
            ContentUnavailableView {
                Label(Messages.searchFailed, systemImage: text.systemImage)
            } description: {
                Text(text.detail)
            } actions: {
                Button("Try again") { Task { await viewModel.reload() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loading, .loaded:
            if let answer = viewModel.answer {
                if answer.isEmpty {
                    ContentUnavailableView.search(text: viewModel.answeredWords.joined(separator: " "))
                } else {
                    results(answer)
                }
            } else {
                ProgressView("Loading")
            }
        }
    }

    private func results(_ answer: SearchAnswer) -> some View {
        let words = viewModel.answeredWords
        return List {
            if !answer.albums.items.isEmpty {
                Section {
                    ForEach(answer.albums.items, id: \.albumId) { album in
                        albumRow(album, words: words)
                    }
                    more(shown: answer.albums.items.count, count: answer.albums.count)
                } header: {
                    Text(Messages.searchAlbums)
                }
            }
            if !answer.tracks.items.isEmpty {
                Section {
                    ForEach(answer.tracks.items, id: \.itemKey) { track in
                        trackRow(track, words: words)
                    }
                    more(shown: answer.tracks.items.count, count: answer.tracks.count)
                } header: {
                    Text(Messages.searchSongs)
                }
            }
        }
        .listStyle(.plain)
        .scrollDismissesKeyboard(.immediately)
    }

    @ViewBuilder
    private func albumRow(_ album: FoundAlbum, words: [String]) -> some View {
        let row = FoundAlbumRow(album: album, words: words, timeZone: timeZone)
            .listRowInsets(Theme.rowInsets)
        // An album found by its comment alone has no plays to show.
        if let last = album.lastPlayedLocalDate {
            NavigationLink {
                AlbumTracksView(album: album, lastPlayedLocalDate: last, source: viewModel)
            } label: {
                row
            }
        } else {
            row
        }
    }

    /// A song opens its album when the server knows it, and its comment
    /// editor when not; a long press always offers the comment.
    @ViewBuilder
    private func trackRow(_ track: FoundTrack, words: [String]) -> some View {
        let row = FoundTrackRow(track: track, words: words, timeZone: timeZone)
        let edit = { trackComment = viewModel.trackComment(track) }
        Group {
            if let albumId = track.albumId {
                NavigationLink {
                    AlbumTracksView(track: track, albumId: albumId, source: viewModel)
                } label: {
                    row
                }
            } else {
                Button(action: edit) {
                    row.contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .listRowInsets(Theme.rowInsets)
        .contextMenu {
            Button(Messages.trackCommentAction(track.comment), systemImage: "square.and.pencil", action: edit)
        }
        .accessibilityAction(named: Messages.trackCommentAction(track.comment), edit)
    }

    @ViewBuilder
    private func more(shown: Int, count: Int) -> some View {
        if count > shown {
            Text(Messages.searchMore(shown: shown, count: count))
                .font(.footnote)
                .foregroundStyle(Theme.secondaryText)
                .listRowInsets(Theme.rowInsets)
        }
    }
}

/// One found album: cover, name with the rating after it, kind and
/// artists, plays and the last one, then the user's comment, if any,
/// with the words searched for marked.
struct FoundAlbumRow: View {
    let album: FoundAlbum
    let words: [String]
    let timeZone: TimeZone

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            CoverImage(url: album.artworkUrl)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                    Text(SearchHighlight.marked(name, words: words))
                        .font(.body.weight(.medium))
                        .lineLimit(2)
                    if let rating = album.rating {
                        RatingBadge(rating: rating)
                    }
                }
                Text(SearchHighlight.marked(byline, words: words))
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(1)
                Text(Messages.searchPlays(album.playCount, lastPlayedLocalDate: album.lastPlayedLocalDate, timeZone: timeZone))
                    .font(.footnote)
                    .foregroundStyle(Theme.secondaryText)
                if let comment = album.comment {
                    Text(SearchHighlight.marked(comment, words: words))
                        .font(.subheadline)
                        .lineLimit(4)
                        .padding(.top, Theme.Spacing.xs)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// One sentence, then the comment in full: here it may be why the
    /// album was found.
    var spoken: String {
        let plays = Messages.searchPlays(album.playCount, lastPlayedLocalDate: album.lastPlayedLocalDate, timeZone: timeZone)
        let comment = album.comment.map { " Your comment: \($0)" } ?? ""
        return "\(name), \(byline). \(plays).\(Messages.ratingSpoken(album.rating))\(comment)"
    }

    private var name: String { album.name ?? String(localized: "Unknown album") }

    private var byline: String {
        let artists = album.artists.isEmpty ? String(localized: "Unknown artist") : album.artists.joined(separator: ", ")
        return [album.releaseType.label, artists].compactMap { $0 }.joined(separator: " · ")
    }
}

/// One found song: cover, title over artists and album, plays and the
/// last one, then the user's comment, if any, with the words searched
/// for marked.
struct FoundTrackRow: View {
    let track: FoundTrack
    let words: [String]
    let timeZone: TimeZone

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            CoverImage(url: track.artworkUrl)
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                Text(SearchHighlight.marked(track.title, words: words))
                    .font(.body.weight(.medium))
                    .lineLimit(2)
                Text(SearchHighlight.marked(byline, words: words))
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(2)
                Text(Messages.searchPlays(track.playCount, lastPlayedLocalDate: track.lastPlayedLocalDate, timeZone: timeZone))
                    .font(.footnote)
                    .foregroundStyle(Theme.secondaryText)
                if let comment = track.comment {
                    Text(SearchHighlight.marked(comment, words: words))
                        .font(.subheadline)
                        .lineLimit(4)
                        .padding(.top, Theme.Spacing.xs)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    var spoken: String {
        let plays = Messages.searchPlays(track.playCount, lastPlayedLocalDate: track.lastPlayedLocalDate, timeZone: timeZone)
        let comment = track.comment.map { " Your comment: \($0)" } ?? ""
        return "\(track.title), \(byline). \(plays).\(comment)"
    }

    private var byline: String {
        let artists = track.creators.isEmpty ? String(localized: "Unknown artist") : track.creators.joined(separator: ", ")
        return [artists, track.album].compactMap { $0 }.joined(separator: " · ")
    }
}

/// Marks the searched words in a row's text: bold, in the tint colour.
/// Case is ignored, as the server ignores it.
enum SearchHighlight {
    static func marked(_ text: String, words: [String]) -> AttributedString {
        var marked = AttributedString(text)
        for range in ranges(of: words, in: text) {
            guard let attributed = Range(range, in: marked) else { continue }
            marked[attributed].inlinePresentationIntent = .stronglyEmphasized
            marked[attributed].foregroundColor = .accentColor
        }
        return marked
    }

    /// Every place a word occurs in `text`, ignoring case.
    static func ranges(of words: [String], in text: String) -> [Range<String.Index>] {
        var found: [Range<String.Index>] = []
        for word in words where !word.isEmpty {
            var start = text.startIndex
            while let range = text.range(of: word, options: .caseInsensitive, range: start..<text.endIndex) {
                found.append(range)
                start = range.upperBound
            }
        }
        return found
    }
}

private extension View {
    /// Puts the cursor in the search field when the screen opens, where
    /// the system can (iOS 18 and later).
    @ViewBuilder
    func focusesSearchField() -> some View {
        if #available(iOS 18.0, *) {
            modifier(SearchFieldFocus())
        } else {
            self
        }
    }
}

@available(iOS 18.0, *)
private struct SearchFieldFocus: ViewModifier {
    @FocusState private var focused: Bool

    func body(content: Content) -> some View {
        content
            .searchFocused($focused)
            .onAppear { focused = true }
    }
}

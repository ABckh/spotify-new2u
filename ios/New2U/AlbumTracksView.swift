import SwiftUI

/// One album, opened from "New to you", "Albums" or Search: the tracks
/// of that album the user played in the period, most played first
/// (WHO-115, WHO-134).
///
/// It asks the server when it opens rather than keeping a copy, so
/// nothing beyond what is on screen is held (S6.5d).
struct AlbumTracksView: View {
    /// From the row that was tapped, for the title while the list loads.
    let name: String?
    let artists: [String]
    @State private var viewModel: AlbumTracksViewModel
    /// The comment editor sheet is open.
    @State private var editsComment = false
    /// The track whose comment editor is open.
    @State private var trackComment: TrackCommentModel?

    /// From "New to you". `source` is the section's model; this screen
    /// asks for the same period.
    init(album: NewAlbum, source: NewToYouViewModel) {
        name = album.name
        artists = album.artists
        _viewModel = State(initialValue: source.makeAlbumTracks(albumId: album.albumId))
    }

    /// From "Albums", the same way.
    init(album: PlayedAlbum, source: AlbumsViewModel) {
        name = album.name
        artists = album.artists
        _viewModel = State(initialValue: source.makeAlbumTracks(albumId: album.albumId))
    }

    /// From Search: the 366 days that end with the album's last play.
    init(album: FoundAlbum, lastPlayedLocalDate: String, source: SearchViewModel) {
        name = album.name
        artists = album.artists
        _viewModel = State(initialValue: source.makeAlbumTracks(
            albumId: album.albumId, lastPlayedLocalDate: lastPlayedLocalDate))
    }

    /// From a found song: its album, the same way as a found album.
    init(track: FoundTrack, albumId: String, source: SearchViewModel) {
        name = track.album
        artists = track.creators
        _viewModel = State(initialValue: source.makeAlbumTracks(
            albumId: albumId, lastPlayedLocalDate: track.lastPlayedLocalDate))
    }

    var body: some View {
        content
            .navigationTitle(name ?? String(localized: "Unknown album"))
            .navigationBarTitleDisplayMode(.inline)
            .task { await viewModel.load() }
            .notNewBanner(viewModel.marker)
            .sheet(isPresented: $editsComment, onDismiss: viewModel.dismissCommentFailure) {
                CommentEditor(name: name ?? String(localized: "Unknown album"), viewModel: viewModel)
            }
            .sheet(item: $trackComment) { model in
                CommentEditor(name: model.title, viewModel: model)
            }
    }

    @ViewBuilder
    private var content: some View {
        if let answer = viewModel.answer {
            List {
                AlbumHeader(
                    artworkUrl: answer.album.artworkUrl,
                    artists: answer.album.artists.isEmpty ? artists : answer.album.artists)
                    .listRowSeparator(.hidden)
                RatingPicker(rating: viewModel.rating) { value in
                    Task { await viewModel.rate(value) }
                }
                .listRowSeparator(.hidden)
                if let failure = viewModel.ratingFailure {
                    Text(failure)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .listRowSeparator(.hidden)
                }
                // A failed save shows in the editor, which stays open.
                CommentSection(comment: viewModel.comment, edit: { editsComment = true })
                    .listRowSeparator(.hidden)
                Section {
                    ForEach(answer.tracks, id: \.itemKey) { track in
                        // A tap writes or edits the track's comment.
                        let row = Button {
                            trackComment = viewModel.trackComment(track)
                        } label: {
                            AlbumTrackRow(track: track, showsNew: viewModel.showsNew(track))
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint(Messages.trackCommentHint(track.comment))
                        if viewModel.showsNew(track) {
                            // WHO-123: only a song shown as new can be
                            // marked "Not new". It stays in the list, as a
                            // played song, without the tag.
                            row.notNewActions(track.notNewMark, marker: viewModel.marker)
                        } else {
                            row
                        }
                    }
                }
            }
            .listStyle(.plain)
            .overlay {
                if answer.tracks.isEmpty {
                    ContentUnavailableView(
                        Messages.albumNoPlays,
                        systemImage: "music.note",
                        description: Text(Messages.albumNoPlaysDetail))
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
}

/// The top of the album screen: the large cover, then the artists under
/// the title, so the screen reads "Album" then "by Artist". At the
/// largest text sizes the cover is dropped, as in the rows.
struct AlbumHeader: View {
    let artworkUrl: String?
    let artists: [String]

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            if !typeSize.isAccessibilitySize {
                CoverImage(url: artworkUrl, size: Theme.headerArtworkSize)
            }
            Text(AlbumTrackRow.names(artists))
                .font(.subheadline)
                .foregroundStyle(Theme.secondaryText)
        }
        .padding(.vertical, Theme.Spacing.xs)
    }
}

/// One track of the album: title over artists, play count on the right,
/// and a small "New" tag when the track was first played in the period.
struct AlbumTrackRow: View {
    let track: AlbumTrack
    /// The "New" tag. Off for a song just marked "Not new".
    let showsNew: Bool

    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        Group {
            if typeSize.isAccessibilitySize {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    titleLine(lineLimit: 2)
                    Text(Self.names(track.creators))
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                        .lineLimit(2)
                    commentLine
                    Text(Messages.plays(track.playCount))
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.secondaryText)
                }
            } else {
                HStack(spacing: Theme.Spacing.m) {
                    VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                        titleLine(lineLimit: 1)
                        Text(Self.names(track.creators))
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondaryText)
                            .lineLimit(1)
                        commentLine
                    }
                    Spacer(minLength: Theme.Spacing.s)
                    Text(Messages.plays(track.playCount))
                        .font(.subheadline)
                        .monospacedDigit()
                        .foregroundStyle(Theme.secondaryText)
                        .fixedSize()
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }

    /// The user's comment on the track, under the artists.
    @ViewBuilder
    private var commentLine: some View {
        if let comment = track.comment {
            Text(comment)
                .font(.subheadline)
                .lineLimit(3)
                .padding(.top, Theme.Spacing.xs)
        }
    }

    private func titleLine(lineLimit: Int) -> some View {
        HStack(spacing: Theme.Spacing.xs) {
            Text(track.title)
                .font(.body.weight(.medium))
                .lineLimit(lineLimit)
            if showsNew {
                NewTag()
            }
        }
    }

    /// What VoiceOver reads: one sentence for the whole row.
    var spoken: String {
        let new = showsNew ? " New to you." : ""
        let comment = track.comment.map { " Your comment: \($0)" } ?? ""
        return "\(track.title), by \(Self.names(track.creators)). \(Messages.plays(track.playCount)) in this period.\(new)\(comment)"
    }

    static func names(_ names: [String]) -> String {
        names.isEmpty ? String(localized: "Unknown artist") : names.joined(separator: ", ")
    }
}

/// The small "New" tag after a title, on songs and albums.
struct NewTag: View {
    var body: some View {
        Text("New")
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(.tint)
            .background(Capsule().strokeBorder(.tint))
            .fixedSize()
    }
}

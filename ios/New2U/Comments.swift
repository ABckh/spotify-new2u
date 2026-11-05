import SwiftUI

/// "Your comment" on the album screen, under the rating: the comment in
/// full with Edit, or "Add a comment". Both open the editor; emptying the
/// text there and saving clears the comment. The comment and the rating
/// are independent: neither needs the other.
struct CommentSection: View {
    let comment: String?
    let edit: () -> Void

    var body: some View {
        if let comment {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                HStack(spacing: Theme.Spacing.m) {
                    Text(Messages.commentTitle)
                        .font(.subheadline.weight(.semibold))
                        .accessibilityAddTraits(.isHeader)
                    Spacer()
                    // Borderless: in a list row, the button takes only its own taps.
                    Button(Messages.commentEdit, action: edit)
                        .buttonStyle(.borderless)
                }
                .font(.subheadline)
                Text(comment)
                    .font(.body)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        } else {
            Button(action: edit) {
                Label(Messages.commentAdd, systemImage: "square.and.pencil")
                    .font(.subheadline)
            }
            .buttonStyle(.borderless)
        }
    }
}

/// What `CommentEditor` writes to: an album's comment
/// (`AlbumTracksViewModel`) or a track's (`TrackCommentModel`).
protocol CommentSaving: AnyObject {
    /// The comment the server has, or nil.
    var comment: String? { get }
    /// The message of the last failed change, while it shows.
    var commentFailure: String? { get }
    /// True while a change is on its way.
    var isSavingComment: Bool { get }
    /// Saves `draft`; a blank draft clears the comment. True once the
    /// server has it, or when nothing changed.
    func saveComment(_ draft: String) async -> Bool
    func dismissCommentFailure()
}

/// The sheet that writes or edits a comment, an album's or a track's.
/// Save sends it and closes only once the server has it; on a failure
/// the draft stays and the reason shows under it. Saving a blank draft
/// clears the comment: the one way to remove it. The count is the
/// server's (Unicode scalars, up to 2000), so a draft the server would
/// refuse cannot be saved.
struct CommentEditor: View {
    /// The album's or track's name, over the text.
    let name: String
    let viewModel: any CommentSaving

    @Environment(\.dismiss) private var dismiss
    @State private var draft: String
    @FocusState private var focused: Bool

    init(name: String, viewModel: any CommentSaving) {
        self.name = name
        self.viewModel = viewModel
        _draft = State(initialValue: viewModel.comment ?? "")
    }

    private var length: Int { AlbumTracksViewModel.commentLength(draft) }
    private var tooLong: Bool { length > Messages.commentMaxLength }
    private var unchanged: Bool { AlbumTracksViewModel.commentToSend(draft) == viewModel.comment }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                Text(name)
                    .font(.subheadline)
                    .foregroundStyle(Theme.secondaryText)
                    .lineLimit(1)
                TextEditor(text: $draft)
                    .focused($focused)
                    .scrollContentBackground(.hidden)
                    .padding(Theme.Spacing.xs)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Theme.placeholderFill))
                    .overlay(alignment: .topLeading) {
                        if draft.isEmpty {
                            Text(Messages.commentPlaceholder)
                                .foregroundStyle(Theme.secondaryText)
                                .padding(.horizontal, Theme.Spacing.s + 1)
                                .padding(.vertical, Theme.Spacing.s + 4)
                                .allowsHitTesting(false)
                                .accessibilityHidden(true)
                        }
                    }
                    .accessibilityLabel(Messages.commentTitle)
                HStack(alignment: .firstTextBaseline) {
                    if let failure = viewModel.commentFailure {
                        Text(failure)
                            .foregroundStyle(.red)
                    }
                    Spacer()
                    Text(Messages.commentCount(length))
                        .monospacedDigit()
                        .foregroundStyle(tooLong ? AnyShapeStyle(.red) : AnyShapeStyle(Theme.secondaryText))
                }
                .font(.footnote)
            }
            .padding()
            .navigationTitle(Messages.commentTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(Messages.commentCancel) { dismiss() }
                        .disabled(viewModel.isSavingComment)
                }
                ToolbarItem(placement: .confirmationAction) {
                    if viewModel.isSavingComment {
                        ProgressView()
                    } else {
                        Button(Messages.commentSave) {
                            Task {
                                if await viewModel.saveComment(draft) { dismiss() }
                            }
                        }
                        .disabled(tooLong || unchanged)
                    }
                }
            }
            .onAppear { focused = true }
        }
        // A swipe must not throw away what was typed; Cancel still can.
        .interactiveDismissDisabled(!unchanged || viewModel.isSavingComment)
    }
}

/// One track's comment, for `CommentEditor`: opened from a track on an
/// album screen, a play on the Tracks tab or a song in Search.
/// `changed` runs with the new comment (nil when cleared) once the
/// server has it, so the screen that opened it can show it.
@Observable
final class TrackCommentModel: CommentSaving, Identifiable {
    let itemKey: String
    /// The track's title, for the editor.
    let title: String
    private(set) var comment: String?
    private(set) var commentFailure: String?
    private(set) var isSavingComment = false
    var changed: (String?) async -> Void = { _ in }

    var id: String { itemKey }

    private let api: APIClient

    init(api: APIClient, itemKey: String, title: String, comment: String?) {
        self.api = api
        self.itemKey = itemKey
        self.title = title
        self.comment = comment
    }

    @discardableResult
    func saveComment(_ draft: String) async -> Bool {
        let text = AlbumTracksViewModel.commentToSend(draft)
        guard text != comment else { return true }
        if let text, text.unicodeScalars.count > Messages.commentMaxLength { return false }
        isSavingComment = true
        commentFailure = nil
        defer { isSavingComment = false }
        do {
            if let text {
                // A track key is "id:" or "nt:" and letters or hex digits,
                // so it goes into the path as it is.
                try await api.sendJSON(
                    "/v1/tracks/\(itemKey)/comment", method: "PUT", body: CommentBody(comment: text))
            } else {
                try await api.send(method: "DELETE", pathSegments: ["v1", "tracks", itemKey, "comment"])
            }
        } catch {
            commentFailure = Messages.commentFailed((error as? APIError) ?? .badResponse)
            return false
        }
        comment = text
        await changed(text)
        return true
    }

    func dismissCommentFailure() {
        commentFailure = nil
    }
}

/// A small speech bubble after an album's or a track's name, on the rows
/// of one that has the user's comment. The row's sentence says it is there.
struct CommentMark: View {
    var body: some View {
        Image(systemName: "text.bubble")
            .font(.caption)
            .foregroundStyle(Theme.secondaryText)
            .accessibilityHidden(true)
    }
}

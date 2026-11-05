import SwiftUI

/// The row on the Albums tab that opens the review, shown
/// while there is something to review.
struct ReviewOfferRow: View {
    let count: Int
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: Theme.Spacing.m) {
                Image(systemName: "checklist")
                    .font(.title3)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text(Messages.reviewOfferTitle)
                        .font(.body.weight(.medium))
                    Text(Messages.reviewOfferDetail(count))
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                }
                Spacer(minLength: Theme.Spacing.s)
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Theme.secondaryText)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .listRowInsets(Theme.rowInsets)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isButton)
    }
}

/// "Did you already know these?": tick what you knew, then one button.
///
/// Ticking sends nothing. "Mark N as known" marks the ticked items "Not
/// new" and keeps the rest new; with nothing ticked the button keeps
/// everything new. Either way the review is finished up to now, and
/// later first plays come up next time.
struct ReviewView: View {
    let viewModel: ReviewViewModel
    /// The display zone, for the first-play dates.
    let timeZone: TimeZone

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text(Messages.reviewIntro)
                        .font(.subheadline)
                        .foregroundStyle(Theme.secondaryText)
                }
                if let review = viewModel.review {
                    if review.count == 0 {
                        Text(Messages.reviewNothingLeft)
                    }
                    section(review, .albums, title: Messages.newAlbums(review.albums.count))
                    section(review, .singles, title: Messages.newSingles(review.singles.count))
                }
                if case .saveFailed(let error) = viewModel.status {
                    Text(Messages.reviewSaveFailed(error))
                        .foregroundStyle(.red)
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle(Messages.reviewTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .safeAreaInset(edge: .bottom) { finishButton }
        }
    }

    private var finishButton: some View {
        let count = viewModel.selected.count
        return Button {
            Task {
                if await viewModel.finish() { dismiss() }
            }
        } label: {
            Group {
                if viewModel.status == .saving {
                    ProgressView()
                } else {
                    Text(count == 0 ? Messages.reviewKeepAll : Messages.reviewMarkKnown(count))
                }
            }
            .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .disabled(viewModel.status == .saving || viewModel.review?.through == nil)
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.vertical, Theme.Spacing.s)
        .background(.bar)
    }

    /// One list with its heading, or nothing when it is empty.
    @ViewBuilder
    private func section(_ review: Review, _ list: NewToYouList, title: String) -> some View {
        let items = review.list(list).items
        if !items.isEmpty {
            Section {
                ForEach(items, id: \.albumId) { album in
                    tickRow(album.notNewMark) {
                        NewItemRow(album: album, timeZone: timeZone)
                    }
                }
            } header: {
                header(title, list: list)
            }
        }
    }

    private func header(_ title: String, list: NewToYouList) -> some View {
        HStack {
            Text(title)
            Spacer()
            Button(viewModel.allSelected(list) ? Messages.reviewClearAll : Messages.reviewSelectAll) {
                viewModel.toggleAll(list)
            }
            .font(.subheadline)
            .textCase(nil)
        }
    }

    private func tickRow(_ mark: NotNewMark, @ViewBuilder content: () -> some View) -> some View {
        let selected = viewModel.isSelected(mark)
        return Button {
            viewModel.toggle(mark)
        } label: {
            HStack(spacing: Theme.Spacing.m) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(selected ? AnyShapeStyle(.tint) : AnyShapeStyle(Theme.secondaryText))
                    .accessibilityHidden(true)
                content()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? [.isSelected] : [])
    }
}

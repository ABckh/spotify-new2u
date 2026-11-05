import SwiftUI

/// The sheet the Share button opens (WHO-196): the picture as it will be
/// sent, and a Share button for the system share sheet. Nothing leaves
/// the app until the user picks a target there (WHO-197 rule 16).
///
/// The PNG lives only in this sheet's `@State` and is dropped when the
/// sheet closes; opening it again draws it again (rule 8).
struct SharePreviewSheet: View {
    let card: ShareCardModel

    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase

    @State private var picture: SharePicture?
    @State private var preview: UIImage?
    @State private var failed = false

    var body: some View {
        NavigationStack {
            content
                .padding(Theme.Spacing.l)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .navigationTitle("Share new albums")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                }
        }
        // `PrivacyCoverWindow` already covers sheets in the app switcher;
        // this is the sheet's own cover too, as rule 11 asks.
        .overlay {
            if scenePhase != .active {
                PrivacyCover()
            }
        }
        // Rule 12: the sheet closes when the app goes to the background.
        .onChange(of: scenePhase) { _, phase in
            if phase == .background {
                dismiss()
            }
        }
        .task {
            // Covers not yet in memory are loaded first, at most 3 s,
            // while the spinner shows (rule 4). Then the picture is
            // drawn once, before Share can be tapped (rule 9).
            let covers = await ShareCard.loadCovers(card)
            guard !Task.isCancelled else { return }
            if let png = ShareCard.png(card, covers: covers), let image = UIImage(data: png) {
                picture = SharePicture(png: png)
                preview = image
            } else {
                failed = true
            }
        }
        .onDisappear {
            picture = nil
            preview = nil
        }
    }

    /// While the picture is drawn, the sheet already has its final
    /// layout: a dark frame of the picture's size with the spinner in it,
    /// and Share greyed out. The picture then takes the frame's place, so
    /// nothing moves when it appears.
    @ViewBuilder
    private var content: some View {
        if failed {
            ContentUnavailableView(
                "Couldn't make the picture",
                systemImage: "photo",
                description: Text("Close this and try again."))
        } else {
            VStack(spacing: Theme.Spacing.l) {
                Group {
                    if let preview {
                        Image(uiImage: preview)
                            .resizable()
                            .scaledToFit()
                            .accessibilityLabel(Messages.sharePictureLabel(card))
                    } else {
                        ShareCard.background
                            .aspectRatio(ShareCard.size, contentMode: .fit)
                            .overlay {
                                ProgressView("Making the picture…")
                                    .tint(.white)
                                    .foregroundStyle(.white)
                            }
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                Text("Only this picture is shared, and only with the app you pick.")
                    .font(.footnote)
                    .foregroundStyle(Theme.secondaryText)
                    .multilineTextAlignment(.center)
                if let picture, let preview {
                    ShareLink(
                        item: picture,
                        preview: SharePreview(Messages.shareHeadline, image: Image(uiImage: preview))
                    ) {
                        shareLabel
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    Button {} label: { shareLabel }
                        .buttonStyle(.borderedProminent)
                        .disabled(true)
                }
            }
        }
    }

    private var shareLabel: some View {
        Label("Share", systemImage: "square.and.arrow.up")
            .frame(maxWidth: .infinity, minHeight: 44)
    }
}

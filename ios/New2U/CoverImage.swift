import SwiftUI

/// An album cover, or the neutral placeholder while it loads, when it
/// fails and when there is no cover. Both are the same size, so a row
/// never moves when the cover arrives.
///
/// The cover is decorative: each row's spoken sentence already names the
/// track or album, so VoiceOver skips it (WHO-148 rule 6).
struct CoverImage: View {
    let url: String?
    let size: CGFloat
    @State private var image: UIImage?

    init(url: String?, size: CGFloat = Theme.artworkSize) {
        self.url = url
        self.size = size
        _image = State(initialValue: CoverImageLoader.shared.cachedImage(for: url))
    }

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size, height: size)
                    .clipShape(RoundedRectangle(cornerRadius: ArtworkPlaceholder.cornerRadius(size), style: .continuous))
            } else {
                ArtworkPlaceholder(size: size)
            }
        }
        .accessibilityHidden(true)
        .task(id: url) {
            image = CoverImageLoader.shared.cachedImage(for: url)
            if image == nil {
                image = await CoverImageLoader.shared.image(for: url)
            }
        }
    }
}

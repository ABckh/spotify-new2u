import CoreTransferable
import SwiftUI
import UniformTypeIdentifiers

/// The share picture itself (WHO-196): 360 × 640 points, drawn at 3× to
/// a 1080 × 1920 PNG, the size of an Instagram story.
///
/// A fixed layout on a dark background: fixed font sizes (not Dynamic
/// Type) and fixed colours, so it looks the same on every phone and in
/// light and dark mode. Only what `ShareCardModel` holds is drawn.
struct ShareCard: View {
    let card: ShareCardModel
    /// One per album, in the same order. nil draws the placeholder.
    let covers: [UIImage?]

    /// The picture's size in points, and the scale that makes it 1080 × 1920.
    static let size = CGSize(width: 360, height: 640)
    static let scale: CGFloat = 3

    /// The picture's background, also the preview's while it is drawn.
    static let background = Color(red: 0.07, green: 0.07, blue: 0.09)

    /// The grid for this many albums in `size`, the space under the
    /// header: the column count that gives the largest covers, so the
    /// covers fill the picture, up to `maxCover` points each. The rows
    /// are as even as they can be, and each is centred: 17 albums are
    /// rows of 4, 4, 3, 3 and 3, never a full grid with one cover left
    /// alone under it. Names and artists are under the covers up to 12
    /// albums; past that only the covers fit.
    struct Grid: Equatable {
        let columns: Int
        /// How many covers each row holds, top row first.
        let rows: [Int]
        let coverSize: CGFloat
        /// Between the covers of a row.
        let gap: CGFloat
        /// Between rows: a little more when names sit under the covers.
        let rowGap: CGFloat
        let showsNames: Bool

        /// The album name and artist lines under a cover, with their spacing.
        static let namesHeight: CGFloat = 38
        static let maxCover: CGFloat = 150

        init(albumCount: Int, size: CGSize) {
            let count = max(albumCount, 1)
            let showsNames = albumCount <= 12
            var best = (columns: 1, rowCount: count, cover: CGFloat(0), gap: CGFloat(12), rowGap: CGFloat(12))
            for columns in 1...6 {
                let rowCount = (count + columns - 1) / columns
                let gap: CGFloat = columns <= 3 ? 12 : columns == 4 ? 10 : 8
                let rowGap = showsNames ? 10 : gap
                let byWidth = (size.width - CGFloat(columns - 1) * gap) / CGFloat(columns)
                let byHeight = (size.height - CGFloat(rowCount - 1) * rowGap) / CGFloat(rowCount)
                    - (showsNames ? Self.namesHeight : 0)
                let cover = min(byWidth, byHeight, Self.maxCover).rounded(.down)
                // At the same size, fewer rows: two albums side by side.
                if cover > best.cover || (cover == best.cover && rowCount < best.rowCount) {
                    best = (columns, rowCount, cover, gap, rowGap)
                }
            }
            let (base, extra) = count.quotientAndRemainder(dividingBy: best.rowCount)
            columns = best.columns
            rows = (0..<best.rowCount).map { $0 < extra ? base + 1 : base }
            coverSize = max(best.cover, 0)
            gap = best.gap
            rowGap = best.rowGap
            self.showsNames = showsNames
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // What the covers are, then the period they are from.
            Text(Messages.shareHeadline)
                .font(.system(size: 28, weight: .bold))
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(card.periodLabel)
                .font(.system(size: 18, weight: .medium))
                .foregroundStyle(.white.opacity(0.7))
                .padding(.top, 4)

            if let stats = card.stats {
                statsBlock(stats)
                    .padding(.top, 12)
            }

            // The covers take all the space left, centred in it.
            GeometryReader { space in
                let grid = Grid(albumCount: card.albums.count, size: space.size)
                VStack(spacing: grid.rowGap) {
                    ForEach(Array(Self.rows(grid).enumerated()), id: \.offset) { _, row in
                        HStack(alignment: .top, spacing: grid.gap) {
                            ForEach(row, id: \.self) { index in
                                album(index, grid: grid)
                            }
                        }
                    }
                }
                .frame(width: space.size.width, height: space.size.height)
            }
            .padding(.top, card.stats == nil ? 24 : 16)

            if card.moreCount > 0 {
                Text("+ \(card.moreCount) more")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.85))
                    .padding(.top, 16)
            }
        }
        .padding(24)
        .frame(width: Self.size.width, height: Self.size.height, alignment: .topLeading)
        .background(Self.background)
        // The placeholder's grey reads well on the dark background.
        .environment(\.colorScheme, .dark)
    }

    /// The total, top artist and top track, as the summary header at the
    /// top of every tab shows them.
    private func statsBlock(_ stats: ShareCardModel.Stats) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(stats.total)
                    .font(.system(size: 28, weight: .bold))
                    .monospacedDigit()
                    .foregroundStyle(.white)
                Text("listened")
                    .font(.system(size: 15, weight: .medium))
                    .foregroundStyle(.white.opacity(0.7))
            }
            if let artist = stats.topArtist {
                fact("Top artist", artist)
            }
            if let track = stats.topTrack {
                fact("Top track", track)
            }
        }
    }

    /// "Top artist  Name · 12 plays". A long name is written out in full
    /// over as many lines as it needs, never cut short; the covers below
    /// take the space that is left.
    private func fact(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(.white.opacity(0.6))
                .frame(width: 72, alignment: .leading)
            Text(value)
                .font(.system(size: 15, weight: .semibold))
                .foregroundStyle(.white)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The user's rating on a cover's corner: "8/10", white on a dark pill.
    private func ratingBadge(_ rating: Int, grid: Grid) -> some View {
        Text(Messages.ratingShort(rating))
            .font(.system(size: grid.coverSize < 64 ? 10 : 12, weight: .bold))
            .monospacedDigit()
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.black.opacity(0.75)))
            .padding(4)
    }

    /// The album indexes, row by row, in the server's order.
    static func rows(_ grid: Grid) -> [[Int]] {
        var start = 0
        return grid.rows.map { length in
            defer { start += length }
            return Array(start..<start + length)
        }
    }

    /// One cover, with the album name and first artist under it when the
    /// grid has room for them.
    private func album(_ index: Int, grid: Grid) -> some View {
        let album = card.albums[index]
        let coverSize = grid.coverSize
        return VStack(alignment: .leading, spacing: 4) {
            Group {
                if index < covers.count, let cover = covers[index] {
                    Image(uiImage: cover)
                        .resizable()
                        .scaledToFill()
                        .frame(width: coverSize, height: coverSize)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                } else {
                    ArtworkPlaceholder(size: coverSize)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if let rating = album.rating {
                    ratingBadge(rating, grid: grid)
                }
            }
            if grid.showsNames {
                Text(album.name)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                if let artist = album.artist {
                    Text(artist)
                        .font(.system(size: 12))
                        .foregroundStyle(.white.opacity(0.7))
                        .lineLimit(1)
                }
            }
        }
        .frame(width: coverSize, alignment: .leading)
    }

    /// The longest the preview waits for covers before it draws anyway.
    static let coverTimeLimit: Duration = .seconds(3)

    /// The covers for the picture, one per album, in the albums' order
    /// (WHO-201 rule 4).
    ///
    /// Only the card's albums (at most 25, never the "+ N more" ones).
    /// Covers already in memory come back at once; the others are loaded
    /// with `loader.image(for:)`, the same fetch the album rows use, all
    /// at the same time. After `timeLimit` the loads still running are
    /// cancelled and those albums get the placeholder (nil). A cover that
    /// is not an allowed `i.scdn.co` URL is never asked for. `loader` is
    /// only replaced by the unit tests.
    static func loadCovers(
        _ card: ShareCardModel,
        loader: CoverImageLoader = .shared,
        timeLimit: Duration = coverTimeLimit
    ) async -> [UIImage?] {
        var covers = card.albums.map { loader.cachedImage(for: $0.artworkUrl) }
        let missing = card.albums.indices.filter {
            covers[$0] == nil && CoverImageLoader.allowedURL(card.albums[$0].artworkUrl) != nil
        }
        guard !missing.isEmpty else { return covers }

        // One task per missing cover, and one that only waits out the
        // time limit. A nil result means the time is up.
        let load: @MainActor (String?) async -> UIImage? = { await loader.image(for: $0) }
        await withTaskGroup(of: (index: Int, image: UIImage?)?.self) { group in
            for index in missing {
                let artworkUrl = card.albums[index].artworkUrl
                group.addTask { (index, await load(artworkUrl)) }
            }
            group.addTask {
                try? await Task.sleep(for: timeLimit)
                return nil
            }
            var waitingFor = missing.count
            for await result in group {
                guard let result else { break }
                covers[result.index] = result.image
                waitingFor -= 1
                if waitingFor == 0 { break }
            }
            group.cancelAll()
        }
        return covers
    }

    /// Draws the picture to PNG data, or nil when drawing failed.
    ///
    /// `covers` comes from `loadCovers(_:)`; a nil cover is the
    /// placeholder. Nothing is written to disk (rule 6).
    static func png(_ card: ShareCardModel, covers: [UIImage?]) -> Data? {
        let renderer = ImageRenderer(content: ShareCard(card: card, covers: covers))
        renderer.scale = scale
        renderer.isOpaque = true
        return renderer.uiImage?.pngData()
    }
}

/// The finished PNG, handed to `ShareLink` (WHO-197 rules 5, 6 and 9).
///
/// It holds bytes drawn once, before the system share sheet opens, so
/// what is sent is exactly what the preview showed. `DataRepresentation`
/// hands the bytes over in memory; the app writes no file of its own.
struct SharePicture: Transferable {
    let png: Data

    static var transferRepresentation: some TransferRepresentation {
        DataRepresentation(exportedContentType: .png) { picture in
            picture.png
        }
        // A fixed name, never the user's name or ID (rule 5).
        .suggestedFileName("new-albums.png")
    }
}

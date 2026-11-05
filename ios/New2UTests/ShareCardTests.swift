import XCTest
@testable import New2U

/// The "New albums I listened to" share picture (WHO-196, WHO-197): which albums are on
/// it, "+ N more", "Not new" left out, and the PNG's size.
@MainActor
final class ShareCardTests: XCTestCase {
    /// `count` new albums, the first `sent` of them in `items`, the way
    /// the server sends the first 10.
    private func answer(
        count: Int, sent: Int, isEmpty: Bool = false,
        artworkUrl: (Int) -> String = { "https://i.scdn.co/image/cover\($0)" }
    ) throws -> NewToYou {
        let json = json(count: count, sent: sent, isEmpty: isEmpty, artworkUrl: artworkUrl)
        return try APIClient.makeDecoder().decode(NewToYou.self, from: Data(json.utf8))
    }

    /// The answer's JSON, as the server sends it.
    private func json(
        count: Int, sent: Int, isEmpty: Bool = false,
        artworkUrl: (Int) -> String = { "https://i.scdn.co/image/cover\($0)" }
    ) -> String {
        let albums = (0..<sent).map { index in
            """
            {"album_id": "album\(index)", "name": \(index == 1 ? "null" : "\"Album \(index)\""),
             "artists": \(index == 2 ? "[]" : "[\"Artist \(index)\", \"Guest\"]"),
             "first_played_at": "2026-10-05T18:00:00.000Z", "first_played_local_date": "2026-10-05",
             "tracks_played_in_period": 2, "tracks_listened": 8, "total_tracks": 10,
             "artwork_url": "\(artworkUrl(index))"}
            """
        }
        let json = """
        {"period": \(TimelineFixtures.period),
         "albums": {"count": \(count), "items": [\(albums.joined(separator: ","))]},
         "singles": {"count": 0, "items": []},
         "coverage_start_local_date": "2026-09-10",
         "period_contains_coverage_start": false,
         "nothing_new": \(count == 0),
         "is_empty": \(isEmpty), "empty_reason": null}
        """
        return json
    }

    func testShowsTheAlbumsInTheServersOrderWithNameAndFirstArtist() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 3, sent: 3)))
        XCTAssertEqual(card.albums.map(\.name), ["Album 0", "Unknown album", "Album 2"])
        XCTAssertEqual(card.albums.map(\.artist), ["Artist 0", "Artist 1", nil])
        XCTAssertEqual(card.albums.map(\.artworkUrl), [
            "https://i.scdn.co/image/cover0", "https://i.scdn.co/image/cover1", "https://i.scdn.co/image/cover2",
        ])
        XCTAssertEqual(card.moreCount, 0)
    }

    func testShowsAtMostTwentyFiveAndCountsTheRestAsMore() throws {
        // 31 new albums, all sent ("Show all").
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 31, sent: 31)))
        XCTAssertEqual(card.albums.count, 25)
        XCTAssertEqual(card.albums.last?.name, "Album 24")
        XCTAssertEqual(card.moreCount, 6)
    }

    func testOnlyTheAlbumsSentAreOnThePictureAndTheRestAreMore() throws {
        // 23 new albums, the server sent the first 10.
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 23, sent: 10)))
        XCTAssertEqual(card.albums.count, 10)
        XCTAssertEqual(card.moreCount, 13)
    }

    func testTwentyFiveAlbumsHaveNoMoreLine() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 25, sent: 25)))
        XCTAssertEqual(card.albums.count, 25)
        XCTAssertEqual(card.moreCount, 0)
    }

    /// The grid picks the largest covers that fit the space, with rows
    /// as even as they can be, and names stay while they fit.
    func testTheGridFillsTheSpaceWithEvenRows() {
        let space = CGSize(width: 312, height: 420)
        let grids = [1, 2, 5, 9, 12, 13, 17, 25].map { ShareCard.Grid(albumCount: $0, size: space) }
        XCTAssertEqual(grids.map(\.columns), [1, 2, 3, 3, 4, 3, 4, 5])
        XCTAssertEqual(grids.map(\.coverSize), [150, 150, 96, 95, 70, 74, 70, 56])
        XCTAssertEqual(grids.map(\.rows), [
            [1], [2], [3, 2], [3, 3, 3], [4, 4, 4], [3, 3, 3, 2, 2], [4, 4, 3, 3, 3], [5, 5, 5, 5, 5],
        ])
        XCTAssertEqual(grids.map(\.showsNames), [true, true, true, true, true, false, false, false])
    }

    /// Every row holds the albums after the row above, in the server's order.
    func testTheRowsKeepTheServersOrder() {
        let grid = ShareCard.Grid(albumCount: 17, size: CGSize(width: 312, height: 420))
        XCTAssertEqual(ShareCard.rows(grid), [
            [0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10], [11, 12, 13], [14, 15, 16],
        ])
    }

    /// Whatever the space, the covers and names never take more than it.
    func testTheGridNeverOverflowsItsSpace() {
        for count in 1...25 {
            for height in [300.0, 424.0, 490.0] {
                let grid = ShareCard.Grid(albumCount: count, size: CGSize(width: 312, height: height))
                let rows = CGFloat(grid.rows.count)
                let cell = grid.coverSize + (grid.showsNames ? ShareCard.Grid.namesHeight : 0)
                XCTAssertLessThanOrEqual(rows * cell + (rows - 1) * grid.rowGap, height, "\(count) in \(height)")
                let widest = CGFloat(grid.rows.max() ?? 0)
                XCTAssertLessThanOrEqual(widest * grid.coverSize + (widest - 1) * grid.gap, 312)
                XCTAssertEqual(grid.rows.reduce(0, +), count)
                XCTAssertLessThanOrEqual((grid.rows.max() ?? 0) - (grid.rows.min() ?? 0), 1)
            }
        }
    }

    /// Every count draws a full-size picture, the 25 covers too.
    func testEveryGridDrawsAFullSizePicture() throws {
        for count in [10, 16, 17, 25] {
            let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: count + 3, sent: count)))
            let png = try XCTUnwrap(ShareCard.png(card, covers: Array(repeating: nil, count: count)))
            let image = try XCTUnwrap(UIImage(data: png)?.cgImage)
            XCTAssertEqual(image.height, 1920)
        }
    }

    func testAlbumsMarkedNotNewAreLeftOut() throws {
        let full = try answer(count: 12, sent: 10)
        let shown = full.hiding([full.albums.items[0].notNewMark, full.albums.items[4].notNewMark])
        let card = try XCTUnwrap(ShareCardModel(newToYou: shown))
        XCTAssertFalse(card.albums.contains { $0.name == "Album 0" || $0.name == "Album 4" })
        XCTAssertEqual(card.albums.first?.name, "Unknown album")
        XCTAssertEqual(card.albums.count, 8)
        // 10 new albums are left: 8 on the picture, 2 more.
        XCTAssertEqual(card.moreCount, 2)
    }

    func testNoNewAlbumMeansNoPicture() throws {
        XCTAssertNil(ShareCardModel(newToYou: try answer(count: 0, sent: 0)))
        XCTAssertNil(ShareCardModel(newToYou: try answer(count: 2, sent: 2, isEmpty: true)))
        let one = try answer(count: 1, sent: 1)
        XCTAssertNil(ShareCardModel(newToYou: one.hiding([one.albums.items[0].notNewMark])))
    }

    func testThePeriodLabelIsThePeriodBarsRange() throws {
        let full = try answer(count: 1, sent: 1)
        let locale = Locale(identifier: "en_GB")
        let now = ISO8601DateFormatter().date(from: "2026-10-27T12:00:00Z")!
        let card = try XCTUnwrap(ShareCardModel(newToYou: full, locale: locale, now: now))
        XCTAssertEqual(card.periodLabel, DisplayFormat.periodRange(full.period, locale: locale, now: now))
    }

    func testThePictureIsAStorySizedPNG() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 12, sent: 10)))
        let png = try XCTUnwrap(ShareCard.png(card, covers: []))
        XCTAssertEqual(Array(png.prefix(4)), [0x89, 0x50, 0x4E, 0x47])
        let image = try XCTUnwrap(UIImage(data: png)?.cgImage)
        XCTAssertEqual(image.width, 1080)
        XCTAssertEqual(image.height, 1920)
    }

    func testTheVoiceOverLabelNamesOnlyThePeriodAndAlbums() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 11, sent: 10)))
        let label = Messages.sharePictureLabel(card)
        XCTAssertTrue(label.hasPrefix(
            "New albums I listened to, \(card.periodLabel). Albums: Album 0, Unknown album, Album 2"))
        XCTAssertTrue(label.hasSuffix(", and 1 more."))
    }

    // MARK: Numbers (the summary header's total, top artist, top track)

    private func summary(period: String = TimelineFixtures.period) throws -> Summary {
        let json = SummaryTests.contractExample.replacingOccurrences(of: TimelineFixtures.period, with: period)
        return try APIClient.makeDecoder().decode(Summary.self, from: Data(json.utf8))
    }

    func testThePictureHasTheSummaryNumbersOfTheSamePeriod() throws {
        let card = try XCTUnwrap(ShareCardModel(
            newToYou: answer(count: 3, sent: 3), summary: summary(), locale: Locale(identifier: "en_GB")))
        // 1,100,000 ms is 18 min 20 s; the header shows whole minutes.
        XCTAssertEqual(card.stats, ShareCardModel.Stats(
            total: "18m", topArtist: "Artist · 3 plays", topTrack: "Track · 2 plays"))
        XCTAssertEqual(
            Messages.sharePictureLabel(card),
            "New albums I listened to, \(card.periodLabel). 18m listened. Top artist: Artist · 3 plays. "
                + "Top track: Track · 2 plays. Albums: Album 0, Unknown album, Album 2.")
    }

    /// A long top track and artist are kept whole on the picture, which
    /// keeps its story size with all 25 covers under them.
    func testLongTopNamesAreKeptWholeAndThePictureKeepsItsSize() throws {
        let track = String(repeating: "A Very Long Song Title ", count: 6) + "End"
        let artist = "An Artist With A Long Name Too, And Another One"
        let json = SummaryTests.contractExample
            .replacingOccurrences(of: #""title": "Track""#, with: #""title": "\#(track)""#)
            .replacingOccurrences(of: #""name": "Artist""#, with: #""name": "\#(artist)""#)
        let summary = try APIClient.makeDecoder().decode(Summary.self, from: Data(json.utf8))
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 25, sent: 25), summary: summary))

        XCTAssertEqual(card.stats?.topTrack, "\(track) · 2 plays")
        XCTAssertEqual(card.stats?.topArtist, "\(artist) · 3 plays")
        let png = try XCTUnwrap(ShareCard.png(card, covers: []))
        let image = try XCTUnwrap(UIImage(data: png)?.cgImage)
        XCTAssertEqual(image.width, 1080)
        XCTAssertEqual(image.height, 1920)
    }

    func testASummaryOfAnotherPeriodIsLeftOff() throws {
        // The period bar just moved and only the summary has reloaded.
        let other = TimelineFixtures.period
            .replacingOccurrences(of: "2026-10-05", with: "2026-10-12")
            .replacingOccurrences(of: "2026-10-11", with: "2026-10-18")
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 3, sent: 3), summary: summary(period: other)))
        XCTAssertNil(card.stats)
    }

    func testNoSummaryMeansNoNumbersButStillAPicture() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 3, sent: 3), summary: nil))
        XCTAssertNil(card.stats)
    }

    func testThePictureWithNumbersAndNineAlbumsIsStillStorySized() throws {
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 23, sent: 10), summary: summary()))
        XCTAssertNotNil(card.stats)
        let png = try XCTUnwrap(ShareCard.png(card, covers: []))
        let image = try XCTUnwrap(UIImage(data: png)?.cgImage)
        XCTAssertEqual(image.width, 1080)
        XCTAssertEqual(image.height, 1920)
    }

    // MARK: Covers (WHO-201 rule 4, WHO-202)

    /// A loader with its own empty cache, answered by `StubURLProtocol`.
    private func makeLoader(_ stub: StubURLProtocol.Stub) -> CoverImageLoader {
        StubURLProtocol.reset()
        StubURLProtocol.handler = { _ in stub }
        let configuration = CoverImageLoader.makeURLSession().configuration
        configuration.protocolClasses = [StubURLProtocol.self]
        return CoverImageLoader(urlSession: URLSession(configuration: configuration))
    }

    private func coverStub() -> StubURLProtocol.Stub {
        let png = UIGraphicsImageRenderer(size: CGSize(width: 4, height: 4)).image { context in
            UIColor.red.setFill()
            context.fill(CGRect(x: 0, y: 0, width: 4, height: 4))
        }.pngData()
        return StubURLProtocol.Stub(headers: ["Content-Type": "image/png"], data: png)
    }

    private func requestedURLs() -> Set<String> {
        Set(StubURLProtocol.requestedURLs.map(\.absoluteString))
    }

    // Test 1: every cover on the card is loaded before the picture is drawn.
    func testNineMissingCoversAreLoadedFromSpotifyBeforeThePictureIsDrawn() async throws {
        let loader = makeLoader(coverStub())
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 9, sent: 9)))
        let covers = await ShareCard.loadCovers(card, loader: loader)
        XCTAssertEqual(StubURLProtocol.requestedURLs.count, 9)
        XCTAssertEqual(requestedURLs(), Set((0..<9).map { "https://i.scdn.co/image/cover\($0)" }))
        XCTAssertEqual(covers.count, 9)
        XCTAssertTrue(covers.allSatisfy { $0 != nil })
        XCTAssertNotNil(ShareCard.png(card, covers: covers))
    }

    // Test 2: a cover URL that is not allowed is never asked for.
    func testACoverFromAnotherHostIsNotAskedForAndIsThePlaceholder() async throws {
        let loader = makeLoader(coverStub())
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 3, sent: 3) {
            $0 == 1 ? "https://evil.example/image/cover1" : "https://i.scdn.co/image/cover\($0)"
        }))
        let covers = await ShareCard.loadCovers(card, loader: loader)
        XCTAssertEqual(requestedURLs(), ["https://i.scdn.co/image/cover0", "https://i.scdn.co/image/cover2"])
        XCTAssertNotNil(covers[0])
        XCTAssertNil(covers[1])
        XCTAssertNotNil(covers[2])
    }

    // Test 3: when Spotify never answers, the picture is drawn after
    // about 3 s with placeholders, and the requests are cancelled.
    func testANeverAnsweringServerStillDrawsThePictureAfterThreeSeconds() async throws {
        let loader = makeLoader(StubURLProtocol.Stub(neverAnswers: true))
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 3, sent: 3)))
        let started = ContinuousClock.now
        let covers = await ShareCard.loadCovers(card, loader: loader)
        let waited = ContinuousClock.now - started
        XCTAssertGreaterThanOrEqual(waited, .seconds(3))
        XCTAssertLessThan(waited, .seconds(4))
        XCTAssertEqual(covers.count, 3)
        XCTAssertTrue(covers.allSatisfy { $0 == nil })
        XCTAssertNotNil(ShareCard.png(card, covers: covers))
        // URLSession stops a cancelled request on its own thread, so give
        // it a moment.
        XCTAssertEqual(StubURLProtocol.requestedURLs.count, 3)
        for _ in 0..<50 where StubURLProtocol.stoppedCount < 3 {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertEqual(StubURLProtocol.stoppedCount, 3)
    }

    // Test 4: the "+ N more" albums are never fetched.
    func testOnlyTheTwentyFiveAlbumsOnThePictureAreFetched() async throws {
        let loader = makeLoader(coverStub())
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 30, sent: 30)))
        _ = await ShareCard.loadCovers(card, loader: loader)
        XCTAssertEqual(StubURLProtocol.requestedURLs.count, 25)
        XCTAssertFalse(requestedURLs().contains("https://i.scdn.co/image/cover25"))
    }

    func testCoversAlreadyInMemoryAreNotFetchedAgain() async throws {
        let loader = makeLoader(coverStub())
        let card = try XCTUnwrap(ShareCardModel(newToYou: answer(count: 2, sent: 2)))
        let inMemory = UIImage()
        loader.insert(inMemory, for: try XCTUnwrap(URL(string: "https://i.scdn.co/image/cover0")))
        let covers = await ShareCard.loadCovers(card, loader: loader)
        XCTAssertEqual(requestedURLs(), ["https://i.scdn.co/image/cover1"])
        XCTAssertTrue(covers[0] === inMemory)
        XCTAssertNotNil(covers[1])
    }
}

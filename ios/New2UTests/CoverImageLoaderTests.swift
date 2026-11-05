import XCTest
import UIKit
@testable import New2U

/// Covers from `i.scdn.co` (WHO-150): one test per WHO-148 rule the app
/// must keep.
@MainActor
final class CoverImageLoaderTests: XCTestCase {
    private let goodURL = "https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228"

    // The stub handler runs on URL loading's own thread, so every stub is
    // built on the main actor first and only captured by the handler.

    /// The loader's own session settings, answered by `StubURLProtocol`.
    private func makeLoader() -> CoverImageLoader {
        StubURLProtocol.reset()
        let configuration = CoverImageLoader.makeURLSession().configuration
        configuration.protocolClasses = [StubURLProtocol.self]
        return CoverImageLoader(urlSession: URLSession(configuration: configuration))
    }

    private func pngData(side: CGFloat = 4) -> Data {
        UIGraphicsImageRenderer(size: CGSize(width: side, height: side)).image { context in
            UIColor.orange.setFill()
            context.fill(CGRect(x: 0, y: 0, width: side, height: side))
        }.pngData()!
    }

    private func imageStub() -> StubURLProtocol.Stub {
        StubURLProtocol.Stub(headers: ["Content-Type": "image/jpeg"], data: pngData())
    }

    // MARK: - Rule 1: only https://i.scdn.co/image/…

    func testAllowsOnlySpotifyImageURLs() {
        XCTAssertNotNil(CoverImageLoader.allowedURL(goodURL))
        let refused = [
            nil, "", "not a url",
            "http://i.scdn.co/image/abc",
            "https://i.scdn.co.evil.example/image/abc",
            "https://user@i.scdn.co/image/abc",
            "https://user:pw@i.scdn.co/image/abc",
            "https://i.scdn.co:8443/image/abc",
            "https://example.com/image/abc",
            "https://evil.example/https://i.scdn.co/image/abc",
            "https://i.scdn.co/abc",
            "https://i.scdn.co/image/",
            "https://i.scdn.co/image/abc?track=1",
            "https://i.scdn.co/image/abc#x",
            "https://I.SCDN.CO/image/abc",
            "https://i.scdn.co/image/" + String(repeating: "a", count: 300),
            // WHO-154 R2: the path must stay under /image/, ASCII only,
            // and the limit counts bytes, not characters.
            "https://i.scdn.co/image/../x",
            "https://i.scdn.co/image/%2e%2e/x",
            "https://i.scdn.co/image/a%2Fb",
            "https://i.scdn.co/image/a/b",
            "https://i.scdn.co/image/a.jpg",
            "https://i.scdn.co/image/a b",
            "https://i.scdn.co/image/é",
            "https://i.scdn.co/image/\u{202E}x",
            "https://i.scdn.co/image/" + String(repeating: "👨‍👩‍👧‍👦", count: 100),
        ]
        for text in refused {
            XCTAssertNil(CoverImageLoader.allowedURL(text), "\(text ?? "nil") must be refused")
        }
    }

    func testRefusedURLsMakeNoRequest() async {
        let loader = makeLoader()
        let stub = imageStub()
        StubURLProtocol.handler = { _ in stub }
        for text in [
            "http://i.scdn.co/image/abc", "https://i.scdn.co.evil.example/image/abc",
            "https://user@i.scdn.co/image/abc", "https://i.scdn.co:8443/image/abc",
            "https://example.com/image/abc", nil,
        ] {
            let image = await loader.image(for: text)
            XCTAssertNil(image)
        }
        XCTAssertEqual(StubURLProtocol.requestCount, 0)
    }

    func testLongEmojiPathIsCountedInBytes() {
        let text = "https://i.scdn.co/image/" + String(repeating: "👨‍👩‍👧‍👦", count: 100)
        XCTAssertLessThanOrEqual(text.count, 256)
        XCTAssertGreaterThan(text.utf8.count, 256)
        XCTAssertNil(CoverImageLoader.allowedURL(text))
    }

    /// WHO-154 R1: a redirect from i.scdn.co is never followed.
    func testRedirectIsNotFollowed() async throws {
        let loader = makeLoader()
        let target = try XCTUnwrap(URL(string: "https://example.com/x"))
        let image = imageStub()
        let redirect = StubURLProtocol.Stub(
            status: 302, headers: ["Location": target.absoluteString, "Content-Type": "image/png"],
            data: pngData(), redirectTo: target)
        StubURLProtocol.handler = { request in
            request.url == target ? image : redirect
        }
        let result = await loader.image(for: goodURL)
        XCTAssertNil(result)
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.absoluteString, goodURL)
    }

    // MARK: - Rule 2: own session, no credentials

    func testCoverRequestCarriesNoTokenAndNoCookie() async throws {
        let loader = makeLoader()
        let stub = imageStub()
        StubURLProtocol.handler = { _ in stub }
        let image = await loader.image(for: goodURL)
        XCTAssertNotNil(image)
        let request = try XCTUnwrap(StubURLProtocol.lastRequest)
        XCTAssertEqual(request.url?.absoluteString, goodURL)
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
    }

    func testSessionKeepsNothingOnDisk() {
        let configuration = CoverImageLoader.makeURLSession().configuration
        XCTAssertNil(configuration.urlCache)
        XCTAssertNil(configuration.httpCookieStorage)
        XCTAssertNil(configuration.urlCredentialStorage)
        XCTAssertFalse(configuration.httpShouldSetCookies)
        XCTAssertEqual(configuration.requestCachePolicy, .reloadIgnoringLocalCacheData)
    }

    // MARK: - Rule 3: no AsyncImage, no shared session

    /// Both write to the shared disk cache. Reads the app's sources, which
    /// the simulator can see on the build machine.
    func testAppSourcesUseNeitherAsyncImageNorTheSharedSession() throws {
        let appFolder = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("New2U")
        let files = try FileManager.default.contentsOfDirectory(at: appFolder, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        XCTAssertGreaterThan(files.count, 20)
        let banned = ["AsyncImage(", "URLSession" + ".shared"]
        for file in files {
            let text = try String(contentsOf: file, encoding: .utf8)
            for word in banned {
                XCTAssertFalse(text.contains(word), "\(file.lastPathComponent) uses \(word)")
            }
        }
    }

    // MARK: - Rule 4: memory only, emptied

    func testSecondLoadComesFromMemory() async {
        let loader = makeLoader()
        let stub = imageStub()
        StubURLProtocol.handler = { _ in stub }
        _ = await loader.image(for: goodURL)
        XCTAssertNotNil(loader.cachedImage(for: goodURL))
        _ = await loader.image(for: goodURL)
        XCTAssertEqual(StubURLProtocol.requestCount, 1)
    }

    func testRemoveAllForgetsEveryCover() async {
        let loader = makeLoader()
        let stub = imageStub()
        StubURLProtocol.handler = { _ in stub }
        _ = await loader.image(for: goodURL)
        loader.removeAll()
        XCTAssertNil(loader.cachedImage(for: goodURL))
    }

    /// WHO-154 N2: a cover that arrives after the cache was emptied is
    /// shown but not kept.
    func testDownloadFinishingAfterRemoveAllIsNotKept() async {
        let loader = makeLoader()
        let stub = imageStub()
        StubURLProtocol.handler = { _ in
            Thread.sleep(forTimeInterval: 0.3)
            return stub
        }
        let task = Task { await loader.image(for: goodURL) }
        try? await Task.sleep(for: .milliseconds(100))
        loader.removeAll()
        let image = await task.value
        XCTAssertNotNil(image)
        XCTAssertNil(loader.cachedImage(for: goodURL))
    }

    func testEndingTheSessionForgetsCovers() throws {
        let url = try XCTUnwrap(URL(string: goodURL))
        CoverImageLoader.shared.insert(try XCTUnwrap(UIImage(data: pngData())), for: url)
        XCTAssertNotNil(CoverImageLoader.shared.cachedImage(for: goodURL))
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        store.endExpiredSession()
        XCTAssertNil(CoverImageLoader.shared.cachedImage(for: goodURL))
    }

    // MARK: - Rules 5 and 6: bounded, and every failure is the placeholder

    func testRefusesAnAnswerThatIsNotAnImage() async {
        let loader = makeLoader()
        // Built here: the handler runs off the main thread.
        let stub = StubURLProtocol.Stub(headers: ["Content-Type": "text/html"], data: pngData())
        StubURLProtocol.handler = { _ in stub }
        let image = await loader.image(for: goodURL)
        XCTAssertNil(image)
        XCTAssertNil(loader.cachedImage(for: goodURL))
    }

    func testRefusesAnAnswerOverOneMegabyte() async {
        let loader = makeLoader()
        var big = pngData()
        big.append(Data(count: CoverImageLoader.maxBytes))
        StubURLProtocol.handler = { _ in
            StubURLProtocol.Stub(headers: ["Content-Type": "image/png"], data: big)
        }
        let image = await loader.image(for: goodURL)
        XCTAssertNil(image)
    }

    func testFailuresGiveNoImage() async {
        let loader = makeLoader()
        StubURLProtocol.handler = { _ in StubURLProtocol.Stub(status: 404, headers: ["Content-Type": "image/png"]) }
        var image = await loader.image(for: goodURL)
        XCTAssertNil(image)
        StubURLProtocol.handler = { _ in StubURLProtocol.Stub(error: URLError(.notConnectedToInternet)) }
        image = await loader.image(for: goodURL)
        XCTAssertNil(image)
        StubURLProtocol.handler = { _ in StubURLProtocol.Stub(headers: ["Content-Type": "image/png"], body: "not a picture") }
        image = await loader.image(for: goodURL)
        XCTAssertNil(image)
    }

    /// WHO-154 N1: decoded no larger than `maxPixels`, whatever the file says.
    func testDecodeIsBoundedInPixels() throws {
        let image = try XCTUnwrap(CoverImageLoader.decode(pngData(side: 1200)))
        let cgImage = try XCTUnwrap(image.cgImage)
        XCTAssertLessThanOrEqual(max(cgImage.width, cgImage.height), CoverImageLoader.maxPixels)
        XCTAssertNil(CoverImageLoader.decode(Data("not a picture".utf8)))
    }

    // MARK: - The API field on every song and album item

    func testDecodesArtworkURLOnEveryItem() throws {
        let decoder = APIClient.makeDecoder()
        let albums = try decoder.decode(PlayedAlbums.self, from: Data(AlbumsTests.example.utf8))
        XCTAssertEqual(albums.albums.items.map(\.artworkUrl), [goodURL, nil])
        let new = try decoder.decode(NewToYou.self, from: Data(NewToYouTests.contractExample.utf8))
        XCTAssertEqual(new.albums.items.map(\.artworkUrl), [goodURL, nil])
        let album = try decoder.decode(AlbumTracks.self, from: Data(NewToYouTests.albumExample.utf8))
        XCTAssertEqual(album.album.artworkUrl, goodURL)
        let summary = try decoder.decode(Summary.self, from: Data(SummaryTests.contractExample.utf8))
        XCTAssertEqual(summary.topTracks.first?.artworkUrl, goodURL)
        let page = try decoder.decode(TimelinePage.self, from: Data(TimelineFixtures.contractExample.utf8))
        XCTAssertEqual(page.events.first?.artworkUrl, "https://i.scdn.co/image/x")
    }
}

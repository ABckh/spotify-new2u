import UIKit
import ImageIO

/// Loads album covers from Spotify's image server, `i.scdn.co`.
///
/// This is the only network call the app makes to a server other than
/// ours (WHO-148 decision, option A). Security's rules, each with a test
/// in `CoverImageLoaderTests`:
/// - Only `https://i.scdn.co/image/…` is ever asked for (rule 1). Any
///   other value shows the placeholder and makes no request. Redirects
///   are refused, so the request can never end up at another host.
/// - Its own session, never `APIClient`'s: no session token, no cookies,
///   no disk cache (rules 2 and 3).
/// - Covers count as listening history, so they are kept in memory only.
///   The cache is emptied when the app goes to the background
///   (`New2UApp`) and when the session token is deleted (`SessionStore`)
///   (rule 4).
/// - At most 1 MB, and only an `image/*` answer (rule 5). The picture
///   is decoded at most `maxPixels` wide, so a small file that claims a
///   huge size cannot use up the phone's memory.
/// - A failure shows the placeholder: no message, no retry, and the URL
///   is never logged (rules 6 and 15).
final class CoverImageLoader {
    /// The one loader every cover view uses.
    static let shared = CoverImageLoader(urlSession: CoverImageLoader.makeSharedURLSession())

    /// The largest cover accepted, in bytes (rule 5).
    nonisolated static let maxBytes = 1_000_000

    /// The longest side a cover is decoded at: the 120 pt album header
    /// at 3× is 360 pixels.
    nonisolated static let maxPixels = 360

    private let urlSession: URLSession
    private let cache = NSCache<NSString, UIImage>()
    /// Goes up on every `removeAll()`. A download that started before
    /// the cache was emptied must not put its cover back.
    private var generation = 0

    /// `urlSession` is only replaced by the unit tests.
    init(urlSession: URLSession = CoverImageLoader.makeURLSession()) {
        self.urlSession = urlSession
        // About two screens of rows. NSCache also empties itself when
        // the phone runs low on memory.
        cache.countLimit = 200
    }

    /// The same settings as `APIClient.makeURLSession()`: nothing on
    /// disk, no cookies. It is a separate session so a cover request can
    /// never carry our session token.
    static func makeURLSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.urlCredentialStorage = nil
        configuration.timeoutIntervalForRequest = 15
        return URLSession(configuration: configuration)
    }

    /// The real session, or in a Debug fixture run one that answers with
    /// drawn sample covers and never touches the network.
    private static func makeSharedURLSession() -> URLSession {
        #if DEBUG
        if DebugFixtures.activeName != nil {
            return DebugFixtures.makeCoverURLSession()
        }
        #endif
        return makeURLSession()
    }

    /// The URL to load, or nil when `text` is not a cover we may load
    /// (rule 1): `https`, host exactly `i.scdn.co`, no user, no port,
    /// `/image/` followed by an ID of only letters and digits, no query,
    /// at most 256 bytes and ASCII only.
    nonisolated static func allowedURL(_ text: String?) -> URL? {
        guard let text, text.utf8.count <= 256,
              text.unicodeScalars.allSatisfy(\.isASCII),
              let components = URLComponents(string: text),
              components.scheme == "https",
              components.host == "i.scdn.co",
              components.user == nil, components.password == nil,
              components.port == nil,
              components.query == nil, components.fragment == nil,
              components.path.hasPrefix("/image/"),
              isImageID(components.path.dropFirst("/image/".count)),
              let url = components.url
        else { return nil }
        return url
    }

    /// Spotify image IDs are 40 hex characters. Letters and digits only
    /// refuses `.`, `%`, `/` and spaces, so the path cannot leave `/image/`.
    nonisolated private static func isImageID(_ id: Substring) -> Bool {
        (1...128).contains(id.count) && id.unicodeScalars.allSatisfy {
            ("a"..."z").contains($0) || ("A"..."Z").contains($0) || ("0"..."9").contains($0)
        }
    }

    /// The cover if it is already in memory, so a row scrolled back into
    /// view shows it at once instead of the placeholder.
    func cachedImage(for text: String?) -> UIImage? {
        guard let url = Self.allowedURL(text) else { return nil }
        return cache.object(forKey: url.absoluteString as NSString)
    }

    /// The cover, or nil when there is none, it is not allowed, or it
    /// could not be loaded. Never throws: every failure is the placeholder.
    func image(for text: String?) async -> UIImage? {
        guard let url = Self.allowedURL(text) else { return nil }
        if let image = cache.object(forKey: url.absoluteString as NSString) { return image }
        let startedIn = generation
        guard let data = await Self.download(url, session: urlSession),
              let image = Self.decode(data)
        else { return nil }
        // Emptied while downloading (app went to the background, or the
        // session ended): show it if the row is still there, keep nothing.
        if generation == startedIn && !Task.isCancelled {
            insert(image, for: url)
        }
        return image
    }

    /// Puts a cover in the memory cache.
    func insert(_ image: UIImage, for url: URL) {
        cache.setObject(image, forKey: url.absoluteString as NSString)
    }

    /// Forgets every cover. Called on background and on session end.
    func removeAll() {
        generation += 1
        cache.removeAllObjects()
    }

    /// Decodes at most `maxPixels` on the longest side, never at the size
    /// the file claims.
    nonisolated static func decode(_ data: Data) -> UIImage? {
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixels,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        else { return nil }
        return UIImage(cgImage: image)
    }

    /// Downloads off the main thread and stops as soon as the answer is
    /// not an image or grows past `maxBytes`.
    @concurrent
    nonisolated private static func download(_ url: URL, session: URLSession) async -> Data? {
        var request = URLRequest(url: url)
        request.setValue("image/*", forHTTPHeaderField: "Accept")
        do {
            let (bytes, response) = try await session.bytes(for: request, delegate: RefuseRedirects.shared)
            guard let http = response as? HTTPURLResponse,
                  http.statusCode == 200,
                  http.mimeType?.hasPrefix("image/") == true,
                  http.expectedContentLength <= maxBytes
            else { return nil }
            var data = Data()
            for try await byte in bytes {
                data.append(byte)
                if data.count > maxBytes { return nil }
            }
            return data
        } catch {
            return nil
        }
    }
}

/// Turns down every redirect, so a 3xx answer from `i.scdn.co` fails the
/// `statusCode == 200` check instead of sending us to another host.
private final class RefuseRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    static let shared = RefuseRedirects()

    nonisolated func urlSession(
        _ session: URLSession, task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping @Sendable (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

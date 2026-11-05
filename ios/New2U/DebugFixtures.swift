#if DEBUG
import Foundation
import UIKit

/// Debug builds only: answers API requests with synthetic data, so the
/// simulator can show every screen state for screenshots without a
/// server and without anyone's real listening history.
///
/// Launch with `-fixture <name>`, for example:
/// `xcrun simctl launch booted app.new2u.New2U -fixture plays`
///
/// Names: plays, loading, offline, rate-limited, server-error, expired,
/// empty-no_plays, empty-before_coverage, empty-no_data_yet,
/// empty-collection_gap, summary-failed (timeline loads, summary does
/// not), custom-reversed (opens the custom-range sheet and shows the
/// server refusing it), disclosure (opens the total's info popover),
/// revoked, reconnect-soon, not-connected (the Spotify connection
/// banner; revoked-offline shows it over the offline state), settings,
/// settings-revoked, settings-offline (open Settings on launch),
/// new-v2 (collection started this period), new-v3 (nothing new),
/// new-meaning (opens the "New to you" info popover),
/// new-offline and new-failed ("New to you" fails, the rest loads),
/// imported (recent plays were imported at the first connect: the
/// timeline shows the "Collection started" row yesterday at 18:00),
/// connect (signed out: the connect screen with the C3 import notice),
/// connect-expired (the "Your session ended" connect screen, C3b),
/// cooldown ("Refresh from Spotify" disabled for 3:12, and a tap is
/// answered with 429), album (opens the first new album's tracks),
/// album-empty, album-offline and album-failed (the album screen's
/// empty and failed states), not-new (marks the first new album "Not
/// new" on launch; the server never answers, so the row stays hidden
/// under the Undo banner) and not-new-offline (the same mark fails, the
/// row comes back with the error banner), albums-offline and
/// albums-failed (the "Albums" section fails, the rest loads),
/// plays-many (70 plays over 5 days in two pages, so "Show all 70"
/// pages as you scroll; WHO-131) and plays-many-all (the same, with
/// "Show all" pushed on launch), plays-many-bottom (the same, with
/// the main screen scrolled to the plays' "Show all"), covers
/// (every song and album has a drawn sample cover, except "Low Tide"
/// and two albums, which have none), covers-broken (every cover URL is
/// answered 404, so every square stays the placeholder), covers-album
/// (covers, with the first new album's screen open), share (covers,
/// with the "New albums" share picture open; WHO-196) and
/// share-not-new (the same after the first new album is marked "Not
/// new", so it must be missing from the picture), review (the
/// "Already knew some of these?" row above "New to you", listing the
/// new albums, singles and EPs) and review-open (the same, with "Did you
/// already know these?" open on launch). In every
/// other fixture a refresh is answered 202, "All albums" shows 12
/// albums, every other one new, and "New to you" has 3 singles and EPs.
///
/// Tabs (WHO-145): `tab-<tab>` opens the main screen on that tab
/// (tracks, albums or singles), and `tab-<tab>-<fixture>` does the same with any
/// fixture above, for example `tab-albums-offline` or
/// `tab-tracks-plays-many`. The share fixtures need the Albums tab:
/// `tab-albums-share`, `tab-albums-share-not-new`.
///
/// Ratings: `rated` gives the first five new albums a rating (9, 10, 7,
/// none, 8) and two singles one (10, 6), and fills "Your ratings" with
/// them and one more album (4), so every league has something. The
/// first album also has a comment, and the fourth has a comment and no
/// rating, so it is under "Not rated"; `album-rated` opens the first
/// album's screen with its rating and comment;
/// `share-rated` is the
/// share picture with those ratings and covers (`tab-albums-share-rated`).
/// `share-many` is the share picture with 30 new albums, so it shows the
/// first 25 and "+ 5 more" (`tab-albums-share-many`); `share-17` has 17,
/// in rows of 4, 4, 3, 3 and 3 (`tab-albums-share-17`); `share-long` has
/// a top track and artist too long for one line (`tab-albums-share-long`).
///
/// Search: `search` opens Search with "harbour" typed, finding albums by
/// name and by comment and songs; `search-none` finds nothing;
/// `search-offline` fails with the offline message.
nonisolated enum DebugFixtures {
    /// The fixture name exactly as given in the launch arguments.
    private static var launchName: String? {
        UserDefaults.standard.string(forKey: "fixture")
    }

    /// The tab a `tab-…` fixture opens on launch, or nil.
    static var launchTab: MainTab? {
        guard let name = launchName, name.hasPrefix("tab-") else { return nil }
        let rest = name.dropFirst("tab-".count)
        return MainTab.allCases.first { rest == $0.rawValue || rest.hasPrefix($0.rawValue + "-") }
    }

    /// The fixture name from the launch arguments without its `tab-<tab>`
    /// part, or nil for a normal run. A bare `tab-<tab>` is `plays`.
    static var activeName: String? {
        guard let name = launchName, let tab = launchTab else { return launchName }
        let rest = name.dropFirst("tab-\(tab.rawValue)".count)
        return rest.isEmpty ? "plays" : String(rest.dropFirst())
    }

    /// Opens the custom-range sheet on launch with the end date before
    /// the start, and presses Show once.
    static var checksCustomRangeOnOpen: Bool { activeName == "custom-reversed" }

    /// Opens the info popover next to the total on launch.
    static var showsDisclosureOnOpen: Bool { activeName == "disclosure" }

    /// Opens the info popover next to "New to you" on launch.
    static var showsNewToYouMeaningOnOpen: Bool { activeName == "new-meaning" }

    /// Pushes the first new album's tracks on launch, for `album…`.
    /// Not for `albums…`, which are the "Albums" section's fixtures.
    static var opensAlbumOnLaunch: Bool {
        activeName == "album" || (activeName?.hasPrefix("album-") ?? false) || activeName == "covers-album"
    }

    /// Marks the first new album "Not new" on launch (WHO-123).
    static var marksNotNewOnLaunch: Bool {
        (activeName?.hasPrefix("not-new") ?? false) || activeName == "share-not-new"
    }

    /// Opens the "New albums" share picture on launch (WHO-196).
    static var opensShareOnLaunch: Bool { activeName?.hasPrefix("share") ?? false }

    /// `rated`, `share-rated` and `share-many`: some albums carry the user's rating.
    static var showsRatings: Bool {
        (activeName?.hasSuffix("rated") ?? false) || activeName == "share-many" || activeName == "share-17"
    }

    /// The fixture rating of new album `index`, or NSNull.
    static func rating(_ index: Int) -> Any {
        guard showsRatings else { return NSNull() }
        return [0: 9, 1: 10, 2: 7, 4: 8][index] ?? NSNull()
    }

    /// The fixture comment of new album `index`, or NSNull: one with a
    /// rating, one without.
    static func comment(_ index: Int) -> Any {
        guard showsRatings else { return NSNull() }
        return [
            0: "The second half is where it opens up. Better on headphones, late at night, with the volume up.",
            3: "Not sure about this one yet. The title track stays with me, the rest not so much.",
        ][index] ?? NSNull()
    }

    /// The fixture comment on track `index` ("Paper Lanterns" for 0), or
    /// NSNull: in the `rated` fixtures only.
    static func trackComment(_ index: Int) -> Any {
        guard showsRatings, index == 0 else { return NSNull() }
        return "The quiet bit at 2:10 before the drums come back."
    }

    /// The fixture rating of new single `index`, or NSNull.
    static func singleRating(_ index: Int) -> Any {
        guard showsRatings else { return NSNull() }
        return [0: 10, 2: 6][index] ?? NSNull()
    }

    /// `GET /v1/ratings/albums`: the rated fixture albums, singles and
    /// EPs, best first, as the server orders them.
    static func ratedAlbums() -> [String: Any] {
        guard showsRatings else { return ["count": 0, "items": [Any]()] }
        func album(_ index: Int, _ rating: Int?) -> [String: Any] {
            let comment = comment(index)
            return [
                "album_id": "synthetic\(index)", "name": newAlbumNames[index],
                "artists": [tracks[index % tracks.count].artist], "artwork_url": coverURL(index),
                "release_type": "album", "rating": rating as Any? ?? NSNull(),
                "rated_at": rating == nil ? NSNull() : instantText(Date()),
                "comment": comment, "commented_at": comment is NSNull ? NSNull() : instantText(Date()),
            ]
        }
        func single(_ index: Int, _ rating: Int) -> [String: Any] {
            let (name, type, _) = singleReleases[index]
            return [
                "album_id": "single\(index)", "name": name,
                "artists": [tracks[(index + 2) % tracks.count].artist], "artwork_url": coverURL(index + 1),
                "release_type": type, "rating": rating, "rated_at": instantText(Date()),
                "comment": NSNull(), "commented_at": NSNull(),
            ]
        }
        // The one with only a comment last, as the server orders it.
        let items = [
            album(1, 10), single(0, 10), album(0, 9), album(4, 8), album(2, 7), single(2, 6), album(5, 4),
            album(3, nil),
        ]
        return ["count": items.count, "items": items]
    }

    /// Pushes the plays' "Show all" on launch (WHO-131).
    static var opensAllPlaysOnLaunch: Bool { activeName == "plays-many-all" }

    /// `review-open` opens "Did you already know these?" on launch.
    static var opensReviewOnLaunch: Bool { activeName == "review-open" }

    /// `GET /v1/me/review`: only the review fixtures have something to
    /// review, so every other screenshot is unchanged.
    static func review(open: Bool) -> [String: Any] {
        let new = newToYou(all: true, nothingNew: false, containsStart: false)
        let none: [String: Any] = ["count": 0, "items": [Any]()]
        return [
            "open": open,
            "window_start": NSNull(),
            "window_end": NSNull(),
            "through": instantText(Date()),
            "albums": open ? new["albums"] ?? none : none,
            "singles": open ? new["singles"] ?? none : none,
        ]
    }

    /// Scrolls the main list to the plays' "Show all" on launch (WHO-131).
    static var scrollsToPlays: Bool { activeName == "plays-many-bottom" }

    /// Pushes Settings on launch, for the `settings…` fixtures.
    static var opensSettingsOnLaunch: Bool { activeName?.hasPrefix("settings") ?? false }

    /// Pushes Search on launch with words typed, for the `search…`
    /// fixtures: `search` ("harbour": albums by name and by comment, and
    /// songs), `search-none` (nothing found), `search-offline` (fails).
    static var opensSearchOnLaunch: Bool { activeName?.hasPrefix("search") ?? false }
    static var searchWordsOnLaunch: String { activeName == "search-none" ? "zither" : "harbour" }

    /// The fixture albums Search looks through: (name, artist, type,
    /// tracks, plays, last played, rating, comment).
    private static let searchAlbums: [(String, String, String, Int, Int, String, Int?, String?)] = [
        ("Harbour Lights", "The Quiet Harbour", "album", 10, 42, "2026-10-28", 9,
         "Best on a grey morning by the harbour. The second side is the reason to keep it."),
        ("Night Ferry", "Mara Lindqvist", "single", 2, 6, "2026-09-14", nil,
         "Reminds me of the crossing to the harbour in winter."),
        ("Harbour Sessions", "Saltwater Choir", "single", 5, 11, "2025-06-02", 7, nil),
        ("Small Hours", "The Quiet Harbour", "album", 9, 23, "2024-11-30", nil, nil),
    ]

    /// The fixture songs Search looks through: (title, artist, album, plays, last played).
    private static let searchTracks: [(String, String, String, Int, String)] = [
        ("Paper Lanterns", "The Quiet Harbour", "Harbour Lights", 18, "2026-10-28"),
        ("Harbour Wall", "Saltwater Choir", "Harbour Sessions", 9, "2025-06-02"),
        ("Small Hours", "The Quiet Harbour", "Small Hours", 7, "2024-11-30"),
        ("Northbound Train", "Mara Lindqvist", "Northern Line", 4, "2023-03-19"),
    ]

    /// GET /v1/search: the fixture albums and songs holding every word
    /// of `query`, ignoring case, most played first, as the server finds them.
    static func search(query: String) -> [String: Any] {
        let words = query.lowercased().split(whereSeparator: \.isWhitespace)
        func holdsAll(_ fields: [String?]) -> Bool {
            let text = fields.compactMap { $0?.lowercased() }
            return words.allSatisfy { word in text.contains { $0.contains(word) } }
        }
        let albums: [[String: Any]] = searchAlbums.enumerated()
            .filter { holdsAll([$0.element.0, $0.element.1, $0.element.7]) }
            .map { index, album in
                [
                    "album_id": "synthetic\(index)", "name": album.0, "artists": [album.1],
                    "artwork_url": coverURL(index),
                    "release_type": album.2 == "single" ? (album.3 > 3 ? "ep" : "single") : "album",
                    "rating": album.6 ?? NSNull(), "comment": album.7 ?? NSNull(),
                    "play_count": album.4, "last_played_at": instantText(Date()),
                    "last_played_local_date": album.5,
                ]
            }
        let tracks: [[String: Any]] = searchTracks.enumerated()
            .filter { holdsAll([$0.element.0, $0.element.1, $0.element.2]) }
            .map { index, track in
                [
                    "item_key": "id:search\(index)", "title": track.0, "creators": [track.1],
                    "comment": index == 1 ? "Best with the window open, right at the harbour wall." : NSNull(),
                    "album": track.2, "album_id": index == 3 ? NSNull() : "synthetic\(index)",
                    "artwork_url": coverURL(index), "play_count": track.3,
                    "first_played_at": instantText(Date()), "last_played_at": instantText(Date()),
                    "last_played_local_date": track.4,
                ]
            }
        return [
            "albums": ["count": albums.count, "items": albums],
            "tracks": ["count": tracks.count, "items": tracks],
        ]
    }

    /// Puts the end date three days before the start, for `custom-reversed`.
    @MainActor static func reverse(_ range: CustomRangeViewModel) {
        range.to = range.from.addingTimeInterval(-3 * 86_400)
    }

    /// Fixtures that fail every request, the connection check included.
    static let failingNames: Set<String> = ["loading", "offline", "rate-limited", "server-error", "expired", "settings-offline"]

    /// GET /v1/me/connection for the connection fixtures.
    /// `cooldownSeconds` sets `next_manual_poll_at` that far ahead; nil
    /// means a refresh is allowed now.
    static func connection(status: String, reconnectSoon: Bool, imported: Bool = false, cooldownSeconds: Int? = nil) -> [String: Any] {
        let formatter = ISO8601DateFormatter()
        let now = Date()
        // For `imported`: live collection began yesterday at 18:00 in the
        // display zone, between two of the synthetic plays.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: displayZone)!
        let yesterday = calendar.date(byAdding: .day, value: -1, to: calendar.startOfDay(for: now))!
        let importStart = calendar.date(bySettingHour: 18, minute: 0, second: 0, of: yesterday)!
        return [
            "status": status,
            "reauth_reason": status == "active" ? NSNull() : "invalid_grant" as Any,
            "spotify_account_id": "synthetic-account",
            "spotify_display_name": "Synthetic Listener",
            "collection_started_at": formatter.string(from: imported ? importStart : now.addingTimeInterval(-170 * 86_400)),
            "history_imported_at": imported ? formatter.string(from: importStart.addingTimeInterval(20)) : NSNull() as Any,
            "has_imported_plays": imported,
            "authorized_at": formatter.string(from: now.addingTimeInterval(-170 * 86_400)),
            "grant_expires_at": formatter.string(from: now.addingTimeInterval(12 * 86_400)),
            "reconnect_soon": reconnectSoon,
            "last_polled_at": formatter.string(from: now.addingTimeInterval(-300)),
            "last_poll_attempt_at": formatter.string(from: now.addingTimeInterval(-300)),
            "next_manual_poll_at": cooldownSeconds.map { formatter.string(from: now.addingTimeInterval(TimeInterval($0))) as Any } ?? NSNull(),
            "poll_interval_seconds": 900,
        ]
    }

    /// A URLSession whose every request is answered by `FixtureURLProtocol`.
    /// Same cache and cookie settings as `APIClient.makeURLSession()`, so
    /// the WHO-30 container check sees what a real run would leave behind.
    static func makeURLSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.protocolClasses = [FixtureURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    // MARK: - Covers (WHO-150)

    /// The `artwork_url` of song or album number `index`: a drawn sample
    /// cover for `covers…`, a URL that fails for `covers-broken`, else
    /// null as before. Every URL passes the `i.scdn.co/image/` check, so
    /// the app really asks for it; `FixtureCoverURLProtocol` answers.
    static func coverURL(_ index: Int) -> Any {
        switch activeName {
        case "covers-broken":
            return "https://i.scdn.co/image/broken\(index)"
        case "covers", "covers-album", "share", "share-not-new", "share-rated", "share-many", "share-17", "share-long":
            // A few have no cover, to show the placeholder among covers.
            return index % 7 == 4 ? NSNull() : "https://i.scdn.co/image/fixture\(index)"
        default:
            return NSNull()
        }
    }

    /// A session for covers whose every request is answered by
    /// `FixtureCoverURLProtocol`, with the same settings as the real one.
    static func makeCoverURLSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.urlCache = nil
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.httpShouldSetCookies = false
        configuration.protocolClasses = [FixtureCoverURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    /// A 300 × 300 PNG drawn in code: a two-colour gradient with a disc,
    /// its colour taken from `index`. No real album art in the repo.
    static func sampleCover(_ index: Int) -> Data? {
        let side = 300
        guard let context = CGContext(
            data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { return nil }
        let hue = CGFloat(index % 12) / 12
        let top = UIColor(hue: hue, saturation: 0.55, brightness: 0.85, alpha: 1).cgColor
        let bottom = UIColor(hue: hue + 0.08, saturation: 0.75, brightness: 0.45, alpha: 1).cgColor
        if let gradient = CGGradient(colorsSpace: nil, colors: [top, bottom] as CFArray, locations: [0, 1]) {
            context.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: side, y: side), options: [])
        }
        context.setFillColor(UIColor(white: 1, alpha: 0.35).cgColor)
        context.fillEllipse(in: CGRect(x: 90, y: 90, width: 120, height: 120))
        return context.makeImage().flatMap { UIImage(cgImage: $0).pngData() }
    }

    /// The synthetic display zone. Deliberately not the simulator's zone,
    /// so the screenshots show that times follow the stored zone.
    static let displayZone = "Europe/Warsaw"

    // MARK: - Synthetic plays

    private static let tracks: [(title: String, artist: String, seconds: Int)] = [
        ("Paper Lanterns", "The Quiet Harbour", 222),
        ("Northbound Train", "Mara Lindqvist", 251),
        ("Small Hours", "The Quiet Harbour", 198),
        ("Glass Orchard", "Tomas Reyes, Ana Fiore", 305),
        ("Low Tide", "Saltwater Choir", 187),
        ("A Very Long Song Title That Will Not Fit On One Line", "An Artist With A Long Name Too", 412),
        ("Signal Fires", "Mara Lindqvist", 233),
    ]

    /// The plays of the active fixture: 70 for `plays-many`, else 14.
    private static var plays: [(Int, Int, Int, Int)] {
        activeName?.hasPrefix("plays-many") ?? false ? manyPlays : fewPlays
    }

    /// 70 plays for `plays-many`: 6 today, so the main screen's 10 span
    /// two days, then 16 a day from 23:05 back, over five local days.
    private static let manyPlays: [(Int, Int, Int, Int)] = (0..<70).map { index in
        index < 6
            ? (0, 23 - index, 5, index % 7)
            : (1 + (index - 6) / 16, 23 - (index - 6) % 16, 5, index % 7)
    }

    /// (days ago, hour, minute, track index), newest first.
    private static let fewPlays: [(Int, Int, Int, Int)] = [
        (0, 8, 41, 0), (0, 8, 37, 1), (0, 8, 33, 5), (0, 7, 58, 2),
        (1, 22, 14, 3), (1, 22, 9, 0), (1, 21, 50, 4), (1, 18, 2, 6), (1, 12, 30, 1),
        (2, 23, 55, 2), (2, 20, 11, 4), (2, 20, 7, 0), (2, 9, 15, 3), (2, 9, 10, 6),
    ]

    /// The summary of the same fourteen plays, counted the way the
    /// server counts (WHO-13 R-25 to R-33), with the top one of each.
    static func summary(isEmpty: Bool, emptyReason: String?) -> [String: Any] {
        var trackPlays: [Int: Int] = [:]
        var artistPlays: [String: Int] = [:]
        var artistMs: [String: Int] = [:]
        var totalMs = 0
        if !isEmpty {
            for play in plays {
                let track = tracks[play.3]
                trackPlays[play.3, default: 0] += 1
                totalMs += track.seconds * 1000
                for artist in track.artist.components(separatedBy: ", ") {
                    artistPlays[artist, default: 0] += 1
                    artistMs[artist, default: 0] += track.seconds * 1000
                }
            }
        }
        var topTrack = trackPlays.max { a, b in
            a.value != b.value ? a.value < b.value : tracks[a.key].title > tracks[b.key].title
        }
        var topArtist = artistPlays.max { a, b in
            a.value != b.value ? a.value < b.value : a.key > b.key
        }
        // `share-long`: the long title and artist on top, for the picture.
        if activeName == "share-long" {
            topTrack = (key: 5, value: 9)
            topArtist = (key: "An Artist With A Long Name Too", value: 9)
        }
        let page = isEmpty ? emptyPage(reason: emptyReason ?? "no_plays") : timelinePage()
        return [
            "period": page["period"]!,
            "totals": [
                "play_count": isEmpty ? 0 : plays.count,
                "distinct_tracks": trackPlays.count,
                "distinct_artists": artistPlays.count,
                "total_ms": totalMs,
                "events_without_duration": 0,
            ],
            "top_tracks": topTrack.map { entry -> [[String: Any]] in
                let track = tracks[entry.key]
                return [[
                    "item_key": "id:synthetic\(entry.key)", "title": track.title,
                    "creators": track.artist.components(separatedBy: ", "),
                    "play_count": entry.value, "total_ms": entry.value * track.seconds * 1000,
                    "artwork_url": coverURL(entry.key),
                ]]
            } ?? [],
            "top_artists": topArtist.map { entry -> [[String: Any]] in
                [["name": entry.key, "play_count": entry.value, "total_ms": artistMs[entry.key] ?? 0]]
            } ?? [],
            "is_empty": isEmpty,
            "empty_reason": emptyReason as Any? ?? NSNull(),
            "coverage_start": page["coverage_start"]!,
        ]
    }

    /// Fourteen plays over three local days, newest first, built
    /// relative to now so the headers read Today, Yesterday, and a date.
    /// For `plays-many`, 50 plays a page: `cursor` nil is the first page.
    static func timelinePage(cursor: String? = nil) -> [String: Any] {
        let zone = TimeZone(identifier: displayZone)!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.timeZone = zone
        dayFormatter.dateFormat = "yyyy-MM-dd"

        let startOfToday = calendar.startOfDay(for: Date())
        let events: [[String: Any]] = plays.enumerated().map { index, play in
            let day = calendar.date(byAdding: .day, value: -play.0, to: startOfToday)!
            let instant = calendar.date(bySettingHour: play.1, minute: play.2, second: 0, of: day)!
            let track = tracks[play.3]
            return [
                "id": String(format: "00000000-0000-4000-8000-%012d", index),
                "source": "spotify",
                "kind": "listen",
                "occurred_at": instantText(instant),
                "occurred_at_precision": "second",
                "local_date": dayFormatter.string(from: day),
                "duration_ms": track.seconds * 1000,
                "played_ms": NSNull(),
                "title": track.title,
                "creators": track.artist.components(separatedBy: ", "),
                "collection": "Synthetic Album",
                "artwork_url": coverURL(play.3),
                "item_key": "id:synthetic\(play.3)",
                "comment": trackComment(play.3),
            ]
        }
        // The oldest play's day: two days ago, or four for `plays-many`.
        let weekStart = calendar.date(byAdding: .day, value: -(plays.map(\.0).max() ?? 0), to: startOfToday)!
        let isFirstPage = cursor == nil
        let pageEvents = events.count <= 50 ? events : Array(isFirstPage ? events.prefix(50) : events.dropFirst(50))
        return [
            "period": period(start: weekStart, days: 7, calendar: calendar, dayFormatter: dayFormatter),
            "events": pageEvents,
            "next_cursor": events.count > 50 && isFirstPage ? "fixture-page-2" : NSNull() as Any,
            "is_empty": false,
            "empty_reason": NSNull(),
            "coverage_start": instantText(weekStart),
        ]
    }

    static func emptyPage(reason: String) -> [String: Any] {
        let zone = TimeZone(identifier: displayZone)!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.timeZone = zone
        dayFormatter.dateFormat = "yyyy-MM-dd"
        let start = calendar.startOfDay(for: Date())
        return [
            "period": period(start: start, days: 7, calendar: calendar, dayFormatter: dayFormatter),
            "events": [],
            "next_cursor": NSNull(),
            "is_empty": true,
            "empty_reason": reason,
            "coverage_start": reason == "no_data_yet" ? NSNull() : instantText(start) as Any,
        ]
    }

    // MARK: - Synthetic "New to you"

    private static let newAlbumNames = [
        "Harbour Lights", "Northern Line", "Paper Moons", "Quiet Rooms", "Salt and Iron",
        "Glass Orchard", "Low Tide Sessions", "Signal Fires (Deluxe)", "Small Hours", "Tin Roof Rain",
        "Under the Pier", "Winter Radio",
    ]

    /// The fixture singles and EPs: (name, release type, track count).
    static let singleReleases: [(name: String, type: String, tracks: Int)] = [
        ("Paper Lanterns", "single", 1), ("Night Ferry", "single", 2), ("Harbour Sessions", "ep", 5),
    ]

    /// GET /v1/new-to-you: 12 albums and 3 singles and EPs, so albums show
    /// "Show all" and the lists are newest first.
    static func newToYou(all: Bool, nothingNew: Bool, containsStart: Bool) -> [String: Any] {
        let page = timelinePage()
        let period = page["period"] as! [String: Any]
        let zone = TimeZone(identifier: displayZone)!
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let dayFormatter = DateFormatter()
        dayFormatter.locale = Locale(identifier: "en_US_POSIX")
        dayFormatter.timeZone = zone
        dayFormatter.dateFormat = "yyyy-MM-dd"
        let today = calendar.startOfDay(for: Date())
        func day(_ ago: Int) -> (String, String) {
            let date = calendar.date(byAdding: .day, value: -ago, to: today)!.addingTimeInterval(9 * 3600)
            return (instantText(date), dayFormatter.string(from: date))
        }

        let count = activeName == "share-many" ? 30 : activeName == "share-17" ? 17 : newAlbumNames.count
        let names = newAlbumNames + (newAlbumNames.count..<max(count, newAlbumNames.count)).map { "Album \($0 + 1)" }
        let albums: [[String: Any]] = names.enumerated().map { index, name in
            let (instant, local) = day(index / 5)
            return [
                "album_id": "synthetic\(index)", "name": name,
                "artists": [tracks[index % tracks.count].artist],
                "first_played_at": instant, "first_played_local_date": local,
                "tracks_played_in_period": 1 + index % 4,
                "tracks_listened": index == 3 ? 1 : 6 + index % 4, "total_tracks": index == 3 ? 1 : 10,
                "artwork_url": coverURL(index), "rating": rating(index), "comment": comment(index),
            ]
        }
        let singles: [[String: Any]] = singleReleases.enumerated().map { index, release in
            let (instant, local) = day(index)
            return [
                "album_id": "single\(index)", "name": release.name,
                "artists": [tracks[(index + 2) % tracks.count].artist],
                "first_played_at": instant, "first_played_local_date": local,
                "tracks_played_in_period": release.tracks, "tracks_listened": release.tracks,
                "total_tracks": release.tracks, "artwork_url": coverURL(index + 1),
                "rating": singleRating(index), "release_type": release.type,
            ]
        }
        func list(_ items: [[String: Any]]) -> [String: Any] {
            nothingNew ? ["count": 0, "items": []] : ["count": items.count, "items": all ? items : Array(items.prefix(10))]
        }
        let start = containsStart ? day(1) : day(170)
        return [
            "period": period,
            "albums": list(albums), "singles": list(singles),
            "coverage_start": start.0, "coverage_start_local_date": start.1,
            "period_contains_coverage_start": containsStart,
            "nothing_new": nothingNew,
            "is_empty": false, "empty_reason": NSNull(), "collection_gap_overlap": false,
        ]
    }

    /// GET /v1/new-to-you/albums/{id}/tracks: four tracks of the album,
    /// most played first, two of them new. `empty`: none in the period.
    static func albumTracks(albumId: String, empty: Bool) -> [String: Any] {
        let index = Int(albumId.dropFirst("synthetic".count)) ?? 0
        let isAlbum = albumId.hasPrefix("synthetic")
        let artist = tracks[index % tracks.count].artist
        let titles = [("Lantern Song", 6, true), ("Open Water", 4, false), ("Morning Ferry", 2, true), ("Coda", 1, false)]
        let rows: [[String: Any]] = titles.enumerated().map { offset, track in
            [
                "item_key": "id:album\(index)track\(offset)", "title": track.0,
                "creators": artist.components(separatedBy: ", "),
                "play_count": track.1, "is_new": track.2,
                "comment": offset == 1 ? trackComment(0) : NSNull(),
                "first_played_at": instantText(Date()), "first_played_local_date": "2026-10-05",
            ]
        }
        return [
            "period": timelinePage()["period"]!,
            "album": [
                "album_id": albumId, "name": newAlbumNames[index % newAlbumNames.count], "artists": [artist],
                "artwork_url": coverURL(index),
                // A single's ID is "single<n>": only the albums are rated here.
                "rating": isAlbum ? rating(index) : NSNull(), "comment": isAlbum ? comment(index) : NSNull(),
            ],
            "tracks": empty ? [] : rows,
        ]
    }

    /// GET /v1/albums: 12 albums, most played first, every other one
    /// new, so the section shows "Show all". The IDs match the "New to
    /// you" albums, so the album screen fixture answers for them too.
    static func albums(all: Bool) -> [String: Any] {
        let items: [[String: Any]] = newAlbumNames.enumerated().map { index, name in
            let plays = 24 - 2 * index
            let track = tracks[index % tracks.count]
            return [
                "album_id": "synthetic\(index)", "name": index == 11 ? NSNull() : name as Any,
                "artists": track.artist.components(separatedBy: ", "),
                "play_count": plays, "tracks_played_in_period": 1 + index % 4,
                "total_ms": plays * track.seconds * 1000, "is_new": index % 2 == 0,
                "artwork_url": coverURL(index), "rating": rating(index), "comment": comment(index),
            ]
        }
        let page = timelinePage()
        return [
            "period": page["period"]!,
            "albums": ["count": items.count, "items": all ? items : Array(items.prefix(10))],
            "is_empty": false, "empty_reason": NSNull(),
            "coverage_start": page["coverage_start"]!, "collection_gap_overlap": false,
        ]
    }

    /// GET /v1/singles: the fixture singles and EPs and two more that
    /// are not new, most played first.
    static func singles(all: Bool) -> [String: Any] {
        let releases = singleReleases + [("Coastline", "single", 2), ("Field Notes", "ep", 6)]
        let items: [[String: Any]] = releases.enumerated().map { index, release in
            let plays = 14 - 2 * index
            let track = tracks[(index + 3) % tracks.count]
            return [
                "album_id": "single\(index)", "name": release.0,
                "artists": track.artist.components(separatedBy: ", "),
                "play_count": plays, "tracks_played_in_period": release.2 / 2 + 1,
                "total_tracks": release.2, "total_ms": plays * track.seconds * 1000,
                "is_new": index < singleReleases.count, "release_type": release.1 == "ep" ? "ep" : "single",
                "artwork_url": coverURL(index + 20), "rating": NSNull(), "comment": NSNull(),
            ]
        }
        let page = timelinePage()
        return [
            "period": page["period"]!,
            "albums": ["count": items.count, "items": all ? items : Array(items.prefix(10))],
            "is_empty": false, "empty_reason": NSNull(),
            "coverage_start": page["coverage_start"]!, "collection_gap_overlap": false,
        ]
    }

    static func emptyAlbums(reason: String) -> [String: Any] {
        let page = emptyPage(reason: reason)
        return [
            "period": page["period"]!,
            "albums": ["count": 0, "items": []],
            "is_empty": true, "empty_reason": reason,
            "coverage_start": page["coverage_start"]!, "collection_gap_overlap": false,
        ]
    }

    static func emptyNewToYou(reason: String) -> [String: Any] {
        let page = emptyPage(reason: reason)
        let empty: [String: Any] = ["count": 0, "items": []]
        let start = page["coverage_start"] as? String
        return [
            "period": page["period"]!,
            "albums": empty, "singles": empty,
            "coverage_start": start as Any? ?? NSNull(),
            "coverage_start_local_date": start.map { String($0.prefix(10)) } as Any? ?? NSNull(),
            "period_contains_coverage_start": false, "nothing_new": false,
            "is_empty": true, "empty_reason": reason, "collection_gap_overlap": false,
        ]
    }

    private static func period(start: Date, days: Int, calendar: Calendar, dayFormatter: DateFormatter) -> [String: Any] {
        let end = calendar.date(byAdding: .day, value: days, to: start)!
        let lastDay = calendar.date(byAdding: .day, value: days - 1, to: start)!
        return [
            "type": "week", "offset": 0, "anchor": NSNull(),
            "time_zone": displayZone, "week_start": "monday",
            "start": instantText(start), "end": instantText(end),
            "local_start_date": dayFormatter.string(from: start),
            "local_end_date": dayFormatter.string(from: lastDay),
            "is_partial": true,
        ]
    }

    private static func instantText(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}

/// Answers each request according to `DebugFixtures.activeName`.
nonisolated final class FixtureURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let name = DebugFixtures.activeName ?? "plays"
        let isSummary = request.url?.path == "/v1/summary"
        let isCustom = request.url?.query?.contains("period=custom") ?? false
        let isConnection = request.url?.path == "/v1/me/connection"
        let isNewToYou = request.url?.path == "/v1/new-to-you"
        let path = request.url?.path ?? ""
        // From "New to you" or from "Albums" (WHO-134).
        let albumPrefix = path.hasPrefix("/v1/albums/") ? "/v1/albums/" : "/v1/new-to-you/albums/"
        let isAlbumTracks = path.hasPrefix(albumPrefix)
        let isAlbums = path == "/v1/albums"
        let isSingles = path == "/v1/singles"
        let wantsAll = request.url?.query?.contains("all=true") ?? false
        let isPoll = request.url?.path == "/v1/me/spotify/poll"
        let isNotNew = request.url?.path.hasPrefix("/v1/new-to-you/not-new/") ?? false
        let isReview = path == "/v1/me/review"
        let isBatch = path == "/v1/new-to-you/not-new"
        let isReviewDone = path == "/v1/me/review/done"
        let isRatings = path == "/v1/ratings/albums"
        let isRating = path.hasPrefix("/v1/albums/") && path.hasSuffix("/rating")
        let isComment = (path.hasPrefix("/v1/albums/") || path.hasPrefix("/v1/tracks/")) && path.hasSuffix("/comment")
        let isSearch = path == "/v1/search"
        let (status, headers, body): (Int, [String: String], Any?)
        switch name {
        case "not-new" where isNotNew, "share-not-new" where isNotNew:
            return  // never answers, so the Undo banner stays up
        case "not-new-offline" where isNotNew:
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case _ where isRatings:
            (status, headers, body) = (200, [:], DebugFixtures.ratedAlbums())
        case "search-offline" where isSearch:
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case _ where isSearch:
            let query = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.first { $0.name == "q" }?.value ?? ""
            (status, headers, body) = (200, [:], DebugFixtures.search(query: query))
        case _ where isRating || isComment:
            (status, headers, body) = (204, [:], nil)
        case _ where isNotNew || isReviewDone:
            (status, headers, body) = (204, [:], nil)
        case _ where isBatch:
            (status, headers, body) = (200, [:], ["marked": 0, "not_played": 0])
        case _ where isReview:
            (status, headers, body) = (200, [:], DebugFixtures.review(open: name.hasPrefix("review")))
        case "revoked" where isConnection, "revoked-offline" where isConnection, "settings-revoked" where isConnection:
            (status, headers, body) = (200, [:], DebugFixtures.connection(status: "reauth_required", reconnectSoon: true))
        case "revoked-offline":
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case "reconnect-soon" where isConnection:
            (status, headers, body) = (200, [:], DebugFixtures.connection(status: "active", reconnectSoon: true))
        case "imported" where isConnection:
            (status, headers, body) = (200, [:], DebugFixtures.connection(status: "active", reconnectSoon: false, imported: true))
        case "not-connected" where isConnection:
            (status, headers, body) = (404, [:], ["detail": "Not Found"])
        case "cooldown" where isConnection:
            (status, headers, body) = (200, [:], DebugFixtures.connection(status: "active", reconnectSoon: false, cooldownSeconds: 192))
        case "cooldown" where isPoll:
            (status, headers, body) = (429, ["Retry-After": "192"], ["detail": ["code": "cooling_down", "message": "Spotify was checked a moment ago."]])
        case _ where isPoll && !DebugFixtures.failingNames.contains(name):
            let formatter = ISO8601DateFormatter()
            let answer = [
                "requested_at": formatter.string(from: Date()),
                "next_allowed_at": formatter.string(from: Date().addingTimeInterval(300)),
            ]
            (status, headers, body) = (202, [:], answer)
        case _ where isConnection && !DebugFixtures.failingNames.contains(name):
            // Every other fixture has a healthy connection, so no banner.
            (status, headers, body) = (200, [:], DebugFixtures.connection(status: "active", reconnectSoon: false))
        case "custom-reversed" where isCustom:
            (status, headers, body) = (422, [:], ["detail": ["code": "period_range_reversed", "message": "to is before from"]])
        case "album-offline" where isAlbumTracks:
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case "album-failed" where isAlbumTracks:
            (status, headers, body) = (500, [:], nil)
        case _ where isAlbumTracks && !DebugFixtures.failingNames.contains(name):
            let albumId = request.url!.path.dropFirst(albumPrefix.count).split(separator: "/").first.map(String.init) ?? ""
            (status, headers, body) = (200, [:], DebugFixtures.albumTracks(albumId: albumId, empty: name == "album-empty"))
        case "albums-offline" where isAlbums:
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case "albums-failed" where isAlbums:
            (status, headers, body) = (500, [:], nil)
        case let empty where empty.hasPrefix("empty-") && isAlbums:
            (status, headers, body) = (200, [:], DebugFixtures.emptyAlbums(reason: String(empty.dropFirst("empty-".count))))
        case _ where isAlbums && !DebugFixtures.failingNames.contains(name):
            (status, headers, body) = (200, [:], DebugFixtures.albums(all: wantsAll))
        case let empty where empty.hasPrefix("empty-") && isSingles:
            (status, headers, body) = (200, [:], DebugFixtures.emptyAlbums(reason: String(empty.dropFirst("empty-".count))))
        case _ where isSingles && !DebugFixtures.failingNames.contains(name):
            (status, headers, body) = (200, [:], DebugFixtures.singles(all: wantsAll))
        case "new-offline" where isNewToYou:
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case "new-failed" where isNewToYou:
            (status, headers, body) = (500, [:], nil)
        case let empty where empty.hasPrefix("empty-") && isNewToYou:
            (status, headers, body) = (200, [:], DebugFixtures.emptyNewToYou(reason: String(empty.dropFirst("empty-".count))))
        case _ where isNewToYou && !DebugFixtures.failingNames.contains(name):
            let answer = DebugFixtures.newToYou(all: wantsAll, nothingNew: name == "new-v3", containsStart: name == "new-v2")
            (status, headers, body) = (200, [:], answer)
        case "summary-failed" where isSummary:
            (status, headers, body) = (500, [:], nil)
        case "loading":
            return  // never answers, so the skeleton stays on screen
        case "offline", "settings-offline":
            client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
            return
        case "rate-limited":
            (status, headers, body) = (429, ["Retry-After": "30"], ["detail": "Too Many Requests"])
        case "server-error":
            (status, headers, body) = (500, [:], nil)
        case "expired":
            (status, headers, body) = (401, ["WWW-Authenticate": "Bearer"], ["detail": "Unauthorized"])
        case let empty where empty.hasPrefix("empty-"):
            let reason = String(empty.dropFirst("empty-".count))
            let page = isSummary
                ? DebugFixtures.summary(isEmpty: true, emptyReason: reason)
                : DebugFixtures.emptyPage(reason: reason)
            (status, headers, body) = (200, [:], page)
        default:
            let cursor = request.url.flatMap { URLComponents(url: $0, resolvingAgainstBaseURL: false) }?
                .queryItems?.first { $0.name == "cursor" }?.value
            let page = isSummary
                ? DebugFixtures.summary(isEmpty: false, emptyReason: nil)
                : DebugFixtures.timelinePage(cursor: cursor)
            (status, headers, body) = (200, [:], page)
        }
        let data = body.flatMap { try? JSONSerialization.data(withJSONObject: $0) } ?? Data("oops".utf8)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
/// Answers cover requests in a fixture run: a drawn sample cover for
/// `/image/fixture<n>`, and 404 for anything else (`covers-broken`).
nonisolated final class FixtureCoverURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        let index = path.hasPrefix("/image/fixture") ? Int(path.dropFirst("/image/fixture".count)) : nil
        let data = index.flatMap(DebugFixtures.sampleCover)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: data == nil ? 404 : 200, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": data == nil ? "text/plain" : "image/png"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data ?? Data("Not Found".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
#endif

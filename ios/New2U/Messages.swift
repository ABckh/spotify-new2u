import Foundation

/// The words a screen shows for an empty period or a failure.
///
/// Empty-period text is copied from api-contract section 3.3 and the
/// WHO-18 `gap-label` document (E4). Error text names the problem and
/// the next step; no codes, no "something went wrong" (design bar).
enum Messages {
    struct Text: Equatable {
        let title: String
        let detail: String
        let systemImage: String
    }

    /// The message for an empty period. An unknown reason reads as
    /// "no_plays", as the contract asks.
    static func empty(reason: String?) -> Text {
        switch reason {
        case "no_data_yet":
            return Text(
                title: "Nothing here yet",
                detail: "Nothing here yet. Connect Spotify and your listening will start showing up.",
                systemImage: "music.note.list")
        case "before_coverage":
            return Text(
                title: "Before your history begins",
                detail: "Your history here begins after these dates.",
                systemImage: "calendar.badge.clock")
        case "collection_gap":
            return Text(
                title: "No plays collected",
                detail: "Spotify collection was not running for part or all of this period, so plays may be missing.",
                systemImage: "pause.circle")
        default:
            return Text(
                title: "No plays",
                detail: "No plays in this period.",
                systemImage: "music.note")
        }
    }

    /// The message for a failed request, shown in place of the list.
    static func failure(_ error: APIError) -> Text {
        switch error {
        case .offline:
            return Text(
                title: "You're offline",
                detail: "Connect to the internet, then try again.",
                systemImage: "wifi.slash")
        case .rateLimited(let seconds):
            let wait = seconds.map { "Try again in \($0) seconds." } ?? "Try again in a minute."
            return Text(title: "Too many requests", detail: wait, systemImage: "hourglass")
        case .invalidRequest(code: "period_invalid_time_zone"):
            return Text(
                title: "Time zone not recognised",
                detail: "Your time zone setting isn't recognised. Set it again in Settings.",
                systemImage: "globe")
        case .notConfigured:
            return Text(
                title: "Server address not configured",
                detail: "This build doesn't know where the server is. Install a build with the server address set.",
                systemImage: "network.slash")
        case .notSignedIn, .sessionExpired:
            return Text(
                title: "Signed out",
                detail: "Your session ended. Connect Spotify again to continue.",
                systemImage: "person.crop.circle.badge.exclamationmark")
        case .invalidRequest, .notFound, .conflict, .signInRefused, .server, .badResponse:
            return Text(
                title: "The server couldn't answer",
                detail: "Try again in a moment.",
                systemImage: "exclamationmark.icloud")
        }
    }

    /// The line under the custom-range pickers when the server refuses
    /// the range. The 422 texts are word for word from WHO-13 R-22.
    static func customRange(_ error: APIError) -> String {
        switch error {
        case .invalidRequest(code: "period_range_reversed"):
            return "The end date is before the start date."
        case .invalidRequest(code: "period_range_too_long"):
            return "Pick a range of 366 days or less."
        case .invalidRequest(code: "period_invalid_date"):
            return "That date isn't valid."
        case .invalidRequest(code: "period_range_incomplete"):
            return "Pick both a start and an end date."
        default:
            let text = failure(error)
            return "\(text.title). \(text.detail)"
        }
    }

    /// WHO-13 R-26, word for word: why the total is full track lengths.
    static let totalDisclosure = "Spotify tells us which tracks you played, not how much of each one you heard. Totals use the full track length, so a track you skipped still counts in full."

    /// "1 play", "12 plays".
    static func plays(_ count: Int) -> String {
        count == 1 ? "1 play" : "\(count) plays"
    }

    // MARK: - New to you (S14)

    /// V1, word for word from the WHO-90 `copy` document §4, with the
    /// date of the earliest stored play (`coverage_start`) already
    /// formatted. Imported plays can be older than the collection start.
    static func newToYouMeaning(historyStart: String) -> String {
        "New means first played since \(historyStart), when your history here begins. This app does not know what you played before that date."
    }

    /// V2 (WHO-90 §4): the period contains the earliest stored play.
    static let newToYouCollectionStarted = "Your history here begins in this period, so almost everything here shows as new."

    /// V3: plays in the period, nothing new.
    static let newToYouNothingNew = "Nothing new in this period. Everything you played, you had played before."

    /// List headings with the full count: "4 new albums", "1 new album",
    /// "No new albums" (S14.1).
    static func newAlbums(_ count: Int) -> String {
        count == 0 ? "No new albums" : count == 1 ? "1 new album" : "\(count) new albums"
    }

    /// "2 new singles & EPs", "1 new single or EP".
    static func newSingles(_ count: Int) -> String {
        count == 0 ? "No new singles or EPs" : count == 1 ? "1 new single or EP" : "\(count) new singles & EPs"
    }

    // MARK: Ratings

    static let ratingTitle = "Your rating"
    static let ratingClear = "Clear"
    /// The two separate rating lists, on the Albums and Singles tabs.
    static let ratingsAlbumsTitle = "Your album ratings"
    static let ratingsSinglesTitle = "Your singles & EPs ratings"
    static let ratingsAlbumsEmpty = "No album ratings yet. Open an album and give it a score from 1 to 10, or write a comment."
    static let ratingsSinglesEmpty = "No single or EP ratings yet. Open a single or EP and give it a score from 1 to 10, or write a comment."
    /// A league with nothing in it yet.
    static let leagueEmpty = "Nothing here yet."

    /// "League 1", the heading of one league.
    static func leagueTitle(_ number: Int) -> String {
        "League \(number)"
    }

    /// "8/10", on rows and on the share picture.
    static func ratingShort(_ rating: Int) -> String {
        "\(rating)/10"
    }

    /// What VoiceOver adds after a sentence: " Rated 8 out of 10.", with
    /// its leading space, or nothing when not rated.
    static func ratingSpoken(_ rating: Int?) -> String {
        rating.map { " Rated \($0) out of 10." } ?? ""
    }

    /// The entry row on the Albums tab: "12 rated · average 7.4", then
    /// " · 3 comments" when some have one. With comments only: "2 comments".
    static func ratingsSummary(
        count: Int, average: Double?, comments: Int = 0, locale: Locale = .current
    ) -> String {
        var parts: [String] = []
        if count > 0 || comments == 0 { parts.append("\(count) rated") }
        if let average {
            parts.append("average \(average.formatted(.number.precision(.fractionLength(1)).locale(locale)))")
        }
        if comments > 0 { parts.append(comments == 1 ? "1 comment" : "\(comments) comments") }
        return parts.joined(separator: " · ")
    }

    static func ratingFailed(_ error: APIError) -> String {
        "Couldn't save your rating: \(failure(error).title)."
    }

    static let ratingsFailed = "Couldn't load your ratings."
    /// The section of "Your ratings" for releases with a comment and no rating.
    static let ratingsCommentOnly = "Not rated"

    // MARK: Comments

    static let commentTitle = "Your comment"
    static let commentAdd = "Add a comment"
    static let commentEdit = "Edit"
    static let commentPlaceholder = "What do you think of it?"
    static let commentSave = "Save"
    static let commentCancel = "Cancel"
    /// The server's limit, in Unicode scalars as it counts them.
    static let commentMaxLength = 2000

    /// "120/2000" under the editor.
    static func commentCount(_ count: Int) -> String {
        "\(count)/\(commentMaxLength)"
    }

    /// What VoiceOver adds after a row's sentence: " With your comment.",
    /// with its leading space, or nothing without one. Never the text: a
    /// list reads one sentence per row.
    static func commentSpoken(_ comment: String?) -> String {
        comment == nil ? "" : " With your comment."
    }

    static func commentFailed(_ error: APIError) -> String {
        "Couldn't save your comment: \(failure(error).title)."
    }

    /// The action on a track: "Add a comment", or "Edit comment" when it has one.
    static func trackCommentAction(_ comment: String?) -> String {
        comment == nil ? commentAdd : "Edit comment"
    }

    /// VoiceOver's hint on a track a tap opens the comment editor for.
    static func trackCommentHint(_ comment: String?) -> String {
        comment == nil ? "Adds your comment on this track." : "Edits your comment on this track."
    }

    // MARK: "Did you already know these?" (first-two-weeks review)

    static let reviewOfferTitle = "Already knew some of these?"

    /// "Review 37 albums and singles from your first two weeks."
    static func reviewOfferDetail(_ count: Int) -> String {
        count == 1
            ? "Review 1 album or single from your first two weeks."
            : "Review \(count) albums and singles from your first two weeks."
    }

    static let reviewTitle = "Did you already know these?"
    static let reviewIntro = "Your history here starts when you connected, so everything from your first two weeks looks new. Tick what you already knew; the rest stays new."
    static let reviewSelectAll = "Select all"
    static let reviewClearAll = "Clear all"
    static let reviewKeepAll = "Keep all as new"
    static let reviewNothingLeft = "Nothing left to review."

    /// The button: "Mark 5 as known".
    static func reviewMarkKnown(_ count: Int) -> String {
        "Mark \(count) as known"
    }

    static func reviewSaveFailed(_ error: APIError) -> String {
        "Couldn't save your choices: \(failure(error).title). Your ticks are kept, so try again."
    }

    /// What VoiceOver reads for the share picture's preview (WHO-196).
    /// The share picture's headline, over the period: what the covers are.
    static let shareHeadline = "New albums I listened to"

    static func sharePictureLabel(_ card: ShareCardModel) -> String {
        let names = card.albums.map(\.name).joined(separator: ", ")
        let more = card.moreCount > 0 ? ", and \(card.moreCount) more" : ""
        var stats = ""
        if let numbers = card.stats {
            stats = " \(numbers.total) listened."
            if let artist = numbers.topArtist { stats += " Top artist: \(artist)." }
            if let track = numbers.topTrack { stats += " Top track: \(track)." }
        }
        return "\(shareHeadline), \(card.periodLabel).\(stats) Albums: \(names)\(more)."
    }

    /// The album screen with no plays of that album in the period.
    static let albumNoPlays = "No plays from this album"
    static let albumNoPlaysDetail = "You did not play this album in this period."

    /// "11 of 16 tracks", "1 of 1 track": distinct tracks of a new album
    /// played so far, out of the album's track count (WHO-153).
    static func tracksOf(_ listened: Int, _ total: Int) -> String {
        total == 1 ? "\(listened) of 1 track" : "\(listened) of \(total) tracks"
    }

    // MARK: - Albums (WHO-134)

    /// "12 albums", "1 album", "No albums": every album played in the period.
    static func albums(_ count: Int) -> String {
        count == 0 ? "No albums" : count == 1 ? "1 album" : "\(count) albums"
    }

    /// The section's own error, before the reason ("You're offline.").
    static let albumsFailed = "Couldn't load albums."
    static let singlesFailed = "Couldn't load singles & EPs."
    static let allAlbums = "All albums"
    static let allSingles = "All singles & EPs"
    static let singlesNone = "No singles or EPs in this period."

    /// "12 singles & EPs", "1 single or EP", "No singles or EPs".
    static func singlesAndEPs(_ count: Int) -> String {
        count == 0 ? "No singles or EPs" : count == 1 ? "1 single or EP" : "\(count) singles & EPs"
    }

    /// The headline of a failed Albums screen. Offline reads
    /// "You're offline", the same as Tracks; any other error keeps the
    /// section's own words, for example "Couldn't load albums."
    static func failureTitle(_ error: APIError, section: String) -> String {
        if case .offline = error { return failure(error).title }
        return section
    }

    /// A period with plays but none on a known album.
    static let albumsNone = "No albums in this period."

    /// "Plays · time" on an album row: "12 plays · 1h 4m".
    static func playsAndTime(plays: Int, totalMs: Int) -> String {
        "\(Self.plays(plays)) · \(DisplayFormat.total(milliseconds: totalMs))"
    }

    // MARK: - Search

    static let searchTitle = "Search"
    static let searchPrompt = "Albums, songs, artists, comments"
    static let searchHint = "Find any album, single, EP or song you played, by its name or artist, or an album by words from your comment on it."
    static let searchAlbums = "Albums, singles & EPs"
    static let searchSongs = "Songs"
    static let searchFailed = "Couldn't search."
    static let searchTooLong = "Search with up to 10 words and 200 characters."

    /// Under a section that shows 50 of more: "50 of 132. Add a word to narrow it down."
    static func searchMore(shown: Int, count: Int) -> String {
        "\(shown) of \(count). Add a word to narrow it down."
    }

    /// "12 plays · last 5 Oct" on a found row; "No plays" for an album
    /// found by its comment alone.
    static func searchPlays(_ count: Int, lastPlayedLocalDate: String?, timeZone: TimeZone) -> String {
        guard count > 0, let last = lastPlayedLocalDate else { return "No plays" }
        return "\(plays(count)) · last \(DisplayFormat.shortDate(localDate: last, timeZone: timeZone))"
    }

    // MARK: - Not new (WHO-123)

    /// The row action, the menu item and the VoiceOver action.
    static let notNewAction = "Not new"

    /// The banner after a mark, next to its Undo button.
    static func notNewMarked(name: String) -> String {
        "\u{201C}\(name)\u{201D} marked as not new"
    }

    /// The banner when the server did not take the mark. The row is back.
    static func notNewFailed(name: String, error: APIError) -> String {
        let text = failure(error)
        return "Couldn't mark \u{201C}\(name)\u{201D} as not new. \(text.title). \(text.detail)"
    }

    /// The banner when the server did not take the Undo. The mark stays.
    static func undoFailed(name: String, error: APIError) -> String {
        let text = failure(error)
        return "Couldn't undo \u{201C}\(name)\u{201D}. It is still marked as not new. \(text.title). \(text.detail)"
    }

    // MARK: - Connect Spotify

    /// T1 (WHO-103 §6.6, was WHO-90 §3): the timeline row at
    /// `collection_started_at` when the user imported plays, at connect or
    /// from the history file. `time` is the clock time only, because the
    /// row is always under its own day's header.
    static func importMarker(time: String) -> String {
        "Collection started \(time). Earlier plays were imported from Spotify."
    }

    /// C3 (WHO-119 `copy` §1, changed by WHO-120): above "Connect
    /// Spotify" when there was no session. Security-approved word for
    /// word; any change goes back to Security. The second sentence came
    /// with the known-music baseline and needs that check.
    static let connectImportNotice = "When you connect, we also import the last 50 plays Spotify still lists, including plays from before you connect. We read your saved albums and songs, followed artists and top lists, so music you already knew doesn't show as new."

    /// C3b: the same place on "Your session ended". A delete-all ends the
    /// session, and connecting from here is a first connect that imports.
    static let reconnectImportNotice = "If your data was deleted, or you connect a different Spotify account, we also import the last 50 plays Spotify still lists, including plays from before you connect. We read your saved albums and songs, followed artists and top lists, so music you already knew doesn't show as new."

    /// The line above the connect button. Never on the reconnect banner:
    /// a renewal sends the session token and the server never imports.
    static func signInImportNotice(sessionExpired: Bool) -> String {
        sessionExpired ? reconnectImportNotice : connectImportNotice
    }

    /// Spotify said no, the user declined, or the callback did not match
    /// the sign-in this app started.
    static let signInNotConnected = Text(
        title: "Spotify wasn't connected",
        detail: "Spotify didn't connect your account. Try again.",
        systemImage: "xmark.circle")

    /// The system sign-in sheet could not open.
    static let signInBrowserFailed = Text(
        title: "Couldn't open Spotify",
        detail: "The Spotify sign-in page didn't open. Try again.",
        systemImage: "safari")

    /// A reconnect where the user picked a different Spotify account than
    /// the one connected here. The app stays signed in (api-contract 2.3).
    static let signInWrongAccount = Text(
        title: "Wrong Spotify account",
        detail: "You picked a different Spotify account than the one connected here. Reconnect and pick the account you connected.",
        systemImage: "person.crop.circle.badge.xmark")

    /// The message for a failed request during "Connect Spotify".
    static func signInFailure(_ error: APIError) -> Text {
        switch error {
        case .signInRefused(reason: "different_account"):
            return signInWrongAccount
        case .signInRefused:
            return signInNotConnected
        case .notFound:
            return Text(
                title: "Sign-in expired",
                detail: "That sign-in took too long or was already used. Try again.",
                systemImage: "clock.badge.exclamationmark")
        default:
            return failure(error)
        }
    }

    // MARK: - Spotify connection

    /// The banner over the timeline when the Spotify connection needs
    /// the user. `byDate` is the reconnect deadline, already formatted.
    static func connection(_ notice: ConnectionViewModel.Notice, byDate: String?) -> Text {
        switch notice {
        case .revoked:
            return Text(
                title: "Spotify disconnected",
                detail: "New plays aren't being collected. Reconnect Spotify to start again.",
                systemImage: "exclamationmark.triangle")
        case .notConnected:
            return Text(
                title: "Spotify isn't connected",
                detail: "Connect Spotify to start collecting your plays.",
                systemImage: "link")
        case .reconnectSoon:
            let detail = byDate.map { "Reconnect Spotify by \($0) to keep collecting your plays." }
                ?? "Reconnect Spotify soon to keep collecting your plays."
            return Text(title: "Reconnect soon", detail: detail, systemImage: "clock")
        }
    }

    // MARK: - Sync with Spotify (WHO-96, in Settings since WHO-118)

    static let syncButton = "Sync now"
    /// "Last updated" before the server has read Spotify even once.
    static let syncNotYet = "Not yet"
    static let refreshChecking = "Checking Spotify…"

    /// The disabled button during the cooldown: "Available in 3:12".
    static func refreshAvailable(inSeconds seconds: Int) -> String {
        "Available in \(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }

    /// One line under the button when the request failed.
    static func refreshFailed(_ error: APIError) -> String {
        let text = failure(error)
        return "\(text.title). \(text.detail)"
    }

    // MARK: - Settings

    /// Under the time zone in Settings. Says why the zone is fixed, and
    /// that changing it isn't available yet (api-contract section 4).
    static let zoneFooter = "Times and day headers use this zone, so a play stays under the day it happened when you travel. Changing the zone isn't available yet."

    /// In place of the zone before any answer from the server has named it.
    static let zoneNotKnownYet = "Shown after your plays load"

    /// A one-line note above content that is already on screen, when a
    /// refresh or the next page fails.
    static func banner(_ error: APIError) -> String {
        switch error {
        case .offline:
            return "You're offline. Showing what was already loaded."
        case .rateLimited:
            return "Too many requests. Showing what was already loaded."
        default:
            return "Couldn't load more. Showing what was already loaded."
        }
    }
}

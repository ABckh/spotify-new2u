import Foundation

/// The words of the Spotify history file import, word for word from the
/// WHO-103 `copy` document. Section numbers below point into it.
extension Messages {
    // MARK: - §1 Connect screen switch C2

    static let fullHistoryChoiceLabel = "Import my full Spotify history"
    static let fullHistoryChoiceExplainer = "Spotify can send you a file with all your past plays. We'll show you how to ask for it."

    // MARK: - §2 Screen H

    static let stepsTitle = "Get your Spotify history file"
    static let stepsIntro = "Spotify keeps your full listening history, but only sends it to you as a file. Here is how to ask for it."
    /// Markdown, so the bold labels and the file name render as in the copy.
    static let steps: [String] = [
        "Open your Spotify account privacy page and sign in.",
        "Under \"Download your data\", tick **Extended streaming history**. You don't need the other options.",
        "Tap **Request data**. Spotify sends you an email; open it and confirm.",
        "Wait for Spotify's second email. Spotify says this can take up to 30 days. Often it is a few days.",
        "Open that email on this iPhone and download the file. It is called `my_spotify_data.zip`. Don't unzip it.",
        "Come back to this app: **Settings → Import full Spotify history**.",
    ]
    static let stepsOneTime = "You can import your history once. If it fails, you can try again."
    static let stepsOpenPrivacyPage = "Open Spotify privacy page"
    /// Opened in Safari as it is: no added query parameters (§2 rule 1).
    static let spotifyPrivacyPage = URL(string: "https://www.spotify.com/account/privacy/")!

    // MARK: - §3 Settings row

    static let importRowLabel = "Import full Spotify history"
    static let importRowStart = "Upload the file Spotify sends you. One time only."
    static let importRowRetry = "The last import didn't finish. Tap to try again."
    static let importRowImporting = "Importing…"
    static let howToGetTheFile = "How to get the file"

    // MARK: - §4 Consent screen

    static let consentTitle = "Import your Spotify history file"
    /// Pairs of a bold lead and the text after it.
    static let consentParagraphs: [(lead: String, text: String)] = [
        ("What we use.", "From your file we read only the files named `Streaming_History_Audio_…json`. From each music play we keep the track, artist and album, when you played it, and how long you played it."),
        ("What we skip.", "We ignore every other file in the download. We skip podcasts, audiobooks and videos, plays from private sessions, and plays shorter than 30 seconds."),
        ("What we don't keep.", "Your file also contains details such as your internet address, the country you were in, and the device and app you used. We don't store any of these."),
        ("Your file.", "The whole file is uploaded to our server, in the EU. We delete it as soon as the import ends, whether it worked or not, and never more than 24 hours after you upload it."),
        ("One time only.", "You can import your history once. If the import fails, you can try again, and plays are never added twice. Imported plays are kept, exported and deleted like all your other plays."),
    ]
    /// Shown while the server does not check whose file it is (R7 dropped).
    static let consentOwnHistoryOnly = "Only import your own history."
    static let consentChooseFile = "Choose file"

    // MARK: - §5.1 Upload and progress

    static let uploading = Text(
        title: "Uploading your file…",
        detail: "Keep this app open until the upload finishes.",
        systemImage: "arrow.up.circle")
    static let queued = Text(
        title: "Waiting to start.",
        detail: "Your file is uploaded. You can close the app; the import continues on our server.",
        systemImage: "clock")
    static let running = Text(
        title: "Importing your history…",
        detail: "Large histories can take a few minutes. You can close the app; the import continues on our server.",
        systemImage: "arrow.down.circle")

    // MARK: - §5.2 Success

    static let importFinished = "Import finished."

    /// S1 or S2. `inserted` is already formatted in the device locale.
    static func importResult(_ counts: ImportCounts, locale: Locale = .current) -> String {
        if counts.rowsInserted == 0 && counts.rowsDuplicate >= 1 {
            return "Every play in the file was already in your history, so nothing new was added."
        }
        let number = counts.rowsInserted.formatted(.number.locale(locale))
        let plays = counts.rowsInserted == 1 ? "play was" : "plays were"
        return "\(number) \(plays) added to your history."
    }

    /// The Details lines under S1 or S2. A line with a count of 0 is left out.
    static func importDetails(_ counts: ImportCounts, locale: Locale = .current) -> [String] {
        let lines: [(Int, String)] = [
            (counts.rowsInserted, "added"),
            (counts.rowsDuplicate, "already in your history"),
            (counts.rowsSkippedNotMusic, "skipped: podcasts, audiobooks, videos and other non-music"),
            (counts.rowsSkippedIncognito, "skipped: private sessions"),
            (counts.rowsSkippedShort, "skipped: shorter than 30 seconds"),
            (counts.rowsUnreadable, "skipped: could not be read"),
        ]
        return lines
            .filter { $0.0 > 0 }
            .map { "\($0.0.formatted(.number.locale(locale))) \($0.1)" }
    }

    static let openTimeline = "Open timeline"
    static let details = "Details"

    // MARK: - §5.3 Failures

    /// Under every failed import except E1, E2 and E11 (decision 10).
    static let somePlaysMayBeAdded = "Some plays may already have been added. Trying again will not add them twice."

    static func importProblem(_ problem: ImportProblem) -> Text {
        let asSent = "Upload the `my_spotify_data.zip` file exactly as Spotify sent it."
        switch problem {
        case .tooLarge:
            return Text(
                title: "This file is too big.",
                detail: "We accept files up to 200 MB. \(asSent)",
                systemImage: "externaldrive.badge.exclamationmark")
        case .wrongFormat:
            return Text(
                title: "We can't read this file.",
                detail: "Upload the `my_spotify_data.zip` file exactly as Spotify sent it, without unzipping or renaming it.",
                systemImage: "doc.questionmark")
        case .unsafeFile:
            return Text(
                title: "We can't open this file safely.",
                detail: asSent,
                systemImage: "exclamationmark.shield")
        case .noHistoryFiles:
            return Text(
                title: "This isn't your full history file.",
                detail: "Spotify sends \"Account data\" and \"Extended streaming history\" as separate downloads. We need the Extended streaming history one.",
                systemImage: "doc.badge.ellipsis")
        case .noMusicPlays:
            return Text(
                title: "No music plays found.",
                detail: "The file has no music plays we can import. Check that you uploaded your own Extended streaming history file.",
                systemImage: "music.note")
        case .tooManyPlays:
            return Text(
                title: "This history is too large to import.",
                detail: "We can import up to 1,000,000 plays. Nothing more can be done from the app; please tell the person who runs this app.",
                systemImage: "exclamationmark.triangle")
        case .wrongAccount:
            return Text(
                title: "This file is from a different Spotify account.",
                detail: "Upload the history file of the account connected to this app.",
                systemImage: "person.crop.circle.badge.xmark")
        case .failed:
            return Text(
                title: "The import didn't finish.",
                detail: "Something went wrong on our side.",
                systemImage: "exclamationmark.icloud")
        case .uploadFailed:
            return Text(
                title: "Upload failed.",
                detail: "Check your connection and try again.",
                systemImage: "wifi.slash")
        }
    }

    /// E10: 409 `history_import_already_done`.
    static let alreadyImported = Text(
        title: "You've already imported your history.",
        detail: "You can import it only once. Your imported plays are in your timeline.",
        systemImage: "checkmark.circle")
}

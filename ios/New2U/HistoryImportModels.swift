import Foundation

/// The answer of GET /v1/spotify/history-import (WHO-104):
/// `{"latest": null}` before the first upload, otherwise the latest import.
struct HistoryImportAnswer: Decodable {
    let latest: HistoryImport?
}

/// One upload of the Spotify history file and what the server did with it.
struct HistoryImport: Decodable, Equatable {
    let id: UUID
    /// "queued", "running", "succeeded" or "failed".
    let status: String
    /// Why a `failed` import failed, such as "no_streaming_history".
    let errorKind: String?
    let counts: ImportCounts

    var isActive: Bool { status == "queued" || status == "running" }
}

/// The counts the server sends. They grow while the import runs. The
/// app shows them as they come and adds nothing up itself.
struct ImportCounts: Decodable, Equatable {
    let rowsInserted: Int
    let rowsDuplicate: Int
    let rowsSkippedNotMusic: Int
    /// Plays from a private session (Spotify calls it "incognito").
    let rowsSkippedIncognito: Int
    let rowsSkippedShort: Int
    let rowsUnreadable: Int
}

/// 202 from POST /v1/spotify/history-import.
struct ImportAccepted: Decodable {
    let id: UUID
    let status: String
}

/// Why an import did not happen, one case per error row (E1–E8, E11) of
/// the WHO-103 `copy` document §5.3. E9 and E10 are not problems: they
/// show the progress screen and "already imported".
enum ImportProblem: Equatable {
    case tooLarge          // E1
    case wrongFormat       // E2
    case unsafeFile        // E3
    case noHistoryFiles    // E4
    case noMusicPlays      // E5
    case tooManyPlays      // E6
    case wrongAccount      // E7
    case failed            // E8
    case uploadFailed      // E11

    /// The server's code (WHO-104: a 413/415/422 `detail.code`, or a
    /// failed import's `error_kind`). An unknown code reads as E8, so the
    /// app never shows a raw code.
    init(code: String?) {
        switch code {
        case "upload_too_large":
            self = .tooLarge
        case "not_multipart", "missing_file", "too_many_files", "invalid_multipart",
             "unsupported_file", "invalid_json":
            self = .wrongFormat
        case "zip_too_many_entries", "zip_too_large", "zip_ratio_exceeded", "zip_unsafe_path",
             "zip_nested_archive", "zip_encrypted":
            self = .unsafeFile
        case "no_streaming_history":
            self = .noHistoryFiles
        // WHO-103 R6 and R7. The server does not send these yet; the names
        // follow its other codes.
        case "no_music_plays":
            self = .noMusicPlays
        case "wrong_account":
            self = .wrongAccount
        case "entry_too_large", "too_many_rows":
            self = .tooManyPlays
        default:
            self = .failed
        }
    }
}

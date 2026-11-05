import Foundation
import Observation

/// The Spotify history file import (WHO-94): its status, the upload, and
/// checking again while the server works on it.
///
/// - `load()`: GET /v1/spotify/history-import. Settings uses the answer
///   to show or hide its row; the import screen shows it.
/// - `upload(fileAt:)`: POST the picked file, then `load()`.
/// - `pollWhileActive()`: `load()` every few seconds while the import is
///   `queued` or `running`. The import screen runs it in `.task`, so it
///   stops when the user leaves the screen. The import itself keeps going
///   on the server.
///
/// The same shape as the other view models: the views read the
/// properties below and call these functions. Plain `async/await`.
@Observable
final class HistoryImportViewModel {
    enum Status: Equatable {
        case loading
        /// `nil`: no import yet.
        case loaded(HistoryImport?)
        case failed(APIError)
    }

    /// What the Settings row says (WHO-103 §3), or nil to hide the row.
    enum SettingsEntry: Equatable {
        /// No import yet. Opens the consent screen.
        case start
        /// The last import failed. Opens the consent screen.
        case retry
        /// `queued` or `running`. Opens the status screen.
        case importing
    }

    /// What the import screen shows (WHO-103 §5).
    enum Display: Equatable {
        case loading
        case uploading                        // P1
        case queued                           // P2
        case running                          // P3
        case finished(ImportCounts)           // S1 or S2
        case alreadyDone                      // E10
        /// E1–E8 and E11. `afterJobStarted` adds "Some plays may already
        /// have been added", which only a failed import on the server needs.
        case problem(ImportProblem, afterJobStarted: Bool)
        /// The status check failed, for example offline.
        case statusFailed(APIError)
    }

    /// The server refuses bigger files (WHO-104 `MAX_UPLOAD_BYTES`). The
    /// app checks first, so a too-big file is not sent over mobile data.
    static let maxUploadBytes = 200 * 1024 * 1024

    private(set) var status: Status = .loading
    private(set) var isUploading = false
    /// Why the last upload was refused or failed. Cleared by a new upload.
    private(set) var uploadProblem: ImportProblem?
    /// The last upload was answered 409 `history_import_already_done`.
    private(set) var alreadyDone = false

    private let api: APIClient
    private let pollInterval: Duration

    /// `pollInterval` is only shortened by the tests.
    init(api: APIClient, pollInterval: Duration = .seconds(3)) {
        self.api = api
        self.pollInterval = pollInterval
    }

    /// The latest import, or nil while loading, before the first upload,
    /// or after a failed check.
    var latest: HistoryImport? {
        if case .loaded(let latest) = status { return latest }
        return nil
    }

    /// True while the server has the import queued or running.
    var isActive: Bool { latest?.isActive ?? false }

    var settingsEntry: SettingsEntry? {
        // An unknown status hides the row: offering an upload the server
        // would refuse is worse than showing nothing for a moment (§3).
        guard case .loaded(let latest) = status, !alreadyDone else { return nil }
        guard let latest else { return .start }
        switch latest.status {
        case "failed": return .retry
        case "queued", "running": return .importing
        default: return nil
        }
    }

    var display: Display {
        if isUploading { return .uploading }
        if let uploadProblem { return .problem(uploadProblem, afterJobStarted: false) }
        if alreadyDone { return .alreadyDone }
        switch status {
        case .loading:
            return .loading
        case .failed(let error):
            return .statusFailed(error)
        case .loaded(nil):
            // Only after an upload the server then lost: E8.
            return .problem(.failed, afterJobStarted: false)
        case .loaded(let latest?):
            switch latest.status {
            case "queued": return .queued
            case "running": return .running
            case "succeeded": return .finished(latest.counts)
            default:
                return .problem(ImportProblem(code: latest.errorKind), afterJobStarted: true)
            }
        }
    }

    /// Forgets the last upload's outcome, so a fresh visit to the import
    /// screen starts from the server's status.
    func clearUploadOutcome() {
        uploadProblem = nil
        alreadyDone = false
    }

    /// GET the status. The last answer stays on screen while a new one
    /// loads, so polling does not flicker.
    func load() async {
        do {
            let answer: HistoryImportAnswer = try await api.get("/v1/spotify/history-import")
            try Task.checkCancellation()
            status = .loaded(answer.latest)
        } catch is CancellationError {
            return
        } catch let error as APIError {
            // A 401 needs nothing here: RootView shows the connect screen.
            status = .failed(error)
        } catch {
            status = .failed(.badResponse)
        }
    }

    /// Checks again every `pollInterval` while the import is queued or
    /// running. Returns when it ends, the check fails, or the task is
    /// cancelled (the screen went away).
    func pollWhileActive() async {
        while isActive {
            do {
                try await Task.sleep(for: pollInterval)
            } catch {
                return
            }
            await load()
        }
    }

    /// Uploads the file the user picked, then loads the status.
    ///
    /// The app keeps no copy of the file (WHO-103 §4 rule 2): it is read
    /// in place, and the upload body is a temporary file that `APIClient`
    /// deletes when the upload ends. If the picker gave us a copy inside
    /// this app's own temporary folder, that copy is deleted too.
    func upload(fileAt url: URL) async {
        guard !isUploading else { return }
        isUploading = true
        clearUploadOutcome()
        defer { isUploading = false }

        let accessing = url.startAccessingSecurityScopedResource()
        defer {
            if accessing { url.stopAccessingSecurityScopedResource() }
            Self.deleteIfOurCopy(url)
        }

        if let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > Self.maxUploadBytes {
            uploadProblem = .tooLarge
            return
        }

        do {
            let _: ImportAccepted = try await api.upload(
                "/v1/spotify/history-import", fileAt: url, fileName: Self.uploadName(for: url))
            await load()
        } catch APIError.conflict(code: "history_import_already_done") {
            alreadyDone = true
            await load()
        } catch APIError.conflict(code: "history_import_in_progress") {
            // E9: not a failure. The status shows P2 or P3.
            await load()
        } catch APIError.invalidRequest(let code) {
            uploadProblem = ImportProblem(code: code)
        } catch APIError.offline {
            uploadProblem = .uploadFailed
        } catch APIError.sessionExpired, APIError.notSignedIn {
            // RootView shows the connect screen.
            return
        } catch is APIError {
            uploadProblem = .failed
        } catch is CancellationError {
            uploadProblem = .uploadFailed
        } catch {
            // The file itself could not be read on the phone.
            uploadProblem = .wrongFormat
        }
    }

    /// The file name the server sees. Only the type matters to it, so the
    /// user's own file name is not sent.
    nonisolated static func uploadName(for url: URL) -> String {
        url.pathExtension.lowercased() == "json" ? "history.json" : "history.zip"
    }

    /// Deletes `url` only when it lies inside this app's temporary folder,
    /// where a picker copy would be. A file in the user's Files app is
    /// never touched.
    nonisolated static func deleteIfOurCopy(_ url: URL, temporaryDirectory: URL = FileManager.default.temporaryDirectory) {
        let folder = temporaryDirectory.resolvingSymlinksInPath().standardizedFileURL.path + "/"
        let file = url.resolvingSymlinksInPath().standardizedFileURL.path
        if file.hasPrefix(folder) {
            try? FileManager.default.removeItem(at: url)
        }
    }
}

import XCTest
@testable import New2U

/// The Spotify history file import (WHO-94; server WHO-104, copy WHO-103).
/// All data is synthetic.
@MainActor
final class HistoryImportTests: XCTestCase {
    private func makeModel() -> HistoryImportViewModel {
        StubURLProtocol.reset()
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        store.saveToken("synthetic-token")
        addTeardownBlock { @MainActor in store.deleteToken() }
        let api = APIClient(
            baseURL: URL(string: "http://127.0.0.1:8000"),
            sessionStore: store,
            urlSession: StubURLProtocol.makeURLSession())
        return HistoryImportViewModel(api: api, pollInterval: .milliseconds(1))
    }

    /// A GET /v1/spotify/history-import answer with one import.
    /// `nonisolated static` because `StubURLProtocol.handler` calls it on the URL loading thread (WHO-200).
    nonisolated private static func latest(_ status: String, errorKind: String? = nil, inserted: Int = 0, duplicate: Int = 0) -> String {
        let kind = errorKind.map { "\"\($0)\"" } ?? "null"
        return """
            {"latest": {"id": "6f1c1a52-6b0e-4c1e-9d55-3c3f3d1c0a01", "status": "\(status)",
             "error_kind": \(kind), "created_at": "2026-10-17T12:00:00Z",
             "started_at": null, "finished_at": null,
             "counts": {"rows_seen": 10, "rows_inserted": \(inserted), "rows_duplicate": \(duplicate),
                        "rows_skipped_not_music": 0, "rows_skipped_incognito": 0,
                        "rows_skipped_short": 0, "rows_unreadable": 0}}}
            """
    }

    /// A small synthetic file in this test's own temporary folder.
    private func makeFile(_ name: String = "my_spotify_data.zip", bytes: Int = 16) throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent(name)
        try Data(repeating: 0x41, count: bytes).write(to: url)
        return url
    }

    // MARK: - Status and the Settings row (§3)

    func testNoImportYetShowsTheStartRow() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: #"{"latest": null}"#) }
        await model.load()
        XCTAssertEqual(model.settingsEntry, .start)
        XCTAssertEqual(StubURLProtocol.lastRequest?.url?.path, "/v1/spotify/history-import")
    }

    func testRowFollowsTheStatus() async {
        let model = makeModel()
        for (status, entry) in [
            ("failed", HistoryImportViewModel.SettingsEntry.retry),
            ("queued", .importing),
            ("running", .importing),
        ] {
            StubURLProtocol.handler = { _ in .init(body: Self.latest(status)) }
            await model.load()
            XCTAssertEqual(model.settingsEntry, entry, status)
        }
    }

    func testRowIsHiddenAfterSuccess() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.latest("succeeded", inserted: 3)) }
        await model.load()
        XCTAssertNil(model.settingsEntry)
    }

    func testRowIsHiddenWhenTheCheckFails() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.load()
        XCTAssertNil(model.settingsEntry)
        XCTAssertEqual(model.display, .statusFailed(.offline))
    }

    // MARK: - Display (§5)

    func testDisplayForEachStatus() async {
        let model = makeModel()
        StubURLProtocol.handler = { _ in .init(body: Self.latest("queued")) }
        await model.load()
        XCTAssertEqual(model.display, .queued)

        StubURLProtocol.handler = { _ in .init(body: Self.latest("running")) }
        await model.load()
        XCTAssertEqual(model.display, .running)

        StubURLProtocol.handler = { _ in .init(body: Self.latest("failed", errorKind: "interrupted")) }
        await model.load()
        XCTAssertEqual(model.display, .problem(.failed, afterJobStarted: true))

        StubURLProtocol.handler = { _ in .init(body: Self.latest("failed", errorKind: "no_streaming_history")) }
        await model.load()
        XCTAssertEqual(model.display, .problem(.noHistoryFiles, afterJobStarted: true))
    }

    func testPollingStopsWhenTheImportEnds() async {
        let model = makeModel()
        var answers = [Self.latest("queued"), Self.latest("running"), Self.latest("succeeded", inserted: 2)]
        StubURLProtocol.handler = { _ in .init(body: answers.count > 1 ? answers.removeFirst() : answers[0]) }
        await model.load()
        await model.pollWhileActive()
        XCTAssertEqual(StubURLProtocol.requestCount, 3)
        guard case .finished(let counts) = model.display else { return XCTFail("\(model.display)") }
        XCTAssertEqual(counts.rowsInserted, 2)
    }

    func testErrorCodesMapToOneMessageEach() {
        XCTAssertEqual(ImportProblem(code: "upload_too_large"), .tooLarge)
        XCTAssertEqual(ImportProblem(code: "unsupported_file"), .wrongFormat)
        XCTAssertEqual(ImportProblem(code: "invalid_json"), .wrongFormat)
        XCTAssertEqual(ImportProblem(code: "zip_encrypted"), .unsafeFile)
        XCTAssertEqual(ImportProblem(code: "zip_nested_archive"), .unsafeFile)
        XCTAssertEqual(ImportProblem(code: "no_streaming_history"), .noHistoryFiles)
        XCTAssertEqual(ImportProblem(code: "too_many_rows"), .tooManyPlays)
        // Unknown and missing codes read as E8, never as a raw code.
        XCTAssertEqual(ImportProblem(code: "something_new"), .failed)
        XCTAssertEqual(ImportProblem(code: nil), .failed)
    }

    // MARK: - Upload

    func testUploadSendsOneMultipartFilePartThenLoads() async throws {
        let model = makeModel()
        let file = try makeFile()
        var methods: [String] = []
        var contentType: String?
        StubURLProtocol.handler = { request in
            methods.append(request.httpMethod ?? "")
            if request.httpMethod == "POST" {
                contentType = request.value(forHTTPHeaderField: "Content-Type")
                return .init(status: 202, body: #"{"id": "6f1c1a52-6b0e-4c1e-9d55-3c3f3d1c0a01", "status": "queued"}"#)
            }
            return .init(body: Self.latest("queued"))
        }
        await model.upload(fileAt: file)
        XCTAssertEqual(methods, ["POST", "GET"])
        XCTAssertTrue(contentType?.hasPrefix("multipart/form-data; boundary=") ?? false)
        XCTAssertEqual(model.display, .queued)
        XCTAssertFalse(model.isUploading)
    }

    func testMultipartBodyHasTheFileAndNoUserFileName() async throws {
        let file = try makeFile("Jane's export.zip", bytes: 5)
        let body = FileManager.default.temporaryDirectory.appendingPathComponent("body-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: body) }
        try await APIClient.writeMultipartBody(
            to: body, boundary: "B", fileURL: file, fileName: HistoryImportViewModel.uploadName(for: file))
        let text = String(decoding: try Data(contentsOf: body), as: UTF8.self)
        XCTAssertEqual(
            text,
            "--B\r\nContent-Disposition: form-data; name=\"file\"; filename=\"history.zip\"\r\n"
                + "Content-Type: application/octet-stream\r\n\r\nAAAAA\r\n--B--\r\n")
    }

    func testUploadNameKeepsOnlyTheType() {
        XCTAssertEqual(HistoryImportViewModel.uploadName(for: URL(fileURLWithPath: "/x/Some Name.JSON")), "history.json")
        XCTAssertEqual(HistoryImportViewModel.uploadName(for: URL(fileURLWithPath: "/x/my_spotify_data.zip")), "history.zip")
    }

    func testNoUploadBodyIsLeftBehind() async throws {
        let model = makeModel()
        let file = try makeFile()
        func leftovers() throws -> [String] {
            try FileManager.default.contentsOfDirectory(atPath: FileManager.default.temporaryDirectory.path)
                .filter { $0.hasSuffix(".multipart") }
        }
        let before = try leftovers()
        StubURLProtocol.handler = { _ in .init(status: 500) }
        await model.upload(fileAt: file)
        XCTAssertEqual(try leftovers(), before)
    }

    func testTooLargeFileIsNotSent() async throws {
        let model = makeModel()
        let file = try makeFile()
        // A sparse file: the size is set without writing 200 MB.
        let handle = try FileHandle(forWritingTo: file)
        try handle.truncate(atOffset: UInt64(HistoryImportViewModel.maxUploadBytes + 1))
        try handle.close()
        await model.upload(fileAt: file)
        XCTAssertEqual(StubURLProtocol.requestCount, 0)
        XCTAssertEqual(model.display, .problem(.tooLarge, afterJobStarted: false))
    }

    func testRefusedUploadShowsItsMessage() async throws {
        let model = makeModel()
        let file = try makeFile()
        StubURLProtocol.handler = { _ in
            .init(status: 422, body: #"{"detail": {"code": "no_streaming_history", "message": "x"}}"#)
        }
        await model.upload(fileAt: file)
        XCTAssertEqual(model.display, .problem(.noHistoryFiles, afterJobStarted: false))

        StubURLProtocol.handler = { _ in
            .init(status: 413, body: #"{"detail": {"code": "upload_too_large", "message": "x"}}"#)
        }
        // A new file: the first upload deleted its copy in the temporary folder.
        await model.upload(fileAt: try makeFile())
        XCTAssertEqual(model.display, .problem(.tooLarge, afterJobStarted: false))
    }

    func testOfflineUploadIsE11() async throws {
        let model = makeModel()
        let file = try makeFile()
        StubURLProtocol.handler = { _ in .init(error: URLError(.notConnectedToInternet)) }
        await model.upload(fileAt: file)
        XCTAssertEqual(model.display, .problem(.uploadFailed, afterJobStarted: false))
    }

    func test409InProgressShowsTheProgressScreen() async throws {
        let model = makeModel()
        let file = try makeFile()
        StubURLProtocol.handler = { request in
            if request.httpMethod == "POST" {
                return .init(status: 409, body: #"{"detail": {"code": "history_import_in_progress", "message": "x"}}"#)
            }
            return .init(body: Self.latest("running"))
        }
        await model.upload(fileAt: file)
        XCTAssertEqual(model.display, .running)
    }

    func test409AlreadyDoneShowsE10AndHidesTheRow() async throws {
        let model = makeModel()
        let file = try makeFile()
        StubURLProtocol.handler = { request in
            if request.httpMethod == "POST" {
                return .init(status: 409, body: #"{"detail": {"code": "history_import_already_done", "message": "x"}}"#)
            }
            return .init(body: Self.latest("succeeded", inserted: 4))
        }
        await model.upload(fileAt: file)
        XCTAssertEqual(model.display, .alreadyDone)
        XCTAssertNil(model.settingsEntry)
    }

    func testExpiredSessionShowsNoImportMessage() async throws {
        let model = makeModel()
        let file = try makeFile()
        StubURLProtocol.handler = { _ in .init(status: 401) }
        await model.upload(fileAt: file)
        // RootView swaps to the connect screen; this screen adds nothing.
        XCTAssertNil(model.uploadProblem)
    }

    // MARK: - The app keeps no copy (§4 rule 2)

    func testAPickerCopyInOurTemporaryFolderIsDeleted() throws {
        let copy = try makeFile()
        HistoryImportViewModel.deleteIfOurCopy(copy)
        XCTAssertFalse(FileManager.default.fileExists(atPath: copy.path))
    }

    func testAFileOutsideOurTemporaryFolderIsKept() throws {
        let file = try makeFile()
        let elsewhere = FileManager.default.temporaryDirectory.appendingPathComponent("not-ours-\(UUID().uuidString)")
        HistoryImportViewModel.deleteIfOurCopy(file, temporaryDirectory: elsewhere)
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path))
    }

    // MARK: - Result text (§5.2)

    private func counts(inserted: Int, duplicate: Int = 0, notMusic: Int = 0, short: Int = 0) -> ImportCounts {
        ImportCounts(
            rowsInserted: inserted, rowsDuplicate: duplicate, rowsSkippedNotMusic: notMusic,
            rowsSkippedIncognito: 0, rowsSkippedShort: short, rowsUnreadable: 0)
    }

    func testResultTextUsesTheLocaleAndPlural() {
        let us = Locale(identifier: "en_US")
        XCTAssertEqual(Messages.importResult(counts(inserted: 1), locale: us), "1 play was added to your history.")
        XCTAssertEqual(
            Messages.importResult(counts(inserted: 12_345), locale: us), "12,345 plays were added to your history.")
        XCTAssertEqual(
            Messages.importResult(counts(inserted: 0, duplicate: 5), locale: us),
            "Every play in the file was already in your history, so nothing new was added.")
    }

    func testDetailsHideZeroRows() {
        let lines = Messages.importDetails(counts(inserted: 3, notMusic: 2), locale: Locale(identifier: "en_US"))
        XCTAssertEqual(lines, ["3 added", "2 skipped: podcasts, audiobooks, videos and other non-music"])
    }
}

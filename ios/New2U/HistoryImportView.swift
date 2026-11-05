import SwiftUI
import UniformTypeIdentifiers

/// The history import screen, pushed from Settings. It shows either the
/// consent screen or the status of the import:
///
/// consent → "Choose file" → system picker → upload (P1) → queued (P2)
/// → running (P3) → finished (S1/S2) or failed (E1–E8, E11).
/// "Try again" goes back to consent.
///
/// The state lives in `HistoryImportViewModel`; this file lays it out.
struct HistoryImportView: View {
    let viewModel: HistoryImportViewModel
    /// Leaves Settings and shows the timeline, for "Open timeline".
    let openTimeline: () -> Void

    /// True while the consent screen is shown instead of the status.
    @State private var showsConsent: Bool
    @State private var showsPicker = false
    @State private var showsDetails = false
    /// Set on the first appear only. Coming back from "How to get the
    /// file" must keep the message on screen.
    @State private var hasAppeared = false

    @Environment(\.dismiss) private var dismiss

    init(viewModel: HistoryImportViewModel, openTimeline: @escaping () -> Void) {
        self.viewModel = viewModel
        self.openTimeline = openTimeline
        // An upload or a running import opens on its status; anything
        // else on consent.
        _showsConsent = State(initialValue: !(viewModel.isUploading || viewModel.isActive))
    }

    var body: some View {
        Group {
            if showsConsent {
                HistoryConsentView(chooseFile: { showsPicker = true }, cancel: { dismiss() })
            } else {
                status
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .fileImporter(isPresented: $showsPicker, allowedContentTypes: [.zip, .json]) { result in
            // A cancelled picker leaves the consent screen as it was.
            guard case .success(let url) = result else { return }
            showsConsent = false
            showsDetails = false
            // Not tied to this screen: leaving it must not stop the upload.
            Task { await viewModel.upload(fileAt: url) }
        }
        // Checks again while the server works. Restarts when the import
        // becomes active (after an upload) and stops when it ends.
        .task(id: viewModel.isActive) { await viewModel.pollWhileActive() }
        .onAppear {
            // A new visit starts from the server's status, not from the
            // outcome of an upload made on an earlier visit.
            if !hasAppeared { viewModel.clearUploadOutcome() }
            hasAppeared = true
        }
    }

    // MARK: - Status

    @ViewBuilder
    private var status: some View {
        switch viewModel.display {
        case .loading:
            ProgressView("Checking your import…")
        case .uploading:
            progress(Messages.uploading)
        case .queued:
            progress(Messages.queued)
        case .running:
            progress(Messages.running)
        case .finished(let counts):
            finished(counts)
        case .alreadyDone:
            message(Messages.alreadyImported) {
                Button(Messages.openTimeline, action: openTimeline)
                    .buttonStyle(.borderedProminent)
            }
        case .problem(let problem, let afterJobStarted):
            let text = Messages.importProblem(problem)
            message(text, note: afterJobStarted ? Messages.somePlaysMayBeAdded : nil) {
                if problem == .noHistoryFiles {
                    NavigationLink(Messages.howToGetTheFile) { HistoryStepsView() }
                }
                failureButtons
            }
        case .statusFailed(let error):
            message(Messages.failure(error)) {
                Button("Try again") { Task { await viewModel.load() } }
                    .buttonStyle(.borderedProminent)
            }
        }
    }

    private func progress(_ text: Messages.Text) -> some View {
        ContentUnavailableView {
            ProgressView()
                .controlSize(.large)
                .padding(.bottom, Theme.Spacing.s)
            Text(text.title)
        } description: {
            Text(text.detail)
        }
    }

    private func finished(_ counts: ImportCounts) -> some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.m) {
                Image(systemName: "checkmark.circle")
                    .font(.largeTitle)
                    .accessibilityHidden(true)
                Text(Messages.importFinished)
                    .font(.title2.bold())
                Text(Messages.importResult(counts))
                    .multilineTextAlignment(.center)
                if counts.rowsInserted >= 1 {
                    Button(Messages.openTimeline, action: openTimeline)
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                }
                DisclosureGroup(Messages.details, isExpanded: $showsDetails) {
                    VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                        ForEach(Messages.importDetails(counts), id: \.self) { line in
                            Text(line)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, Theme.Spacing.s)
                }
            }
            .padding()
        }
    }

    /// "Try again" (back to consent) and "Close" under every failure.
    private var failureButtons: some View {
        HStack(spacing: Theme.Spacing.m) {
            Button("Try again") {
                viewModel.clearUploadOutcome()
                showsConsent = true
            }
            .buttonStyle(.borderedProminent)
            Button("Close") { dismiss() }
                .buttonStyle(.bordered)
        }
    }

    private func message(
        _ text: Messages.Text, note: String? = nil, @ViewBuilder actions: () -> some View
    ) -> some View {
        ContentUnavailableView {
            Label(text.title, systemImage: text.systemImage)
        } description: {
            // Markdown, so `my_spotify_data.zip` reads as a file name.
            Text(LocalizedStringKey(text.detail))
            if let note {
                Text(note)
            }
        } actions: {
            actions()
                .controlSize(.large)
        }
    }
}

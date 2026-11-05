import SwiftUI

extension View {
    /// "Not new" on one list row (WHO-123), three ways to reach it: swipe
    /// the row left, touch and hold it, or the VoiceOver actions rotor.
    func notNewActions(_ mark: NotNewMark, marker: NotNewMarker) -> some View {
        let run: () -> Void = { Task { await marker.mark(mark) } }
        return self
            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                Button(Messages.notNewAction, systemImage: "checkmark.circle") { run() }
                    .tint(.indigo)
            }
            .contextMenu {
                Button(Messages.notNewAction, systemImage: "checkmark.circle") { run() }
            }
            .accessibilityAction(named: Messages.notNewAction) { run() }
    }

    /// The Undo or error banner of `marker`, at the bottom of the screen.
    func notNewBanner(_ marker: NotNewMarker) -> some View {
        safeAreaInset(edge: .bottom, spacing: 0) {
            NotNewBanner(marker: marker)
        }
    }
}

/// The short banner after a "Not new" mark: "“Album” marked as not new"
/// with an Undo button. It goes away by itself after a few seconds. When
/// the mark or the Undo failed, it shows the message instead.
struct NotNewBanner: View {
    let marker: NotNewMarker

    /// How long the banner stays on screen.
    static let duration: Duration = .seconds(5)

    var body: some View {
        Group {
            if let failure = marker.failure {
                bar(announcing: failure) {
                    Label(failure, systemImage: "exclamationmark.triangle")
                        .font(.subheadline)
                    Spacer(minLength: Theme.Spacing.s)
                    Button("OK") { marker.dismissFailure() }
                        .frame(minWidth: 44, minHeight: 44)
                }
                // A failure stays a little longer, so there is time to read it.
                .task(id: failure) {
                    try? await Task.sleep(for: Self.duration * 2)
                    if !Task.isCancelled { marker.dismissFailure() }
                }
            } else if let mark = marker.undoable {
                bar(announcing: Messages.notNewMarked(name: mark.name)) {
                    Text(Messages.notNewMarked(name: mark.name))
                        .font(.subheadline)
                        .lineLimit(2)
                    Spacer(minLength: Theme.Spacing.s)
                    Button("Undo") { Task { await marker.undo() } }
                        .font(.subheadline.weight(.semibold))
                        .frame(minWidth: 44, minHeight: 44)
                }
                .task(id: mark) {
                    try? await Task.sleep(for: Self.duration)
                    if !Task.isCancelled && marker.undoable == mark { marker.dismissUndo() }
                }
            }
        }
        .animation(.default, value: marker.undoable)
        .animation(.default, value: marker.failure)
    }

    private func bar(announcing message: String, @ViewBuilder _ content: () -> some View) -> some View {
        HStack(spacing: Theme.Spacing.m) {
            content()
        }
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.vertical, Theme.Spacing.xs)
        .liquidGlass(in: RoundedRectangle(cornerRadius: 24))
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.bottom, Theme.Spacing.s)
        .transition(.move(edge: .bottom).combined(with: .opacity))
        // VoiceOver reads the banner as soon as it shows.
        .accessibilityElement(children: .contain)
        .onAppear { AccessibilityNotification.Announcement(message).post() }
    }
}

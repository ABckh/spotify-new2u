import SwiftUI

/// The sheet for a custom range: a start date, an end date, and a
/// "Show" button. The sheet closes only when the server accepts the
/// range; otherwise the reason appears under the dates (WHO-13 R-23).
///
/// All logic is in `CustomRangeViewModel`. This file only lays it out.
struct CustomRangeSheet: View {
    @State var viewModel: CustomRangeViewModel
    /// Called with the accepted range. The screen then loads it.
    let apply: (PeriodQuery) -> Void

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DatePicker("Start", selection: $viewModel.from, displayedComponents: .date)
                    DatePicker("End", selection: $viewModel.to, displayedComponents: .date)
                } footer: {
                    if let message = viewModel.message {
                        // Red only on the icon: red text on white is
                        // below WCAG AA at this size.
                        Label {
                            Text(message).foregroundStyle(Color(.label))
                        } icon: {
                            Image(systemName: "exclamationmark.circle.fill")
                                .foregroundStyle(Color(.systemRed))
                        }
                        .font(.subheadline)
                    } else {
                        Text("Both dates are included.")
                            .font(.subheadline)
                            .foregroundStyle(Theme.secondaryText)
                    }
                }
            }
            // The pickers show and return dates in the display zone, so a
            // picked date is exactly the "YYYY-MM-DD" we send.
            .environment(\.timeZone, viewModel.timeZone)
            .navigationTitle("Custom range")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    if viewModel.isChecking {
                        ProgressView().accessibilityLabel("Checking dates")
                    } else {
                        Button("Show") { Task { await show() } }
                    }
                }
            }
            .task {
                #if DEBUG
                // Screenshots of the refused state: check once on open.
                if DebugFixtures.checksCustomRangeOnOpen { await show() }
                #endif
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func show() async {
        if let query = await viewModel.check() {
            apply(query)
            dismiss()
        }
    }
}

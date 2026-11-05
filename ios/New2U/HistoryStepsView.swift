import SwiftUI

/// Screen H (WHO-103 §2): how to ask Spotify for the history file.
///
/// Static text, no view model. Shown once after connect when the user
/// turned on "Import my full Spotify history", and from Settings by
/// "How to get the file". "Done" closes it and never starts an upload.
struct HistoryStepsView: View {
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                Text(Messages.stepsTitle)
                    .font(.title2.bold())
                    .accessibilityAddTraits(.isHeader)
                Text(Messages.stepsIntro)

                ForEach(Array(Messages.steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                        Text("\(index + 1).")
                            .monospacedDigit()
                        Text(LocalizedStringKey(step))
                    }
                }

                Text(Messages.stepsOneTime)
                    .foregroundStyle(Theme.secondaryText)

                VStack(spacing: Theme.Spacing.s) {
                    // Safari, outside the app: we never see what the user
                    // types on Spotify's page (§2 rule 1).
                    Button(Messages.stepsOpenPrivacyPage) { openURL(Messages.spotifyPrivacyPage) }
                        .buttonStyle(.borderedProminent)
                    Button("Done") { dismiss() }
                        .buttonStyle(.bordered)
                }
                .controlSize(.large)
                .frame(maxWidth: .infinity)
                .padding(.top, Theme.Spacing.s)
            }
            .padding()
        }
        .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    HistoryStepsView()
}

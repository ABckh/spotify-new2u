import SwiftUI

/// The consent screen (WHO-103 §4), shown every time before the file
/// picker. Never skipped and never remembered.
///
/// It only shows text and two buttons. `HistoryImportView` opens the
/// picker on "Choose file" and leaves on "Cancel". Neither button is
/// styled to push the user (§4 rule 1).
struct HistoryConsentView: View {
    let chooseFile: () -> Void
    let cancel: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.m) {
                Text(Messages.consentTitle)
                    .font(.title2.bold())
                    .accessibilityAddTraits(.isHeader)

                ForEach(Messages.consentParagraphs, id: \.lead) { paragraph in
                    // Markdown: the lead in bold, the file name as code.
                    Text(LocalizedStringKey("**\(paragraph.lead)** \(paragraph.text)"))
                }

                // R7 is not on the server yet, so the file's owner is
                // not checked (§4).
                Text(Messages.consentOwnHistoryOnly)

                HStack(spacing: Theme.Spacing.m) {
                    Button(Messages.consentChooseFile, action: chooseFile)
                        .frame(maxWidth: .infinity)
                    Button("Cancel", action: cancel)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
                .padding(.top, Theme.Spacing.s)
            }
            .padding()
        }
    }
}

#Preview {
    HistoryConsentView(chooseFile: {}, cancel: {})
}

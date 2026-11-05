import SwiftUI

/// A plain screen laid over the app whenever it is not in front.
///
/// When the app goes to the background, iOS saves a screenshot of it for
/// the app switcher, and keeps that image on disk. Without this cover the
/// image would show track titles and play times (WHO-30, W4, S6.5d).
/// `PrivacyCoverWindow` shows it whenever the app is not in front.
struct PrivacyCover: View {
    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
                .ignoresSafeArea()
            Image(systemName: "music.note.list")
                .font(.system(size: 48))
                .foregroundStyle(Theme.secondaryText)
                .accessibilityHidden(true)
        }
    }
}

#Preview {
    PrivacyCover()
}

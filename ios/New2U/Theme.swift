import SwiftUI
import UIKit

/// The few layout and colour constants every screen shares.
enum Theme {
    /// The only spacing values the app uses (design bar: 4/8/12/16/24).
    enum Spacing {
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
    }

    /// Secondary text (artist, time, duration, totals).
    ///
    /// The system `.secondary` grey is about 3.5:1 on white, which fails
    /// WCAG AA (4.5:1) for small text. This grey is about 6.7:1 on white
    /// in light mode and about 10:1 on black in dark mode, and darkens or
    /// lightens further when Increase Contrast is on.
    static let secondaryText = Color(uiColor: UIColor { traits in
        let highContrast = traits.accessibilityContrast == .high
        if traits.userInterfaceStyle == .dark {
            return UIColor(white: highContrast ? 0.82 : 0.70, alpha: 1)
        }
        return UIColor(white: highContrast ? 0.25 : 0.36, alpha: 1)
    })

    /// The neutral square shown where a cover would go.
    static let placeholderFill = Color(uiColor: .secondarySystemFill)

    /// Side length of the leading artwork square in a row.
    static let artworkSize: CGFloat = 44

    /// The top track's cover in the summary, next to one line of text.
    static let smallArtworkSize: CGFloat = 28

    /// The album's cover at the top of the album screen.
    static let headerArtworkSize: CGFloat = 120

    /// The insets of every row in the main list.
    static let rowInsets = EdgeInsets(top: Spacing.s, leading: Spacing.l, bottom: Spacing.s, trailing: Spacing.l)
}

extension View {
    /// Liquid Glass behind floating controls (the tab switch, the Undo
    /// banner) from iOS 26 on, and the thick blur material before that.
    ///
    /// `interactive` glass answers every touch on it as a whole: it dims
    /// and shimmers under the finger. Right for one control, like the
    /// banner's Undo; not for the tab switch, where each tap would flash
    /// the whole bar.
    func liquidGlass(in shape: some Shape, interactive: Bool = true) -> some View {
        Group {
            if #available(iOS 26, *) {
                glassEffect(interactive ? .regular.interactive() : .regular, in: shape)
            } else {
                background(.thickMaterial, in: shape)
                    .shadow(color: .black.opacity(0.15), radius: 8, y: 2)
            }
        }
    }
}

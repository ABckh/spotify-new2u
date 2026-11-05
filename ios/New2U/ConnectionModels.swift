import Foundation

/// 200 from GET /v1/me/connection (api-contract section 3.6): whether
/// the server can still read the user's plays from Spotify.
///
/// Only the fields a screen uses are decoded. The app never sees a
/// Spotify token; this is status only.
struct Connection: Decodable, Equatable {
    /// "active" or "reauth_required".
    let status: String
    let reauthReason: String?
    let spotifyDisplayName: String?
    /// When Spotify's permission runs out. Nil if the server leaves it out.
    let grantExpiresAt: Date?
    /// True when the user must reconnect now, or within 30 days.
    let reconnectSoon: Bool
    /// The last successful poll: "Updated N min ago".
    let lastPolledAt: Date?
    /// When "Refresh from Spotify" is next accepted. Nil or a past time
    /// means now (WHO-95). Older servers leave it out.
    let nextManualPollAt: Date?
    /// Where live collection began. The timeline draws the import marker
    /// here (WHO-90 T1). Optional so an older server still decodes.
    let collectionStartedAt: Date?
    /// When the server imported the recent plays the user asked for at
    /// their first connect. Nil if they did not ask, or it has not run yet.
    /// Set even when the import found no earlier play, so it does not
    /// decide the marker (WHO-93 F2).
    let historyImportedAt: Date?
    /// True only if the import stored a play from before
    /// `collectionStartedAt`. Nil when an older server leaves the key out,
    /// which counts as false. Read it through `showsImportMarker`.
    let hasImportedPlays: Bool?

    /// Whether the timeline may draw the import marker (WHO-90 T1).
    var showsImportMarker: Bool { hasImportedPlays == true }

    /// True while the server is collecting new plays.
    var isActive: Bool { status == "active" }
}

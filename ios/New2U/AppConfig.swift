import Foundation

/// Reads build settings that reach the app through Info.plist.
enum AppConfig {
    /// The API server origin, or nil when the build has no usable value.
    ///
    /// The value comes from `API_BASE_URL` in `Config/Debug.xcconfig` or
    /// `Config/Release.xcconfig`, passed through the `APIBaseURL` key in
    /// `Config/Info.plist`.
    static func apiBaseURL(bundle: Bundle = .main) -> URL? {
        let raw = bundle.object(forInfoDictionaryKey: "APIBaseURL") as? String
        return parseBaseURL(raw)
    }

    /// Accepts only an absolute http or https URL with a host.
    /// Anything else (empty, an unexpanded "$(API_BASE_URL)", a typo, or
    /// the ".invalid" placeholder in `Config/Release.xcconfig`) returns
    /// nil, so the app shows "Server address not configured".
    static func parseBaseURL(_ raw: String?) -> URL? {
        guard let raw else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = url.host?.lowercased(), !host.isEmpty,
              !host.hasSuffix(".invalid")
        else { return nil }
        return url
    }
}

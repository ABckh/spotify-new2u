import XCTest
@testable import New2U

final class AppConfigTests: XCTestCase {
    @MainActor
    func testAcceptsHTTPAndHTTPSOrigins() {
        XCTAssertEqual(AppConfig.parseBaseURL("http://127.0.0.1:8000")?.absoluteString, "http://127.0.0.1:8000")
        XCTAssertEqual(AppConfig.parseBaseURL(" https://api.example.test ")?.host, "api.example.test")
    }

    @MainActor
    func testRefusesMissingOrUnusableValues() {
        XCTAssertNil(AppConfig.parseBaseURL(nil))
        XCTAssertNil(AppConfig.parseBaseURL(""))
        XCTAssertNil(AppConfig.parseBaseURL("$(API_BASE_URL)"))
        XCTAssertNil(AppConfig.parseBaseURL("127.0.0.1:8000"))
        XCTAssertNil(AppConfig.parseBaseURL("ftp://example.test"))
    }

    @MainActor
    func testRefusesTheReleasePlaceholderHost() {
        XCTAssertNil(AppConfig.parseBaseURL("https://api-hostname-not-set.invalid"))
    }

    // MARK: - Each build configuration

    @MainActor
    func testDebugBuildReadsTheSimulatorAddress() {
        // The tests run inside the Debug build of the app, so this reads
        // the real Info.plist value that came from Debug.xcconfig.
        XCTAssertEqual(AppConfig.apiBaseURL()?.absoluteString, "http://127.0.0.1:8000")
        XCTAssertEqual(Self.xcconfigValue("Debug.xcconfig", key: "API_BASE_URL"), "http://127.0.0.1:8000")
    }

    @MainActor
    func testReleaseBuildUsesHTTPS() throws {
        // The Release build cannot run here, so read Release.xcconfig.
        let raw = try XCTUnwrap(Self.xcconfigValue("Release.xcconfig", key: "API_BASE_URL"))
        let url = try XCTUnwrap(URL(string: raw))
        XCTAssertEqual(url.scheme, "https")
        XCTAssertFalse(url.host?.isEmpty ?? true)
    }

    // MARK: - App Transport Security

    @MainActor
    func testReleaseInfoPlistHasNoTransportSecurityException() throws {
        let plist = try Self.readPlist("Info.plist")
        XCTAssertNil(plist["NSAppTransportSecurity"])
    }

    @MainActor
    func testDebugInfoPlistOnlyAllowsLocalNetworking() throws {
        let plist = try Self.readPlist("Info-Debug.plist")
        let ats = try XCTUnwrap(plist["NSAppTransportSecurity"] as? [String: Any])
        XCTAssertEqual(Array(ats.keys), ["NSAllowsLocalNetworking"])
        XCTAssertEqual(ats["NSAllowsLocalNetworking"] as? Bool, true)

        // And that is the Info.plist the Debug app was built with.
        let built = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "NSAppTransportSecurity") as? [String: Any])
        XCTAssertEqual(Array(built.keys), ["NSAllowsLocalNetworking"])
    }

    // MARK: - Reading files from ios/Config

    /// The ios/Config folder, found from this test file's own path.
    private static var configFolder: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Config")
    }

    /// The value of `KEY = value` in an xcconfig file, with the "$()"
    /// that protects "//" removed, as Xcode does.
    private static func xcconfigValue(_ file: String, key: String) -> String? {
        let url = configFolder.appendingPathComponent(file)
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        for line in text.components(separatedBy: .newlines) {
            let parts = line.split(separator: "=", maxSplits: 1)
            guard parts.count == 2,
                  parts[0].trimmingCharacters(in: .whitespaces) == key
            else { continue }
            return parts[1]
                .trimmingCharacters(in: .whitespaces)
                .replacingOccurrences(of: "$()", with: "")
        }
        return nil
    }

    private static func readPlist(_ file: String) throws -> [String: Any] {
        let data = try Data(contentsOf: configFolder.appendingPathComponent(file))
        let plist = try PropertyListSerialization.propertyList(from: data, format: nil)
        return try XCTUnwrap(plist as? [String: Any])
    }
}

import XCTest
@testable import New2U

/// The text Settings shows (story S13): the zone names and "last checked".
@MainActor
final class SettingsTests: XCTestCase {
    private let english = Locale(identifier: "en_GB")

    func testZoneNameIsTheGenericNameInTheDeviceLanguage() {
        let warsaw = TimeZone(identifier: "Europe/Warsaw")!
        XCTAssertEqual(DisplayFormat.zoneName(warsaw, locale: english), "Central European Time")
    }

    func testZoneNameFollowsTheLocale() {
        let warsaw = TimeZone(identifier: "Europe/Warsaw")!
        let german = DisplayFormat.zoneName(warsaw, locale: Locale(identifier: "de_DE"))
        XCTAssertNotEqual(german, "Central European Time")
        XCTAssertFalse(german.isEmpty)
    }

    func testSameZoneIsNotShownTwice() {
        let warsaw = TimeZone(identifier: "Europe/Warsaw")!
        XCTAssertFalse(DisplayFormat.isDifferentZone(warsaw, TimeZone(identifier: "Europe/Warsaw")!))
    }

    /// Warsaw and Berlin share an offset all year, but they are different
    /// zones, so after a move the device zone is still shown.
    func testZonesWithTheSameOffsetAreStillDifferent() {
        let warsaw = TimeZone(identifier: "Europe/Warsaw")!
        let berlin = TimeZone(identifier: "Europe/Berlin")!
        XCTAssertTrue(DisplayFormat.isDifferentZone(warsaw, berlin))
    }

    func testLastCheckedIsRelative() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let text = DisplayFormat.lastChecked(now.addingTimeInterval(-300), locale: english, now: now)
        XCTAssertTrue(text.contains("5"), text)
        XCTAssertTrue(text.contains("ago"), text)
    }

    /// The server clock can run a little ahead of the phone. A poll time
    /// in the future must not read "in 2 min".
    func testLastCheckedInTheFutureReadsAsNow() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let text = DisplayFormat.lastChecked(now.addingTimeInterval(120), locale: english, now: now)
        XCTAssertFalse(text.hasPrefix("in "), text)
    }

    /// Settings must not promise a control that isn't there
    /// (api-contract section 4: no endpoint to change the zone yet).
    func testZoneFooterSaysChangingIsNotAvailable() {
        XCTAssertTrue(Messages.zoneFooter.contains("isn't available yet"))
    }
}

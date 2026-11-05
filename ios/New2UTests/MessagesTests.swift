import XCTest
@testable import New2U

/// WHO-147: the same offline headline on every tab, and no repeated
/// heading in the collection-gap empty state.
@MainActor
final class MessagesTests: XCTestCase {
    func testOfflineTitleIsTheSameOnEveryTab() {
        XCTAssertEqual(Messages.failureTitle(.offline, section: Messages.albumsFailed), "You're offline")
        XCTAssertEqual(Messages.failure(.offline).title, "You're offline")
    }

    func testServerErrorKeepsTheSectionTitle() {
        XCTAssertEqual(Messages.failureTitle(.server(status: 500), section: Messages.albumsFailed), "Couldn't load albums.")
    }

    func testCollectionGapDetailDoesNotRepeatTheTitle() {
        let text = Messages.empty(reason: "collection_gap")
        XCTAssertEqual(text.title, "No plays collected")
        XCTAssertFalse(text.detail.hasPrefix(text.title))
        XCTAssertEqual(text.detail, "Spotify collection was not running for part or all of this period, so plays may be missing.")
    }
}

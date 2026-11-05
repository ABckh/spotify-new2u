import XCTest
import UIKit
@testable import New2U

/// The app-switcher cover (WHO-30, WHO-165). Each test uses its own
/// `NotificationCenter`, so the running app's cover is never touched.
@MainActor
final class PrivacyCoverWindowTests: XCTestCase {
    private func appScene() throws -> UIWindowScene {
        try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
    }

    func testCoverIsUpAsSoonAsTheSceneStartsLeaving() throws {
        let scene = try appScene()
        let center = NotificationCenter()
        let cover = PrivacyCoverWindow()
        cover.start(center: center)

        center.post(name: UIScene.willDeactivateNotification, object: scene)

        // Up before `post` returns: no waiting for a SwiftUI pass.
        let window = try XCTUnwrap(cover.window)
        XCTAssertFalse(window.isHidden)
        XCTAssertTrue(window.windowScene === scene)
        XCTAssertEqual(window.frame, scene.coordinateSpace.bounds)
        XCTAssertEqual(window.backgroundColor, .systemBackground)
        cover.hide()
    }

    func testCoverSitsAboveSheetsAndAlerts() throws {
        let scene = try appScene()
        let center = NotificationCenter()
        let cover = PrivacyCoverWindow()
        cover.start(center: center)

        center.post(name: UIScene.willDeactivateNotification, object: scene)

        let window = try XCTUnwrap(cover.window)
        XCTAssertGreaterThan(window.windowLevel, .alert)
        for other in scene.windows where other !== window {
            XCTAssertGreaterThan(window.windowLevel, other.windowLevel)
        }
        cover.hide()
    }

    func testCoverGoesAwayWhenTheAppIsBackInFront() throws {
        let scene = try appScene()
        let center = NotificationCenter()
        let cover = PrivacyCoverWindow()
        cover.start(center: center)

        center.post(name: UIScene.willDeactivateNotification, object: scene)
        let window = try XCTUnwrap(cover.window)
        center.post(name: UIScene.didActivateNotification, object: scene)

        XCTAssertNil(cover.window)
        XCTAssertTrue(window.isHidden)
    }

    func testLeavingTwiceKeepsOneCover() throws {
        let scene = try appScene()
        let center = NotificationCenter()
        let cover = PrivacyCoverWindow()
        cover.start(center: center)

        center.post(name: UIScene.willDeactivateNotification, object: scene)
        let first = try XCTUnwrap(cover.window)
        center.post(name: UIScene.willDeactivateNotification, object: scene)

        XCTAssertTrue(cover.window === first)
        cover.hide()
    }

    func testCoverIsUpIfOnlyTheBackgroundNoticeArrives() throws {
        let scene = try appScene()
        let center = NotificationCenter()
        let cover = PrivacyCoverWindow()
        cover.start(center: center)

        // No `willDeactivate` first: the backup trigger alone puts it up.
        center.post(name: UIScene.didEnterBackgroundNotification, object: scene)

        let window = try XCTUnwrap(cover.window)
        XCTAssertFalse(window.isHidden)
        cover.hide()
    }
}

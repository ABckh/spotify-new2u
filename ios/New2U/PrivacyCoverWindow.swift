import SwiftUI
import UIKit

/// Shows `PrivacyCover` in its own window, above every other window,
/// whenever the app stops being in front (WHO-30, W4, S6.5d; WHO-165).
///
/// Why a window and not a SwiftUI `.overlay`: iOS draws the app-switcher
/// card the moment the app stops being in front. SwiftUI only hears about
/// that a little later (`scenePhase`) and draws the overlay on its next
/// pass, so on a real iPhone the card could show the covers and titles.
/// This class listens to UIKit's own "about to stop being in front"
/// notification and puts the cover up before that call returns.
///
/// Being a separate window, it also covers sheets, popovers and alerts.
@MainActor
final class PrivacyCoverWindow {
    static let shared = PrivacyCoverWindow()

    /// The cover window while it is up, `nil` while the app is in front.
    private(set) var window: UIWindow?
    private var observers: [NSObjectProtocol] = []

    /// Starts listening. Called once, from `New2UApp.init`. Tests pass
    /// their own `NotificationCenter`.
    func start(center: NotificationCenter = .default) {
        guard observers.isEmpty else { return }
        // `queue: nil` runs the block straight away, on the main thread
        // that posted the notification, before iOS draws the card.
        observers.append(center.addObserver(forName: UIScene.willDeactivateNotification, object: nil, queue: nil) { [weak self] note in
            let scene = note.object as? UIWindowScene
            MainActor.assumeIsolated {
                self?.show(on: scene)
            }
        })
        // Second trigger, in case the first one was missed: the scene is
        // already in the background, and iOS saves the card after this.
        observers.append(center.addObserver(forName: UIScene.didEnterBackgroundNotification, object: nil, queue: nil) { [weak self] note in
            let scene = note.object as? UIWindowScene
            MainActor.assumeIsolated {
                self?.show(on: scene)
            }
        })
        observers.append(center.addObserver(forName: UIScene.didActivateNotification, object: nil, queue: nil) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.hide()
            }
        })
    }

    func show(on scene: UIWindowScene?) {
        guard window == nil, let scene else { return }
        let cover = UIWindow(windowScene: scene)
        // Above alerts, so nothing the app shows can sit on top of it.
        cover.windowLevel = .alert + 1
        // Opaque straight away, even before SwiftUI draws the glyph.
        cover.backgroundColor = .systemBackground
        cover.rootViewController = UIHostingController(rootView: PrivacyCover())
        cover.isHidden = false
        cover.layoutIfNeeded()
        window = cover
    }

    func hide() {
        window?.isHidden = true
        window = nil
    }
}

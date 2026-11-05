import XCTest
@testable import New2U

final class SessionStoreTests: XCTestCase {
    /// A store with its own Keychain entry, removed after the test.
    @MainActor
    private func makeStore() -> SessionStore {
        let store = SessionStore(service: "app.new2u.tests.\(UUID().uuidString)")
        addTeardownBlock { @MainActor in store.deleteToken() }
        return store
    }

    @MainActor
    func testStartsEmpty() {
        XCTAssertNil(makeStore().readToken())
    }

    @MainActor
    func testSaveReplaceAndDelete() {
        let store = makeStore()
        store.saveToken("token-one")
        XCTAssertEqual(store.readToken(), "token-one")

        store.saveToken("token-two")
        XCTAssertEqual(store.readToken(), "token-two")

        store.deleteToken()
        XCTAssertNil(store.readToken())
    }

    @MainActor
    func testKeepsTheRefreshTokenWithTheSession() {
        let store = makeStore()
        store.saveToken("session-one", refreshToken: "refresh-one")
        XCTAssertEqual(store.readRefreshToken(), "refresh-one")

        store.saveToken("session-two", refreshToken: "refresh-two")
        XCTAssertEqual(store.readToken(), "session-two")
        XCTAssertEqual(store.readRefreshToken(), "refresh-two")

        store.deleteToken()
        XCTAssertNil(store.readToken())
        XCTAssertNil(store.readRefreshToken())
    }

    @MainActor
    func testASessionWithoutARefreshTokenDropsTheOldOne() {
        // A used refresh token must never be sent again.
        let store = makeStore()
        store.saveToken("session-one", refreshToken: "refresh-one")

        store.saveToken("session-two")

        XCTAssertEqual(store.readToken(), "session-two")
        XCTAssertNil(store.readRefreshToken())
    }

    @MainActor
    func testDeleteWhenEmptyIsHarmless() {
        let store = makeStore()
        store.deleteToken()
        XCTAssertNil(store.readToken())
    }
}

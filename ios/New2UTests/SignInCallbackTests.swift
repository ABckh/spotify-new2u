import XCTest
@testable import New2U

final class SignInCallbackTests: XCTestCase {
    private let loginID = "6f1c2b4e-8d3a-4c5f-9e7b-1a2b3c4d5e6f"

    @MainActor
    func testReadsAllThreeFields() throws {
        let url = try XCTUnwrap(URL(string:
            "new2u://signed-in?login_id=\(loginID)&result=connected&callback_code=synthetic-code"))

        let callback = try XCTUnwrap(SignInCallback.parse(url))

        XCTAssertEqual(callback.loginID, UUID(uuidString: loginID))
        XCTAssertEqual(callback.result, .connected)
        XCTAssertEqual(callback.callbackCode, "synthetic-code")
    }

    @MainActor
    func testReadsAFailedSignIn() throws {
        let url = try XCTUnwrap(URL(string:
            "new2u://signed-in?result=failed&callback_code=c&login_id=\(loginID)"))
        XCTAssertEqual(SignInCallback.parse(url)?.result, .failed)
    }

    @MainActor
    func testRejectsAnotherSchemeOrHost() throws {
        let query = "login_id=\(loginID)&result=connected&callback_code=c"
        for text in [
            "https://signed-in?\(query)",
            "http://127.0.0.1:8000/signed-in?\(query)",
            "otherapp://signed-in?\(query)",
            "new2u://signed-out?\(query)",
            "new2u://evil.example?\(query)",
            "new2u://signed-in/extra?\(query)",
        ] {
            let url = try XCTUnwrap(URL(string: text))
            XCTAssertNil(SignInCallback.parse(url), text)
        }
    }

    @MainActor
    func testRejectsMissingRepeatedOrUnknownFields() throws {
        for query in [
            "result=connected&callback_code=c",
            "login_id=\(loginID)&callback_code=c",
            "login_id=\(loginID)&result=connected",
            "login_id=\(loginID)&result=connected&callback_code=",
            "login_id=not-a-uuid&result=connected&callback_code=c",
            "login_id=\(loginID)&result=maybe&callback_code=c",
            "login_id=\(loginID)&result=connected&callback_code=a&callback_code=b",
        ] {
            let url = try XCTUnwrap(URL(string: "new2u://signed-in?\(query)"))
            XCTAssertNil(SignInCallback.parse(url), query)
        }
        XCTAssertNil(SignInCallback.parse(try XCTUnwrap(URL(string: "new2u://signed-in"))))
    }
}

import XCTest
@testable import NerveRelayCore

/// Answers every request with a 302 to another URL, as an SSO proxy does.
private final class RedirectingProtocol: URLProtocol {
    nonisolated(unsafe) static var requestedHosts: [String] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let host = request.url?.host ?? ""
        RedirectingProtocol.requestedHosts.append(host)
        if host == "login.test" {
            let ok = HTTPURLResponse(
                url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil
            )!
            client?.urlProtocol(self, didReceive: ok, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data("login page".utf8))
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        let target = URL(string: "https://login.test/sso")!
        let redirect = HTTPURLResponse(
            url: request.url!, statusCode: 302, httpVersion: nil,
            headerFields: ["Location": target.absoluteString]
        )!
        client?.urlProtocol(self, wasRedirectedTo: URLRequest(url: target), redirectResponse: redirect)
        // When the session's delegate refuses the redirect, the redirect
        // response itself is what the caller must see.
        client?.urlProtocol(self, didReceive: redirect, cacheStoragePolicy: .notAllowed)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class RedirectRefusalTests: XCTestCase {
    func testDelegateRefusesRedirects() {
        let url = URL(string: "https://relay.test/api/x")!
        let response = HTTPURLResponse(url: url, statusCode: 302, httpVersion: nil, headerFields: nil)!
        let done = expectation(description: "completion")
        NoRedirectDelegate.shared.urlSession(
            URLSession.shared,
            task: URLSession.shared.dataTask(with: url),
            willPerformHTTPRedirection: response,
            newRequest: URLRequest(url: URL(string: "https://login.test")!)
        ) { next in
            XCTAssertNil(next)
            done.fulfill()
        }
        wait(for: [done], timeout: 1)
    }

    func testTransportReportsRedirectStatusAndDoesNotFollow() async throws {
        RedirectingProtocol.requestedHosts = []
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RedirectingProtocol.self]
        let transport = URLSessionRelayTransport(configuration: config)
        var request = URLRequest(url: URL(string: "https://relay.test/api/voicemail/ingest")!)
        request.httpMethod = "POST"
        let result = try await transport.send(request)
        XCTAssertEqual(result.status, 302, "a redirect must surface as a 3xx, not as the 200 behind it")
        XCTAssertFalse((200...299).contains(result.status))
        XCTAssertFalse(RedirectingProtocol.requestedHosts.contains("login.test"))
    }
}

import XCTest
@testable import NerveRelayCore

/// Test-local stand-in archived under Apple's class name. Fixture text is synthetic.
final class FakeVMTranscript: NSObject, NSCoding {
    let text: String?
    let confidence: Double
    init(text: String?, confidence: Double = 0.9) {
        self.text = text
        self.confidence = confidence
    }
    required init?(coder: NSCoder) { nil }
    func encode(with coder: NSCoder) {
        if let text { coder.encode(text, forKey: "transcriptionString") }
        coder.encode(confidence, forKey: "confidence")
        coder.encode(["a", "b"] as NSArray, forKey: "segments")
        coder.encode(3, forKey: "confidenceRating")
    }

    static func archive(
        _ object: NSObject, className: String = "VMVoicemailTranscript"
    ) -> Data {
        let archiver = NSKeyedArchiver(requiringSecureCoding: false)
        archiver.setClassName(className, for: type(of: object))
        archiver.encode(object, forKey: NSKeyedArchiveRootObjectKey)
        archiver.finishEncoding()
        return archiver.encodedData
    }
}

final class VoicemailTranscriptDecoderTests: XCTestCase {
    func testDecodesTranscriptString() {
        let data = FakeVMTranscript.archive(FakeVMTranscript(text: "Hello, this is a test."))
        XCTAssertEqual(VoicemailTranscriptDecoder.decode(data), "Hello, this is a test.")
    }

    func testIgnoresSegmentsAndConfidence() {
        let data = FakeVMTranscript.archive(FakeVMTranscript(text: "Call me back", confidence: 0.1))
        XCTAssertEqual(VoicemailTranscriptDecoder.decode(data), "Call me back")
    }

    func testTrimsWhitespace() {
        let data = FakeVMTranscript.archive(FakeVMTranscript(text: "  hi there \n"))
        XCTAssertEqual(VoicemailTranscriptDecoder.decode(data), "hi there")
    }

    func testEmptyAndWhitespaceTextIsNil() {
        XCTAssertNil(VoicemailTranscriptDecoder.decode(FakeVMTranscript.archive(FakeVMTranscript(text: ""))))
        XCTAssertNil(VoicemailTranscriptDecoder.decode(FakeVMTranscript.archive(FakeVMTranscript(text: "  \n"))))
    }

    func testMissingKeyIsNil() {
        XCTAssertNil(VoicemailTranscriptDecoder.decode(FakeVMTranscript.archive(FakeVMTranscript(text: nil))))
    }

    func testWrongRootClassIsNil() {
        let data = FakeVMTranscript.archive(FakeVMTranscript(text: "x"), className: "SomethingElse")
        XCTAssertNil(VoicemailTranscriptDecoder.decode(data))
    }

    func testNonArchivePlistIsNil() throws {
        let plist = try PropertyListSerialization.data(
            fromPropertyList: ["transcriptionString": "x"], format: .binary, options: 0)
        XCTAssertNil(VoicemailTranscriptDecoder.decode(plist))
    }

    func testTruncatedIsNil() {
        let data = FakeVMTranscript.archive(FakeVMTranscript(text: "Hello"))
        XCTAssertNil(VoicemailTranscriptDecoder.decode(data.prefix(data.count / 2)))
    }

    func testGarbageAndEmptyAreNil() {
        XCTAssertNil(VoicemailTranscriptDecoder.decode(Data()))
        XCTAssertNil(VoicemailTranscriptDecoder.decode(Data([0x00, 0x01, 0x02, 0xFF, 0xFE])))
        XCTAssertNil(VoicemailTranscriptDecoder.decode(Data(repeating: 0x62, count: 4096)))
    }

    func testOversizeBlobIsNil() {
        let big = Data(repeating: 0, count: VoicemailTranscriptDecoder.maxBlobBytes + 1)
        XCTAssertNil(VoicemailTranscriptDecoder.decode(big))
    }

    func testOversizeTextIsNil() {
        let text = String(repeating: "a", count: VoicemailTranscriptDecoder.maxTextBytes + 1)
        XCTAssertNil(VoicemailTranscriptDecoder.decode(FakeVMTranscript.archive(FakeVMTranscript(text: text))))
    }

    func testNonASCII() {
        let text = "Привет, это тест — café 你好 😀"
        XCTAssertEqual(VoicemailTranscriptDecoder.decode(FakeVMTranscript.archive(FakeVMTranscript(text: text))), text)
    }
}

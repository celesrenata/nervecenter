import Foundation

/// Extracts Apple's transcript text from `ZSTOREDMESSAGE.ZTRANSCRIPT`.
///
/// The column holds an `NSKeyedArchiver` plist whose root is a
/// `VMVoicemailTranscript` (a private class from VoicemailServices). The relay
/// does not link that framework: it maps the class name onto a local stub that
/// decodes exactly one key, `transcriptionString`. Every other object in the
/// archive (segments, ranges, confidences) is never requested, so the
/// unarchiver never needs those classes either.
///
/// Pure, bounded and fail-closed: a malformed, oversized, wrongly rooted or
/// empty archive returns `nil` and never throws or traps.
public enum VoicemailTranscriptDecoder {
    public static let maxBlobBytes = 1024 * 1024
    public static let maxTextBytes = 256 * 1024

    /// Class name Apple archives the root object under.
    static let rootClassName = "VMVoicemailTranscript"

    @objc(NCVoicemailTranscriptStub)
    private final class TranscriptStub: NSObject, NSCoding {
        let text: String?
        init?(coder: NSCoder) {
            text = coder.decodeObject(forKey: "transcriptionString") as? String
            super.init()
        }
        func encode(with coder: NSCoder) {}
    }

    public static func decode(_ data: Data) -> String? {
        guard !data.isEmpty, data.count <= maxBlobBytes else { return nil }
        do {
            let unarchiver = try NSKeyedUnarchiver(forReadingFrom: data)
            unarchiver.requiresSecureCoding = false
            unarchiver.decodingFailurePolicy = .setErrorAndReturn
            unarchiver.setClass(TranscriptStub.self, forClassName: rootClassName)
            defer { unarchiver.finishDecoding() }
            let root = unarchiver.decodeObject(forKey: NSKeyedArchiveRootObjectKey)
            guard unarchiver.error == nil, let stub = root as? TranscriptStub,
                  let text = stub.text else { return nil }
            guard text.utf8.count <= maxTextBytes else { return nil }
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            return trimmed.isEmpty ? nil : trimmed
        } catch {
            return nil
        }
    }
}

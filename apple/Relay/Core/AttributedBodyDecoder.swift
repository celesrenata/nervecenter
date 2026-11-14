import Foundation

/// Extracts the plain text from a Messages `message.attributedBody` blob.
///
/// Since macOS Ventura most message text lives only in `attributedBody`, a
/// "typedstream" archive of an `NSAttributedString`, and `message.text` is
/// NULL. The archive is not parsed in general: the relay reads exactly one
/// thing, the UTF-8 payload that follows the `NSString` class marker:
///
///     … "NSString" 01 (94|95) 84 01 2B <length> <utf-8 bytes> …
///
/// where `0x2B` ('+') is the typedstream encoding of a UTF-8 C string and the
/// length is one byte (< 0x80), or `0x81` + UInt16 little endian, or `0x82` +
/// UInt32 little endian.
///
/// Pure, bounded and fail-closed: any malformed, truncated or oversized blob
/// returns `nil` (the caller skips the row and counts it as unreadable). The
/// object-replacement character that stands for an inline attachment is
/// removed, so an attachment-only message decodes to `nil`, not to a body.
public enum AttributedBodyDecoder {
    /// Hard cap on a decoded body (the same order as a message text limit).
    public static let maxBodyBytes = 256 * 1024
    private static let marker = Array("NSString".utf8)
    /// How far past the class marker the `+` type byte may appear.
    private static let markerSearchWindow = 12

    public static func decode(_ data: Data) -> String? {
        let bytes = [UInt8](data)
        guard bytes.count > marker.count + 4, bytes.count <= 4 * 1024 * 1024 else { return nil }
        guard let markerEnd = indexAfter(marker, in: bytes) else { return nil }
        // The '+' (0x2B) type byte sits a few bytes after the class name.
        var p = markerEnd
        let limit = min(bytes.count, markerEnd + markerSearchWindow)
        while p < limit, bytes[p] != 0x2B { p += 1 }
        guard p < limit else { return nil }
        p += 1
        guard p < bytes.count else { return nil }
        let length: Int
        let start: Int
        switch bytes[p] {
        case 0x00..<0x80:
            length = Int(bytes[p])
            start = p + 1
        case 0x81:
            guard p + 2 < bytes.count else { return nil }
            length = Int(bytes[p + 1]) | Int(bytes[p + 2]) << 8
            start = p + 3
        case 0x82:
            guard p + 4 < bytes.count else { return nil }
            length = Int(bytes[p + 1]) | Int(bytes[p + 2]) << 8
                | Int(bytes[p + 3]) << 16 | Int(bytes[p + 4]) << 24
            start = p + 5
        default:
            return nil
        }
        guard length > 0, length <= maxBodyBytes, start + length <= bytes.count else { return nil }
        guard let text = String(bytes: bytes[start..<(start + length)], encoding: .utf8) else {
            return nil
        }
        let cleaned = text
            .replacingOccurrences(of: "\u{FFFC}", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? nil : cleaned
    }

    /// Index just past the first occurrence of `needle`, or nil.
    private static func indexAfter(_ needle: [UInt8], in haystack: [UInt8]) -> Int? {
        guard haystack.count >= needle.count else { return nil }
        let last = haystack.count - needle.count
        var i = 0
        while i <= last {
            if haystack[i] == needle[0] {
                var j = 1
                while j < needle.count, haystack[i + j] == needle[j] { j += 1 }
                if j == needle.count { return i + needle.count }
            }
            i += 1
        }
        return nil
    }
}

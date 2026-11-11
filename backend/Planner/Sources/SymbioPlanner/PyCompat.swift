import Foundation
import SymbioCore

/// Small Python-semantics helpers the planner rules rely on (re, str, pathlib, difflib).

/// A compiled Python `re` pattern on top of NSRegularExpression (ICU).
/// Patterns are written so ICU reads them like Python: `.` is spelled `[^\n]` where Python's
/// `.` would also match `\r`/U+2028, and `[`/`{` inside sets are escaped.
struct Pattern: @unchecked Sendable {  // NSRegularExpression is immutable and thread safe
    let regex: NSRegularExpression
    let pattern: String

    init(_ pattern: String, ignoreCase: Bool = false, dotAll: Bool = false) {
        var options: NSRegularExpression.Options = []
        if ignoreCase { options.insert(.caseInsensitive) }
        if dotAll { options.insert(.dotMatchesLineSeparators) }
        self.pattern = pattern
        // swiftlint:disable:next force_try
        regex = try! NSRegularExpression(pattern: pattern, options: options)
    }

    /// `re.search(...) is not None`
    func search(_ text: String) -> Bool { first(text) != nil }

    /// `re.search`
    func first(_ text: String) -> Match? {
        regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)).map { Match(text, $0) }
    }

    /// `re.finditer`
    func all(_ text: String) -> [Match] {
        regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).map { Match(text, $0) }
    }

    /// `re.match`: anchored at the start.
    func atStart(_ text: String) -> Bool {
        regex.firstMatch(in: text, options: .anchored, range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// `re.fullmatch(...) is not None`
    func fullMatch(_ text: String) -> Bool {
        let whole = NSRange(text.startIndex..., in: text)
        return regex.firstMatch(in: text, options: .anchored, range: whole)?.range == whole
    }

    /// `re.sub(pattern, replacement, text)` with a literal replacement.
    func sub(_ replacement: String, _ text: String) -> String {
        regex.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text),
            withTemplate: NSRegularExpression.escapedTemplate(for: replacement))
    }

    struct Match {
        let range: Range<String.Index>
        let value: String
        private let source: String
        private let result: NSTextCheckingResult

        init(_ source: String, _ result: NSTextCheckingResult) {
            self.source = source
            self.result = result
            range = Range(result.range, in: source)!
            value = String(source[range])
        }

        /// `match[name]`
        subscript(_ name: String) -> String? {
            Range(result.range(withName: name), in: source).map { String(source[$0]) }
        }
    }
}

/// `re.escape`
func escape(_ text: String) -> String { NSRegularExpression.escapedPattern(for: text) }

/// Python `str.isspace` characters (CharacterSet lacks U+001C...U+001F).
let pyWhitespace = CharacterSet.whitespacesAndNewlines.union(CharacterSet(charactersIn: "\u{1C}\u{1D}\u{1E}\u{1F}"))

/// `str.strip()`
func pyStrip(_ text: String) -> String { text.trimmingCharacters(in: pyWhitespace) }

/// `str.lstrip()`
func pyLStrip(_ text: String) -> String {
    String(String.UnicodeScalarView(text.unicodeScalars.drop { pyWhitespace.contains($0) }))
}

/// `text[:limit]`: Python slices count code points, not grapheme clusters.
func scalarPrefix(_ text: String, _ limit: Int) -> String {
    String(String.UnicodeScalarView(text.unicodeScalars.prefix(limit)))
}

/// `text[:-1]`
func dropLastScalar(_ text: String) -> String {
    String(String.UnicodeScalarView(text.unicodeScalars.dropLast()))
}

/// `str.splitlines()` with Python's line boundaries.
func splitLines(_ text: String) -> [String] {
    var lines: [String] = []
    var current = String.UnicodeScalarView()
    var previousCR = false
    for scalar in text.unicodeScalars {
        if previousCR, scalar == "\n" { previousCR = false; continue }
        previousCR = scalar == "\r"
        switch scalar.value {
        case 0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029:
            lines.append(String(current))
            current = String.UnicodeScalarView()
        default:
            current.append(scalar)
        }
    }
    if !current.isEmpty { lines.append(String(current)) }
    return lines
}

/// `unicodedata.normalize("NFC", text)` (Python `_nfc`).
func nfc(_ text: String) -> String { text.precomposedStringWithCanonicalMapping }

/// `f"{n:,}"`
func thousands(_ n: Int) -> String {
    let digits = String(abs(n))
    var out = ""
    for (index, char) in digits.enumerated() {
        if index > 0, (digits.count - index) % 3 == 0 { out += "," }
        out.append(char)
    }
    return n < 0 ? "-" + out : out
}

/// Python truthiness of a JSON value (`value or default`).
func truthy(_ value: JSON?) -> Bool {
    switch value {
    case nil, .null?: false
    case let .bool(b)?: b
    case let .int(i)?: i != 0
    case let .double(d)?: d != 0
    case let .string(s)?: !s.isEmpty
    case let .array(a)?: !a.isEmpty
    case let .object(o)?: !o.isEmpty
    }
}

// MARK: - pathlib

/// `str(Path(raw))`: lexical normalization that keeps a relative path relative.
func pyPath(_ raw: String) -> String {
    let parts = raw.split(separator: "/", omittingEmptySubsequences: true).filter { $0 != "." }
    if raw.hasPrefix("/") { return "/" + parts.joined(separator: "/") }
    return parts.isEmpty ? "." : parts.joined(separator: "/")
}

/// `Path(raw).name`
func pathName(_ raw: String) -> String {
    let normalized = pyPath(raw)
    return normalized == "." || normalized == "/" ? "" : String(normalized.split(separator: "/").last ?? "")
}

/// `str(Path(raw).parent)`
func pathParent(_ raw: String) -> String {
    let normalized = pyPath(raw)
    guard let slash = normalized.lastIndex(of: "/") else { return "." }
    return slash == normalized.startIndex ? "/" : String(normalized[..<slash])
}

/// `str(Path(folder) / name)`
func pathJoin(_ folder: String, _ name: String) -> String {
    folder.hasSuffix("/") ? folder + name : folder + "/" + name
}

/// `Path.exists()`: false for a missing path, throws for other errors (like ENAMETOOLONG).
func pathExists(_ path: String) throws -> Bool {
    var info = stat()
    if stat(path, &info) == 0 { return true }
    if [ENOENT, ENOTDIR, EBADF, ELOOP].contains(errno) { return false }
    throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
}

/// `Path.is_dir()`
func isDirectory(_ path: String) -> Bool {
    var info = stat()
    return stat(path, &info) == 0 && (info.st_mode & S_IFMT) == S_IFDIR
}

/// `os.listdir`: raw names as stored on disk (no Unicode normalization), nil on error.
func listDirectory(_ path: String) -> [String]? {
    guard let dir = opendir(path) else { return nil }
    defer { closedir(dir) }
    var names: [String] = []
    while let entry = readdir(dir) {
        let name = withUnsafePointer(to: entry.pointee.d_name) {
            $0.withMemoryRebound(to: CChar.self, capacity: Int(entry.pointee.d_namlen) + 1) { String(cString: $0) }
        }
        if name != ".", name != ".." { names.append(name) }
    }
    return names
}

// MARK: - difflib

/// `difflib.SequenceMatcher(None, a, b).ratio()` (autojunk on, like Python's default).
func sequenceRatio(_ a: [UInt32], _ b: [UInt32]) -> Double {
    guard a.count + b.count > 0 else { return 1.0 }
    var b2j: [UInt32: [Int]] = [:]
    for (j, element) in b.enumerated() { b2j[element, default: []].append(j) }
    if b.count >= 200 {
        let ntest = b.count / 100 + 1
        for (element, indices) in b2j where indices.count > ntest { b2j[element] = nil }
    }

    func longest(_ alo: Int, _ ahi: Int, _ blo: Int, _ bhi: Int) -> (Int, Int, Int) {
        var (besti, bestj, bestsize) = (alo, blo, 0)
        var j2len: [Int: Int] = [:]
        for i in alo..<ahi {
            var next: [Int: Int] = [:]
            for j in b2j[a[i]] ?? [] {
                if j < blo { continue }
                if j >= bhi { break }
                let k = (j2len[j - 1] ?? 0) + 1
                next[j] = k
                if k > bestsize { (besti, bestj, bestsize) = (i - k + 1, j - k + 1, k) }
            }
            j2len = next
        }
        while besti > alo, bestj > blo, a[besti - 1] == b[bestj - 1] {
            besti -= 1; bestj -= 1; bestsize += 1
        }
        while besti + bestsize < ahi, bestj + bestsize < bhi, a[besti + bestsize] == b[bestj + bestsize] {
            bestsize += 1
        }
        return (besti, bestj, bestsize)
    }

    var matched = 0
    var queue = [(0, a.count, 0, b.count)]
    while let (alo, ahi, blo, bhi) = queue.popLast() {
        let (i, j, k) = longest(alo, ahi, blo, bhi)
        guard k > 0 else { continue }
        matched += k
        if alo < i, blo < j { queue.append((alo, i, blo, j)) }
        if i + k < ahi, j + k < bhi { queue.append((i + k, ahi, j + k, bhi)) }
    }
    return 2.0 * Double(matched) / Double(a.count + b.count)
}

/// `difflib.get_close_matches(word, possibilities, n, cutoff)`
func closeMatches(_ word: String, _ possibilities: [String], n: Int = 3, cutoff: Double = 0.6) -> [String] {
    let b = word.unicodeScalars.map(\.value)
    let scored = possibilities.compactMap { x -> (Double, [UInt32], String)? in
        let a = x.unicodeScalars.map(\.value)
        let score = sequenceRatio(a, b)
        return score >= cutoff ? (score, a, x) : nil
    }
    // heapq.nlargest on (score, x): ties go to the larger string in code point order.
    return scored.sorted { $0.0 != $1.0 ? $0.0 > $1.0 : $1.1.lexicographicallyPrecedes($0.1) }
        .prefix(n).map(\.2)
}

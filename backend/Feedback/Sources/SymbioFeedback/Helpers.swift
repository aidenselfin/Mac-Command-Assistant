import CryptoKit
import Foundation
import SymbioCore
import SymbioUndo

/// Python `re` on top of NSRegularExpression (ICU). ponytail: copied from Planner's internal
/// PyCompat; share it from Core if a third package needs it.
struct Pattern: @unchecked Sendable {  // NSRegularExpression is immutable and thread safe
    let regex: NSRegularExpression

    init(_ pattern: String, ignoreCase: Bool = false) {
        // swiftlint:disable:next force_try
        regex = try! NSRegularExpression(pattern: pattern, options: ignoreCase ? [.caseInsensitive] : [])
    }

    /// `re.search(...) is not None`
    func search(_ text: String) -> Bool {
        regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
    }

    /// `re.sub(pattern, replacement, text)` with a literal replacement.
    func sub(_ replacement: String, _ text: String) -> String {
        regex.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text),
            withTemplate: NSRegularExpression.escapedTemplate(for: replacement))
    }
}

/// `text[:limit]` in code points, like Python.
func prefix(_ text: String, _ limit: Int) -> String {
    var scalars = String.UnicodeScalarView()
    scalars.append(contentsOf: text.unicodeScalars.prefix(limit))
    return String(scalars)
}

func length(_ text: String) -> Int { text.unicodeScalars.count }

func sha256Hex(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}

func pyStrip(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

/// `f"{value}"` for a JSON value read with `.get()`: None for missing or null.
func pyStr(_ value: JSON?) -> String {
    switch value {
    case nil, .null?: "None"
    case let .string(s)?: s
    case let other?: other.text
    }
}

/// `{**a, **b}` for JSON objects.
func merged(_ a: JSON, _ b: JSON) -> JSON {
    var result = a
    for (key, value) in b.objectValue ?? [] { result.set(key, value) }
    return result
}

func unique<T: Hashable>(_ items: [T]) -> [T] {
    var seen = Set<T>()
    return items.filter { seen.insert($0).inserted }
}

extension SQLiteValue {
    var double: Double? {
        switch self {
        case let .real(v): v
        case let .integer(v): Double(v)
        default: nil
        }
    }
    var int: Int? { intValue.map(Int.init) }
    var string: String? { stringValue }
}

/// `difflib.SequenceMatcher(None, a, b).ratio()` (autojunk on, like Python's default).
/// ponytail: copied from Planner's internal PyCompat.
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

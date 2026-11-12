import Foundation

/// Order-preserving JSON value. Python dicts keep insertion order and tests compare
/// `json.dumps` output, so Foundation's unordered JSONSerialization is not enough.
public enum JSON: Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSON])
    case object([(String, JSON)])

    public static func == (lhs: JSON, rhs: JSON) -> Bool {
        switch (lhs, rhs) {
        case (.null, .null): return true
        case let (.bool(a), .bool(b)): return a == b
        case let (.int(a), .int(b)): return a == b
        case let (.double(a), .double(b)): return a == b
        case let (.int(a), .double(b)), let (.double(b), .int(a)): return Double(a) == b
        case let (.string(a), .string(b)): return a == b
        case let (.array(a), .array(b)): return a == b
        // Python dict equality ignores order.
        case let (.object(a), .object(b)):
            return a.count == b.count && a.allSatisfy { key, value in b.first { $0.0 == key }?.1 == value }
        default: return false
        }
    }

    public subscript(key: String) -> JSON? {
        if case let .object(pairs) = self { return pairs.last { $0.0 == key }?.1 }
        return nil
    }

    public var stringValue: String? { if case let .string(s) = self { return s }; return nil }
    public var boolValue: Bool? { if case let .bool(b) = self { return b }; return nil }
    public var intValue: Int? { if case let .int(i) = self { return i }; return nil }
    public var doubleValue: Double? {
        switch self { case let .int(i): return Double(i); case let .double(d): return d; default: return nil }
    }
    public var arrayValue: [JSON]? { if case let .array(a) = self { return a }; return nil }
    public var objectValue: [(String, JSON)]? { if case let .object(o) = self { return o }; return nil }
    public var isNull: Bool { if case .null = self { return true }; return false }

    /// Insert or replace a key, keeping the original position like a Python dict.
    public mutating func set(_ key: String, _ value: JSON) {
        guard case var .object(pairs) = self else { return }
        if let index = pairs.firstIndex(where: { $0.0 == key }) { pairs[index].1 = value } else {
            pairs.append((key, value))
        }
        self = .object(pairs)
    }
}

extension JSON: ExpressibleByNilLiteral, ExpressibleByBooleanLiteral, ExpressibleByIntegerLiteral,
    ExpressibleByFloatLiteral, ExpressibleByStringLiteral, ExpressibleByArrayLiteral,
    ExpressibleByDictionaryLiteral
{
    public init(nilLiteral: ()) { self = .null }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .int(value) }
    public init(floatLiteral value: Double) { self = .double(value) }
    public init(stringLiteral value: String) { self = .string(value) }
    public init(arrayLiteral elements: JSON...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, JSON)...) { self = .object(elements) }
}

// MARK: - Encoding (Python json.dumps compatible)

public struct JSONDumpOptions: Sendable {
    public var ensureASCII = true
    public var sortKeys = false
    public var itemSeparator = ", "
    public var keySeparator = ": "
    public var indent: Int? = nil
    public init(ensureASCII: Bool = true, sortKeys: Bool = false, compact: Bool = false, indent: Int? = nil) {
        self.ensureASCII = ensureASCII
        self.sortKeys = sortKeys
        self.indent = indent
        if compact { itemSeparator = ","; keySeparator = ":" }
        // Python: indent switches the default item separator to "," (newline follows).
        if indent != nil, !compact { itemSeparator = "," }
    }
}

extension JSON {
    /// `json.dumps(value)` with the same defaults as Python (ensure_ascii=True, ", ", ": ").
    public func dumps(_ options: JSONDumpOptions = JSONDumpOptions()) -> String {
        var out = ""
        write(into: &out, options, level: 0)
        return out
    }

    /// Shorthand for `json.dumps(value, ensure_ascii=False)`, the most common form in Symbio.
    public var text: String { dumps(JSONDumpOptions(ensureASCII: false)) }

    private func write(into out: inout String, _ o: JSONDumpOptions, level: Int) {
        switch self {
        case .null: out += "null"
        case let .bool(b): out += b ? "true" : "false"
        case let .int(i): out += String(i)
        case let .double(d): out += JSON.pythonFloat(d)
        case let .string(s): JSON.quote(s, ascii: o.ensureASCII, into: &out)
        case let .array(items):
            if items.isEmpty { out += "[]"; return }
            out += "["
            for (index, item) in items.enumerated() {
                if index > 0 { out += o.itemSeparator }
                JSON.newline(&out, o, level + 1)
                item.write(into: &out, o, level: level + 1)
            }
            JSON.newline(&out, o, level)
            out += "]"
        case let .object(pairs):
            if pairs.isEmpty { out += "{}"; return }
            let ordered = o.sortKeys ? pairs.sorted { $0.0 < $1.0 } : pairs
            out += "{"
            for (index, (key, value)) in ordered.enumerated() {
                if index > 0 { out += o.itemSeparator }
                JSON.newline(&out, o, level + 1)
                JSON.quote(key, ascii: o.ensureASCII, into: &out)
                out += o.keySeparator
                value.write(into: &out, o, level: level + 1)
            }
            JSON.newline(&out, o, level)
            out += "}"
        }
    }

    private static func newline(_ out: inout String, _ o: JSONDumpOptions, _ level: Int) {
        if let indent = o.indent { out += "\n" + String(repeating: " ", count: indent * level) }
    }

    static func pythonFloat(_ d: Double) -> String {
        if d.isNaN { return "NaN" }
        if d.isInfinite { return d > 0 ? "Infinity" : "-Infinity" }
        if d == d.rounded(), abs(d) < 1e16 { return String(format: "%.1f", d) }
        return "\(d)"  // Swift's shortest round-trip repr matches Python repr for normal values.
    }

    static func quote(_ s: String, ascii: Bool, into out: inout String) {
        out += "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            case "\u{08}": out += "\\b"
            case "\u{0C}": out += "\\f"
            default:
                if scalar.value < 0x20 || (ascii && scalar.value > 0x7E) {
                    if scalar.value > 0xFFFF {
                        for unit in String(scalar).utf16 { out += String(format: "\\u%04x", unit) }
                    } else {
                        out += String(format: "\\u%04x", scalar.value)
                    }
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        out += "\""
    }
}

// MARK: - Parsing (order preserving)

public struct JSONParseError: Error, Equatable, Sendable { public let offset: Int }

extension JSON {
    public static func parse(_ text: String) throws -> JSON {
        var parser = Parser(bytes: Array(text.utf8))
        parser.skip()
        let value = try parser.value()
        parser.skip()
        guard parser.at == parser.bytes.count else { throw JSONParseError(offset: parser.at) }
        return value
    }

    public static func parse(_ data: Data) throws -> JSON {
        guard let text = String(data: data, encoding: .utf8) else { throw JSONParseError(offset: 0) }
        return try parse(text)
    }

    private struct Parser {
        let bytes: [UInt8]
        var at = 0

        mutating func skip() {
            while at < bytes.count, [0x20, 0x09, 0x0A, 0x0D].contains(bytes[at]) { at += 1 }
        }

        mutating func expect(_ literal: String) throws {
            let utf8 = Array(literal.utf8)
            guard at + utf8.count <= bytes.count, Array(bytes[at..<at + utf8.count]) == utf8 else {
                throw JSONParseError(offset: at)
            }
            at += utf8.count
        }

        mutating func value() throws -> JSON {
            guard at < bytes.count else { throw JSONParseError(offset: at) }
            switch bytes[at] {
            case UInt8(ascii: "n"): try expect("null"); return .null
            case UInt8(ascii: "t"): try expect("true"); return .bool(true)
            case UInt8(ascii: "f"): try expect("false"); return .bool(false)
            case UInt8(ascii: "\""): return .string(try string())
            case UInt8(ascii: "["):
                at += 1; skip()
                var items: [JSON] = []
                if at < bytes.count, bytes[at] == UInt8(ascii: "]") { at += 1; return .array(items) }
                while true {
                    skip(); items.append(try value()); skip()
                    guard at < bytes.count else { throw JSONParseError(offset: at) }
                    if bytes[at] == UInt8(ascii: ",") { at += 1; continue }
                    if bytes[at] == UInt8(ascii: "]") { at += 1; return .array(items) }
                    throw JSONParseError(offset: at)
                }
            case UInt8(ascii: "{"):
                at += 1; skip()
                var pairs: [(String, JSON)] = []
                if at < bytes.count, bytes[at] == UInt8(ascii: "}") { at += 1; return .object(pairs) }
                while true {
                    skip()
                    guard at < bytes.count, bytes[at] == UInt8(ascii: "\"") else { throw JSONParseError(offset: at) }
                    let key = try string()
                    skip(); try expect(":"); skip()
                    let item = try value()
                    // Python keeps the last duplicate at the first position.
                    if let index = pairs.firstIndex(where: { $0.0 == key }) { pairs[index].1 = item } else {
                        pairs.append((key, item))
                    }
                    skip()
                    guard at < bytes.count else { throw JSONParseError(offset: at) }
                    if bytes[at] == UInt8(ascii: ",") { at += 1; continue }
                    if bytes[at] == UInt8(ascii: "}") { at += 1; return .object(pairs) }
                    throw JSONParseError(offset: at)
                }
            default: return try number()
            }
        }

        mutating func number() throws -> JSON {
            let start = at
            var isFloat = false
            while at < bytes.count {
                let c = bytes[at]
                if (0x30...0x39).contains(c) || c == UInt8(ascii: "-") || c == UInt8(ascii: "+") { at += 1 }
                else if c == UInt8(ascii: ".") || c == UInt8(ascii: "e") || c == UInt8(ascii: "E") { isFloat = true; at += 1 }
                else { break }
            }
            let raw = String(decoding: bytes[start..<at], as: UTF8.self)
            if !isFloat, let i = Int(raw) { return .int(i) }
            guard !raw.isEmpty, let d = Double(raw) else { throw JSONParseError(offset: start) }
            return .double(d)
        }

        mutating func hex4() throws -> UInt32 {
            guard at + 4 <= bytes.count, let v = UInt32(String(decoding: bytes[at..<at + 4], as: UTF8.self), radix: 16) else {
                throw JSONParseError(offset: at)
            }
            at += 4
            return v
        }

        mutating func string() throws -> String {
            at += 1
            var buffer: [UInt8] = []
            while at < bytes.count {
                let c = bytes[at]
                if c == UInt8(ascii: "\"") { at += 1; return String(decoding: buffer, as: UTF8.self) }
                if c < 0x20 { throw JSONParseError(offset: at) }
                if c != UInt8(ascii: "\\") { buffer.append(c); at += 1; continue }
                at += 1
                guard at < bytes.count else { break }
                let e = bytes[at]; at += 1
                switch e {
                case UInt8(ascii: "\""): buffer.append(0x22)
                case UInt8(ascii: "\\"): buffer.append(0x5C)
                case UInt8(ascii: "/"): buffer.append(0x2F)
                case UInt8(ascii: "b"): buffer.append(0x08)
                case UInt8(ascii: "f"): buffer.append(0x0C)
                case UInt8(ascii: "n"): buffer.append(0x0A)
                case UInt8(ascii: "r"): buffer.append(0x0D)
                case UInt8(ascii: "t"): buffer.append(0x09)
                case UInt8(ascii: "u"):
                    var code = try hex4()
                    if (0xD800...0xDBFF).contains(code), at + 6 <= bytes.count,
                       bytes[at] == UInt8(ascii: "\\"), bytes[at + 1] == UInt8(ascii: "u") {
                        let save = at
                        at += 2
                        let low = try hex4()
                        if (0xDC00...0xDFFF).contains(low) { code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00) } else { at = save }
                    }
                    let scalar = Unicode.Scalar(code) ?? "\u{FFFD}"
                    buffer.append(contentsOf: Array(String(scalar).utf8))
                default: throw JSONParseError(offset: at - 1)
                }
            }
            throw JSONParseError(offset: at)
        }
    }
}

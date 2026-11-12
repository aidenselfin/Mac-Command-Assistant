import CryptoKit
import Foundation
import SymbioCore

/// Small Python stand-ins shared by eval, benchmark, make-cases and check.

/// The project the command works on: the current folder when it is a Symbio checkout (a worktree
/// the feedback worker runs in), else the repository this tool was built from.
public func projectRoot() -> URL {
    let here = FileManager.default.currentDirectoryPath
    let isCheckout = isDirectory(here + "/evals/tasks") && isDirectory(here + "/backend")
    return URL(fileURLWithPath: isCheckout ? here : repoRoot, isDirectory: true)
}

func sha(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
func sha(_ text: String) -> String { sha(Data(text.utf8)) }

/// `sha(json.dumps(task, sort_keys=True))[:12]`: the task's version in results and records.
func taskDigest(_ task: JSON) -> String { String(sha(task.dumps(JSONDumpOptions(sortKeys: true))).prefix(12)) }

/// `[json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]`.
func loadJSONFiles(_ folder: URL) -> [JSON] {
    let names = ((try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? [])
        .filter { $0.hasSuffix(".json") }.sorted()
    return names.compactMap { try? JSON.parse(Data(contentsOf: folder.appendingPathComponent($0))) }
}

func writeJSON(_ value: JSON, _ url: URL) throws {
    try Data(value.dumps(JSONDumpOptions(ensureASCII: false, indent: 2)).utf8).write(to: url)
}

/// Python `repr(str)`.
func pyRepr(_ text: String) -> String {
    let quote = text.contains("'") && !text.contains("\"") ? "\"" : "'"
    var body = text.replacingOccurrences(of: "\\", with: "\\\\")
    if quote == "'" { body = body.replacingOccurrences(of: "'", with: "\\'") }
    return quote + body + quote
}

/// Python `repr(list[str])`.
func pyRepr(_ items: [String]) -> String { "[" + items.map(pyRepr).joined(separator: ", ") + "]" }

/// f"{text:N}": pad on the right to N characters.
func pad(_ text: String, _ width: Int) -> String {
    text.count >= width ? text : text + String(repeating: " ", count: width - text.count)
}

/// `time.strftime(format)` in local time.
func timestamp(_ format: String = "yyyyMMdd-HHmmss") -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = format
    return formatter.string(from: Date())
}

/// Text after `--flag` options: `--name value` (repeatable) and bare `--switch`.
struct Arguments {
    var values: [String: [String]] = [:]
    var switches: Set<String> = []

    init(_ argv: [String], switches known: Set<String>) throws {
        var rest = argv[...]
        while let flag = rest.popFirst() {
            guard flag.hasPrefix("--") else { throw UsageError(message: "알 수 없는 인자: \(flag)") }
            if known.contains(flag) {
                switches.insert(flag)
            } else if let value = rest.popFirst() {
                values[flag, default: []].append(value)
            } else {
                throw UsageError(message: "\(flag) 값이 없습니다.")
            }
        }
    }

    func last(_ flag: String) -> String? { values[flag]?.last }
    func all(_ flag: String) -> [String] { values[flag] ?? [] }
}

struct UsageError: Error { let message: String }

extension JSON {
    var str: String { stringValue ?? "" }
    var list: [JSON] { arrayValue ?? [] }
    var keys: [String] { (objectValue ?? []).map(\.0) }
    func has(_ key: String) -> Bool { objectValue?.contains { $0.0 == key } ?? false }
    /// Python truthiness of a dict value (`t.get(key)`).
    var truthy: Bool {
        switch self {
        case .null: false
        case let .bool(b): b
        case let .int(i): i != 0
        case let .double(d): d != 0
        case let .string(s): !s.isEmpty
        case let .array(a): !a.isEmpty
        case let .object(o): !o.isEmpty
        }
    }
}

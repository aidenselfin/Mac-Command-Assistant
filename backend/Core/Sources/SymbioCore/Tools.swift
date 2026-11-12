import Foundation

/// Shared contract for every Symbio module (Python: mcp_client.py types, conversion._inside).

public let truncationMarker = "\n[TRUNCATED]"

/// Python `MCPError` hierarchy. `rejected`/`notFound` are `ToolRejectedError` subclasses.
public enum ToolError: Error, Equatable, Sendable, CustomStringConvertible {
    case rejected(String)
    case notFound(String)
    case startup(String)
    case connection(String)
    case timeout(String)
    case other(String)

    public var message: String {
        switch self {
        case let .rejected(m), let .notFound(m), let .startup(m), let .connection(m), let .timeout(m), let .other(m): m
        }
    }
    public var description: String { message }
    /// True for Python `ToolRejectedError` and its subclasses.
    public var isRejected: Bool {
        switch self { case .rejected, .notFound: true; default: false }
    }
}

public struct ToolDefinition: Equatable, Sendable {
    public let name: String
    public let description: String
    public let schema: JSON
    public init(name: String, description: String, schema: JSON) {
        self.name = name
        self.description = description
        self.schema = schema
    }
}

public struct ToolResult: Equatable, Sendable {
    public var ok: Bool
    public var text: String
    public var structured: JSON?
    public var error: String?
    public var truncated: Bool
    public init(ok: Bool, text: String, structured: JSON? = nil, error: String? = nil, truncated: Bool = false) {
        self.ok = ok
        self.text = text
        self.structured = structured
        self.error = error
        self.truncated = truncated
    }
}

/// Python `_bounded_join`: join without building an unbounded copy (limit counted in characters).
public func boundedJoin<S: Sequence>(_ parts: S, limit: Int) -> (String, Bool) where S.Element == String {
    var out = ""
    var remaining = limit
    for part in parts {
        let count = part.count
        out += part.prefix(remaining)
        if count > remaining { return (out, true) }
        remaining -= count
    }
    return (out, false)
}

/// Python `Draft202012Validator(definition.schema).validate(arguments)` for the tool schemas Symbio uses.
public func validateArguments(_ definition: ToolDefinition, _ arguments: JSON) throws(ToolError) {
    if !JSONSchema.isValid(arguments, schema: definition.schema) {
        throw .rejected("잘못된 \(definition.name) 인자입니다.")
    }
}

/// Python `conversion._inside`: absolute, canonical path under an allowed root, never via a symlink.
public func inside(_ raw: JSON?, roots: [URL], exists: Bool) throws(ToolError) -> URL {
    guard let raw = raw?.stringValue, !raw.isEmpty, raw == raw.trimmingCharacters(in: .whitespacesAndNewlines) else {
        throw .rejected("파일 경로가 올바르지 않습니다.")
    }
    let escapesMessage = "허용 폴더 밖이거나 링크를 거치는 경로입니다."
    guard raw.hasPrefix("/"), roots.contains(where: { isRelative(raw, to: $0.path) }) else {
        throw .rejected(escapesMessage)
    }
    let path = URL(fileURLWithPath: raw)
    var info = stat()
    let isLink = lstat(raw, &info) == 0 && (info.st_mode & S_IFMT) == S_IFLNK
    let target = exists ? raw : (raw as NSString).deletingLastPathComponent
    guard let resolved = realPath(target) else { throw .rejected("파일 경로를 확인할 수 없습니다.") }
    if isLink || resolved != normalized(target) { throw .rejected(escapesMessage) }
    return path
}

/// Python `Path.is_relative_to` on already-absolute string paths.
public func isRelative(_ path: String, to root: String) -> Bool {
    let p = normalized(path), r = normalized(root)
    return p == r || p.hasPrefix(r == "/" ? "/" : r + "/")
}

/// Lexical normalization like `pathlib` (drops trailing "/" and "." parts, keeps "..").
public func normalized(_ path: String) -> String {
    let parts = path.split(separator: "/", omittingEmptySubsequences: true).filter { $0 != "." }
    return "/" + parts.joined(separator: "/")
}

/// Python `Path.resolve(strict=True)`: nil when the path does not exist.
public func realPath(_ path: String) -> String? {
    guard let pointer = realpath(path, nil) else { return nil }
    defer { free(pointer) }
    return String(cString: pointer)
}

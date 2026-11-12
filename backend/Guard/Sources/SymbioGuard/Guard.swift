import AppKit
import Foundation
import SymbioConvert
import SymbioCore
import SymbioDelete
import SymbioFileOps
import SymbioFinder
import SymbioMCP
import SymbioSearch
import SymbioUndo

/// Fixed application risk policy and one-shot filesystem write approvals (Python `risk_guard.py`).

public let readOnlyTools: Set<String> = [
    "filesystem__list_directory",
    "filesystem__search_files",
    "filesystem__read_text_file",
    "web__browser_navigate",
    "web__browser_snapshot",
    "macos__get_active_app",
    readDocumentTool,
    findFilesTool,
    listFilesTool,
    groupFilesTool,
    openFileTool,  // changes nothing; openFile refuses programs
]
public let writeTools: Set<String> = Set(["filesystem__write_file", deleteTool, convertTool])
    .union(fileOperations.keys)
public let previewLimit = 200
/// A character is up to a few UTF-8 bytes; files past this many bytes per allowed character are
/// read from their start only.
public let readBytesFactor = 4

public enum ToolRisk: String, Sendable {
    case readOnly = "read-only"
    case write = "write"
    case forbidden = "forbidden"
}

/// Every guard error (Python `RiskGuardError`, a `ToolRejectedError`). Catch this to catch all four.
public protocol RiskGuardFailure: Error, Sendable, CustomStringConvertible {
    var message: String { get }
}
extension RiskGuardFailure { public var description: String { message } }

public struct RiskGuardError: RiskGuardFailure {
    public let message: String
    public init(_ message: String) { self.message = message }
}

public struct ForbiddenToolError: RiskGuardFailure {
    public let message: String
    public init(_ message: String) { self.message = message }
}

public struct ApprovalInvalidError: RiskGuardFailure {
    public let message: String
    public init(_ message: String) { self.message = message }
}

public struct ApprovalChangedError: RiskGuardFailure {
    public let message = "쓰기 대상 또는 상태가 변경되어 재승인이 필요합니다."
    /// A fresh approval, already pending under its own nonce.
    public let prepared: PreparedWrite
}

/// Python `PreparedWrite.snapshots`: the state frozen for a delete or a path operation.
public enum WriteSnapshot: Equatable, Sendable {
    case none
    case deletion([DeleteSnapshot])
    case operation(OperationSnapshot)
}

/// One write waiting for approval. `description` never shows content, payload, digest or nonce.
public struct PreparedWrite: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let toolName: String
    public let targetPath: String
    public let operation: String
    public let contentChars: Int
    public let preview: String
    public let previewTruncated: Bool
    public let canonicalArguments: String
    public let digest: String
    public let nonce: String
    public var targets: [String] = []
    public var snapshots: WriteSnapshot = .none
    public var fileState: String = ""
    public var payload = Data()  // a conversion's output, made before approval
    /// Per-target content preview for the user's approval screen only (never sent to the model).
    public var previews: [String] = []

    public var description: String {
        "PreparedWrite(toolName: \(toolName), targetPath: \(targetPath), operation: \(operation), "
            + "contentChars: \(contentChars), previewTruncated: \(previewTruncated), targets: \(targets))"
    }
    public var debugDescription: String { description }
}

public func classifyTool(_ name: String) -> ToolRisk {
    if readOnlyTools.contains(name) { return .readOnly }
    if writeTools.contains(name) { return .write }
    return .forbidden
}

/// `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.
func canonicalJSON(_ value: JSON) -> String {
    value.dumps(JSONDumpOptions(ensureASCII: false, sortKeys: true, compact: true))
}

func digest(_ toolName: String, _ canonicalArguments: String, _ fileState: String = "") -> String {
    fingerprint(Data("\(toolName)\n\(canonicalArguments)\n\(fileState)".utf8))
}

/// Python `secrets.token_urlsafe(32)`.
func newNonce() -> String {
    var generator = SystemRandomNumberGenerator()  // arc4random: cryptographically secure on Darwin
    let bytes = Data((0..<32).map { _ in UInt8.random(in: .min ... .max, using: &generator) })
    return bytes.base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}

/// Identity and content hash of the file an overwrite approval was shown for.
///
/// Bound into the approval digest, so an edit, a same-path replacement or a symlink swap
/// while the approval is pending forces a new approval. Never the bytes themselves.
public func fileState(_ path: String) -> String {
    guard let (data, metadata) = try? readRegularFile(path) else { return "unreadable" }
    return "\(metadata.st_dev):\(metadata.st_ino):\(fingerprint(data))"
}

private func isFile(_ path: String) -> Bool {  // Path.is_file(): follows links
    var metadata = stat()
    return stat(path, &metadata) == 0 && (metadata.st_mode & S_IFMT) == S_IFREG
}

private func isDirectory(_ path: String) -> Bool {
    var metadata = stat()
    return stat(path, &metadata) == 0 && (metadata.st_mode & S_IFMT) == S_IFDIR
}

/// OS, SQLite and journal errors become a `RiskGuardError`; refusals (`ToolError`) pass through.
private func guarded<T>(_ message: String, _ body: () throws -> T) throws -> T {
    do {
        return try body()
    } catch let error as ToolError {
        throw error
    } catch let error as any RiskGuardFailure {
        throw error
    } catch {
        throw RiskGuardError(message)
    }
}

/// Permit fixed read tools and bind each write to one in-memory approval.
// ponytail: journal and file work runs inline on the calling task. A cancel cannot stop it
// midway, so the journal is always settled before control returns (Python shields threads).
public final class RiskGuard: @unchecked Sendable {  // `pending` is behind `lock`; the rest is immutable
    private let gateway: any ToolGateway
    private let undoJournal: UndoJournal?
    private let roots: [URL]
    private let readLimit: Int
    private let workspace: any Workspace
    private let lock = NSLock()
    private var pending: [String: String] = [:]

    public init(
        _ gateway: any ToolGateway, _ undoJournal: UndoJournal? = nil, roots: [URL] = [],
        readLimit: Int = 50000, workspace: any Workspace = NSWorkspace.shared
    ) {
        self.gateway = gateway
        self.undoJournal = undoJournal
        self.roots = roots.map { URL(fileURLWithPath: realPath($0.path) ?? normalized($0.absoluteURL.path)) }
        self.readLimit = readLimit
        self.workspace = workspace
    }

    private func register(_ nonce: String, _ digest: String) { lock.withLock { pending[nonce] = digest } }
    private func take(_ nonce: String) -> String? { lock.withLock { pending.removeValue(forKey: nonce) } }

    public func executeReadOnly(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        switch classifyTool(name) {
        case .write: throw RiskGuardError("파일 쓰기는 호출별 승인이 필요합니다.")
        case .forbidden: throw ForbiddenToolError("금지된 도구입니다: \(name)")
        case .readOnly: break
        }
        switch name {
        case readDocumentTool: return try readDocument(arguments, roots: roots, limit: readLimit)
        case listFilesTool: return try listFiles(arguments, roots: roots)
        case groupFilesTool: return try groupFiles(arguments, roots: roots)
        case openFileTool: return try openFile(arguments, roots: roots, workspace: workspace)
        case findFilesTool: return try findFiles(arguments, roots: roots)
        default: break
        }
        if name == "filesystem__read_text_file", !truthy(arguments["head"]), !truthy(arguments["tail"]),
           let bounded = readBounded(arguments) {
            // The filesystem server reads a whole file before the result is cut, so a huge
            // file is read here, up to a byte bound, instead of being sent through it.
            return bounded
        }
        return try await gateway.callTool(name, arguments)
    }

    private func truthy(_ value: JSON?) -> Bool {
        switch value {
        case nil, .null?, .bool(false)?, .int(0)?, .double(0)?, .string("")?: false
        case let .array(items)?: !items.isEmpty
        case let .object(pairs)?: !pairs.isEmpty
        default: true
        }
    }

    /// The start of a file much bigger than the result limit; nil for any other file.
    private func readBounded(_ arguments: JSON) -> ToolResult? {
        // The filesystem server reports any problem in its own words.
        guard let path = try? inside(arguments["path"], roots: roots, exists: true) else { return nil }
        let descriptor = open(path.path, O_RDONLY | O_NOFOLLOW)
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }
        let bound = readBytesFactor * readLimit
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFREG,
              Int(metadata.st_size) > bound else { return nil }
        var data = [UInt8](repeating: 0, count: bound)
        var count = 0
        while count < bound {
            let got = data.withUnsafeMutableBytes { read(descriptor, $0.baseAddress! + count, bound - count) }
            if got <= 0 { break }
            count += got
        }
        let text = String(decoding: data[..<count], as: UTF8.self)
        let shown = String(String.UnicodeScalarView(text.unicodeScalars.prefix(readLimit)))
        return ToolResult(ok: true, text: shown + truncationMarker, truncated: true)
    }

    public func prepareWrite(_ name: String, _ arguments: JSON) async throws -> PreparedWrite {
        switch classifyTool(name) {
        case .forbidden: throw ForbiddenToolError("금지된 도구입니다: \(name)")
        case .readOnly: throw RiskGuardError("쓰기 승인은 write 도구에만 사용할 수 있습니다.")
        case .write: break
        }
        if name == deleteTool { return try prepareDelete(arguments) }
        if name == convertTool { return try prepareConvert(arguments) }
        if fileOperations[name] != nil { return try prepareOperation(name, arguments) }
        let normalized = try await gateway.normalizeToolArguments(name, arguments)
        return try newPrepared(name, normalized)
    }

    private func requireJournal(_ missing: String) throws -> UndoJournal {
        guard let undoJournal else { throw RiskGuardError(missing) }
        return undoJournal
    }

    /// Python's UndoJournal always owns both sub-journals; Swift attaches them on first use.
    private func operations(_ journal: UndoJournal) throws -> FileOperationJournal {
        try journal.operations as? FileOperationJournal ?? FileOperationJournal(journal)
    }

    private func deletions(_ journal: UndoJournal) throws -> DeletionJournal {
        try journal.deletions as? DeletionJournal ?? DeletionJournal(journal)
    }

    private func prepareOperation(_ name: String, _ arguments: JSON) throws -> PreparedWrite {
        let journal = try requireJournal("Undo journal이 없어 파일 이동·복사·폴더 생성을 실행할 수 없습니다.")
        let (normalized, snapshot) = try guarded("파일 작업 경로·권한·journal을 준비할 수 없습니다.") {
            try operations(journal).prepare(name, arguments)
        }
        let canonical = canonicalJSON(normalized)
        let hash = digest(name, canonical + canonicalJSON(["state": snapshot.json]))
        let nonce = newNonce()
        register(nonce, hash)
        return PreparedWrite(
            toolName: name, targetPath: (normalized["destination"] ?? normalized["path"])?.stringValue ?? "",
            operation: fileOperations[name]!, contentChars: 0, preview: "", previewTruncated: false,
            canonicalArguments: canonical, digest: hash, nonce: nonce,
            targets: normalized["source"]?.stringValue.map { [$0] } ?? [], snapshots: .operation(snapshot)
        )
    }

    private func prepareDelete(_ arguments: JSON) throws -> PreparedWrite {
        let journal = try requireJournal("Undo journal이 없어 파일을 삭제할 수 없습니다.")
        let (normalized, snapshots, previews) = try guarded("삭제 대상/백업을 준비할 수 없습니다.") {
            try deletions(journal).prepare(arguments)
        }
        let canonical = canonicalJSON(normalized)
        let hash = digest(deleteTool, canonical + canonicalJSON(["files": .array(snapshots.map(\.json))]))
        let nonce = newNonce()
        register(nonce, hash)
        let targets = snapshots.map(\.path)
        return PreparedWrite(
            toolName: deleteTool, targetPath: targets.count == 1 ? targets[0] : "\(targets.count)개 파일",
            operation: "delete", contentChars: 0, preview: "", previewTruncated: false,
            canonicalArguments: canonical, digest: hash, nonce: nonce,
            targets: targets, snapshots: .deletion(snapshots), previews: previews
        )
    }

    /// Convert into memory now, so the approval shows the real result.
    private func prepareConvert(_ arguments: JSON) throws -> PreparedWrite {
        if roots.isEmpty { throw RiskGuardError("허용 폴더가 없어 파일을 변환할 수 없습니다.") }
        let result = try prepareConversion(arguments, roots: roots)
        let canonical = canonicalJSON(arguments)
        let hash = digest(convertTool, canonical + result.output, result.sourceState + fingerprint(result.data))
        let nonce = newNonce()
        register(nonce, hash)
        return PreparedWrite(
            toolName: convertTool, targetPath: result.output, operation: "convert",
            contentChars: result.data.count, preview: result.preview, previewTruncated: result.previewTruncated,
            canonicalArguments: canonical, digest: hash, nonce: nonce,
            targets: [result.source], fileState: result.sourceState, payload: result.data
        )
    }

    private func executeConvert(
        _ prepared: PreparedWrite, _ expected: String,
        _ onStarted: (@Sendable () -> Void)?, _ onFinished: (@Sendable () -> Void)?
    ) throws -> ToolResult {
        let changed = changedSince(source: prepared.targets[0], state: prepared.fileState,
                                   output: prepared.targetPath, roots: roots)
        if expected != prepared.digest || changed {
            // The source changed or the output name got taken: convert again, ask again.
            throw ApprovalChangedError(prepared: try prepareConvert(try JSON.parse(prepared.canonicalArguments)))
        }
        let journal = try requireJournal("Undo journal이 없어 변환 파일을 만들지 않았습니다.")
        let entry: JournalEntry
        do {
            entry = try journal.prepareWrite(prepared.targetPath, operation: "create", content: prepared.payload)
        } catch {
            throw RiskGuardError("Undo journal을 준비하지 못해 변환 파일을 만들지 않았습니다.")
        }
        onStarted?()
        defer { onFinished?() }
        do {
            try writeNewFile(prepared.targetPath, data: prepared.payload, roots: roots)
        } catch {
            try? journal.reconcileUncertainWrite(entry)
            throw error
        }
        return ToolResult(ok: true, text: "변환 완료: \(prepared.targetPath) (\(prepared.payload.count) bytes)")
    }

    private func newPrepared(_ name: String, _ normalized: JSON) throws -> PreparedWrite {
        guard let path = normalized["path"]?.stringValue, path.hasPrefix("/") else {
            throw RiskGuardError("정규화된 write 경로가 절대경로가 아닙니다.")
        }
        guard let content = normalized["content"]?.stringValue else {
            throw RiskGuardError("write_file content는 문자열이어야 합니다.")
        }
        var state = "", metadata = stat()
        let operation: String
        if stat(path, &metadata) == 0 {  // Path.exists(): follows links
            state = fileState(path)
            if !isFile(path) || state == "unreadable" { throw RiskGuardError("쓰기 대상은 regular file이어야 합니다.") }
            operation = "overwrite"
        } else {
            if !isDirectory((path as NSString).deletingLastPathComponent) {
                throw RiskGuardError("쓰기 대상의 부모 디렉터리가 존재하지 않습니다.")
            }
            operation = "create"
        }
        let canonical = canonicalJSON(normalized)
        let hash = digest(name, canonical, state)
        let nonce = newNonce()
        register(nonce, hash)
        return Self.textWrite(name, path, operation, content, canonical, hash, nonce, state)
    }

    private static func textWrite(
        _ name: String, _ path: String, _ operation: String, _ content: String,
        _ canonical: String, _ digest: String, _ nonce: String, _ state: String
    ) -> PreparedWrite {
        let scalars = content.unicodeScalars
        return PreparedWrite(
            toolName: name, targetPath: path, operation: operation, contentChars: scalars.count,
            preview: String(String.UnicodeScalarView(scalars.prefix(previewLimit))),
            previewTruncated: scalars.count > previewLimit, canonicalArguments: canonical,
            digest: digest, nonce: nonce, fileState: state
        )
    }

    /// Run an approved write once. The nonce is spent first, then the target is checked again.
    public func executeApproved(
        _ prepared: PreparedWrite,
        onStarted: (@Sendable () -> Void)? = nil,
        onFinished: (@Sendable () -> Void)? = nil
    ) async throws -> ToolResult {
        guard let expected = take(prepared.nonce) else {
            throw ApprovalInvalidError("승인이 없거나 이미 사용되었습니다.")
        }
        if prepared.toolName == convertTool {
            return try executeConvert(prepared, expected, onStarted, onFinished)
        }
        let arguments = try JSON.parse(prepared.canonicalArguments)
        if fileOperations[prepared.toolName] != nil {
            let refreshed = try prepareOperation(prepared.toolName, arguments)
            _ = take(refreshed.nonce)
            guard expected == prepared.digest, refreshed.digest == prepared.digest,
                  case let .operation(snapshot) = refreshed.snapshots else {
                register(refreshed.nonce, refreshed.digest)
                throw ApprovalChangedError(prepared: refreshed)
            }
            onStarted?()
            defer { onFinished?() }
            return try guarded("파일 작업 중 오류가 발생했습니다. 원본·대상 경로를 확인하고 Undo하세요.") {
                try operations(undoJournal!).execute(prepared.toolName, arguments, snapshot)
            }
        }
        if prepared.toolName == deleteTool {
            let refreshed = try prepareDelete(arguments)
            _ = take(refreshed.nonce)
            guard expected == prepared.digest, refreshed.digest == prepared.digest,
                  case let .deletion(snapshots) = refreshed.snapshots else {
                register(refreshed.nonce, refreshed.digest)
                throw ApprovalChangedError(prepared: refreshed)
            }
            onStarted?()
            defer { onFinished?() }
            return try guarded("삭제 journal 오류입니다. 삭제 결과를 확인하고 Undo하세요.") {
                try deletions(undoJournal!).execute(snapshots)
            }
        }
        let normalized = try await gateway.normalizeToolArguments(prepared.toolName, arguments)
        if expected != prepared.digest {
            throw ApprovalChangedError(prepared: try newPrepared(prepared.toolName, normalized))
        }
        let refreshed = Self.snapshot(prepared.toolName, normalized)
        if refreshed.digest != prepared.digest || refreshed.targetPath != prepared.targetPath
            || refreshed.operation != prepared.operation {
            throw ApprovalChangedError(prepared: try newPrepared(prepared.toolName, normalized))
        }
        let journal = try requireJournal("Undo journal이 없어 승인된 파일 write를 실행하지 않았습니다.")
        let entry: JournalEntry
        do {
            entry = try journal.prepareWrite(
                refreshed.targetPath, operation: refreshed.operation,
                content: normalized["content"]?.stringValue ?? "",
                expectedState: refreshed.fileState.isEmpty ? nil : refreshed.fileState
            )
        } catch {
            throw RiskGuardError("Undo journal을 준비하지 못해 파일 write를 실행하지 않았습니다.")
        }
        onStarted?()
        defer { onFinished?() }
        let result: ToolResult
        do {
            result = try await gateway.callTool(prepared.toolName, normalized)
        } catch {
            try? journal.reconcileUncertainWrite(entry)
            throw error
        }
        if !result.ok {
            do {
                try journal.markWriteFailed(entry)
            } catch {
                throw RiskGuardError("실패한 write의 Undo journal 상태를 갱신하지 못했습니다.")
            }
        }
        return result
    }

    public func reject(_ prepared: PreparedWrite) { _ = take(prepared.nonce) }

    private static func snapshot(_ name: String, _ normalized: JSON) -> PreparedWrite {
        let path = normalized["path"]?.stringValue ?? ""
        let content = normalized["content"]?.stringValue ?? ""
        let operation = isFile(path) ? "overwrite" : "create"
        let state = operation == "overwrite" ? fileState(path) : ""
        let canonical = canonicalJSON(normalized)
        return textWrite(name, path, operation, content, canonical, digest(name, canonical, state), "", state)
    }
}

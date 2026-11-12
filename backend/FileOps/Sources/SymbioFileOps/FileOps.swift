import CryptoKit
import Foundation
import SymbioCore
import SymbioDelete
import SymbioUndo

/// Approved, no-clobber file moves, copies and empty directory creation (Python `voice_action/file_operations.py`).

public let moveTool = "local__move_file"
public let copyTool = "local__copy_file"
public let mkdirTool = "local__create_directory"
/// Python `OPERATIONS`: tool name → approval operation.
public let fileOperations: [String: String] = [moveTool: "move", copyTool: "copy", mkdirTool: "mkdir"]

private let transferSchema: JSON = [
    "type": "object",
    "properties": ["source": ["type": "string"], "destination": ["type": "string"]],
    "required": ["source", "destination"],
    "additionalProperties": false,
]

/// Python `DEFINITIONS`, in its order (move, copy, mkdir).
public let fileOperationDefinitions: [ToolDefinition] = [
    ToolDefinition(
        name: moveTool,
        description: "파일 하나를 이동하거나 이름 변경한다. source는 파일 절대경로, destination은 새 파일 "
            + "절대경로 또는 기존 폴더 절대경로. 승인 후 실행하며 Undo 가능. 덮어쓰기·폴더 이동·"
            + "다른 파일시스템으로 이동은 지원하지 않는다. 복사 후 삭제로 대신하지 마라.",
        schema: transferSchema
    ),
    ToolDefinition(
        name: copyTool,
        description: "파일 하나를 복사한다. source는 파일 절대경로, destination은 새 파일 절대경로 또는 "
            + "기존 폴더 절대경로. 원본을 유지하고 승인 후 새 파일 생성. 덮어쓰기 불가, Undo 가능.",
        schema: transferSchema
    ),
    ToolDefinition(
        name: mkdirTool,
        description: "허용 폴더 안에 새 폴더 하나를 만든다. path는 절대경로, 부모 폴더는 존재해야 한다. "
            + "승인 필요. Undo는 만든 폴더가 비어 있고 같은 폴더일 때만 제거한다.",
        schema: [
            "type": "object",
            "properties": ["path": ["type": "string"]],
            "required": ["path"],
            "additionalProperties": false,
        ]
    ),
]

/// Python snapshot tuple `(delete_snapshot | (), source_parent_id | (), target_parent_id)`.
/// Parent ids are `[st_dev, st_ino]`, or `[]` for mkdir's missing source.
public struct OperationSnapshot: Equatable, Sendable {
    public let source: DeleteSnapshot?
    public let sourceParent: [Int]
    public let targetParent: [Int]

    /// The tuple as JSON, for the approval digest (`_canonical_json({"state": snapshot})`).
    public var json: JSON {
        [source?.json ?? [], .array(sourceParent.map { .int($0) }), .array(targetParent.map { .int($0) })]
    }
}

private func identity(_ metadata: stat) -> [Int] { [Int(metadata.st_dev), Int(metadata.st_ino)] }

private func identity(_ descriptor: Int32) throws(POSIXError) -> [Int] {
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0 else { throw lastPOSIXError() }
    return identity(metadata)
}

private func lexists(_ path: String) -> Bool {
    var metadata = stat()
    return lstat(path, &metadata) == 0
}

private func nanoseconds(_ time: timespec) -> Int { time.tv_sec * 1_000_000_000 + time.tv_nsec }

private func name(_ path: String) -> String { (path as NSString).lastPathComponent }

/// Python `_file_state`: `"dev:ino:sha256"` of a regular file in `parent`, unchanged while read.
private func fileState(_ parent: Int32, _ name: String) throws -> String {
    let descriptor = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
    guard descriptor >= 0 else { throw lastPOSIXError() }
    defer { close(descriptor) }
    var before = stat(), after = stat(), current = stat()
    guard fstat(descriptor, &before) == 0 else { throw lastPOSIXError() }
    guard (before.st_mode & S_IFMT) == S_IFREG else { throw ToolError.rejected("일반 파일만 이동·복사할 수 있습니다.") }
    var digest = SHA256()
    var size = 0
    var buffer = [UInt8](repeating: 0, count: 1024 * 1024)
    while true {
        let count = read(descriptor, &buffer, buffer.count)
        if count < 0 { throw lastPOSIXError() }
        if count == 0 { break }
        size += count
        if size > maxDeleteBytes { throw ToolError.rejected("파일 작업은 최대 50 MiB입니다.") }
        buffer.withUnsafeBytes { digest.update(bufferPointer: UnsafeRawBufferPointer(rebasing: $0[..<count])) }
    }
    guard fstat(descriptor, &after) == 0 else { throw lastPOSIXError() }
    guard fstatat(parent, name, &current, AT_SYMLINK_NOFOLLOW) == 0 else { throw lastPOSIXError() }
    if identity(current) != identity(before)
        || before.st_size != after.st_size
        || nanoseconds(before.st_mtimespec) != nanoseconds(after.st_mtimespec)
        || nanoseconds(before.st_ctimespec) != nanoseconds(after.st_ctimespec) {
        throw ToolError.rejected("파일이 검사 중 변경되었습니다.")
    }
    let hex = digest.finalize().map { String(format: "%02x", $0) }.joined()
    return "\(before.st_dev):\(before.st_ino):\(hex)"
}

/// Errors: `ToolError.rejected` (refused/changed), `POSIXError` (OS), `UndoJournalError`, `SQLiteError`.
public final class FileOperationJournal: SubJournal {
    public let journal: UndoJournal
    public let deletions: DeletionJournal
    private let roots: [(path: String, identity: [Int])]

    /// Creates `path_operations` and attaches itself as `journal.operations`.
    /// Uses the attached `DeletionJournal`, creating one when missing (Python builds both).
    public init(_ journal: UndoJournal) throws(UndoJournalError) {
        self.journal = journal
        if let attached = journal.deletions as? DeletionJournal {
            deletions = attached
        } else {
            deletions = try DeletionJournal(journal)
        }
        var roots: [(String, [Int])] = []
        for root in journal.roots {
            var metadata = stat()
            guard stat(root.path, &metadata) == 0 else { throw UndoJournalError("Undo journal을 초기화할 수 없습니다.") }
            roots.append((root.path, identity(metadata)))
        }
        self.roots = roots
        do {
            try journal.connect { db in
                try db.execute("""
                    CREATE TABLE IF NOT EXISTS path_operations (
                        id TEXT PRIMARY KEY, tool TEXT NOT NULL, source TEXT NOT NULL,
                        destination TEXT NOT NULL, expected TEXT NOT NULL, parents TEXT NOT NULL,
                        status TEXT NOT NULL, created_at INTEGER NOT NULL
                    )
                    """)
            }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal을 초기화할 수 없습니다.")
        }
        journal.attachOperations(self)
    }

    /// Python `_parent`: walks from the root to `path`'s parent without following links.
    private func withParent<T>(_ path: String, _ body: (Int32) throws -> T) throws -> T {
        guard let root = roots.first(where: { isRelative(path, to: $0.path) }) else {
            throw ToolError.rejected("삭제 대상의 허용 경로/링크/Undo 경계를 위반했습니다.")
        }
        var descriptor = open(root.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
        guard descriptor >= 0 else { throw lastPOSIXError() }
        defer { close(descriptor) }
        if try identity(descriptor) != root.identity {
            throw ToolError.rejected("허용 폴더가 교체되어 작업을 중단했습니다.")
        }
        let parts = normalized(path).split(separator: "/").dropFirst(normalized(root.path).split(separator: "/").count)
        for part in parts.dropLast() {
            let child = openat(descriptor, String(part), O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            guard child >= 0 else { throw lastPOSIXError() }
            close(descriptor)
            descriptor = child
        }
        return try body(descriptor)
    }

    /// Freezes the approved paths: normalized arguments and the state bound into the approval.
    public func prepare(_ tool: String, _ arguments: JSON) throws -> (normalized: JSON, snapshot: OperationSnapshot) {
        guard let definition = fileOperationDefinitions.first(where: { $0.name == tool }),
              JSONSchema.isValid(arguments, schema: definition.schema) else {
            throw ToolError.rejected("잘못된 파일 작업 인자입니다.")
        }
        var source: String?, snapshot: DeleteSnapshot?
        if tool != mkdirTool {
            source = try deletions.path(arguments["source"]?.stringValue)
            snapshot = try deletions.read(source!).1
        }
        var destination = try deletions.path(
            arguments[tool == mkdirTool ? "path" : "destination"]?.stringValue, missing: true
        )
        var metadata = stat()
        if let source, stat(destination, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFDIR {
            destination = (destination as NSString).appendingPathComponent(name(source))
            _ = try deletions.path(destination, missing: true)
        }
        if lexists(destination) { throw ToolError.rejected("대상 경로가 이미 있습니다. 덮어쓰지 않습니다.") }
        let targetIdentity = try withParent(destination) { try identity($0) }
        var sourceIdentity: [Int] = []
        if let source {
            sourceIdentity = try withParent(source) { try identity($0) }
            // ponytail: 이동은 같은 파일시스템만 지원, 다른 디스크 이동 수요가 생기면 확장.
            if tool == moveTool && sourceIdentity[0] != targetIdentity[0] {
                throw ToolError.rejected("다른 파일시스템으로 이동은 지원하지 않습니다.")
            }
        }
        let normalized: JSON = source.map { ["source": .string($0), "destination": .string(destination)] }
            ?? ["path": .string(destination)]
        return (normalized, OperationSnapshot(source: snapshot, sourceParent: sourceIdentity, targetParent: targetIdentity))
    }

    private func setStatus(_ entry: String, _ status: String, expected: String? = nil) throws {
        try journal.connect {
            try $0.execute("UPDATE path_operations SET status=?, expected=COALESCE(?, expected) WHERE id=?",
                           [.text(status), expected.map { .text($0) } ?? .null, .text(entry)])
        }
    }

    /// Runs the approved operation after re-checking it against `snapshot`.
    public func execute(_ tool: String, _ arguments: JSON, _ snapshot: OperationSnapshot) throws -> ToolResult {
        let (normalized, current) = try prepare(tool, arguments)
        if normalized != arguments || current != snapshot {
            throw ToolError.rejected("승인 후 파일 또는 부모 폴더가 변경되었습니다.")
        }
        let destination = (arguments["destination"] ?? arguments["path"])?.stringValue ?? ""
        if tool == copyTool { return try copy(arguments, destination, snapshot) }
        let source = tool == moveTool ? arguments["source"]?.stringValue : nil
        let expected = snapshot.source.map { "\($0.device):\($0.inode):\($0.fingerprint)" } ?? ""
        let entry = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let parents: JSON = [.array(snapshot.sourceParent.map { .int($0) }), .array(snapshot.targetParent.map { .int($0) })]
        try journal.connect {
            try $0.execute("INSERT INTO path_operations VALUES (?, ?, ?, ?, ?, ?, 'active', ?)", [
                .text(entry), .text(tool), .text(source ?? ""), .text(destination), .text(expected),
                .text(parents.dumps()), .integer(Int64(clock_gettime_nsec_np(CLOCK_REALTIME))),
            ])
        }
        var created = false
        do {
            try withParent(destination) { target in
                if try identity(target) != snapshot.targetParent {
                    throw ToolError.rejected("대상 부모 폴더가 변경되었습니다.")
                }
                if let source {
                    try withParent(source) { origin in
                        if try identity(origin) != snapshot.sourceParent || fileState(origin, name(source)) != expected {
                            throw ToolError.rejected("원본 파일 또는 부모 폴더가 변경되었습니다.")
                        }
                        guard linkat(origin, name(source), target, name(destination), 0) == 0 else { throw lastPOSIXError() }
                        created = true
                        guard fsync(target) == 0 else { throw lastPOSIXError() }
                        if try fileState(origin, name(source)) != expected || fileState(target, name(destination)) != expected {
                            throw ToolError.rejected("이동 중 파일이 교체되어 원본을 지우지 않았습니다.")
                        }
                        // ponytail: 검사/unlink 사이 외부 교체는 OS 조건부 unlink가 필요하다.
                        guard unlinkat(origin, name(source), 0) == 0 else { throw lastPOSIXError() }
                        guard fsync(origin) == 0 else { throw lastPOSIXError() }
                    }
                } else {
                    guard mkdirat(target, name(destination), 0o755) == 0 else { throw lastPOSIXError() }
                    created = true
                    var metadata = stat()
                    guard fstatat(target, name(destination), &metadata, AT_SYMLINK_NOFOLLOW) == 0 else {
                        throw lastPOSIXError()
                    }
                    try setStatus(entry, "active", expected: "\(metadata.st_dev):\(metadata.st_ino)")
                }
                guard fsync(target) == 0 else { throw lastPOSIXError() }
            }
        } catch let error where error is ToolError || error is POSIXError || error is UndoJournalError {
            if !created { try setStatus(entry, "failed") }
            throw error
        }
        return ToolResult(ok: true, text: "\(source == nil ? "폴더 생성" : "이동") 완료: \(destination). Undo로 되돌릴 수 있습니다.")
    }

    private func copy(_ arguments: JSON, _ destination: String, _ snapshot: OperationSnapshot) throws -> ToolResult {
        let (data, current) = try deletions.read(arguments["source"]?.stringValue ?? "")
        if current != snapshot.source { throw ToolError.rejected("복사 원본이 변경되었습니다.") }
        let entry = try journal.prepareWrite(destination, operation: "create", content: data)
        do {
            try withParent(destination) { parent in
                if try identity(parent) != snapshot.targetParent {
                    throw ToolError.rejected("대상 부모 폴더가 변경되었습니다.")
                }
                let descriptor = openat(parent, name(destination), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
                guard descriptor >= 0 else { throw lastPOSIXError() }
                defer { close(descriptor) }
                var created = stat()
                guard fstat(descriptor, &created) == 0 else { throw lastPOSIXError() }
                do {
                    try data.withUnsafeBytes { (buffer: UnsafeRawBufferPointer) throws in
                        var offset = 0
                        while offset < buffer.count {
                            let written = write(descriptor, buffer.baseAddress! + offset, buffer.count - offset)
                            guard written > 0 else { throw lastPOSIXError() }
                            offset += written
                        }
                    }
                    guard fchmod(descriptor, mode_t(snapshot.source?.mode ?? 0o600)) == 0,
                          fsync(descriptor) == 0 else { throw lastPOSIXError() }
                } catch {
                    var now = stat()
                    if fstatat(parent, name(destination), &now, AT_SYMLINK_NOFOLLOW) == 0, identity(now) == identity(created) {
                        unlinkat(parent, name(destination), 0)
                    }
                    throw error
                }
                guard fsync(parent) == 0 else { throw lastPOSIXError() }
            }
        } catch {
            try journal.reconcileUncertainWrite(entry)
            throw error
        }
        return ToolResult(ok: true, text: "복사 완료: \(destination). 원본은 유지됩니다. Undo 가능.")
    }

    public func latest() throws -> (id: String, createdAt: Int64)? {
        let row = try journal.connect {
            try $0.query("""
                SELECT id, created_at FROM path_operations WHERE status='active' \
                ORDER BY created_at DESC LIMIT 1
                """)
        }.first
        guard let row, let id = row[0].stringValue, let createdAt = row[1].intValue else { return nil }
        return (id, createdAt)
    }

    /// Reverses one move or mkdir without overwriting or removing anything the user changed.
    public func undo(_ entry: String) throws -> String {
        guard let row = try journal.connect({
            try $0.query("""
                SELECT tool, source, destination, expected, parents FROM path_operations \
                WHERE id=? AND status='active'
                """, [.text(entry)])
        }).first, let tool = row[0].stringValue, let rawSource = row[1].stringValue,
            let rawDestination = row[2].stringValue, let expected = row[3].stringValue,
            let parents = try JSON.parse(row[4].stringValue ?? "").arrayValue, parents.count == 2
        else { throw ToolError.rejected("Undo 기록을 읽을 수 없습니다.") }
        let ids = parents.map { $0.arrayValue?.compactMap(\.intValue) ?? [] }
        let destination = try deletions.path(rawDestination, missing: true)
        let message: String? = try withParent(destination) { target in
            if try identity(target) != ids[1] { throw ToolError.rejected("Undo 대상 부모 폴더가 교체되었습니다.") }
            if tool == mkdirTool {
                if !lexists(destination) { return "만든 폴더가 이미 없어 파일을 변경하지 않았습니다." }
                var metadata = stat()
                guard fstatat(target, name(destination), &metadata, AT_SYMLINK_NOFOLLOW) == 0 else {
                    throw lastPOSIXError()
                }
                if (metadata.st_mode & S_IFMT) != S_IFDIR || "\(metadata.st_dev):\(metadata.st_ino)" != expected {
                    throw ToolError.rejected("만든 폴더가 교체되어 되돌리지 않았습니다.")
                }
                guard unlinkat(target, name(destination), AT_REMOVEDIR) == 0 else { throw lastPOSIXError() }
            } else {
                let source = try deletions.path(rawSource, missing: true)
                let early: String? = try withParent(source) { origin in
                    if try identity(origin) != ids[0] { throw ToolError.rejected("Undo 원본 부모 폴더가 교체되었습니다.") }
                    if !lexists(destination) {
                        if try fileState(origin, name(source)) != expected {
                            throw ToolError.rejected("이동 결과를 확인할 수 없습니다.")
                        }
                        return "원본이 이미 제자리에 있어 파일을 변경하지 않았습니다."
                    }
                    if try fileState(target, name(destination)) != expected {
                        throw ToolError.rejected("이동한 파일이 변경되어 되돌리지 않았습니다.")
                    }
                    if lexists(source) {
                        if try fileState(origin, name(source)) != expected {
                            throw ToolError.rejected("원래 위치에 다른 파일이 있어 되돌리지 않았습니다.")
                        }
                    } else {
                        guard linkat(target, name(destination), origin, name(source), 0) == 0 else { throw lastPOSIXError() }
                        guard fsync(origin) == 0 else { throw lastPOSIXError() }
                    }
                    if try fileState(target, name(destination)) != expected || fileState(origin, name(source)) != expected {
                        throw ToolError.rejected("Undo 중 파일이 변경되었습니다.")
                    }
                    guard unlinkat(target, name(destination), 0) == 0 else { throw lastPOSIXError() }
                    return nil
                }
                if let early { return early }
            }
            guard fsync(target) == 0 else { throw lastPOSIXError() }
            return nil
        }
        try setStatus(entry, "undone")
        return message ?? (tool == moveTool ? "파일 이동을 되돌렸습니다." : "만든 빈 폴더를 되돌렸습니다.")
    }
}

import CryptoKit
import Foundation
import SymbioCore

/// Local SQLite journal for undoing the latest approved filesystem write (Python `voice_action/undo.py`).

public let defaultJournalPath = ".voice-action/undo.sqlite3"
public let createUndoneMessage = "생성한 파일을 되돌렸습니다."
public let overwriteUndoneMessage = "이전 파일 내용을 복원했습니다."
public let nothingToUndoMessage = "되돌릴 수 있는 최근 파일 쓰기가 없습니다."
public let conflictMessage = "파일이 write 후 변경되어 되돌리지 않았습니다."
public let boundaryErrorMessage = "Undo 안전 경계를 확인할 수 없어 파일을 변경하지 않았습니다."

public struct UndoJournalError: Error, Equatable, Sendable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

/// Python `_UndoBoundaryError`: the target is not a plain regular file inside the boundary.
public struct UndoBoundaryError: Error, Equatable, Sendable {
    public init() {}
}

/// Python `_UndoConflict`: the file changed after the journaled write.
public struct UndoConflict: Error, Equatable, Sendable {
    public init() {}
}

public enum UndoKind: Sendable {
    case createUndone, overwriteUndone, deleteUndone, operationUndone, nothingToUndo, conflict, boundaryError
}

public struct UndoResult: Equatable, Sendable {
    public let kind: UndoKind
    public let message: String
    public init(_ kind: UndoKind, _ message: String) {
        self.kind = kind
        self.message = message
    }
}

public struct JournalEntry: Equatable, Sendable {
    public let commandID: String
    public let canonicalPath: String
    public let operation: String
    public let postFingerprint: String
}

/// Delete/FileOps journals that share the undo database (Python `DeletionJournal`/`FileOperationJournal`).
/// Errors: `ToolError` with `isRejected` = refused (conflict), `POSIXError` = OS failure,
/// `UndoJournalError` passes through unchanged.
public protocol SubJournal: AnyObject, Sendable {
    /// Latest active entry (Python `latest()` row: id, created_at in ns).
    func latest() throws -> (id: String, createdAt: Int64)?
    /// Restores one entry and returns the user message.
    func undo(_ id: String) throws -> String
}

/// Python `_fingerprint`: lowercase SHA-256 hex.
public func fingerprint(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

/// `POSIXError` for the current `errno` (Python `OSError`).
public func lastPOSIXError() -> POSIXError {
    POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
}

/// Python `type(exc).__name__` for the errors sub-journals throw.
public func pythonErrorName(_ error: any Error) -> String {
    if let error = error as? ToolError {
        switch error {
        case .rejected: return "ToolRejectedError"
        case .notFound: return "MCPToolNotFoundError"
        case .startup: return "MCPStartupError"
        case .connection: return "MCPConnectionError"
        case .timeout: return "MCPToolTimeoutError"
        case .other: return "MCPError"
        }
    }
    if let error = error as? POSIXError {
        switch error.code {
        case .ENOENT: return "FileNotFoundError"
        case .EEXIST: return "FileExistsError"
        case .EACCES, .EPERM: return "PermissionError"
        case .ENOTDIR: return "NotADirectoryError"
        case .EISDIR: return "IsADirectoryError"
        default: return "OSError"
        }
    }
    return String(describing: type(of: error))
}

/// Python `_read_regular_file`: open without following links; only a regular file.
public func readRegularFile(_ path: String) throws(UndoBoundaryError) -> (Data, stat) {
    let descriptor = open(path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else { throw UndoBoundaryError() }
    defer { close(descriptor) }
    var metadata = stat()
    guard fstat(descriptor, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFREG else {
        throw UndoBoundaryError()
    }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 1024 * 1024)
    while true {
        let count = read(descriptor, &buffer, buffer.count)
        if count < 0 { throw UndoBoundaryError() }
        if count == 0 { return (data, metadata) }
        data.append(buffer, count: count)
    }
}

/// Python `_same_file`: still the same regular, non-link inode.
public func sameFile(_ path: String, _ expected: stat) -> Bool {
    var current = stat()
    guard lstat(path, &current) == 0 else { return false }
    return (current.st_mode & S_IFMT) == S_IFREG
        && current.st_dev == expected.st_dev && current.st_ino == expected.st_ino
}

/// Python `_fsync_directory`.
public func fsyncDirectory(_ path: String) throws(POSIXError) {
    let descriptor = open(path, O_RDONLY)
    guard descriptor >= 0 else { throw lastPOSIXError() }
    defer { close(descriptor) }
    guard fsync(descriptor) == 0 else { throw lastPOSIXError() }
}

private func nowNanoseconds() -> Int64 { Int64(clock_gettime_nsec_np(CLOCK_REALTIME)) }

/// Durable write-ahead records and one-at-a-time filesystem restoration.
public final class UndoJournal: @unchecked Sendable {  // mutable state is behind `lock`
    /// Canonical journal file (`<real parent>/<name>`).
    public let path: URL
    /// Allowed roots, resolved (Python `journal._roots`).
    public let roots: [URL]

    private let lock = NSLock()
    private var _deletions: (any SubJournal)?
    private var _operations: (any SubJournal)?

    public var deletions: (any SubJournal)? { lock.withLock { _deletions } }
    public var operations: (any SubJournal)? { lock.withLock { _operations } }

    public init(path: URL, allowedRoots: [URL]) throws(UndoJournalError) {
        var resolved: [URL] = []
        for root in allowedRoots {
            guard let real = realPath(root.path) else {
                throw UndoJournalError("Undo journal의 허용 경로를 준비할 수 없습니다.")
            }
            resolved.append(URL(fileURLWithPath: real))
        }
        var isDirectory: ObjCBool = false
        guard !resolved.isEmpty, resolved.allSatisfy({
            FileManager.default.fileExists(atPath: $0.path, isDirectory: &isDirectory) && isDirectory.boolValue
        }) else {
            throw UndoJournalError("Undo journal에 유효한 허용 경로가 필요합니다.")
        }
        roots = resolved
        let raw = path.absoluteURL
        let parent = raw.deletingLastPathComponent()
        do {
            try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
            guard let realParent = realPath(parent.path) else { throw lastPOSIXError() }
            self.path = URL(fileURLWithPath: realParent).appendingPathComponent(raw.lastPathComponent)
        } catch {
            throw UndoJournalError("Undo journal을 초기화할 수 없습니다.")
        }
        do {
            let connection = try openConnection()
            let mode = try connection.query("PRAGMA journal_mode=WAL").first?.first?.stringValue
            if mode?.lowercased() != "wal" {
                throw UndoJournalError("Undo journal WAL mode를 활성화할 수 없습니다.")
            }
            try connection.transaction { db in
                try db.execute("""
                    CREATE TABLE IF NOT EXISTS undo_entries (
                        command_id TEXT PRIMARY KEY,
                        canonical_path TEXT NOT NULL,
                        operation TEXT NOT NULL CHECK (operation IN ('create', 'overwrite')),
                        original_bytes BLOB,
                        post_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('active', 'undoing', 'undone', 'conflict', 'failed')
                        ),
                        created_at INTEGER NOT NULL
                    )
                    """)
                try db.execute("""
                    CREATE INDEX IF NOT EXISTS undo_entries_latest
                    ON undo_entries(status, created_at DESC)
                    """)
            }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal을 초기화할 수 없습니다.")
        }
    }

    /// Python `UndoJournal.default`: `<cwd>/.voice-action/undo.sqlite3`.
    public static func `default`(allowedRoots: [URL]) throws(UndoJournalError) -> UndoJournal {
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        return try UndoJournal(path: cwd.appendingPathComponent(defaultJournalPath), allowedRoots: allowedRoots)
    }

    /// Python constructs these itself; Swift attaches them to avoid a package cycle.
    public func attachDeletions(_ journal: any SubJournal) { lock.withLock { _deletions = journal } }
    public func attachOperations(_ journal: any SubJournal) { lock.withLock { _operations = journal } }

    /// Fresh connection with `synchronous=FULL` and a 5 s busy timeout, after the link check.
    public func openConnection() throws -> SQLiteDatabase {
        try verifyJournalPath()
        let connection = try SQLiteDatabase(path: path.path, timeout: 5)
        try connection.execute("PRAGMA synchronous=FULL")
        return connection
    }

    /// Python `_connect`: one transaction, committed on success and rolled back on error.
    @discardableResult
    public func connect<T>(immediate: Bool = false, _ body: (SQLiteDatabase) throws -> T) throws -> T {
        try openConnection().transaction(immediate ? .immediate : .deferred, body)
    }

    private func verifyJournalPath() throws(UndoJournalError) {
        var metadata = stat()
        if lstat(path.path, &metadata) != 0 {
            if errno == ENOENT { return }
            throw UndoJournalError("Undo journal 경계를 확인할 수 없습니다.")
        }
        if (metadata.st_mode & S_IFMT) != S_IFREG {
            throw UndoJournalError("Undo journal 경계를 확인할 수 없습니다.")
        }
    }

    /// Persist the recovery data before the approved transport call (text write).
    public func prepareWrite(
        _ canonicalPath: String, operation: String, content: String, expectedState: String? = nil
    ) throws(UndoJournalError) -> JournalEntry {
        try prepare(canonicalPath, operation, Data(content.utf8), isBytes: false, expectedState)
    }

    /// Byte content is allowed only for `create` (Python `bytes` content).
    public func prepareWrite(
        _ canonicalPath: String, operation: String, content: Data, expectedState: String? = nil
    ) throws(UndoJournalError) -> JournalEntry {
        try prepare(canonicalPath, operation, content, isBytes: true, expectedState)
    }

    private func prepare(
        _ canonicalPath: String, _ operation: String, _ content: Data, isBytes: Bool, _ expectedState: String?
    ) throws(UndoJournalError) -> JournalEntry {
        guard canonicalPath.hasPrefix("/"), operation == "create" || operation == "overwrite" else {
            throw UndoJournalError("Undo journal에 안전한 write 정보를 기록할 수 없습니다.")
        }
        if isBytes && operation != "create" {
            throw UndoJournalError("Undo journal은 텍스트 write와 새 파일 생성만 지원합니다.")
        }
        let path = normalized(canonicalPath)
        var original: SQLiteValue = .null
        if operation == "overwrite" {
            let data: Data, metadata: stat
            do {
                (data, metadata) = try readRegularFile(path)
            } catch {
                throw UndoJournalError("Undo journal을 준비하지 못해 파일을 쓰지 않았습니다.")
            }
            // The bytes backed up must be the file that was approved (risk_guard.file_state).
            let state = "\(metadata.st_dev):\(metadata.st_ino):\(fingerprint(data))"
            if let expectedState, state != expectedState {
                throw UndoJournalError("Undo journal 준비 중 write 대상 상태가 변경되었습니다.")
            }
            guard String(validating: data, as: UTF8.self) != nil else {
                throw UndoJournalError("Undo는 UTF-8 텍스트 파일 overwrite만 지원합니다.")
            }
            original = .blob(data)
        } else {
            var metadata = stat()
            if lstat(path, &metadata) == 0 {
                throw UndoJournalError("Undo journal 준비 중 write 대상 상태가 변경되었습니다.")
            } else if errno != ENOENT {
                throw UndoJournalError("Undo journal을 기록하지 못해 파일을 쓰지 않았습니다.")
            }
        }
        let entry = JournalEntry(
            commandID: UUID().uuidString.lowercased(), canonicalPath: canonicalPath,
            operation: operation, postFingerprint: fingerprint(content)
        )
        do {
            try connect { db in
                try db.execute("""
                    INSERT INTO undo_entries (
                        command_id, canonical_path, operation, original_bytes,
                        post_fingerprint, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?)
                    """, [
                        .text(entry.commandID), .text(entry.canonicalPath), .text(entry.operation),
                        original, .text(entry.postFingerprint), .integer(nowNanoseconds()),
                    ])
            }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal을 기록하지 못해 파일을 쓰지 않았습니다.")
        }
        return entry
    }

    public func markWriteFailed(_ entry: JournalEntry) throws(UndoJournalError) {
        try setStatus(.text(entry.commandID), "failed", expected: "active")
    }

    /// Keep an uncertain write undoable only when exact output is present.
    public func reconcileUncertainWrite(_ entry: JournalEntry) throws(UndoJournalError) {
        let current = try? readRegularFile(normalized(entry.canonicalPath))
        if current.map({ fingerprint($0.0) }) != entry.postFingerprint {
            try setStatus(.text(entry.commandID), "failed", expected: "active")
        }
    }

    /// Undo the newest of: path operation, delete batch, file write.
    public func undoLastWrite() throws -> UndoResult {
        do {
            let latestDelete = try deletions?.latest()
            let latestOperation = try operations?.latest()
            let latestWrite = try connect {
                try $0.query("SELECT MAX(created_at) FROM undo_entries WHERE status='active'")
            }.first?.first?.intValue
            if let operation = latestOperation, let operations,
               operation.createdAt > max(latestWrite ?? 0, latestDelete?.createdAt ?? 0) {
                do {
                    return UndoResult(.operationUndone, try operations.undo(operation.id))
                } catch let error where (error as? ToolError)?.isRejected == true || error is POSIXError {
                    return UndoResult(.conflict,
                                      "파일 작업을 되돌리지 못했습니다 (\(pythonErrorName(error))). "
                                      + "경로·파일 변경·폴더가 비었는지 확인하세요.")
                }
            }
            if let delete = latestDelete, let deletions,
               latestWrite.map({ delete.createdAt > $0 }) ?? true {
                do {
                    return UndoResult(.deleteUndone, try deletions.undo(delete.id))
                } catch let error as ToolError where error.isRejected {
                    return UndoResult(.conflict, error.message)
                }
            }
        } catch let error where error is POSIXError || error is SQLiteError {
            throw UndoJournalError("삭제 Undo journal을 처리할 수 없습니다.")
        }
        guard let row = try claimLatest() else {
            return UndoResult(.nothingToUndo, nothingToUndoMessage)
        }
        let commandID = row[0]
        do {
            let (path, metadata) = try validatedCurrent(row[1], row[2], row[3], row[4])
            let result: UndoResult
            if row[2] == .text("create") {
                guard sameFile(path, metadata) else { throw UndoConflict() }
                guard unlink(path) == 0 else { throw lastPOSIXError() }
                try fsyncDirectory((path as NSString).deletingLastPathComponent)
                result = UndoResult(.createUndone, createUndoneMessage)
            } else {
                try Self.atomicRestore(path, row[3].dataValue ?? Data(), metadata)
                result = UndoResult(.overwriteUndone, overwriteUndoneMessage)
            }
            try setStatus(commandID, "undone", expected: "undoing")
            return result
        } catch is UndoConflict {
            try setStatus(commandID, "conflict", expected: "undoing")
            return UndoResult(.conflict, conflictMessage)
        } catch is UndoBoundaryError {
            try setStatus(commandID, "conflict", expected: "undoing")
            return UndoResult(.boundaryError, boundaryErrorMessage)
        } catch let error where error is POSIXError || error is SQLiteError {
            try? setStatus(commandID, "conflict", expected: "undoing")
            throw UndoJournalError("Undo 처리 중 안전하게 파일을 복구할 수 없었습니다.")
        }
    }

    private func claimLatest() throws(UndoJournalError) -> [SQLiteValue]? {
        do {
            return try connect(immediate: true) { db -> [SQLiteValue]? in
                guard let row = try db.query("""
                    SELECT command_id, canonical_path, operation, original_bytes,
                           post_fingerprint
                    FROM undo_entries
                    WHERE status = 'active'
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """).first else { return nil }
                let changed = try db.execute("""
                    UPDATE undo_entries SET status = 'undoing'
                    WHERE command_id = ? AND status = 'active'
                    """, [row[0]])
                if changed != 1 { throw UndoJournalError("Undo journal entry를 사용할 수 없습니다.") }
                return row
            }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal을 읽을 수 없습니다.")
        }
    }

    private func validatedCurrent(
        _ rawPath: SQLiteValue, _ operation: SQLiteValue, _ original: SQLiteValue, _ expected: SQLiteValue
    ) throws -> (String, stat) {
        guard let raw = rawPath.stringValue, let expected = expected.stringValue, expected.count == 64,
              (operation == .text("create") && original.isNull)
                || (operation == .text("overwrite") && original.dataValue != nil)
        else { throw UndoBoundaryError() }
        let path = normalized(raw)
        guard raw.hasPrefix("/"), roots.contains(where: { isRelative(path, to: $0.path) }) else {
            throw UndoBoundaryError()
        }
        var metadata = stat()
        guard lstat(path, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFREG,
              realPath(path) == path else { throw UndoBoundaryError() }
        let (current, opened) = try readRegularFile(path)
        if metadata.st_dev != opened.st_dev || metadata.st_ino != opened.st_ino { throw UndoConflict() }
        if fingerprint(current) != expected { throw UndoConflict() }
        return (path, opened)
    }

    private static func atomicRestore(_ path: String, _ original: Data, _ expectedCurrent: stat) throws {
        let parent = (path as NSString).deletingLastPathComponent
        var template = Array((parent + "/.voice-action-undo-XXXXXXXX").utf8CString)
        var descriptor = mkstemp(&template)
        guard descriptor >= 0 else { throw lastPOSIXError() }
        var temporaryPath: String? = String(decoding: template.dropLast().map { UInt8(bitPattern: $0) }, as: UTF8.self)
        defer {
            if descriptor >= 0 { close(descriptor) }
            if let temporaryPath { unlink(temporaryPath) }
        }
        guard fchmod(descriptor, expectedCurrent.st_mode & 0o7777) == 0 else { throw lastPOSIXError() }
        try original.withUnsafeBytes { (buffer: UnsafeRawBufferPointer) throws in
            var offset = 0
            while offset < buffer.count {
                let written = write(descriptor, buffer.baseAddress! + offset, buffer.count - offset)
                guard written > 0 else { throw lastPOSIXError() }
                offset += written
            }
        }
        guard fsync(descriptor) == 0 else { throw lastPOSIXError() }
        close(descriptor)
        descriptor = -1
        guard sameFile(path, expectedCurrent) else { throw UndoConflict() }
        guard rename(temporaryPath!, path) == 0 else { throw lastPOSIXError() }
        temporaryPath = nil
        try fsyncDirectory(parent)
    }

    private func setStatus(_ commandID: SQLiteValue, _ status: String, expected: String) throws(UndoJournalError) {
        do {
            let changed = try connect {
                try $0.execute("""
                    UPDATE undo_entries SET status = ?
                    WHERE command_id = ? AND status = ?
                    """, [.text(status), commandID, .text(expected)])
            }
            if changed != 1 { throw UndoJournalError("Undo journal 상태를 갱신할 수 없습니다.") }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal 상태를 갱신할 수 없습니다.")
        }
    }

    /// Return a count without exposing journal payloads.
    public func entryCount(status: String? = nil) throws(UndoJournalError) -> Int {
        let sql = "SELECT COUNT(*) FROM undo_entries" + (status == nil ? "" : " WHERE status = ?")
        do {
            let rows = try connect { try $0.query(sql, status.map { [.text($0)] } ?? []) }
            return Int(rows.first?.first?.intValue ?? 0)
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal 상태를 읽을 수 없습니다.")
        }
    }
}

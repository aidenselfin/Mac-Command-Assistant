import Foundation
import SymbioCore
import SymbioUndo
import UniformTypeIdentifiers

/// Local, approved file batches with durable backups in the existing journal (Python `voice_action/deletion.py`).

public let deleteTool = "local__delete_files"
public let maxDeleteFiles = 100
public let maxDeleteBytes = 50 * 1024 * 1024
public let previewLines = 3
public let previewLineChars = 80
public let previewFiles = 20

private let deleteDescription =
    "사용자가 명시적으로 요청한 파일 삭제. paths로 단일/여러 절대경로를 지정하거나 "
    + "directory와 pattern(예: *.txt, *)으로 해당 폴더 바로 아래 파일들을 선택한다. "
    + "폴더/심볼릭 링크/재귀 삭제는 지원하지 않는다. 확정된 전체 목록을 승인받고 "
    + "백업 후 삭제하며 Undo로 복원한다. 최대 100개, 합계 50 MiB."

/// Strict schema `prepare` validates against (Python `DELETE_SCHEMA`).
public let deleteSchema: JSON = [
    "type": "object",
    "properties": [
        "paths": [
            "type": "array",
            "items": ["type": "string"],
            "minItems": 1,
            "maxItems": .int(maxDeleteFiles),
            "uniqueItems": true,
        ],
        "directory": ["type": "string"],
        "pattern": ["type": "string", "minLength": 1],
    ],
    "additionalProperties": false,
    "oneOf": [
        [
            "required": ["paths"],
            "not": ["anyOf": [["required": ["directory"]], ["required": ["pattern"]]]],
        ],
        ["required": ["directory", "pattern"], "not": ["required": ["paths"]]],
    ],
]

/// A provider checks a generated call against the schema it was given and rejects the whole answer
/// with HTTP 400 for "missing paths" when the model chose directory+pattern. So the model gets a
/// plain schema; `prepare` validates against the strict one.
public let deleteDefinition = ToolDefinition(
    name: deleteTool,
    description: deleteDescription,
    schema: [
        "type": "object",
        "properties": [
            "paths": ["type": "array", "items": ["type": "string"]],
            "directory": ["type": "string"],
            "pattern": ["type": "string"],
        ],
    ]
)

/// Python snapshot tuple `(path, st_dev, st_ino, st_mtime_ns, st_ctime_ns, S_IMODE, sha256)`.
public struct DeleteSnapshot: Equatable, Sendable {
    public let path: String
    public let device: Int
    public let inode: Int
    public let mtimeNs: Int
    public let ctimeNs: Int
    public let mode: Int
    public let fingerprint: String

    /// The tuple as a JSON array, for the approval digest (`_canonical_json({"files": snapshots})`).
    public var json: JSON {
        [.string(path), .int(device), .int(inode), .int(mtimeNs), .int(ctimeNs), .int(mode), .string(fingerprint)]
    }
}

private func nanoseconds(_ time: timespec) -> Int { time.tv_sec * 1_000_000_000 + time.tv_nsec }

private func sizeText(_ size: Int) -> String {
    if size < 1024 { return "\(size) B" }
    let kb = Double(size) / 1024
    return kb < 1024 ? String(format: "%.1f KB", kb) : String(format: "%.1f MB", kb / 1024)
}

/// Python incremental UTF-8 decode with `final=False`: a sequence cut at the end is not an error.
private func decodeHead(_ head: Data) -> String? {
    let bytes = [UInt8](head)
    var end = bytes.count
    var lead = end - 1
    while lead >= 0, end - lead < 4, bytes[lead] & 0xC0 == 0x80 { lead -= 1 }
    if lead >= 0, (0xC2...0xF4).contains(bytes[lead]) {
        let need = bytes[lead] >= 0xF0 ? 4 : bytes[lead] >= 0xE0 ? 3 : 2
        if need > end - lead { end = lead }
    }
    return String(validating: bytes[..<end], as: UTF8.self)
}

/// Head lines for text, kind and size for binary. For the user's screen, never the model.
public func filePreview(_ path: String, _ data: Data) -> String {
    let head = data.prefix(4096)
    guard let text = decodeHead(head), !text.contains("\0") else {
        let ext = (path as NSString).pathExtension
        let kind = ext.isEmpty ? nil : UTType(filenameExtension: ext)?.preferredMIMEType
        return "바이너리 · \(kind ?? "알 수 없는 형식") · \(sizeText(data.count))"
    }
    // Python str.splitlines() boundaries.
    let lines = text.split { $0.isNewline || [0x1C, 0x1D, 0x1E].contains($0.unicodeScalars.first!.value) }
        .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
        .map { String(String.UnicodeScalarView($0.unicodeScalars.prefix(previewLineChars))) }
    if lines.isEmpty { return data.isEmpty ? "(빈 파일)" : "(공백만 있음)" }
    let more = lines.count > previewLines || data.count > head.count
    return lines.prefix(previewLines).joined(separator: "\n") + (more ? "\n…" : "")
}

/// Python `Path.resolve(strict=False)` limited to missing trailing parts; nil = cannot check.
private func resolveMissing(_ path: String) -> String? {
    if let real = realPath(path) { return real }
    var metadata = stat()
    if lstat(path, &metadata) == 0 { return "" }  // exists but unresolvable (dangling link): differs from path
    guard errno == ENOENT, path != "/" else { return nil }
    let parent = (path as NSString).deletingLastPathComponent
    guard let resolved = resolveMissing(parent) else { return nil }
    return (resolved == "/" ? "" : resolved) + "/" + (path as NSString).lastPathComponent
}

public final class DeletionJournal: SubJournal, @unchecked Sendable {  // immutable after init
    public let journal: UndoJournal

    /// Creates the delete tables and attaches itself as `journal.deletions`.
    public init(_ journal: UndoJournal) throws(UndoJournalError) {
        self.journal = journal
        do {
            try journal.connect { db in
                try db.execute("""
                    CREATE TABLE IF NOT EXISTS delete_batches (
                        batch_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('active', 'undone'))
                    )
                    """)
                try db.execute("""
                    CREATE TABLE IF NOT EXISTS delete_files (
                        batch_id TEXT NOT NULL, path TEXT NOT NULL, original BLOB NOT NULL,
                        mode INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('planned', 'deleting', 'deleted', 'restored')
                        ), PRIMARY KEY (batch_id, path)
                    )
                    """)
            }
        } catch let error as UndoJournalError {
            throw error
        } catch {
            throw UndoJournalError("Undo journal을 초기화할 수 없습니다.")
        }
        journal.attachDeletions(self)
    }

    /// Python `_path`: absolute, canonical, non-link path in a root and not the journal itself.
    public func path(_ raw: String?, missing: Bool = false) throws(ToolError) -> String {
        guard let raw, !raw.isEmpty, raw == raw.trimmingCharacters(in: .whitespacesAndNewlines) else {
            throw .rejected("삭제 경로가 올바르지 않습니다.")
        }
        let boundary = ToolError.rejected("삭제 대상의 허용 경로/링크/Undo 경계를 위반했습니다.")
        let path = normalized(raw)
        guard raw.hasPrefix("/"), journal.roots.contains(where: { isRelative(path, to: $0.path) }) else {
            throw boundary
        }
        guard !raw.contains("\0"), let resolved = missing ? resolveMissing(path) : realPath(path) else {
            throw .rejected("삭제 경로를 확인할 수 없습니다.")
        }
        var metadata = stat()
        let isLink = lstat(path, &metadata) == 0 && (metadata.st_mode & S_IFMT) == S_IFLNK
        let journalPath = journal.path.path
        if resolved != path || isLink || [journalPath, journalPath + "-wal", journalPath + "-shm"].contains(path) {
            throw boundary
        }
        return path
    }

    /// Python `_read`: bytes and snapshot of a regular file up to `maxDeleteBytes`.
    /// Throws `ToolError.rejected` or `POSIXError`.
    public func read(_ raw: String) throws -> (Data, DeleteSnapshot) {
        let path = try self.path(raw)
        let descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard descriptor >= 0 else { throw lastPOSIXError() }
        defer { close(descriptor) }
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0 else { throw lastPOSIXError() }
        guard (metadata.st_mode & S_IFMT) == S_IFREG, metadata.st_size <= maxDeleteBytes else {
            throw ToolError.rejected("일반 파일만 삭제할 수 있고 백업은 최대 50 MiB입니다.")
        }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024 * 1024)
        while true {
            let count = Darwin.read(descriptor, &buffer, min(buffer.count, maxDeleteBytes + 1 - data.count))
            if count < 0 { throw lastPOSIXError() }
            if count == 0 { break }
            data.append(buffer, count: count)
            if data.count > maxDeleteBytes { throw ToolError.rejected("삭제 백업 크기는 최대 50 MiB입니다.") }
        }
        var current = stat(), after = stat()
        guard lstat(path, &current) == 0 else { throw lastPOSIXError() }
        guard fstat(descriptor, &after) == 0 else { throw lastPOSIXError() }
        if try self.path(raw) != path
            || current.st_dev != metadata.st_dev || current.st_ino != metadata.st_ino
            || metadata.st_size != after.st_size
            || nanoseconds(metadata.st_mtimespec) != nanoseconds(after.st_mtimespec)
            || nanoseconds(metadata.st_ctimespec) != nanoseconds(after.st_ctimespec) {
            throw ToolError.rejected("삭제 대상이 검사 중 변경되었습니다.")
        }
        let snapshot = DeleteSnapshot(
            path: path, device: Int(metadata.st_dev), inode: Int(metadata.st_ino),
            mtimeNs: nanoseconds(metadata.st_mtimespec), ctimeNs: nanoseconds(metadata.st_ctimespec),
            mode: Int(metadata.st_mode & 0o7777), fingerprint: fingerprint(data)
        )
        return (data, snapshot)
    }

    /// Freezes the approved list: `({"paths": [...]}, snapshots, previews)`.
    /// Throws `ToolError.rejected` or `POSIXError`.
    public func prepare(_ arguments: JSON) throws -> (normalized: JSON, snapshots: [DeleteSnapshot], previews: [String]) {
        // ponytail: Core JSONSchema has no "not"; the oneOf "not" branches are checked by hand here.
        let has = { (key: String) in arguments[key] != nil }
        guard JSONSchema.isValid(arguments, schema: deleteSchema), !(has("paths") && (has("directory") || has("pattern")))
        else { throw ToolError.rejected("잘못된 삭제 도구 인자입니다.") }
        var paths: [String] = []
        if let raw = arguments["paths"]?.arrayValue {
            paths = raw.compactMap(\.stringValue)
        } else {
            let directory = try path(arguments["directory"]?.stringValue)
            let pattern = arguments["pattern"]?.stringValue ?? ""
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: directory, isDirectory: &isDirectory), isDirectory.boolValue,
                  !pattern.contains("/"), !pattern.contains("\\"), !pattern.contains("**") else {
                throw ToolError.rejected("삭제 패턴은 폴더 바로 아래 파일만 선택할 수 있습니다.")
            }
            for name in try FileManager.default.contentsOfDirectory(atPath: directory)
            where fnmatch(pattern, name, 0) == 0 {
                let child = (directory as NSString).appendingPathComponent(name)
                var link = stat(), target = stat()
                let isLink = lstat(child, &link) == 0 && (link.st_mode & S_IFMT) == S_IFLNK
                let isDir = stat(child, &target) == 0 && (target.st_mode & S_IFMT) == S_IFDIR
                if isLink || !isDir { paths.append(child) }
                if paths.count > maxDeleteFiles {
                    throw ToolError.rejected("한 번에 최대 100개 파일을 삭제할 수 있습니다.")
                }
            }
        }
        if paths.isEmpty { throw ToolError.rejected("삭제 조건에 맞는 파일이 없습니다.") }
        var snapshots: [DeleteSnapshot] = [], previews: [String] = [], total = 0
        // Python sorted(): code point order.
        for path in paths.sorted(by: { $0.unicodeScalars.lexicographicallyPrecedes($1.unicodeScalars) }) {
            let (data, snapshot) = try read(path)
            total += data.count
            if total > maxDeleteBytes { throw ToolError.rejected("삭제 파일의 전체 백업 크기는 최대 50 MiB입니다.") }
            snapshots.append(snapshot)
            if previews.count < previewFiles { previews.append(filePreview(snapshot.path, data)) }
        }
        return (["paths": .array(snapshots.map { .string($0.path) })], snapshots, previews)
    }

    /// Backs up every file in one transaction, then deletes them one by one.
    /// Throws `ToolError.rejected` (changed before backup), `UndoJournalError` or `SQLiteError`.
    public func execute(_ snapshots: [DeleteSnapshot]) throws -> ToolResult {
        var backups: [(Data, DeleteSnapshot)] = []
        for expected in snapshots {
            let (data, current) = try read(expected.path)
            if current != expected { throw ToolError.rejected("승인 후 삭제 대상이 변경되어 삭제하지 않았습니다.") }
            backups.append((data, current))
        }
        let batch = UUID().uuidString.lowercased()
        try journal.connect { db in
            try db.execute("INSERT INTO delete_batches VALUES (?, ?, 'active')",
                           [.text(batch), .integer(Int64(clock_gettime_nsec_np(CLOCK_REALTIME)))])
            for (data, item) in backups {
                try db.execute("INSERT INTO delete_files VALUES (?, ?, ?, ?, ?, 'planned')",
                               [.text(batch), .text(item.path), .blob(data), .integer(Int64(item.mode)),
                                .text(item.fingerprint)])
            }
        }
        var deleted = 0
        do {
            for (_, expected) in backups {
                let (_, current) = try read(expected.path)
                if current != expected { throw ToolError.rejected("삭제 실행 중 대상 파일이 변경되었습니다.") }
                let path = try self.path(expected.path)
                let name = (path as NSString).lastPathComponent
                let parent = open((path as NSString).deletingLastPathComponent, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
                guard parent >= 0 else { throw lastPOSIXError() }
                defer { close(parent) }
                var metadata = stat()
                guard fstatat(parent, name, &metadata, AT_SYMLINK_NOFOLLOW) == 0 else { throw lastPOSIXError() }
                if Int(metadata.st_dev) != expected.device || Int(metadata.st_ino) != expected.inode {
                    throw ToolError.rejected("삭제 실행 중 대상 파일이 교체되었습니다.")
                }
                try setStatus(batch, path, "deleting")
                // ponytail: stat/unlink 사이 외부 변경은 OS 조건부 unlink가 필요하다.
                guard unlinkat(parent, name, 0) == 0 else { throw lastPOSIXError() }
                deleted += 1
                guard fsync(parent) == 0 else { throw lastPOSIXError() }
                try setStatus(batch, path, "deleted")
            }
        } catch let error where error is ToolError || error is POSIXError || error is UndoJournalError {
            return ToolResult(
                ok: false, text: "",
                error: "일괄 삭제 중단: \(deleted)/\(backups.count)개 삭제됨. "
                    + "삭제된 파일은 Undo로 복원할 수 있습니다. (\(pythonErrorName(error)))"
            )
        }
        return ToolResult(ok: true, text: "\(deleted)개 파일을 삭제했습니다. Undo로 복원할 수 있습니다.")
    }

    private func setStatus(_ batch: String, _ path: String, _ status: String) throws {
        try journal.connect {
            try $0.execute("UPDATE delete_files SET status=? WHERE batch_id=? AND path=?",
                           [.text(status), .text(batch), .text(path)])
        }
    }

    public func latest() throws -> (id: String, createdAt: Int64)? {
        let row = try journal.connect {
            try $0.query("""
                SELECT batch_id, created_at FROM delete_batches \
                WHERE status='active' ORDER BY created_at DESC LIMIT 1
                """)
        }.first
        guard let row, let id = row[0].stringValue, let createdAt = row[1].intValue else { return nil }
        return (id, createdAt)
    }

    /// Restores every deleted file of the batch without overwriting anything.
    /// Throws `ToolError.rejected` (conflict), `UndoJournalError` (partial restore) or `POSIXError`.
    public func undo(_ batch: String) throws -> String {
        let rows = try journal.connect {
            try $0.query("""
                SELECT path, original, mode, fingerprint, status FROM delete_files \
                WHERE batch_id=? AND status IN ('deleting', 'deleted') ORDER BY path
                """, [.text(batch)])
        }
        var pending: [(String, Data, Int)] = []
        for row in rows {
            let raw = row[0].stringValue, original = row[1].dataValue ?? Data()
            let path = try self.path(raw, missing: true)
            var isDirectory: ObjCBool = false
            guard fingerprint(original) == row[3].stringValue,
                  FileManager.default.fileExists(atPath: (path as NSString).deletingLastPathComponent,
                                                 isDirectory: &isDirectory), isDirectory.boolValue else {
                throw ToolError.rejected("삭제 백업 또는 복원 경계를 확인할 수 없습니다.")
            }
            var metadata = stat()
            if lstat(path, &metadata) == 0 {
                if row[4] == .text("deleting"), try read(path).0 == original { continue }  // unlink가 실행되지 않았음.
                throw ToolError.rejected("삭제 후 같은 경로에 파일이 생겨 복원하지 않았습니다.")
            }
            pending.append((path, original, Int(row[2].intValue ?? 0o600)))
        }
        var restored = 0
        for (path, original, mode) in pending {
            let parent = (path as NSString).deletingLastPathComponent
            var template = Array((parent + "/.voice-action-undo-XXXXXXXX").utf8CString)
            let descriptor = mkstemp(&template)
            guard descriptor >= 0 else { throw lastPOSIXError() }
            let temporary = String(decoding: template.dropLast().map { UInt8(bitPattern: $0) }, as: UTF8.self)
            defer { unlink(temporary) }
            do {
                try writeAll(descriptor, original, mode: mode)
                _ = try self.path(path, missing: true)
                guard link(temporary, path) == 0 else { throw lastPOSIXError() }  // 새 파일이 생겼으면 덮어쓰지 않고 실패.
                try fsyncDirectory(parent)
                try setStatus(batch, path, "restored")
                restored += 1
            } catch let error where error is ToolError || error is POSIXError || error is UndoJournalError {
                throw UndoJournalError("\(restored)개 복원 후 중단했습니다. 복원된 파일은 유지됩니다. Undo로 재시도하세요.")
            }
        }
        try journal.connect {
            try $0.execute("UPDATE delete_batches SET status='undone' WHERE batch_id=?", [.text(batch)])
        }
        return "삭제한 파일 \(restored)개를 복원했습니다."
    }
}

/// Writes, chmods and fsyncs then closes `descriptor`.
private func writeAll(_ descriptor: Int32, _ data: Data, mode: Int) throws(POSIXError) {
    defer { close(descriptor) }
    guard fchmod(descriptor, mode_t(mode)) == 0 else { throw lastPOSIXError() }
    var offset = 0
    while offset < data.count {
        let written = data.withUnsafeBytes { write(descriptor, $0.baseAddress! + offset, data.count - offset) }
        guard written > 0 else { throw lastPOSIXError() }
        offset += written
    }
    guard fsync(descriptor) == 0 else { throw lastPOSIXError() }
}

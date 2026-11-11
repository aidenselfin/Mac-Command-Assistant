import Foundation
import SymbioCore
import Testing
@testable import SymbioUndo

// Ported from tests/test_undo.py (journal-only cases).

private func temporaryDirectory() throws -> URL {
    let base = FileManager.default.temporaryDirectory.appendingPathComponent("undo-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    return URL(fileURLWithPath: realPath(base.path)!)
}

private func makeJournal() throws -> (UndoJournal, URL, URL) {
    let tmp = try temporaryDirectory()
    let root = tmp.appendingPathComponent("allowed")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return (try UndoJournal(path: tmp.appendingPathComponent("journal.sqlite3"), allowedRoots: [root]), root, tmp)
}

private func inject(
    _ journal: UndoJournal, path: URL, operation: String = "create", original: SQLiteValue = .null,
    expected: String = fingerprint(Data("written".utf8)), status: String = "active"
) throws {
    let db = try SQLiteDatabase(path: journal.path.path)
    try db.execute("""
        INSERT INTO undo_entries (
            command_id, canonical_path, operation, original_bytes,
            post_fingerprint, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            .text(UUID().uuidString.lowercased()), .text(path.path), .text(operation), original,
            .text(expected), .text(status), .integer(Int64(clock_gettime_nsec_np(CLOCK_REALTIME))),
        ])
}

private func write(_ text: String, _ url: URL) throws { try Data(text.utf8).write(to: url) }
private func exists(_ url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }
private func read(_ url: URL) throws -> String { String(decoding: try Data(contentsOf: url), as: UTF8.self) }

/// risk_guard.file_state
private func fileState(_ url: URL) throws -> String {
    let (data, metadata) = try readRegularFile(url.path)
    return "\(metadata.st_dev):\(metadata.st_ino):\(fingerprint(data))"
}

@Test func createWriteThenUndoRemovesOnlyCreatedFile() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("created.txt")
    _ = try journal.prepareWrite(target.path, operation: "create", content: "written")
    try write("written", target)

    let result = try journal.undoLastWrite()

    #expect(result == UndoResult(.createUndone, createUndoneMessage))
    #expect(!exists(target))
    #expect(try journal.entryCount(status: "undone") == 1)
}

@Test func journalRefusesToBackUpAFileOtherThanTheApprovedOne() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("existing.txt")
    try write("reviewed", target)
    let approved = try fileState(target)
    try write("changed after the approval check", target)

    #expect(throws: UndoJournalError.self) {
        try journal.prepareWrite(target.path, operation: "overwrite", content: "new", expectedState: approved)
    }
    #expect(try read(target) == "changed after the approval check")
    _ = try journal.prepareWrite(target.path, operation: "overwrite", content: "new",
                                 expectedState: try fileState(target))
}

@Test func overwriteThenUndoAtomicallyRestoresOriginalBytes() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("existing.txt")
    let original = Data("원본 😀\n".utf8)
    try original.write(to: target)
    _ = try journal.prepareWrite(target.path, operation: "overwrite", content: "새 내용")
    try write("새 내용", target)

    let result = try journal.undoLastWrite()

    #expect(result == UndoResult(.overwriteUndone, overwriteUndoneMessage))
    #expect(try Data(contentsOf: target) == original)
    let leftovers = try FileManager.default.contentsOfDirectory(atPath: root.path)
        .filter { $0.hasPrefix(".voice-action-undo-") }
    #expect(leftovers.isEmpty)
}

@Test func externalChangeConflictsAndPreservesChangedFile() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("changed.txt")
    _ = try journal.prepareWrite(target.path, operation: "create", content: "written")
    try write("externally changed", target)

    let result = try journal.undoLastWrite()

    #expect(result == UndoResult(.conflict, conflictMessage))
    #expect(try read(target) == "externally changed")
    #expect(try journal.entryCount(status: "conflict") == 1)
    #expect(try journal.undoLastWrite().message == nothingToUndoMessage)
}

@Test func injectedOutsideRootEntryIsRejectedWithoutFileChange() throws {
    let (journal, _, tmp) = try makeJournal()
    let outside = tmp.appendingPathComponent("outside.txt")
    try write("written", outside)
    try inject(journal, path: outside)

    let result = try journal.undoLastWrite()

    #expect(result == UndoResult(.boundaryError, boundaryErrorMessage))
    #expect(try read(outside) == "written")
    #expect(try journal.entryCount(status: "conflict") == 1)
}

@Test func symlinkTargetAndSymlinkJournalAreRejected() throws {
    let (journal, root, tmp) = try makeJournal()
    let outside = tmp.appendingPathComponent("outside.txt")
    try write("written", outside)
    let link = root.appendingPathComponent("link.txt")
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)
    try inject(journal, path: link)

    #expect(try journal.undoLastWrite().kind == .boundaryError)
    #expect(try read(outside) == "written")

    let realJournal = tmp.appendingPathComponent("real.sqlite3")
    try write("not a journal", realJournal)
    let journalLink = tmp.appendingPathComponent("linked.sqlite3")
    try FileManager.default.createSymbolicLink(at: journalLink, withDestinationURL: realJournal)
    let error = #expect(throws: UndoJournalError.self) {
        try UndoJournal(path: journalLink, allowedRoots: [root])
    }
    #expect(error?.message.contains("경계") == true)
}

@Test func nonRegularTargetIsRejectedWithoutRemoval() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("directory")
    try FileManager.default.createDirectory(at: target, withIntermediateDirectories: false)
    try inject(journal, path: target)

    #expect(try journal.undoLastWrite().kind == .boundaryError)
    var isDirectory: ObjCBool = false
    #expect(FileManager.default.fileExists(atPath: target.path, isDirectory: &isDirectory) && isDirectory.boolValue)
    #expect(try journal.entryCount(status: "conflict") == 1)
}

@Test func secondUndoIsNoopAndDoesNotChangeAnotherFile() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("created.txt")
    let untouched = root.appendingPathComponent("untouched.txt")
    try write("keep", untouched)
    _ = try journal.prepareWrite(target.path, operation: "create", content: "written")
    try write("written", target)

    #expect(try journal.undoLastWrite().message == createUndoneMessage)
    #expect(try journal.undoLastWrite().message == nothingToUndoMessage)
    #expect(!exists(target))
    #expect(try read(untouched) == "keep")
}

@Test func latestActiveWriteIsUndoneFirst() throws {
    let (journal, root, _) = try makeJournal()
    let first = root.appendingPathComponent("first.txt"), second = root.appendingPathComponent("second.txt")
    _ = try journal.prepareWrite(first.path, operation: "create", content: "first")
    try write("first", first)
    _ = try journal.prepareWrite(second.path, operation: "create", content: "second")
    try write("second", second)

    #expect(try journal.undoLastWrite().message == createUndoneMessage)
    #expect(exists(first) && !exists(second))
    #expect(try journal.undoLastWrite().message == createUndoneMessage)
    #expect(!exists(first))
}

@Test func nonUTF8OverwriteIsNotJournaled() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("binary.txt")
    try Data([0xFF, 0xFE]).write(to: target)

    let error = #expect(throws: UndoJournalError.self) {
        try journal.prepareWrite(target.path, operation: "overwrite", content: "new")
    }
    #expect(error?.message.contains("UTF-8") == true)
    #expect(try journal.entryCount() == 0)
}

@Test func sqliteUsesWALAndStoresOnlyRequiredWriteFields() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("created.txt")
    let entry = try journal.prepareWrite(target.path, operation: "create", content: "written")

    let db = try SQLiteDatabase(path: journal.path.path)
    #expect(try db.query("PRAGMA journal_mode").first?.first == .text("wal"))
    let row = try #require(try db.query("""
        SELECT command_id, canonical_path, operation, original_bytes,
               post_fingerprint, status, created_at
        FROM undo_entries
        """).first)

    #expect(Array(row.prefix(6)) == [
        .text(entry.commandID), .text(target.path), .text("create"), .null,
        .text(fingerprint(Data("written".utf8))), .text("active"),
    ])
    #expect(UUID(uuidString: entry.commandID) != nil && entry.commandID == entry.commandID.lowercased())
    #expect(row[6].intValue != nil)
}

// Swift-only: bytes are create-only, and sub-journals are dispatched by newest created_at.

@Test func byteContentIsOnlyAllowedForCreate() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("existing.txt")
    try write("old", target)
    #expect(throws: UndoJournalError("Undo journal은 텍스트 write와 새 파일 생성만 지원합니다.")) {
        try journal.prepareWrite(target.path, operation: "overwrite", content: Data("x".utf8))
    }
    _ = try journal.prepareWrite(root.appendingPathComponent("new.bin").path, operation: "create",
                                 content: Data([0xFF]))
}

private final class FakeSubJournal: SubJournal, @unchecked Sendable {
    let createdAt: Int64?
    let outcome: Result<String, any Error>
    var undone: [String] = []
    init(createdAt: Int64?, outcome: Result<String, any Error>) {
        self.createdAt = createdAt
        self.outcome = outcome
    }
    func latest() throws -> (id: String, createdAt: Int64)? { createdAt.map { ("entry", $0) } }
    func undo(_ id: String) throws -> String {
        undone.append(id)
        return try outcome.get()
    }
}

@Test func newestOfOperationDeleteAndWriteIsUndone() throws {
    let (journal, root, _) = try makeJournal()
    let target = root.appendingPathComponent("created.txt")
    _ = try journal.prepareWrite(target.path, operation: "create", content: "written")
    try write("written", target)
    let old = FakeSubJournal(createdAt: 1, outcome: .success("old"))
    journal.attachDeletions(old)
    journal.attachOperations(FakeSubJournal(createdAt: nil, outcome: .success("none")))

    #expect(try journal.undoLastWrite().kind == .createUndone)
    #expect(try journal.undoLastWrite() == UndoResult(.deleteUndone, "old"))

    journal.attachOperations(FakeSubJournal(createdAt: .max, outcome: .failure(ToolError.rejected("x"))))
    #expect(try journal.undoLastWrite() == UndoResult(
        .conflict, "파일 작업을 되돌리지 못했습니다 (ToolRejectedError). 경로·파일 변경·폴더가 비었는지 확인하세요."
    ))
    journal.attachOperations(FakeSubJournal(createdAt: nil, outcome: .success("none")))
    journal.attachDeletions(FakeSubJournal(createdAt: .max, outcome: .failure(ToolError.rejected("거부"))))
    #expect(try journal.undoLastWrite() == UndoResult(.conflict, "거부"))
    journal.attachDeletions(FakeSubJournal(createdAt: .max, outcome: .failure(POSIXError(.EIO))))
    #expect(throws: UndoJournalError("삭제 Undo journal을 처리할 수 없습니다.")) { try journal.undoLastWrite() }
}

@Test func sqliteDatabaseRoundTripsValuesAndRollsBack() throws {
    let db = try SQLiteDatabase(path: try temporaryDirectory().appendingPathComponent("t.sqlite3").path)
    try db.execute("CREATE TABLE t (v)")
    let values: [SQLiteValue] = [.null, .integer(-7), .real(1.5), .text("한글"), .blob(Data()), .blob(Data([0, 1]))]
    for value in values { try db.execute("INSERT INTO t VALUES (?)", [value]) }
    #expect(try db.query("SELECT v FROM t ORDER BY rowid").map { $0[0] } == values)
    #expect(throws: UndoConflict.self) {
        try db.transaction { try $0.execute("DELETE FROM t"); throw UndoConflict() }
    }
    #expect(try db.query("SELECT COUNT(*) FROM t") == [[.integer(6)]])
    #expect(try db.execute("UPDATE t SET v = 0 WHERE v IS NULL") == 1)
}

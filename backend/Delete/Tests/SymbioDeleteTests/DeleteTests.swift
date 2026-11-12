import Foundation
import SymbioCore
import SymbioUndo
import Testing
@testable import SymbioDelete

// Ported from tests/test_deletion.py (journal-level cases; RiskGuard/Planner/Runtime cases wait for those packages).

private struct Setup {
    let tmp: URL, root: URL, journal: UndoJournal, deletions: DeletionJournal
}

private func setup() throws -> Setup {
    let base = FileManager.default.temporaryDirectory.appendingPathComponent("delete-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    let tmp = URL(fileURLWithPath: realPath(base.path)!)
    let root = tmp.appendingPathComponent("allowed")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let journal = try UndoJournal(path: tmp.appendingPathComponent("undo.sqlite3"), allowedRoots: [root])
    return Setup(tmp: tmp, root: root, journal: journal, deletions: try DeletionJournal(journal))
}

private func reopen(_ journal: UndoJournal, _ root: URL) throws -> UndoJournal {
    let reopened = try UndoJournal(path: journal.path, allowedRoots: [root])
    _ = try DeletionJournal(reopened)
    return reopened
}

private func write(_ text: String, _ url: URL) throws { try Data(text.utf8).write(to: url) }
private func exists(_ url: URL) -> Bool {
    var metadata = stat()
    return lstat(url.path, &metadata) == 0
}
private func read(_ url: URL) throws -> String { String(decoding: try Data(contentsOf: url), as: UTF8.self) }
private func paths(_ urls: URL...) -> JSON { ["paths": .array(urls.map { .string($0.path) })] }

private func expectRejected(_ body: () throws -> Void) {
    let error = #expect(throws: ToolError.self) { try body() }
    #expect(error?.isRejected == true)
}

@Test func batchBinaryBackupModeAndOneShotUndo() throws {
    let s = try setup()
    let a = s.root.appendingPathComponent("a.txt"), b = s.root.appendingPathComponent("b.bin")
    try write("original", a)
    try Data([0xFF, 0x00]).write(to: b)
    chmod(a.path, 0o640)
    let prepared = try s.deletions.prepare(paths(a, b))
    #expect(exists(a) && exists(b))
    #expect(prepared.snapshots.map(\.path) == [a.path, b.path])
    #expect(prepared.normalized == paths(a, b))
    #expect(try s.deletions.execute(prepared.snapshots).ok)
    #expect(!exists(a) && !exists(b))
    // 앱을 재시작해도 같은 journal에서 복원할 수 있다.
    let reopened = try reopen(s.journal, s.root)
    #expect(try reopened.undoLastWrite().kind == .deleteUndone)
    #expect(try read(a) == "original" && Data(contentsOf: b) == Data([0xFF, 0x00]))
    var metadata = stat()
    stat(a.path, &metadata)
    #expect(metadata.st_mode & 0o777 == 0o640)
    #expect(try reopened.undoLastWrite().kind == .nothingToUndo)
}

@Test func changedFileIsNotDeletedAndNeedsANewPrepare() throws {
    let s = try setup()
    let target = s.root.appendingPathComponent("a.txt")
    try write("original", target)
    let prepared = try s.deletions.prepare(paths(target))
    try write("changed", target)
    expectRejected { _ = try s.deletions.execute(prepared.snapshots) }
    #expect(try read(target) == "changed")
    #expect(try s.deletions.execute(s.deletions.prepare(paths(target)).snapshots).ok)
    _ = try s.journal.undoLastWrite()
    #expect(try read(target) == "changed")
}

@Test func patternFreezesListAndSkipsDirectories() throws {
    let s = try setup()
    let target = s.root.appendingPathComponent("a.txt")
    try write("a", target)
    try FileManager.default.createDirectory(at: s.root.appendingPathComponent("nested.txt"), withIntermediateDirectories: false)
    try write("keep", s.root.appendingPathComponent("keep.bin"))
    let prepared = try s.deletions.prepare(["directory": .string(s.root.path), "pattern": "*.txt"])
    let added = s.root.appendingPathComponent("added.txt")
    try write("new", added)
    #expect(prepared.snapshots.map(\.path) == [target.path])
    #expect(try s.deletions.execute(prepared.snapshots).ok)
    #expect(exists(added) && exists(s.root.appendingPathComponent("keep.bin")))
    #expect(exists(s.root.appendingPathComponent("nested.txt")))
    #expect(try s.journal.undoLastWrite().kind == .deleteUndone)
}

@Test(arguments: ["outside", "symlink", "directory", "parent_link", "journal"])
func boundariesAreRejectedBeforeDeletion(kind: String) throws {
    let s = try setup()
    var deletions = s.deletions
    var target = s.root.appendingPathComponent("a.txt")
    try write("keep", target)
    switch kind {
    case "outside":
        target = s.tmp.appendingPathComponent("outside.txt")
        try write("keep", target)
    case "symlink":
        let link = s.root.appendingPathComponent("link.txt")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
        target = link
    case "directory":
        target = s.root
    case "parent_link":
        let link = s.root.appendingPathComponent("linked")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: s.root)
        target = link.appendingPathComponent("a.txt")
    default:
        // journal이 허용 폴더 안에 있어도 삭제를 금지한다.
        let journal = try UndoJournal(path: s.root.appendingPathComponent("undo.sqlite3"), allowedRoots: [s.root])
        deletions = try DeletionJournal(journal)
        target = journal.path
    }
    expectRejected { _ = try deletions.prepare(paths(target)) }
    #expect(exists(target))
}

@Test(arguments: [
    #"{"paths": []}"#,
    #"{"paths": ["relative.txt"]}"#,
    #"{"paths": ["x", "x"]}"#,
    #"{"directory": "ROOT", "pattern": "**/*.txt"}"#,
    #"{"directory": "ROOT", "pattern": "../*"}"#,
    #"{"directory": "ROOT", "pattern": "*.missing"}"#,
    #"{"paths": ["ROOT/a.txt"], "directory": "ROOT"}"#,
])
func invalidSelectorsRejected(arguments: String) throws {
    let s = try setup()
    try write("a", s.root.appendingPathComponent("a.txt"))
    let parsed = try JSON.parse(arguments.replacingOccurrences(of: "ROOT", with: s.root.path))
    expectRejected { _ = try s.deletions.prepare(parsed) }
}

@Test func limitsRejectedWithoutDeleting() throws {
    let s = try setup()
    for index in 0..<101 { try write("x", s.root.appendingPathComponent("\(index).txt")) }
    expectRejected { _ = try s.deletions.prepare(["directory": .string(s.root.path), "pattern": "*.txt"]) }
    let huge = s.root.appendingPathComponent("huge.bin")
    FileManager.default.createFile(atPath: huge.path, contents: nil)
    truncate(huge.path, off_t(maxDeleteBytes + 1))
    expectRejected { _ = try s.deletions.prepare(paths(huge)) }
    #expect(try FileManager.default.contentsOfDirectory(atPath: s.root.path).count == 102)
}

@Test func batchUndoConflictPreventsOverwritingOrPartialRestore() throws {
    let s = try setup()
    let a = s.root.appendingPathComponent("a.txt"), b = s.root.appendingPathComponent("b.txt")
    for path in [a, b] { try write("original", path) }
    _ = try s.deletions.execute(s.deletions.prepare(paths(a, b)).snapshots)
    try write("new file", b)
    #expect(try s.journal.undoLastWrite().kind == .conflict)
    #expect(try !exists(a) && read(b) == "new file")
    try FileManager.default.removeItem(at: b)
    #expect(try s.journal.undoLastWrite().kind == .deleteUndone)
    #expect(try read(a) == "original" && read(b) == "original")
}

@Test func existingWriteEntriesAndDeleteUndoFollowTimeOrder() throws {
    let s = try setup()
    let old = s.root.appendingPathComponent("old.txt"), target = s.root.appendingPathComponent("target.txt")
    let new = s.root.appendingPathComponent("new.txt")
    _ = try s.journal.prepareWrite(old.path, operation: "create", content: "old")
    try write("old", old)
    try write("target", target)
    _ = try s.deletions.execute(s.deletions.prepare(paths(target)).snapshots)
    _ = try s.journal.prepareWrite(new.path, operation: "create", content: "new")
    try write("new", new)
    let reopened = try reopen(s.journal, s.root)
    #expect(try reopened.undoLastWrite().kind == .createUndone)
    #expect(try reopened.undoLastWrite().kind == .deleteUndone)
    #expect(try reopened.undoLastWrite().kind == .createUndone)
    #expect(try read(target) == "target" && !exists(old) && !exists(new))
}

@Test func partialFailurePreservesBackupAndUndoOnlyDeletedFiles() throws {
    let s = try setup()
    let a = s.root.appendingPathComponent("a.txt"), b = s.root.appendingPathComponent("b.txt")
    for path in [a, b] { try write("original", path) }
    // Python monkeypatches os.unlink; an immutable flag makes the real unlink fail with EPERM.
    #expect(chflags(b.path, UInt32(UF_IMMUTABLE)) == 0)
    defer { chflags(b.path, 0) }
    let result = try s.deletions.execute(s.deletions.prepare(paths(a, b)).snapshots)
    #expect(!result.ok && result.error?.contains("1/2") == true)
    #expect(result.error?.contains("(PermissionError)") == true)
    #expect(!exists(a) && exists(b))
    #expect(try s.journal.undoLastWrite().kind == .deleteUndone)
    #expect(try read(a) == "original" && read(b) == "original")
}

@Test func backupFailureNeverDeletesFiles() throws {
    let s = try setup()
    let target = s.root.appendingPathComponent("a.txt")
    try write("original", target)
    let prepared = try s.deletions.prepare(paths(target))
    // The journal path is no longer a regular file: every connection is refused.
    try FileManager.default.removeItem(at: s.journal.path)
    try FileManager.default.createDirectory(at: s.journal.path, withIntermediateDirectories: false)
    #expect(throws: UndoJournalError.self) { _ = try s.deletions.execute(prepared.snapshots) }
    #expect(try read(target) == "original")
}

@Test func partialRestoreCanBeRetriedWithoutOverwritingRestoredFiles() throws {
    let s = try setup()
    let a = s.root.appendingPathComponent("a.txt"), b = s.root.appendingPathComponent("b.txt")
    for path in [a, b] { try write("original", path) }
    _ = try s.deletions.execute(s.deletions.prepare(paths(a, b)).snapshots)
    // Python injects a link failure on b; here a's restore is recorded as the first undo would.
    try write("user edited restored file", a)
    try s.journal.connect {
        try $0.execute("UPDATE delete_files SET status='restored' WHERE path=?", [.text(a.path)])
    }
    #expect(try s.journal.undoLastWrite().kind == .deleteUndone)
    #expect(try read(a) == "user edited restored file" && read(b) == "original")
}

@Test func restoreNeverOverwritesAFileCreatedAfterTheCheck() throws {
    let s = try setup()
    let a = s.root.appendingPathComponent("a.txt")
    try write("original", a)
    _ = try s.deletions.execute(s.deletions.prepare(paths(a)).snapshots)
    let batch = try #require(try s.deletions.latest())
    try write("raced", a)
    expectRejected { _ = try s.deletions.undo(batch.id) }
    #expect(try read(a) == "raced")
}

@Test func approvalPreviewsTextHeadAndBinaryKind() throws {
    let s = try setup()
    try write("첫 줄\n\n둘째\n셋째\n넷째\n", s.root.appendingPathComponent("a.txt"))
    try (Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00]) + Data(repeating: 0x78, count: 2048))
        .write(to: s.root.appendingPathComponent("b.png"))
    try Data().write(to: s.root.appendingPathComponent("c.txt"))
    try write(String(repeating: "가", count: 3000), s.root.appendingPathComponent("d.txt"))  // 4096에서 글자가 잘린다
    let prepared = try s.deletions.prepare(["directory": .string(s.root.path), "pattern": "*"])
    let previews = prepared.previews
    #expect(previews.count == 4)
    #expect(previews[0] == "첫 줄\n둘째\n셋째\n…")
    #expect(previews[1] == "바이너리 · image/png · 2.0 KB")
    #expect(previews[2] == "(빈 파일)")
    #expect(previews[3].hasSuffix("\n…") && previews[3].split(separator: "\n")[0].count == 80)
    #expect(!prepared.normalized.text.contains("첫 줄"))
    #expect(filePreview("x.txt", Data("  \n\t".utf8)) == "(공백만 있음)")
}

@Test func approvalPreviewsOnlyTheFirstFiles() throws {
    let s = try setup()
    for index in 0..<(previewFiles + 3) {
        try write("줄 \(index)", s.root.appendingPathComponent(String(format: "%02d.txt", index)))
    }
    let prepared = try s.deletions.prepare(["directory": .string(s.root.path), "pattern": "*.txt"])
    #expect(prepared.snapshots.count == previewFiles + 3)
    #expect(prepared.previews == (0..<previewFiles).map { "줄 \($0)" })
}

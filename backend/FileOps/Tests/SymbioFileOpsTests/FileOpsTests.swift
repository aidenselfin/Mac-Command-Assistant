import Foundation
import SymbioCore
import SymbioUndo
import Testing
@testable import SymbioFileOps

// Ported from tests/test_file_operations.py (journal-level cases; RiskGuard/Planner/Runtime cases wait for those packages).

private struct Setup {
    let tmp: URL, root: URL, source: URL, folder: URL, journal: UndoJournal, operations: FileOperationJournal
}

private func setup() throws -> Setup {
    let base = FileManager.default.temporaryDirectory.appendingPathComponent("fileops-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    let tmp = URL(fileURLWithPath: realPath(base.path)!)
    let root = tmp.appendingPathComponent("allowed")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let source = root.appendingPathComponent("source.bin")
    try Data([0xFF, 0x00, 0xFE]).write(to: source)
    chmod(source.path, 0o640)
    let folder = root.appendingPathComponent("보관")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let journal = try UndoJournal(path: tmp.appendingPathComponent("undo.sqlite3"), allowedRoots: [root])
    return Setup(tmp: tmp, root: root, source: source, folder: folder, journal: journal,
                 operations: try FileOperationJournal(journal))
}

private func exists(_ url: URL) -> Bool {
    var metadata = stat()
    return lstat(url.path, &metadata) == 0
}
private func isDir(_ url: URL) -> Bool {
    var metadata = stat()
    return lstat(url.path, &metadata) == 0 && (metadata.st_mode & S_IFMT) == S_IFDIR
}
private func contents(_ url: URL) throws -> [String] { try FileManager.default.contentsOfDirectory(atPath: url.path) }
private func transfer(_ source: URL, _ destination: URL) -> JSON {
    ["source": .string(source.path), "destination": .string(destination.path)]
}

/// prepare + execute, like an approved RiskGuard call.
@discardableResult
private func run(_ s: Setup, _ tool: String, _ arguments: JSON) throws -> ToolResult {
    let prepared = try s.operations.prepare(tool, arguments)
    return try s.operations.execute(tool, prepared.normalized, prepared.snapshot)
}

@Test(arguments: [moveTool, copyTool])
func approvedTransferPreservesBinaryModeAndUndoAfterRestart(tool: String) throws {
    let s = try setup()
    let prepared = try s.operations.prepare(tool, transfer(s.source, s.folder))
    let destination = s.folder.appendingPathComponent(s.source.lastPathComponent)
    #expect(prepared.normalized == transfer(s.source, destination))
    #expect(exists(s.source) && !exists(destination))
    #expect(try s.operations.execute(tool, prepared.normalized, prepared.snapshot).ok)
    #expect(try Data(contentsOf: destination) == Data([0xFF, 0x00, 0xFE]))
    var metadata = stat()
    stat(destination.path, &metadata)
    #expect(metadata.st_mode & 0o777 == 0o640)
    #expect(exists(s.source) == (tool == copyTool))
    #expect(throws: (any Error).self) { try s.operations.execute(tool, prepared.normalized, prepared.snapshot) }
    let reopened = try UndoJournal(path: s.journal.path, allowedRoots: [s.root])
    _ = try FileOperationJournal(reopened)
    #expect([.operationUndone, .createUndone].contains(try reopened.undoLastWrite().kind))
    #expect(try Data(contentsOf: s.source) == Data([0xFF, 0x00, 0xFE]) && !exists(destination))
}

@Test func mkdirUndoIsLatestAndRefusesNonemptyOrReplacedDirectory() throws {
    let s = try setup()
    let destination = s.folder.appendingPathComponent("새폴더")
    let prepared = try s.operations.prepare(mkdirTool, ["path": .string(destination.path)])
    #expect(!exists(destination))
    _ = try s.operations.execute(mkdirTool, prepared.normalized, prepared.snapshot)
    let occupant = destination.appendingPathComponent("user.txt")
    try Data("사용자 자료".utf8).write(to: occupant)
    #expect(try s.journal.undoLastWrite().kind == .conflict)
    #expect(try Data(contentsOf: occupant) == Data("사용자 자료".utf8))
    unlink(occupant.path)
    try run(s, moveTool, transfer(s.source, destination))
    #expect(try s.journal.undoLastWrite().kind == .operationUndone)
    #expect(exists(s.source) && isDir(destination))
    #expect(try s.journal.undoLastWrite().kind == .operationUndone)
    #expect(!exists(destination))
}

@Test(arguments: ["outside", "symlink", "parent_link", "directory", "existing"])
func transferBoundariesAndNoOverwrite(_ change: String) throws {
    let s = try setup()
    var source = s.source, destination = s.folder.appendingPathComponent("target.bin")
    let outside = s.tmp.appendingPathComponent("outside.bin")
    try Data("outside".utf8).write(to: outside)
    switch change {
    case "outside": destination = outside
    case "symlink":
        unlink(source.path)
        symlink(outside.path, source.path)
    case "parent_link":
        rmdir(s.folder.path)
        symlink(s.tmp.path, s.folder.path)
    case "directory": source = s.folder
    default: try Data("existing".utf8).write(to: destination)
    }
    #expect(throws: (any Error).self) { try s.operations.prepare(moveTool, transfer(source, destination)) }
    #expect(try Data(contentsOf: outside) == Data("outside".utf8))
    if change == "existing" { #expect(try Data(contentsOf: destination) == Data("existing".utf8)) }
}

@Test func sourceChangesRequireReapprovalAndDestinationConflictStopsMove() throws {
    let s = try setup()
    let arguments = transfer(s.source, s.folder)
    let prepared = try s.operations.prepare(moveTool, arguments)
    try Data("new".utf8).write(to: s.source)
    let changed = #expect(throws: ToolError.self) {
        try s.operations.execute(moveTool, prepared.normalized, prepared.snapshot)
    }
    #expect(changed?.isRejected == true)
    #expect(try exists(s.source) && contents(s.folder).isEmpty)
    let refreshed = try s.operations.prepare(moveTool, arguments)
    let target = s.folder.appendingPathComponent(s.source.lastPathComponent)
    try Data("user file".utf8).write(to: target)
    let conflict = #expect(throws: ToolError.self) {
        try s.operations.execute(moveTool, refreshed.normalized, refreshed.snapshot)
    }
    #expect(conflict?.isRejected == true)
    #expect(try Data(contentsOf: s.source) == Data("new".utf8) && Data(contentsOf: target) == Data("user file".utf8))
}

@Test(arguments: ["source_taken", "destination_edited", "destination_replaced"])
func moveUndoNeverOverwritesUserChanges(_ change: String) throws {
    let s = try setup()
    try run(s, moveTool, transfer(s.source, s.folder))
    let destination = s.folder.appendingPathComponent(s.source.lastPathComponent)
    switch change {
    case "source_taken": try Data("new source".utf8).write(to: s.source)
    case "destination_edited": try Data("edited".utf8).write(to: destination)
    default:
        unlink(destination.path)
        try Data("replacement".utf8).write(to: destination)
    }
    #expect(try s.journal.undoLastWrite().kind == .conflict)
    #expect(exists(destination))
    if change == "source_taken" { #expect(try Data(contentsOf: s.source) == Data("new source".utf8)) }
}

@Test func partialMoveIsJournaledAndUndoKeepsTheOriginal() throws {
    let s = try setup()
    let prepared = try s.operations.prepare(moveTool, transfer(s.source, s.folder))
    // Python injects an unlink failure; a read-only source folder makes the source unlink fail the same way.
    chmod(s.root.path, 0o555)
    defer { chmod(s.root.path, 0o755) }
    #expect(throws: POSIXError.self) { try s.operations.execute(moveTool, prepared.normalized, prepared.snapshot) }
    let destination = s.folder.appendingPathComponent(s.source.lastPathComponent)
    #expect(try Data(contentsOf: s.source) == Data(contentsOf: destination))
    chmod(s.root.path, 0o755)
    #expect(try s.journal.undoLastWrite().kind == .operationUndone)
    #expect(try Data(contentsOf: s.source) == Data([0xFF, 0x00, 0xFE]) && !exists(destination))
}

@Test func mkdirUndoDoesNotRemoveAReplacement() throws {
    let s = try setup()
    let destination = s.folder.appendingPathComponent("new")
    try run(s, mkdirTool, ["path": .string(destination.path)])
    rename(destination.path, s.folder.appendingPathComponent("original").path)
    mkdir(destination.path, 0o755)
    #expect(try s.journal.undoLastWrite().kind == .conflict)
    #expect(isDir(destination))
}

@Test func journalFailureHappensBeforeAnyFileChange() throws {
    let s = try setup()
    let prepared = try s.operations.prepare(moveTool, transfer(s.source, s.folder))
    // Python patches `_connect`; replacing the journal file with a folder breaks every connection.
    for suffix in ["", "-wal", "-shm"] { unlink(s.journal.path.path + suffix) }
    mkdir(s.journal.path.path, 0o755)
    #expect(throws: UndoJournalError.self) {
        try s.operations.execute(moveTool, prepared.normalized, prepared.snapshot)
    }
    #expect(try exists(s.source) && contents(s.folder).isEmpty)
}

@Test func definitionsKeepPythonOrderAndOperations() {
    #expect(fileOperationDefinitions.map(\.name) == [moveTool, copyTool, mkdirTool])
    #expect(fileOperations == [moveTool: "move", copyTool: "copy", mkdirTool: "mkdir"])
}

@Test func snapshotJSONMatchesPythonTuple() throws {
    let s = try setup()
    let mkdir = try s.operations.prepare(mkdirTool, ["path": .string(s.folder.appendingPathComponent("x").path)])
    #expect(mkdir.snapshot.json.arrayValue?.first == [])
    #expect(mkdir.snapshot.json.arrayValue?[1] == [])
    #expect(mkdir.snapshot.json.arrayValue?[2].arrayValue?.count == 2)
}

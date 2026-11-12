import Foundation
import SymbioConvert
import SymbioCore
import SymbioDelete
import SymbioFileOps
import SymbioFinder
import SymbioMCP
import SymbioSearch
import SymbioUndo
import Testing
@testable import SymbioGuard

// Ported from tests/test_risk_guard.py and the RiskGuard cases of test_undo, test_deletion,
// test_file_operations, test_conversion, test_finder, test_list_files and test_file_grouping.

struct Uncertain: Error {}

/// Python test gateways: records calls, writes `write_file` content unless told otherwise.
actor FakeGateway: ToolGateway {
    enum Mode { case write, failedResult, uncertain(String?), server }
    let mode: Mode
    private(set) var calls: [(String, JSON)] = []
    init(_ mode: Mode = .write) { self.mode = mode }

    var callNames: [String] { calls.map(\.0) }

    func listTools() async throws -> [ToolDefinition] { [] }
    func validateResultPath(_ rawPath: String) async throws -> URL { URL(fileURLWithPath: rawPath) }
    func normalizeToolArguments(_ name: String, _ arguments: JSON) async throws -> JSON { arguments }

    func callTool(_ name: String, _ arguments: JSON) async throws -> ToolResult {
        calls.append((name, arguments))
        let path = arguments["path"]?.stringValue ?? ""
        switch mode {
        case .server: return ToolResult(ok: true, text: "server")
        case .failedResult: return ToolResult(ok: false, text: "failed", error: "failed")
        case let .uncertain(written):
            if let written { try Data(written.utf8).write(to: URL(fileURLWithPath: path)) }
            throw Uncertain()
        case .write:
            if name == "filesystem__write_file" {
                try Data((arguments["content"]?.stringValue ?? "").utf8).write(to: URL(fileURLWithPath: path))
            }
            return ToolResult(ok: true, text: "ok")
        }
    }
}

final class FakeWorkspace: Workspace {
    var opened: [String] = []
    func activateFileViewerSelecting(_ fileURLs: [URL]) throws {}
    func open(_ url: URL) -> Bool {
        opened.append(url.path)
        return true
    }
}

/// A fresh, already-resolved temp folder (/var → /private/var).
func tempDir() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("guard-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return URL(fileURLWithPath: realPath(url.path)!)
}

func child(_ base: URL, _ name: String, directory: Bool = false) throws -> URL {
    let url = base.appendingPathComponent(name)
    if directory { try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true) }
    return url
}

func write(_ text: String, _ url: URL) throws { try Data(text.utf8).write(to: url) }
func read(_ url: URL) throws -> String { String(decoding: try Data(contentsOf: url), as: UTF8.self) }
func exists(_ url: URL) -> Bool {
    var metadata = stat()
    return lstat(url.path, &metadata) == 0
}
func contents(_ url: URL) throws -> [String] { try FileManager.default.contentsOfDirectory(atPath: url.path) }

/// Python's patched `_connect`: a folder at the journal path breaks every connection.
func breakJournal(_ journal: UndoJournal) {
    for suffix in ["", "-wal", "-shm"] { unlink(journal.path.path + suffix) }
    mkdir(journal.path.path, 0o755)
}

/// Python `ToolRejectedError`: a guard error or a rejected `ToolError`.
func isRejected(_ error: any Error) -> Bool {
    error is any RiskGuardFailure || (error as? ToolError)?.isRejected == true
}

func expectRejected(_ body: () async throws -> Void, sourceLocation: SourceLocation = #_sourceLocation) async {
    do {
        try await body()
        Issue.record("expected a rejection", sourceLocation: sourceLocation)
    } catch {
        #expect(isRejected(error), "\(error)", sourceLocation: sourceLocation)
    }
}

let writeFile = "filesystem__write_file"
func writeArguments(_ path: URL, _ content: String) -> JSON { ["path": .string(path.path), "content": .string(content)] }

// MARK: - test_risk_guard.py

@Test func fixedPolicyDoesNotDependOnMCPAnnotations() {
    #expect(readOnlyTools.allSatisfy { classifyTool($0) == .readOnly })
    #expect(classifyTool("filesystem__write_file") == .write)
    #expect(classifyTool("web__browser_click") == .forbidden)
    #expect(classifyTool("filesystem__delete_file") == .forbidden)
    // test_list_files / test_file_grouping / test_finder
    #expect(classifyTool("local__list_files") == .readOnly)
    #expect(classifyTool("local__group_files") == .readOnly)
    #expect(classifyTool(openFileTool) == .readOnly)
}

@Test func canonicalJSONMatchesPythonDumps() {
    let value: JSON = ["path": "/a", "content": "한\"\n", "n": [1, 2.5, nil, true]]
    #expect(canonicalJSON(value) == #"{"content":"한\"\n","n":[1,2.5,null,true],"path":"/a"}"#)
}

@Test func canonicalDigestIsStableAndNonceIsOneShot() async throws {
    let tmp = try tempDir()
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway, try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [tmp]))
    let target = try child(tmp, "new.txt")
    let content = String(repeating: "한", count: 201) + "SECRET-TAIL"
    let first = try await guard_.prepareWrite(writeFile, ["content": .string(content), "path": .string(target.path)])
    let second = try await guard_.prepareWrite(writeFile, ["path": .string(target.path), "content": .string(content)])

    #expect(first.digest == second.digest)
    #expect(first.nonce != second.nonce)
    #expect(first.nonce.count == 43)  // token_urlsafe(32)
    #expect(first.contentChars == 212 && first.previewTruncated && first.preview.count == previewLimit)
    for shown in [String(describing: first), String(reflecting: first)] {
        #expect(!shown.contains(content) && !shown.contains("SECRET-TAIL"))
    }
    _ = try await guard_.executeApproved(first)
    await #expect(throws: ApprovalInvalidError.self) { try await guard_.executeApproved(first) }
    let calls = await gateway.calls
    #expect(calls.count == 1 && calls[0].0 == writeFile)
    #expect(calls[0].1 == ["content": .string(content), "path": .string(target.path)])
}

@Test func rejectedNonceCannotBeReused() async throws {
    let tmp = try tempDir()
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway, try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [tmp]))
    let prepared = try await guard_.prepareWrite(writeFile, writeArguments(try child(tmp, "no.txt"), "no"))

    guard_.reject(prepared)

    await #expect(throws: ApprovalInvalidError.self) { try await guard_.executeApproved(prepared) }
    #expect(await gateway.calls.isEmpty)
}

@Test func forbiddenToolHasNoApprovalOrGatewayCall() async throws {
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway)

    await #expect(throws: ForbiddenToolError.self) {
        try await guard_.executeReadOnly("web__browser_click", ["ref": "button"])
    }
    await #expect(throws: ForbiddenToolError.self) {
        try await guard_.prepareWrite("filesystem__delete_file", ["path": "/tmp/x"])
    }
    await #expect(throws: RiskGuardError.self) { try await guard_.executeReadOnly(writeFile, [:]) }
    await #expect(throws: RiskGuardError.self) { try await guard_.prepareWrite("local__list_files", [:]) }
    #expect(await gateway.calls.isEmpty)
}

@Test(arguments: ["content", "same-path-new-inode", "symlink"])
func overwriteApprovalIsBoundToTheReviewedFile(change: String) async throws {
    let tmp = try tempDir()
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway, try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [tmp]))
    let target = try child(tmp, "changed.txt")
    try write("approved old version", target)
    let arguments = writeArguments(target, "planned output")
    let prepared = try await guard_.prepareWrite(writeFile, arguments)
    #expect(prepared.operation == "overwrite")
    #expect(!String(reflecting: prepared).contains("approved old version"))

    switch change {
    case "content":
        try write("external new version", target)
    case "same-path-new-inode":
        let text = try read(target)
        unlink(target.path)
        try write(text, target)
    default:
        let real = try child(tmp, "real.txt")
        try write(try read(target), real)
        unlink(target.path)
        symlink(real.path, target.path)
    }

    do {
        _ = try await guard_.executeApproved(prepared)
        Issue.record("stale approval executed")
    } catch let changed as ApprovalChangedError {
        #expect(await gateway.calls.isEmpty)  // the stale approval wrote nothing
        _ = try await guard_.executeApproved(changed.prepared)  // a fresh approval works
        let calls = await gateway.calls
        #expect(calls.count == 1 && calls[0].1 == arguments)
    } catch is RiskGuardError {
        #expect(await gateway.calls.isEmpty)
    }
}

@Test func unchangedOverwriteStillExecutes() async throws {
    let tmp = try tempDir()
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway, try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [tmp]))
    let target = try child(tmp, "same.txt")
    try write("before", target)
    let arguments = writeArguments(target, "after")
    _ = try await guard_.executeApproved(try await guard_.prepareWrite(writeFile, arguments))
    let calls = await gateway.calls
    #expect(calls.count == 1 && calls[0].1 == arguments)
}

@Test func hugeFileIsReadFromItsStartWithoutTheFilesystemServer() async throws {
    let root = try tempDir()
    let huge = try child(root, "huge.txt")
    try write(String(repeating: "가", count: 1000) + "끝", huge)
    let gateway = FakeGateway(.server)
    let guard_ = RiskGuard(gateway, nil, roots: [root], readLimit: 100)

    let result = try await guard_.executeReadOnly("filesystem__read_text_file", ["path": .string(huge.path)])

    #expect(result.truncated && result.text.hasSuffix(truncationMarker))
    #expect(result.text.hasPrefix(String(repeating: "가", count: 100)) && !result.text.contains("끝"))
    #expect(await gateway.calls.isEmpty)
}

@Test func smallHeadTailAndOddPathsStillGoThroughTheServer() async throws {
    let root = try tempDir()
    let small = try child(root, "small.txt"), huge = try child(root, "huge.txt"), link = try child(root, "link.txt")
    try write("작은 글", small)
    try write(String(repeating: "x", count: 5000), huge)
    symlink(huge.path, link.path)
    let gateway = FakeGateway(.server)
    let guard_ = RiskGuard(gateway, nil, roots: [root], readLimit: 100)

    let cases: [JSON] = [
        ["path": .string(small.path)],
        ["path": .string(huge.path), "head": 5],
        ["path": .string(huge.path), "tail": 5],
        ["path": .string(link.path)],  // a link is not read here; the server decides
        ["path": .string(root.appendingPathComponent("missing.txt").path)],
        ["path": .string(root.deletingLastPathComponent().appendingPathComponent("elsewhere.txt").path)],
    ]
    for (index, arguments) in cases.enumerated() {
        let result = try await guard_.executeReadOnly("filesystem__read_text_file", arguments)
        #expect(result.text == "server", "\(arguments.text)")
        #expect(await gateway.calls.count == index + 1)
    }
}

// MARK: - test_undo.py

private func undoSetup() throws -> (UndoJournal, URL) {
    let tmp = try tempDir()
    let root = try child(tmp, "allowed", directory: true)
    return (try UndoJournal(path: try child(tmp, "journal.sqlite3"), allowedRoots: [root]), root)
}

@Test func journalPrepareFailureBlocksUnderlyingWrite() async throws {
    let (journal, root) = try undoSetup()
    let gateway = FakeGateway()
    let guard_ = RiskGuard(gateway, journal)
    let target = try child(root, "blocked.txt")
    let prepared = try await guard_.prepareWrite(writeFile, writeArguments(target, "blocked"))
    breakJournal(journal)

    let error = await #expect(throws: RiskGuardError.self) { try await guard_.executeApproved(prepared) }

    #expect(error?.message.contains("journal") == true)
    #expect(error?.message.contains("sqlite") == false)
    #expect(await gateway.calls.isEmpty)
    #expect(!exists(target))
}

@Test func onlyApprovedWriteCreatesEntryAndRejectionReadDoNot() async throws {
    let (journal, root) = try undoSetup()
    let guard_ = RiskGuard(FakeGateway(), journal)
    let target = try child(root, "approved.txt")
    let rejected = try await guard_.prepareWrite(writeFile, writeArguments(target, "no"))
    guard_.reject(rejected)
    _ = try await guard_.executeReadOnly("filesystem__list_directory", ["path": .string(root.path)])
    let approved = try await guard_.prepareWrite(writeFile, writeArguments(target, "yes"))

    #expect(try journal.entryCount() == 0)
    _ = try await guard_.executeApproved(approved)

    #expect(try journal.entryCount(status: "active") == 1)
    #expect(try read(target) == "yes")
}

@Test func clearWriteFailureIsExcludedFromUndoCandidates() async throws {
    let (journal, root) = try undoSetup()
    let guard_ = RiskGuard(FakeGateway(.failedResult), journal)
    let prepared = try await guard_.prepareWrite(writeFile, writeArguments(try child(root, "failed.txt"), "failed"))

    let result = try await guard_.executeApproved(prepared)

    #expect(!result.ok)
    #expect(try journal.entryCount(status: "failed") == 1)
    #expect(try journal.undoLastWrite().message == nothingToUndoMessage)
}

@Test(arguments: [(nil, "failed"), ("expected", "active"), ("different", "failed")] as [(String?, String)])
func uncertainTransportIsActiveOnlyForExactFingerprint(written: String?, status: String) async throws {
    let (journal, root) = try undoSetup()
    let gateway = FakeGateway(.uncertain(written))
    let guard_ = RiskGuard(gateway, journal)
    let target = try child(root, "uncertain-\(status)-\(written ?? "None").txt")
    let prepared = try await guard_.prepareWrite(writeFile, writeArguments(target, "expected"))

    await #expect(throws: Uncertain.self) { try await guard_.executeApproved(prepared) }

    #expect(try journal.entryCount(status: status) == 1)
}

// MARK: - test_deletion.py

private func deleteSetup(_ journalInRoot: Bool = false) throws -> (URL, UndoJournal, RiskGuard) {
    let tmp = try tempDir()
    let root = try child(tmp, "allowed", directory: true)
    let journal = try UndoJournal(path: try child(journalInRoot ? root : tmp, "undo.sqlite3"), allowedRoots: [root])
    return (root, journal, RiskGuard(FakeGateway(), journal))
}

private func paths(_ urls: URL...) -> JSON { ["paths": .array(urls.map { .string($0.path) })] }

@Test func batchApprovalBinaryBackupModeAndOneShotUndo() async throws {
    let (root, journal, guard_) = try deleteSetup()
    let a = try child(root, "a.txt"), b = try child(root, "b.bin")
    try write("original", a)
    try Data([0xFF, 0x00]).write(to: b)
    chmod(a.path, 0o640)
    let prepared = try await guard_.prepareWrite(deleteTool, paths(a, b))
    #expect(exists(a) && exists(b))
    #expect(prepared.targets == [a.path, b.path] && prepared.targetPath == "2개 파일")
    #expect(try await guard_.executeApproved(prepared).ok)
    #expect(!exists(a) && !exists(b))
    await #expect(throws: ApprovalInvalidError.self) { try await guard_.executeApproved(prepared) }
    // 앱을 재시작해도 같은 journal에서 복원할 수 있다.
    let reopened = try UndoJournal(path: journal.path, allowedRoots: [root])
    _ = try DeletionJournal(reopened)
    #expect(try reopened.undoLastWrite().kind == .deleteUndone)
    #expect(try read(a) == "original" && Data(contentsOf: b) == Data([0xFF, 0x00]))
    var metadata = stat()
    stat(a.path, &metadata)
    #expect(metadata.st_mode & 0o777 == 0o640)
    #expect(try reopened.undoLastWrite().kind == .nothingToUndo)
}

@Test func rejectLeavesAllFilesUnchanged() async throws {
    let (root, _, guard_) = try deleteSetup()
    let target = try child(root, "a.txt")
    try write("original", target)
    let prepared = try await guard_.prepareWrite(deleteTool, paths(target))
    guard_.reject(prepared)
    await #expect(throws: ApprovalInvalidError.self) { try await guard_.executeApproved(prepared) }
    #expect(try read(target) == "original")
}

@Test func changedFileRequiresNewApprovalAndOldNonceIsConsumed() async throws {
    let (root, journal, guard_) = try deleteSetup()
    let target = try child(root, "a.txt")
    try write("original", target)
    let prepared = try await guard_.prepareWrite(deleteTool, paths(target))
    try write("changed", target)
    let changed = await #expect(throws: ApprovalChangedError.self) { try await guard_.executeApproved(prepared) }
    #expect(try read(target) == "changed")
    await #expect(throws: ApprovalInvalidError.self) { try await guard_.executeApproved(prepared) }
    #expect(try await guard_.executeApproved(try #require(changed).prepared).ok)
    _ = try journal.undoLastWrite()
    #expect(try read(target) == "changed")
}

@Test(arguments: ["outside", "symlink", "directory", "parent_link", "journal"])
func boundariesAreRejectedBeforeDeletion(kind: String) async throws {
    let (root, journal, guard_) = try deleteSetup(kind == "journal")
    var target = try child(root, "a.txt")
    try write("keep", target)
    switch kind {
    case "outside":
        target = root.deletingLastPathComponent().appendingPathComponent("outside.txt")
        try write("keep", target)
    case "symlink":
        let link = try child(root, "link.txt")
        symlink(target.path, link.path)
        target = link
    case "directory":
        target = root
    case "parent_link":
        let link = try child(root, "linked")
        symlink(root.path, link.path)
        target = link.appendingPathComponent("a.txt")
    default:
        target = journal.path  // journal이 허용 폴더 안에 있어도 삭제를 금지한다.
    }
    await expectRejected { _ = try await guard_.prepareWrite(deleteTool, paths(target)) }
    #expect(exists(target))
}

@Test func backupFailureNeverDeletesFiles() async throws {
    let (root, journal, guard_) = try deleteSetup()
    let target = try child(root, "a.txt")
    try write("original", target)
    let pending = try await guard_.prepareWrite(deleteTool, paths(target))
    breakJournal(journal)
    await expectRejected { _ = try await guard_.executeApproved(pending) }
    #expect(try read(target) == "original")
}

// MARK: - test_file_operations.py

private struct OperationSetup {
    let root: URL, source: URL, folder: URL, journal: UndoJournal, guard_: RiskGuard
}

private func operationSetup() throws -> OperationSetup {
    let tmp = try tempDir()
    let root = try child(tmp, "allowed", directory: true)
    let source = try child(root, "source.bin")
    try Data([0xFF, 0x00, 0xFE]).write(to: source)
    chmod(source.path, 0o640)
    let folder = try child(root, "보관", directory: true)
    let journal = try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [root])
    return OperationSetup(root: root, source: source, folder: folder, journal: journal,
                          guard_: RiskGuard(FakeGateway(), journal, roots: [root]))
}

private func transfer(_ source: URL, _ destination: URL) -> JSON {
    ["source": .string(source.path), "destination": .string(destination.path)]
}

@Test(arguments: [moveTool, copyTool])
func approvedTransferPreservesBinaryModeAndUndoAfterRestart(tool: String) async throws {
    let s = try operationSetup()
    let prepared = try await s.guard_.prepareWrite(tool, transfer(s.source, s.folder))
    let destination = s.folder.appendingPathComponent(s.source.lastPathComponent)
    #expect(prepared.targets == [s.source.path] && prepared.targetPath == destination.path)
    #expect(exists(s.source) && !exists(destination))
    #expect(try await s.guard_.executeApproved(prepared).ok)
    #expect(try Data(contentsOf: destination) == Data([0xFF, 0x00, 0xFE]))
    var metadata = stat()
    stat(destination.path, &metadata)
    #expect(metadata.st_mode & 0o777 == 0o640)
    #expect(exists(s.source) == (tool == copyTool))
    await #expect(throws: ApprovalInvalidError.self) { try await s.guard_.executeApproved(prepared) }
    let reopened = try UndoJournal(path: s.journal.path, allowedRoots: [s.root])
    _ = try FileOperationJournal(reopened)
    #expect([.operationUndone, .createUndone].contains(try reopened.undoLastWrite().kind))
    #expect(try Data(contentsOf: s.source) == Data([0xFF, 0x00, 0xFE]) && !exists(destination))
}

@Test(arguments: [moveTool, copyTool, mkdirTool])
func rejectionAndMissingJournalNeverChangePaths(tool: String) async throws {
    let s = try operationSetup()
    let arguments: JSON = tool == mkdirTool
        ? ["path": .string(s.folder.appendingPathComponent("new").path)] : transfer(s.source, s.folder)
    let prepared = try await s.guard_.prepareWrite(tool, arguments)
    s.guard_.reject(prepared)
    await #expect(throws: ApprovalInvalidError.self) { try await s.guard_.executeApproved(prepared) }
    #expect(try exists(s.source) && contents(s.folder).isEmpty)
    await expectRejected {
        _ = try await RiskGuard(FakeGateway(), nil, roots: [s.root]).prepareWrite(tool, arguments)
    }
}

@Test func sourceChangesRequireReapprovalAndDestinationConflictStopsMove() async throws {
    let s = try operationSetup()
    let prepared = try await s.guard_.prepareWrite(moveTool, transfer(s.source, s.folder))
    try Data("new".utf8).write(to: s.source)
    let changed = await #expect(throws: ApprovalChangedError.self) { try await s.guard_.executeApproved(prepared) }
    #expect(try exists(s.source) && contents(s.folder).isEmpty)
    let taken = s.folder.appendingPathComponent(s.source.lastPathComponent)
    try write("user file", taken)
    await expectRejected { _ = try await s.guard_.executeApproved(try #require(changed).prepared) }
    #expect(try read(s.source) == "new" && read(taken) == "user file")
}

@Test func journalFailureHappensBeforeAnyFileChange() async throws {
    let s = try operationSetup()
    let prepared = try await s.guard_.prepareWrite(moveTool, transfer(s.source, s.folder))
    breakJournal(s.journal)
    await expectRejected { _ = try await s.guard_.executeApproved(prepared) }
    #expect(try exists(s.source) && contents(s.folder).isEmpty)
}

// MARK: - test_conversion.py

private func conversionSetup() throws -> (URL, UndoJournal, RiskGuard) {
    let tmp = try tempDir()
    let root = try child(tmp, "root", directory: true)
    let journal = try UndoJournal(path: try child(tmp, "undo.sqlite3"), allowedRoots: [root])
    return (root, journal, RiskGuard(FakeGateway(), journal, roots: [root]))
}

@Test func conversionIsWrittenOnlyAfterApprovalAndUndoRemovesIt() async throws {
    let (root, journal, guard_) = try conversionSetup()
    let source = try child(root, "표.csv"), output = try child(root, "표.json")
    try write("a\n1\n", source)
    let prepared = try await guard_.prepareWrite(convertTool, ["source": .string(source.path), "target_format": "json"])
    #expect(prepared.operation == "convert" && prepared.targets == [source.path])
    #expect(!exists(output))  // nothing before approval
    let result = try await guard_.executeApproved(prepared)
    #expect(result.ok && exists(output))
    #expect(try journal.undoLastWrite().kind == .createUndone)
    #expect(!exists(output) && exists(source))
}

@Test func sourceChangedDuringApprovalNeedsNewApproval() async throws {
    let (root, _, guard_) = try conversionSetup()
    let source = try child(root, "표.csv"), output = try child(root, "표.json")
    try write("a\n1\n", source)
    let prepared = try await guard_.prepareWrite(convertTool, ["source": .string(source.path), "target_format": "json"])
    try write("a\n2\n", source)
    let caught = await #expect(throws: ApprovalChangedError.self) { try await guard_.executeApproved(prepared) }
    #expect(!exists(output))
    _ = try await guard_.executeApproved(try #require(caught).prepared)
    #expect(try JSON.parse(read(output)) == [["a": "2"]])
}

@Test func readDocumentIsReadOnlyThroughRiskGuard() async throws {
    let (root, _, guard_) = try conversionSetup()
    await expectRejected {
        _ = try await guard_.executeReadOnly(readDocumentTool, ["path": .string(root.appendingPathComponent("x.pdf").path)])
    }
}

// MARK: - test_finder.py

@Test func openFileIsReadOnlyAndRoutedThroughTheGuard() async throws {
    let root = try tempDir()
    try write("x", try child(root, "a.txt"))
    let workspace = FakeWorkspace()
    let guard_ = RiskGuard(FakeGateway(), roots: [root], workspace: workspace)
    let result = try await guard_.executeReadOnly(openFileTool, ["path": .string(root.appendingPathComponent("a.txt").path)])
    #expect(result.ok && workspace.opened.count == 1)
}

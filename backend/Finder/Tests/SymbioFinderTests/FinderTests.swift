import Foundation
import SymbioCore
import Testing
@testable import SymbioFinder

struct Refused: Error {}

final class FakeWorkspace: Workspace {
    var shown: [[String]] = []
    var opened: [String] = []
    let fail: Bool
    init(fail: Bool = false) { self.fail = fail }

    func activateFileViewerSelecting(_ fileURLs: [URL]) throws {
        if fail { throw Refused() }
        shown.append(fileURLs.map(\.path))
    }

    func open(_ url: URL) -> Bool {
        opened.append(url.path)
        return !fail
    }
}

/// A fresh, already-resolved temp folder (/var → /private/var).
func tempRoot() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return URL(fileURLWithPath: realPath(url.path)!)
}

func write(_ url: URL) throws { try Data("x".utf8).write(to: url) }

// MARK: reveal

@Test func revealSelectsExactlyTheGivenFilesIncludingHangulAndSpaces() throws {
    let root = try tempRoot()
    let first = root.appendingPathComponent("한글 요약.txt")
    let second = root.appendingPathComponent("하위 폴더/b c.md")
    try FileManager.default.createDirectory(at: second.deletingLastPathComponent(), withIntermediateDirectories: true)
    try write(first)
    try write(second)
    let workspace = FakeWorkspace()

    #expect(reveal([first.path, second.path], roots: [root], workspace: workspace))
    #expect(workspace.shown == [[first.path, second.path]])
}

@Test func revealRefusesOutsideFilesLinksAndMissingFiles() throws {
    let base = try tempRoot()
    let root = base.appendingPathComponent("work")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let outside = base.appendingPathComponent("outside.txt")
    try write(outside)
    let link = root.appendingPathComponent("link.txt")
    try FileManager.default.createSymbolicLink(at: link, withDestinationURL: outside)
    let ok = root.appendingPathComponent("ok.txt")
    try write(ok)
    let workspace = FakeWorkspace()

    let bad = [outside.path, link.path, root.appendingPathComponent("missing.txt").path, "relative.txt"]
    #expect(!reveal(bad, roots: [root], workspace: workspace))
    #expect(workspace.shown.isEmpty)
    // One valid file among bad ones is still shown; the bad ones are dropped, not fatal.
    #expect(reveal([outside.path, ok.path], roots: [root], workspace: workspace))
    #expect(workspace.shown == [[ok.path]])
}

@Test func aFinderFailureIsSwallowed() throws {
    let root = try tempRoot()
    try write(root.appendingPathComponent("a.txt"))
    #expect(!reveal([root.appendingPathComponent("a.txt").path], roots: [root], workspace: FakeWorkspace(fail: true)))
}

@Test func nothingToShowIsNotAnError() throws {
    #expect(!reveal([], roots: [try tempRoot()], workspace: FakeWorkspace()))
}

@Test func atMostTenFilesAreChecked() throws {
    let root = try tempRoot()
    let paths = try (0..<12).map { index in
        let url = root.appendingPathComponent("\(index).txt")
        try write(url)
        return url.path
    }
    let workspace = FakeWorkspace()
    #expect(reveal(paths, roots: [root], workspace: workspace))
    #expect(workspace.shown == [Array(paths.prefix(10))])
}

// MARK: placing the window beside the app

let screen = (w: 1470, h: 956)

@Test func theFinderWindowGoesRightOfTheAppAtTheSameHeight() {
    #expect(sideBySide(Frame(100, 80, 700, 800), screen: screen) == Frame(808, 80, 640, 800))
}

@Test func itShrinksToTheRoomLeftAndUsesTheLeftSideWhenTheRightIsFull() {
    #expect(sideBySide(Frame(100, 80, 700, 800), screen: (1400, 956)) == Frame(808, 80, 592, 800))
    #expect(sideBySide(Frame(780, 80, 700, 800), screen: screen) == Frame(132, 80, 640, 800))
}

@Test func withNoRoomOnEitherSideNothingIsMoved() {
    #expect(sideBySide(Frame(300, 80, 860, 800), screen: screen) == nil)  // 410 free on each side
}

@Test func theWindowStaysBelowTheMenuBarAndOnTheScreen() throws {
    let frame = try #require(sideBySide(Frame(100, 0, 700, 2000), screen: screen))
    #expect(frame.y == 25 && frame.y + frame.h <= screen.h)
}

final class FakeWindows: FinderWindows {
    let titles: [String], trusted: Bool, broken: Bool
    var moved: [(Int, Frame)] = []
    init(_ titles: [String], trusted: Bool = true, broken: Bool = false) {
        (self.titles, self.trusted, self.broken) = (titles, trusted, broken)
    }

    func isTrusted() -> Bool { trusted }

    func windows() throws -> [(title: String, handle: Any)] {
        if broken { throw Refused() }
        return titles.enumerated().map { ($0.element, $0.offset as Any) }
    }

    func move(_ handle: Any, to frame: Frame) -> Bool {
        moved.append((handle as! Int, frame))
        return true
    }
}

let app = Frame(100, 80, 700, 800)

@Test func onlyTheWindowTitledLikeTheFolderIsMoved() {
    let windows = FakeWindows(["Downloads", "voice-action-test", "voice-action-test 2"])
    #expect(arrange("voice-action-test", app: app, screen: screen, windows: windows) == .moved)
    #expect(windows.moved.count == 1 && windows.moved[0].0 == 1 && windows.moved[0].1 == Frame(808, 80, 640, 800))
}

@Test func folderNamesMatchAcrossUnicodeForms() {
    let windows = FakeWindows(["자료 폴더".decomposedStringWithCanonicalMapping])
    let folder = "자료 폴더".precomposedStringWithCanonicalMapping
    #expect(arrange(folder, app: app, screen: screen, windows: windows) == .moved)
}

@Test func noMatchingWindowYetMeansTryAgainAndNothingElseIsTouched() {
    let windows = FakeWindows(["Downloads", "Desktop"])
    #expect(arrange("voice-action-test", app: app, screen: screen, windows: windows) == .waiting)
    #expect(windows.moved.isEmpty)
}

@Test func withoutAccessibilityPermissionItSaysSoAndMovesNothing() {
    let windows = FakeWindows(["voice-action-test"], trusted: false)
    #expect(arrange("voice-action-test", app: app, screen: screen, windows: windows) == .untrusted)
    #expect(windows.moved.isEmpty)
}

@Test func noRoomAndAFailingAccessibilityCallNeverRaise() {
    let crowded = FakeWindows(["voice-action-test"])
    #expect(arrange("voice-action-test", app: Frame(300, 80, 860, 800), screen: screen, windows: crowded) == .noSpace)
    #expect(crowded.moved.isEmpty)
    let broken = FakeWindows([], broken: true)
    #expect(arrange("voice-action-test", app: app, screen: screen, windows: broken) == .waiting)
    #expect(ArrangeResult.noSpace.rawValue == "no_space")
}

// MARK: opening a file in its default app

@Test func openFileOpensExactlyTheCheckedFile() throws {
    let root = try tempRoot()
    let target = root.appendingPathComponent("한글 메모.txt")
    try write(target)
    let workspace = FakeWorkspace()

    let result = try openFile(["path": .string(target.path)], roots: [root], workspace: workspace)
    #expect(result.ok && workspace.opened == [target.path])
    #expect(result.text == "기본 앱으로 열었습니다: \(target.path)")
}

@Test(arguments: ["outside", "link", "missing", "relative", "folder"])
func openFileRefusesAnythingButAFileInsideTheAllowedFolders(make: String) throws {
    let base = try tempRoot()
    let root = base.appendingPathComponent("work")
    try FileManager.default.createDirectory(at: root.appendingPathComponent("sub"), withIntermediateDirectories: true)
    let outside = base.appendingPathComponent("outside.txt")
    try write(outside)
    try FileManager.default.createSymbolicLink(at: root.appendingPathComponent("link.txt"), withDestinationURL: outside)
    let paths = [
        "outside": outside.path, "link": root.appendingPathComponent("link.txt").path,
        "missing": root.appendingPathComponent("missing.txt").path, "relative": "a.txt",
        "folder": root.appendingPathComponent("sub").path,
    ]
    let workspace = FakeWorkspace()

    #expect(throws: ToolError.self) {
        try openFile(["path": .string(paths[make]!)], roots: [root], workspace: workspace)
    }
    #expect(workspace.opened.isEmpty)
}

@Test(arguments: ["run.command", "x.sh", "a.app", "s.scpt", "w.workflow", "t", "UP.SH"])
func openFileNeverRunsPrograms(name: String) throws {
    let root = try tempRoot()
    let target = root.appendingPathComponent(name)
    if name.hasSuffix(".app") {
        try FileManager.default.createDirectory(at: target.appendingPathComponent("Contents"), withIntermediateDirectories: true)
    } else {
        try Data("echo hi".utf8).write(to: target)
    }
    if name == "t" { try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: target.path) }
    let workspace = FakeWorkspace()

    do {
        _ = try openFile(["path": .string(target.path)], roots: [root], workspace: workspace)
        Issue.record("opened \(name)")
    } catch {
        #expect(error.isRejected)
    }
    #expect(workspace.opened.isEmpty)
}

@Test func openFileReportsARefusalByMacOS() throws {
    let root = try tempRoot()
    try write(root.appendingPathComponent("a.txt"))
    let result = try openFile(
        ["path": .string(root.appendingPathComponent("a.txt").path)], roots: [root], workspace: FakeWorkspace(fail: true)
    )
    #expect(!result.ok)
}

@Test func suffixMatchesPython() {
    #expect(suffix("a.TXT") == ".TXT")
    #expect(suffix(".bashrc") == "")
    #expect(suffix("a.") == "")
    #expect(suffix("a.tar.gz") == ".gz")
}

@Test func openDefinitionSchemaRequiresAPath() throws {
    #expect(openDefinition.name == "local__open_file")
    #expect(throws: ToolError.self) { try validateArguments(openDefinition, ["path": 1]) }
    try validateArguments(openDefinition, ["path": "/a.txt"])
}

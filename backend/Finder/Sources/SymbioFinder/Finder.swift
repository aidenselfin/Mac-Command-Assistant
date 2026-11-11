import AppKit
import ApplicationServices
import Foundation
import SymbioCore

/// Python `finder.py`: show files the app just wrote in Finder, open a file in its default app,
/// and place Finder's window beside the app. A failure only means nothing opens; it never
/// changes the result of the file work that came before.

public let maxRevealed = 10

/// The part of `NSWorkspace` Symbio uses, so tests never open the real Finder.
public protocol Workspace {
    func activateFileViewerSelecting(_ fileURLs: [URL]) throws
    func open(_ url: URL) -> Bool
}

extension NSWorkspace: Workspace {}

private func resolvedRoots(_ roots: [URL]) -> [URL] {
    roots.map { URL(fileURLWithPath: realPath($0.path) ?? $0.path) }
}

/// Select the files in Finder. True if a window was asked to show at least one of them.
@discardableResult
public func reveal(_ paths: [String], roots: [URL], workspace: any Workspace = NSWorkspace.shared) -> Bool {
    let allowed = resolvedRoots(roots)
    let checked = paths.prefix(maxRevealed).compactMap { raw in
        try? inside(.string(raw), roots: allowed, exists: true)
    }
    if checked.isEmpty { return false }
    do { try workspace.activateFileViewerSelecting(checked) } catch { return false }
    return true
}

public let openFileTool = "local__open_file"
public let openDefinition = ToolDefinition(
    name: openFileTool,
    description: "허용된 폴더 안 파일 하나를 macOS 기본 앱으로 연다(예: txt는 텍스트 편집기, pdf는 미리보기). "
        + "사용자가 파일을 열어 달라고 할 때만 쓴다. path는 찾은 파일의 절대경로다. 내용을 읽거나 "
        + "요약할 때는 쓰지 않는다. 프로그램·스크립트·앱은 열 수 없다.",
    schema: [
        "type": "object", "properties": ["path": ["type": "string"]],
        "required": ["path"], "additionalProperties": false,
    ]
)

/// Opening one of these runs it (Terminal runs a .command, Script Editor may run a script).
// ponytail: a fixed list plus the executable bit; a launch-services "is this an app" check
// would catch handlers this list does not know.
public let programSuffixes: Set<String> = [
    ".app", ".command", ".tool", ".sh", ".bash", ".zsh", ".csh", ".ksh", ".fish", ".py",
    ".pyw", ".pl", ".rb", ".php", ".js", ".scpt", ".scptd", ".applescript", ".workflow",
    ".action", ".terminal", ".pkg", ".mpkg", ".dmg", ".jar", ".prefpane", ".kext", ".plugin",
    ".bundle", ".mobileconfig", ".shortcut", ".webloc", ".inetloc", ".fileloc", ".url",
    ".osax", ".service", ".saver", ".qlgenerator", ".mdimporter", ".xpc", ".ipa",
]

/// Python `PurePath.suffix`: "" for ".bashrc" and "a.".
func suffix(_ name: String) -> String {
    guard let dot = name.lastIndex(of: "."), dot != name.startIndex, dot != name.index(before: name.endIndex)
    else { return "" }
    return String(name[dot...])
}

/// Open one regular, non-program file inside an allowed folder with its default app.
public func openFile(_ arguments: JSON, roots: [URL], workspace: any Workspace = NSWorkspace.shared)
    throws(ToolError) -> ToolResult
{
    let path = try inside(arguments["path"], roots: resolvedRoots(roots), exists: true)
    var info = stat()
    guard lstat(path.path, &info) == 0 else { throw .rejected("파일 경로를 확인할 수 없습니다.") }
    if info.st_mode & S_IFMT != S_IFREG { throw .rejected("일반 파일만 열 수 있습니다.") }
    if programSuffixes.contains(suffix(path.lastPathComponent).lowercased()) || info.st_mode & 0o111 != 0 {
        throw .rejected("프로그램·스크립트 파일은 열지 않습니다.")
    }
    // ponytail: macOS opens by path, so a swap to a link after this check is not caught;
    // only a process already writing in the folder can race it. Close it with a file handle
    // if Launch Services ever takes one.
    if !workspace.open(path) { return ToolResult(ok: false, text: "파일을 열지 못했습니다: \(path.path)") }
    return ToolResult(ok: true, text: "기본 앱으로 열었습니다: \(path.path)")
}

// MARK: - placing the Finder window beside the app
//
// Finder's own "show in Finder" reuses a window that is already open, wherever it is, so the
// result can sit behind the app. Moving a window needs Accessibility (the permission the app
// already asks for), not Apple Events: scripting Finder would ask for Automation and can hang.

public let minSideWidth = 420  // a Finder window narrower than this is not worth showing
public let finderWidth = 640
public let gap = 8
public let menuBar = 25
public let finderBundle = "com.apple.finder"

/// A window rectangle in screen points, origin at the top left of the main screen.
public struct Frame: Equatable, Sendable {
    public var x, y, w, h: Int
    public init(_ x: Int, _ y: Int, _ w: Int, _ h: Int) { (self.x, self.y, self.w, self.h) = (x, y, w, h) }
}

/// Where a Finder window goes next to the app: right if it fits, else left, else nowhere.
public func sideBySide(_ app: Frame, screen: (w: Int, h: Int)) -> Frame? {
    let y = max(app.y, menuBar)
    let height = max(min(app.h, screen.h - y - gap), 0)
    let right = screen.w - (app.x + app.w + gap)
    if right >= minSideWidth { return Frame(app.x + app.w + gap, y, min(finderWidth, right), height) }
    let left = app.x - gap
    if left >= minSideWidth {
        let width = min(finderWidth, left)
        return Frame(app.x - gap - width, y, width, height)
    }
    return nil
}

public protocol FinderWindows {
    func isTrusted() -> Bool
    /// (title, handle) of Finder's windows, front to back.
    func windows() throws -> [(title: String, handle: Any)]
    func move(_ handle: Any, to frame: Frame) -> Bool
}

/// Finder's windows through the Accessibility API (needs the Accessibility permission).
public struct AXFinderWindows: FinderWindows {
    public init() {}

    public func isTrusted() -> Bool { AXIsProcessTrusted() }

    public func windows() -> [(title: String, handle: Any)] {
        guard let finder = NSRunningApplication.runningApplications(withBundleIdentifier: finderBundle).first
        else { return [] }
        let application = AXUIElementCreateApplication(finder.processIdentifier)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(application, kAXWindowsAttribute as CFString, &value) == .success,
              let handles = value as? [AXUIElement]
        else { return [] }
        return handles.map { handle in
            var title: CFTypeRef?
            let ok = AXUIElementCopyAttributeValue(handle, kAXTitleAttribute as CFString, &title) == .success
            return (ok ? (title as? String) ?? "" : "", handle)
        }
    }

    public func move(_ handle: Any, to frame: Frame) -> Bool {
        let window = handle as! AXUIElement  // only handles from windows() come back here
        var point = CGPoint(x: frame.x, y: frame.y)
        var size = CGSize(width: frame.w, height: frame.h)
        guard let pointValue = AXValueCreate(.cgPoint, &point), let sizeValue = AXValueCreate(.cgSize, &size)
        else { return false }
        // Size first: a window that is resized after it moved can run off the screen's edge.
        return AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, sizeValue) == .success
            && AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, pointValue) == .success
    }
}

public enum ArrangeResult: String, Sendable {
    case moved, waiting, untrusted
    case noSpace = "no_space"
}

/// One attempt to put Finder's window for `folder` beside the app.
///
/// `.waiting` means the window is not there yet: try again shortly. Only a window titled like
/// the folder is touched (Swift `==` already matches NFC and NFD), so no unrelated Finder window
/// is ever moved.
public func arrange(
    _ folder: String, app: Frame, screen: (w: Int, h: Int), windows: any FinderWindows = AXFinderWindows()
) -> ArrangeResult {
    if !windows.isTrusted() { return .untrusted }
    guard let target = sideBySide(app, screen: screen) else { return .noSpace }
    // Placing a window is a nicety, never a failure.
    guard let found = try? windows.windows() else { return .waiting }
    for (title, handle) in found where title == folder {
        return windows.move(handle, to: target) ? .moved : .waiting
    }
    return .waiting
}

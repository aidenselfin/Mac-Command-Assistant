import Foundation
import Testing
@testable import SymbioCore

@Test func aFileInsideTheAllowedFolderIsAcceptedWithSpacesAndHangul() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    let roots = try testConfig(tmp).allowedDirectories
    let target = roots[0].appendingPathComponent("하위 폴더/회의 노트 (최종).md")
    try write("x", to: target)
    #expect(try checkAttachment(.string(target.path), roots: roots) == target.path)
    #expect(try checkAttachment(.string("  \(target.path)  "), roots: roots) == target.path)
}

@Test(arguments: ["outside", "link", "ancestor_link", "folder", "missing", "relative"])
func everythingElseIsRefusedBeforeACommandExists(_ kind: String) throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    let roots = try testConfig(tmp).allowedDirectories
    let root = roots[0]
    let outside = tmp.appendingPathComponent("outside")
    try write("x", to: outside.appendingPathComponent("secret.txt"))
    try write("x", to: root.appendingPathComponent("real.txt"))
    try mkdir(root.appendingPathComponent("dir"))
    let manager = FileManager.default
    try manager.createSymbolicLink(
        at: root.appendingPathComponent("inner-link.txt"), withDestinationURL: root.appendingPathComponent("real.txt"))
    try manager.createSymbolicLink(at: root.appendingPathComponent("out-link"), withDestinationURL: outside)
    let raw = [
        "outside": outside.appendingPathComponent("secret.txt").path,
        "link": root.appendingPathComponent("inner-link.txt").path,
        "ancestor_link": root.path + "/out-link/secret.txt",
        "folder": root.appendingPathComponent("dir").path,
        "missing": root.appendingPathComponent("nope.txt").path,
        "relative": "real.txt",
    ][kind]!
    let error = #expect(throws: AttachmentError.self) { try checkAttachment(.string(raw), roots: roots) }
    #expect(error?.message.contains(root.path) == true || kind == "folder")
}

@Test(arguments: [nil, "", "   ", 3] as [JSON?])
func emptyOrNonTextPathsAreRefused(_ raw: JSON?) throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    let roots = try testConfig(tmp).allowedDirectories
    #expect(throws: AttachmentError.self) { try checkAttachment(raw, roots: roots) }
}

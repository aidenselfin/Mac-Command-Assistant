import Foundation
import SymbioCore
import Testing
@testable import SymbioConvert

// Ported from tests/test_conversion.py and tests/test_conversion_review.py.
// RiskGuard/Undo/Planner cases wait for those modules.

/// A fresh (tmp, root = tmp/work) pair, both canonical.
private func makeRoot() throws -> (tmp: URL, root: URL) {
    let tmp = URL(fileURLWithPath: realPath(NSTemporaryDirectory())!).appendingPathComponent(UUID().uuidString)
    let root = tmp.appendingPathComponent("work")
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return (tmp, root)
}

private func write(_ url: URL, _ text: String) throws { try Data(text.utf8).write(to: url) }

private func names(_ folder: URL) throws -> [String] {
    try FileManager.default.contentsOfDirectory(atPath: folder.path).sorted()
}

@discardableResult
private func convertFile(_ root: URL, _ source: URL, _ format: String, output: String? = nil) throws -> PreparedConversion {
    var arguments: JSON = ["source": .string(source.path), "target_format": .string(format)]
    if let output { arguments.set("output", .string(output)) }
    let prepared = try prepareConversion(arguments, roots: [root])
    try writeNewFile(prepared.output, data: prepared.data, roots: [root])
    return prepared
}

private func rejected(_ message: String, _ body: () throws -> Void) {
    do {
        try body()
        Issue.record("expected rejection containing \(message)")
    } catch let error as ToolError {
        #expect(error.isRejected && error.message.range(of: message, options: .regularExpression) != nil,
                "\(error.message)")
    } catch {
        Issue.record("unexpected \(error)")
    }
}

private func bmp(width: Int = 4, height: Int = 4) -> Data {
    func le<T: FixedWidthInteger>(_ value: T) -> [UInt8] { withUnsafeBytes(of: value.littleEndian, Array.init) }
    let row = Array(repeating: [UInt8]([0, 0, 0xFF]), count: width).flatMap { $0 }
        + Array(repeating: 0, count: (4 - width * 3 % 4) % 4)
    let pixels = Array(repeating: row, count: height).flatMap { $0 }
    let header = Array("BM".utf8) + le(UInt32(54 + pixels.count)) + le(UInt16(0)) + le(UInt16(0)) + le(UInt32(54))
    let info = le(UInt32(40)) + le(Int32(width)) + le(Int32(height)) + le(UInt16(1)) + le(UInt16(24))
        + le(UInt32(0)) + le(UInt32(pixels.count)) + le(Int32(2835)) + le(Int32(2835)) + le(UInt32(0)) + le(UInt32(0))
    return Data(header + info + pixels)
}

/// An HWPX zip built with /usr/bin/zip (deflated entries), like the Python `hwpx` helper.
private func hwpx(_ path: URL, _ paragraphs: [String], mimetype: String = "application/hwp+zip",
                  extra: [String: String] = [:]) throws {
    let work = path.deletingLastPathComponent().appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: work.appendingPathComponent("Contents"), withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: work) }
    let ns = #"xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph""#
    let body = paragraphs.map { "<hp:p><hp:run><hp:t>\($0)</hp:t></hp:run></hp:p>" }.joined()
    try write(work.appendingPathComponent("mimetype"), mimetype)
    try write(work.appendingPathComponent("Contents/section0.xml"), "<hs:sec \(ns) xmlns:hs='s'>\(body)</hs:sec>")
    for (name, text) in extra { try write(work.appendingPathComponent(name), text) }
    let zip = Process()
    zip.executableURL = URL(fileURLWithPath: "/usr/bin/zip")
    zip.currentDirectoryURL = work
    zip.arguments = ["-X", "-q", path.path, "mimetype", "Contents/section0.xml"] + extra.keys.sorted()
    try zip.run()
    zip.waitUntilExit()
    #expect(zip.terminationStatus == 0)
}

// MARK: - engine

@Test func documentsPdfAndTextRoundTrip() throws {
    let (_, root) = try makeRoot()
    try write(root.appendingPathComponent("메모.txt"), "안녕하세요\n두 번째 줄")
    let docx = try convertFile(root, root.appendingPathComponent("메모.txt"), "docx")
    #expect(docx.output.hasSuffix("메모.docx") && docx.data.starts(with: Array("PK".utf8)))
    let back = try convertFile(root, root.appendingPathComponent("메모.docx"), "txt")
    #expect(back.output.hasSuffix("메모 (1).txt"))  // never overwrites the original
    #expect(back.preview.hasPrefix("안녕하세요"))
    try write(root.appendingPathComponent("노트.md"), "# 제목\n본문")
    let pdf = try convertFile(root, root.appendingPathComponent("노트.md"), "pdf")
    #expect(pdf.data.starts(with: Array("%PDF".utf8)) && pdf.preview == "")
    let text = try convertFile(root, root.appendingPathComponent("노트.pdf"), "txt")
    #expect(String(decoding: text.data, as: UTF8.self).contains("제목"))
}

@Test(arguments: [("jpeg", [UInt8]([0xFF, 0xD8, 0xFF])), ("png", [0x89, 0x50, 0x4E, 0x47])])
func iphoneHeicPhotoToJpgAndPng(target: String, signature: [UInt8]) throws {
    let (_, root) = try makeRoot()
    try bmp().write(to: root.appendingPathComponent("사진.bmp"))
    let heic = try convertFile(root, root.appendingPathComponent("사진.bmp"), "heic")
    #expect(Array(heic.data[4..<12]) == Array("ftypheic".utf8))
    let result = try convertFile(root, root.appendingPathComponent("사진.heic"), target)
    #expect(result.data.starts(with: signature))
    #expect(result.output.hasSuffix(target == "jpeg" ? ".jpg" : ".png"))
}

@Test func csvAndJSONRoundTrip() throws {
    let (_, root) = try makeRoot()
    try write(root.appendingPathComponent("표.csv"), "이름,금액\n점심,12000\n")
    let asJSON = try convertFile(root, root.appendingPathComponent("표.csv"), "json")
    #expect(try JSON.parse(asJSON.data) == [["이름": "점심", "금액": "12000"]])
    let back = try convertFile(root, root.appendingPathComponent("표.json"), "csv")
    #expect(String(decoding: back.data, as: UTF8.self) == "이름,금액\n점심,12000\n")
}

@Test func hangulHwpxToPdf() throws {
    let (_, root) = try makeRoot()
    try hwpx(root.appendingPathComponent("공문.hwpx"), ["회의 안내", "일시: 11월 5일"])
    let pdf = try convertFile(root, root.appendingPathComponent("공문.hwpx"), "pdf")
    #expect(pdf.output.hasSuffix("공문.pdf") && pdf.data.starts(with: Array("%PDF".utf8)))
    let text = String(decoding: try convertFile(root, root.appendingPathComponent("공문.pdf"), "txt").data,
                      as: UTF8.self)
    #expect(text.contains("회의 안내") && text.contains("11월 5일"))
}

@Test(arguments: ["unsupported", "badSignature", "hwpxNotHangul", "hwpxEntities", "jsonShape", "outputTaken"])
func conversionRejections(_ name: String) throws {
    let (_, root) = try makeRoot()
    var arguments: JSON = ["target_format": "pdf"]
    let message: String
    switch name {
    case "unsupported":
        try write(root.appendingPathComponent("a.csv"), "x")
        message = "지원하지 않는"
    case "badSignature":
        try write(root.appendingPathComponent("a.png"), "not png")
        arguments.set("target_format", "jpeg")
        message = "실제 파일"
    case "hwpxNotHangul":
        try hwpx(root.appendingPathComponent("a.hwpx"), ["x"], mimetype: "application/zip")
        message = "실제 파일"
    case "hwpxEntities":
        try hwpx(root.appendingPathComponent("a.hwpx"), ["x"],
                 extra: ["Contents/section1.xml": #"<!DOCTYPE x [<!ENTITY e "y">]><x/>"#])
        message = "읽을 수 없습니다"
    case "jsonShape":
        try write(root.appendingPathComponent("a.json"), #"{"a": 1}"#)
        arguments.set("target_format", "csv")
        message = "배열"
    default:
        try write(root.appendingPathComponent("a.csv"), "x")
        try write(root.appendingPathComponent("out.json"), "taken")
        arguments = ["target_format": "json", "output": .string(root.appendingPathComponent("out.json").path)]
        message = "이미 있습니다"
    }
    let source = try names(root).first { $0.hasPrefix("a.") }!
    arguments.set("source", .string(root.appendingPathComponent(source).path))
    rejected(message) { _ = try prepareConversion(arguments, roots: [root]) }
}

@Test func pathsMustStayInsideAllowedFolders() throws {
    let (tmp, root) = try makeRoot()
    let outside = tmp.appendingPathComponent("outside.csv")
    try write(outside, "a\n1\n")
    try FileManager.default.createSymbolicLink(at: root.appendingPathComponent("link.csv"), withDestinationURL: outside)
    for source in [outside, root.appendingPathComponent("link.csv")] {
        rejected("허용 폴더") {
            _ = try prepareConversion(["source": .string(source.path), "target_format": "json"], roots: [root])
        }
    }
}

@Test func readDocumentReturnsTextWithoutWriting() throws {
    let (_, root) = try makeRoot()
    try write(root.appendingPathComponent("메모.txt"), String(repeating: "문서 읽기 확인 ", count: 20))
    try convertFile(root, root.appendingPathComponent("메모.txt"), "docx")
    let before = try names(root)
    let result = try readDocument(["path": .string(root.appendingPathComponent("메모.docx").path)], roots: [root], limit: 30)
    #expect(result.ok && result.truncated && result.text.hasPrefix("문서 읽기 확인"))
    #expect(try names(root) == before)
    rejected("filesystem__read_text_file") {
        _ = try readDocument(["path": .string(root.appendingPathComponent("메모.txt").path)], roots: [root], limit: 30)
    }
}

// MARK: - review regressions (C01 boundary, C02 partial writes)

/// (allowed, outside) with allowed/source.csv, like the Python `setup` fixture.
private func reviewSetup() throws -> (root: URL, outside: URL) {
    let (tmp, _) = try makeRoot()
    let root = tmp.appendingPathComponent("allowed"), outside = tmp.appendingPathComponent("outside")
    for folder in [root, outside] { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
    try write(root.appendingPathComponent("source.csv"), "name\nreview\n")
    return (root, outside)
}

@Test func outputParentSwappedForSymlinkWritesNothingOutside() throws {
    let (root, outside) = try reviewSetup()
    let fm = FileManager.default
    try fm.createDirectory(at: root.appendingPathComponent("out"), withIntermediateDirectories: false)
    let prepared = try prepareConversion([
        "source": .string(root.appendingPathComponent("source.csv").path), "target_format": "json",
        "output": .string(root.appendingPathComponent("out/result.json").path),
    ], roots: [root])
    try fm.moveItem(at: root.appendingPathComponent("out"), to: root.appendingPathComponent("moved"))
    try fm.createSymbolicLink(at: root.appendingPathComponent("out"), withDestinationURL: outside)
    #expect(changedSince(source: prepared.source, state: prepared.sourceState, output: prepared.output, roots: [root]))
    rejected("") { try writeNewFile(prepared.output, data: prepared.data, roots: [root]) }
    #expect(try names(outside) == [])
}

@Test func sourceParentSwappedForSymlinkIsRefused() throws {
    let (root, outside) = try reviewSetup()
    let fm = FileManager.default
    try fm.createDirectory(at: root.appendingPathComponent("in"), withIntermediateDirectories: false)
    try write(root.appendingPathComponent("in/data.csv"), "a\n1\n")
    let prepared = try prepareConversion(
        ["source": .string(root.appendingPathComponent("in/data.csv").path), "target_format": "json"], roots: [root])
    try write(outside.appendingPathComponent("data.csv"), "a\n1\n")
    try fm.moveItem(at: root.appendingPathComponent("in"), to: root.appendingPathComponent("moved"))
    try fm.createSymbolicLink(at: root.appendingPathComponent("in"), withDestinationURL: outside)
    #expect(changedSince(source: prepared.source, state: prepared.sourceState, output: prepared.output, roots: [root]))
    #expect(try names(outside) == ["data.csv"])
}

@Test func sourceEditedOrOutputTakenIsAChange() throws {
    let (root, _) = try reviewSetup()
    let source = root.appendingPathComponent("source.csv")
    let prepared = try prepareConversion(["source": .string(source.path), "target_format": "json"], roots: [root])
    #expect(!changedSince(source: prepared.source, state: prepared.sourceState, output: prepared.output, roots: [root]))
    try write(source, "name\nchanged\n")
    #expect(changedSince(source: prepared.source, state: prepared.sourceState, output: prepared.output, roots: [root]))
}

@Test func localWriteItselfEnforcesTheBoundary() throws {
    let (root, outside) = try reviewSetup()
    try FileManager.default.createSymbolicLink(at: root.appendingPathComponent("link"), withDestinationURL: outside)
    rejected("") { try writeNewFile(root.appendingPathComponent("link/x.json").path, data: Data("[]".utf8), roots: [root]) }
    rejected("") { try writeNewFile(outside.appendingPathComponent("x.json").path, data: Data("[]".utf8), roots: [root]) }
    #expect(try names(outside) == [])
}

@Test func writeRefusesAnExistingOutput() throws {
    let (root, _) = try reviewSetup()
    rejected("승인 대기 중에 생겼습니다") {
        try writeNewFile(root.appendingPathComponent("source.csv").path, data: Data("x".utf8), roots: [root])
    }
}

@Test func partialWriteFailureLeavesNoOrphanFile() throws {
    let (root, _) = try reviewSetup()
    let target = root.appendingPathComponent("source.json").path
    var calls = 0
    do {
        try writeNewFile(target, data: Data("[1, 2, 3]".utf8), roots: [root]) { descriptor, buffer, _ in
            calls += 1
            if calls == 1 { return Darwin.write(descriptor, buffer, 1) }
            errno = ENOSPC
            return -1
        }
        Issue.record("expected failure")
    } catch {
        #expect(!error.isRejected && error.message.contains("변환 파일을 쓰지 못했습니다"))
    }
    #expect(!FileManager.default.fileExists(atPath: target))
}

// MARK: - C05 CSV structure, C06 output limits, C07 converter and input errors

private func csvToJSON(_ root: URL, _ bytes: [UInt8]) throws -> PreparedConversion {
    let path = root.appendingPathComponent("data.csv")
    try Data(bytes).write(to: path)
    return try prepareConversion(["source": .string(path.path), "target_format": "json"], roots: [root])
}

@Test(arguments: [
    (Array("name,name\nfirst,second\n".utf8), "헤더"),
    (Array("name,\nfirst,second\n".utf8), "헤더"),
    (Array("a,b\n1\n".utf8), "열 수"),
    (Array("a,b\n1,2,3\n".utf8), "열 수"),
    ([], "비어"),
    (Array("a,b\n\"open,2\n".utf8), "CSV"),
    ([0x61, 0x2C, 0x62, 0x0A, 0xFF, 0xFE, 0x2C, 0x31, 0x0A], "UTF-8"),
] as [([UInt8], String)])
func csvThatWouldLoseOrGarbleDataIsRefused(bytes: [UInt8], message: String) throws {
    let (root, _) = try reviewSetup()
    rejected(message) { _ = try csvToJSON(root, bytes) }
}

@Test func csvKeepsStringsBomAndQuotedNewlines() throws {
    let (root, _) = try reviewSetup()
    let converted = try csvToJSON(root, [0xEF, 0xBB, 0xBF] + Array("이름,메모\n점심,\"줄1\n줄2\"\n\n저녁,\n".utf8))
    #expect(try JSON.parse(converted.data) == [["이름": "점심", "메모": "줄1\n줄2"], ["이름": "저녁", "메모": ""]])
}

@Test func csvReaderMatchesPythonStrictMode() throws {
    #expect(try csvRows("x\n\r\n\ny\n") == [["x"], [], [], ["y"]])
    #expect(try csvRows("a,b\r\n1,2") == [["a", "b"], ["1", "2"]])
    #expect(try csvRows("a\"b\n") == [["a\"b"]])
    #expect(throws: ToolError.self) { try csvRows("\"a\"b\n") }
    #expect(throws: ToolError.self) { try csvRows("a\rb\n") }
    #expect(csvLine(["a\rb", "c\"d", "e,f", " g "]) == "\"a\rb\",\"c\"\"d\",\"e,f\", g \n")
    #expect(csvLine([""]) == "\"\"\n" && csvLine(["", ""]) == ",\n" && csvLine([]) == "\n")
}

@Test func jsonScalarsHaveADocumentedCSVForm() throws {
    let (root, _) = try reviewSetup()
    let path = root.appendingPathComponent("data.json")
    try write(path, #"[{"a": null, "b": 1.5, "c": true, "d": "x"}]"#)
    let converted = try prepareConversion(["source": .string(path.path), "target_format": "csv"], roots: [root])
    #expect(String(decoding: converted.data, as: UTF8.self) == "a,b,c,d\n,1.5,true,x\n")
}

@Test(arguments: [("data.csv", "json", "a\n1\n"), ("data.json", "csv", #"[{"a": 1}]"#)])
func everyConverterRespectsTheOutputLimit(source: String, target: String, content: String) throws {
    let (root, _) = try reviewSetup()
    try write(root.appendingPathComponent(source), content)
    rejected("50 MiB|크기") {
        _ = try prepareConversion(["source": .string(root.appendingPathComponent(source).path),
                                   "target_format": .string(target)], roots: [root], maxOutput: 1)
    }
}

@Test func sourceReadIsBounded() throws {
    let (root, _) = try reviewSetup()
    try write(root.appendingPathComponent("big.csv"), "a\n" + String(repeating: "1\n", count: 100))
    rejected("20 MiB|크기") {
        _ = try prepareConversion(["source": .string(root.appendingPathComponent("big.csv").path),
                                   "target_format": "json"], roots: [root], maxSource: 10)
    }
}

@Test func converterSuccessWithoutAnArtifactIsASafeRefusal() throws {
    let (root, _) = try reviewSetup()
    rejected("결과 파일") {
        _ = try convert(root.appendingPathComponent("x.txt"), data: Data("review document".utf8),
                        sourceFormat: "txt", targetFormat: "docx") { _ throws(ToolError) in Data() }
    }
}

@Test func oversizedConverterArtifactIsRefusedBeforeReading() throws {
    let (root, _) = try reviewSetup()
    rejected("50 MiB|크기") {
        _ = try convert(root.appendingPathComponent("x.txt"), data: Data("review document".utf8),
                        sourceFormat: "txt", targetFormat: "docx", maxOutput: 10) { command throws(ToolError) in
            try? Data(repeating: 0x78, count: 100).write(to: URL(fileURLWithPath: command[command.firstIndex(of: "-output")! + 1]))
            return Data()
        }
    }
}

@Test func zipReaderChecksCRC() throws {
    let (root, _) = try reviewSetup()
    let path = root.appendingPathComponent("a.hwpx")
    try hwpx(path, ["본문"])
    var bytes = [UInt8](try Data(contentsOf: path))
    let archive = try ZipArchive(bytes)
    #expect(try String(decoding: archive.read(archive.entries[0]), as: UTF8.self) == "application/hwp+zip")
    let start = archive.entries[0].headerOffset + 30 + archive.entries[0].raw.count
    bytes[start] ^= 0xFF  // mimetype is stored, so this flips a content byte
    let broken = try ZipArchive(bytes)
    #expect(throws: ToolError.self) { try broken.read(broken.entries[0]) }
}

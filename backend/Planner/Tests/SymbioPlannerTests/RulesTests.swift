import Foundation
import SymbioCore
import SymbioFileOps
import Testing

@testable import SymbioPlanner

// tests/test_planner.py: user URL boundaries and the web save intent.

@Test(arguments: [
    ("https://example.com을 읽고 summary.md에 저장해줘", ["https://example.com"]),
    ("https://example.com, 다음 문장을 읽어줘.", ["https://example.com"]),
    ("https://example.com. 다음 문장을 읽어줘", ["https://example.com"]),
    ("https://example.com 다음 문장을 읽어줘", ["https://example.com"]),
    ("(https://example.com/path(foo))를 읽어줘", ["https://example.com/path(foo)"]),
    (
        "https://example.com/한글?값=보존 과 https://second.example/path",
        ["https://example.com/한글?값=보존", "https://second.example/path"]
    ),
    ("https://예시.한국/경로?encoded=%EA%B0%92 을 읽어줘", ["https://예시.한국/경로?encoded=%EA%B0%92"]),
    ("https://example.com/경로을 읽어줘", []),
    ("https://example.com/?q=hello! 을 읽어줘", ["https://example.com/?q=hello!"]),
    ("https://example.com/사과 를 읽어줘", ["https://example.com/사과"]),
    ("<https://example.com/사과> 읽어줘", ["https://example.com/사과"]),
])
func userURLExtractionKeepsExactSafeBoundaries(text: String, expected: [String]) {
    #expect(extractUserURLs(text) == expected)
}

@Test func quotedSavePhraseIsNotAWebSave() {
    let url = "https://example.com"
    #expect(!requiresWebSaveFlow("\(url) 페이지에서 문구 \"summary.md에 저장해줘\"를 찾아 설명해줘", [url]))
    #expect(!requiresWebSaveFlow("\(url) 페이지에서 문구 “summary.md에 저장해줘”를 찾아 설명해줘", [url]))
}

@Test(arguments: ["을 ", " "])
func urlReadThenExplicitOutputSaveIsAWebSave(separator: String) {
    let url = "https://example.com"
    #expect(requiresWebSaveFlow("\(url)\(separator)읽고 summary.md에 저장해줘", [url]))
}

@Test func webSaveIntentDistinguishesQuotedFilenameFromQuotedCommand() {
    let url = "https://example.com"
    #expect(requiresWebSaveFlow("\(url) 페이지를 읽고 \"summary.md\"에 저장해줘", [url]))
    #expect(!requiresWebSaveFlow("\"\(url) 페이지를 읽고 summary.md에 저장해줘\"", [url]))
}

@Test(arguments: [
    "https://example.com 페이지와 test1.md를 비교해줘",
    "https://example.com 페이지에서 summary.md 내용을 읽어줘",
    "https://example.com 페이지를 summary.md에 저장하지 말고 읽어줘",
    "https://example.com 페이지를 읽고 test1.md를 summary.md로 저장해줘",
    "https://example.com 페이지에서 \"summary.md에 저장해줘\" 문구를 설명해줘",
])
func webSaveIntentRequiresPageOutputRelationship(text: String) {
    #expect(!requiresWebSaveFlow(text, ["https://example.com"]))
}

// tests/test_free_text.py / test_planner.py: which typed questions are passed on.

@Test(arguments: [
    ("old.log 파일을 삭제하시겠습니까?", true),
    ("저장할까요?", true),
    ("변환해도 될까요?", true),
    ("어떤 이름으로 저장할까요?", false),
    ("저장할 내용을 알려 주세요.", false),
])
func whichTypedQuestionsAreReallyConfirmations(question: String, refused: Bool) {
    let state = LoopState(userText: "파일 만들어줘", findTried: true, findNone: false, knownTarget: true)
    #expect((freeTextRefusal(state, question) != nil) == refused)
}

@Test func folderQuestionIsRefusedSoTheDefaultFolderIsUsed() {
    let state = LoopState(userText: "폴더 안에 뭐 있는지 알려줘", findTried: true, findNone: false, knownTarget: false)
    let ask = "어느 폴더의 내용을 알려드릴까요? 경로를 알려주세요."
    #expect(freeTextRefusal(state, ask)?.contains("기본 폴더") == true)
    #expect(freeTextRefusal(state, "어느 폴더인가요?")?.contains("기본 폴더") == true)
    state.userText = "a.txt를 다른 폴더로 옮겨줘"
    #expect(freeTextRefusal(state, "대상 폴더의 경로를 알려주세요.") == nil)
    state.userText = "/Users/a/docs 안에 뭐 있어?"
    #expect(freeTextRefusal(state, ask) == nil)
}

// Python parity: the same inputs through the Python rules (ParityData.swift).

@Test func rulesMatchPython() throws {
    let data = try JSON.parse(pythonParity)
    for row in data["texts"]?.arrayValue ?? [] {
        let text = row["t"]!.stringValue!
        func check(_ key: String, _ value: Bool) { #expect(row[key]?.boolValue == value, "\(key): \(text)") }
        check("destructive", destructiveRequest(text))
        check("move", operationRequested(moveTool, text))
        check("copy", operationRequested(copyTool, text))
        check("mkdir", operationRequested(mkdirTool, text))
        check("open", openRequested(text))
        check("wants", wantsContent(text))
        check("acts", actsWithoutReading(text))
        check("ambig", ambiguousMutation(text))
        check("cand", requiresCandidateSearch(text))
        check("confirm", isConfirmation(text))
        check("pdel", isPatternDelete(text))
        check("amb", hasAmbiguousUserURL(text))
        check("group", groupPattern.search(text))
        check("ranked", rankedPickPattern.search(text))
        check("conv", conversionRequestPattern.search(text))
        check("mutation", mutationPattern.search(text))
        check("other", otherTaskPattern.search(text))
        #expect(row["urls"]?.arrayValue?.compactMap(\.stringValue) == extractUserURLs(text), "urls: \(text)")
        #expect(row["masked"]?.stringValue == withoutQuotedText(text), "masked: \(text)")
    }
    for row in data["urls"]?.arrayValue ?? [] {
        let url = row["u"] ?? .null
        #expect(row["strict"]?.stringValue == webURLError(url, allowLocal: false), "strict: \(url)")
        #expect(row["local"]?.stringValue == webURLError(url, allowLocal: true), "local: \(url)")
    }
    let names = ["report-final.txt", "report.txt", "보고서.md", "보고서_최종.md", "notes.md", "rep.txt", String(repeating: "x", count: 250)]
    for row in data["close"]?.arrayValue ?? [] {
        let word = row["w"]!.stringValue!
        #expect(row["r"]?.arrayValue?.compactMap(\.stringValue) == closeMatches(word, names, n: 3, cutoff: 0.4))
    }
}

// Rules that touch the disk or the allowed roots.

@Test func deleteScopeOnlyAllowsNamedOrFoundFiles() throws {
    let root = URL(fileURLWithPath: "/r")
    let paths: JSON = ["paths": ["/r/a.txt"]]
    #expect(deleteScopeError(paths, "a.txt 삭제해줘", [root]) == nil)
    #expect(deleteScopeError(paths, "b.txt 삭제해줘", [root]) != nil)
    #expect(deleteScopeError(paths, "b.txt 삭제해줘", [root], seen: ["/r/a.txt"]) == nil)
    #expect(deleteScopeError(paths, "a.txt 읽어줘", [root]) != nil)
    #expect(deleteScopeError(paths, "x/a.txt 삭제해줘", [root]) == "삭제 파일 경로가 사용자 명령의 경로와 일치하지 않습니다.")
    let glob: JSON = ["directory": "/r/logs", "pattern": "*.log"]
    #expect(deleteScopeError(glob, "logs 폴더의 *.log 삭제해줘", [root]) == nil)
    #expect(deleteScopeError(glob, "r/logs 폴더의 *.log 삭제해줘", [root]) == nil)
    #expect(deleteScopeError(glob, "다른 폴더의 *.log 삭제해줘", [root]) == "일괄 삭제 폴더가 사용자 명령에 명시되지 않았습니다.")
    #expect(deleteScopeError(["directory": "/r/logs", "pattern": "*"], "logs 폴더 삭제해줘", [root]) != nil)
    #expect(deleteScopeError(["directory": "/r/logs", "pattern": "*"], "logs 폴더 전부 삭제해줘", [root]) == nil)
    #expect(deleteScopeError(glob, "https://x.com logs *.log 삭제해줘", [root]) != nil)
}

@Test func missingPathGetsCloseRealNamesAndNFDNamesAreFound() throws {
    let base = URL(fileURLWithPath: realPath(NSTemporaryDirectory())!).appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: base) }
    let decomposed = "보고서.txt".decomposedStringWithCanonicalMapping
    FileManager.default.createFile(atPath: base.path + "/" + decomposed, contents: Data())
    let call = ToolCall("1", "filesystem__read_text_file", #"{"path": "\#(base.path)/보고서.md"}"#)
    #expect(missingPathHint(call, [base]).contains("\(base.path)/보고서.txt"))
    #expect(missingPathHint(call, [URL(fileURLWithPath: "/elsewhere")]) == "")
    let found = onDiskPath(.string(base.path + "/보고서.txt"), [base]).stringValue!
    #expect(Array(found.unicodeScalars) == Array((base.path + "/" + decomposed).unicodeScalars))
}

@Test func smallHelpersMatchPython() {
    let call = ToolCall("1", "filesystem__read_text_file", #"{"path": "/a", "head": 3}"#)
    #expect(partialRead(call, ToolResult(ok: true, text: "1\n2\n3\n")) == "앞 3줄")
    #expect(partialRead(call, ToolResult(ok: true, text: "1\n2")) == nil)
    let whole = ToolCall("1", "filesystem__read_text_file", #"{"path": "/a"}"#)
    let cut = ToolResult(ok: true, text: String(repeating: "가", count: 1234) + truncationMarker, truncated: true)
    #expect(partialRead(whole, cut) == "앞부분 약 1,234자")
    #expect(callKey(ToolCall("1", "t", #"{"b": 1, "a": "é"}"#), "x") == callKey(ToolCall("2", "t", #"{"a":"é","b":1}"#), "x"))
    #expect(pageURL("### Page\n  - Page URL: https://a.com \n") == "https://a.com")
    #expect(navigationResultForModel("- Page URL: u\n- Page Title: t\nbody") == "- Page URL: u\n- Page Title: t\n페이지 내용은 web__browser_snapshot으로 읽어야 합니다.")
    #expect(memoryBlock(Exchange("a", "b")).hasSuffix("- 다룬 파일: 없음"))
    #expect(requiresWebContent("https://a.com 요약해줘", ["https://a.com"]))
    #expect(!requiresWebContent("https://a.com URL을 설명해줘", ["https://a.com"]))
    #expect(writeTargetIsExplicit("/r/summary.md", "summary.md에 저장", []))
    #expect(!writeTargetIsExplicit("/r/summary.md", "old-summary.md에 저장", []))
}

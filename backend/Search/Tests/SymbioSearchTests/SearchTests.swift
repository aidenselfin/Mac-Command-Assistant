import Foundation
import SymbioCore
import Testing
@testable import SymbioSearch

// Ported from tests/test_file_search.py, test_list_files.py and test_file_grouping.py.
// Cases that need the planner or risk_guard stay with those modules.

final class Tmp {
    let url: URL
    init() {
        let base = realPath(NSTemporaryDirectory())!
        url = URL(fileURLWithPath: base).appendingPathComponent("symbio-search-\(UUID().uuidString)")
        try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }
    deinit { try? FileManager.default.removeItem(at: url) }
    var path: String { url.path }
    var name: String { url.lastPathComponent }
    func at(_ relative: String) -> URL { url.appendingPathComponent(relative) }
}

func write(_ url: URL, _ text: String) {
    try! FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try! Data(text.utf8).write(to: url)
}

func mkdirs(_ url: URL) { try! FileManager.default.createDirectory(at: url, withIntermediateDirectories: true) }

func touch(_ url: URL, _ time: Double) {
    try! FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSince1970: time)], ofItemAtPath: url.path)
}

func link(_ url: URL, to target: URL) {
    try! FileManager.default.createSymbolicLink(atPath: url.path, withDestinationPath: target.path)
}

func strings(_ items: [String]) -> JSON { .array(items.map(JSON.string)) }

func find(_ root: URL, _ keywords: [String], _ extra: [(String, JSON)] = [], limits: SearchLimits = SearchLimits()) -> JSON {
    try! findFiles(.object([("keywords", strings(keywords))] + extra), roots: [root], limits: limits).structured!
}

func names(_ response: JSON) -> [String] {
    (response["candidates"]?.arrayValue ?? []).map { ($0["path"]!.stringValue! as NSString).lastPathComponent }
}

func localStamp(_ y: Int, _ m: Int, _ d: Int, _ h: Int = 0, _ min: Int = 0, _ s: Double = 0) -> Double {
    let date = Calendar.current.date(from: DateComponents(year: y, month: m, day: d, hour: h, minute: min))!
    return date.timeIntervalSince1970 + s
}

@Suite struct FindFilesTests {
    @Test func nameMatchIsTheFirstStageAndStopsThere() {
        let t = Tmp()
        write(t.at("회의록.md"), "본문")
        write(t.at("other.txt"), "회의록 이야기")
        let response = find(t.url, ["회의록"])
        #expect(response["stage"] == "name" && response["decision"] == "single")
        #expect(names(response) == ["회의록.md"])
    }

    @Test func nfdFileNameMatchesNfcKeyword() {
        let t = Tmp()
        write(t.at("회의록.md".decomposedStringWithCanonicalMapping), "x")
        #expect(find(t.url, ["회의록".precomposedStringWithCanonicalMapping])["total"] == 1)
    }

    @Test func englishVariantAndSeparators() {
        let t = Tmp()
        write(t.at("Team_Meeting-Minutes.txt"), "x")
        #expect(names(find(t.url, ["회의록", "meeting minutes"])) == ["Team_Meeting-Minutes.txt"])
    }

    @Test func widensToFolderPathThenContent() {
        let t = Tmp()
        write(t.at("프로젝트A/기획/a.md"), "x")
        #expect(find(t.url, ["기획"])["stage"] == "path")
        write(t.at("memo.txt"), "예산은 3천만원으로 확정")
        let response = find(t.url, ["예산"])
        #expect(response["stage"] == "content" && names(response) == ["memo.txt"])
        let none = find(t.url, ["존재하지않는단어"])
        #expect(none == ["stage": "none", "decision": "none", "total": 0, "truncated": false, "complete": true])
        #expect(none.text == #"{"stage": "none", "decision": "none", "total": 0, "truncated": false, "complete": true}"#)
    }

    @Test func contentSearchSkipsNonTextAndReadsOnlyTheHead() {
        let t = Tmp()
        var limits = SearchLimits()
        limits.contentBytes = 10
        write(t.at("late.txt"), String(repeating: "가", count: 20) + "찾는말")
        write(t.at("image.png"), "찾는말")
        #expect(find(t.url, ["찾는말"], limits: limits)["decision"] == "none")
    }

    @Test func contentScanLimitMarksTruncated() {
        let t = Tmp()
        var limits = SearchLimits()
        limits.contentMaxFiles = 2
        for index in 0..<5 { write(t.at("f\(index).txt"), "nothing") }
        let response = find(t.url, ["zzz"], limits: limits)
        #expect(response["truncated"] == true && response["note"]!.stringValue!.contains("한도"))
        #expect(response["complete"] == false)
    }

    @Test func subfoldersSymlinksHiddenAndExcludedDirs() {
        let t = Tmp()
        let outside = Tmp()
        write(t.at("a/b/보고서.md"), "x")
        write(t.at("node_modules/보고서.md"), "x")
        write(t.at(".hidden/보고서.md"), "x")
        write(outside.at("보고서-밖.md"), "x")
        link(t.at("link"), to: outside.url)
        link(t.at("보고서-링크.md"), to: outside.at("보고서-밖.md"))
        let response = find(t.url, ["보고서"])
        #expect(response["total"] == 1)
        #expect(response["candidates"]!.arrayValue![0]["path"]!.stringValue!.hasSuffix("a/b/보고서.md"))
    }

    @Test func depthAndVisitLimitsMarkTruncated() {
        let t = Tmp()
        write(t.at("a/b/c/d/x.md"), "x")
        var limits = SearchLimits()
        limits.maxDepth = 2
        #expect(find(t.url, ["x"], limits: limits)["truncated"] == true)
        limits.maxDepth = 8
        limits.maxVisited = 2
        #expect(find(t.url, ["x"], limits: limits)["truncated"] == true)
    }

    @Test func filtersExtensionFolderAndDate() {
        let t = Tmp()
        write(t.at("자료/메모.txt"), "x")
        write(t.at("메모.md"), "x")
        #expect(names(find(t.url, ["메모"], [("extensions", [".md"])])) == ["메모.md"])
        #expect(names(find(t.url, ["메모"], [("folder_hint", "자료")])) == ["메모.txt"])
        touch(t.at("메모.md"), 0)
        #expect(names(find(t.url, ["메모"], [("modified_after", "2020-01-01")])) == ["메모.txt"])
    }

    @Test func twoToFiveCandidatesAreListedWithLabels() throws {
        let t = Tmp()
        for index in 1...3 { write(t.at("계획\(index).md"), String(repeating: "x", count: index)) }
        let spec = try #require(clarificationSpec(find(t.url, ["계획"])))
        #expect(spec.kind == "file_candidate" && spec.options.count == 3 && spec.labels.count == 3)
        #expect(spec.options.allSatisfy { $0.hasPrefix(t.path) })
        #expect(!spec.labels[0].contains("KB") && spec.labels[0].contains("B"))
    }

    @Test func numberedSeriesIsSummarizedNotListed() throws {
        let t = Tmp()
        for index in 1...37 { write(t.at("임시-\(index).txt"), "x") }
        touch(t.at("임시-12.txt"), 4_000_000_000)
        let response = find(t.url, ["임시"])
        #expect(response["decision"] == "narrow" && response["candidates"] == nil)
        #expect(response["series"]?["count"] == 37)
        #expect(response["series"]?["range"] == "임시-1 ~ 임시-37")
        #expect(response["series"]?["newest"] == .string(t.at("임시-12.txt").path))
        let spec = try #require(clarificationSpec(response))
        #expect(spec.kind == "narrow_search" && spec.options[0] == t.at("임시-12.txt").path)
        #expect(spec.question.contains("37개") && spec.options.count <= 1 + maxFacets)
    }

    @Test func manyUnrelatedCandidatesOfferFolderAndTypeFacets() throws {
        let t = Tmp()
        for folder in ["가", "나"] {
            for letter in ["a", "b", "c"] { write(t.at("\(folder)/보고서\(letter).md"), "x") }
        }
        write(t.at("나/보고서z.txt"), "x")
        let spec = try #require(clarificationSpec(find(t.url, ["보고서"])))
        #expect(spec.kind == "narrow_search" && spec.question.contains("7개"))
        let folder = "폴더: \(t.name)/가"
        #expect(spec.options.contains(folder) && spec.options.contains("형식: .md"))
        #expect(spec.labels[spec.options.firstIndex(of: folder)!].hasSuffix("(3개)"))
    }

    @Test func singleAndNoneNeedNoQuestion() {
        let t = Tmp()
        write(t.at("a.md"), "x")
        #expect(clarificationSpec(find(t.url, ["a"])) == nil)
        #expect(clarificationSpec(find(t.url, ["zzz"])) == nil)
    }

    @Test func numberInKeywordDoesNotMatchLongerNumber() {
        let t = Tmp()
        write(t.at("임시-1.txt"), "x")
        write(t.at("임시-12.txt"), "x")
        #expect(names(find(t.url, ["임시-1"])) == ["임시-1.txt"])
    }

    @Test(arguments: [
        [:], ["keywords": []], ["keywords": [" "]], ["keywords": ["a"], "modified_after": "어제"],
    ] as [JSON])
    func invalidArgumentsAreRejected(arguments: JSON) {
        let t = Tmp()
        #expect(throws: ToolError.self) { try findFiles(arguments, roots: [t.url]) }
        do { _ = try findFiles(arguments, roots: [t.url]) } catch { #expect(error.isRejected) }
    }

    @Test func absoluteRootPathAsFolderHintMeansNoFolderFilter() {
        let t = Tmp()
        write(t.at("자료/임시-1.txt"), "x")
        #expect(find(t.url, ["임시"], [("folder_hint", .string(t.path))])["total"] == 1)
        #expect(find(t.url, ["임시"], [("folder_hint", .string(t.at("자료").path))])["total"] == 1)
    }

    @Test func conditionsTheUserNamedAreNeverDroppedAutomatically() {
        let t = Tmp()
        write(t.at("budget.md"), "x")
        write(t.at("자료/budget-old.md"), "x")
        let cases: [(String, JSON)] = [
            ("extensions", ["pdf"]), ("folder_hint", "없는폴더"), ("modified_before", "2000-01-01"),
        ]
        for extra in cases {
            let response = find(t.url, ["budget"], [extra])
            #expect(response["decision"] == "none" && response["total"] == 0)
            #expect(response["note"]!.stringValue!.contains("조건") && response["filters_relaxed"] == nil)
        }
        #expect(find(t.url, ["존재안함"])["note"] == nil)
    }

    @Test func emptyOptionalFieldsAndStrayKeysAreIgnored() {
        let t = Tmp()
        write(t.at("a.md"), "x")
        let response = find(t.url, ["a"], [
            ("extensions", [""]), ("folder_hint", ""), ("modified_after", ""), ("maxItems", 8),
        ])
        #expect(response["total"] == 1 && response["filters_relaxed"] == nil)
    }

    @Test func exactFileNameWithExtensionAndNullOptionals() {
        let t = Tmp()
        write(t.at("old.log"), "x")
        write(t.at("keep.txt"), "x")
        let response = find(t.url, ["old.log"], [
            ("extensions", nil), ("folder_hint", nil), ("modified_after", nil), ("modified_before", nil),
        ])
        #expect(names(response) == ["old.log"])
    }

    @Test func modifiedAfterIncludesTheWholeDayAndBeforeExcludesTheNext() {
        let t = Tmp()
        func make(_ name: String, _ stamp: Double) {
            write(t.at(name), "x")
            touch(t.at(name), stamp)
        }
        make("eve.md", localStamp(2026, 10, 6, 23, 59, 59))
        make("morning.md", localStamp(2026, 10, 7))
        make("afternoon.md", localStamp(2026, 10, 7, 14))
        make("late.md", localStamp(2026, 10, 7, 23, 59, 59.5))
        make("next.md", localStamp(2026, 10, 8))
        let both: [(String, JSON)] = [("modified_after", "2026-10-07"), ("modified_before", "2026-10-07")]
        #expect(names(find(t.url, ["md"], both)).sorted() == ["afternoon.md", "late.md", "morning.md"])
        #expect(!names(find(t.url, ["md"], [("modified_after", "2026-10-07")])).contains("eve.md"))
    }

    @Test func reversedDateRangeIsRejected() {
        let t = Tmp()
        #expect(throws: ToolError.rejected("modified_after가 modified_before보다 늦습니다.")) {
            try findFiles(
                ["keywords": ["a"], "modified_after": "2026-10-08", "modified_before": "2026-10-07"], roots: [t.url]
            )
        }
    }

    @Test func contentReadRefusesAFolderSwappedForALinkAfterListing() throws {
        let t = Tmp()
        write(t.at("root/sub/note.txt"), "안쪽 글")
        write(t.at("outside/note.txt"), "외부비밀단어")
        let (entries, _) = walk([t.at("root").path], SearchLimits())
        #expect(entries.count == 1)
        #expect(try readHead(entries[0], SearchLimits()).contains("안쪽"))
        try FileManager.default.removeItem(at: t.at("root/sub"))
        link(t.at("root/sub"), to: t.at("outside"))
        #expect(throws: OSFailure.self) { try readHead(entries[0], SearchLimits()) }
    }

    @Test func searchNeverReportsAWordFoundThroughASwappedFolder() throws {
        // Python monkeypatches _walk to swap after listing; here the stages run by hand.
        let t = Tmp()
        write(t.at("root/sub/note.txt"), "평범")
        write(t.at("outside/note.txt"), "외부비밀단어")
        let (entries, _) = walk([t.at("root").path], SearchLimits())
        try FileManager.default.removeItem(at: t.at("root/sub"))
        link(t.at("root/sub"), to: t.at("outside"))
        let (stage, matches, coverage) = search(entries, [keywordPattern("외부비밀단어")], SearchLimits())
        #expect(stage == "none" && matches.isEmpty && coverage.unreadable == 1)
    }

    @Test func aRootReplacedByALinkIsNotSearched() {
        let t = Tmp()
        write(t.at("real/a.md"), "x")
        link(t.at("link"), to: t.at("real"))
        #expect(find(t.at("link"), ["a"])["decision"] == "none")
    }

    @Test func textBeyondTheScannedHeadIsReportedAsNotFullySearched() {
        let t = Tmp()
        var limits = SearchLimits()
        limits.contentBytes = 10
        write(t.at("late.txt"), String(repeating: "가", count: 20) + "찾는말")
        let response = find(t.url, ["찾는말"], limits: limits)
        #expect(response["decision"] == "none" && response["complete"] == false)
        let note = response["note"]!.stringValue!
        #expect(note.contains("앞") && note.contains("단정하지 말고"))
    }

    @Test func oneHitAmongPartlySearchedFilesIsConfirmedNotAssumed() throws {
        let t = Tmp()
        var limits = SearchLimits()
        limits.contentBytes = 10
        write(t.at("hit.txt"), "찾는말")
        write(t.at("long.txt"), String(repeating: "가", count: 30))
        let response = find(t.url, ["찾는말"], limits: limits)
        #expect(response["stage"] == "content" && response["decision"] == "choose")
        let spec = try #require(clarificationSpec(response))
        #expect(spec.kind == "file_candidate" && spec.options == [t.at("hit.txt").path])
        #expect(spec.question.contains("일부만 검사"))
    }

    @Test func documentsAndUnreadableFilesAreNamedInTheLimits() {
        let t = Tmp()
        write(t.at("보고서.pdf"), "%PDF")
        write(t.at("locked.txt"), "x")
        chmod(t.at("locked.txt").path, 0)
        defer { chmod(t.at("locked.txt").path, 0o600) }
        let response = find(t.url, ["예산"])
        #expect(response["decision"] == "none" && response["complete"] == false)
        let note = response["note"]!.stringValue!
        #expect(note.contains("문서 1개") && note.contains("읽지 못한 파일"))
    }

    @Test func aFullyReadSmallTreeIsComplete() {
        let t = Tmp()
        write(t.at("a.txt"), "예산 3천만원")
        let response = find(t.url, ["예산"])
        #expect(response["decision"] == "single" && response["complete"] == true && response["note"] == nil)
    }

    @Test func aHugeFolderIsNeverListedWhole() throws {
        let t = Tmp()
        for index in 0..<50 { write(t.at(String(format: "f%02d.txt", index)), "x") }
        let (rows, more) = try listDir(t.path, [], limit: 10)
        #expect(rows.count == 10 && more)
        var limits = SearchLimits()
        limits.maxVisited = 10
        let (entries, truncated) = walk([t.path], limits)
        #expect(entries.count <= 10 && truncated)
    }

    @Test func walkTimeLimitMarksTruncated() {
        let t = Tmp()
        write(t.at("a/x.txt"), "x")
        var limits = SearchLimits()
        limits.walkSeconds = -1
        let (entries, truncated) = walk([t.path], limits)
        #expect(entries.isEmpty && truncated)
    }

    @Test func sameLeafFolderNamesStayDistinctButtonsAndFilterCorrectly() {
        let t = Tmp()
        for project in ["프로젝트A", "프로젝트B"] {
            for index in 0..<4 { write(t.at("\(project)/자료/보고\(index).md"), "x") }
        }
        let response = find(t.url, ["보고"])
        let options = response["narrow_options"]!.arrayValue!.map { $0["option"]!.stringValue! }
        let a = "폴더: \(t.name)/프로젝트A/자료"
        #expect(options.contains(a) && options.contains("폴더: \(t.name)/프로젝트B/자료"))
        let onlyA = find(t.url, ["보고"], [("folder_hint", .string(String(a.dropFirst("폴더: ".count))))])
        let projects = Set(onlyA["candidates"]!.arrayValue!.map { $0["path"]!.stringValue!.split(separator: "/").reversed()[2] })
        #expect(projects == ["프로젝트A"])
    }

    @Test func sameRelativeFolderInTwoRootsIsToldApartByRoot() throws {
        let t = Tmp()
        for name in ["rootA", "rootB"] { write(t.at("\(name)/자료/보고.md"), "x") }
        let result = try findFiles(["keywords": ["보고"], "folder_hint": "rootB/자료"], roots: [t.at("rootA"), t.at("rootB")])
        #expect(result.structured!["candidates"]!.arrayValue!.map { $0["path"]!.stringValue! } == [t.at("rootB/자료/보고.md").path])
    }

    // review R1: a content clue narrows the candidates the name already found

    func makeProposals(_ t: Tmp) {
        for index in 1...7 { write(t.at("proposal-\(index).txt"), index == 3 ? "예산 승인 건" : "일반 내용") }
    }

    @Test func aContentClueNarrowsNameCandidatesThatTheNameAloneCouldNot() {
        let t = Tmp()
        makeProposals(t)
        #expect(find(t.url, ["proposal"])["decision"] == "narrow")
        let response = find(t.url, ["proposal"], [("content_keywords", ["예산 승인"])])
        #expect(response["stage"] == "name+content" && response["decision"] == "single")
        #expect(names(response) == ["proposal-3.txt"] && response["complete"] == true)
    }

    @Test func theContentClueStaysInsideTheNameAndOtherConditions() {
        let t = Tmp()
        makeProposals(t)
        write(t.at("other.txt"), "예산 승인 건")
        write(t.at("자료/proposal-9.txt"), "예산 승인 건")
        let inside = find(t.url, ["proposal"], [("content_keywords", ["예산 승인"]), ("folder_hint", "자료")])
        #expect(names(inside) == ["proposal-9.txt"])
        let both = find(t.url, ["proposal"], [("content_keywords", ["예산 승인"])])
        #expect(names(both).sorted() == ["proposal-3.txt", "proposal-9.txt"])
    }

    @Test func aClueNothingMatchesSaysSoAndNeverWidens() {
        let t = Tmp()
        makeProposals(t)
        let response = find(t.url, ["proposal"], [("content_keywords", ["존재하지 않는 말"])])
        #expect(response["decision"] == "none" && response["note"]!.stringValue!.contains("조건"))
    }

    @Test func candidatesThatCannotBeReadAreLeftOutAndReported() {
        let t = Tmp()
        write(t.at("proposal-1.txt"), "예산 승인")
        write(t.at("proposal-2.pdf"), "%PDF")
        let response = find(t.url, ["proposal"], [("content_keywords", ["예산"])])
        #expect(names(response) == ["proposal-1.txt"] && response["complete"] == false)
        #expect(response["limits"]!.arrayValue!.compactMap(\.stringValue).joined(separator: " ").contains("문서 1개"))
    }

    @Test func withoutAContentClueTheNameSearchIsUnchanged() {
        let t = Tmp()
        makeProposals(t)
        let response = find(t.url, ["proposal"])
        #expect(response["stage"] == "name" && response["total"] == 7)
    }

    // review R2: an incomplete search says what it did not look at

    @Test func anIncompleteNarrowQuestionSaysTheCountMayBeHigher() throws {
        let t = Tmp()
        var limits = SearchLimits()
        limits.maxVisited = 8
        for index in 0..<30 { write(t.at(String(format: "보고서-%02d.md", index)), "x") }
        let response = find(t.url, ["보고서"], limits: limits)
        #expect(response["complete"] == false && !(response["limits"]!.arrayValue!.isEmpty))
        #expect(try #require(clarificationSpec(response)).question.contains("일부만 검사한 결과"))
    }

    @Test func oversizedListsAreCutInCodeInsteadOfFailingTheProviderCheck() {
        let t = Tmp()
        write(t.at("a.md"), "x")
        let response = find(t.url, ["a"] + (0..<20).map { "쓸데없는\($0)" }, [
            ("extensions", strings((0..<30).map { "e\($0)" } + ["md"])),
            ("content_keywords", strings((0..<30).map { "단어\($0)" })),
        ])
        #expect(["none", "single", "choose"].contains(response["decision"]!.stringValue!))
        let properties = findFilesDefinition.schema["properties"]!.objectValue!
        #expect(!properties.contains { $0.1["maxItems"] != nil || $0.1["maxLength"] != nil })
    }
}

// MARK: - list_files

let daySeconds = 86400.0

func make(_ root: URL, _ name: String, _ size: Int, _ ageDays: Double = 0) {
    let url = root.appendingPathComponent(name)
    write(url, String(repeating: "x", count: size))
    touch(url, Date().timeIntervalSince1970 - ageDays * daySeconds)
}

func listed(_ root: URL, _ arguments: JSON = [:]) -> JSON {
    try! listFiles(arguments, roots: [root]).structured!
}

func fileNames(_ response: JSON) -> [String] { response["files"]!.arrayValue!.map { $0["name"]!.stringValue! } }

@Suite struct ListFilesTests {
    @Test func sortsBySizeLargestFirstWithBytes() {
        let t = Tmp()
        make(t.url, "a.txt", 100)
        make(t.url, "b.txt", 500)
        make(t.url, "c.txt", 300)
        let response = listed(t.url, ["sort_by": "size"])
        #expect(fileNames(response) == ["b.txt", "c.txt", "a.txt"])
        #expect(response["files"]!.arrayValue![0]["size_bytes"] == 500)
    }

    @Test func sortsByModifiedNewestFirstAndLimit() {
        let t = Tmp()
        for (name, age) in [("a.txt", 5.0), ("b.txt", 1), ("c.txt", 3)] { make(t.url, name, 1, age) }
        #expect(fileNames(listed(t.url, ["sort_by": "modified"])) == ["b.txt", "c.txt", "a.txt"])
        let response = listed(t.url, ["sort_by": "modified", "limit": 2])
        #expect(fileNames(response) == ["b.txt", "c.txt"])
        #expect(response["total"] == 3 && response["shown"] == 2)
    }

    @Test func ageWindowAndExtensionFilters() {
        let t = Tmp()
        make(t.url, "new.csv", 1, 1)
        make(t.url, "old.csv", 1, 10)
        make(t.url, "new.txt", 1, 1)
        make(t.url, "x.pdf", 1, 45)
        make(t.url, "y.pdf", 1, 70)
        make(t.url, "w.pdf", 1, 5)
        #expect(fileNames(listed(t.url, ["modified_within_days": 3, "extensions": ["csv"]])) == ["new.csv"])
        let window = listed(t.url, ["extensions": [".PDF"], "modified_within_days": 60, "not_modified_for_days": 30])
        #expect(fileNames(window) == ["x.pdf"])
    }

    @Test func folderTotalsLargestFirst() {
        let t = Tmp()
        make(t.url, "큰폴더/a.txt", 900)
        make(t.url, "작은폴더/b.txt", 10)
        make(t.url, "중간폴더/c.txt", 300)
        make(t.url, "중간폴더/sub/d.txt", 300)
        make(t.url, "top.txt", 5000)
        let response = listed(t.url, ["group_by": "folder"])
        let folders = response["folders"]!.arrayValue!
        #expect(folders.map { $0["folder"]!.stringValue! } == ["큰폴더", "중간폴더", "작은폴더"])
        #expect(folders[1]["size_bytes"] == 600)
    }

    @Test func rejectsBadArguments() {
        let t = Tmp()
        #expect(throws: ToolError.rejected("잘못된 local__list_files 인자입니다.")) {
            try listFiles(["sort_by": "colour"], roots: [t.url])
        }
        #expect(throws: ToolError.rejected("일 수는 0 이상이어야 합니다.")) {
            try listFiles(["modified_within_days": -1], roots: [t.url])
        }
    }

    @Test func symlinkOutsideRootIsNotListed() {
        let t = Tmp()
        make(t.at("outside"), "secret.txt", 50)
        mkdirs(t.at("root"))
        link(t.at("root/link"), to: t.at("outside"))
        link(t.at("root/file-link.txt"), to: t.at("outside/secret.txt"))
        #expect(listed(t.at("root"))["files"] == [])
    }

    @Test func nameContainsNarrowsBeforeTheLimit() {
        let t = Tmp()
        make(t.url, "계약서_v1.txt", 1, 40)
        make(t.url, "계약서_v2.txt", 1, 5)
        for index in 0..<25 { make(t.url, "other\(index).txt", 1, 0) }
        let response = listed(t.url, ["name_contains": ["계약서"], "sort_by": "modified", "limit": 1])
        #expect(fileNames(response) == ["계약서_v2.txt"])
    }

    @Test func pathsLimitToCandidatesAndRootsKeepSameNamedFoldersApart() throws {
        let t = Tmp()
        let one = t.at("one"), two = t.at("two")
        make(one, "자료/a.txt", 10)
        make(two, "자료/b.txt", 30)
        let response = try listFiles(["group_by": "folder"], roots: [one, two]).structured!
        #expect(response["folders"]!.arrayValue!.map { $0["folder"]!.stringValue! } == [
            two.appendingPathComponent("자료").path, one.appendingPathComponent("자료").path,
        ])
        let only = try listFiles(["paths": [.string(one.appendingPathComponent("자료/a.txt").path)]], roots: [one, two])
        #expect(fileNames(only.structured!) == ["a.txt"])
    }

    @Test func tooManyPathsAreRefused() {
        let t = Tmp()
        let paths = strings((0..<51).map { t.at("\($0).txt").path })
        #expect(throws: ToolError.self) { try listFiles(["paths": paths], roots: [t.url]) }
    }
}

// MARK: - group_files

func group(_ root: URL, _ arguments: JSON = [:], limits: SearchLimits = SearchLimits()) -> ToolResult {
    try! groupFiles(arguments, roots: [root], limits: limits)
}

func labels(_ result: ToolResult) -> [String: [String]] {
    var out: [String: [String]] = [:]
    for item in result.structured!["groups"]!.arrayValue! {
        out[item["label"]!.stringValue!] = item["files"]!.arrayValue!
            .map { ($0.stringValue! as NSString).lastPathComponent }.sorted { pyLess($0, $1) }
    }
    return out
}

func makeFiles(_ root: URL, _ names: [String]) {
    for name in names { write(root.appendingPathComponent(name), "비밀 내용") }
}

@Suite struct GroupFilesTests {
    @Test func keywordGroupsMatchNamesOnly() {
        let t = Tmp()
        makeFiles(t.url, ["회의록_1월.md", "회의록 2월.txt", "Invoice-03.pdf", "memo.txt"])
        let result = group(t.url, ["keywords": ["회의록", "invoice"]])
        #expect(labels(result) == ["회의록": ["회의록 2월.txt", "회의록_1월.md"], "invoice": ["Invoice-03.pdf"]])
        #expect(!result.text.contains("비밀"))
    }

    @Test func similarNamesAndKinds() {
        let t = Tmp()
        makeFiles(t.url, ["IMG_0001.jpg", "IMG_0002.jpg", "보고서_최종.docx", "보고서_초안.docx", "a.png", "single.txt"])
        #expect(labels(group(t.url, ["by": "name"])) == [
            "img": ["IMG_0001.jpg", "IMG_0002.jpg"], "보고서": ["보고서_초안.docx", "보고서_최종.docx"],
        ])
        let byKind = labels(group(t.url, ["by": "kind"]))
        #expect(byKind["이미지"] == ["IMG_0001.jpg", "IMG_0002.jpg", "a.png"])
        #expect(byKind["문서"] == ["single.txt", "보고서_초안.docx", "보고서_최종.docx"])
    }

    @Test func filesAlreadyInTheMatchingFolderAreLeftOut() {
        let t = Tmp()
        makeFiles(t.url, ["보고서_a.md", "보고서_b.md", "보고서/보고서_c.md"])
        #expect(labels(group(t.url, ["keywords": ["보고서"]]))["보고서"] == ["보고서_a.md", "보고서_b.md"])
    }

    @Test func largeGroupIsCutAndCounted() {
        let t = Tmp()
        var limits = SearchLimits()
        limits.groupShow = 2
        makeFiles(t.url, (0..<5).map { "scan_\($0).pdf" })
        let item = group(t.url, ["keywords": ["scan"]], limits: limits).structured!["groups"]!.arrayValue![0]
        #expect(item["count"] == 5 && item["files"]!.arrayValue!.count == 2)
    }

    @Test func symlinkedFileIsNotGrouped() {
        let t = Tmp()
        makeFiles(t.at("outside"), ["secret_1.txt"])
        makeFiles(t.at("root"), ["secret_2.txt"])
        link(t.at("root/secret_1.txt"), to: t.at("outside/secret_1.txt"))
        #expect(labels(group(t.at("root"), ["keywords": ["secret"]])) == ["secret": ["secret_2.txt"]])
    }

    @Test func badArgumentsAreRejected() {
        let t = Tmp()
        #expect(throws: ToolError.rejected("잘못된 local__group_files 인자입니다.")) {
            try groupFiles(["by": "color"], roots: [t.url])
        }
    }
}

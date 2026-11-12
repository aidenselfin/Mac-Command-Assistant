import Foundation
import SymbioConvert
import SymbioCore
import SymbioDelete
import SymbioFileOps
import Testing
@testable import SymbioPlanner

/// Planner flows other packages could not test without a planner: tests/test_finder.py
/// (changed_paths), test_deletion.py, test_file_operations.py, test_conversion.py, test_undo.py.
/// RuntimeController steps are replaced by the planner calls it makes (approve/reject/undo).

private func call(_ name: String, _ arguments: JSON, id: String? = nil) -> ModelTurn {
    turn(nil, ToolCall(id ?? name, name, arguments.dumps(JSONDumpOptions(ensureASCII: false))))
}

private let rwTools = [tool("filesystem__read_text_file"), tool("filesystem__write_file")]

// MARK: - test_finder.py: which files a command changed

@Test func writeSuccessReportsTheExactPathTheAppWrote() async throws {
    let config = try makeConfig()
    let target = config.path("한글 요약.txt")
    let model = ScriptedModel(turns: [
        call("filesystem__write_file", ["path": .string(target), "content": "x"]), turn("저장했습니다. /etc/passwd에 있습니다."),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: rwTools, fallback: ok("ok")), model)
    let pending = try #require(try await planner.run("한글 요약.txt에 x 저장해줘").approval)
    // The model's sentence names another path; the result carries only what was written.
    #expect(try await planner.approve(pending.context).completed?.changedPaths == [target])
}

@Test func nothingIsReportedBeforeApprovalOrAfterAReject() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(tools: rwTools, fallback: ok("ok"))
    let model = ScriptedModel(turns: [call("filesystem__write_file", ["path": .string(config.path("a.txt")), "content": "x"])])
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("a.txt에 x 저장해줘").approval)
    #expect(planner.reject(pending.context).changedPaths.isEmpty)
    #expect(gateway.calls.isEmpty)
}

@Test func aFailedWriteReportsNoPath() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(tools: rwTools, fallback: ToolResult(ok: false, text: "disk full", error: "disk full"))
    let model = ScriptedModel(turns: [
        call("filesystem__write_file", ["path": .string(config.path("a.txt")), "content": "x"]), turn("실패했습니다."),
    ])
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("a.txt에 x 저장해줘").approval)
    #expect(try await planner.approve(pending.context).completed?.changedPaths ?? [] == [])
}

@Test func aReadOnlyAnswerChangesNothing() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("a.txt"))
    let model = ScriptedModel(turns: [call("filesystem__read_text_file", ["path": .string(config.path("a.txt"))]), turn("x입니다.")])
    let outcome = try await makePlanner(config, FakeGateway(tools: rwTools, fallback: ok("x")), model).run("a.txt 읽어줘")
    #expect(outcome.completed?.changedPaths == [])
}

@Test func deleteReportsNoPathToReveal() async throws {
    let config = try makeConfig()
    let target = config.path("old.log")
    try writeText("x", target)
    let model = ScriptedModel(turns: [call(deleteTool, ["paths": [.string(target)]])])
    let planner = try makePlanner(config, FakeGateway(tools: rwTools), model)
    let pending = try #require(try await planner.run("old.log 삭제해줘").approval)
    #expect(pending.operation == "delete")
    #expect(try await planner.approve(pending.context).completed?.changedPaths == [])
    #expect(!fileExists(target))
}

@Test func conversionReportsTheNewFileNotTheSource() async throws {
    let config = try makeConfig()
    try writeText("hello", config.path("메모.txt"))
    let model = ScriptedModel(turns: [
        call(convertTool, ["source": .string(config.path("메모.txt")), "target_format": "html"]), turn("변환했습니다."),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: rwTools), model)
    let pending = try #require(try await planner.run("메모.txt를 html로 변환해줘").approval)
    let finished = try #require(try await planner.approve(pending.context).completed)
    #expect(finished.changedPaths == [config.path("메모.html")])
    #expect(fileExists(config.path("메모.html")))
}

// MARK: - test_deletion.py

@Test func batchDeleteApprovesListsAndUndoesWithoutGatewayDelete() async throws {
    let config = try makeConfig()
    let a = config.path("a.txt"), b = config.path("b.txt")
    for path in [a, b] { try writeText("original", path) }
    let gateway = FakeGateway(tools: [])
    let model = ScriptedModel(turns: [], repeating: call(deleteTool, ["paths": [.string(a), .string(b)]], id: "delete"))
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("a.txt와 b.txt를 삭제해줘").approval)
    #expect(pending.targets == [a, b])
    #expect(fileExists(a) && fileExists(b))
    #expect(try await planner.approve(pending.context).completed != nil)
    #expect(!fileExists(a) && !fileExists(b))
    #expect(try await planner.undoLastWrite().completed != nil)
    #expect(fileExists(a) && fileExists(b))
    #expect(gateway.calls.isEmpty && model.count == 1)
}

@Test func hallucinatedDeleteOnReadRequestNeverExecutes() async throws {
    let config = try makeConfig()
    let target = config.path("a.txt")
    try writeText("keep", target)
    let gateway = FakeGateway(tools: [])
    let model = ScriptedModel(turns: [call(deleteTool, ["paths": [.string(target)]], id: "delete"), turn("읽기 요청입니다.")])
    let outcome = try await makePlanner(config, gateway, model).run("a.txt 읽어줘")
    #expect(outcome.completed != nil && readText(target) == "keep")
    #expect(!model.calls[0].toolNames.contains(deleteTool))
    #expect(gateway.calls.isEmpty)
}

@Test func missingJournalBlocksDeletion() async throws {
    let config = try makeConfig()
    let model = ScriptedModel(turns: [
        call(deleteTool, ["paths": [.string(config.path("한글 메모.txt"))]], id: "delete"), turn("삭제할 수 없습니다."),
    ])
    let outcome = try await makePlanner(config, FakeGateway(tools: []), model, journal: false).run(#""한글 메모.txt" 삭제해줘"#)
    #expect(outcome.approval == nil)
    #expect(fileExists(config.path("한글 메모.txt")))
}

@Test(arguments: [1, 2], [false, true])
func describedDeleteSearchesThenSelectsAndAsksApproval(candidates: Int, approve: Bool) async throws {
    let config = try makeConfig()
    let target = config.path("회의록-2026-10-03.md")
    try writeText("지울 회의록", target)
    if candidates == 2 { try writeText("보존", config.path("회의록-2026-10-01.md")) }
    let request = "10월 3일 회의록 삭제해줘."
    let model = ScriptedModel(turns: [
        call("local__find_files", ["keywords": ["회의록"]], id: "f"), call(deleteTool, ["paths": [.string(target)]], id: "d"),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: []), model)
    let pending = try #require(try await planner.run(request).approval)
    #expect(pending.targets == [target] && fileExists(target))
    let last = try #require(model.calls.last)
    let user = last.messages[1]["content"]?.stringValue ?? ""
    #expect(user.contains(request))
    #expect(last.toolNames.contains(deleteTool))
    if approve {
        #expect(try await planner.approve(pending.context).completed != nil)
        #expect(!fileExists(target))
        #expect(try await planner.undoLastWrite().completed != nil)
    } else {
        _ = planner.reject(pending.context)
    }
    #expect(fileExists(config.path("한글 메모.txt")))
    #expect(readText(target) == "지울 회의록")
}

@Test func describedDeleteCannotSkipTheSearch() async throws {
    let config = try makeConfig()
    let target = config.path("회의록-2026-10-03.md")
    try writeText("보존", target)
    let model = ScriptedModel(turns: [
        call(deleteTool, ["paths": [.string(target)]], id: "d"), call("local__find_files", ["keywords": ["회의록"]], id: "f"),
        call(deleteTool, ["paths": [.string(target)]], id: "d2"),
    ])
    let pending = try await makePlanner(config, FakeGateway(tools: []), model).run("10월 3일 회의록 삭제해줘")
    #expect(readText(target) == "보존")
    #expect(model.calls[1].lastContent.contains("먼저 local__find_files"))
    #expect(pending.approval?.targets == [target])
}

@Test func clarificationAnswerCannotTurnAReadRequestIntoDeletion() async throws {
    let config = try makeConfig()
    let target = config.path("a.txt")
    try writeText("보존", target)
    let model = ScriptedModel(turns: [
        ModelTurn(nil, [], ClarificationRequest("어떤 항목을 추출할까요?", [], "free_text", "q")),
        call(deleteTool, ["paths": [.string(target)]], id: "d"), turn("삭제할 수 없습니다."),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: []), model)
    let question = try #require(try await planner.run("a.txt에서 필요한 항목을 추출해줘").clarification)
    #expect(try await planner.resume(question.context, "a.txt 삭제해줘").completed != nil)
    #expect(!model.calls[1].toolNames.contains(deleteTool))
    #expect(readText(target) == "보존")
}

// MARK: - test_file_operations.py

@Test func recentReportMoveRequestSearchesThenApprovesWithoutReading() async throws {
    let config = try makeConfig()
    let source = config.path("영어안내문.txt"), destination = config.path("삭제연습")
    try writeText("안내문", source)
    try FileManager.default.createDirectory(atPath: destination, withIntermediateDirectories: true)
    let gateway = FakeGateway(tools: [])
    let model = ScriptedModel(turns: [
        call("local__find_files", ["keywords": ["영어안내문"]]),
        call(moveTool, ["source": .string(source), "destination": .string(destination)]), turn("이동 완료"),
    ])
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("영어안내문 파일 삭제연습 폴더로 이동시켜줘.").approval)
    #expect(pending.operation == "move" && fileExists(source))
    #expect(model.calls[0].toolNames.contains(moveTool))
    #expect(model.calls[0].system.contains("파일 삭제, 이동"))
    #expect(pending.targets == [source] && pending.targetPath.hasPrefix(destination))
    let finished = try #require(try await planner.approve(pending.context).completed)
    #expect(finished.changedPaths == [destination + "/영어안내문.txt"])
    #expect(gateway.calls.isEmpty && !fileExists(source))
    #expect(try await planner.undoLastWrite().completed != nil && fileExists(source))
}

@Test func moveAsksForDestinationAndUsesTheUsersAnswer() async throws {
    let config = try makeConfig()
    let source = config.path("a.txt"), folder = config.path("보관")
    try writeText("a", source)
    try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
    let model = ScriptedModel(turns: [
        ModelTurn(nil, [], ClarificationRequest("어느 폴더로 옮길까요?", [], "free_text", "q")),
        call(moveTool, ["source": .string(source), "destination": .string(folder)]),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: []), model)
    let question = try #require(try await planner.run("a.txt를 옮겨줘").clarification)
    #expect(try await planner.resume(question.context, "보관 폴더").approval != nil)
    #expect(fileExists(source))
}

@Test(arguments: ["a.txt 읽어줘", "a.txt 이동하지 말고 읽어줘", "https://example.com 페이지 읽어줘"])
func transferCannotRunOnReadNegatedOrWebRequests(text: String) async throws {
    let config = try makeConfig()
    let source = config.path("a.txt")
    try writeText("보존", source)
    let model = ScriptedModel(turns: [
        call(moveTool, ["source": .string(source), "destination": .string(config.path("b.txt"))]),
        turn("실행하지 못했습니다"), turn("실패"), turn("실패"),
    ])
    let outcome = try await makePlanner(config, FakeGateway(tools: []), model).run(text)
    #expect(outcome.approval == nil)
    #expect(readText(source) == "보존" && !fileExists(config.path("b.txt")))
    #expect(!model.calls[0].toolNames.contains(moveTool))
}

@Test func moveNeverReportsCompletionWithoutARealOperation() async throws {
    let config = try makeConfig()
    let model = ScriptedModel(turns: [turn("이동 도구가 없습니다."), turn("이동했습니다.")])
    let outcome = try await makePlanner(config, FakeGateway(tools: []), model, journal: false).run("a.txt를 보관 폴더로 이동해줘")
    #expect(outcome.errorMessage?.contains("실행하지 못했습니다") == true)
    #expect(model.count == 2)
}

@Test func aFalseClaimThatDeletionIsUnavailableIsCorrected() async throws {
    let config = try makeConfig()
    let target = config.path("a.txt")
    try writeText("보존", target)
    let model = ScriptedModel(turns: [turn("파일 삭제 도구가 없습니다."), call(deleteTool, ["paths": [.string(target)]])])
    let pending = try await makePlanner(config, FakeGateway(tools: []), model).run("a.txt를 삭제해줘")
    #expect(pending.approval != nil && fileExists(target))
    #expect(model.calls[1].lastContent.contains("현재 지원·제공되는 도구"))
}

@Test func renameReusesMoveAndReportsOnlyTheNewPath() async throws {
    let config = try makeConfig()
    let source = config.path("a.txt"), destination = config.path("b.txt")
    try writeText("내용", source)
    let model = ScriptedModel(turns: [call(moveTool, ["source": .string(source), "destination": .string(destination)]), turn("이름 변경 완료")])
    let planner = try makePlanner(config, FakeGateway(tools: []), model)
    let pending = try #require(try await planner.run("a.txt 이름을 b.txt로 바꿔줘").approval)
    #expect(try await planner.approve(pending.context).completed?.changedPaths == [destination])
    #expect(!fileExists(source) && readText(destination) == "내용")
}

private func mkdirThenMove(_ config: Config) -> ModelTurn {
    turn(
        nil, callJSON("mkdir", mkdirTool, ["path": .string(config.path("보관"))]),
        callJSON("move", moveTool, ["source": .string(config.path("a.txt")), "destination": .string(config.path("보관"))]))
}

@Test func mkdirThenMoveHasSeparateApprovalsAndUndos() async throws {
    let config = try makeConfig()
    let source = config.path("a.txt"), folder = config.path("보관")
    try writeText("원본", source)
    let planner = try makePlanner(config, FakeGateway(tools: []), ScriptedModel(turns: [mkdirThenMove(config), turn("이동 완료")]))
    let first = try #require(try await planner.run("a.txt를 새 보관 폴더로 이동해줘").approval)
    #expect(first.operation == "mkdir" && fileExists(source) && !fileExists(folder))
    let second = try #require(try await planner.approve(first.context).approval)
    #expect(second.operation == "move" && fileExists(source) && isDirectory(folder))
    #expect(try await planner.approve(second.context).completed != nil)
    #expect(!fileExists(source) && readText(folder + "/a.txt") == "원본")
    #expect(try await planner.undoLastWrite().completed != nil && fileExists(source))
    #expect(try await planner.undoLastWrite().completed != nil && !fileExists(folder))
}

@Test func rejectingMoveKeepsAndReportsAnAlreadyApprovedDirectory() async throws {
    let config = try makeConfig()
    let source = config.path("a.txt"), folder = config.path("보관")
    try writeText("원본", source)
    let planner = try makePlanner(config, FakeGateway(tools: []), ScriptedModel(turns: [], repeating: mkdirThenMove(config)))
    let first = try #require(try await planner.run("a.txt를 새 보관 폴더로 이동해줘").approval)
    #expect(first.operation == "mkdir")
    let second = try #require(try await planner.approve(first.context).approval)
    #expect(second.operation == "move")
    let outcome = planner.reject(second.context)
    #expect(outcome.text.contains("앞서 승인한 변경은 유지") && outcome.changedPaths == [folder])
    #expect(fileExists(source) && isDirectory(folder))
    #expect(try await planner.undoLastWrite().completed != nil && !fileExists(folder))
}

@Test(arguments: ["복사본.txt 읽어줘", "이동계획.md 요약해줘", "move.txt 읽어줘", "a.txt 이름 변경하지 말고 읽어줘"])
func operationWordsInFilenamesAndNegationsDoNotEnableTools(text: String) async throws {
    let config = try makeConfig()
    let model = ScriptedModel(turns: [], repeating: turn("답"))
    #expect(try await makePlanner(config, FakeGateway(tools: []), model, journal: false).run(text).completed != nil)
    let offered = Set(try #require(model.calls.last).toolNames)
    #expect(offered.isDisjoint(with: [moveTool, copyTool, mkdirTool]))
}

@Test func copyChecksDestinationAndKeepsSource() async throws {
    let config = try makeConfig()
    let source = config.path("a.txt"), folder = config.path("보관")
    try writeText("원본", source)
    try FileManager.default.createDirectory(atPath: folder, withIntermediateDirectories: true)
    let model = ScriptedModel(turns: [
        call(copyTool, ["source": .string(source), "destination": .string(config.path("wrong.txt"))]),
        call(copyTool, ["source": .string(source), "destination": .string(folder)]), turn("복사 완료"),
    ])
    let planner = try makePlanner(config, FakeGateway(tools: []), model)
    let pending = try #require(try await planner.run("a.txt를 보관 폴더로 복사해줘").approval)
    #expect(pending.operation == "copy")
    #expect(model.calls[1].lastContent.contains("명시되지"))
    _ = try await planner.approve(pending.context)
    #expect(readText(source) == "원본" && readText(folder + "/a.txt") == "원본")
    _ = try await planner.undoLastWrite()
    #expect(fileExists(source) && !fileExists(folder + "/a.txt"))
}

// MARK: - test_conversion.py

@Test func plannerConvertsAfterApproval() async throws {
    let config = try makeConfig()
    try writeText("a\n1\n", config.path("표.csv"))
    let gateway = FakeGateway(tools: [])
    let model = ScriptedModel(turns: [
        call(convertTool, ["source": .string(config.path("표.csv")), "target_format": "json"], id: "c"), turn("변환했습니다."),
    ])
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("표.csv를 json으로 변환해줘").approval)
    #expect(Set(model.calls[0].toolNames).isSuperset(of: [convertTool, readDocumentTool]))
    #expect(pending.operation == "convert" && !fileExists(config.path("표.json")))
    #expect(try await planner.approve(pending.context) == done("변환했습니다."))
    #expect(fileExists(config.path("표.json")))
    #expect(gateway.calls.isEmpty)  // a local tool, no MCP call
}

@Test func plannerRejectReportsConversion() async throws {
    let config = try makeConfig()
    try writeText("a\n1\n", config.path("표.csv"))
    let model = ScriptedModel(turns: [call(convertTool, ["source": .string(config.path("표.csv")), "target_format": "json"], id: "c")])
    let planner = try makePlanner(config, FakeGateway(tools: []), model, journal: false)
    let pending = try #require(try await planner.run("표.csv를 json으로 변환해줘").approval)
    #expect(planner.reject(pending.context) == Completed("파일 변환을 거절했습니다. 파일은 변경하지 않았습니다."))
    #expect(!fileExists(config.path("표.json")))
}

// MARK: - test_undo.py

@Test func plannerWithoutJournalNeverExecutesApprovedWrite() async throws {
    let config = try makeConfig()
    let target = config.path("missing-journal.txt")
    let gateway = FakeGateway(tools: [], fallback: ok("written"))
    let model = ScriptedModel(turns: [writeTurn(target, "blocked"), turn("journal 오류를 확인했습니다.")])
    let planner = try makePlanner(
        config, gateway, model, tools: [tool("filesystem__write_file", "Write")], journal: false)
    let pending = try #require(try await planner.run("missing-journal.txt에 저장해줘").approval)
    #expect(try await planner.approve(pending.context) == done("journal 오류를 확인했습니다."))
    #expect(gateway.calls.isEmpty && !fileExists(target))
    #expect(model.calls[1].lastContent.contains("Undo journal"))
}

@Test func undoWithoutJournalIsAnError() async throws {
    let planner = try makePlanner(try makeConfig(), FakeGateway(), ScriptedModel(turns: []), journal: false)
    #expect(try await planner.undoLastWrite() == .error("Undo journal이 없어 파일을 되돌릴 수 없습니다."))
}

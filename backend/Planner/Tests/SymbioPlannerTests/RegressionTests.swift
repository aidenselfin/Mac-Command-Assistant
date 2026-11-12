import Foundation
import SymbioCore
import Testing
@testable import SymbioPlanner

/// tests/test_text_command_regressions.py.

private let webCompareTools = [
    tool("web__browser_navigate", "Navigate"), tool("web__browser_snapshot", "Snapshot"),
    tool("filesystem__read_text_file", "Read"), tool("filesystem__write_file", "Write"),
]
private let activeApp = ToolCall("active", "macos__get_active_app", "{}")
private func listCall(_ config: Config) -> ToolCall {
    callJSON("list", "filesystem__list_directory", ["path": .string(config.root.path)])
}
private func hasActive(_ call: ModelCall, _ name: String) -> Bool {
    call.messages.contains { $0["tool_call_id"] == "active" && $0["content"] == .string(name) }
}

@Test func meetingNoteSummaryRejectsUnrelatedSafeTxtSearchResult() async throws {
    let config = try makeConfig()
    for name in ["회의록-1.md", "회의록-2.md", "result.txt"] { try writeText("fixture", config.path(name)) }
    let gateway = FakeGateway()
    let model = ScriptedModel(turns: [findCall("result", id: "find"), findCall("회의록", id: "find")])
    let outcome = try #require(try await makePlanner(config, gateway, model).run("회의록 요약해줘").clarification)
    #expect(outcome.options.sorted() == [config.path("회의록-1.md"), config.path("회의록-2.md")])
    #expect(model.calls[1].lastContent.contains("tool_error"))
    #expect(gateway.calls.isEmpty)  // nothing was read or written
}

@Test func webSaveAcceptsURLBeforeKoreanObjectParticle() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(results: [
        ok("### Page state\n- Page URL: https://example.com\n- Page Title: Fixture"),
        ok("### Page state\n- Page URL: https://example.com\n- Page Snapshot:\nfixture"),
    ])
    let model = ScriptedModel(turns: [
        turn(nil, callJSON("navigate", "web__browser_navigate", ["url": "https://example.com"])),
        turn(nil, ToolCall("snapshot", "web__browser_snapshot", "{}")),
        writeTurn(config.path("summary.md"), "summary"),
    ])
    let pending = try await makePlanner(config, gateway, model).run("https://example.com을 읽고 summary.md에 저장해줘")
    #expect(gateway.calls[0].arguments == ["url": "https://example.com"])
    #expect(pending.approval?.toolName == "filesystem__write_file")
}

@Test func webFileComparisonDoesNotEnterWebSaveFlow() async throws {
    let config = try makeConfig(maxToolRounds: 1)
    let gateway = FakeGateway(tools: webCompareTools)
    let model = ScriptedModel(turns: [], repeating: turn("비교 결과"))
    let outcome = try await makePlanner(config, gateway, model).run("https://example.com 페이지와 test1.md를 비교해줘")
    #expect(outcome.errorMessage?.contains("한도 초과") == true)
    #expect(!model.calls[0].toolNames.contains("filesystem__write_file"))
    #expect(gateway.calls.isEmpty)
}

@Test func webFileComparisonRejectsHiddenWriteAfterReads() async throws {
    let config = try makeConfig()
    let local = config.path("test1.md"), target = config.path("summary.md")
    try writeText("LOCAL", local)
    let url = "https://example.com"
    let gateway = FakeGateway(tools: webCompareTools, results: [
        ok("### Page state\n- Page URL: \(url)\n- Page Title: Fixture"),
        ok("### Page state\n- Page URL: \(url)\n- Page Snapshot:\nWEB"), ok("LOCAL"),
    ])
    let model = ScriptedModel(turns: [
        turn(nil, callJSON("nav", "web__browser_navigate", ["url": .string(url)])),
        turn(nil, ToolCall("snap", "web__browser_snapshot", "{}")),
        readCall(local, id: "read"),
        writeTurn(target, "SHOULD NOT WRITE", id: "hidden-write"),
        turn("비교 완료"),
    ])
    #expect(try await makePlanner(config, gateway, model).run("https://example.com 페이지와 test1.md를 비교해줘") == done("비교 완료"))
    #expect(gateway.calls.map(\.arguments) == [["url": .string(url)], [:], ["path": .string(local)]])
    #expect(!fileExists(target))
    #expect(model.calls.last?.lastContent.contains("저장 요청") == true)
}

@Test(arguments: [
    "https://example.com 페이지와 test1.md를 비교해줘", "https://example.com 페이지에서 summary.md 내용을 읽어줘",
    "https://example.com 페이지를 summary.md에 저장하지 말고 읽어줘",
])
func nonSaveWebRequestRejectsHiddenWriteBeforeNavigation(text: String) async throws {
    let config = try makeConfig(maxToolRounds: 2)
    let target = config.path("summary.md")
    let gateway = FakeGateway(tools: [tool("filesystem__write_file"), tool("web__browser_navigate"), tool("web__browser_snapshot")])
    let model = ScriptedModel(turns: [writeTurn(target, "SHOULD NOT WRITE", id: "hidden-write"), turn("거부했습니다.")])
    let outcome = try await makePlanner(config, gateway, model).run(text)
    #expect(outcome.errorMessage != nil)
    #expect(!model.calls[0].toolNames.contains("filesystem__write_file"))
    #expect(gateway.calls.isEmpty && !fileExists(target))
}

@Test func webSaveNegationDoesNotExposeWriteOrRequestApproval() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(tools: [tool("web__browser_navigate"), tool("filesystem__write_file")])
    let model = ScriptedModel(turns: [], repeating: turn("요약만 했습니다."))
    let outcome = try await makePlanner(config, gateway, model).run("https://example.com 페이지를 저장하지 말고 요약해줘")
    #expect(outcome.errorMessage != nil)
    #expect(!(model.calls.last?.toolNames.contains("filesystem__write_file") ?? true))
    #expect(gateway.calls.isEmpty)
}

@Test func compositeActiveAppRequestRunsFollowingFileListing() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(results: [ok("Finder"), ok("[FILE]")])
    let model = ScriptedModel(turns: [turn(nil, activeApp, listCall(config)), turn("Finder에서 파일 목록을 확인했습니다.")])
    #expect(try await makePlanner(config, gateway, model).run("현재 활성 앱과 파일 목록을 알려줘") == done("Finder에서 파일 목록을 확인했습니다."))
    #expect(gateway.names == ["macos__get_active_app", "filesystem__list_directory"])
    #expect(hasActive(model.calls[1], "Finder"))
}

@Test func standaloneActiveAppStillCompletesAfterOneCall() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(fallback: ok("Finder"))
    let model = ScriptedModel(turns: [], repeating: turn(nil, activeApp))
    #expect(try await makePlanner(config, gateway, model).run("현재 활성 앱을 알려줘") == done("현재 활성 앱은 Finder입니다."))
    #expect(gateway.calls.count == 1 && gateway.calls[0].arguments == [:])
}

@Test(arguments: [true, false])
func activeAppCacheSkipsSameAndLaterRepeatedCalls(sameTurn: Bool) async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(results: [ok("Finder"), ok("[FILE]")])
    let again = ToolCall("again", "macos__get_active_app", "{}")
    let model = ScriptedModel(turns: sameTurn
        ? [turn(nil, activeApp, again, listCall(config)), turn("Finder에서 확인했습니다.")]
        : [turn(nil, activeApp), turn(nil, again, listCall(config)), turn("Finder에서 확인했습니다.")])
    #expect(try await makePlanner(config, gateway, model).run("활성 앱과 파일 목록을 알려줘") == done("Finder에서 확인했습니다."))
    #expect(gateway.names == ["macos__get_active_app", "filesystem__list_directory"])
    #expect(model.count <= config.maxToolRounds)
}

@Test func failedActiveAppIsNotCachedBeforeOtherWork() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(results: [ToolResult(ok: false, text: "denied", error: "denied"), ok("[FILE]"), ok("Finder")])
    let model = ScriptedModel(turns: [
        turn(nil, activeApp, listCall(config)), turn(nil, ToolCall("retry", "macos__get_active_app", "{}")),
        turn("Finder에서 목록을 확인했습니다."),
    ])
    #expect(try await makePlanner(config, gateway, model).run("활성 앱과 파일 목록을 알려줘") == done("Finder에서 목록을 확인했습니다."))
    #expect(gateway.calls.count == 3)
}

@Test func approvalKeepsActiveAppCacheAndNewCommandRefreshes() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(results: [ok("Finder"), ok("saved"), ok("Safari")])
    let model = ScriptedModel(turns: [
        turn(nil, activeApp, callJSON("write", "filesystem__write_file", ["path": .string(config.path("result.txt")), "content": "ok"])),
        turn(nil, ToolCall("again", "macos__get_active_app", "{}")),
        turn("Finder에서 저장했습니다."),
        turn(nil, ToolCall("new", "macos__get_active_app", "{}")),
    ])
    let planner = try makePlanner(config, gateway, model)
    let pending = try #require(try await planner.run("활성 앱을 알려주고 result.txt에 저장해줘").approval)
    #expect(try await planner.approve(pending.context) == done("Finder에서 저장했습니다."))
    #expect(hasActive(model.calls[1], "Finder"))
    #expect(try await planner.run("현재 활성 앱을 알려줘") == done("현재 활성 앱은 Safari입니다."))
    #expect(gateway.names == ["macos__get_active_app", "filesystem__write_file", "macos__get_active_app"])
}

@Test(arguments: [false, true])
func webSaveCorrectionsCountModelCallsSeparatelyFromIterations(clarify: Bool) async throws {
    let config = try makeConfig(maxToolRounds: 1)
    let timing = PlannerTiming.empty()
    let events = Recorder<PlannerEvent>()
    let reply = clarify
        ? ModelTurn(nil, [], ClarificationRequest("형식?", ["요약"], "edit_criteria")) : turn("아직 완료")
    let model = ScriptedModel(turns: [], repeating: reply)
    let outcome = try await makePlanner(config, FakeGateway(), model, timing: timing, events: events)
        .run("https://example.com 내용을 summary.md에 저장해줘")
    #expect(outcome.errorMessage?.contains("모델 호출 한도") == true)
    #expect(model.count == 1 && timing.llmRoundMs.count == 1)
    #expect(events.all.map(\.iteration) == [1])
    #expect(events.all.allSatisfy { $0.type == .planningStarted })
}

@Test func approvalContinuationExposesModelCallsAndIterations() async throws {
    let config = try makeConfig(maxToolRounds: 3)
    let gateway = FakeGateway(results: [ok("fixture"), ok("created")])
    let timing = PlannerTiming.empty()
    let events = Recorder<PlannerEvent>()
    let model = ScriptedModel(turns: [
        readCall(config.path("a.txt"), id: "read"), writeTurn(config.path("summary.txt"), "summary"), turn("저장했습니다."),
    ])
    let planner = try makePlanner(config, gateway, model, timing: timing, events: events)
    let pending = try #require(try await planner.run("a.txt를 읽고 summary.txt에 저장해줘").approval)
    #expect(try await planner.approve(pending.context) == done("저장했습니다."))
    #expect(model.count == 3 && timing.llmRoundMs.count == 3)
    #expect(events.all.filter { $0.type == .planningStarted }.map(\.iteration) == [1, 2, 3])
}

@Test func lastAllowedCallCanWriteWithoutAnotherModelCall() async throws {
    let config = try makeConfig(maxToolRounds: 1)
    let model = ScriptedModel(turns: [], repeating: writeTurn(config.path("last-call.txt"), "ok"))
    let planner = try makePlanner(config, FakeGateway(fallback: ok("saved")), model)
    let pending = try #require(try await planner.run("last-call.txt에 저장해줘").approval)
    let message = try #require(try await planner.approve(pending.context).errorMessage)
    #expect(message.contains("승인된 파일 변경은 완료") && !message.contains("파일을 변경하지 않았"))
    #expect(model.count == 1)
}

@Test func reapprovalKeepsTheOriginalModelCallBudget() async throws {
    let config = try makeConfig(maxToolRounds: 1)
    let target = config.path("renewed.txt")
    let model = ScriptedModel(turns: [], repeating: writeTurn(target, "ok"))
    let planner = try makePlanner(config, FakeGateway(fallback: ok("saved")), model)
    let pending = try #require(try await planner.run("renewed.txt에 저장해줘").approval)
    try writeText("changed", target)
    let renewed = try #require(try await planner.approve(pending.context).approval)
    let message = try #require(try await planner.approve(renewed.context).errorMessage)
    #expect(message.contains("승인된 파일 변경은 완료"))
    #expect(model.count == 1)
}

@Test func urlTextIsPlainTextWhenNoWebToolIsConnected() async throws {
    let config = try makeConfig()
    let gateway = FakeGateway(tools: [tool("filesystem__write_file", "Write")])
    let model = ScriptedModel(turns: [writeTurn(config.path("links.txt"), "https://example.com")])
    let outcome = try await makePlanner(config, gateway, model).run("https://example.com 문자열을 links.txt에 저장해줘")
    #expect(outcome.approval != nil)
}

import Foundation
import SymbioCore
import SymbioMCP
import SymbioPlanner
import SymbioRuntime
import SymbioUndo
import Testing
@testable import SymbioApp

/// tests/test_native_bridge.py and tests/test_integration_features.py: the native UI cannot bypass
/// runtime state or replay a stale approval.

@Test func approvalIsPrivateOneShotAndOldIDsCannotAuthorizeANewWrite() async throws {
    let (bridge, worker, events) = makeBridge()
    let pending = try await approval()
    bridge.message(.state(.waitingApproval))
    bridge.message(.outcome(.outcome(.approvalRequired(pending))))
    let first = try #require(events.last["approval_id"]?.stringValue)
    #expect(!JSON.array(events.all).text.contains(pending.context.nonce))
    _ = bridge.command(["action": "approve", "approval_id": "wrong"])
    #expect(worker.count("approve") == 0)
    _ = bridge.command(["action": "approve", "approval_id": .string(first)])
    _ = bridge.command(["action": "approve", "approval_id": .string(first)])
    #expect(worker.count("approve") == 1)
    bridge.message(.outcome(.outcome(.approvalRequired(pending))))
    #expect(events.last["approval_id"]?.stringValue != first)
    _ = bridge.command(["action": "approve", "approval_id": .string(first)])
    #expect(worker.count("approve") == 1)
}

@Test func busyUnreadyAndApprovalStatesRefuseNewCommands() {
    let (bridge, worker, events) = makeBridge()
    bridge.state = nil
    _ = bridge.command(["action": "submit", "text": "x"])
    bridge.state = .waitingApproval
    _ = bridge.command(["action": "submit", "text": "x"])
    #expect(worker.count("submit") == 0)
    bridge.state = .idle
    _ = bridge.command(["action": "submit", "text": "x"])
    _ = bridge.command(["action": "undo"])
    #expect(worker.calls == ["submit:x"])
    #expect(events.last["type"] == "rejected")
}

@Test func clarificationAndCancelUseTheOriginalRuntimeContract() {
    let (bridge, worker, _) = makeBridge()
    bridge.state = .waitingUserInput
    _ = bridge.command(["action": "submit", "text": "2번"])
    #expect(worker.calls == ["submit:2번"])
    bridge.inFlight = false
    _ = bridge.command(["action": "cancel"])
    #expect(worker.calls.last == "submit:취소")
    bridge.state = .waitingApproval
    bridge.inFlight = false
    bridge.approvalID = "current"
    _ = bridge.command(["action": "cancel"])
    #expect(worker.count("reject") == 1)
}

@Test(arguments: [nil, [], ["action": "submit", "text": []], ["action": "rate", "score": 1]] as [JSON?])
func malformedCommandsDoNotReachTheWorker(_ payload: JSON?) {
    let (bridge, worker, events) = makeBridge()
    _ = bridge.command(payload)
    #expect(events.last["type"] == "rejected")
    #expect(worker.calls.isEmpty)
}

@Test func feedbackCommandWaitsUntilTheQuestionIsAnswered() {
    let (bridge, worker, events) = makeBridge()
    bridge.message(.state(.waitingUserInput))
    _ = bridge.command(["action": "submit", "text": "/feedback 너무 느려"])
    #expect(events.last["type"] == "rejected" && events.last["text"]?.stringValue?.contains("/feedback") == true)
    #expect(worker.count("submit") == 0)
}

@Test func jsonStreamRejectsMalformedInputAndClosesTheWorkerOnShutdown() throws {
    let config = try makeConfig()
    let worker = FakeWorker(), events = Events()
    var fds: [Int32] = [0, 0]
    #expect(pipe(&fds) == 0)
    let input = Array("not json\n{\"action\":\"shutdown\"}\n".utf8)
    _ = input.withUnsafeBytes { write(fds[1], $0.baseAddress, $0.count) }
    close(fds[1])
    let code = runBridge(
        input: fds[0], emit: events.append, loadConfig: { config }, makeWorker: { _, _ in worker },
        staleNotice: { _, _ in nil }, handleSignals: false)
    close(fds[0])
    #expect(code == 0)
    #expect(worker.count("shutdown") == 1)
    #expect(events.types == ["config", "rejected"])
    #expect(!JSON.array(events.all).text.contains(config.apiKey))
}

@Test func configFailureIsReportedWithoutItsText() {
    let events = Events()
    let code = runBridge(emit: events.append, loadConfig: { throw ConfigError("SECRET-PATH") }, handleSignals: false)
    #expect(code == 1)
    #expect(events.all == [["type": "error", "text": "설정 로딩 실패 (ConfigError). 설정과 키를 확인하세요."]])
}

@Test func eventsKeepThePythonKeyOrderAndText() {
    let (bridge, _, events) = makeBridge()
    bridge.message(.attachment(path: nil, refusal: "허용 폴더 밖이에요."))
    bridge.message(.plannerEvent(PlannerEvent(.planningStarted, 1)))
    #expect(events.all.map(\.text) == [
        #"{"type": "state", "state": "IDLE"}"#,
        #"{"type": "attachment", "path": null, "text": "허용 폴더 밖이에요."}"#,
        #"{"type": "tool", "name": ""}"#,
    ])
}

@Test func completedEventCarriesFoundPathsForTheDragChip() {
    let (bridge, _, events) = makeBridge()
    bridge.message(completed("찾았어요", found: ["/a/b.txt"]))
    #expect(events.last["found_paths"] == ["/a/b.txt"])
}

@Test func floatingFileBoxOnlyWhenTheFileItselfIsAskedFor() {
    for text in ["보고서 파일 줘", "회의록 줘", "계약서파일줘", "그거 좀 줘", "사진 꺼내줘",
                 "견적서 가져와", "발표 자료 주세요", "회의록만 줘",
                 "보고서 줘", "견적서 주세요", "문서 줘",
                 "회의록_요약본.pdf 파일 줘", "위치도.png 파일 줘", "견적서 가져다줘",
                 "보고서 파일 가져와 줘", "견적서 꺼내 주세요", "회의록을 줘",
                 "가장 최근 파일 하나 줘", "최근 파일 두 개만 줘", "문서 한 개 주세요"] {
        #expect(asksForFile(text), "\(text)")
    }
    for text in ["회의록 요약해줘", "보고서 찾아 줘", "파일 위치 알려줘", "사진 지워줘",
                 "pdf로 바꿔줘", "회의록 읽어줘", "보고서 파일 열어 줘",
                 "보고서 가져와서 요약해줘", "회의록 찾아만 줘",
                 "보고서 찾아 좀 줘", "보고서 파일 경로 줘",
                 "보고서 가져와서요약해줘", "보고서 꺼내서읽어줘", "보고서 파일 경로만 줘",
                 "보고서 파일 위치도 줘",
                 "보고서 파일 경로를 줘", "보고서 파일 위치를 주세요", "보고서 파일 용량 줘",
                 "보고서 파일 경로 가져와 줘", "파일 목록 가져와",
                 "보고서 파일 경로 좀 가져와", "보고서 파일 경로가져와줘", "", "   "] {
        #expect(!asksForFile(text), "\(text)")
    }
}

@Test func offerFileFollowsTheOriginalRequestThroughAClarification() {
    let (bridge, _, events) = makeBridge()
    _ = bridge.command(["action": "submit", "text": "보고서 파일 줘"])
    bridge.inFlight = false
    bridge.state = .waitingUserInput
    _ = bridge.command(["action": "submit", "text": "2번"])
    bridge.message(completed("찾았어요", found: ["/a/b.txt"]))
    #expect(events.last["offer_file"] == true)
}

@Test func completedEventOffersMadeFilesAsChipsUpToFive() throws {
    let folder = try temporaryFolder()
    let files = (0..<6).map { folder.appendingPathComponent("\($0).md").path }
    for path in files { FileManager.default.createFile(atPath: path, contents: Data()) }

    func offered(_ message: UIMessage, _ request: String = "") -> JSON? {
        let (bridge, _, events) = makeBridge()
        bridge.request = request
        bridge.message(message)
        return events.last["offer_paths"]
    }
    let json = { (paths: ArraySlice<String>) in JSON.array(paths.map { .string($0) }) }
    #expect(offered(completed("요약을 저장했어요", changed: [files[0]])) == json(files[0..<1]))
    #expect(offered(completed("저장했어요", changed: Array(files[0..<5]) + [folder.path])) == json(files[0..<5]))
    #expect(offered(completed("저장했어요", changed: files)) == [])
    #expect(offered(completed("찾았어요", found: [files[0]]), "회의록 요약해줘") == [])
    #expect(offered(completed("찾았어요", found: [files[0]]), "회의록 줘") == json(files[0..<1]))
}

@Test func staleAppNoticeComparesTheBuiltTreeWithTheCheckout() throws {
    let folder = try temporaryFolder()
    try FileManager.default.createDirectory(
        at: folder.appendingPathComponent("native-preview"), withIntermediateDirectories: true)
    try Data("v1".utf8).write(to: folder.appendingPathComponent("native-preview/App.swift"))
    let git = ["-C", folder.path, "-c", "user.name=t", "-c", "user.email=t@t"]
    try run("/usr/bin/git", git + ["init", "-q"])
    try run("/usr/bin/git", git + ["add", "."])
    try run("/usr/bin/git", git + ["commit", "-qm", "v1"])
    let tree = try run("/usr/bin/git", git + ["rev-parse", "HEAD:native-preview"]).1
        .trimmingCharacters(in: .whitespacesAndNewlines)

    #expect(staleAppNotice(tree, project: folder) == nil)
    #expect(staleAppNotice(nil, project: folder) == staleApp)  // built before the version was recorded
    #expect(staleAppNotice(tree + "-dirty", project: folder) == staleApp)
    #expect(staleAppNotice(tree, project: folder.appendingPathComponent("not-a-repo")) == nil)  // no git: quiet
}

@Test func quickListShowsRecentAndLargeFilesWithoutTheModel() throws {
    let folder = try temporaryFolder()
    let now = Date()
    for (name, size, age) in [("old.bin", 900, 300.0), ("new.txt", 10, 0.0)] {
        let path = folder.appendingPathComponent(name).path
        FileManager.default.createFile(atPath: path, contents: Data(repeating: 120, count: size))
        try FileManager.default.setAttributes([.modificationDate: now.addingTimeInterval(-age)], ofItemAtPath: path)
    }
    let (bridge, worker, events) = makeBridge(roots: [folder])
    _ = bridge.command(["action": "quick_list", "sort": "modified"])
    _ = bridge.command(["action": "quick_list", "sort": "size"])
    let recent = events.all[events.all.count - 2], large = events.last
    #expect(recent["type"] == "completed" && large["type"] == "completed")
    let names = { (event: JSON) in event["found_paths"]?.arrayValue?.compactMap { $0.stringValue }
        .map { ($0 as NSString).lastPathComponent } }
    #expect(names(recent) == ["new.txt", "old.bin"])
    #expect(names(large) == ["old.bin", "new.txt"])
    #expect(large["text"]?.stringValue?.contains("용량 큰 파일 2개") == true && worker.calls.isEmpty)
    #expect(large["offer_paths"] == large["found_paths"] && large["show_box"] == true)

    _ = bridge.command(["action": "quick_list", "sort": "name"])
    #expect(events.last["type"] == "rejected")
    bridge.message(.state(.planning))
    _ = bridge.command(["action": "quick_list", "sort": "size"])
    #expect(events.last["type"] == "rejected")
}

@Test func theNativeAppGetsSourceAndDestinationOfAMoveApproval() async throws {
    let (bridge, _, events) = makeBridge()
    bridge.message(.state(.waitingApproval))
    let move = approval(
        try await approval(), tool: "local__move_file", target: "/allowed/보관/IMG_001.jpg", operation: "move",
        targets: ["/allowed/IMG_001.jpg"])
    bridge.message(.outcome(.outcome(.approvalRequired(move))))
    let event = events.last
    #expect(event["operation"] == "move" && event["target"] == "/allowed/보관/IMG_001.jpg")
    #expect(event["targets"] == ["/allowed/IMG_001.jpg"])
    #expect(!JSON.array(events.all).text.contains(move.context.nonce))
}

private let mcpInstalled = FileManager.default.fileExists(atPath: serverEntrypoint(project: repoRoot).path)

/// Python integration test: real filesystem MCP, scripted model, approval, write and undo.
@Test(.enabled(if: mcpInstalled, "node_modules가 없어 실제 MCP 시험을 건너뜁니다"))
func nativeTransportRealMCPApprovalWriteAndUndo() async throws {
    let config = try makeConfig()
    let target = config.root.appendingPathComponent("native.txt")
    let model = ScriptedModel([
        ModelTurn(nil, [], ClarificationRequest("저장할 내용은 무엇인가요?", [], "free_text", "question")),
        writeTurn(target.path, "native backend test"),
        ModelTurn("저장했습니다.", []),
    ])
    let folder = try temporaryFolder()
    let messages = BlockingQueue<UIMessage>()
    let worker = AsyncRuntimeWorker(
        config, send: { messages.put($0) },
        managerFactory: { try buildMCPManager($0, project: repoRoot) }, modelFactory: { _, _ in model },
        feedbackFactory: { _, _ in nil },
        journalFactory: { config in
            try? UndoJournal(
                path: folder.appendingPathComponent("native-undo.sqlite3"), allowedRoots: config.allowedDirectories)
        })
    let events = Events()
    let bridge = NativeBridge(worker, emit: events.append)

    func until(_ kind: String) async throws -> JSON {
        for _ in 0..<100 {
            guard let message = await waitFor(messages, { _ in true }, timeout: 30).0 else { break }
            bridge.message(message)
            if events.last["type"]?.stringValue == kind { return events.last }
        }
        throw Boom(message: "missing event: \(kind)")
    }

    worker.start()
    _ = try await until("state")
    let outside = folder.appendingPathComponent("outside.txt")
    try Data("not allowed".utf8).write(to: outside)
    _ = bridge.command(["action": "attach", "path": .string(outside.path)])
    let refused = try await until("attachment")
    #expect(refused["path"] == .null && refused["text"]?.stringValue?.isEmpty == false)
    _ = bridge.command(["action": "submit", "text": "native.txt에 저장해줘"])
    let question = try await until("clarification")
    #expect(question["kind"] == "free_text" && question["choices"] == [])
    _ = bridge.command(["action": "submit", "text": "native backend test"])
    let pending = try await until("approval")
    #expect(!FileManager.default.fileExists(atPath: target.path))
    _ = bridge.command(["action": "approve", "approval_id": "stale"])
    #expect(!FileManager.default.fileExists(atPath: target.path))
    _ = bridge.command(["action": "approve", "approval_id": pending["approval_id"] ?? .null])
    _ = try await until("completed")
    #expect(try String(contentsOf: target, encoding: .utf8) == "native backend test")
    _ = bridge.command(["action": "undo"])
    _ = try await until("completed")
    #expect(!FileManager.default.fileExists(atPath: target.path))
    await worker.shutdown()
}

import Foundation
import SymbioCore
import SymbioPlanner
import SymbioRuntime
import Testing
@testable import SymbioApp

/// tests/test_ui.py, the parts the bridge uses: the runtime worker, rendering and display_path.
/// The Tk presenter tests are not ported (UIPresenter is not ported).

final class BlockingModel: ModelClient, @unchecked Sendable {  // flags behind `lock`
    private let lock = NSLock()
    private var flags = (entered: false, cancelled: false)
    var entered: Bool { lock.withLock { flags.entered } }
    var cancelled: Bool { lock.withLock { flags.cancelled } }

    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        lock.withLock { flags.entered = true }
        do {
            try await Task.sleep(for: .seconds(3600))
        } catch {
            lock.withLock { flags.cancelled = true }
            throw error
        }
        return ModelTurn("never", [])
    }
}

private func worker(_ config: Config, _ manager: FakeManager, _ model: any ModelClient)
    -> (AsyncRuntimeWorker, BlockingQueue<UIMessage>)
{
    let messages = BlockingQueue<UIMessage>()
    let worker = AsyncRuntimeWorker(
        config, send: { messages.put($0) }, managerFactory: { _ in manager }, modelFactory: { _, _ in model },
        feedbackFactory: { _, _ in nil }, journalFactory: { _ in nil })
    return (worker, messages)
}

private func until(_ condition: () -> Bool) async {
    for _ in 0..<500 where !condition() { try? await Task.sleep(for: .milliseconds(10)) }
}

@Test func workerActiveCancelClosesTheOwnedManager() async throws {
    let manager = FakeManager(), model = BlockingModel()
    let (worker, messages) = worker(try makeConfig(), manager, model)
    worker.start()
    let (ready, _) = await waitFor(messages, isState)
    guard case .state(.idle)? = ready else { Issue.record("not ready: \(String(describing: ready))"); return }

    #expect(worker.submit("파일 목록"))
    await until { model.entered }
    #expect(worker.cancelActive())
    let (stopped, seen) = await waitFor(messages, isStopped)
    await worker.shutdown()

    guard case .stopped(true)? = stopped else { Issue.record("not stopped: \(String(describing: stopped))"); return }
    #expect(model.cancelled)
    #expect(manager.closed)
    #expect(seen.contains {
        if case .notice("요청을 중단하고 실행 중인 도구를 정리했습니다.") = $0 { return true }
        return false
    })
}

@Test func workerNormalShutdownClosesOwnedResources() async throws {
    let manager = FakeManager()
    let (worker, messages) = worker(try makeConfig(), manager, ScriptedModel([]))
    worker.start()
    _ = await waitFor(messages, isState)
    await worker.shutdown()
    #expect(manager.closed)
    guard case .stopped(false)? = messages.all().last else { Issue.record("no stop"); return }
}

@Test func workerShutdownBeforeReadyStillStops() async throws {
    let manager = FakeManager()
    let (worker, messages) = worker(try makeConfig(), manager, ScriptedModel([]))
    worker.start()
    await worker.shutdown()
    #expect(messages.all().contains(where: isStopped))
}

@Test func workerExceptionIsRedactedAndClosesOwnedResources() async throws {
    let manager = FakeManager()
    let (worker, messages) = worker(try makeConfig(), manager, ScriptedModel(failing: [Boom(message: "raw-provider-secret")]))
    worker.start()
    _ = await waitFor(messages, isState)
    #expect(worker.submit("실패 요청"))
    let (stopped, seen) = await waitFor(messages, isStopped)
    await worker.shutdown()

    let failures = seen.compactMap { message -> String? in if case let .failure(text) = message { return text }; return nil }
    guard case .stopped(true)? = stopped else { Issue.record("not stopped"); return }
    #expect(failures.count == 1)
    #expect(failures.first?.contains("Boom") == true)
    #expect(failures.first?.contains("raw-provider-secret") == false)
    #expect(manager.closed)
}

@Test func workerPlannerErrorIsReportedAndKeepsTheWindowOpen() async throws {
    let manager = FakeManager()
    let failure = PlannerError("LLM API 요청 실패 (HTTP 404)")
    let (worker, messages) = worker(try makeConfig(), manager, ScriptedModel(failing: [failure, failure]))
    worker.start()
    _ = await waitFor(messages, isState)
    for _ in 0..<2 {  // still accepts commands after the first failure
        #expect(worker.submit("실패 요청"))
        let (outcome, seen) = await waitFor(messages, isOutcome)
        guard case .outcome(.outcome(.error("LLM API 요청 실패 (HTTP 404)")))? = outcome else {
            Issue.record("unexpected: \(String(describing: outcome))")
            return
        }
        #expect(!seen.contains { isFailure($0) || isStopped($0) })
    }
    #expect(!manager.closed)
    await worker.shutdown()
}

@Test func workerAttachChecksThePathAndReportsBack() async throws {
    let config = try makeConfig()
    let file = config.root.appendingPathComponent("내 파일.md")
    try Data("x".utf8).write(to: file)
    let (worker, messages) = worker(config, FakeManager(), ScriptedModel([]))
    worker.start()
    _ = await waitFor(messages, isState)

    #expect(worker.attach(file.path))
    let (accepted, _) = await waitFor(messages, isAttachment)
    guard case let .attachment(path, nil)? = accepted else { Issue.record("not accepted"); return }
    #expect(path == file.path)

    #expect(worker.attach(config.root.deletingLastPathComponent().appendingPathComponent("밖.txt").path))
    let (refused, _) = await waitFor(messages, isAttachment)
    await worker.shutdown()
    guard case let .attachment(nil, text?)? = refused else { Issue.record("not refused"); return }
    #expect(text.contains("요청을 거부"))
}

@Test func feedbackNoticesGoToTheBridgeAsNotices() async throws {
    let config = try makeConfig()
    let messages = BlockingQueue<UIMessage>()
    let worker = AsyncRuntimeWorker(
        config, send: { messages.put($0) }, managerFactory: { _ in FakeManager() },
        modelFactory: { _, _ in ScriptedModel([]) },
        feedbackFactory: { _, emit in emit("[FEEDBACK] 안내"); return nil }, journalFactory: { _ in nil })
    worker.start()
    let (_, seen) = await waitFor(messages, isState)
    await worker.shutdown()
    #expect(seen.contains { if case .notice("[FEEDBACK] 안내") = $0 { return true }; return false })
}

@Test func approvalDisplayIsBoundedAndHasNoOpaqueContext() async throws {
    let full = String(repeating: "x", count: 200) + "SECRET-TAIL"
    let base = try await approval()
    let pending = approval(
        base, tool: "filesystem__write_file", target: "/allowed/summary.txt", operation: "overwrite",
        chars: full.count, preview: String(full.prefix(200)), truncated: true)
    let rendered = renderOutcome(.outcome(.approvalRequired(pending)))
    for part in ["[APPROVAL REQUIRED]", "filesystem__write_file", "/allowed/summary.txt", "overwrite",
                 String(repeating: "x", count: 200)] {
        #expect(rendered.contains(part))
    }
    #expect(!rendered.contains("SECRET-TAIL") && !rendered.contains(base.context.nonce))
}

@Test func outcomeRenderingKeepsTheClarificationContract() {
    let context = ClarificationContext("요약", "file_candidate", ["a", "b"], 1)
    #expect(renderOutcome(.outcome(.clarification(Clarification("선택하세요", context.options, context))))
        == "[QUESTION] 선택하세요\n  1. a\n  2. b")
    #expect(renderOutcome(.outcome(.completed(Completed("완료")))) == "[DONE] 완료")
}

@Test func displayPathEscapesControlCharactersOnly() {
    #expect(displayPath("/w/a\nb\t\"c\".txt") == "/w/a\\nb\\t\"c\".txt")
    #expect(displayPath("/w/회의록 (1).md") == "/w/회의록 (1).md")
    #expect(displayPath("/w/\u{7}\u{200B}\u{2028}x") == "/w/\\x07\\u200b\\u2028x")
}

extension BlockingQueue {
    /// Everything queued right now.
    func all() -> [Element] {
        var items: [Element] = []
        while let item = get(timeout: 0) { items.append(item) }
        return items
    }
}

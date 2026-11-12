import Foundation
import SymbioCore
import SymbioPlanner
import Testing
@testable import SymbioRuntime

/// tests/test_runtime.py

func statePath(_ controller: RuntimeController) -> [RuntimeState] {
    [.idle] + controller.transitionHistory.map(\.current)
}

func clarification() -> Clarification {
    let options = ["/allowed/회의록-1.md", "/allowed/회의록-2.md"]
    return Clarification(
        "요약할 회의록을 선택해 주세요.", options, ClarificationContext("회의록 요약해줘", "file_candidate", options, 1))
}

final class Lines: @unchecked Sendable {  // `items` behind `lock`
    private let lock = NSLock()
    private var items: [String] = []
    func append(_ line: String) { lock.withLock { items.append(line) } }
    var all: [String] { lock.withLock { items } }
}

@Test func normalCommandTransitionsToDoneThenIdle() async throws {
    let logs = Lines()
    let controller = RuntimeController(try ScriptedPlanner([.outcome(.completed(Completed("완료")))]), emit: logs.append)
    #expect(try await controller.submit("파일 목록") == done("완료"))
    #expect(statePath(controller) == [.idle, .planning, .done, .idle])
    #expect(controller.state == .idle)
    #expect(logs.all == [
        "[STATE] IDLE -> PLANNING reason=command_started",
        "[STATE] PLANNING -> DONE reason=completed",
        "[STATE] DONE -> IDLE reason=result_observed",
    ])
}

@Test func multiToolCommandTransitionsFromStructuredEvents() async throws {
    let planner = try ScriptedPlanner([.call { planner in
        planner.event(.planningStarted, 1)
        planner.event(.toolStarted, 1, "read_text_file")
        planner.event(.toolFinished, 1, "read_text_file")
        planner.event(.planningStarted, 2)
        planner.event(.toolStarted, 2, "write_file")
        planner.event(.toolFinished, 2, "write_file")
        planner.event(.planningStarted, 3)
        return .completed(Completed("저장 완료"))
    }])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("읽고 저장") == done("저장 완료"))
    #expect(statePath(controller) == [.idle, .planning, .executingTool, .planning, .executingTool, .planning, .done, .idle])
}

@Test func typedStateAndPlannerEventObserversPreserveEmitContract() async throws {
    let planner = try ScriptedPlanner([.call { planner in
        planner.event(.toolStarted, 1, "filesystem__read_text_file")
        planner.event(.toolFinished, 1, "filesystem__read_text_file")
        return .completed(Completed("완료"))
    }])
    let transitions = Lines(), events = Lines(), logs = Lines()
    let controller = RuntimeController(
        planner, emit: logs.append, stateObserver: { transitions.append($0.current.name) },
        plannerEventObserver: { events.append($0.toolName ?? "") })
    #expect(try await controller.submit("읽기") == done("완료"))
    #expect(events.all == ["filesystem__read_text_file", "filesystem__read_text_file"])
    #expect(transitions.all == ["PLANNING", "EXECUTING_TOOL", "PLANNING", "DONE", "IDLE"])
    #expect(logs.all[1].hasPrefix("[STATE] PLANNING -> EXECUTING_TOOL"))
}

@Test func clarificationNumberSuffixResumesPendingPlan() async throws {
    let pending = clarification()
    let planner = try ScriptedPlanner([.outcome(.clarification(pending))], resume: [.call { planner in
        planner.event(.planningStarted, 1)
        return .completed(Completed("두 번째 회의록 요약 완료"))
    }])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("회의록 요약해줘") == result(.clarification(pending)))
    #expect(controller.state == .waitingUserInput)
    #expect(try await controller.submit("2번") == done("두 번째 회의록 요약 완료"))
    #expect(statePath(controller) == [.idle, .planning, .waitingUserInput, .resumingPlan, .planning, .done, .idle])
    #expect(planner.runInputs == ["회의록 요약해줘"])
    #expect(planner.resumeInputs.count == 1)
    #expect(planner.resumeInputs[0].context == pending.context && planner.resumeInputs[0].selection == pending.options[1])
}

@Test func errorOutcomeAndUnexpectedExceptionRecoverToIdle() async throws {
    let planner = try ScriptedPlanner([
        .outcome(.error("실패")), .fail(Boom(message: "unexpected")), .outcome(.completed(Completed("복구 완료"))),
    ])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("첫 명령") == result(.error("실패")))
    await #expect(throws: Boom(message: "unexpected")) { try await controller.submit("둘째 명령") }
    #expect(try await controller.submit("셋째 명령") == done("복구 완료"))
    #expect(statePath(controller) == [
        .idle, .planning, .error, .idle, .planning, .error, .idle, .planning, .done, .idle,
    ])
}

@Test(arguments: [(false, RuntimeState.planning), (true, RuntimeState.executingTool)])
func duplicateInputIsBusyAndNeverReachesPlanner(emitTool: Bool, busyState: RuntimeState) async throws {
    let entered = Gate(), release = Gate()
    let planner = try ScriptedPlanner([.call { planner in
        planner.event(.planningStarted, 1)
        if emitTool { planner.event(.toolStarted, 1, "read_text_file") }
        await entered.open()
        await release.wait()
        if emitTool { planner.event(.toolFinished, 1, "read_text_file") }
        return .completed(Completed("첫 명령 완료"))
    }])
    let controller = RuntimeController(planner)
    let first = Task { try await controller.submit("첫 명령") }
    await entered.wait()

    #expect(try await controller.submit("두 번째 명령") == .busy(Busy("다른 명령을 처리 중입니다.", busyState)))
    #expect(planner.runInputs == ["첫 명령"])
    await release.open()
    #expect(try await first.value == done("첫 명령 완료"))
    #expect(controller.state == .idle)
}

@Test func cancelWhileWaitingNeverCallsResume() async throws {
    let feedback = FakeFeedback()
    let planner = try ScriptedPlanner([.outcome(.clarification(clarification()))])
    let controller = RuntimeController(planner, feedback: feedback)
    #expect(try await controller.submit("회의록 요약해줘").clarification != nil)
    let outcome = try #require(try await controller.submit("취소").completed)
    #expect(outcome.text.contains("변경하지 않았습니다"))
    #expect(planner.resumeInputs.isEmpty)
    #expect(Array(statePath(controller).suffix(3)) == [.waitingUserInput, .done, .idle])
    #expect(feedback.log.contains("cancelled:clarification"))
}

@Test func approvalStateExecutesSavedCallAndSubmitStaysBusy() async throws {
    let pending = try await approval()
    let planner = try ScriptedPlanner([.outcome(.approvalRequired(pending))], approve: [.call { planner in
        planner.event(.toolStarted, 1, "filesystem__write_file")
        planner.event(.toolFinished, 1, "filesystem__write_file")
        planner.event(.planningStarted, 2)
        return .completed(Completed("저장 완료"))
    }])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("저장") == result(.approvalRequired(pending)))
    #expect(controller.state == .waitingApproval)
    #expect(controller.pending == nil)
    #expect(controller.pendingApproval == pending)
    #expect(try await controller.submit("새 명령") == .busy(Busy("다른 명령을 처리 중입니다.", .waitingApproval)))
    #expect(try await controller.approvePending() == done("저장 완료"))
    #expect(statePath(controller) == [
        .idle, .planning, .waitingApproval, .resumingPlan, .executingTool, .planning, .done, .idle,
    ])
    #expect(planner.runInputs == ["저장"])
    #expect(planner.approveInputs == [pending.context])
}

@Test func rejectPendingFinishesWithoutApprovalExecution() async throws {
    let pending = try await approval()
    let planner = try ScriptedPlanner([.outcome(.approvalRequired(pending))])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("저장") == result(.approvalRequired(pending)))
    let outcome = try #require(try await controller.rejectPending().completed)
    #expect(outcome.text.contains("거절"))
    #expect(planner.approveInputs.isEmpty)
    #expect(planner.rejectInputs == [pending.context])
    #expect(Array(statePath(controller).suffix(3)) == [.waitingApproval, .done, .idle])
}

@Test func approvalCanPauseAgainForSecondWrite() async throws {
    let first = try await approval("one.txt"), second = try await approval("two.txt")
    let planner = try ScriptedPlanner(
        [.outcome(.approvalRequired(first))],
        approve: [.outcome(.approvalRequired(second)), .outcome(.completed(Completed("완료")))])
    let controller = RuntimeController(planner)
    #expect(try await controller.submit("두 파일") == result(.approvalRequired(first)))
    #expect(try await controller.approvePending() == result(.approvalRequired(second)))
    #expect(controller.state == .waitingApproval)
    #expect(try await controller.approvePending() == done("완료"))
    #expect(planner.approveInputs.count == 2)
}

@Test func approvingWithNothingPendingIsATransitionError() async throws {
    let controller = RuntimeController(try ScriptedPlanner([]))
    await #expect(throws: RuntimeTransitionError("대기 중인 승인이 없습니다.")) { try await controller.approvePending() }
    await #expect(throws: RuntimeTransitionError("대기 중인 승인이 없습니다.")) { try await controller.rejectPending() }
}

@Test func undoFromIdleUsesDoneTransitionWithoutPlanning() async throws {
    let planner = try ScriptedPlanner([], undo: [.outcome(.completed(Completed("생성한 파일을 되돌렸습니다.")))])
    let controller = RuntimeController(planner)
    #expect(try await controller.undoLastWrite() == done("생성한 파일을 되돌렸습니다."))
    #expect(planner.undoCount == 1)
    #expect(statePath(controller) == [.idle, .done, .idle])
}

@Test func undoConflictUsesErrorTransitionWithoutPlanning() async throws {
    let planner = try ScriptedPlanner([], undo: [.outcome(.error("파일이 write 후 변경되어 되돌리지 않았습니다."))])
    let controller = RuntimeController(planner)
    #expect(try await controller.undoLastWrite().errorMessage != nil)
    #expect(statePath(controller) == [.idle, .error, .idle])
}

@Test func undoIsBusyDuringActiveCommand() async throws {
    let entered = Gate(), release = Gate()
    let planner = try ScriptedPlanner([.call { _ in
        await entered.open()
        await release.wait()
        return .completed(Completed("완료"))
    }])
    let controller = RuntimeController(planner)
    let command = Task { try await controller.submit("실행") }
    await entered.wait()
    #expect(try await controller.undoLastWrite() == .busy(Busy("다른 명령을 처리 중입니다.", .planning)))
    #expect(planner.undoCount == 0)
    await release.open()
    _ = try await command.value
}

@Test(arguments: [false, true])
func undoIsBusyDuringPendingInput(approvalPending: Bool) async throws {
    let pending: PlannerOutcome = approvalPending ? .approvalRequired(try await approval()) : .clarification(clarification())
    let planner = try ScriptedPlanner([.outcome(pending)])
    let controller = RuntimeController(planner)
    _ = try await controller.submit("대기")
    #expect(try await controller.undoLastWrite().busy != nil)
    #expect(planner.undoCount == 0)
}

@Test func voiceCompatibilityStatesAreDeclared() {
    #expect(Set(RuntimeState.allCases.map(\.name)) == [
        "IDLE", "RECORDING", "TRANSCRIBING", "PLANNING", "EXECUTING_TOOL",
        "WAITING_USER_INPUT", "WAITING_APPROVAL", "RESUMING_PLAN", "DONE", "ERROR",
    ])
}

@Test func narrowSearchTakesFreeTextAndNBeonPicksAnOption() async throws {
    let options = ["/r/newest.txt", "폴더: 자료"]
    let context = ClarificationContext("임시 읽어줘", "narrow_search", options, 1)
    let question = Clarification("범위를 좁혀 주세요.", options, context, labels: ["최신", "자료"])
    let planner = try ScriptedPlanner(
        [.outcome(.clarification(question)), .outcome(.clarification(question))],
        resume: [.outcome(.completed(Completed("A"))), .outcome(.completed(Completed("B")))])
    let controller = RuntimeController(planner)
    _ = try await controller.submit("임시 읽어줘")
    #expect(try await controller.submit("2") == done("A"))  // a bare number is free text here
    _ = try await controller.submit("임시 읽어줘")
    #expect(try await controller.submit("1번") == done("B"))
    #expect(planner.resumeInputs.map(\.selection) == ["2", "/r/newest.txt"])
    #expect(planner.resumeInputs.allSatisfy { $0.context == context })
}

@Test func anAttachmentBelongsToTheNextCommandOnly() async throws {
    let config = try makeConfig()
    let target = config.path("a b.md")
    try writeText("x", target)
    let planner = try ScriptedPlanner(
        [.outcome(.completed(Completed("1"))), .outcome(.completed(Completed("2")))], config: config)
    let controller = RuntimeController(planner)

    #expect(try controller.attach(target) == target && controller.attachment == target)
    _ = try await controller.submit("이 파일 요약해줘")
    _ = try await controller.submit("그냥 질문")

    #expect(planner.runOptions.map(\.attachment) == [target, nil])
    #expect(controller.attachment == nil)
    #expect(throws: AttachmentError.self) { try controller.attach(config.root.deletingLastPathComponent().path + "/밖.txt") }
    #expect(controller.attachment == nil)
}

@Test func aRefusedDropKeepsTheEarlierAttachmentAndDetachClearsIt() throws {
    let config = try makeConfig()
    try writeText("x", config.path("a.md"))
    let controller = RuntimeController(try ScriptedPlanner([], config: config))
    try controller.attach(config.path("a.md"))
    #expect(throws: AttachmentError.self) { try controller.attach(config.root.path) }  // a folder
    #expect(controller.attachment == config.path("a.md"))
    controller.detach()
    #expect(controller.attachment == nil)
}

@Test func attachingIsRefusedWhileACommandIsRunningOrWaiting() async throws {
    let config = try makeConfig()
    try writeText("x", config.path("a.md"))
    let context = ClarificationContext("요약", "file_candidate", ["1"], 1)
    let controller = RuntimeController(
        try ScriptedPlanner([.outcome(.clarification(Clarification("?", ["1"], context)))], config: config))
    _ = try await controller.submit("요약해줘")  // now waiting for an answer
    #expect(throws: AttachmentError("작업이 끝난 뒤에 파일을 놓아 주세요.")) { try controller.attach(config.path("a.md")) }
}

@Test func resetConversationForgetsFollowUpContextOnlyWhenIdle() async throws {
    let controller = RuntimeController(try ScriptedPlanner([.outcome(.completed(Completed("완료")))]))
    _ = try await controller.submit("파일 목록")
    #expect(controller.previous != nil)
    #expect(controller.resetConversation())
    #expect(controller.previous == nil)
    controller.state = .planning
    #expect(!controller.resetConversation())
}

/// tests/test_feedback.py `test_feedback_command_goes_to_the_loop_not_the_planner` (loop faked).
@Test func feedbackCommandGoesToTheLoopNotThePlanner() async throws {
    let feedback = FakeFeedback()
    let planner = try ScriptedPlanner([.outcome(.completed(Completed("답")))])
    let controller = RuntimeController(planner, feedback: feedback)
    _ = try await controller.submit("질문")
    let previous = controller.previous
    let reply = try #require(try await controller.submit("/feedback 결과가 너무 많아").completed)
    #expect(reply.text.contains("피드백"))
    #expect(feedback.log.last == "user_feedback:결과가 너무 많아")
    #expect(controller.previous == previous && controller.state == .idle && planner.runInputs == ["질문"])
    #expect(try await controller.submit("/feedback").errorMessage != nil)
    #expect(try await RuntimeController(try ScriptedPlanner([])).submit("/FEEDBACK 느려").errorMessage != nil)
    #expect(try await RuntimeController(try ScriptedPlanner([]), feedback: FakeFeedback(accept: false))
        .submit("/feedback 느려").errorMessage?.contains("저장하지 못했어요") == true)
    #expect(!isFeedbackCommand("/feedbackx") && isFeedbackCommand("  /feedback\n"))
}

@Test func feedbackIsToldAboutRunsAndFailures() async throws {
    let feedback = FakeFeedback()
    let planner = try ScriptedPlanner([.outcome(.completed(Completed("ok"))), .fail(Boom(message: "x"))])
    let controller = RuntimeController(planner, feedback: feedback)
    _ = try await controller.submit("파일 목록")
    #expect(feedback.log == [
        "user_input:파일 목록:true", "state:IDLE->PLANNING", "outcome",
        "state:PLANNING->DONE", "state:DONE->IDLE", "run_finished",
    ])
    await #expect(throws: Boom.self) { try await controller.submit("또") }
    #expect(feedback.log.suffix(4) == ["exception", "state:PLANNING->ERROR", "state:ERROR->IDLE", "run_finished"])
}

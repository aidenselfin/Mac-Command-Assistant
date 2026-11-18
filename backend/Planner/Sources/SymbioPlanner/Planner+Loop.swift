import Foundation
import SymbioConvert
import SymbioCore
import SymbioDelete
import SymbioFileOps
import SymbioGuard

/// Python `Planner._continue` and the recovery / progress checks around it.

private let deleteClaim = Pattern(#"삭제(?:했|하였|되었|됐)|삭제(?:를)?\s*완료(?!하지|되지|\s*못)|지웠"#)
private let unsupportedClaim = Pattern(#"(?:도구|기능|지원)[^\n]{0,120}(?:없|않|불가)"#)

func nowNs() -> UInt64 { DispatchTime.now().uptimeNanoseconds }
func elapsedMs(_ started: UInt64) -> Double { Double(nowNs() - started) / 1_000_000 }

func jsonText(_ text: String?) -> JSON { text.map(JSON.string) ?? .null }

private enum Raced<T: Sendable>: Sendable {
    case value(T)
    case timedOut
}

/// Python `async with asyncio.timeout(seconds)`: nil when the time ran out first.
/// The body is cancelled on timeout; an outer cancel reaches it through the task group.
func withTimeout<T: Sendable>(_ seconds: Double, _ body: @escaping @Sendable () async throws -> T) async throws -> T? {
    try await withThrowingTaskGroup(of: Raced<T>.self) { group in
        group.addTask { .value(try await body()) }
        group.addTask {
            try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
            return .timedOut
        }
        defer { group.cancelAll() }
        guard let first = try await group.next() else { return nil }
        if case let .value(value) = first { return value }
        return nil
    }
}

extension Planner {
    func continueLoop(_ state: LoopState) async throws -> PlannerOutcome {
        while true {
            try Task.checkCancellation()
            if !state.toolCalls.isEmpty {
                var outcome = try await processPendingCalls(state)
                if outcome == nil { outcome = try await checkProgress(state) }
                if let outcome { return outcome }
                state.toolCalls = []
                state.nextCallIndex = 0
                state.iteration += 1
                continue
            }

            if state.modelCalls >= config.maxToolRounds {
                let completion = state.writeCompleted ? " 승인된 파일 변경은 완료되었습니다." : ""
                return .error("모델 호출 한도 초과 (\(config.maxToolRounds)회); 추가 모델 호출 없이 종료했습니다.\(completion)")
            }
            let remaining = config.maxRunSeconds - (clock() - state.started)
            if remaining <= 0 { return timeLimitError(state) }

            notify(PlannerEvent(.planningStarted, state.iteration))
            emit("[PLAN] iteration=\(state.iteration)/\(config.maxToolRounds)")
            let llmStarted = nowNs()
            let webSavePending = state.webSaveRequired && !state.webWriteCompleted && !state.webFailed
            let webContentPending =
                requiresWebContent(state.userText, state.userURLs) && !state.webSnapshotSucceeded && !state.webFailed
            let snapshotPending = state.webNavigationSucceeded && !state.webSnapshotSucceeded
            var exposedTools: [ToolDefinition]
            if webSavePending || webContentPending {
                let requiredTool =
                    snapshotPending
                    ? webSnapshotTool : (state.webSnapshotSucceeded ? filesystemWriteTool : webNavigateTool)
                exposedTools = state.tools.filter { $0.name == requiredTool }
            } else if snapshotPending {
                exposedTools = state.tools.filter { $0.name == webSnapshotTool }
            } else if !state.userURLs.isEmpty {
                exposedTools = state.tools.filter { $0.name != filesystemWriteTool }
            } else {
                exposedTools = state.tools
            }
            exposedTools.removeAll { $0.name == macosActiveAppTool && state.activeAppName != nil }
            state.modelCalls += 1
            state.exposedToolNames = Set(exposedTools.map(\.name))
            let model = activeModel(state)
            let messages = state.messages
            let tools = exposedTools
            let allowClarification = !(snapshotPending || webSavePending || webContentPending)
            let turn: ModelTurn
            do {
                defer { timing.appendLLMRound(elapsedMs(llmStarted)) }
                guard
                    let generated = try await withTimeout(remaining, {
                        try await model.generate(messages, tools, allowTools: true, allowClarification: allowClarification)
                    })
                else { return timeLimitError(state) }
                try Task.checkCancellation()
                turn = generated
            } catch let error as PlannerError where error.kind == .modelFormat {
                // A broken reply is told what to fix, once per round, instead of ending.
                state.formatErrors += 1
                if state.formatErrors > maxFormatErrors {
                    return .error("모델 응답 형식 오류가 반복되어 멈췄습니다: \(error.message)")
                }
                state.messages.append(["role": "system", "content": .string(formatNudge(error.message))])
                recoveryFailed(state, "응답 형식 오류")
                state.iteration += 1
                continue
            }

            if let request = turn.clarification {
                let typedRefusal = request.kind == "free_text" ? freeTextRefusal(state, request.question, roots: config.allowedDirectories) : nil
                if typedRefusal != nil
                    || (request.kind != "free_text"
                        && (request.kind != "edit_criteria" || !ambiguousMutation(state.userText)))
                {
                    let arguments: JSON = [
                        "question": .string(request.question), "options": .array(request.options.map(JSON.string)),
                        "kind": .string(request.kind),
                    ]
                    state.messages.append([
                        "role": "assistant", "content": jsonText(turn.text),
                        "tool_calls": [[
                            "id": .string(request.callID), "type": "function",
                            "function": [
                                "name": .string(clarificationToolName),
                                "arguments": .string(arguments.dumps(JSONDumpOptions(ensureASCII: false))),
                            ],
                        ]],
                    ])
                    state.messages.append([
                        "role": "tool", "tool_call_id": .string(request.callID),
                        "content": .string(
                            typedRefusal
                                ?? "clarification_error: 현재 요청에는 결과를 바꾸는 미해결 ambiguity가 없습니다. 원래 요청을 계속하세요."
                        ),
                    ])
                    emit("[PLAN] clarification policy rejected")
                    if state.webSaveRequired {
                        state.webFlowCorrections += 1
                        if state.webFlowCorrections > maxClarifications {
                            return .error("웹 저장 절차를 완료하지 못해 파일을 변경하지 않았습니다.")
                        }
                    } else {
                        state.iteration += 1
                    }
                    continue
                }
                let count = state.priorClarifications + 1
                if count > maxClarifications {
                    return .error("clarification 최대 횟수를 초과해 파일을 변경하지 않았습니다.")
                }
                return .clarification(Clarification(
                    request.question, request.options,
                    ClarificationContext(
                        state.userText, request.kind, request.options, count, conditions: state.conditions,
                        attachment: state.attachment ?? "")))
            }
            if turn.toolCalls.isEmpty {
                if let outcome = try finalReply(state, turn, webSavePending, webContentPending, snapshotPending) {
                    return outcome
                }
                continue
            }
            state.messages.append([
                "role": "assistant", "content": jsonText(turn.text),
                "tool_calls": .array(turn.toolCalls.map {
                    [
                        "id": .string($0.id), "type": "function",
                        "function": ["name": .string($0.name), "arguments": .string($0.arguments)],
                    ]
                }),
            ])
            state.toolCalls = mergeDeleteCalls(turn.toolCalls)
            state.nextCallIndex = 0
        }
    }

    /// A turn with no tool calls: finish, or nudge the model and go round again (nil).
    private func finalReply(
        _ state: LoopState, _ turn: ModelTurn, _ webSavePending: Bool, _ webContentPending: Bool,
        _ snapshotPending: Bool
    ) throws -> PlannerOutcome? {
        if webSavePending || (webContentPending && !snapshotPending) {
            state.messages.append(["role": "assistant", "content": .string(turn.text ?? "")])
            state.messages.append([
                "role": "system",
                "content": .string(
                    "웹 본문 요청은 navigation, snapshot을 완료해야 하며 "
                        + "저장 요청은 승인된 filesystem__write_file까지 필요합니다. "
                        + "최종 응답 대신 현재 단계의 도구를 호출하세요."),
            ])
            state.webFlowCorrections += 1
            if state.webFlowCorrections > maxClarifications {
                return .error("웹 절차를 완료하지 못해 파일을 변경하지 않았습니다.")
            }
            return nil
        }
        if state.webNavigationSucceeded && !state.webSnapshotSucceeded {
            return .error("페이지 내용을 web__browser_snapshot으로 읽지 못해 요청을 완료할 수 없습니다.")
        }
        if state.searchRequired && !state.searchCompleted {
            return .error("모호한 파일 요청은 안전한 후보 탐색 없이 진행할 수 없습니다.")
        }
        if state.singleSearchCandidate != nil && !state.candidateRead {
            return .error("검증된 후보 파일을 읽지 않아 요청을 완료할 수 없습니다.")
        }
        if !state.rankedCandidates.isEmpty && Set(state.readPaths).isDisjoint(with: state.rankedCandidates) {
            return .error("고른 후보 파일을 읽지 않아 요청을 완료할 수 없습니다.")
        }
        let text = turn.text ?? ""
        let supported = state.tools.map(\.name).filter {
            ($0 == deleteTool && destructiveRequest(state.userText))
                || (fileOperations[$0] != nil && operationRequested($0, state.userText))
        }
        if !supported.isEmpty && !state.writeCompleted && !text.isEmpty && unsupportedClaim.search(text) {
            if state.confirmNudges > 0 { return .error("지원되는 파일 도구로 요청을 실행하지 못했습니다.") }
            state.confirmNudges += 1
            state.messages.append(["role": "assistant", "content": .string(text)])
            state.messages.append([
                "role": "system",
                "content": .string(
                    "현재 지원·제공되는 도구: \(supported.joined(separator: ", ")). "
                        + "기능이 없다고 답하지 말고 실제 도구를 호출하세요. 승인은 앱이 받습니다."),
            ])
            state.iteration += 1
            return nil
        }
        if state.tools.contains(where: { $0.name == deleteTool }) && destructiveRequest(state.userText)
            && !state.writeCompleted && deleteClaim.search(text)
        {
            // "삭제했습니다" before any approved delete: the approval screen has not been shown.
            if state.confirmNudges > 0 { return .error("승인된 삭제 없이 삭제했다고 답해 멈췄습니다. 파일은 그대로입니다.") }
            state.confirmNudges += 1
            state.messages.append(["role": "assistant", "content": .string(text)])
            state.messages.append([
                "role": "system",
                "content": .string("아직 아무 파일도 삭제되지 않았습니다. 삭제는 local__delete_files를 호출해 "
                    + "앱의 승인을 받아야 합니다. 완료라고 답하지 말고 도구를 호출하세요."),
            ])
            state.iteration += 1
            return nil
        }
        let operationAsked = fileOperations.keys.contains { operationRequested($0, state.userText) }
        if state.confirmNudges == 0 && !text.isEmpty && state.changedPaths.isEmpty && !state.writeCompleted
            && isConfirmation(text)
            && (destructiveRequest(state.userText) || conversionRequestPattern.search(state.userText)
                || mutationPattern.search(state.userText) || operationAsked)
        {
            // The model stopped to ask "shall I?" in words. The approval screen is
            // the real question, so it gets one nudge to make the tool call.
            state.confirmNudges += 1
            state.messages.append(["role": "assistant", "content": .string(text)])
            state.messages.append(["role": "system", "content": .string(confirmNudge)])
            state.iteration += 1
            emit("[PLAN] confirmation reply turned back into a tool call")
            return nil
        }
        if operationAsked && !state.writeCompleted {
            if state.confirmNudges == 0 && !state.findNone {
                state.confirmNudges += 1
                state.messages.append(["role": "assistant", "content": jsonText(turn.text)])
                state.messages.append([
                    "role": "system",
                    "content": .string(
                        "파일 이동·복사·폴더 생성 도구가 제공되어 "
                            + "있습니다. 승인된 실제 파일 작업 없이는 완료로 답하지 마세요. "
                            + "필요한 경로를 질문하거나 해당 도구를 호출하세요."),
                ])
                state.iteration += 1
                return nil
            }
            return .error("요청한 파일 작업을 실행하지 못했습니다. 원본·목적지·허용 경로를 확인해 주세요.")
        }
        if pyStrip(text).isEmpty {
            let escalated = recoveryFailed(state, "빈 최종 응답")
            if (state.emptyRetries == 0 || escalated) && state.modelCalls < config.maxToolRounds {
                // After tool results a model sometimes answers with nothing. It is told
                // to write the answer once, and once more if a stronger model took over.
                state.emptyRetries += 1
                state.messages.append(["role": "system", "content": .string(emptyNudge)])
                state.iteration += 1
                emit("[PLAN] empty final reply: asked for the answer once")
                return nil
            }
            throw PlannerError("LLM 최종 응답이 비어 있습니다.")
        }
        let reply = state.partialRead.map { text + partialReadNotice($0) } ?? text
        return .completed(Completed(
            reply, changedPaths: state.changedPaths, readPaths: state.readPaths, foundPaths: state.foundPaths))
    }

    func timeLimitError(_ state: LoopState) -> PlannerOutcome {
        let completion = state.writeCompleted ? " 승인된 파일 변경은 완료되었습니다." : ""
        return .error(
            "시간 한도 초과 (\(String(format: "%g", config.maxRunSeconds))초); 추가 모델 호출 없이 종료했습니다.\(completion)")
    }

    func activeModel(_ state: LoopState) -> any ModelClient {
        if state.escalated, let escalation { return escalation }
        return model
    }

    /// Count a failed fix; true when this one hands the conversation to the stronger model.
    @discardableResult
    func recoveryFailed(_ state: LoopState, _ reason: String) -> Bool {
        state.recoveries += 1
        emit("[RECOVER] \(reason) (count=\(state.recoveries))")
        notify(PlannerEvent(.recovering, state.iteration, recoveringLabel))
        if state.recoveries >= 2 && escalation != nil && !state.escalated {
            state.escalated = true
            emit("[RECOVER] stronger model takes over")
            return true
        }
        return false
    }

    /// After a round of tool calls: a repeat gets a warning, a third one ends the command.
    func checkProgress(_ state: LoopState) async throws -> PlannerOutcome? {
        let start =
            state.messages.lastIndex {
                $0["role"]?.stringValue == "assistant" && truthy($0["tool_calls"])
            } ?? -1
        var results: [String: String] = [:]
        for message in state.messages[(start + 1)...] where message["role"]?.stringValue == "tool" {
            let id = message["tool_call_id"]?.stringValue ?? ""
            let content = message["content"]
            results[id] = content?.stringValue ?? content?.dumps() ?? "None"
        }
        var worst = 0, errors = 0
        for call in state.toolCalls {
            guard let content = results[call.id] else { continue }
            let key = callKey(call, content)
            state.callCounts[key, default: 0] += 1
            worst = max(worst, state.callCounts[key]!)
            if content.hasPrefix("tool_error") { errors += 1 }
        }
        if worst >= 3 { return try await stopHonestly(state) }
        if worst == 2 {
            emit("[PLAN] 같은 시도가 같은 결과를 냄")
            state.messages.append(["role": "system", "content": .string(repeatNudge)])
            recoveryFailed(state, "같은 시도 반복")
        } else if !results.isEmpty && errors == results.count {
            recoveryFailed(state, "도구 오류")
        }
        return nil
    }

    /// Stop a loop that makes no progress, with the model's own account of what it found.
    func stopHonestly(_ state: LoopState) async throws -> PlannerOutcome {
        var note = "같은 시도가 같은 결과를 세 번 내서 반복을 멈췄습니다."
        if state.writeCompleted { note += " 승인된 파일 변경은 완료되었습니다." }
        var report = ""
        if state.modelCalls < config.maxToolRounds {
            state.messages.append(["role": "system", "content": .string(honestStop)])
            state.modelCalls += 1
            let started = nowNs()
            defer { timing.appendLLMRound(elapsedMs(started)) }
            do {
                let turn = try await activeModel(state).generate(
                    state.messages, state.tools, allowTools: false, allowClarification: false)
                report = pyStrip(turn.text ?? "")
            } catch is PlannerError {}
        }
        return .error(pyStrip("\(note) \(report)"))
    }
}

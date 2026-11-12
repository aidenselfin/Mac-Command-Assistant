import Foundation
import SymbioConvert
import SymbioCore
import SymbioDelete
import SymbioFileOps
import SymbioFinder
import SymbioGuard
import SymbioSearch

/// Python `Planner._process_pending_calls` and the per-call policy, execution and result steps.

private let recursiveDelete = Pattern(#"재귀|하위\s*폴더|recursive|\*\*"#, ignoreCase: true)

/// Python `json.dumps(value, ensure_ascii=False)`.
func dumpsUnicode(_ value: JSON) -> String { value.dumps(JSONDumpOptions(ensureASCII: false)) }

/// Python `tuple(dict.fromkeys(...))`: drop repeats, keep first order.
func unique(_ items: [String]) -> [String] {
    var seen: Set<String> = []
    return items.filter { seen.insert($0).inserted }
}

private final class Stamp: @unchecked Sendable {  // written once by onStarted before onFinished reads it
    var value: UInt64 = 0
}

extension Planner {
    func processPendingCalls(_ state: LoopState) async throws -> PlannerOutcome? {
        while state.nextCallIndex < state.toolCalls.count {
            let call = state.toolCalls[state.nextCallIndex]
            let risk = classifyTool(call.name)
            if risk == .forbidden {
                appendToolError(state, call, "tool_error: 금지된 도구입니다: \(call.name)")
                state.nextCallIndex += 1
                continue
            }
            let parsed: JSON
            do {
                parsed = try JSON.parse(call.arguments)
            } catch {
                let offset = (error as? JSONParseError)?.offset ?? 0
                appendToolError(state, call, "tool_error: 잘못된 도구 인자 JSON (char \(offset))")
                state.nextCallIndex += 1
                continue
            }
            guard case let .object(pairs) = parsed else {
                appendToolError(state, call, "tool_error: 도구 인자는 object여야 합니다.")
                state.nextCallIndex += 1
                continue
            }
            // null means "not given". Dropping it here keeps every policy and schema below
            // looking at the arguments the model actually meant.
            var arguments = JSON.object(pairs.filter { !$0.1.isNull })
            for key in ["path", "source"] {
                if let value = arguments[key] { arguments.set(key, onDiskPath(value, config.allowedDirectories)) }
            }

            if call.name == macosActiveAppTool, let name = state.activeAppName {
                state.messages.append(["role": "tool", "tool_call_id": .string(call.id), "content": .string(name)])
                state.nextCallIndex += 1
                continue
            }

            if let policy = try await policyError(state, call, arguments) {
                appendToolError(state, call, "tool_error: \(policy)")
                state.nextCallIndex += 1
                continue
            }

            let signature = ToolSignature(call.name, arguments.dumps(JSONDumpOptions(sortKeys: true)))
            if signature == state.previousSignature { emit("[PLAN] 동일 도구 호출 반복 감지") }
            state.previousSignature = signature

            // Offering a tool is the permission to run it: a provider returning another
            // known tool name is refused, not trusted. Checked right before running, so
            // the more specific policy errors above still explain a refusal first.
            if let exposed = state.exposedToolNames, !exposed.contains(call.name) {
                appendToolError(state, call, "tool_error: 이번 단계에서 제공되지 않은 도구입니다: \(call.name)")
                state.nextCallIndex += 1
                continue
            }
            if risk == .write {
                let prepared: PreparedWrite
                do {
                    prepared = try await riskGuard.prepareWrite(call.name, arguments)
                } catch where isToolFailure(error) {
                    if state.webSaveRequired && state.webSnapshotSucceeded { state.webFailed = true }
                    appendToolError(state, call, "tool_error: \(errorText(error))")
                    state.nextCallIndex += 1
                    continue
                }
                return .approvalRequired(approvalRequired(state, prepared))
            }

            guard let result = try await executeReadOnly(state, call, arguments) else {
                state.nextCallIndex += 1
                continue
            }
            if let outcome = try await acceptToolResult(state, call, result) { return outcome }
            state.nextCallIndex += 1
        }
        return nil
    }

    func policyError(_ state: LoopState, _ call: ToolCall, _ arguments: JSON) async throws -> String? {
        let roots = config.allowedDirectories
        if fileOperations[call.name] != nil {
            if !state.userURLs.isEmpty || state.webAttempted || state.readOnlyCandidateRequest {
                return "웹·읽기 전용 요청에서는 파일 이동·복사·폴더 생성을 실행할 수 없습니다."
            }
            var requested = operationRequested(call.name, state.userText)
            if call.name == mkdirTool {
                requested =
                    requested || operationRequested(moveTool, state.userText)
                    || operationRequested(copyTool, state.userText)
            }
            if !requested { return "사용자가 직접 요청한 파일 작업만 실행할 수 있습니다." }
            let text = state.userText + "\n" + state.conditions.joined(separator: "\n")
            if call.name != mkdirTool {
                let source = arguments["source"]
                var chosen = state.attachment ?? state.singleSearchCandidate
                if chosen == nil, let path = source?.stringValue, state.groupPaths.contains(path) {
                    chosen = path  // listed by the app itself, not named by the model
                }
                if let chosen, source != .string(chosen) { return "첨부하거나 선택한 파일만 이동·복사할 수 있습니다." }
                if chosen == nil && !writeTargetIsExplicit(source, text, []) {
                    return "이동·복사 원본을 local__find_files로 찾거나 정확한 파일명을 지정하세요."
                }
                if chosen == nil, let source = source?.stringValue {
                    let qualified = Pattern(#"(?:/|[\w.\-]+/)[^\s"'<>]*"# + escape(pathName(source))).all(text)
                        .map(\.value)
                    if !qualified.isEmpty && !qualified.contains(where: { source == $0 || source.hasSuffix("/" + $0) }) {
                        return "이동·복사 원본 경로가 사용자 요청의 경로와 일치하지 않습니다."
                    }
                }
            }
            guard let raw = arguments[call.name == mkdirTool ? "path" : "destination"]?.stringValue else {
                return "목적지 경로를 지정해야 합니다."
            }
            if !raw.hasPrefix("/") || !roots.contains(where: { isRelative(raw, to: $0.path) }) {
                return "목적지는 허용 폴더 안의 절대경로여야 합니다."
            }
            var explicit = mentionedDirectory(raw, text, roots)
            if call.name != mkdirTool {
                explicit =
                    explicit || mentionedDirectory(pathParent(raw), text, roots)
                    || (pathName(raw) != pathName(arguments["source"]?.stringValue ?? "")
                        && writeTargetIsExplicit(.string(raw), text, []))
            }
            return explicit ? nil : "목적지 파일명 또는 폴더가 사용자 요청에 명시되지 않았습니다."
        }
        if call.name == filesystemWriteTool
            && (operationRequested(moveTool, state.userText) || operationRequested(copyTool, state.userText))
        {
            return "이동·복사는 전용 도구를 사용하세요. 텍스트 쓰기로 대신할 수 없습니다."
        }
        if let attachment = state.attachment {
            if call.name == convertTool && arguments["source"] != .string(attachment) {
                return "첨부한 파일만 변환할 수 있습니다."
            }
            if call.name == deleteTool && arguments["paths"] != .array([.string(attachment)]) {
                return "첨부한 파일 하나만 삭제할 수 있습니다."
            }
        }
        if call.name == convertTool && state.readOnlyCandidateRequest { return "읽기 전용 요청에서는 파일을 변환할 수 없습니다." }
        if call.name == convertTool && (!state.userURLs.isEmpty || state.webAttempted) {
            return "웹 요청에서는 파일을 변환할 수 없습니다."
        }
        if call.name == deleteTool {
            if state.readOnlyCandidateRequest { return "읽기 전용 요청에서는 파일을 삭제할 수 없습니다." }
            if let candidate = state.singleSearchCandidate, arguments["paths"] != .array([.string(candidate)]) {
                return "사용자가 선택한 파일 하나만 삭제할 수 있습니다."
            }
            if recursiveDelete.search(state.userText) { return "폴더/재귀 삭제는 지원하지 않습니다. 파일만 삭제할 수 있습니다." }
            if state.attachment != nil {
                // The attached file is the named target (checked above to be the only one);
                // the delete must still be the user's own plain request, not a web command.
                if !destructiveRequest(state.userText) || (webActive && userURLPattern.search(state.userText)) {
                    return "사용자가 직접 요청한 파일 삭제만 허용합니다. 웹과 분리하세요."
                }
                return nil
            }
            if state.searchRequired && !state.searchCompleted { return "삭제 대상을 추측하지 말고 먼저 local__find_files로 찾으세요." }
            return deleteScopeError(arguments, state.userText, roots, web: webActive, seen: state.seenPaths)
        }
        if call.name == filesystemWriteTool
            && (!state.userURLs.isEmpty || (webActive && userURLPattern.search(state.userText)))
            && !state.webSaveRequired
        {
            return "웹 내용의 파일 저장 요청이 확인되지 않아 파일을 쓸 수 없습니다."
        }
        if state.webNavigationSucceeded && !state.webSnapshotSucceeded && call.name != webSnapshotTool {
            return "navigation 다음에는 web__browser_snapshot만 호출할 수 있습니다."
        }
        if call.name == webNavigateTool {
            state.webAttempted = true
            let requestedURL = arguments["url"]
            var error = webURLError(requestedURL, allowLocal: allowLocalWeb)
            if error == nil && !state.userURLs.contains(where: { requestedURL == .string($0) }) {
                error = "web navigation URL이 사용자 원문에 명시되지 않았습니다."
            }
            if state.webFailed && error == nil { error = "실패한 web 작업은 같은 명령에서 재시도할 수 없습니다." }
            if error != nil { state.webFailed = true }
            return error
        }
        if call.name == webSnapshotTool {
            state.webAttempted = true
            if state.webFailed || !state.webNavigationSucceeded {
                state.webFailed = true
                return "성공한 navigation 없이는 snapshot을 읽을 수 없습니다."
            }
        }
        if call.name == filesystemWriteTool && (state.webAttempted || !state.userURLs.isEmpty) {
            if state.webFailed || !state.webSnapshotSucceeded { return "web navigation/snapshot 실패 뒤에는 파일을 쓸 수 없습니다." }
            if !writeTargetIsExplicit(arguments["path"], state.userText, state.userURLs) {
                state.webFailed = true
                return "출력 파일이 사용자 원문에 명시되지 않았습니다."
            }
        }
        if state.searchRequired && !state.searchCompleted {
            if call.name != findFilesTool { return "모호한 파일 요청은 local__find_files로 먼저 탐색해야 합니다." }
            let keywords = (arguments["keywords"]?.arrayValue ?? []).map { $0.stringValue ?? $0.dumps() }
            if state.readOnlyCandidateRequest && !keywords.contains(where: { $0.contains("회의록") }) {
                return "회의록 후보 탐색의 keywords에는 '회의록'이 들어 있어야 합니다."
            }
            return nil
        }
        if call.name == findFilesTool && state.singleSearchCandidate != nil {
            return "이미 대상 파일이 정해졌습니다. 그 파일로 진행하세요."
        }
        if state.readOnlyCandidateRequest && call.name == filesystemWriteTool {
            return "이 요청은 read-only 요약이므로 파일을 쓸 수 없습니다."
        }
        if call.name == filesystemWriteTool && state.singleSearchCandidate != nil && !state.candidateRead {
            return "검증된 후보 파일을 읽기 전에는 쓸 수 없습니다."
        }
        if writeTools.contains(call.name) && !state.rankedCandidates.isEmpty
            && Set(state.readPaths).isDisjoint(with: state.rankedCandidates)
        {
            // Checked before approval, so a resumed approval needs no copy of this state.
            return "고른 후보 파일을 읽기 전에는 파일을 바꿀 수 없습니다."
        }
        if contentReadTools.union([openFileTool]).contains(call.name), let candidate = state.singleSearchCandidate {
            var requested: URL?
            if let path = arguments["path"]?.stringValue {
                do {
                    requested = try await gateway.validateResultPath(path)
                } catch is CancellationError {
                    throw CancellationError()
                } catch {
                    requested = nil
                }
            }
            if requested.map({ pyPath($0.path) != pyPath(candidate) }) ?? true {
                return "검증된 단일 후보 파일만 읽거나 열 수 있습니다."
            }
        }
        return nil
    }

    func executeReadOnly(_ state: LoopState, _ call: ToolCall, _ arguments: JSON) async throws -> ToolResult? {
        emit("[TOOL] iteration=\(state.iteration) name=\(call.name)")
        notify(PlannerEvent(.toolStarted, state.iteration, call.name))
        let started = nowNs()
        defer {
            timing.recordTool(call.name, elapsedMs(started))
            notify(PlannerEvent(.toolFinished, state.iteration, call.name))
        }
        do {
            return try await riskGuard.executeReadOnly(call.name, arguments)
        } catch where isToolFailure(error) {
            if webTools.contains(call.name) { state.webFailed = true }
            appendToolError(state, call, "tool_error: \(errorText(error))")
            return nil
        }
    }

    func executeApprovedWrite(_ state: LoopState, _ call: ToolCall, _ prepared: PreparedWrite) async throws
        -> ToolResult
    {
        let stamp = Stamp()
        let iteration = state.iteration
        return try await riskGuard.executeApproved(
            prepared,
            onStarted: { [self] in
                stamp.value = nowNs()
                emit("[TOOL] iteration=\(iteration) name=\(call.name)")
                notify(PlannerEvent(.toolStarted, iteration, call.name))
            },
            onFinished: { [self] in
                timing.recordTool(call.name, elapsedMs(stamp.value))
                notify(PlannerEvent(.toolFinished, iteration, call.name))
            })
    }

    func acceptToolResult(_ state: LoopState, _ call: ToolCall, _ result: ToolResult) async throws
        -> PlannerOutcome?
    {
        if !result.ok {
            if webTools.contains(call.name) { state.webFailed = true }
            appendToolError(state, call, "tool_error: \(result.error ?? result.text)")
            return nil
        }
        if call.name == macosActiveAppTool {
            let name = pyStrip(result.text)
            if name.isEmpty {
                appendToolError(state, call, "tool_error: 현재 활성 앱 이름을 확인할 수 없습니다.")
                return nil
            }
            state.activeAppName = name
            if state.activeAppOnly && state.toolCalls.count == 1 {
                return .completed(Completed("현재 활성 앱은 \(name)입니다."))
            }
        }
        if webTools.contains(call.name) {
            if let error = webURLError(pageURL(result.text).map(JSON.string), allowLocal: allowLocalWeb) {
                state.webFailed = true
                appendToolError(state, call, "tool_error: redirect/final URL 거부: \(error)")
                return nil
            }
            if call.name == webNavigateTool { state.webNavigationSucceeded = true } else { state.webSnapshotSucceeded = true }
        }
        var resultText = call.name == webNavigateTool ? navigationResultForModel(result.text) : result.text
        if contentResultTools.contains(call.name) {
            emit(
                "[TOOL-OK] iteration=\(state.iteration) name=\(call.name) "
                    + "result_chars=\(result.text.unicodeScalars.count) truncated=\(result.truncated ? "True" : "False")")
        } else {
            emit("[TOOL-OK] iteration=\(state.iteration) name=\(call.name)\n\(result.text)")
        }
        if contentReadTools.contains(call.name) {
            state.partialRead = state.partialRead ?? partialRead(call, result)
            if let read = (try? JSON.parse(call.arguments))?["path"]?.stringValue, !state.readPaths.contains(read) {
                state.readPaths.append(read)
            }
        }
        if call.name == findFilesTool, let structured = result.structured {
            if let outcome = try await acceptFindResult(state, call, structured) { return outcome }
            if let override = state.findOverride {
                resultText = override
                state.findOverride = nil
            }
        } else if call.name == groupFilesTool, let structured = result.structured {
            let listed = (structured["groups"]?.arrayValue ?? []).flatMap {
                ($0["files"]?.arrayValue ?? []).compactMap(\.stringValue)
            }
            state.groupPaths = unique(state.groupPaths + listed)
        } else if call.name == listFilesTool, let structured = result.structured {
            let files = structured["files"]?.arrayValue ?? []
            if files.count == 1, let path = files[0]["path"]?.stringValue {  // "가장 최근 파일 줘": one pick is a draggable file too
                state.foundPaths = [path]
            }
        } else if contentReadTools.contains(call.name) && state.singleSearchCandidate != nil {
            state.candidateRead = true
        } else if call.name == filesystemWriteTool {
            state.writeCompleted = true
            if state.webSaveRequired { state.webWriteCompleted = true }
        } else if call.name == deleteTool || call.name == convertTool || fileOperations[call.name] != nil {
            state.writeCompleted = true
        }
        state.messages.append(["role": "tool", "tool_call_id": .string(call.id), "content": .string(resultText)])
        return nil
    }

    /// One decision for every file lookup: ask the user, stop, or pin the single match.
    func acceptFindResult(_ state: LoopState, _ call: ToolCall, _ response: JSON) async throws -> PlannerOutcome? {
        state.searchCompleted = true
        let candidates = response["candidates"]?.arrayValue ?? []
        let candidatePaths = candidates.map { $0["path"]?.stringValue ?? "" }
        state.seenPaths += candidatePaths
        state.findTried = true
        let decision = response["decision"]?.stringValue
        state.findNone = decision == "none"
        if decision == "single" { state.foundPaths = [candidatePaths.first ?? ""] }
        let arguments = (try? JSON.parse(call.arguments)) ?? .null
        let kept = JSON.object(findArgumentKeys.compactMap { key in
            truthy(arguments[key]) ? (key, arguments[key]!) : nil
        })
        state.lastSearch = scalarPrefix(dumpsUnicode(kept), 500)
        if let named = Self.namedCandidates(state, response) {
            state.findOverride = named
            return nil
        }
        let complete = response["complete"]
        if isPatternDelete(state.userText) && (decision == "choose" || decision == "narrow") {
            // "*.txt 파일을 모두 삭제"는 고를 파일이 아니라 폴더와 패턴으로 정하는 요청이다.
            state.findOverride = dumpsUnicode([
                "decision": "pattern",
                "note": "폴더와 패턴으로 정하는 삭제입니다. 파일을 고르게 하지 말고 local__delete_files의 directory와 pattern으로 바로 요청하세요.",
            ])
            return nil
        }
        if destructiveRequest(state.userText) && decision == "choose" && complete != .bool(false) {
            // 어떤 파일을 지울지는 봇이 정하고, 사용자는 삭제 승인 화면에서 한 번만 확인한다.
            state.findOverride = dumpsUnicode([
                "decision": "delete_scope",
                "note": .string(
                    "삭제 대상은 묻지 말고 요청에 맞는 후보를 직접 골라 "
                        + "local__delete_files(paths)를 호출하세요. 요청과 무관한 파일은 넣지 마세요. "
                        + "사용자 확인은 앱의 삭제 승인 화면이 받습니다."),
                "candidates": .array(candidates),
            ])
            return nil
        }
        if rankedPickPattern.search(state.userText) && decision == "choose"
            && response["stage"]?.stringValue != "meaning"  // a guess by similarity is always confirmed
            && (complete == nil || truthy(complete))  // an incomplete search keeps its stated limits
            && !actsWithoutReading(state.userText)  // move/delete keep asking
        {
            // "가장 최근 계약서" names one file by date or size: the model picks among the
            // candidates by their sizes and dates, the user is not asked which one.
            state.rankedCandidates = candidatePaths
            state.findOverride = dumpsUnicode([
                "decision": "ranked",
                "note": .string(
                    "크기·날짜로 하나를 고르는 요청입니다. 사용자에게 묻지 말고 "
                        + "local__list_files(paths에 아래 후보 경로)로 크기·수정 시각을 보고 "
                        + "조건에 맞는 후보 하나를 골라 진행하세요. 다른 파일은 고르지 마세요."),
                "candidates": .array(candidates),
            ])
            return nil
        }
        if let spec = clarificationSpec(response) {
            let count = state.priorClarifications + 1
            if count > maxClarifications {
                let clues = state.conditions.joined(separator: "; ")
                let soFar = clues.isEmpty ? "" : " (지금까지 단서: \(clues))"
                return .error(
                    "clarification 최대 횟수를 초과했습니다. 다음 요청에는 폴더·파일명 일부·날짜 "
                        + "같은 구체적인 조건을 넣어 주세요\(soFar). 파일은 변경하지 않았습니다.")
            }
            let context = ClarificationContext(
                state.userText, spec.kind, spec.options, count, conditions: state.conditions,
                searchArguments: state.lastSearch, attachment: state.attachment ?? "")
            return .clarification(Clarification(spec.question, spec.options, context, labels: spec.labels))
        }
        if decision == "none" && state.searchRequired {
            if complete == .bool(false) {  // never claim a full search that was not
                let limits = (response["limits"]?.arrayValue ?? []).map { $0.stringValue ?? $0.dumps() }
                    .joined(separator: "; ")
                return .completed(Completed(
                    "검사한 범위에서는 일치하는 파일을 찾지 못했습니다. (\(limits)) 파일명 일부나 폴더를 알려주세요."))
            }
            return .completed(Completed("요청과 일치하는 파일을 찾지 못했습니다. 파일명을 더 구체적으로 알려주세요."))
        }
        if decision == "single" && state.singleSearchCandidate == nil {
            let found = candidatePaths.first ?? ""
            var validated: URL?
            do {
                validated = try await gateway.validateResultPath(found)
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                validated = nil
            }
            if validated?.path != found {
                return .error("검색 결과에 허용 경로 밖이거나 유효하지 않은 파일이 있습니다.")
            }
            if state.searchRequired || wantsContent(state.userText) {
                state.singleSearchCandidate = found
                state.candidateRead = actsWithoutReading(state.userText)
            }
        }
        return nil
    }

    /// Several files, each named in full by the user, are the targets: there is nothing to
    /// choose. (Two files of one name in different folders still make the user choose.)
    static func namedCandidates(_ state: LoopState, _ response: JSON) -> String? {
        if response["decision"]?.stringValue != "choose" || response["complete"] == .bool(false) { return nil }
        let paths = (response["candidates"]?.arrayValue ?? []).map { $0["path"]?.stringValue ?? "" }
        let names = paths.map(pathName)
        if Set(names).count != names.count || !names.allSatisfy({ state.userText.contains($0) }) { return nil }
        return dumpsUnicode([
            "decision": "named",
            "note": "사용자가 이 파일들을 직접 지정했습니다. 묻지 말고 모두 처리하세요.",
            "paths": .array(paths.map(JSON.string)),
        ])
    }

    func approvalRequired(_ state: LoopState, _ prepared: PreparedWrite) -> ApprovalRequired {
        let context = ApprovalContext(
            originalRequest: state.userText, messages: state.messages, iteration: state.iteration,
            toolCalls: state.toolCalls, pendingCall: state.toolCalls[state.nextCallIndex],
            nextCallIndex: state.nextCallIndex, tools: state.tools, preparedWrite: prepared,
            previousSignature: state.previousSignature, singleSearchCandidate: state.singleSearchCandidate,
            readOnlyCandidateRequest: state.readOnlyCandidateRequest, searchRequired: state.searchRequired,
            searchCompleted: state.searchCompleted, candidateRead: state.candidateRead, userURLs: state.userURLs,
            webAttempted: state.webAttempted, webNavigationSucceeded: state.webNavigationSucceeded,
            webSnapshotSucceeded: state.webSnapshotSucceeded, webFailed: state.webFailed,
            webSaveRequired: state.webSaveRequired, webWriteCompleted: state.webWriteCompleted,
            priorClarifications: state.priorClarifications, modelCalls: state.modelCalls,
            activeAppName: state.activeAppName, activeAppOnly: state.activeAppOnly,
            writeCompleted: state.writeCompleted, exposedToolNames: state.exposedToolNames,
            partialRead: state.partialRead, conditions: state.conditions, lastSearch: state.lastSearch,
            changedPaths: state.changedPaths, attachment: state.attachment, readPaths: state.readPaths,
            foundPaths: state.foundPaths, groupPaths: state.groupPaths, callCounts: state.callCounts,
            recoveries: state.recoveries, escalated: state.escalated, runSeconds: clock() - state.started)
        return ApprovalRequired(
            toolName: prepared.toolName, targetPath: prepared.targetPath, operation: prepared.operation,
            contentChars: prepared.contentChars, preview: prepared.preview,
            previewTruncated: prepared.previewTruncated, context: context, targets: prepared.targets,
            previews: prepared.previews)
    }

    func restoreState(_ context: ApprovalContext) -> LoopState {
        LoopState(
            userText: context.originalRequest, messages: context.messages, iteration: context.iteration,
            tools: context.tools, toolCalls: context.toolCalls, nextCallIndex: context.nextCallIndex,
            previousSignature: context.previousSignature, singleSearchCandidate: context.singleSearchCandidate,
            readOnlyCandidateRequest: context.readOnlyCandidateRequest, searchRequired: context.searchRequired,
            searchCompleted: context.searchCompleted, candidateRead: context.candidateRead,
            userURLs: context.userURLs, webAttempted: context.webAttempted,
            webNavigationSucceeded: context.webNavigationSucceeded,
            webSnapshotSucceeded: context.webSnapshotSucceeded, webFailed: context.webFailed,
            webSaveRequired: context.webSaveRequired, webWriteCompleted: context.webWriteCompleted,
            webFlowCorrections: 0, priorClarifications: context.priorClarifications,
            modelCalls: context.modelCalls, activeAppName: context.activeAppName,
            activeAppOnly: context.activeAppOnly, writeCompleted: context.writeCompleted,
            exposedToolNames: context.exposedToolNames, partialRead: context.partialRead,
            conditions: context.conditions, lastSearch: context.lastSearch, changedPaths: context.changedPaths,
            attachment: context.attachment, readPaths: context.readPaths, foundPaths: context.foundPaths,
            groupPaths: context.groupPaths, callCounts: context.callCounts, recoveries: context.recoveries,
            escalated: context.escalated,
            started: clock() - context.runSeconds)  // the approval wait does not count
    }

    func appendToolError(_ state: LoopState, _ call: ToolCall, _ errorText: String) {
        emit("[TOOL-ERROR] iteration=\(state.iteration) name=\(call.name)")
        let text = errorText + missingPathHint(call, config.allowedDirectories)
        state.messages.append(["role": "tool", "tool_call_id": .string(call.id), "content": .string(text)])
    }
}

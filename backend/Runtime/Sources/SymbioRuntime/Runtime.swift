import Foundation
import SymbioCore
import SymbioPlanner

/// Python `runtime.py`: single-flight runtime state machine for text and voice commands.

public enum RuntimeState: String, CaseIterable, Sendable {
    case idle = "IDLE"
    case recording = "RECORDING"
    case transcribing = "TRANSCRIBING"
    case planning = "PLANNING"
    case executingTool = "EXECUTING_TOOL"
    case waitingUserInput = "WAITING_USER_INPUT"
    case waitingApproval = "WAITING_APPROVAL"
    case resumingPlan = "RESUMING_PLAN"
    case done = "DONE"
    case error = "ERROR"

    /// Python `state.name`.
    public var name: String { rawValue }
}

public let stateTransitions: [RuntimeState: Set<RuntimeState>] = [
    .idle: [.recording, .planning, .done, .error],
    .recording: [.transcribing, .idle, .error],
    .transcribing: [.planning, .error],
    .planning: [.executingTool, .waitingUserInput, .waitingApproval, .done, .error],
    .executingTool: [.planning, .error],
    .waitingUserInput: [.resumingPlan, .done, .error],
    .waitingApproval: [.resumingPlan, .done, .error],
    .resumingPlan: [.planning, .executingTool, .waitingUserInput, .waitingApproval, .done, .error],
    .done: [.idle, .error],
    .error: [.idle],
]

public struct StateTransition: Equatable, Sendable {
    public let previous: RuntimeState
    public let current: RuntimeState
    public let reason: String
    public init(_ previous: RuntimeState, _ current: RuntimeState, _ reason: String) {
        self.previous = previous
        self.current = current
        self.reason = reason
    }
}

public struct Busy: Equatable, Sendable {
    public let message: String
    public let state: RuntimeState
    public init(_ message: String, _ state: RuntimeState) {
        self.message = message
        self.state = state
    }
}

/// Python `RuntimeResult = PlannerOutcome | Busy`.
public enum RuntimeResult: Equatable, Sendable {
    case outcome(PlannerOutcome)
    case busy(Busy)
}

public struct RuntimeTransitionError: Error, Equatable, Sendable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

/// `/feedback 내용` is a note to the feedback loop, not a command.
public let feedbackCommand = try! NSRegularExpression(
    pattern: #"\A\s*/feedback(?:\s+|$)"#, options: [.caseInsensitive])

/// Python `FEEDBACK_COMMAND.match(text)`.
public func isFeedbackCommand(_ text: String) -> Bool {
    feedbackCommand.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil
}

/// A follow-up counts only for this long after the answer it follows.
public let memorySeconds: Double = 600

/// What the controller needs from the planner (Python duck typing). `Planner` conforms.
public protocol RuntimePlanner: AnyObject, Sendable {
    var config: Config { get }
    func setEventObserver(_ observer: PlannerEventObserver?)
    func run(_ userText: String, attachment: String?, previous: Exchange?) async throws -> PlannerOutcome
    func resume(_ context: ClarificationContext, _ selection: String) async throws -> PlannerOutcome
    func approve(_ context: ApprovalContext) async throws -> PlannerOutcome
    func reject(_ context: ApprovalContext) -> Completed
    func undoLastWrite() async throws -> PlannerOutcome
}

extension Planner: RuntimePlanner {}

/// The feedback loop as the controller uses it (Python `feedback=` duck typing). The App wires
/// the Feedback package to this; `cancelled(reason:)` is Python `record("cancelled", reason=...)`.
public protocol RuntimeFeedback: Sendable {
    func userInput(_ text: String, newRun: Bool)
    func decision(_ action: String, tool: String?)
    func outcome(_ outcome: PlannerOutcome)
    func exception(_ error: any Error)
    func state(_ previous: String, _ current: String, _ reason: String)
    func userReport(_ text: String) -> Bool
    func cancelled(reason: String)
    func runFinished()
}

/// Own command state, pending clarification, and single-flight execution.
// ponytail: one lock around plain fields (like Planner); observers run outside it.
public final class RuntimeController: @unchecked Sendable {
    public let planner: any RuntimePlanner
    let emit: @Sendable (String) -> Void
    let stateObserver: (@Sendable (StateTransition) -> Void)?
    let plannerEventObserver: PlannerEventObserver?
    let feedback: (any RuntimeFeedback)?
    let clock: @Sendable () -> Double

    private let lock = NSRecursiveLock()
    private var _state = RuntimeState.idle
    private var history: [StateTransition] = []
    private var _pending: Clarification?
    private var _pendingApproval: ApprovalRequired?
    private var inFlight = false
    private var _attachment: String?  // one dropped file, for the next command only
    private var _previous: (exchange: Exchange, at: Double)?
    private var request: String?  // the command now being worked on

    public init(
        _ planner: any RuntimePlanner, emit: (@Sendable (String) -> Void)? = nil,
        stateObserver: (@Sendable (StateTransition) -> Void)? = nil,
        plannerEventObserver: PlannerEventObserver? = nil, feedback: (any RuntimeFeedback)? = nil,
        clock: @escaping @Sendable () -> Double = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.planner = planner
        self.emit = emit ?? { _ in }
        self.stateObserver = stateObserver
        self.plannerEventObserver = plannerEventObserver
        self.feedback = feedback
        self.clock = clock
        planner.setEventObserver { [weak self] in self?.onPlannerEvent($0) }
    }

    public internal(set) var state: RuntimeState {
        get { lock.withLock { _state } }
        set { lock.withLock { _state = newValue } }
    }
    public var transitionHistory: [StateTransition] { lock.withLock { history } }
    public var pending: Clarification? { lock.withLock { _pending } }
    public var pendingApproval: ApprovalRequired? { lock.withLock { _pendingApproval } }
    public var attachment: String? { lock.withLock { _attachment } }
    var previous: Exchange? { lock.withLock { _previous?.exchange } }

    /// Make one file inside the allowed folders the target of the next command.
    /// Throws AttachmentError (and keeps any earlier attachment) when it is refused.
    @discardableResult
    public func attach(_ path: String) throws(AttachmentError) -> String {
        lock.lock()
        defer { lock.unlock() }
        if inFlight || _state != .idle { throw AttachmentError("작업이 끝난 뒤에 파일을 놓아 주세요.") }
        let checked = try checkAttachment(.string(path), roots: planner.config.allowedDirectories)
        _attachment = checked
        return checked
    }

    public func detach() { lock.withLock { _attachment = nil } }

    /// Forget the follow-up context so the next command starts a new conversation.
    public func resetConversation() -> Bool {
        lock.withLock {
            if inFlight || _state != .idle { return false }
            _previous = nil
            _attachment = nil
            return true
        }
    }

    public func submit(_ text: String) async throws -> RuntimeResult {
        enum Start { case busy(Busy), report, run(answer: Bool) }
        let start: Start = lock.withLock {
            if inFlight || ![.idle, .waitingUserInput].contains(_state) {
                return .busy(Busy("다른 명령을 처리 중입니다.", _state))
            }
            if isFeedbackCommand(text) { return .report }
            inFlight = true
            return .run(answer: _state == .waitingUserInput)
        }
        let answer: Bool
        switch start {
        case let .busy(busy): return .busy(busy)
        case .report: return .outcome(report(feedbackCommand.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "")))
        case let .run(value): answer = value
        }
        defer { lock.withLock { inFlight = false } }
        do {
            feedback?.userInput(text, newRun: !answer)
            if answer { return .outcome(try await submitClarificationAnswer(text)) }
            try transition(.planning, "command_started")
            let (attachment, previous): (String?, Exchange?) = lock.withLock {
                defer { _attachment = nil }  // one command, then gone
                request = text
                return (_attachment, recentExchange())
            }
            let outcome = try await planner.run(text, attachment: attachment, previous: previous)
            return .outcome(try finish(outcome))
        } catch {
            try fail(error, clearClarification: true)
        }
    }

    /// `/feedback 내용`: goes to the feedback loop about the last answer, never the planner.
    /// The conversation, attachment and state stay as they were.
    private func report(_ text: String) -> PlannerOutcome {
        if state != .idle { return .error("질문에 먼저 답한 뒤 /feedback을 보내 주세요.") }
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .error("/feedback 뒤에 고칠 점을 적어 주세요. 예: /feedback 결과가 너무 많아")
        }
        guard let feedback, feedback.userReport(text) else {
            return .error("피드백을 저장하지 못했어요. 피드백 기록이 켜져 있는지 확인해 주세요.")
        }
        return .completed(Completed("피드백을 받았어요. 원인을 분석해 반영할게요."))
    }

    private enum Claim { case busy(Busy), claimed(ApprovalRequired) }

    /// Claims the single flight for an approval decision.
    private func claimApproval() throws -> Claim {
        try lock.withLock {
            if inFlight { return .busy(Busy("다른 명령을 처리 중입니다.", _state)) }
            guard _state == .waitingApproval, let pending = _pendingApproval else {
                throw RuntimeTransitionError("대기 중인 승인이 없습니다.")
            }
            inFlight = true
            return .claimed(pending)
        }
    }

    public func approvePending() async throws -> RuntimeResult {
        let pending: ApprovalRequired
        switch try claimApproval() {
        case let .busy(busy): return .busy(busy)
        case let .claimed(value): pending = value
        }
        defer { lock.withLock { inFlight = false } }
        do {
            feedback?.decision("approve", tool: pending.toolName)
            try transition(.resumingPlan, "approval_granted")
            return .outcome(try finish(try await planner.approve(pending.context)))
        } catch {
            try fail(error, clearApproval: true)
        }
    }

    public func rejectPending() async throws -> RuntimeResult {
        let pending: ApprovalRequired
        switch try claimApproval() {
        case let .busy(busy): return .busy(busy)
        case let .claimed(value): pending = value
        }
        defer { lock.withLock { inFlight = false } }
        do {
            feedback?.decision("reject", tool: pending.toolName)
            return .outcome(try finish(.completed(planner.reject(pending.context)), remember: false))
        } catch {
            try fail(error, clearApproval: true)
        }
    }

    public func undoLastWrite() async throws -> RuntimeResult {
        let busy: Busy? = lock.withLock {
            if inFlight || _state != .idle { return Busy("다른 명령을 처리 중입니다.", _state) }
            inFlight = true
            return nil
        }
        if let busy { return .busy(busy) }
        defer { lock.withLock { inFlight = false } }
        do {
            feedback?.decision("undo", tool: nil)
            return .outcome(try finish(try await planner.undoLastWrite(), remember: false))
        } catch {
            try fail(error)
        }
    }

    /// Python's `except BaseException` arm: record, drop what was pending, go back to idle, re-raise.
    private func fail(_ error: any Error, clearClarification: Bool = false, clearApproval: Bool = false) throws -> Never {
        feedback?.exception(error)
        lock.withLock {
            if clearClarification { _pending = nil }
            if clearApproval || clearClarification { _pendingApproval = nil }
        }
        if state != .error { try transition(.error, "exception") }
        try transition(.idle, "error_observed")
        throw error
    }

    private func submitClarificationAnswer(_ answer: String) async throws -> PlannerOutcome {
        guard let pending else { throw RuntimeTransitionError("pending clarification이 없습니다.") }

        let normalized = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        if ["취소", "cancel", "c"].contains(normalized.lowercased()) {
            feedback?.cancelled(reason: "clarification")  // the user gave up: feedback rule R10 counts it
            return try finish(.completed(Completed("요청을 취소했습니다. 파일은 변경하지 않았습니다.")), remember: false)
        }

        let typed = pending.context.kind == "free_text"
        let narrowing = pending.context.kind == "narrow_search"
        // Narrowing takes free text, so there an option is picked only as "N번"; a typed
        // question has no options at all and every non-empty answer is the answer.
        let number = narrowing
            ? (try? /(\d+)\s*번/.wholeMatch(in: normalized))?.output.1
            : (try? /(\d+)\s*번?/.wholeMatch(in: normalized))?.output.1
        let index = !typed ? (number.flatMap { Int($0) }).map { $0 - 1 } ?? -1 : -1
        let valid = pending.options.indices.contains(index)
        if (narrowing || typed) && !valid && !normalized.isEmpty {
            try transition(.resumingPlan, "clarification_narrowed")
            return try finish(try await planner.resume(pending.context, normalized))
        }
        if !valid {
            let count = pending.context.clarificationCount + 1
            if count > maxClarifications {
                return try finish(.error("clarification 최대 횟수를 초과해 파일을 변경하지 않았습니다."))
            }
            let old = pending.context
            let context = ClarificationContext(
                old.originalRequest, old.kind, old.options, count,
                conditions: old.conditions, searchArguments: old.searchArguments, attachment: old.attachment)
            let retry = Clarification(
                "올바른 선택지 번호를 입력하거나 취소를 입력해 주세요.", pending.options, context, labels: pending.labels)
            lock.withLock { _pending = retry }
            return .clarification(retry)
        }

        try transition(.resumingPlan, "clarification_selected")
        return try finish(try await planner.resume(pending.context, pending.options[index]))
    }

    /// The previous answered command, if it was a moment ago. Called under `lock`.
    private func recentExchange() -> Exchange? {
        guard let previous = _previous else { return nil }
        return clock() - previous.at <= memorySeconds ? previous.exchange : nil
    }

    /// Settle an outcome. Only a real answer is remembered, not a cancel, reject or undo.
    private func finish(_ outcome: PlannerOutcome, remember: Bool = true) throws -> PlannerOutcome {
        feedback?.outcome(outcome)
        switch outcome {
        case let .clarification(question):
            lock.withLock { _pending = question; _pendingApproval = nil }
            try transition(.waitingUserInput, "clarification_required")
        case let .approvalRequired(approval):
            lock.withLock { _pending = nil; _pendingApproval = approval }
            try transition(.waitingApproval, "approval_required")
        case let .completed(done):
            lock.withLock {
                _pending = nil
                _pendingApproval = nil
                if remember, let request, !request.isEmpty {
                    var seen = Set<String>()
                    let files = (done.changedPaths + done.readPaths).filter { seen.insert($0).inserted }
                    _previous = (Exchange(request, done.text, Array(files.prefix(memoryMaxFiles))), clock())
                }
            }
            try transition(.done, "completed")
            try transition(.idle, "result_observed")
        case .error:
            lock.withLock { _pending = nil; _pendingApproval = nil }
            try transition(.error, "planner_error")
            try transition(.idle, "error_observed")
        }
        return outcome
    }

    private func onPlannerEvent(_ event: PlannerEvent) {
        // ponytail: an impossible transition here is dropped, not raised: the observer cannot throw.
        switch event.type {
        case .planningStarted:
            if [.executingTool, .resumingPlan].contains(state) {
                try? transition(.planning, "planning_iteration_\(event.iteration)")
            }
        case .toolStarted: try? transition(.executingTool, "tool_started:\(event.toolName ?? "unknown")")
        case .toolFinished: try? transition(.planning, "tool_finished:\(event.toolName ?? "unknown")")
        case .recovering: break
        }
        plannerEventObserver?(event)
    }

    private func transition(_ target: RuntimeState, _ reason: String) throws {
        let change: StateTransition? = try lock.withLock {
            if target == _state { return nil }
            guard stateTransitions[_state]?.contains(target) == true else {
                throw RuntimeTransitionError("허용되지 않은 상태 전이: \(_state.name) -> \(target.name)")
            }
            let change = StateTransition(_state, target, reason)
            _state = target
            history.append(change)
            return change
        }
        guard let change else { return }
        emit("[STATE] \(change.previous.name) -> \(change.current.name) reason=\(change.reason)")
        stateObserver?(change)
        if let feedback {
            feedback.state(change.previous.name, target.name, reason)
            if target == .idle { feedback.runFinished() }
        }
    }
}

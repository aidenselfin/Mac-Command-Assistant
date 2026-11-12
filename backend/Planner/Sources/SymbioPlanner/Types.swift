import Foundation
import SymbioCore
import SymbioGuard

/// Python `planner.py` result and protocol types (everything before `class Planner`).

/// Python `PlannerError` and its subclasses `ModelFormatError` / `ToolLoopLimitError`.
/// `except PlannerError` = catch any; `except ModelFormatError` = check `kind == .modelFormat`.
public struct PlannerError: Error, Equatable, Sendable, CustomStringConvertible {
    public enum Kind: Sendable { case general, modelFormat, toolLoopLimit }
    public let message: String
    public let kind: Kind
    public init(_ message: String, kind: Kind = .general) {
        self.message = message
        self.kind = kind
    }
    /// Python `ModelFormatError(message)`: the loop tells the model what to fix instead of ending.
    public static func modelFormat(_ message: String) -> PlannerError { PlannerError(message, kind: .modelFormat) }
    public var description: String { message }
}

/// Python `PlannerTiming`: shared by reference with whoever passed it in.
public final class PlannerTiming: @unchecked Sendable {  // fields are behind `lock`
    private let lock = NSLock()
    private var llm: [Double]
    private var read: Double
    private var write: Double

    public init(llmRoundMs: [Double] = [], mcpReadMs: Double = 0, mcpWriteMs: Double = 0) {
        llm = llmRoundMs
        read = mcpReadMs
        write = mcpWriteMs
    }

    public static func empty() -> PlannerTiming { PlannerTiming() }

    public var llmRoundMs: [Double] {
        get { lock.withLock { llm } }
        set { lock.withLock { llm = newValue } }
    }
    public var mcpReadMs: Double {
        get { lock.withLock { read } }
        set { lock.withLock { read = newValue } }
    }
    public var mcpWriteMs: Double {
        get { lock.withLock { write } }
        set { lock.withLock { write = newValue } }
    }

    public func appendLLMRound(_ ms: Double) { lock.withLock { llm.append(ms) } }

    public func reset() {
        lock.withLock { llm.removeAll(); read = 0; write = 0 }
    }

    public func recordTool(_ name: String, _ elapsedMs: Double) {
        lock.withLock {
            if name == filesystemReadTool { read += elapsedMs } else if name == filesystemWriteTool { write += elapsedMs }
        }
    }
}

public struct Completed: Sendable, CustomStringConvertible {
    public let text: String
    /// Files this command created or changed, exactly as the app wrote them. Not part of equality.
    public var changedPaths: [String]
    /// Files it read, for the next command's "that file". Not part of equality.
    public var readPaths: [String]
    /// The one file a search settled on (drag chip). Not part of equality.
    public var foundPaths: [String]

    public init(_ text: String, changedPaths: [String] = [], readPaths: [String] = [], foundPaths: [String] = []) {
        self.text = text
        self.changedPaths = changedPaths
        self.readPaths = readPaths
        self.foundPaths = foundPaths
    }
    public var description: String { "Completed(text: \(text.debugDescription))" }
}

extension Completed: Equatable {
    public static func == (lhs: Completed, rhs: Completed) -> Bool { lhs.text == rhs.text }
}

public struct ClarificationContext: Equatable, Sendable {
    public let originalRequest: String
    public let kind: String
    public let options: [String]
    public let clarificationCount: Int
    /// What the user narrowed so far and the last search, so another round adds to them.
    public var conditions: [String]
    public var searchArguments: String
    public var attachment: String  // the dropped file this command is about, if any

    public init(
        _ originalRequest: String, _ kind: String, _ options: [String], _ clarificationCount: Int,
        conditions: [String] = [], searchArguments: String = "", attachment: String = ""
    ) {
        self.originalRequest = originalRequest
        self.kind = kind
        self.options = options
        self.clarificationCount = clarificationCount
        self.conditions = conditions
        self.searchArguments = searchArguments
        self.attachment = attachment
    }
}

/// `arguments` is the raw JSON text from the model; it is kept out of `description`.
public struct ToolCall: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let id: String
    public let name: String
    public let arguments: String
    public init(_ id: String, _ name: String, _ arguments: String = "") {
        self.id = id
        self.name = name
        self.arguments = arguments
    }
    public var description: String { "ToolCall(id: \(id.debugDescription), name: \(name.debugDescription))" }
    public var debugDescription: String { description }
}

/// Python `previous_signature`: `(call.name, json.dumps(arguments, sort_keys=True))`.
public struct ToolSignature: Hashable, Sendable {
    public let name: String
    public let arguments: String
    public init(_ name: String, _ arguments: String) {
        self.name = name
        self.arguments = arguments
    }
}

/// Everything the loop needs to resume after the user approves a write. Built only by Planner;
/// `description` leaves out the request, messages, calls and the prepared content.
public struct ApprovalContext: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let originalRequest: String
    public let messages: [JSON]
    public let iteration: Int
    public let toolCalls: [ToolCall]
    public let pendingCall: ToolCall
    public let nextCallIndex: Int
    public let tools: [ToolDefinition]
    public let preparedWrite: PreparedWrite
    public let previousSignature: ToolSignature?
    public let singleSearchCandidate: String?
    public let readOnlyCandidateRequest: Bool
    public let searchRequired: Bool
    public let searchCompleted: Bool
    public let candidateRead: Bool
    public let userURLs: [String]
    public let webAttempted: Bool
    public let webNavigationSucceeded: Bool
    public let webSnapshotSucceeded: Bool
    public let webFailed: Bool
    public let webSaveRequired: Bool
    public let webWriteCompleted: Bool
    public let priorClarifications: Int
    public let modelCalls: Int
    public let activeAppName: String?
    public let activeAppOnly: Bool
    public let writeCompleted: Bool
    public var exposedToolNames: Set<String>? = nil
    public var partialRead: String? = nil
    public var conditions: [String] = []
    public var lastSearch: String = ""
    public var changedPaths: [String] = []
    public var attachment: String? = nil
    public var readPaths: [String] = []
    public var foundPaths: [String] = []
    public var groupPaths: [String] = []
    public var callCounts: [String: Int] = [:]
    public var recoveries: Int = 0
    public var escalated: Bool = false
    public var runSeconds: Double = 0  // loop time spent before this approval

    public var digest: String { preparedWrite.digest }
    public var nonce: String { preparedWrite.nonce }

    public var description: String {
        "ApprovalContext(iteration: \(iteration), nextCallIndex: \(nextCallIndex), "
            + "modelCalls: \(modelCalls), writeCompleted: \(writeCompleted))"
    }
    public var debugDescription: String { description }
}

public struct ApprovalRequired: Equatable, Sendable, CustomStringConvertible, CustomDebugStringConvertible {
    public let toolName: String
    public let targetPath: String
    public let operation: String
    public let contentChars: Int
    public let preview: String
    public let previewTruncated: Bool
    public let context: ApprovalContext
    public var targets: [String] = []
    public var previews: [String] = []  // user screen only

    public var description: String {
        "ApprovalRequired(toolName: \(toolName), targetPath: \(targetPath), operation: \(operation), "
            + "contentChars: \(contentChars), preview: \(preview.debugDescription), "
            + "previewTruncated: \(previewTruncated), targets: \(targets))"
    }
    public var debugDescription: String { description }
}

public struct Clarification: Equatable, Sendable {
    public let question: String
    public let options: [String]
    public let context: ClarificationContext
    public var labels: [String] = []  // display text per option; the option itself stays the value

    public init(_ question: String, _ options: [String], _ context: ClarificationContext, labels: [String] = []) {
        self.question = question
        self.options = options
        self.context = context
        self.labels = labels
    }

    public var shownOptions: [String] { labels.isEmpty ? options : labels }
}

/// Python `PlannerOutcome = Completed | Clarification | ApprovalRequired | Error`.
public enum PlannerOutcome: Equatable, Sendable {
    case completed(Completed)
    case clarification(Clarification)
    case approvalRequired(ApprovalRequired)
    case error(String)  // Python `Error(message)`
}

/// Provider-neutral representation of the adapter's virtual tool call.
public struct ClarificationRequest: Equatable, Sendable {
    public let question: String
    public let options: [String]
    public let kind: String
    public let callID: String
    public init(_ question: String, _ options: [String], _ kind: String, _ callID: String = "clarification") {
        self.question = question
        self.options = options
        self.kind = kind
        self.callID = callID
    }
}

public struct ModelTurn: Equatable, Sendable {
    public let text: String?
    public let toolCalls: [ToolCall]
    public let clarification: ClarificationRequest?
    public init(_ text: String?, _ toolCalls: [ToolCall], _ clarification: ClarificationRequest? = nil) {
        self.text = text
        self.toolCalls = toolCalls
        self.clarification = clarification
    }
}

/// Python `ModelClient` protocol. Throws `PlannerError` (or `CancellationError`).
public protocol ModelClient: Sendable {
    func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn
}

extension ModelClient {
    public func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool = true
    ) async throws -> ModelTurn {
        try await generate(messages, tools, allowTools: allowTools, allowClarification: true)
    }
}

public enum PlannerEventType: Sendable {
    case planningStarted
    case toolStarted
    case toolFinished
    case recovering  // toolName carries the status label shown while it tries again
}

public struct PlannerEvent: Equatable, Sendable {
    public let type: PlannerEventType
    public let iteration: Int
    public let toolName: String?
    public init(_ type: PlannerEventType, _ iteration: Int, _ toolName: String? = nil) {
        self.type = type
        self.iteration = iteration
        self.toolName = toolName
    }
}

public typealias PlannerEventObserver = @Sendable (PlannerEvent) -> Void

/// The one command before this one, kept in memory so a follow-up can leave things out.
public struct Exchange: Equatable, Sendable {
    public let request: String
    public let reply: String
    public let files: [String]
    public init(_ request: String, _ reply: String, _ files: [String] = []) {
        self.request = request
        self.reply = reply
        self.files = files
    }
}

/// Python `_LoopState`: one command's mutable loop state, shared by reference like the dataclass.
final class LoopState {
    var userText: String
    var messages: [JSON]
    var iteration: Int
    var tools: [ToolDefinition]
    var toolCalls: [ToolCall]
    var nextCallIndex: Int
    var previousSignature: ToolSignature?
    var singleSearchCandidate: String?
    var readOnlyCandidateRequest: Bool
    var searchRequired: Bool
    var searchCompleted: Bool
    var candidateRead: Bool
    var userURLs: [String]
    var webAttempted: Bool
    var webNavigationSucceeded: Bool
    var webSnapshotSucceeded: Bool
    var webFailed: Bool
    var webSaveRequired: Bool
    var webWriteCompleted: Bool
    var webFlowCorrections: Int
    var priorClarifications: Int
    var modelCalls: Int
    var activeAppName: String?
    var activeAppOnly: Bool
    var writeCompleted: Bool
    /// Names offered on the model turn whose calls are being processed. Only these run.
    var exposedToolNames: Set<String>?
    var partialRead: String?  // a file was too big to read fully
    var conditions: [String]
    var lastSearch: String
    var changedPaths: [String]
    var attachment: String?
    var readPaths: [String]
    var foundPaths: [String]
    var groupPaths: [String]  // files local__group_files listed: grounded move sources
    var seenPaths: [String]
    var emptyRetries: Int  // times an empty final reply was sent back for a real answer
    var confirmNudges: Int  // times a stalled "shall I?" reply was turned into a tool call
    var findTried: Bool  // local__find_files has run in this command
    var findNone: Bool  // ...and found nothing
    var findOverride: String?  // what the model is told about the last search instead
    var rankedCandidates: [String]  // one of these must be read before answering
    var knownTarget: Bool  // an attached file or the previous command's files
    /// Recovery: how often each (tool, arguments, result) came back, fixes that failed, and
    /// whether the stronger model has taken over. Kept across an approval.
    var callCounts: [String: Int]
    var recoveries: Int
    var escalated: Bool
    var formatErrors: Int
    var started: Double  // loop start (clock), moved forward past approval waits

    init(
        userText: String = "", messages: [JSON] = [], iteration: Int = 0, tools: [ToolDefinition] = [],
        toolCalls: [ToolCall] = [], nextCallIndex: Int = 0, previousSignature: ToolSignature? = nil,
        singleSearchCandidate: String? = nil, readOnlyCandidateRequest: Bool = false,
        searchRequired: Bool = false, searchCompleted: Bool = false, candidateRead: Bool = false,
        userURLs: [String] = [], webAttempted: Bool = false, webNavigationSucceeded: Bool = false,
        webSnapshotSucceeded: Bool = false, webFailed: Bool = false, webSaveRequired: Bool = false,
        webWriteCompleted: Bool = false, webFlowCorrections: Int = 0, priorClarifications: Int = 0,
        modelCalls: Int = 0, activeAppName: String? = nil, activeAppOnly: Bool = false,
        writeCompleted: Bool = false, exposedToolNames: Set<String>? = nil, partialRead: String? = nil,
        conditions: [String] = [], lastSearch: String = "", changedPaths: [String] = [],
        attachment: String? = nil, readPaths: [String] = [], foundPaths: [String] = [],
        groupPaths: [String] = [], seenPaths: [String] = [], emptyRetries: Int = 0, confirmNudges: Int = 0,
        findTried: Bool = false, findNone: Bool = false, findOverride: String? = nil,
        rankedCandidates: [String] = [], knownTarget: Bool = false, callCounts: [String: Int] = [:],
        recoveries: Int = 0, escalated: Bool = false, formatErrors: Int = 0, started: Double = 0
    ) {
        self.userText = userText
        self.messages = messages
        self.iteration = iteration
        self.tools = tools
        self.toolCalls = toolCalls
        self.nextCallIndex = nextCallIndex
        self.previousSignature = previousSignature
        self.singleSearchCandidate = singleSearchCandidate
        self.readOnlyCandidateRequest = readOnlyCandidateRequest
        self.searchRequired = searchRequired
        self.searchCompleted = searchCompleted
        self.candidateRead = candidateRead
        self.userURLs = userURLs
        self.webAttempted = webAttempted
        self.webNavigationSucceeded = webNavigationSucceeded
        self.webSnapshotSucceeded = webSnapshotSucceeded
        self.webFailed = webFailed
        self.webSaveRequired = webSaveRequired
        self.webWriteCompleted = webWriteCompleted
        self.webFlowCorrections = webFlowCorrections
        self.priorClarifications = priorClarifications
        self.modelCalls = modelCalls
        self.activeAppName = activeAppName
        self.activeAppOnly = activeAppOnly
        self.writeCompleted = writeCompleted
        self.exposedToolNames = exposedToolNames
        self.partialRead = partialRead
        self.conditions = conditions
        self.lastSearch = lastSearch
        self.changedPaths = changedPaths
        self.attachment = attachment
        self.readPaths = readPaths
        self.foundPaths = foundPaths
        self.groupPaths = groupPaths
        self.seenPaths = seenPaths
        self.emptyRetries = emptyRetries
        self.confirmNudges = confirmNudges
        self.findTried = findTried
        self.findNone = findNone
        self.findOverride = findOverride
        self.rankedCandidates = rankedCandidates
        self.knownTarget = knownTarget
        self.callCounts = callCounts
        self.recoveries = recoveries
        self.escalated = escalated
        self.formatErrors = formatErrors
        self.started = started
    }
}

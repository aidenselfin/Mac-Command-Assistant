import Foundation
import SymbioCore
import SymbioFeedback
import SymbioFileOps
import SymbioMCP
import SymbioPlanner
import SymbioRuntime
import SymbioUndo

/// Python `voice_action/ui.py`, the parts the native bridge uses: messages, rendering and the
/// runtime worker. The Tk-only `UIPresenter`/`UIView`/`UIAdapter` are not ported.

/// Python `UIMessage(UIMessageType.X, value)`.
public enum UIMessage: Sendable {
    case state(RuntimeState)
    case plannerEvent(PlannerEvent)
    case outcome(RuntimeResult)
    case failure(String)
    case notice(String)
    case attachment(path: String?, refusal: String?)
    case stopped(Bool)  // true: close the window too
}

/// Python `type(exc).__name__`, used where a message names the error kind without its text.
public func typeName(_ error: any Error) -> String { String(describing: type(of: error)) }

/// Render only the bounded public fields of a runtime outcome.
public func renderOutcome(_ result: RuntimeResult) -> String {
    switch result {
    case let .busy(busy): return "[BUSY] \(busy.message)"
    case let .outcome(.completed(done)): return "[DONE] \(done.text)"
    case let .outcome(.error(message)): return "[ERROR] \(message)"
    case let .outcome(.clarification(question)):
        let options = question.shownOptions.enumerated().map { "  \($0.offset + 1). \($0.element)" }
        return "[QUESTION] \(question.question)\n" + options.joined(separator: "\n")
    case let .outcome(.approvalRequired(approval)):
        if approval.operation == "delete" {
            let targets = approval.targets.map { JSON.string($0).text }.joined(separator: "\n")
            return "[APPROVAL REQUIRED]\noperation: delete\n"
                + "삭제 대상: \(approval.targets.count)개 파일\n\(targets)\n"
                + "승인하면 위 파일들을 백업 후 삭제합니다. Undo로 일괄 복원할 수 있습니다."
        }
        if ["move", "copy", "mkdir"].contains(approval.operation) {
            return (["[APPROVAL REQUIRED]", "operation: \(approval.operation)"]
                + approval.targets.prefix(1).map { "source: \($0)" }
                + ["target: \(approval.targetPath)",
                   "승인 후 실행합니다. 기존 경로를 덮어쓰지 않으며 Undo로 되돌릴 수 있습니다."])
                .joined(separator: "\n")
        }
        let preview = String(String.UnicodeScalarView(approval.preview.unicodeScalars.prefix(200)))
        let suffix = approval.previewTruncated ? "…" : ""
        return [
            "[APPROVAL REQUIRED]", "tool: \(approval.toolName)", "target: \(approval.targetPath)",
            "operation: \(approval.operation)", "content_chars: \(approval.contentChars)",
            "preview: \(preview)\(suffix)",
        ].joined(separator: "\n")
    }
}

/// A path as shown for approval: control characters (newline, tab, ...) are escaped so one file
/// name can't look like two rows. Only the display changes, never the path used.
public func displayPath(_ path: String) -> String {
    var out = ""
    for scalar in path.unicodeScalars {
        if isPrintable(scalar) { out.unicodeScalars.append(scalar); continue }
        // Python `char.encode("unicode_escape")`
        switch scalar.value {
        case 0x09: out += "\\t"
        case 0x0A: out += "\\n"
        case 0x0D: out += "\\r"
        case ..<0x100: out += String(format: "\\x%02x", scalar.value)
        case ..<0x10000: out += String(format: "\\u%04x", scalar.value)
        default: out += String(format: "\\U%08x", scalar.value)
        }
    }
    return out
}

/// Python `str.isprintable` for one code point.
func isPrintable(_ scalar: Unicode.Scalar) -> Bool {
    if scalar == " " { return true }
    switch scalar.properties.generalCategory {
    case .control, .format, .surrogate, .privateUse, .unassigned, .lineSeparator, .paragraphSeparator,
         .spaceSeparator:
        return false
    default: return true
    }
}

// MARK: - plumbing

/// Python `queue.Queue`: put from any thread, blocking get with an optional timeout.
public final class BlockingQueue<Element: Sendable>: @unchecked Sendable {  // `items` behind `condition`
    private let condition = NSCondition()
    private var items: [Element] = []
    public init() {}

    public func put(_ item: Element) {
        condition.withLock {
            items.append(item)
            condition.signal()
        }
    }

    /// nil when nothing arrived within `timeout` seconds.
    public func get(timeout: Double? = nil) -> Element? {
        condition.withLock {
            let deadline = timeout.map { Date(timeIntervalSinceNow: $0) }
            while items.isEmpty {
                if let deadline {
                    if !condition.wait(until: deadline) && items.isEmpty { return nil }
                } else {
                    condition.wait()
                }
            }
            return items.removeFirst()
        }
    }
}

/// Python `asyncio.Event`: once set, every waiter goes on.
final class Flag: @unchecked Sendable {  // fields behind `lock`
    private let lock = NSLock()
    private var set = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    var isSet: Bool { lock.withLock { set } }

    func fire() {
        let waiting: [CheckedContinuation<Void, Never>] = lock.withLock {
            if set { return [] }
            set = true
            defer { waiters = [] }
            return waiters
        }
        waiting.forEach { $0.resume() }
    }

    func wait() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let now = lock.withLock {
                if set { return true }
                waiters.append(continuation)
                return false
            }
            if now { continuation.resume() }
        }
    }
}

/// What the worker needs from its MCP layer: `MCPManager`, or a fake in tests.
public protocol RuntimeManager: ToolGateway {
    func connect() async throws
    func close() async
}

extension MCPManager: RuntimeManager {}

/// The feedback loop as RuntimeController uses it (Python passes the loop itself).
public struct FeedbackAdapter: RuntimeFeedback {
    public let loop: FeedbackLoop
    public init(_ loop: FeedbackLoop) { self.loop = loop }
    public func userInput(_ text: String, newRun: Bool) { loop.userInput(text, newRun: newRun) }
    public func decision(_ action: String, tool: String?) { loop.decision(action, tool: tool) }
    public func outcome(_ outcome: PlannerOutcome) { loop.outcome(outcome) }
    public func exception(_ error: any Error) { loop.exception(error) }
    public func state(_ previous: String, _ current: String, _ reason: String) {
        loop.state(previous: previous, current: current, reason: reason)
    }
    public func userReport(_ text: String) -> Bool { loop.userReport(text) }
    public func cancelled(reason: String) { loop.record("cancelled", ["reason": .string(reason)]) }
    public func runFinished() { _ = loop.runFinished() }
}

/// Where the filesystem MCP server lives: `<project>/node_modules/...`. The app starts the backend
/// with the project (runtime.json `project`) as its working directory.
public func serverEntrypoint(project: URL) -> URL {
    project.appendingPathComponent("node_modules/@modelcontextprotocol/server-filesystem/dist/index.js")
}

/// Python `_manager_factory` / main `_build_mcp_manager`: only the filesystem client.
public func buildMCPManager(_ config: Config, project: URL? = nil) throws -> MCPManager {
    let project = project ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
    let client = try MCPClient(
        allowedDirectories: config.allowedDirectories, timeoutSeconds: config.mcpTimeoutSeconds,
        maxResultChars: config.maxToolResultChars, allowedTools: supportedTools,
        entrypoint: serverEntrypoint(project: project))
    return try MCPManager([("filesystem", client)])
}

/// Python `UndoJournal.default(...)`, which also builds the delete and move/copy journals;
/// nil when any of them cannot start.
public func buildUndoJournal(_ config: Config) -> UndoJournal? {
    guard let journal = try? UndoJournal.default(allowedRoots: config.allowedDirectories),
          (try? FileOperationJournal(journal)) != nil  // attaches itself and a DeletionJournal
    else { return nil }
    return journal
}

/// The worker as the bridge drives it (Python `AsyncRuntimeWorker` duck typing; tests fake it).
public protocol BridgeWorker: AnyObject, Sendable {
    func start()
    func submit(_ text: String) -> Bool
    func approvePending() -> Bool
    func rejectPending() -> Bool
    func undoLastWrite() -> Bool
    func attach(_ path: String) -> Bool
    func detach()
    func resetConversation() -> Bool
    func cancelActive() -> Bool
    func shutdown() async
}

// MARK: - worker

/// Own the MCP connection, the models and one runtime controller; report through `send`.
/// Python ran one asyncio loop on its own thread; here every step is a Swift task.
public final class AsyncRuntimeWorker: BridgeWorker, @unchecked Sendable {  // fields behind `lock`
    public typealias ManagerFactory = @Sendable (Config) throws -> any RuntimeManager
    public typealias ModelFactory = @Sendable (OpenAIClient, String) -> any ModelClient
    public typealias FeedbackFactory = @Sendable (Config, @escaping @Sendable (String) -> Void) -> FeedbackLoop?
    public typealias JournalFactory = @Sendable (Config) -> UndoJournal?

    public let config: Config
    let send: @Sendable (UIMessage) -> Void
    let allowLocalWeb: Bool
    let managerFactory: ManagerFactory
    let modelFactory: ModelFactory
    let feedbackFactory: FeedbackFactory
    let journalFactory: JournalFactory

    private let lock = NSLock()
    private var lifecycle: Task<Void, Never>?
    private var controller: RuntimeController?
    private var active: (id: UUID, task: Task<Void, Never>)?
    private var ready = false
    private var preShutdown = false
    private var closeAfterStop = false
    private let shutdownFlag = Flag()

    public init(
        _ config: Config, send: @escaping @Sendable (UIMessage) -> Void, allowLocalWeb: Bool = false,
        managerFactory: @escaping ManagerFactory = { try buildMCPManager($0) },
        modelFactory: @escaping ModelFactory = { OpenAICompatibleModel($0, $1) },
        feedbackFactory: @escaping FeedbackFactory = { config, emit in
            buildFeedback(config, emit: emit, executable: siblingExecutable("symbio"))
        },
        journalFactory: @escaping JournalFactory = buildUndoJournal
    ) {
        self.config = config
        self.send = send
        self.allowLocalWeb = allowLocalWeb
        self.managerFactory = managerFactory
        self.modelFactory = modelFactory
        self.feedbackFactory = feedbackFactory
        self.journalFactory = journalFactory
    }

    /// The lifecycle task; ends after `stopped` is sent.
    public var task: Task<Void, Never>? { lock.withLock { lifecycle } }

    public func start() {
        lock.withLock {
            precondition(lifecycle == nil, "UI worker가 이미 시작되었습니다.")
            lifecycle = Task.detached { [self] in await run() }
        }
    }

    public func submit(_ text: String) -> Bool { dispatch { try await $0.submit(text) } }
    public func approvePending() -> Bool { dispatch { try await $0.approvePending() } }
    public func rejectPending() -> Bool { dispatch { try await $0.rejectPending() } }
    public func undoLastWrite() -> Bool { dispatch { try await $0.undoLastWrite() } }

    public func attach(_ path: String) -> Bool {
        guard let controller = readyController() else { return false }
        do {
            send(.attachment(path: try controller.attach(path), refusal: nil))
        } catch {
            send(.attachment(path: nil, refusal: error.message))
        }
        return true
    }

    public func detach() { lock.withLock { controller }?.detach() }

    public func resetConversation() -> Bool { lock.withLock { controller }?.resetConversation() ?? false }

    public func cancelActive() -> Bool {
        let scheduled: Bool = lock.withLock {
            guard active != nil, ready else { return false }
            return true
        }
        guard scheduled else { return false }
        Task.detached { [self] in
            lock.withLock { closeAfterStop = true }
            await cancelActiveTask()
            send(.notice("요청을 중단하고 실행 중인 도구를 정리했습니다."))
            shutdownFlag.fire()
        }
        return true
    }

    /// Cancel any command, close MCP and the feedback loop, and wait until `stopped` was sent.
    public func shutdown() async {
        let (task, starting) = lock.withLock {
            preShutdown = true
            return (lifecycle, controller == nil)
        }
        guard let task else { return }
        await cancelActiveTask()
        if starting { task.cancel() } else { shutdownFlag.fire() }
        await task.value
    }

    private func readyController() -> RuntimeController? {
        lock.withLock { ready ? controller : nil }
    }

    private func dispatch(_ action: @escaping @Sendable (RuntimeController) async throws -> RuntimeResult) -> Bool {
        lock.withLock {
            guard ready, let controller else { return false }
            if active != nil { return true }  // Python schedules it, then drops it
            let id = UUID()
            active = (id, Task.detached { [self] in
                let message = await perform(action, controller)
                lock.withLock { if active?.id == id { active = nil } }
                if let message { send(message) }  // after clearing, so the next command is accepted
                if case .failure = message { shutdownFlag.fire() }
            })
            return true
        }
    }

    /// The message the command ends with; nil when it was cancelled.
    private func perform(
        _ action: @Sendable (RuntimeController) async throws -> RuntimeResult, _ controller: RuntimeController
    ) async -> UIMessage? {
        do {
            return .outcome(try await action(controller))
        } catch where Task.isCancelled || error is CancellationError {
            return nil
        } catch let error as PlannerError {
            // Known, already-redacted failures (e.g. the LLM API rejected the request): the
            // controller is back to IDLE, so report it and keep the window usable.
            return .outcome(.outcome(.error(error.message)))
        } catch {
            lock.withLock { closeAfterStop = true }
            return .failure("실행 실패 (\(typeName(error))); 설정과 MCP/네트워크 연결을 확인하세요.")
        }
    }

    private func cancelActiveTask() async {
        guard let task = lock.withLock({ active?.task }) else { return }
        task.cancel()
        await task.value
    }

    private func run() async {
        var feedback: FeedbackLoop?
        var manager: (any RuntimeManager)?
        do {
            if lock.withLock({ preShutdown }) { throw CancellationError() }
            let gateway = try managerFactory(config)
            manager = gateway
            let send = self.send
            feedback = feedbackFactory(config) { send(.notice($0)) }
            try await gateway.connect()
            let tools = try await gateway.listTools()
            let timeout = config.llmTimeoutSeconds
            var model = modelFactory(
                OpenAIClient(apiKey: config.apiKey, baseURL: config.baseURL, timeout: timeout), config.model)
            if let key = config.fallbackApiKey, !key.isEmpty {
                model = FallbackModel(model, modelFactory(
                    OpenAIClient(apiKey: key, baseURL: "https://api.openai.com/v1", timeout: timeout),
                    config.fallbackModel))
            }
            var stronger: (any ModelClient)?
            if let name = config.escalationModel, !name.isEmpty, let key = config.feedbackApiKey, !key.isEmpty {
                let router = modelFactory(OpenAIClient(apiKey: key, baseURL: openRouterBaseURL, timeout: timeout), name)
                stronger = feedback.map { $0.wrap(router) } ?? router
            }
            let planner = Planner(
                config, gateway, feedback.map { $0.wrap(model) } ?? model, tools: tools,
                allowLocalWeb: allowLocalWeb, undoJournal: journalFactory(config), escalationModel: stronger)
            let controller = RuntimeController(
                planner, stateObserver: { send(.state($0.current)) },
                plannerEventObserver: { send(.plannerEvent($0)) }, feedback: feedback.map(FeedbackAdapter.init))
            let stopping = lock.withLock {
                self.controller = controller
                ready = true
                return preShutdown
            }
            send(.state(.idle))
            if stopping { shutdownFlag.fire() }
            await shutdownFlag.wait()
            await cancelActiveTask()
        } catch where Task.isCancelled || error is CancellationError {
        } catch {
            lock.withLock { closeAfterStop = true }
            send(.failure("실행 실패 (\(typeName(error))); 설정과 MCP/네트워크 연결을 확인하세요."))
        }
        await manager?.close()
        if let feedback { await feedback.close(waitSeconds: 5) }
        let close = lock.withLock {
            ready = false
            controller = nil
            active = nil
            return closeAfterStop
        }
        send(.stopped(close))
    }
}

/// `<folder of this executable>/<name>`: `symbio-bridge` starts `symbio autonomy ...` next to it.
public func siblingExecutable(_ name: String) -> String {
    let me = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0]).resolvingSymlinksInPath()
    return me.deletingLastPathComponent().appendingPathComponent(name).path
}

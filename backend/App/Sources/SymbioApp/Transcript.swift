import CryptoKit
import Foundation
import SymbioCore
import SymbioMCP
import SymbioPlanner
import SymbioRuntime
import SymbioUndo

/// Python `llm_mcp_transcript.py`: run one text command and append one privacy-bounded JSONL
/// record per execution. Now `symbio transcript "명령" [options]`.

public let defaultTranscriptLog = "llm_mcp_transcript.log"
public let rawPayloadFlag = "--include-raw-payloads"

func timestamp() -> String {  // Python `datetime.now().astimezone().isoformat()`
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSSxxxxx"
    return formatter.string(from: Date())
}

func summary(_ text: String) -> JSON {
    let digest = SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    return ["chars": .int(text.unicodeScalars.count), "sha256": .string(digest)]
}

private let secretKeys = ["api_key", "authorization", "password", "token", "secret"]
private let secretMarkers = ["sk-", "gsk_", "bearer ", "api_key=", "openai_api_key", "groq_api_key"]

/// Secret-looking keys and strings become "[REDACTED]".
public func redactJSON(_ value: JSON) -> JSON {
    switch value {
    case let .object(pairs):
        return .object(pairs.map { key, item in
            let folded = key.lowercased()
            return (key, secretKeys.contains { folded.contains($0) } ? "[REDACTED]" : redactJSON(item))
        })
    case let .array(items): return .array(items.map(redactJSON))
    case let .string(text):
        let folded = text.lowercased()
        return secretMarkers.contains { folded.contains($0) } ? "[REDACTED]" : value
    default: return value
    }
}

func strings(_ values: [String]) -> JSON { .array(values.map { .string($0) }) }

func rawTurn(_ turn: ModelTurn) -> JSON {
    [
        "text": turn.text.map { .string($0) } ?? .null,
        "tool_calls": .array(turn.toolCalls.map {
            ["id": .string($0.id), "name": .string($0.name), "arguments": .string($0.arguments)]
        }),
        "clarification": turn.clarification.map {
            ["question": .string($0.question), "options": strings($0.options), "kind": .string($0.kind),
             "call_id": .string($0.callID)]
        } ?? .null,
    ]
}

/// Public fields only: an approval's context (prepared content, nonce) never goes to the log.
func rawOutcome(_ result: RuntimeResult) -> JSON {
    switch result {
    case let .busy(busy): return ["message": .string(busy.message), "state": .string(busy.state.name)]
    case let .outcome(.completed(done)):
        return ["text": .string(done.text), "changed_paths": strings(done.changedPaths),
                "read_paths": strings(done.readPaths), "found_paths": strings(done.foundPaths)]
    case let .outcome(.error(message)): return ["message": .string(message)]
    case let .outcome(.clarification(question)):
        return ["question": .string(question.question), "options": strings(question.options),
                "kind": .string(question.context.kind)]
    case let .outcome(.approvalRequired(approval)):
        return ["tool_name": .string(approval.toolName), "target_path": .string(approval.targetPath),
                "operation": .string(approval.operation), "content_chars": .int(approval.contentChars),
                "preview": .string(approval.preview), "preview_truncated": .bool(approval.previewTruncated),
                "targets": strings(approval.targets)]
    }
}

func outcomeSummary(_ result: RuntimeResult, raw: Bool) -> JSON {
    var summaryJSON: JSON
    switch result {
    case .busy: summaryJSON = ["type": "Busy"]
    case let .outcome(.completed(done)): summaryJSON = ["type": "Completed", "text": summary(done.text)]
    case let .outcome(.error(message)): summaryJSON = ["type": "Error", "message": summary(message)]
    case let .outcome(.clarification(question)):
        summaryJSON = ["type": "Clarification", "question": summary(question.question),
                       "option_count": .int(question.options.count)]
    case let .outcome(.approvalRequired(approval)):
        summaryJSON = ["type": "ApprovalRequired", "tool_name": .string(approval.toolName),
                       "operation": .string(approval.operation), "target_path": summary(approval.targetPath),
                       "content_chars": .int(approval.contentChars)]
    }
    if raw { summaryJSON.set("raw", redactJSON(rawOutcome(result))) }
    return summaryJSON
}

/// Parse the append-only JSONL transcript by execution record.
public func readRecords(_ path: URL) throws -> [JSON] {
    guard FileManager.default.fileExists(atPath: path.path) else { return [] }
    return try String(contentsOf: path, encoding: .utf8).split(separator: "\n")
        .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }.map { try JSON.parse(String($0)) }
}

/// Collect one execution and atomically append its bounded JSONL record.
public final class TranscriptLog: @unchecked Sendable {  // fields behind `lock`
    public let path: URL
    public let includeRawPayloads: Bool
    public let runID = UUID().uuidString.lowercased()
    private let lock = NSLock()
    private var startedAt: String?
    private var events: [JSON] = []
    private var finished = false
    public private(set) var writeError: String?
    /// Tests replace it to make the write fail.
    var append: (URL, String) throws -> Void = appendLocked

    public init(_ path: URL, includeRawPayloads: Bool = false) {
        self.path = path
        self.includeRawPayloads = includeRawPayloads
    }

    public func start(_ text: String) {
        lock.withLock {
            startedAt = timestamp()
            var event: JSON = ["event": "start", "sequence": 1, "user_text": summary(text)]
            if includeRawPayloads { event.set("raw_user_text", redactJSON(.string(text))) }
            events.append(event)
        }
    }

    public func event(_ name: String, _ fields: [(String, JSON)] = []) {
        lock.withLock {
            events.append(.object([("event", .string(name)), ("sequence", .int(events.count + 1))] + fields))
        }
    }

    public func finish(_ status: String, outcome: RuntimeResult? = nil, error: (any Error)? = nil) {
        let record: JSON? = lock.withLock {
            if finished { return nil }
            finished = true
            var record: JSON = [
                "version": 1, "run_id": .string(runID), "started_at": .string(startedAt ?? timestamp()),
                "ended_at": .string(timestamp()), "status": .string(status), "events": .array(events),
            ]
            if let outcome { record.set("outcome", outcomeSummary(outcome, raw: includeRawPayloads)) }
            if let error {
                var failure: JSON = ["type": .string(typeName(error)), "message": summary(String(describing: error))]
                if includeRawPayloads { failure.set("raw_message", redactJSON(.string(String(describing: error)))) }
                record.set("error", failure)
            }
            return record
        }
        guard let record else { return }
        do {
            try append(path, record.dumps(JSONDumpOptions(ensureASCII: false, compact: true)) + "\n")
        } catch {
            lock.withLock { writeError = typeName(error) }
        }
    }
}

/// One `flock`-guarded append, so concurrent runs never interleave a line.
func appendLocked(_ path: URL, _ line: String) throws {
    try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
    let fd = open(path.path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
    guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    defer { close(fd) }
    flock(fd, LOCK_EX)
    defer { flock(fd, LOCK_UN) }
    let bytes = Array(line.utf8)
    guard bytes.withUnsafeBytes({ write(fd, $0.baseAddress, $0.count) }) == bytes.count else {
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
}

/// Record bounded model-turn metadata; raw payloads require explicit opt-in.
public final class TranscriptModel: ModelClient, @unchecked Sendable {  // `turn` behind `lock`
    let model: any ModelClient
    let transcript: TranscriptLog
    private let lock = NSLock()
    private var turn = 0

    public init(_ model: any ModelClient, _ transcript: TranscriptLog) {
        self.model = model
        self.transcript = transcript
    }

    public func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        let number = lock.withLock { turn += 1; return turn }
        var input: [(String, JSON)] = [
            ("turn", .int(number)), ("message_count", .int(messages.count)),
            ("message_roles", .array(messages.map { $0["role"] ?? .null })),
            ("tool_names", strings(tools.map(\.name))), ("allow_tools", .bool(allowTools)),
            ("allow_clarification", .bool(allowClarification)),
        ]
        if transcript.includeRawPayloads {
            input.append(("raw_messages", redactJSON(.array(messages))))
            input.append(("raw_tools", redactJSON(.array(tools.map {
                ["name": .string($0.name), "description": .string($0.description), "schema": $0.schema]
            }))))
        }
        transcript.event("llm_input", input)
        let result = try await model.generate(
            messages, tools, allowTools: allowTools, allowClarification: allowClarification)
        var output: [(String, JSON)] = [
            ("turn", .int(number)), ("text", summary(result.text ?? "")),
            ("tool_calls", .array(result.toolCalls.map {
                ["id": .string($0.id), "name": .string($0.name), "arguments": summary($0.arguments)]
            })),
            ("clarification", result.clarification.map {
                ["kind": .string($0.kind), "question": summary($0.question), "option_count": .int($0.options.count)]
            } ?? .null),
        ]
        if transcript.includeRawPayloads { output.append(("raw", redactJSON(rawTurn(result)))) }
        transcript.event("llm_output", output)
        return result
    }
}

/// Python `run(...)`: exactly one terminal record per execution; unknown errors are re-thrown.
public func runTranscript(
    _ text: String, overrides: JSON = [:], out: @escaping @Sendable (String) -> Void, transcript: TranscriptLog,
    loadConfig load: (JSON) throws -> Config = { try loadConfig(overrides: $0) },
    makeManager: (Config) throws -> any RuntimeManager = { try buildMCPManager($0) },
    makeModel: (Config) -> any ModelClient = {
        OpenAICompatibleModel(
            OpenAIClient(apiKey: $0.apiKey, baseURL: $0.baseURL, timeout: $0.llmTimeoutSeconds), $0.model)
    },
    undoJournal: (Config) -> UndoJournal? = buildUndoJournal
) async throws {
    transcript.start(text)
    let outcome: RuntimeResult
    do {
        let config = try load(overrides)
        transcript.event("configuration", [("model", .string(config.model)), ("provider", .string(config.provider))])
        let manager = try makeManager(config)
        do {
            try await manager.connect()
            let planner = Planner(
                config, manager, TranscriptModel(makeModel(config), transcript), emit: out,
                tools: try await manager.listTools(), undoJournal: undoJournal(config))
            outcome = try await RuntimeController(planner, emit: out).submit(text)
            await manager.close()
        } catch {
            await manager.close()
            throw error
        }
    } catch let error as PlannerError {
        transcript.finish("planner_error", error: error)
        out("[ERROR] \(error.message)")
        out("[LOG] \(transcript.path.path)")
        return
    } catch {
        transcript.finish(error is CancellationError ? "cancelled" : "exception", error: error)
        throw error
    }
    transcript.finish("completed", outcome: outcome)
    printOutcome(outcome, out)
    out("[LOG] \(transcript.path.path)")
}

let transcriptUsage = """
    usage: symbio transcript TEXT [--provider {openai,groq}] [--model M] [--base-url URL]
                             [--allowed-directory DIR]... [--log-file PATH] [\(rawPayloadFlag)]
    """

/// Parsed `symbio transcript` arguments, or nil on a usage error.
func parseTranscriptArguments(_ argv: [String]) -> (text: String, overrides: JSON, log: URL, raw: Bool)? {
    var text: String?, overrides: JSON = [:], log = URL(fileURLWithPath: defaultTranscriptLog), raw = false
    var folders: [JSON] = []
    var rest = argv[...]
    while let arg = rest.popFirst() {
        let options = ["--provider": "provider", "--model": "model", "--base-url": "base_url"]
        if let key = options[arg] {
            guard let value = rest.popFirst() else { return nil }
            if key == "provider" && !["openai", "groq"].contains(value) { return nil }
            overrides.set(key, .string(value))
        } else if arg == "--allowed-directory" {
            guard let value = rest.popFirst() else { return nil }
            folders.append(.string(value))
        } else if arg == "--log-file" {
            guard let value = rest.popFirst() else { return nil }
            log = URL(fileURLWithPath: value)
        } else if arg == rawPayloadFlag {
            raw = true
        } else if text == nil && !arg.hasPrefix("--") {
            text = arg
        } else {
            return nil
        }
    }
    if !folders.isEmpty { overrides.set("allowed_directories", .array(folders)) }
    guard let text else { return nil }
    return (text, overrides, log, raw)
}

public func transcriptMain(_ argv: [String]) -> Int32 {
    guard let args = parseTranscriptArguments(argv) else {
        standardError(transcriptUsage)
        return 2
    }
    let transcript = TranscriptLog(args.log, includeRawPayloads: args.raw)
    let done = DispatchSemaphore(value: 0)
    let status = Box<Int32>(0)
    Task.detached {
        do {
            try await runTranscript(args.text, overrides: args.overrides, out: { print($0) }, transcript: transcript)
        } catch {
            print("[ERROR] \(errorMessage(error))")
            status.value = 1
        }
        done.signal()
    }
    done.wait()
    return status.value
}

/// A value a detached task hands back to synchronous code.
final class Box<Value: Sendable>: @unchecked Sendable {  // `stored` behind `lock`
    private let lock = NSLock()
    private var stored: Value
    init(_ value: Value) { stored = value }
    var value: Value {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

import Foundation
import SymbioCore
import SymbioFeedback
import SymbioFinder
import SymbioMCP
import SymbioPlanner
import SymbioRuntime
import SymbioUndo

/// `symbio-dev eval` (Python `evals/run_eval.py`): run JSON tasks through the real service path
/// (RuntimeController) in throwaway folders.
///
///     symbio-dev eval                        # every task, scripted model turns
///     symbio-dev eval --live --repeat 3      # model from config (costs API calls)
///     symbio-dev eval --live --lesson "..."  # same, with one candidate lesson
///
/// Grading checks state and side effects only (files, approvals, outcome status, required tools
/// run), not the quality of a summary. Negative "control" tasks validate the grader with
/// scripted turns and are left out of --live runs.
public enum Eval {
    public typealias TrialRunner = @Sendable (JSON, Config?, [String]) async -> JSON

    static let skip: Set<String> = [".eval"]  // harness-owned files inside the trial root

    // MARK: model stand-ins

    struct ScriptExhausted: Error {}  // Python LookupError: a harness/task defect, not an agent one

    /// Replays model turns from the task.
    final class Scripted: ModelClient, @unchecked Sendable {  // fields are behind `lock`
        private let lock = NSLock()
        private var turns: [JSON]
        private var count = 0
        var calls: Int { lock.withLock { count } }

        init(_ turns: [JSON], root: String) {
            self.turns = turns.compactMap { try? JSON.parse($0.dumps().replacingOccurrences(of: "{root}", with: root)) }
        }

        func generate(
            _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
        ) async throws -> ModelTurn {
            let (n, turn) = try lock.withLock {
                count += 1
                if turns.isEmpty { throw ScriptExhausted() }
                return (count, turns.removeFirst())
            }
            if let error = turn["format_error"] { throw PlannerError.modelFormat(error.str) }  // a reply the adapter could not use
            let calls = (turn["calls"]?.list ?? []).enumerated().map { i, call in
                ToolCall("c\(n)_\(i)", call["name"]?.str ?? "", (call["args"] ?? .null).dumps())
            }
            // {"question", "options", "kind": file_candidate|edit_criteria}
            let clarification = turn["ask"].flatMap { ask in
                ask.truthy ? ClarificationRequest(
                    ask["question"]?.str ?? "", (ask["options"]?.list ?? []).map(\.str), ask["kind"]?.str ?? "", "c\(n)")
                    : nil
            }
            return ModelTurn(turn["text"]?.stringValue, calls, clarification)
        }
    }

    /// Trials never launch apps: an open is checked by the app's rules, then only recorded.
    final class NoApps: Workspace, @unchecked Sendable {  // `paths` is behind `lock`
        private let lock = NSLock()
        private var paths: [String] = []
        var opened: [String] { lock.withLock { paths } }
        func activateFileViewerSelecting(_ fileURLs: [URL]) throws {}
        func open(_ url: URL) -> Bool {
            lock.withLock { paths.append(url.path) }
            return true
        }
    }

    /// Counts calls and keeps why a live trial failed: what the app told the model, what it said.
    final class Counting: ModelClient, @unchecked Sendable {  // fields are behind `lock`
        private let lock = NSLock()
        let inner: any ModelClient
        private var count = 0
        private var kept: [JSON] = []
        var calls: Int { lock.withLock { count } }
        var trace: [JSON] { lock.withLock { kept } }

        init(_ inner: any ModelClient) { self.inner = inner }

        func generate(
            _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
        ) async throws -> ModelTurn {
            lock.withLock { count += 1 }
            let turn = try await inner.generate(messages, tools, allowTools: allowTools, allowClarification: allowClarification)
            lock.withLock {
                guard kept.count < 12 else { return }
                let heard = messages.last?["content"].map { $0.stringValue ?? ($0.isNull ? "" : $0.text) } ?? ""
                kept.append([
                    "heard": .string(String(heard.unicodeScalars.prefix(160))),
                    "said": .string(String((turn.text ?? "").unicodeScalars.prefix(160))),
                    "calls": .array(turn.toolCalls.map { .string($0.name) }),
                    "asked": turn.clarification.map { .string(String($0.question.unicodeScalars.prefix(120))) } ?? .null,
                ])
            }
            return turn
        }
    }

    static func defaultLiveModel(_ config: Config) -> any ModelClient {
        OpenAICompatibleModel(
            OpenAIClient(apiKey: config.apiKey, baseURL: config.baseURL, timeout: config.llmTimeoutSeconds), config.model)
    }

    // MARK: grading helpers

    /// What a user who knows the task would say; nil when even they could not tell.
    static func autoAnswer(_ options: [String], _ task: JSON, root: String) -> String? {
        let request = task["request"]?.str ?? ""
        if options.isEmpty {  // a free question, in practice about where to put things
            return "처음 요청은 '\(request)'였습니다. 이름은 거기에 말한 그대로이고, 위치는 허용된 폴더 \(root) 안입니다."
        }
        let named = (task["expect"]?["files"]?.keys ?? []).map { ($0 as NSString).lastPathComponent }
        for wanted in [{ (n: String) in request.contains(n) }, { named.contains($0) }] {
            let hits = options.indices.filter { wanted(options[$0].name) }
            if hits.count == 1 { return "\(hits[0] + 1)번" }
        }
        return nil
    }

    static func manifest(_ root: String) -> [String: String] {
        var out: [String: String] = [:]
        guard let walk = FileManager.default.enumerator(atPath: root) else { return out }
        while let relative = walk.nextObject() as? String {
            if skip.contains(String(relative.split(separator: "/").first ?? "")) {
                walk.skipDescendants()
                continue
            }
            let path = root + "/" + relative
            var directory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &directory), !directory.boolValue,
                  let data = FileManager.default.contents(atPath: path) else { continue }
            out[relative] = sha(data)
        }
        return out
    }

    /// Initial state plus the task's declared changes; everything else must stay as it was.
    static func expectedManifest(_ initial: [String: String], _ files: JSON) -> [String: String] {
        var expected = initial
        for (path, content) in files.objectValue ?? [] {
            if content.isNull {
                expected[path] = nil
            } else {
                expected[path] = content.stringValue == "*" ? "*" : sha(content.str)
            }
        }
        return expected
    }

    static func diff(_ expected: [String: String], _ actual: [String: String]) -> [String] {
        Set(expected.keys).union(actual.keys).sorted().compactMap { path in
            let want = expected[path], got = actual[path]
            if want == "*" && got != nil || want == got { return nil }
            let kind = got == nil ? "missing" : want == nil ? "unexpected" : "content"
            return "\(path): \(kind)"
        }
    }

    /// Python `error_message` (main.py): SDK errors can carry request bodies and headers.
    static func errorMessage(_ error: any Error) -> String {
        switch error {
        case let error as ConfigError: error.message
        case let error as ToolError: error.message
        case let error as PlannerError: error.message
        default: "실행 실패 (\(type(of: error))); 설정과 MCP/네트워크 연결을 확인하세요."
        }
    }

    /// `asyncio.wait_for`: nil when the time ran out (the body is cancelled).
    static func withTimeout<T: Sendable>(_ seconds: Double, _ body: @escaping @Sendable () async throws -> T) async throws -> T? {
        try await withThrowingTaskGroup(of: T?.self) { group in
            group.addTask { try await body() }
            group.addTask {
                try await Task.sleep(for: .seconds(seconds))
                return nil
            }
            defer { group.cancelAll() }
            return try await group.next() ?? nil
        }
    }

    // MARK: one trial

    /// What one conversation leaves behind for grading.
    final class Conversation: @unchecked Sendable {  // fields are behind `lock`
        private let lock = NSLock()
        let task: JSON
        let root: String
        let live: Bool
        let initial: [String: String]
        private var answers: [String]
        private var decisions: [Bool]
        private var autoLeft = 3  // a model that keeps asking is not a simulator problem
        private(set) var violations: [String] = []
        private(set) var changed: [String] = []  // what the final Completed reports as written
        private var ran: [JSON] = []
        var toolsRun: [JSON] { lock.withLock { ran } }

        init(_ task: JSON, root: String, live: Bool, initial: [String: String]) {
            self.task = task
            self.root = root
            self.live = live
            self.initial = initial
            answers = (task["user"]?["clarify"]?.list ?? []).map(\.str)
            decisions = (task["user"]?["approve"]?.list ?? []).map(\.truthy)
        }

        func toolStarted(_ name: String?) { lock.withLock { ran.append(name.map { .string($0) } ?? .null) } }

        func relative(_ path: String) -> String { path.hasPrefix(root + "/") ? String(path.dropFirst(root.count + 1)) : path }

        func run(_ controller: RuntimeController) async throws -> Outcome {
            if let attach = task["attach"], attach.truthy {  // a file dropped on the window before the command
                _ = try controller.attach(root + "/" + attach.str)
            }
            var seen = initial  // the files as of the last decision
            var outcome = RuntimeResult.outcome(.error(""))
            // Follow-ups are separate commands typed one after another, as a user would.
            for request in [task["request"]?.str ?? ""] + (task["followups"]?.list ?? []).map(\.str) {
                outcome = try await controller.submit(request)
                answering: while true {
                    switch outcome {
                    case let .outcome(.clarification(question)):
                        // Live: a simulated user answers what the script did not foresee.
                        if answers.isEmpty, live, autoLeft > 0 {
                            autoLeft -= 1
                            if let reply = Eval.autoAnswer(question.options, task, root: root) { answers.append(reply) }
                        }
                        if answers.isEmpty { return Outcome("simulator_failure", "unscripted question: \(question.question)") }
                        outcome = try await controller.submit(answers.removeFirst())
                    case let .outcome(.approvalRequired(approval)):
                        // Nothing may change between one decision and the next ask.
                        if Eval.manifest(root) != seen { lock.withLock { violations.append("changed before approval") } }
                        if decisions.isEmpty, live {  // Live: approve only what the task says should change.
                            let wanted = task["expect"]?["files"]?.keys ?? []
                            decisions.append(wanted.contains(relative(approval.targetPath)))
                        }
                        if decisions.isEmpty { return Outcome("simulator_failure", "unscripted approval: \(approval.targetPath)") }
                        outcome = try await decisions.removeFirst() ? controller.approvePending() : controller.rejectPending()
                        seen = Eval.manifest(root)
                    default:
                        break answering
                    }
                }
                guard case let .outcome(.completed(done)) = outcome else { break }
                lock.withLock { changed += done.changedPaths.map(relative) }
            }
            if case .outcome(.completed) = outcome, task["expect"]?["undo"]?.truthy == true {
                outcome = try await controller.undoLastWrite()
            }
            switch outcome {
            case let .outcome(.completed(done)): return Outcome("completed", done.text)
            case let .outcome(.error(message)): return Outcome("error", message)
            case let .busy(busy): return Outcome("error", busy.message)
            default: return Outcome("error", "")
            }
        }
    }

    struct Outcome: Sendable {
        let status: String
        let text: String
        init(_ status: String, _ text: String) {
            self.status = status
            self.text = text
        }
    }

    static func makeRoot() throws -> String {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("va-eval-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return realPath(url.path) ?? url.path
    }

    /// Write the task's files, then set their ages (modification time, for date-based tasks).
    static func populate(_ task: JSON, root: String) {
        for (path, content) in task["files"]?.objectValue ?? [] {
            let target = root + "/" + path
            try? FileManager.default.createDirectory(
                atPath: (target as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
            if let b64 = content["b64"]?.stringValue {  // a real binary file (image, docx, zip)
                FileManager.default.createFile(atPath: target, contents: Data(base64Encoded: b64) ?? Data())
            } else {
                FileManager.default.createFile(atPath: target, contents: Data(content.str.utf8))
            }
        }
        for (path, days) in task["ages"]?.objectValue ?? [] {  // modification time, for date-based tasks
            let aged = Date(timeIntervalSinceNow: -(days.doubleValue ?? 0) * 86400)
            try? FileManager.default.setAttributes([.modificationDate: aged], ofItemAtPath: root + "/" + path)
        }
    }

    static func runTrial(
        _ task: JSON, _ config: Config?, _ lessons: [String],
        liveModel: (Config) -> any ModelClient = defaultLiveModel
    ) async -> JSON {
        let root: String
        do { root = try makeRoot() } catch { return ["task": task["id"] ?? .null, "status": "harness_failure", "passed": false, "valid": false] }
        defer { try? FileManager.default.removeItem(atPath: root) }
        populate(task, root: root)
        let initial = manifest(root)
        let rootURL = URL(fileURLWithPath: root, isDirectory: true)
        let trialConfig: Config
        let scripted: Scripted?
        let counting: Counting?
        if var config {
            config.allowedDirectories = [rootURL]
            config.feedbackEnabled = false
            trialConfig = config
            scripted = nil
            counting = Counting(liveModel(trialConfig))
        } else {
            trialConfig = Config(provider: "openai", model: "scripted", apiKey: "unused", allowedDirectories: [rootURL])
            scripted = Scripted(task["script"]?.list ?? [], root: root)
            counting = nil
        }
        let model: any ModelClient = scripted ?? counting!
        let conversation = Conversation(task, root: root, live: config != nil, initial: initial)
        let workspace = NoApps()
        var status = "", text = ""
        var feedback: FeedbackLoop?
        let started = ProcessInfo.processInfo.systemUptime
        do {
            try FileManager.default.createDirectory(atPath: root + "/.eval", withIntermediateDirectories: true)
            if !lessons.isEmpty {  // throwaway store: trial lessons never reach ~/.voice-action
                let loop = try FeedbackLoop(URL(fileURLWithPath: root + "/.eval/feedback.sqlite3"))
                feedback = loop
                let db = try SQLiteDatabase(path: loop.path.path)
                for lesson in lessons {
                    _ = try db.execute(
                        "INSERT INTO lessons (signature, text, created_ts, last_seen_ts) VALUES ('eval', ?, 0, 1e12)",
                        [.text(lesson)])
                }
            }
            let undo = try UndoJournal(path: URL(fileURLWithPath: root + "/.eval/undo.sqlite3"), allowedRoots: [rootURL])
            let client = try MCPClient(
                allowedDirectories: [rootURL], timeoutSeconds: trialConfig.mcpTimeoutSeconds,
                maxResultChars: trialConfig.maxToolResultChars, allowedTools: supportedTools)
            let manager = try MCPManager([("filesystem", client)])
            let wrapped: any ModelClient = feedback.map { $0.wrap(model) } ?? model
            let timeout = task["timeout"]?.doubleValue ?? 120
            let done = try await manager.withConnection { [feedback] gateway in
                let planner = Planner(trialConfig, gateway, wrapped, tools: try await gateway.listTools(), undoJournal: undo,
                    workspace: workspace)
                let controller = RuntimeController(
                    planner,
                    plannerEventObserver: { event in
                        if event.type == .toolStarted { conversation.toolStarted(event.toolName) }
                    },
                    feedback: feedback)
                return try await withTimeout(timeout) { try await conversation.run(controller) }
            }
            if let done { (status, text) = (done.status, done.text) } else { status = "timeout" }
        } catch {
            // Exhausted script = broken task/harness; anything else here is provider/network.
            status = error is ScriptExhausted ? "harness_failure" : "provider_failure"
            text = errorMessage(error)  // same redaction as the app
        }
        if let feedback { await feedback.close() }

        let expect = task["expect"] ?? [:]
        let undone = expect["undo"]?.truthy == true
        let after = manifest(root)
        var problems = diff(expectedManifest(initial, undone ? [:] : expect["files"] ?? [:]), after)
        let toolsRun = conversation.toolsRun
        problems += (expect["must_run"]?.list ?? []).filter { !toolsRun.contains($0) }.map { "\($0.str): not run" }
        if let want = expect["opened"] {  // NFC: Foundation hands paths back decomposed
            let opened = workspace.opened.map { nfcRelative($0, root) }, wanted = want.list.map(\.str)
            if opened != wanted { problems.append("opened \(pyRepr(opened)), expected \(pyRepr(wanted))") }
        }
        problems += (expect["reply_contains"]?.list ?? []).map(\.str)
            .filter { !text.contains($0) && !text.replacingOccurrences(of: ",", with: "").contains($0) }
            .map { "reply: missing \(pyRepr($0))" }
        problems += (expect["reply_excludes"]?.list ?? []).map(\.str).filter { text.contains($0) }
            .map { "reply: should not mention \(pyRepr($0))" }
        // The words must appear in this order (later mentions may repeat earlier ones).
        var position = text.startIndex
        let order = (expect["reply_order"]?.list ?? []).map(\.str)
        for word in order {
            guard let found = text.range(of: word, range: position..<text.endIndex) else {
                problems.append("reply: expected order \(pyRepr(order))")
                break
            }
            position = found.upperBound
        }
        if !undone {  // what a produced file must say, beyond that it exists
            for (path, words) in expect["files_contain"]?.objectValue ?? [] {
                let body = FileManager.default.contents(atPath: root + "/" + path).map { String(decoding: $0, as: UTF8.self) } ?? ""
                problems += words.list.map(\.str).filter { !body.contains($0) }.map { "\(path): missing \(pyRepr($0))" }
            }
        }
        problems += (expect["absent_dirs"]?.list ?? []).map(\.str).filter { isDirectory(root + "/" + $0) }
            .map { "\($0): directory should not exist" }
        if let want = expect["changed_paths"] {
            let wanted = want.list.map(\.str).sorted(), got = conversation.changed.sorted()
            if got != wanted { problems.append("changed_paths: \(pyRepr(got)) != \(pyRepr(wanted))") }
        }
        let violations = conversation.violations
        let valid = status == "completed" || status == "error"
        var passed = valid && problems.isEmpty && violations.isEmpty && status == (expect["status"]?.stringValue ?? "completed")
        if task["negative"]?.truthy == true { passed = valid && !passed }  // deliberately wrong run: the grader must reject it
        var result: JSON = [
            "task": task["id"] ?? .null, "family": task["family"] ?? .null, "axis": task["axis"] ?? .null,
            "level": task["level"] ?? .null, "status": .string(status), "passed": .bool(passed), "valid": .bool(valid),
            "problems": .array(problems.map { .string($0) }), "violations": .array(violations.map { .string($0) }),
            "model_calls": .int(scripted?.calls ?? counting?.calls ?? 0), "tools_run": .array(toolsRun),
            "graded": "state_and_side_effects",
            "seconds": .double(((ProcessInfo.processInfo.systemUptime - started) * 100).rounded() / 100),
            "reply": .string(String(text.unicodeScalars.prefix(200))),
        ]
        if let counting { result.set("trace", .array(counting.trace)) }
        return result
    }

    // MARK: versions

    /// Everything that decides a trial's behavior, for the results' code_hash: backend/**/*.swift.
    static func codeFiles(_ root: URL) -> [String] {
        let backend = root.appendingPathComponent("backend").path
        var files: [String] = []
        guard let walk = FileManager.default.enumerator(atPath: backend) else { return files }
        while let relative = walk.nextObject() as? String {
            if (relative as NSString).lastPathComponent == ".build" {
                walk.skipDescendants()
            } else if relative.hasSuffix(".swift") {
                files.append(backend + "/" + relative)
            }
        }
        return files.sorted()
    }

    static func versions(root: URL, model: String, lessons: [String], tasks: [JSON]) -> JSON {
        func git(_ args: String...) -> String {
            let done = try? run(["git", "-C", root.path] + args, captureError: true)
            return done?.output.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }
        var code = Data()
        for path in codeFiles(root) { code.append(FileManager.default.contents(atPath: path) ?? Data()) }
        return [
            "commit": .string(git("rev-parse", "--short", "HEAD")),
            "dirty": .bool(!git("status", "--porcelain").isEmpty),
            "code_hash": .string(String(sha(code).prefix(12))),
            "model": .string(model),
            "lessons": .array(lessons.map { .string(String(sha($0).prefix(12))) }),
            "tasks": .object(tasks.map { ($0["id"]?.str ?? "", .string(taskDigest($0))) }),
        ]
    }

    // MARK: entry point

    static func nfcRelative(_ path: String, _ root: String) -> String {
        (path.hasPrefix(root + "/") ? String(path.dropFirst(root.count + 1)) : path).precomposedStringWithCanonicalMapping
    }

    public static func main(
        _ argv: [String], root: URL = projectRoot(), trial: TrialRunner? = nil,
        loadConfig: () throws -> Config = { try SymbioCore.loadConfig() }
    ) async -> Int32 {
        let args: Arguments
        do { args = try Arguments(argv, switches: ["--live", "--include-targets"]) } catch {
            writeError("usage: symbio-dev eval [--task ID] [--live] [--repeat N] [--lesson TEXT] [--axis AXIS] "
                + "[--include-targets] [--dir DIR]\n")
            return 2
        }
        let live = args.switches.contains("--live")
        let lessons = args.all("--lesson")
        let tasksDir = root.appendingPathComponent("evals/tasks")
        let dir = args.last("--dir").map { URL(fileURLWithPath: $0) } ?? tasksDir
        var tasks = loadJSONFiles(dir)
        if live && dir.standardizedFileURL.path == tasksDir.standardizedFileURL.path {
            // real failures the feedback loop kept, verbatim
            tasks += loadJSONFiles(root.appendingPathComponent(".voice-action/failure-tasks"))
        }
        let ids = args.all("--task")
        if !ids.isEmpty { tasks = tasks.filter { ids.contains($0["id"]?.str ?? "") } }
        let axes = args.all("--axis")
        if !axes.isEmpty { tasks = tasks.filter { axes.contains($0["axis"]?.stringValue ?? "\u{0}") } }
        if !args.switches.contains("--include-targets") { tasks = tasks.filter { $0["status"]?.stringValue != "target" } }
        if !live {
            tasks = tasks.filter { $0.has("script") }
        } else {  // controls pass only when a run goes wrong; they would invert a live score
            // A task marked "live": false pins one scripted path, so only replays use it.
            tasks = tasks.filter { $0["negative"]?.truthy != true && ($0["live"]?.truthy ?? true) }
        }
        var config: Config?
        if live {
            do { config = try loadConfig() } catch {
                writeOutput(errorMessage(error) + "\n")
                return 1
            }
            HeavyLock.acquire()  // live runs make many model calls; one at a time across sessions
        }
        let runTrial: TrialRunner = trial ?? { task, config, lessons in await Eval.runTrial(task, config, lessons) }
        let repeats = Int(args.last("--repeat") ?? "1") ?? 1
        var trials: [JSON] = []
        var outage = 0
        rounds: for _ in 0..<max(repeats, 0) {
            for task in tasks {
                let r = await runTrial(task, config, lessons)
                trials.append(r)
                let passed = r["passed"]?.truthy == true, valid = r["valid"]?.truthy == true
                let mark = passed ? "PASS" : valid ? "FAIL" : "INVALID"
                var why = ((r["problems"]?.list ?? []) + (r["violations"]?.list ?? [])).map(\.str).joined(separator: "; ")
                if why.isEmpty && !valid { why = String((r["reply"]?.str ?? "").unicodeScalars.prefix(100)) }
                let status = r["status"]?.str ?? ""
                writeOutput("\(pad(mark, 7)) \(pad(r["task"]?.str ?? "", 28)) \(pad(status, 17)) "
                    + "calls=\(r["model_calls"]?.intValue ?? 0) \(why)\n")
                outage = ["provider_failure", "timeout"].contains(status) ? outage + 1 : 0
                if outage >= 3 {  // same outage again and again: stop paying for it
                    writeOutput("\n제공자 오류 3연속이라 중단합니다. 키·네트워크·속도 제한을 확인하세요.\n")
                    break rounds
                }
            }
        }
        let passedCount = trials.filter { $0["passed"]?.truthy == true }.count
        let invalid = trials.filter { $0["valid"]?.truthy != true }.count
        writeOutput("\npassed \(passedCount)/\(trials.count) (invalid \(invalid))\n")
        let results = root.appendingPathComponent("evals/results")
        let name = timestamp() + ".json"
        do {
            try FileManager.default.createDirectory(at: results, withIntermediateDirectories: true)
            let model = config?.model ?? "scripted"
            try writeJSON(
                ["versions": versions(root: root, model: model, lessons: lessons, tasks: tasks), "trials": .array(trials)],
                results.appendingPathComponent(name))
        } catch {
            writeError("결과를 저장하지 못했습니다: \(error)\n")
            return 1
        }
        writeOutput("saved evals/results/\(name)\n")
        return passedCount == trials.count ? 0 : 1
    }
}

/// The controller's feedback hooks on the Feedback package's loop (the App wires the same).
extension FeedbackLoop: @retroactive RuntimeFeedback {
    public func exception(_ error: any Error) { exception(error, kind: "exception") }
    public func state(_ previous: String, _ current: String, _ reason: String) {
        state(previous: previous, current: current, reason: reason)
    }
    public func cancelled(reason: String) { _ = record("cancelled", ["reason": .string(reason)]) }
    public func runFinished() { let _: [(rule: String, signature: String)] = runFinished() }
}

private extension String {
    var name: String { (self as NSString).lastPathComponent }
}

import Foundation
import SymbioCore
import SymbioPlanner
import Testing
@testable import SymbioDev

// Python tests/test_eval_runner.py, test_benchmark.py, test_make_cases.py and the score-round
// contract from test_autonomy.py.

func evalTask(_ extra: [(String, JSON)] = []) -> JSON {
    var t: JSON = ["id": "t", "files": ["notes.txt": "x"], "request": "notes.txt 내용 알려줘", "expect": ["files": [:]]]
    for (key, value) in extra { t.set(key, value) }
    return t
}

func liveConfig() -> Config {
    Config(provider: "openai", model: "live-model", apiKey: "unused-key",
           allowedDirectories: [URL(fileURLWithPath: repoRoot)], feedbackEnabled: false)
}

struct SecretError: Error { let text = "Authorization: Bearer sk-live-SECRET request body" }

struct Raising: ModelClient {
    func generate(_ m: [JSON], _ t: [ToolDefinition], allowTools: Bool, allowClarification: Bool) async throws -> ModelTurn {
        throw SecretError()
    }
}

struct Answering: ModelClient {
    func generate(_ m: [JSON], _ t: [ToolDefinition], allowTools: Bool, allowClarification: Bool) async throws -> ModelTurn {
        ModelTurn("x입니다.", [])
    }
}

final class Seen: @unchecked Sendable {
    private let lock = NSLock()
    private var ids: [String] = []
    var all: [String] { lock.withLock { ids } }
    func add(_ id: String) { lock.withLock { ids.append(id) } }
}

/// A throwaway project folder: evals/tasks (+ backend so `projectRoot()` accepts it as cwd).
func project(_ tasks: [JSON]) throws -> URL {
    let root = URL(fileURLWithPath: try temporaryDirectory())
    for folder in ["evals/tasks", "evals/results", "backend"] {
        try FileManager.default.createDirectory(at: root.appendingPathComponent(folder), withIntermediateDirectories: true)
    }
    for t in tasks { try writeJSON(t, root.appendingPathComponent("evals/tasks/\(t["id"]!.str).json")) }
    return root
}

/// Run `Eval.main` with a fake trial and return the ids it ran.
func mainSeen(_ argv: [String], _ tasks: [JSON], status: String = "completed") async throws -> [String] {
    setenv(HeavyLock.heldEnv, "1", 1)  // --live takes the cross-session lock; a fake run must not wait on it
    let root = try project(tasks)
    defer { try? FileManager.default.removeItem(at: root) }
    let seen = Seen()
    let ok = status == "completed"
    _ = await Eval.main(argv, root: root, trial: { t, _, _ in
        seen.add(t["id"]!.str)
        return ["task": t["id"]!, "passed": .bool(ok), "valid": .bool(ok), "status": .string(status), "model_calls": 0,
                "problems": [], "violations": [], "reply": "LLM 인증 실패"]
    }, loadConfig: liveConfig)
    return seen.all
}

@Suite struct EvalRunnerTests {
    @Test func providerErrorTextNeverReachesResults() async {
        let result = await Eval.runTrial(evalTask(), liveConfig(), [], liveModel: { _ in Raising() })
        #expect(result["status"] == "provider_failure")
        #expect(!result.text.contains("SECRET") && !result.text.contains("Bearer"))
    }

    @Test func requiredToolMustActuallyRun() async {
        let script: JSON = [["text": "요약했습니다."]]  // answers without reading the file
        let result = await Eval.runTrial(
            evalTask([("script", script), ("expect", ["files": [:], "must_run": ["filesystem__read_text_file"]])]), nil, [])
        #expect(result["passed"] == false && result["tools_run"] == [])
        #expect(result["graded"] == "state_and_side_effects")
    }

    @Test func liveRunsLeaveOutNegativeControlTasks() async throws {
        let seen = try await mainSeen(["--live"], [evalTask([("id", "a")]), evalTask([("id", "n"), ("negative", true)])])
        #expect(seen == ["a"])
    }

    @Test func trialResultCarriesAxisAndLevel() async {
        let result = await Eval.runTrial(evalTask([("script", [["text": "x"]]), ("axis", "see"), ("level", 10)]), nil, [])
        #expect(result["axis"] == "see" && result["level"] == 10)
    }

    @Test func mainSkipsTargetTasksByDefaultAndIncludeTargetsRunsThem() async throws {
        let tasks = [evalTask([("id", "a")]), evalTask([("id", "b"), ("status", "target")])]
        #expect(try await mainSeen(["--live"], tasks) == ["a"])
        #expect(try await mainSeen(["--live", "--include-targets"], tasks) == ["a", "b"])
    }

    @Test func replyContainsGradesTheAnswer() async {
        let script: JSON = [["text": "파일은 a.txt, b.md 입니다."]]
        let ok = await Eval.runTrial(evalTask([("script", script), ("expect", ["files": [:], "reply_contains": ["a.txt", "b.md"]])]), nil, [])
        let bad = await Eval.runTrial(evalTask([("script", script), ("expect", ["files": [:], "reply_contains": ["c.csv"]])]), nil, [])
        #expect(ok["passed"] == true && bad["passed"] == false)
        #expect(bad["problems"] == ["reply: missing 'c.csv'"])
    }

    @Test func replyOrderAndExcludesAreGraded() async {
        let script: JSON = [["text": "b.txt, c.txt, a.txt 순서입니다."]]
        let ok = await Eval.runTrial(evalTask([("script", script), ("expect", [
            "files": [:], "reply_order": ["b.txt", "c.txt", "a.txt"], "reply_excludes": ["old.txt"]])]), nil, [])
        let bad = await Eval.runTrial(evalTask([("script", script), ("expect", [
            "files": [:], "reply_order": ["a.txt", "b.txt"], "reply_excludes": ["c.txt"]])]), nil, [])
        #expect(ok["passed"] == true && bad["passed"] == false && bad["problems"]?.list.count == 2)
    }

    @Test func binaryFixtureAndFilesContainAreGraded() async {
        let blob = Data([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01]).base64EncodedString()
        let script: JSON = [
            ["calls": [["name": "filesystem__write_file", "args": ["path": "{root}/out.md", "content": "매출 120억, 비용 30억"]]]],
            ["text": "저장했습니다."],
        ]
        func run(_ words: JSON) async -> JSON {
            await Eval.runTrial(evalTask([
                ("files", ["pic.png": ["b64": .string(blob)]]), ("script", script), ("user", ["approve": [true]]),
                ("expect", ["files": ["out.md": "*"], "files_contain": ["out.md": words]]),
            ]), nil, [])
        }
        let ok = await run(["120억", "30억"]), bad = await run(["999억"])
        #expect(ok["passed"] == true && bad["passed"] == false)
        #expect(bad["problems"] == ["out.md: missing '999억'"])
    }

    @Test func replyOrderAllowsAnIntroThatMentionsANameFirst() async {
        let script: JSON = [["text": "a.txt를 포함해 정렬하면 b.txt, c.txt, a.txt 순입니다."]]
        let result = await Eval.runTrial(
            evalTask([("script", script), ("expect", ["files": [:], "reply_order": ["b.txt", "c.txt", "a.txt"]])]), nil, [])
        #expect(result["passed"] == true)
    }

    @Test func absentDirsCatchesALeftoverFolder() async {
        let script: JSON = [["calls": [["name": "local__create_directory", "args": ["path": "{root}/정리"]]]], ["text": "만들었습니다."]]
        let result = await Eval.runTrial(evalTask([
            ("script", script), ("request", "정리 폴더를 만들어줘"), ("user", ["approve": [true]]),
            ("expect", ["files": [:], "absent_dirs": ["정리"]]),
        ]), nil, [])
        #expect(result["passed"] == false && result["problems"]?.list.contains("정리: directory should not exist") == true)
    }

    @Test func runStopsAfterThreeProviderFailuresInARow() async throws {
        let tasks = (0..<6).map { evalTask([("id", .string("t\($0)"))]) }
        #expect(try await mainSeen(["--live"], tasks, status: "provider_failure").count == 3)
    }

    @Test func liveTrialRecordsWhatTheModelHeardAndSaid() async {
        let result = await Eval.runTrial(evalTask(), liveConfig(), [], liveModel: { _ in Answering() })
        #expect(result["status"] == "completed")
        #expect(result["trace"]?.list.first?["said"] == "x입니다.")
    }

    @Test func agesSetModificationTimeBeforeTheRun() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(atPath: root) }
        Eval.populate(evalTask([("ages", ["notes.txt": 5])]), root: root)
        let modified = try FileManager.default.attributesOfItem(atPath: root + "/notes.txt")[.modificationDate] as! Date
        let age = -modified.timeIntervalSinceNow
        #expect(5 * 86400 - 60 < age && age < 5 * 86400 + 600)
    }

    @Test func axisFilterRunsOnlyThatAxis() async throws {
        let tasks = [evalTask([("id", "a"), ("axis", "see")]), evalTask([("id", "b"), ("axis", "read")])]
        #expect(try await mainSeen(["--live", "--axis", "read"], tasks) == ["b"])
    }

    @Test func codeHashCoversEntryPointAndHarness() {
        let names = Set(Eval.codeFiles(URL(fileURLWithPath: repoRoot)).map { ($0 as NSString).lastPathComponent })
        #expect(names.isSuperset(of: ["main.swift", "Eval.swift", "Planner.swift"]))
    }

    @Test func autoAnswerPicksTheOptionTheRequestNamesAndGivesUpOtherwise() {
        let t: JSON = ["request": "a.txt 지워줘", "expect": ["files": [:]]]
        #expect(Eval.autoAnswer(["/r/a_old.txt", "/r/a.txt"], t, root: "/r") == "2번")
        #expect(Eval.autoAnswer(["/r/x.txt", "/r/y.txt"], t, root: "/r") == nil)
        #expect(Eval.autoAnswer([], t, root: "/r")?.contains("/r") == true)
    }

    /// Feedback's contract: a `saved <path relative to root>` line and the versions it compares.
    @Test func savedLineAndVersionsFollowTheFeedbackContract() throws {
        let root = try project([evalTask([("id", "a"), ("script", [["text": "x"]])])])
        defer { try? FileManager.default.removeItem(at: root) }
        let done = try run([tool, "eval"], captureError: true, directory: root.path)
        let saved = try #require(done.output.split(separator: "\n").last { $0.hasPrefix("saved ") })
        let data = try JSON.parse(Data(contentsOf: root.appendingPathComponent(String(saved.dropFirst(6)))))
        #expect(Set(data["versions"]?.keys ?? []) == ["commit", "dirty", "code_hash", "model", "lessons", "tasks"])
        #expect(data["versions"]?["model"] == "scripted" && data["trials"]?.list.first?["task"] == "a")
    }
}

@Suite struct MakeCasesTests {
    static let task: JSON = ["id": "t", "files": ["a.txt": "x"], "request": "a.txt 지워줘", "expect": ["files": ["a.txt": nil]], "script": []]

    @Test func level1OnlyRewords() {
        let out = MakeCases.mutate(Self.task, 1)
        #expect(out["files"] == Self.task["files"] && out["request"] != Self.task["request"])
        #expect(out["expect"] == Self.task["expect"] && out["base"] == "t")
    }

    @Test func level3AddsNoiseAndDecoyWithoutTouchingTheOriginal() {
        let out = MakeCases.mutate(Self.task, 3)
        #expect(out["files"]?.keys.count == 1 + 200 + 1 && out["files"]?["a_old.txt"] != nil)
        #expect(out["expect"] == Self.task["expect"] && Self.task["files"] == ["a.txt": "x"])
    }

    @Test func sameTaskSameVariant() { #expect(MakeCases.mutate(Self.task, 2) == MakeCases.mutate(Self.task, 2)) }

    @Test func pickMatchesPython() {  // int(sha256(id).hexdigest(), 16) % 3
        #expect([MakeCases.pick("t", 3), MakeCases.pick("delete_pattern", 3), MakeCases.pick("org_copy_keep_both", 3)] == [0, 2, 1] as [Int])
    }
}

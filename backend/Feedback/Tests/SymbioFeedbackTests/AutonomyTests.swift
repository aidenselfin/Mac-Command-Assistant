import Foundation
import SymbioCore
import SymbioPlanner
import Testing
@testable import SymbioFeedback

/// tests/test_autonomy.py (test_bench_job_picks_the_lowest_blocked_task_per_axis needs the
/// benchmark scorer, which moves to the Dev package with evals/benchmark.py).

func summary(_ tasks: [String: (Int, Int)]) -> EvalSummary {
    EvalSummary(tasks: tasks.mapValues { [$0.0, $0.1] }, invalid: 0, passed: tasks.values.reduce(0) { $0 + $1.0 })
}

@Test func summarizeCountsInvalidApartFromFailures() {
    let trials: [JSON] = [
        ["task": "a", "valid": true, "passed": true],
        ["task": "a", "valid": true, "passed": false],
        ["task": "a", "valid": false, "passed": false],
    ]
    #expect(summarize(trials) == EvalSummary(tasks: ["a": [1, 1]], invalid: 1, passed: 1))
}

@Test func decideNeedsAClearWinOverTheBestBaseline() {
    let base = [summary(["a": (3, 0), "b": (1, 2)]), summary(["a": (3, 0), "b": (2, 1)])]
    #expect(decide(base, summary(["a": (3, 0), "b": (3, 0)])) == "adopt")
    #expect(decide(base, summary(["a": (3, 0), "b": (2, 1)])) == "hold")  // inside the baselines' spread
    #expect(decide(base, summary(["a": (2, 1), "b": (3, 0)])) == "reject")  // a never-failing task failed
    var noisy = summary(["a": (3, 0), "b": (3, 0)])
    noisy.invalid = 5
    #expect(decide(base, noisy) == "hold")
}

@Test func patchesStayOffSafetyFilesAndExistingTests() {
    let newTest = "backend/Planner/Tests/SymbioPlannerTests/NewTests.swift"
    let ok = ["3\t1\tbackend/Planner/Sources/SymbioPlanner/Planner.swift", "20\t0\t\(newTest)", "5\t0\tdocs/status.md"]
    #expect(checkDiff(ok, [newTest]).isEmpty)
    let bad = [
        "1\t1\tbackend/Guard/Sources/SymbioGuard/Guard.swift", "0\t4\tbackend/Planner/Tests/SymbioPlannerTests/PlannerTests.swift",
        "1\t0\tevals/tasks/x.json", "1\t0\tmain.py", "1\t0\tconfig.json", "1\t0\tbackend/Planner/Package.swift",
    ]
    #expect(checkDiff(bad, []).count == 6)
    #expect(checkDiff(["2\t0\tevals/tasks/new.json"], ["evals/tasks/new.json"]).isEmpty)
}

final class Started: @unchecked Sendable {  // `items` is behind `lock`
    private let lock = NSLock()
    private var items: [(String, Int)] = []
    func append(_ kind: String, _ ref: Int) { lock.withLock { items.append((kind, ref)) } }
    var all: [(String, Int)] { lock.withLock { items } }
}

@Test func analysisStartsWorkersWithinBudget() async throws {
    let started = Started()
    let h = try makeLoop(analyzer: FakeAnalyzer(lessonResult), launch: { started.append($0, $1) })
    try h.loop.queue("R4", "reject:a")
    await h.loop.drain()
    let lessonID = try h.loop.candidateLessons()[0].id
    #expect(started.all.map(\.0) == ["lesson"] && started.all.map(\.1) == [lessonID])

    h.loop.analyzer = FakeAnalyzer(["root_cause": "버그", "action": "code_fix", "fix_request": "고쳐줘", "confidence": 0.9])
    for sig in ["x", "y", "z", "w"] {
        try h.loop.queue("R1", "error:\(sig)")
        await h.loop.drain()
    }
    try h.loop.startWorker("fix", 99, "error:x")  // same signature again
    #expect(started.all.filter { $0.0 == "fix" }.count == 3)  // the daily limit cuts the fourth
    #expect(try h.loop.recentIncidents()[0].analysis?["action"] == "code_fix")
}

@Test func withoutALauncherNothingIsStarted() async throws {
    let h = try makeLoop(analyzer: FakeAnalyzer(lessonResult))
    try h.loop.queue("R4", "reject:a")
    await h.loop.drain()
    #expect(try h.loop.candidateLessons().count == 1)
    #expect(try h.loop.all("SELECT COUNT(*) FROM events WHERE kind='autonomy_start'")[0][0].int == 0)
}

// ---- worker flows with the slow parts (paid eval, agent, git, check tool) replaced

func evalResult(_ passed: Int, _ failed: Int) -> JSON {
    [
        "versions": ["code_hash": "h", "model": "m", "tasks": [:], "lessons": []],
        "trials": .array((0..<(passed + failed)).map { ["task": "a", "valid": true, "passed": .bool($0 < passed)] }),
    ]
}

@Test func lessonJobAdoptsRejectsOrHolds() throws {
    for (candidate, baseline, verdict, active) in [
        ((3, 0), (2, 1), "adopt", 1), ((2, 1), (3, 0), "reject", 0), ((3, 0), (3, 0), "hold", 0),
    ] {
        let h = try makeLoop()
        try h.loop.addLesson("sig", "지침")
        let lessonID = try h.loop.candidateLessons()[0].id
        let worker = AutonomyWorker(root: h.dir, devCommand: ["symbio-dev"])
        worker.matchingBaselines = { _ in [] }
        worker.liveEval = { lessons, _ in lessons.isEmpty ? evalResult(baseline.0, baseline.1) : evalResult(candidate.0, candidate.1) }
        #expect(try worker.lessonJob(h.loop, lessonID)["status"]?.stringValue == verdict)
        #expect(try h.loop.activeLessons().count == active)
        #expect(try (h.loop.candidateLessons().count == 1) == (verdict == "hold"))
    }
}

/// Answers the commands fixJob runs.
final class FakeGit: @unchecked Sendable {  // used from one job at a time
    var calls: [String] = []
    var checks = 0
    let numstat: String, added: String
    var checkResults: [(Int32, String)]
    var ff: [Int32]
    let pull: Int32, ancestor: Int32

    init(_ numstat: String, _ added: String = "", checks: [(Int32, String)] = Array(repeating: (0, ""), count: 9),
         ff: [Int32] = Array(repeating: 0, count: 9), pull: Int32 = 0, ancestor: Int32 = 1)
    {
        self.numstat = numstat
        self.added = added
        checkResults = checks
        self.ff = ff
        self.pull = pull
        self.ancestor = ancestor
    }

    func run(_ command: [String], _ cwd: URL, _ timeout: Double) -> CommandResult {
        let text = command.joined(separator: " ")
        calls.append(text)
        var status: Int32 = 0, out = ""
        if text.contains("vibe worktree") {
            out = "/tmp/wt\n"
        } else if text.contains("diff --cached --numstat") {
            out = numstat
        } else if text.contains("--diff-filter=A") {
            out = added
        } else if text.contains("--abbrev-ref") {
            out = "main"
        } else if command.last == "check" {
            checks += 1
            (status, out) = checkResults[checks - 1]
        } else if text.contains("merge --ff-only") {
            status = ff.removeFirst()
        } else if text.contains("merge main") {
            status = pull
        } else if text.contains("--is-ancestor") {
            status = ancestor
        }
        return CommandResult(status: status, output: out)
    }
}

func runFix(_ fake: FakeGit) throws -> JSON {
    let h = try makeLoop()
    _ = try h.loop.all(
        "INSERT INTO incidents (id, ts, rule, signature, status, evidence) VALUES (1, 0, 'R1', 'error:x', 'analyzed', '[]')")
    h.loop.record("feedback", ["incident": 1, "root_cause": "원인", "fix_request": "고쳐줘"])
    let worker = AutonomyWorker(root: h.dir, devCommand: ["symbio-dev"])
    worker.run = fake.run
    worker.agentCommand = { _, _ in ["agent"] }
    return try worker.fixJob(h.loop, 1)
}

let good = "3\t0\tbackend/Planner/Tests/SymbioPlannerTests/NewTests.swift"
let goodAdded = "backend/Planner/Tests/SymbioPlannerTests/NewTests.swift"

@Test func fixJobMergesAfterTheCheckToolPasses() throws {
    let fake = FakeGit(good, goodAdded)
    #expect(try runFix(fake)["status"] == "merged")
    #expect(fake.checks == 1 && fake.calls.contains { $0.contains("tag -f autofix-pre-1") })
}

@Test func fixJobNeverMergesAProtectedOrFailingPatch() throws {
    var fake = FakeGit("1\t1\tbackend/Guard/Sources/SymbioGuard/Guard.swift")
    #expect(try runFix(fake)["status"] == "rejected")
    #expect(fake.checks == 0 && !fake.calls.contains { $0.contains("merge") })

    fake = FakeGit(good, goodAdded, checks: [(1, "FAIL  eval replay  x\n")])
    let result = try runFix(fake)
    #expect(result["status"] == "rejected" && result["why"]!.stringValue!.contains("eval replay"))
    #expect(!fake.calls.contains { $0.contains("merge") })
}

@Test func fixJobRetriesOnceAfterMainMoved() throws {
    var fake = FakeGit(good, goodAdded, ff: [1, 0])
    #expect(try runFix(fake)["status"] == "merged")
    #expect(fake.checks == 2)  // re-checked with main merged in

    fake = FakeGit(good, goodAdded, ff: [1, 1], ancestor: 0)  // not about main moving
    #expect(try runFix(fake)["status"] == "branch_kept")
    #expect(fake.checks == 1)

    fake = FakeGit(good, goodAdded, ff: [1], pull: 1)  // main conflicts
    #expect(try runFix(fake)["status"] == "branch_kept")
    #expect(fake.calls.contains { $0.contains("merge --abort") })
}

@Test func fixJobReportsAnAgentTimeout() throws {
    let h = try makeLoop()
    _ = try h.loop.all(
        "INSERT INTO incidents (id, ts, rule, signature, status, evidence) VALUES (1, 0, 'R1', 'error:x', 'analyzed', '[]')")
    h.loop.record("feedback", ["incident": 1, "root_cause": "원인", "fix_request": "고쳐줘"])
    let fake = FakeGit(good, goodAdded)
    let worker = AutonomyWorker(root: h.dir, devCommand: ["symbio-dev"])
    worker.run = { command, cwd, timeout in
        if command == ["agent"] { throw CommandTimeout() }
        return fake.run(command, cwd, timeout)
    }
    worker.agentCommand = { _, _ in ["agent"] }
    let result = try worker.fixJob(h.loop, 1)
    #expect(result["status"] == "failed" && result["why"] == "에이전트 시간 초과")
    #expect(fake.calls.contains { $0.contains("worktree remove --force") })
}

@Test func runCommandCapturesOutputAndTimesOut() throws {
    let done = try runCommand(["sh", "-c", "echo hi; echo err >&2; exit 3"], tempDir(), 10)
    #expect(done == CommandResult(status: 3, output: "hi\n", error: "err\n"))
    #expect(throws: CommandTimeout.self) { try runCommand(["sleep", "5"], tempDir(), 0.2) }
}

@Test func benchmarkGapBecomesAnAnalysedIncident() async throws {
    let analyzer = FakeAnalyzer(lessonResult)
    let h = try makeLoop(analyzer: analyzer)
    let gap: JSON = [
        "task": "file_info", "axis": "see", "level": 15, "rate": 0.0, "kind": "target",
        "problems": ["tool: not run"], "definition": "{}",
    ]
    #expect(h.loop.ingestBenchmark([gap]) == ["file_info"])
    await h.loop.drain()
    let (rule, bundle) = analyzer.calls[0]
    #expect(rule == "B1" && bundle.contains("benchmark_gap") && bundle.contains("file_info"))
    #expect(h.loop.runID == nil)
    #expect(h.emitted.all.contains { $0.contains("문제 감지 B1") })
}

@Test func sameCommandCancelledTwiceIsAnIncident() throws {
    let h = try makeLoop()
    var seen: [[String]] = []
    for _ in 0..<2 {
        h.clock.now += 700
        h.loop.userInput("사진 정리해줘", newRun: true)
        h.loop.exception(CancellationError())
        seen.append(h.loop.rules())
    }
    #expect(!seen[0].contains("R10") && seen[1].contains("R10"))
    h.clock.now += 700
    h.loop.userInput("사진 정리해줘", newRun: true)
    h.loop.outcome(.completed(Completed("ok")))
    #expect(!h.loop.rules().contains("R10"))
}

@Test func benchRoundLaunchesWorkersOnlyWhenFeedbackAndAutonomyAreOn() async throws {
    for (enabled, auto, launches) in [(true, true, true), (true, false, false), (false, true, false)] {
        let h = try makeLoop()
        let config = Config(
            provider: "openai", model: "m", apiKey: "k", allowedDirectories: [], feedbackEnabled: enabled,
            feedbackApiKey: "k", feedbackDailyLimit: 1, feedbackAutonomy: auto)
        let worker = AutonomyWorker(root: h.dir, devCommand: ["symbio-dev"])
        worker.benchJob = { _, _ in (["status": "done", "eval": "r.json", "gaps": []], true) }
        worker.loadConfig = { config }
        let result = try await worker.benchMain(h.loop)
        #expect((h.loop.launch != nil) == launches)
        #expect((h.loop.analyzer != nil) == enabled)  // feedback off: no paid analysis
        if enabled { #expect(h.loop.dailyLimit == 1) }
        #expect(result["queued"] == [] && result["gaps"] == nil)
    }
}

@Test func untrustedBenchRoundQueuesNothing() async throws {
    let h = try makeLoop()
    let worker = AutonomyWorker(root: h.dir, devCommand: ["symbio-dev"])
    worker.benchJob = { _, _ in (["status": "done", "eval": "r.json", "gaps": [["task": "t"]]], false) }
    let result = try await worker.benchMain(h.loop)
    #expect(result["gaps"] == [] && result["why"] == "평가 결과를 믿을 수 없어 분석 생략")
}

import Foundation
import SymbioCore
import Testing
@testable import SymbioDev

// Python tests/test_benchmark.py: scoring is pure, deterministic and live-results only.

let benchConfig: JSON = ["threshold": 0.8, "axes": ["see": 1, "read": 1]]

func trial(_ id: String, _ passed: Bool = true, _ valid: Bool = true) -> JSON {
    ["task": .string(id), "passed": .bool(passed), "valid": .bool(valid)]
}

func bt(_ id: String, _ axis: String, _ level: Int, _ extra: [(String, JSON)] = []) -> JSON {
    var t: JSON = ["id": .string(id), "axis": .string(axis), "level": .int(level), "status": "active"]
    for (key, value) in extra { t.set(key, value) }
    return t
}

func rates(_ r: Benchmark.Rates) -> [String: Double?] { Dictionary(uniqueKeysWithValues: r.map { ($0.id, $0.rate) }) }

let repoTasks = loadJSONFiles(URL(fileURLWithPath: repoRoot + "/evals/tasks"))
let repoConfig = try! JSON.parse(Data(contentsOf: URL(fileURLWithPath: repoRoot + "/evals/benchmark.json")))

/// A project folder with one task file per task, a live result and the test config.
func benchProject(model: String = "live-model", tasks: [JSON]? = nil, trials: [JSON] = [trial("a")]) throws -> URL {
    let root = try project(tasks ?? [bt("a", "see", 5)])
    try writeJSON(["versions": ["model": .string(model)], "trials": .array(trials)],
                  root.appendingPathComponent("evals/results/20261028-000000.json"))
    try writeJSON(benchConfig, root.appendingPathComponent("evals/benchmark.json"))
    return root
}

func bench(_ root: URL, _ args: [String]) throws -> Output {
    let done = try run([tool, "benchmark"] + args, captureError: true, directory: root.path)
    return Output(status: done.status, stdout: done.output, stderr: done.error)
}

func records(_ root: URL) -> [String] {
    (try? FileManager.default.contentsOfDirectory(atPath: root.path + "/evals/benchmarks")) ?? []
}

func resultFile(_ root: URL) -> URL { root.appendingPathComponent("evals/results/20261028-000000.json") }

@Suite struct BenchmarkScoreTests {
    @Test func axisScoreThresholdBoundary() {
        #expect(Benchmark.axisScore([(10, 0.8)]) == 10)
        #expect(Benchmark.axisScore([(10, 0.79)]) == 0)
    }

    @Test func axisScoreStopsAtFirstFailingLevel() {
        #expect(Benchmark.axisScore([(10, 1.0), (20, 0.0), (30, 1.0)]) == 10)
        #expect(Benchmark.axisScore([(5, 0.0), (10, 1.0)]) == 0)
        #expect(Benchmark.axisScore([(10, 1.0), (10, 0.0), (20, 1.0)]) == 0)
        #expect(Benchmark.axisScore((1..<20).map { ($0, 1.0) } + [(50, 0.0)]) == 19)
    }

    @Test func overallRules() {
        #expect(Benchmark.overall([("a", 50), ("b", nil)], ["a": 1, "b": 1]) == 50)
        #expect(Benchmark.overall([("a", nil)], ["a": 1]) == nil)
        #expect(Benchmark.overall([("a", 100), ("b", 0)], ["a": 3, "b": 1]) == 75)
        #expect(Benchmark.overall([("a", 100), ("b", 99)], ["a": 1, "b": 1]) == 99)
        #expect(Benchmark.overall([("a", 15), ("b", 34)], ["a": 1, "b": 1]) == 25)
        #expect(Benchmark.overall([("a", 100), ("b", 100)], ["a": 1, "b": 1]) == 100)
        #expect(Benchmark.overall([("a", 100), ("b", nil)], ["a": 1, "b": 1]) == 99)
    }

    @Test func taskRates() {
        #expect(rates(Benchmark.taskRates([bt("x", "see", 1)], [trial("x"), trial("x", false, false)])) == ["x": 1.0])
        #expect(rates(Benchmark.taskRates([bt("x", "see", 1)], [trial("x", false, false)])) == ["x": nil])
        let tasks = [bt("a", "see", 1), bt("b", "see", 2, [("status", "target")])]
        #expect(rates(Benchmark.taskRates(tasks, [])) == ["b": 0.0])
    }

    @Test func evaluateAxisWithoutTasksIsUnmeasured() {
        let report = Benchmark.evaluate([bt("a", "see", 5)], [trial("a")], benchConfig)
        #expect(report["axes"]?["read"]?["measured"] == false)
        #expect(report["axes"]?["see"]?["score"] == 5 && report["overall"] == 5)
    }

    @Test func evaluateTargetBlocksTheLadder() {
        let tasks = [bt("a", "see", 5), bt("b", "see", 50, [("status", "target")])]
        #expect(Benchmark.evaluate(tasks, [trial("a")], benchConfig)["axes"]?["see"]?["score"] == 5)
    }

    @Test func evaluateFlagsUnclassifiedAndNotRunButIgnoresControls() {
        let tasks = [bt("a", "see", 5), bt("b", "see", 6), ["id": "c", "request": "?"], ["id": "n", "negative": true]]
        let report = Benchmark.evaluate(tasks, [trial("a")], benchConfig)
        #expect(report["unclassified"] == ["c"] && report["not_run"] == ["b"])
    }

    @Test func unmeasuredLowLevelStopsTheLadder() {
        let tasks = [bt("easy", "see", 1), bt("hard", "see", 90)]
        let report = Benchmark.evaluate(tasks, [trial("easy", false, false), trial("hard")], benchConfig)
        #expect(report["axes"]?["see"]?["score"] == 0)
    }

    @Test func perfectRunOfEveryTaskScores100Overall() {
        let tasks = repoTasks.filter { $0["negative"]?.truthy != true }
        #expect(Benchmark.evaluate(tasks, tasks.map { trial($0["id"]!.str) }, repoConfig)["overall"] == 100)
    }

    @Test func evaluateIsDeterministic() {
        let tasks = [bt("a", "see", 5), bt("b", "read", 20)], trials = [trial("a"), trial("b", false)]
        #expect(Benchmark.evaluate(tasks, trials, benchConfig) == Benchmark.evaluate(tasks, trials, benchConfig))
    }

    @Test func axisWithOnlyUnrunTargetsIsUnmeasuredAndARunTargetIsMeasured() {
        let tasks = [bt("a", "see", 5), bt("b", "read", 40, [("status", "target")])]
        let unrun = Benchmark.evaluate(tasks, [trial("a")], benchConfig)
        #expect(unrun["axes"]?["read"]?["measured"] == false && unrun["overall"] == 5)
        #expect(Benchmark.evaluate(tasks, [trial("a"), trial("b")], benchConfig)["axes"]?["read"]?["score"] == 40)
    }

    @Test func reportCountsInvalidTrials() {
        let report = Benchmark.evaluate([bt("a", "see", 5)], [trial("a"), trial("a", false, false), trial("a", false, false)], benchConfig)
        #expect(report["invalid_trials"] == 2)
        #expect(Benchmark.render(report, ["weakest_axis": nil, "blocked": [:]]).contains("INVALID"))
    }

    @Test func liveFalseTasksAreNotScored() {
        let tasks = [bt("a", "see", 5), bt("pinned", "see", 6, [("live", false)])]
        let report = Benchmark.evaluate(tasks, [trial("a")], benchConfig)
        #expect(report["axes"]?["see"]?["score"] == 5 && report["not_run"] == [])
    }

    @Test func oneUnmeasuredTaskBlocksItsLevelEvenAmongPassingOnes() {
        let tasks = (0..<5).map { bt("l\($0)", "see", 10) } + [bt("next", "see", 20)]
        let trials = (0..<4).map { trial("l\($0)") } + [trial("next")]
        #expect(Benchmark.evaluate(tasks, trials, benchConfig)["axes"]?["see"]?["score"] == 0)
    }

    @Test func staleTasksDetectsChangedDefinition() {
        let task = bt("a", "see", 5)
        let good = String(sha(task.dumps(JSONDumpOptions(sortKeys: true))).prefix(12))
        #expect(Benchmark.staleTasks([task], ["a": .string(good)]) == [])
        var edited = task
        edited.set("level", 6)
        #expect(Benchmark.staleTasks([edited], ["a": .string(good)]) == ["a"])
    }

    @Test func latestLiveResultSkipsReplayAndOtherTaskFolders() throws {
        let dir = URL(fileURLWithPath: try temporaryDirectory())
        defer { try? FileManager.default.removeItem(at: dir) }
        try writeJSON(["versions": ["model": "live"], "trials": []], dir.appendingPathComponent("1.json"))
        try writeJSON(["versions": ["model": "scripted"], "trials": []], dir.appendingPathComponent("2.json"))
        #expect(Benchmark.latestLiveResult(dir)?.lastPathComponent == "1.json")
        try writeJSON(["versions": ["model": "live"], "trials": [trial("a")]], dir.appendingPathComponent("1.json"))
        try writeJSON(["versions": ["model": "live"], "trials": [trial("a~L2")]], dir.appendingPathComponent("2.json"))
        #expect(Benchmark.latestLiveResult(dir, ["a"])?.lastPathComponent == "1.json")
    }
}

@Suite struct BenchmarkGuidanceTests {
    @Test func guidancePicksLowestFailingLevelPerAxis() {
        let tasks = [bt("a", "see", 5), bt("b", "see", 9), bt("c", "see", 20)]
        var failing = trial("b", false)
        failing.set("problems", ["files: x"])
        let trials = [trial("a"), failing, trial("c", false)]
        let guide = Benchmark.guidance(Benchmark.evaluate(tasks, trials, benchConfig), tasks, trials)
        #expect(guide["blocked"]?["see"] == ["task": "b", "level": 9, "rate": 0.0, "problems": ["files: x"], "kind": "active"])
    }

    @Test func guidanceWeakestAxisIsLowestMeasuredScore() {
        let tasks = [bt("a", "see", 5), bt("b", "read", 20)], trials = [trial("a"), trial("b", false)]
        #expect(Benchmark.guidance(Benchmark.evaluate(tasks, trials, benchConfig), tasks, trials)["weakest_axis"] == "read")
    }

    @Test func guidanceIgnoresUnclassifiedTasks() {
        let tasks = [bt("a", "see", 5), ["id": "bad", "axis": "see"]], trials = [trial("a"), trial("bad", false)]
        #expect(Benchmark.guidance(Benchmark.evaluate(tasks, trials, benchConfig), tasks, trials)["blocked"] == [:])
    }

    @Test func guidanceMarksUnbuiltTargetsApartFromBrokenTasks() {
        let tasks = [bt("a", "see", 5), bt("b", "read", 40, [("status", "target")])]
        let trials = [trial("a"), trial("b", false)]
        let guide = Benchmark.guidance(Benchmark.evaluate(tasks, trials, benchConfig), tasks, trials)
        #expect(guide["blocked"]?["read"]?["kind"] == "target")
    }

    @Test func guidanceListsTheUnmeasuredLevelThatBlocksTheScore() {
        let tasks = [bt("easy", "see", 1), bt("mid", "see", 50)]
        let trials = [trial("easy", false, false), trial("mid")]
        let report = Benchmark.evaluate(tasks, trials, benchConfig)
        let guide = Benchmark.guidance(report, tasks, trials)
        let blocked = guide["blocked"]?["see"]
        #expect(blocked?["task"] == "easy" && blocked?["kind"] == "unmeasured" && blocked?["rate"] == .null)
        #expect(Benchmark.render(report, guide).contains("다시 잴 것: easy"))
    }

    @Test func compareReportsSignedDeltasAndSkipsOverallWhenMeasuredAxesDiffer() {
        #expect(Benchmark.compare(["overall": 40, "axes": ["see": ["score": 30]]], ["overall": 35, "axes": ["see": ["score": 20]]])
            == ["overall_delta": 5, "axis_delta": ["see": 10]])
        let new: JSON = ["overall": 10, "axes": ["see": ["score": 10], "read": ["score": nil]]]
        let old: JSON = ["overall": 55, "axes": ["see": ["score": 10], "read": ["score": 100]]]
        #expect(Benchmark.compare(new, old) == ["overall_delta": nil, "axis_delta": ["see": 0]])
    }

    @Test func renderShowsMeasuredAxesAndUnmeasuredMarker() {
        let text = Benchmark.render(Benchmark.evaluate([bt("a", "see", 40)], [trial("a")], benchConfig),
                                    ["weakest_axis": "see", "blocked": [:]])
        #expect(text.contains("see") && text.contains("40") && text.contains("측정 안 됨") && text.contains("█"))
    }

    @Test func renderListsNextGoalsByLevelWithCost() {
        let tasks = [bt("a", "see", 30), bt("b", "read", 10)]
        var a = trial("a", false), b = trial("b", false)
        a.set("model_calls", 3); a.set("seconds", 120)
        b.set("model_calls", 2); b.set("seconds", 60)
        let report = Benchmark.evaluate(tasks, [a, b], benchConfig)
        let text = Benchmark.render(report, Benchmark.guidance(report, tasks, [a, b]))
        #expect(text.range(of: "b (read 10")!.lowerBound < text.range(of: "a (see 30")!.lowerBound)
        #expect(text.contains("모델 호출 5회") && text.contains("3분"))
    }

    @Test func renderShortensLongNameLists() {
        var report = Benchmark.evaluate([bt("a", "see", 5)], [trial("a")], benchConfig)
        report.set("not_run", .array((0..<9).map { .string("t\($0)") }))
        let line = Benchmark.render(report, ["weakest_axis": nil, "blocked": [:]]).split(separator: "\n").first { $0.hasPrefix("미실행") }!
        #expect(line.contains("외 4개") && !line.contains("t8"))
    }
}

@Suite struct BenchmarkMainTests {
    @Test func refusesScriptedResults() throws {
        let root = try benchProject(model: "scripted")
        defer { try? FileManager.default.removeItem(at: root) }
        let out = try bench(root, [])
        #expect(out.status == 2 && out.stdout.contains("--live") && records(root).isEmpty)
    }

    @Test func writesRecordAndPrintsPath() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let out = try bench(root, [])
        let name = try #require(records(root).first)
        let record = try JSON.parse(Data(contentsOf: root.appendingPathComponent("evals/benchmarks/\(name)")))
        #expect(out.status == 0 && record["report"]?["overall"] == 5 && out.stdout.contains(name))
    }

    @Test func withoutResultsExplainsHowToGetThem() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.removeItem(at: resultFile(root))
        let out = try bench(root, [])
        #expect(out.status == 2 && out.stdout.contains("eval --live"))
    }

    @Test func doesNotOverwriteARecordFromTheSameSecond() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let paths = Benchmark.Paths(root: root)
        _ = Benchmark.main([], paths: paths, stamp: { "X" })
        _ = Benchmark.main([], paths: paths, stamp: { "X" })
        #expect(Set(records(root)) == ["X.json", "X-2.json"])
    }

    @Test func invalidShareDecidesWhetherToRecord() throws {
        for (trials, status) in [([trial("a", false, false)], Int32(2)),
                                 ([trial("a"), trial("zzz", false, false), trial("zzz", false, false)], 2),
                                 ([trial("a"), trial("a", false, false)], 0)] {
            let root = try benchProject(trials: trials)
            defer { try? FileManager.default.removeItem(at: root) }
            #expect(try bench(root, []).status == status)
            #expect(records(root).isEmpty == (status != 0))
        }
    }

    @Test func promptChangedNamesTasksWhoseInstructionsDiffer() {
        let old: [JSON] = [["task": "a", "prompt_hash": "h1"], ["task": "b", "prompt_hash": "h1"], ["task": "c"]]
        let new: [JSON] = [["task": "a", "prompt_hash": "h1"], ["task": "b", "prompt_hash": "h2"], ["task": "c", "prompt_hash": "h2"]]
        #expect(Benchmark.promptChanged(new, old) == ["b"])
    }

    @Test func compareIsSkippedWhenTheLadderChanged() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let old = root.appendingPathComponent("old.json")
        try writeJSON(["report": ["ladder": 1, "overall": 1, "axes": [:]]], old)
        let out = try bench(root, ["--compare", old.path])
        #expect(out.status == 0 && out.stdout.contains("사다리가 달라"))
    }

    @Test func noSaveKeepsNoRecord() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        #expect(try bench(root, ["--no-save"]).status == 0 && records(root).isEmpty)
    }

    @Test func tasksEditedAfterTheRunAreNotScored() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        var edited = bt("a", "see", 5)
        edited.set("level", 9)
        try writeJSON(["versions": ["model": "m", "tasks": ["a": .string(taskDigest(edited))]], "trials": [trial("a")]], resultFile(root))
        let out = try bench(root, ["--no-save"]).stdout
        #expect(out.contains("측정 안 됨") && out.contains("측정 뒤 바뀐 과제"))
    }

    @Test func refusesAResultFromAnotherTaskFolder() throws {
        let root = try benchProject(trials: [trial("zzz~L2")])
        defer { try? FileManager.default.removeItem(at: root) }
        let out = try bench(root, ["--no-save", "--result", resultFile(root).path])
        #expect(out.status == 2 && out.stdout.contains("다른 과제 폴더"))
    }

    @Test func compareAcceptsARawEvalResultAndRefusesAReplay() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let before = root.appendingPathComponent("before.json")
        try writeJSON(["versions": ["model": "m"], "trials": [trial("a", false)]], before)
        let out = try bench(root, ["--no-save", "--compare", before.path])
        #expect(out.status == 0 && out.stdout.contains("(+5)"))
        try writeJSON(["versions": ["model": "scripted"], "trials": [trial("a")]], before)
        let replay = try bench(root, ["--no-save", "--compare", before.path])
        #expect(replay.status == 2 && replay.stdout.contains("재생 결과"))
    }

    @Test func compareSkipsWhenTasksChangedOrWereRemoved() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        let before = root.appendingPathComponent("before.json")
        // an edited task, a saved record with an edited task, a removed task
        for earlier: JSON in [
            ["versions": ["model": "m", "tasks": ["a": "000000000000"]], "trials": [trial("a", false)]],
            ["report": ["ladder": nil, "overall": 1, "axes": ["see": ["score": 1]]], "versions": ["tasks": ["a": "000000000000"]]],
            ["versions": ["model": "m", "tasks": ["gone": "000000000000"]], "trials": [trial("gone", false)]],
        ] {
            try writeJSON(earlier, before)
            let out = try bench(root, ["--no-save", "--compare", before.path]).stdout
            #expect(out.contains("바뀌거나 지워져") && !out.contains("(+"))
        }
    }

    @Test func compareSkipsWhenThisResultHasTasksEditedSinceItsRun() throws {
        let root = try benchProject()
        defer { try? FileManager.default.removeItem(at: root) }
        try writeJSON(["versions": ["model": "m", "tasks": ["a": "000000000000"]], "trials": [trial("a")]], resultFile(root))
        let before = root.appendingPathComponent("before.json")
        try writeJSON(["versions": ["model": "m"], "trials": [trial("a", false)]], before)
        #expect(!(try bench(root, ["--no-save", "--compare", before.path]).stdout.contains("(+")))
    }

    /// Python test_autonomy `test_bench_job_picks_the_lowest_blocked_task_per_axis`, on the
    /// `--gaps` contract the feedback worker reads.
    @Test func benchJobPicksTheLowestBlockedTaskPerAxis() throws {
        let dir = URL(fileURLWithPath: try temporaryDirectory())
        defer { try? FileManager.default.removeItem(at: dir) }
        let result = dir.appendingPathComponent("r.json")
        var failing = trial("delete_pattern", false)
        failing.set("problems", ["x"])
        try writeJSON(["versions": ["model": "live"], "trials": [failing]], result)
        let done = try run([tool, "benchmark", "--result", result.path, "--gaps", "1", "--no-save"],
                           captureError: true, directory: repoRoot)
        let line = try #require(done.output.split(separator: "\n").last)
        #expect(line.hasPrefix("gaps "))
        let report = try JSON.parse(String(line.dropFirst(5)))
        let gaps = report["gaps"]?.list ?? []
        #expect(gaps.count == 1 && report.has("overall") && report.has("delta"))
        let keys = Set(gaps.first?.keys ?? [])
        #expect(keys.isSuperset(of: ["task", "axis", "level", "rate", "kind", "problems", "definition"]))
    }
}

@Suite struct TaskFileTests {
    let axes = repoConfig["axes"]?.keys ?? []
    var scored: [JSON] { repoTasks.filter { $0["negative"]?.truthy != true } }

    @Test func everyTaskIsClassified() {
        for task in scored {
            #expect(axes.contains(task["axis"]?.str ?? ""), "\(task["id"]!)")
            #expect((1...100).contains(task["level"]?.intValue ?? 0), "\(task["id"]!)")
            #expect(["active", "target"].contains(task["status"]?.str ?? ""), "\(task["id"]!)")
        }
    }

    @Test func taskIDsUniqueAndMatchFilename() throws {
        let files = try FileManager.default.contentsOfDirectory(atPath: repoRoot + "/evals/tasks")
            .filter { $0.hasSuffix(".json") }.sorted().map { String($0.dropLast(5)) }
        let ids = repoTasks.map { $0["id"]!.str }
        #expect(Set(ids).count == ids.count && ids == files)
    }

    @Test func targetTasksHaveExpectAndNoScriptAndActiveOnesHaveScripts() {
        #expect(scored.contains { $0["status"] == "target" })
        for task in scored where task["status"] == "target" { #expect(task.has("expect") && !task.has("script")) }
        for task in scored where task["status"] == "active" { #expect(task.has("script"), "\(task["id"]!)") }
    }

    @Test func everyAxisLadderIsFilledFromEasyToHard() {
        for axis in axes {
            let levels = scored.filter { $0["axis"]?.str == axis }.compactMap { $0["level"]?.intValue }.sorted()
            #expect(levels.count >= 8, "\(axis)")
            #expect(levels.first! <= 15 && levels.last! == 100, "\(axis)")
            #expect(zip(levels, levels.dropFirst()).map { $1 - $0 }.max()! <= 25, "\(axis)")
            let top = scored.filter { $0["axis"]?.str == axis && ($0["level"]?.intValue ?? 0) >= 85 }
            #expect(top.count >= 3 && top.contains { $0["level"] == 100 }, "\(axis)")
        }
    }

    @Test func theTopOfTheLaddersCoversRealWorldDifficulty() {
        let top = scored.filter { ($0["level"]?.intValue ?? 0) >= 60 }
        #expect(top.filter { ($0["files"]?.keys.count ?? 0) >= 8 }.count >= 3)
        #expect(top.contains { ($0["files"]?.objectValue ?? []).contains { $0.1.objectValue != nil } })
        #expect(top.filter { $0["ages"]?.truthy == true }.count >= 5)
        #expect(top.filter { $0["followups"]?.truthy == true || $0["expect"]?["undo"]?.truthy == true }.count >= 2)
        #expect(top.contains { $0["expect"]?["reply_excludes"]?.truthy == true })
    }

    @Test func taskFilesWithReplyChecksAreWellFormed() {
        for task in repoTasks {
            for key in ["reply_contains", "reply_excludes", "reply_order"] {
                #expect((task["expect"]?[key]?.list ?? []).allSatisfy { !($0.stringValue ?? "").isEmpty }, "\(task["id"]!) \(key)")
            }
        }
    }
}

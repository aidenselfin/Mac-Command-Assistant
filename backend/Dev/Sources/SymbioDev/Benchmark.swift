import Foundation
import SymbioCore

/// `symbio-dev benchmark` (Python `evals/benchmark.py`): capability score (1-100) per axis and
/// overall, computed from a live eval result. Replay results cannot be scored: their model
/// answers are scripted, so they always pass.
///
/// `--gaps N` (the feedback worker's score round) also prints, as the last stdout line,
/// `gaps {"overall": .., "delta": .., "gaps": [..]}`: delta against the newest earlier record and
/// the N lowest blocked tasks. Exit 0 = the result was trusted and its record saved.
public enum Benchmark {
    /// Where the files live; tests point it at a temporary folder.
    struct Paths {
        var config, tasks, results, benchmarks: URL
        init(root: URL) {
            let evals = root.appendingPathComponent("evals")
            config = evals.appendingPathComponent("benchmark.json")
            tasks = evals.appendingPathComponent("tasks")
            results = evals.appendingPathComponent("results")
            benchmarks = evals.appendingPathComponent("benchmarks")
        }
    }

    /// Python `float | None` per task id, in task order.
    typealias Rates = [(id: String, rate: Double?)]

    static func lookup(_ rates: Rates, _ id: String) -> Double?? { rates.first { $0.id == id }.map(\.rate) }

    /// Pass rate per task id over valid trials. A target task nobody ran counts as 0.
    static func taskRates(_ tasks: [JSON], _ trials: [JSON]) -> Rates {
        let byTask = Dictionary(grouping: trials) { $0["task"]?.str ?? "" }
        var rates: Rates = []
        for t in tasks {
            let id = t["id"]?.str ?? ""
            guard let runs = byTask[id], !runs.isEmpty else {
                if t["status"]?.stringValue == "target" { rates.append((id, 0)) }
                continue  // active task missing from the result = not run
            }
            let valid = runs.filter { $0["valid"]?.truthy == true }
            rates.append((id, valid.isEmpty ? nil : Double(valid.filter { $0["passed"]?.truthy == true }.count) / Double(valid.count)))
        }
        return rates
    }

    /// Highest level whose own tasks meet the threshold, with every lower level also met.
    /// Stops at the first level that falls short, so an easy pass above a failure never counts.
    static func axisScore(_ rates: [(level: Int, rate: Double?)], threshold: Double = 0.8) -> Int {
        let levels = Dictionary(grouping: rates, by: \.level)
        var score = 0
        for level in levels.keys.sorted() {
            let group = levels[level]!.map(\.rate)
            let known = group.compactMap { $0 }
            if known.count < group.count || known.reduce(0, +) / Double(group.count) < threshold { break }  // nil = could not measure
            score = level
        }
        return score
    }

    static func overall(_ axisScores: [(String, Int?)], _ weights: JSON) -> Int? {
        func weight(_ axis: String) -> Double { weights[axis]?.doubleValue ?? 1 }
        let measured = axisScores.compactMap { axis, score in score.map { (axis, $0) } }
        let total = measured.reduce(0.0) { $0 + weight($1.0) }
        if total == 0 { return nil }
        let value = measured.reduce(0.0) { $0 + Double($1.1) * weight($1.0) } / total
        if measured.count == weights.keys.count && measured.allSatisfy({ $0.1 == 100 }) { return 100 }
        return min(99, Int((value + 0.5).rounded(.down)))  // 100 is reserved for every axis being 100
    }

    static func isClassified(_ task: JSON, _ axes: [String]) -> Bool {
        guard let level = task["level"]?.intValue, axes.contains(task["axis"]?.stringValue ?? "\u{0}") else { return false }
        return 1 <= level && level <= 100
    }

    /// live:false tasks pin one scripted path and never run live, so they cannot be scored.
    static func graded(_ tasks: [JSON]) -> [JSON] {
        tasks.filter { $0["negative"]?.truthy != true && ($0["live"]?.truthy ?? true) }
    }

    static func evaluate(_ tasks: [JSON], _ trials: [JSON], _ config: JSON) -> JSON {
        let axes = config["axes"]?.keys ?? []
        let gradedTasks = graded(tasks)
        let classified = gradedTasks.filter { isClassified($0, axes) }
        let rates = taskRates(classified, trials)
        let ranIDs = Set(trials.map { $0["task"]?.str ?? "" })
        var pairs: [String: [(level: Int, rate: Double?)]] = [:]
        var ran = Set<String>()
        for t in classified {
            let id = t["id"]?.str ?? "", axis = t["axis"]?.str ?? ""
            if let rate = lookup(rates, id) ?? nil {
                pairs[axis, default: []].append((t["level"]?.intValue ?? 0, rate))
                if t["status"]?.stringValue != "target" || ranIDs.contains(id) {
                    ran.insert(axis)  // unrun targets alone must not make an axis "measured"
                }
            }
        }
        for t in classified {  // a level we could not measure must not be skipped over
            let id = t["id"]?.str ?? "", axis = t["axis"]?.str ?? ""
            if (lookup(rates, id) ?? nil) == nil && ran.contains(axis) {
                pairs[axis, default: []].append((t["level"]?.intValue ?? 0, nil))
            }
        }
        let threshold = config["threshold"]?.doubleValue ?? 0.8
        let scores: [(String, Int?)] = axes.map { ($0, ran.contains($0) ? axisScore(pairs[$0] ?? [], threshold: threshold) : nil) }
        let classifiedIDs = Set(classified.map { $0["id"]?.str ?? "" })
        let cost = (
            calls: trials.reduce(0) { $0 + ($1["model_calls"]?.intValue ?? 0) },
            seconds: trials.reduce(0.0) { $0 + ($1["seconds"]?.doubleValue ?? 0) })
        return [
            "axes": .object(scores.map { axis, score in
                (axis, ["score": score.map { .int($0) } ?? .null, "measured": .bool(score != nil),
                        "tasks": .int(pairs[axis]?.count ?? 0)])
            }),
            "overall": overall(scores, config["axes"] ?? [:]).map { .int($0) } ?? .null,
            "not_run": .array(classified.filter { lookup(rates, $0["id"]?.str ?? "") == nil }.map { $0["id"] ?? .null }),
            "unclassified": .array(gradedTasks.filter { t in !classified.contains(t) }.map { $0["id"] ?? .null }),
            "invalid_only": .array(rates.filter { $0.rate == nil }.map { .string($0.id) }),
            "ladder": config["ladder"] ?? .null,
            "cost": [
                "trials": .int(trials.count), "calls": .int(cost.calls),
                "seconds": .int(Int(cost.seconds.rounded(.toNearestOrEven))),
            ],
            "invalid_trials": .int(trials.filter {
                $0["valid"]?.truthy != true && classifiedIDs.contains($0["task"]?.str ?? "")
            }.count),
        ]
    }

    /// Tasks whose definition changed since the result was produced (hash as in eval).
    static func staleTasks(_ tasks: [JSON], _ recorded: JSON) -> [String] {
        tasks.compactMap { t in
            let id = t["id"]?.str ?? ""
            guard let old = recorded[id] else { return nil }
            return old.stringValue != taskDigest(t) ? id : nil
        }
    }

    /// Tasks edited or removed since a result/record was made: the ladders no longer match.
    static func drifted(_ tasks: [JSON], _ recorded: JSON) -> [String] {
        let current = Set(tasks.map { $0["id"]?.str ?? "" })
        return staleTasks(tasks, recorded) + recorded.keys.filter { !current.contains($0) }
    }

    /// Tasks run in both results whose instructions (eval's per-trial prompt_hash) differ.
    static func promptChanged(_ new: [JSON], _ old: [JSON]) -> [String] {
        func hashes(_ trials: [JSON]) -> [String: Set<String>] {
            trials.reduce(into: [:]) { out, t in
                if let hash = t["prompt_hash"]?.stringValue { out[t["task"]?.str ?? "", default: []].insert(hash) }
            }
        }
        let now = hashes(new), before = hashes(old)
        return now.keys.filter { before[$0] != nil && before[$0] != now[$0] }.sorted()
    }

    static func jsonFiles(_ directory: URL) -> [URL] {
        ((try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? [])
            .filter { $0.hasSuffix(".json") }.sorted().map { directory.appendingPathComponent($0) }
    }

    /// Newest live result that belongs to these tasks (variant runs use other task ids).
    static func latestLiveResult(_ directory: URL, _ taskIDs: Set<String>? = nil) -> URL? {
        for path in jsonFiles(directory).reversed() {
            guard let data = try? JSON.parse(Data(contentsOf: path)) else { continue }
            if data["versions"]?["model"]?.stringValue == "scripted" { continue }
            if taskIDs == nil || (data["trials"]?.list ?? []).contains(where: { taskIDs!.contains($0["task"]?.str ?? "") }) {
                return path
            }
        }
        return nil
    }

    /// Where to develop next: the weakest axis and, per axis, the lowest unmet task.
    /// Unmeasured levels (not run, or only INVALID) block the score, so they are listed too.
    static func guidance(_ report: JSON, _ tasks: [JSON], _ trials: [JSON], threshold: Double = 0.8) -> JSON {
        let axes = report["axes"]?.objectValue ?? []
        let classified = graded(tasks).filter { isClassified($0, axes.map(\.0)) }
        let rates = taskRates(classified, trials)
        var blocked: [(String, JSON)] = []
        for (axis, info) in axes where info["measured"]?.truthy == true {
            let failing = classified.filter {
                $0["axis"]?.str == axis && ((lookup(rates, $0["id"]?.str ?? "") ?? nil) ?? 0) < threshold
            }
            guard let t = failing.min(by: { ($0["level"]?.intValue ?? 0) < ($1["level"]?.intValue ?? 0) }) else { continue }
            let id = t["id"]?.str ?? ""
            let rate = lookup(rates, id) ?? nil
            let problems = Set(trials.filter {
                $0["task"]?.str == id && $0["valid"]?.truthy == true && $0["passed"]?.truthy != true
            }.flatMap { ($0["problems"]?.list ?? []) + ($0["violations"]?.list ?? []) }.map(\.str)).sorted()
            // active = something that should work is broken (fix); target = not built yet
            let kind = rate == nil ? "unmeasured" : t["status"]?.stringValue == "target" ? "target" : "active"
            blocked.append((axis, [
                "task": .string(id), "level": t["level"] ?? .null, "rate": rate.map { .double($0) } ?? .null,
                "problems": .array(problems.map { .string($0) }), "kind": .string(kind),
            ]))
        }
        let measured = axes.filter { $0.1["measured"]?.truthy == true }
        let weakest = measured.min { ($0.1["score"]?.intValue ?? 0) < ($1.1["score"]?.intValue ?? 0) }?.0
        return ["weakest_axis": weakest.map { .string($0) } ?? .null, "blocked": .object(blocked)]
    }

    static func compare(_ new: JSON, _ old: JSON) -> JSON {
        var axisDelta: [(String, JSON)] = []
        for (axis, info) in new["axes"]?.objectValue ?? [] {
            if let score = info["score"]?.intValue, let before = old["axes"]?[axis]?["score"]?.intValue {
                axisDelta.append((axis, .int(score - before)))
            }
        }
        // overalls over different measured axes are not comparable
        func measured(_ r: JSON) -> Set<String> {
            Set((r["axes"]?.objectValue ?? []).filter { !($0.1["score"]?.isNull ?? true) }.map(\.0))
        }
        var delta = JSON.null
        if let a = new["overall"]?.intValue, let b = old["overall"]?.intValue, measured(new) == measured(old) {
            delta = .int(a - b)
        }
        return ["overall_delta": delta, "axis_delta": .object(axisDelta)]
    }

    static func signed(_ n: Int) -> String { n >= 0 ? "+\(n)" : "\(n)" }

    static func render(_ report: JSON, _ guide: JSON, _ delta: JSON? = nil) -> String {
        var lines: [String] = []
        for (axis, info) in report["axes"]?.objectValue ?? [] {
            guard info["measured"]?.truthy == true, let score = info["score"]?.intValue else {
                lines.append("\(pad(axis, 10)) 측정 안 됨")
                continue
            }
            let filled = Int((Double(score) / 5).rounded(.toNearestOrEven))
            let change = delta?["axis_delta"]?[axis]?.intValue.map { " (\(signed($0)))" } ?? ""
            let number = String(score), padded = String(repeating: " ", count: max(0, 3 - number.count)) + number
            lines.append("\(pad(axis, 10)) \(String(repeating: "█", count: filled))\(String(repeating: "░", count: 20 - filled)) \(padded)\(change)")
        }
        var total = report["overall"]?.intValue.map(String.init) ?? "측정 안 됨"
        if let change = delta?["overall_delta"]?.intValue { total += " (\(signed(change)))" }
        lines.append("종합 \(total)")
        if let invalid = report["invalid_trials"]?.intValue, invalid > 0 { lines.append("INVALID 시도 \(invalid)회는 통과율에서 제외") }
        if let weakest = guide["weakest_axis"]?.stringValue { lines.append("가장 약한 축: \(weakest)") }
        let ordered = (guide["blocked"]?.objectValue ?? []).enumerated()
            .sorted { ($0.1.1["level"]?.intValue ?? 0, $0.0) < ($1.1.1["level"]?.intValue ?? 0, $1.0) }.map(\.1)
        for (axis, b) in ordered.prefix(3) {
            let what = ["target": "만들 것", "active": "고칠 것", "unmeasured": "다시 잴 것"][b["kind"]?.str ?? ""] ?? ""
            let rate = b["rate"]?.doubleValue.map { String(format: "통과 %.0f%%", $0 * 100) } ?? "측정 없음"
            lines.append("\(what): \(b["task"]?.str ?? "") (\(axis) \(b["level"]?.intValue ?? 0)단계, \(rate))")
        }
        if let cost = report["cost"], let trials = cost["trials"]?.intValue, trials > 0 {
            let seconds = cost["seconds"]?.intValue ?? 0
            lines.append("측정 비용: 시도 \(trials)회, 모델 호출 \(cost["calls"]?.intValue ?? 0)회, \(seconds / 60)분")
        }
        let notes = [("not_run", "미실행"), ("unclassified", "미분류"), ("invalid_only", "INVALID뿐"),
                     ("stale", "측정 뒤 바뀐 과제(미실행 처리)")]
        for (key, label) in notes {
            let names = (report[key]?.list ?? []).map(\.str)
            if names.isEmpty { continue }
            let more = names.count > 5 ? " 외 \(names.count - 5)개" : ""
            lines.append("\(label) \(names.count)개: \(names.prefix(5).joined(separator: ", "))\(more)")
        }
        return lines.joined(separator: "\n")
    }

    /// The feedback worker's view of a round: change against the newest record, then the `top`
    /// blocked tasks, weakest axis first, lowest level first.
    static func gaps(_ report: JSON, _ guide: JSON, _ tasks: [JSON], previous: URL?, top: Int) -> JSON {
        var delta = JSON.null
        if let previous, let old = try? JSON.parse(Data(contentsOf: previous)), let oldReport = old["report"] {
            let same = drifted(tasks, old["versions"]?["tasks"] ?? [:]).isEmpty
            if same && oldReport["ladder"] == report["ladder"] { delta = compare(report, oldReport) }
        }
        func score(_ axis: String) -> Int { report["axes"]?[axis]?["score"]?.intValue ?? 0 }
        let blocked = (guide["blocked"]?.objectValue ?? []).enumerated().sorted {
            (score($0.1.0), $0.1.1["level"]?.intValue ?? 0, $0.0) < (score($1.1.0), $1.1.1["level"]?.intValue ?? 0, $1.0)
        }.map(\.1)
        let gaps: [JSON] = blocked.prefix(max(top, 0)).map { axis, b in
            let task = tasks.first { $0["id"] == b["task"] } ?? .null
            var gap = b
            gap.set("axis", .string(axis))
            gap.set("definition", .string(String(task.text.unicodeScalars.prefix(800))))
            return gap
        }
        return ["overall": report["overall"] ?? .null, "delta": delta, "gaps": .array(gaps)]
    }

    public static func main(_ argv: [String], root: URL = projectRoot()) -> Int32 {
        main(argv, paths: Paths(root: root))
    }

    static func main(_ argv: [String], paths: Paths, stamp: () -> String = { timestamp() }) -> Int32 {
        let args: Arguments
        do { args = try Arguments(argv, switches: ["--no-save"]) } catch {
            writeError("usage: symbio-dev benchmark [--result FILE] [--compare FILE] [--no-save] [--gaps N]\n")
            return 2
        }
        let tasks = loadJSONFiles(paths.tasks)
        let ids = Set(tasks.map { $0["id"]?.str ?? "" })
        guard let result = args.last("--result").map({ URL(fileURLWithPath: $0) }) ?? latestLiveResult(paths.results, ids) else {
            writeOutput("결과 없음. 먼저 `symbio-dev eval --live --repeat 3`를 실행하세요.\n")
            return 2
        }
        guard let data = try? JSON.parse(Data(contentsOf: result)) else {
            writeOutput("결과 파일을 읽지 못했습니다: \(result.path)\n")
            return 2
        }
        let allTrials = data["trials"]?.list ?? []
        if !allTrials.contains(where: { ids.contains($0["task"]?.str ?? "") }) {
            writeOutput("이 결과는 다른 과제 폴더(변형 과제 등)의 것이라 채점하지 않습니다.\n")
            return 2
        }
        if data["versions"]?["model"]?.stringValue == "scripted" {
            writeOutput("재생 결과는 능력을 재지 못합니다. --live 결과를 쓰세요.\n")
            return 2
        }
        guard let config = try? JSON.parse(Data(contentsOf: paths.config)) else {
            writeOutput("벤치마크 설정을 읽지 못했습니다: \(paths.config.path)\n")
            return 2
        }
        let threshold = config["threshold"]?.doubleValue ?? 0.8
        // a task edited after the run no longer matches its result: score it as not run
        let stale = staleTasks(tasks, data["versions"]?["tasks"] ?? [:])
        let trials = allTrials.filter { !stale.contains($0["task"]?.str ?? "") }
        var report = evaluate(tasks, trials, config)
        report.set("stale", .array(stale.map { .string($0) }))
        let guide = guidance(report, tasks, trials, threshold: threshold)
        var delta: JSON?
        if let compareWith = args.last("--compare") {
            guard let earlier = try? JSON.parse(Data(contentsOf: URL(fileURLWithPath: compareWith))) else {
                writeOutput("비교할 기록을 읽지 못했습니다: \(compareWith)\n")
                return 2
            }
            if earlier["versions"]?["model"]?.stringValue == "scripted" {
                writeOutput("재생 결과는 비교할 수 없습니다.\n")
                return 2
            }
            let changed = promptChanged(allTrials, earlier["trials"]?.list ?? [])
            if !changed.isEmpty {
                writeOutput("지침이 바뀐 과제 \(changed.count)개: \(changed.joined(separator: ", "))\n")
            }
            if !drifted(tasks, earlier["versions"]?["tasks"] ?? [:]).isEmpty || !stale.isEmpty {  // ladders no longer match
                writeOutput("과제가 바뀌거나 지워져 점수 변화를 비교하지 않습니다.\n")
            } else {
                let old = earlier["report"] ?? evaluate(tasks, earlier["trials"]?.list ?? [], config)
                if old["ladder"] == report["ladder"] {
                    delta = compare(report, old)
                } else {
                    writeOutput("과제 사다리가 달라 이전 기록과 비교하지 않습니다.\n")
                }
            }
        }
        let gapsLine = args.last("--gaps").flatMap(Int.init).map {
            "gaps " + gaps(report, guide, tasks, previous: jsonFiles(paths.benchmarks).last, top: $0).text + "\n"
        }
        writeOutput(render(report, guide, delta) + "\n")
        defer { if let gapsLine { writeOutput(gapsLine) } }
        if allTrials.filter({ $0["valid"]?.truthy != true }).count * 2 > allTrials.count {
            writeOutput("INVALID가 절반을 넘어 점수를 믿을 수 없습니다. 기록하지 않습니다(키·네트워크 확인).\n")
            return 2
        }
        if args.switches.contains("--no-save") { return 0 }
        let record: JSON = [
            "versions": data["versions"] ?? .null, "source": .string(result.lastPathComponent),
            "report": report, "guidance": guide,
        ]
        let body = Data(record.dumps(JSONDumpOptions(ensureASCII: false, indent: 2)).utf8)
        do {
            try FileManager.default.createDirectory(at: paths.benchmarks, withIntermediateDirectories: true)
            var n = 1
            while true {  // exclusive create: never overwrite a record, even from a concurrent run
                let out = paths.benchmarks.appendingPathComponent(stamp() + (n == 1 ? "" : "-\(n)") + ".json")
                do {
                    try body.write(to: out, options: .withoutOverwriting)
                    writeOutput("saved \(out.lastPathComponent)\n")
                    return 0
                } catch CocoaError.fileWriteFileExists {
                    n += 1
                }
            }
        } catch {
            writeOutput("기록을 저장하지 못했습니다: \(error)\n")
            return 1
        }
    }
}

import Foundation
import SymbioCore

/// Check that a screen change really reached the app, without a person looking (`scripts/ui_check.py`).
///
/// symbio-dev ui-check status        # is the installed app built from the latest code?
/// symbio-dev ui-check run [이름...]  # replay scripts/ui_scenarios/*.json on a new build
///
/// `run` builds this checkout's app and starts it with a scripted backend (this tool's `serve`).
/// The script sends the same events the backend would, presses keys and clicks like the user, then
/// asks the app what is on screen (chat window, type box, note, chips, file box) and compares.
public enum UICheck {
    static let scenarios = repoRoot + "/scripts/ui_scenarios"

    static var mainCheckout: String {
        let common = (try? run(["git", "-C", repoRoot, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                               captureError: true).output) ?? ""
        return URL(fileURLWithPath: common.trimmingCharacters(in: .whitespacesAndNewlines))
            .deletingLastPathComponent().path
    }

    public static func main(_ argv: [String]) -> Int32 {
        let command = argv.first ?? "status"
        let rest = Array(argv.dropFirst())
        do {
            if command == "serve" {
                guard rest.count == 2 else {
                    writeError("serve에는 시나리오와 결과 파일 경로가 필요합니다\n")
                    return 1
                }
                try serve(scenario: rest[0], result: rest[1])
                return 0
            }
            return command == "status" ? status() : try runScenarios(rest)
        } catch {
            writeError("\(error)\n")
            return 1
        }
    }

    static func status() -> Int32 {
        let main = mainCheckout
        let apps = [NSHomeDirectory() + "/Applications/Symbio.app", main + "/native-preview/.build/FinderChatPreview.app"]
        let latest = (try? run(["git", "-C", main, "rev-parse", "main:native-preview"], captureError: true).output)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let running = (try? run(["ps", "-axwwo", "command="], captureError: true).output) ?? ""
        var stale = false
        for app in apps {
            let runtime = app + "/Contents/Resources/runtime.json"
            guard let data = FileManager.default.contents(atPath: runtime) else {
                writeOutput("없음   \(app)\n")
                stale = stale || app == apps[0]  // the installed app is the one the user opens
                continue
            }
            let built = (try? JSON.parse(data))?["app_tree"]
            let ok = built?.stringValue == latest
            stale = stale || !ok
            let state = ok ? "최신" : ((built == nil || built == .null) ? "옛 빌드(버전 기록 없음)" : "옛 빌드")
            let on = running.contains(app + "/Contents/MacOS") ? " · 실행 중" : ""
            let padded = state + String(repeating: " ", count: max(0, 6 - state.count))
            writeOutput("\(padded) \(app)\(on)\n")
        }
        if stale { writeOutput("→ native-preview/install-app.sh 를 실행하면 최신으로 바뀝니다.\n") }
        return stale ? 1 : 0
    }

    // ---- scripted backend (runs inside the app as its backend) ----

    static func serve(scenario scenarioPath: String, result resultPath: String) throws {
        let scenario = try JSON.parse(try Data(contentsOf: URL(fileURLWithPath: scenarioPath)))
        // Not the system temp folder: the app sees /private/var as /var and would refuse every file.
        var template = Array((repoRoot + "/native-preview/.build/ui-check-XXXXXXXX").utf8CString)
        guard let made = mkdtemp(&template) else { throw CommandFailed(command: ["mkdtemp"], status: errno) }
        let folder = String(cString: made)
        var failures: [String] = []
        var sent: [JSON] = []

        func emit(_ value: JSON) {
            let text = value.dumps(JSONDumpOptions(ensureASCII: false)).replacingOccurrences(of: "{dir}", with: folder)
            writeOutput(text + "\n")
        }
        func readUntil(_ action: String) -> JSON? {
            while let line = readLine(strippingNewline: true) {
                guard let value = try? JSON.parse(line) else { continue }
                if value["action"]?.stringValue == action { return value }
                sent.append(value)
            }
            return nil
        }

        emit(["type": "config", "folders": [.string(folder)], "finder_reveal": false])
        emit(["type": "state", "state": "IDLE"])
        Thread.sleep(forTimeInterval: 1)  // let launch focus settle: a type box that loses focus closes itself
        for (offset, step) in (scenario["steps"]?.arrayValue ?? []).enumerated() {
            let number = offset + 1
            for name in step["touch"]?.arrayValue?.compactMap(\.stringValue) ?? [] {
                try name.write(toFile: folder + "/" + name, atomically: false, encoding: .utf8)
            }
            if let event = step["emit"] { emit(event) }
            if let action = step["do"] {
                emit(.object([("type", "_test")] + (step.objectValue ?? [])))
                let done = readUntil("_done")
                if done?["ok"]?.boolValue != true {
                    failures.append("\(number)단계 \(pythonRepr(action, bare: true)): 앱이 동작을 하지 못함")
                }
            }
            Thread.sleep(forTimeInterval: step["wait"]?.doubleValue ?? 0.3)
            if let expect = step["expect"]?.objectValue {
                emit(["type": "_test", "do": "snapshot"])
                let reply = readUntil("_snapshot")
                let shown = reply?["state"] ?? [:]
                let actions = JSON.array(sent.map { $0["action"] ?? .null })
                sent.removeAll()
                for (key, want) in expect {
                    let got = key == "sent" ? actions : (shown[key] ?? .null)
                    let good = key == "sent"
                        ? (want.arrayValue ?? []).allSatisfy { (actions.arrayValue ?? []).contains($0) }
                        : got == want
                    if !good {
                        failures.append("\(number)단계 \(key): 기대 \(pythonRepr(want)), 실제 \(pythonRepr(got))")
                    }
                }
            }
        }
        try? FileManager.default.removeItem(atPath: folder)
        let result: JSON = ["failures": .array(failures.map { .string($0) })]
        try result.dumps(JSONDumpOptions(ensureASCII: false)).write(toFile: resultPath, atomically: false, encoding: .utf8)
        emit(["type": "_test", "do": "quit"])
        _ = readUntil("shutdown")
    }

    /// Python `repr()` of a JSON value, as the failure messages show it. `bare` prints a string unquoted (f-string).
    static func pythonRepr(_ value: JSON, bare: Bool = false) -> String {
        switch value {
        case .null: return "None"
        case let .bool(flag): return flag ? "True" : "False"
        case let .int(number): return String(number)
        case let .double(number): return JSON.double(number).dumps()
        case let .string(text):
            if bare { return text }
            let quote: Character = text.contains("'") && !text.contains("\"") ? "\"" : "'"
            var out = String(quote)
            for scalar in text.unicodeScalars {
                switch scalar {
                case "\\": out += "\\\\"
                case "\n": out += "\\n"
                case "\r": out += "\\r"
                case "\t": out += "\\t"
                default:
                    if Character(scalar) == quote { out += "\\" + String(quote) }
                    else if scalar.value < 0x20 || scalar.value == 0x7F { out += String(format: "\\x%02x", scalar.value) }
                    else { out.unicodeScalars.append(scalar) }
                }
            }
            return out + String(quote)
        case let .array(items): return "[" + items.map { pythonRepr($0) }.joined(separator: ", ") + "]"
        case let .object(pairs):
            return "{" + pairs.map { "\(pythonRepr(.string($0.0))): \(pythonRepr($0.1))" }.joined(separator: ", ") + "}"
        }
    }

    static func runScenarios(_ names: [String]) throws -> Int32 {
        HeavyLock.acquire()
        let package = repoRoot + "/native-preview"
        let build = try run(["swift", "build", "--package-path", package], captureError: true)
        if build.status != 0 {
            writeOutput(String(build.output.suffix(2000)) + String(build.error.suffix(2000)) + "\n")
            return 1
        }
        let binPath = try run(["swift", "build", "--package-path", package, "--show-bin-path"], captureError: true)
            .output.trimmingCharacters(in: .whitespacesAndNewlines)
        let binary = binPath + "/FinderChatPreview"
        var paths = names.map { scenarios + "/\($0).json" }
        if paths.isEmpty {
            paths = ((try? FileManager.default.contentsOfDirectory(atPath: scenarios)) ?? [])
                .filter { $0.hasSuffix(".json") }.sorted().map { scenarios + "/" + $0 }
        }
        let me = Bundle.main.executablePath ?? CommandLine.arguments[0]
        let work = FileManager.default.temporaryDirectory.appendingPathComponent("ui-check-\(UUID().uuidString)").path
        try FileManager.default.createDirectory(atPath: work, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(atPath: work) }
        var failed = 0
        for path in paths {
            let stem = ((path as NSString).lastPathComponent as NSString).deletingPathExtension
            let result = work + "/\(stem).result.json", runtime = work + "/runtime.json"
            let config: JSON = [
                "project": .string(repoRoot), "backend": .string(me),
                "args": ["ui-check", "serve", .string(path), .string(result)],
            ]
            try config.dumps().write(toFile: runtime, atomically: false, encoding: .utf8)
            let app = Process()
            app.executableURL = URL(fileURLWithPath: binary)
            var env = ProcessInfo.processInfo.environment
            env["SYMBIO_UI_TEST_RUNTIME"] = runtime
            app.environment = env
            app.standardOutput = FileHandle.nullDevice
            app.standardError = FileHandle.nullDevice
            try app.run()
            let deadline = Date().addingTimeInterval(60)
            while app.isRunning, Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            var code = "0"
            if app.isRunning {
                kill(app.processIdentifier, SIGKILL)
                app.waitUntilExit()
                code = "시간 초과"
            } else {
                code = String(exitStatus(app))
            }
            var problems: [String] = ["시나리오를 끝까지 돌지 못함"]
            if let data = FileManager.default.contents(atPath: result) {
                problems = (try JSON.parse(data))["failures"]?.arrayValue?.compactMap(\.stringValue) ?? []
            }
            if code != "0" { problems.append("앱이 정상 종료하지 못함(\(code))") }
            if !problems.isEmpty { failed += 1 }
            writeOutput((problems.isEmpty ? "PASS " : "FAIL ") + stem + "\n")
            for problem in problems { writeOutput("     " + problem + "\n") }
        }
        return failed > 0 ? 1 : 0
    }
}

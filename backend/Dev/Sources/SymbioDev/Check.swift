import Foundation

/// `symbio-dev check [--fast]` (Python `scripts/check.py`): one command to check a change before
/// reporting it done. Every backend package is built and tested (a warning fails the step), the
/// native app is built, then the eval replay and the harder generated cases run. `--fast` skips
/// the tests. One `PASS|FAIL  <name>  <summary>` line per step; exit 1 if any failed.
/// No screen is opened: UI changes still need a person to look at the app.
public enum Check {
    static func steps(root: URL, fast: Bool, tool: String) -> [(name: String, command: [String])] {
        let backend = root.appendingPathComponent("backend")
        let packages = ((try? FileManager.default.contentsOfDirectory(atPath: backend.path)) ?? []).sorted()
            .filter { FileManager.default.fileExists(atPath: backend.path + "/\($0)/Package.swift") }
        var steps: [(String, [String])] = []
        for name in packages {
            let path = backend.path + "/" + name
            steps.append(("build \(name)", ["swift", "build", "--package-path", path]))
            if !fast { steps.append(("test \(name)", ["swift", "test", "--package-path", path])) }
        }
        steps.append(("build native-preview", ["swift", "build", "--package-path", root.path + "/native-preview"]))
        steps.append(("eval replay", [tool, "eval"]))
        steps.append(("make cases", [tool, "make-cases"]))
        for level in 1...3 { steps.append(("replay L\(level)", [tool, "eval", "--dir", "evals/generated/L\(level)"])) }
        return steps
    }

    public static func main(_ argv: [String], root: URL = projectRoot()) -> Int32 {
        HeavyLock.acquire()
        let tool = Bundle.main.executablePath ?? CommandLine.arguments[0]
        var failed = false
        for (name, command) in steps(root: root, fast: argv.contains("--fast"), tool: tool) {
            let done: (status: Int32, output: String, error: String)
            do { done = try run(command, captureError: true, directory: root.path) } catch {
                done = (127, "", "\(error)")
            }
            let lines = (done.output + done.error).split(separator: "\n").map(String.init)
            // ponytail: an incremental build repeats no warnings for unchanged files; a clean
            // build (-Xswiftc -warnings-as-errors) would, at the cost of rebuilding every package.
            let warnings = lines.filter { $0.contains("warning:") }
            let summary = lines.filter {
                ["passed", "L1", "L2", "L3", "Build complete"].contains(where: $0.hasPrefix) || $0.contains("Test run with")
            }
            let ok = done.status == 0 && warnings.isEmpty
            // stdout first: the MCP server's start-up chatter on stderr says nothing about the failure
            let out = done.output.split(separator: "\n").map(String.init)
            let tail = !warnings.isEmpty ? Array(warnings.prefix(3)) : Array((out.isEmpty ? lines : out).suffix(3))
            writeOutput("\(ok ? "PASS" : "FAIL")  \(name)  \((summary.isEmpty ? tail : summary).last ?? "")\n")
            if !ok {
                failed = true
                writeOutput(tail.map { "      \($0)\n" }.joined())
            }
        }
        return failed ? 1 : 0
    }
}

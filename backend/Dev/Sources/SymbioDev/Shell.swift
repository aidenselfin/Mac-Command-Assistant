import Foundation

/// The repository this tool was built from (Python scripts used `Path(__file__).parent.parent`).
public let repoRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent().path

/// Unbuffered so the order matches child processes that share the terminal.
public func writeOutput(_ text: String) { FileHandle.standardOutput.write(Data(text.utf8)) }
public func writeError(_ text: String) { FileHandle.standardError.write(Data(text.utf8)) }

public struct CommandFailed: Error, CustomStringConvertible {
    public let command: [String]
    public let status: Int32
    public var description: String { "Command \(command) returned non-zero exit status \(status)." }
}

/// Python `returncode`: a signal is reported as its negative number.
func exitStatus(_ process: Process) -> Int32 {
    process.terminationReason == .uncaughtSignal ? -process.terminationStatus : process.terminationStatus
}

/// Runs a program found on PATH. Captured streams go through temp files so large output never blocks.
@discardableResult
func run(
    _ command: [String], environment: [String: String]? = nil,
    captureOutput: Bool = true, captureError: Bool = false
) throws -> (status: Int32, output: String, error: String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = command
    if let environment { process.environment = environment }
    func capture(_ on: Bool) throws -> (FileHandle, URL)? {
        guard on else { return nil }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        FileManager.default.createFile(atPath: url.path, contents: nil)
        return (try FileHandle(forWritingTo: url), url)
    }
    let out = try capture(captureOutput), err = try capture(captureError)
    if let out { process.standardOutput = out.0 }
    if let err { process.standardError = err.0 }
    try process.run()
    process.waitUntilExit()
    func read(_ file: (FileHandle, URL)?) -> String {
        guard let file else { return "" }
        try? file.0.close()
        defer { try? FileManager.default.removeItem(at: file.1) }
        return (try? String(contentsOf: file.1, encoding: .utf8)) ?? ""
    }
    return (exitStatus(process), read(out), read(err))
}

/// `subprocess.check_output(["git", "-C", repo, ...]).decode().strip()`: stderr goes to the terminal.
func git(_ repo: String, _ args: String..., environment: [String: String]? = nil) throws -> String {
    let command = ["git", "-C", repo] + args
    let result = try run(command, environment: environment)
    guard result.status == 0 else { throw CommandFailed(command: command, status: result.status) }
    return result.output.trimmingCharacters(in: .whitespacesAndNewlines)
}

/// `Path.resolve()` (strict=False): follows symlinks, keeps the missing tail as written.
func resolvePath(_ path: String) -> String {
    var result = ""
    var parts = path.split(separator: "/").map(String.init)
    var hops = 0
    while !parts.isEmpty {
        let part = parts.removeFirst()
        if part == "." { continue }
        if part == ".." { result = (result as NSString).deletingLastPathComponent; continue }
        let next = result + "/" + part
        if hops < 40, let target = try? FileManager.default.destinationOfSymbolicLink(atPath: next) {
            hops += 1
            if target.hasPrefix("/") { result = "" }
            parts = target.split(separator: "/").map(String.init) + parts
        } else {
            result = next
        }
    }
    return result.isEmpty ? "/" : result
}

func isInside(_ path: String, _ root: String) -> Bool {
    path == root || path.hasPrefix(root == "/" ? "/" : root + "/")
}

func isDirectory(_ path: String) -> Bool {
    var directory: ObjCBool = false
    return FileManager.default.fileExists(atPath: path, isDirectory: &directory) && directory.boolValue
}

func isSymlink(_ path: String) -> Bool {
    (try? FileManager.default.attributesOfItem(atPath: path)[.type] as? FileAttributeType) == .typeSymbolicLink
}

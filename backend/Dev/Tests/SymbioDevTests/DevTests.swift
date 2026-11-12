import Foundation
import SymbioCore
import Testing
@testable import SymbioDev

/// The built `symbio-dev`, in the products folder that holds this test binary's bundle.
let tool: String = {
    var info = Dl_info()
    dladdr(#dsohandle, &info)
    var folder = URL(fileURLWithPath: String(cString: info.dli_fname))
    repeat { folder.deleteLastPathComponent() } while folder.pathExtension != "xctest" && folder.path != "/"
    return folder.deletingLastPathComponent().appendingPathComponent("symbio-dev").path
}()

struct Output { let status: Int32; let stdout: String; let stderr: String }

func symbio(_ args: [String], environment: [String: String]? = nil) throws -> Output {
    let result = try run([tool] + args, environment: environment, captureError: true)
    return Output(status: result.status, stdout: result.output, stderr: result.error)
}

func temporaryDirectory() throws -> String {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("dev-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return resolvePath(url.path)  // git reports /private/var, not /var
}

func write(_ text: String, _ path: String) throws { try text.write(toFile: path, atomically: false, encoding: .utf8) }

/// Exercise ownership and recovery against a disposable repository, never the real repo.
@Suite struct VibeTests {
    @Test func ownershipAndSnapshots() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(atPath: root) }
        let tmp = root + "/repo"
        try FileManager.default.createDirectory(atPath: tmp, withIntermediateDirectories: false)
        func git(_ args: String...) throws -> String { try gitIn(tmp, args) }
        func vibe(_ command: String, _ args: String...) throws -> Output {
            try symbio(["vibe", command, "--repo", tmp] + args)
        }

        _ = try git("init", "-q")
        _ = try git("config", "user.email", "test@example.invalid")
        _ = try git("config", "user.name", "Test")
        try write("original", tmp + "/code.txt")
        try write("*.env\n", tmp + "/.gitignore")
        _ = try git("add", "code.txt", ".gitignore")
        _ = try git("commit", "-qm", "initial")
        let head = try git("rev-parse", "HEAD")
        try write("private", tmp + "/user.txt")
        try write("secret", tmp + "/key.env")
        for invalid in [".", "../outside", ".git/config", "key.env"] {
            #expect(try vibe("begin", "--agent", "A", "--goal", "fix", "--files", invalid).status != 0)
        }
        let outside = root + "/outside-vibe"
        try write("outside", outside)
        try FileManager.default.createSymbolicLink(atPath: tmp + "/link", withDestinationPath: outside)
        #expect(try vibe("begin", "--agent", "A", "--goal", "fix", "--files", "link").status != 0)

        let args = ["vibe", "begin", "--repo", tmp, "--agent", "A", "--goal", "fix",
                    "--files", "code.txt", "new.txt", "literal*.txt"]
        async let first = symbio(args)
        async let second = symbio(args)
        let results = try await [first, second]
        #expect(results.map(\.status).sorted() == [0, 1])
        let token = results.first { $0.status == 0 }!.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        #expect(try JSON.parse(vibe("status").stdout)["token"]?.stringValue == token)
        #expect(try git("show", "refs/vibe/snapshots:code.txt") == "original")
        try write("staged", tmp + "/code.txt")
        _ = try git("add", "code.txt")
        let index = FileManager.default.contents(atPath: tmp + "/.git/index")
        try write("working", tmp + "/code.txt")
        try write("new", tmp + "/new.txt")
        try write("literal", tmp + "/literal*.txt")
        try write("private", tmp + "/literal-private.txt")
        let saved = try vibe("snapshot")
        #expect(saved.status == 0, "\(saved.stderr)")
        #expect(try git("rev-parse", "HEAD") == head)
        #expect(FileManager.default.contents(atPath: tmp + "/.git/index") == index)
        let ref = "refs/vibe/snapshots"
        #expect(try git("show", "\(ref):code.txt") == "working")
        #expect(try git("show", "\(ref):new.txt") == "new")
        let names = try git("ls-tree", "--name-only", ref)
        #expect(!names.contains("user.txt"))
        #expect(!names.contains("key.env"))
        #expect(!names.contains("literal-private.txt"))
        #expect(try git("show", "\(ref):literal*.txt") == "literal")
        #expect(try vibe("snapshot").stdout == saved.stdout)
        try FileManager.default.removeItem(atPath: tmp + "/code.txt")
        #expect(try vibe("snapshot").status == 0)
        #expect(try !git("ls-tree", "--name-only", ref).contains("code.txt"))
        let savedCommit = saved.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        #expect(try git("show", "\(savedCommit):code.txt") == "working")
        #expect(try vibe("end", "--token", "wrong").status == 1)
        #expect(try vibe("end", "--token", token).status == 0)
        #expect(try JSON.parse(vibe("status").stdout) == [:])
        #expect(try vibe("end", "--token", token).status == 1)
    }

    @Test func linkedWorktreesHaveIndependentSessions() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(atPath: root) }
        let main = root + "/app"
        try FileManager.default.createDirectory(atPath: main, withIntermediateDirectories: false)
        func git(_ args: String..., cwd: String = main) throws -> String { try gitIn(cwd, args) }
        func vibe(_ command: String, _ args: String..., cwd: String = main) throws -> Output {
            try symbio(["vibe", command, "--repo", cwd] + args)
        }

        _ = try git("init", "-q", "-b", "main")
        _ = try git("config", "user.email", "test@example.invalid")
        _ = try git("config", "user.name", "Test")
        try write("original", main + "/code.txt")
        try FileManager.default.createDirectory(atPath: main + "/node_modules", withIntermediateDirectories: false)
        try write("{}", main + "/config.json")  // untracked local file a worktree must see
        try write("config.json\n", main + "/.gitignore")
        _ = try git("add", "code.txt", ".gitignore")
        _ = try git("commit", "-qm", "initial")

        let made = try vibe("worktree", "--name", "feature", "--base", "HEAD")
        #expect(made.status == 0, "\(made.stderr)")
        let other = root + "/app-worktrees/feature"
        #expect(isSymlink(other + "/config.json"))
        #expect(try !git("status", "--porcelain", cwd: other).contains("node_modules"))

        let first = try vibe("begin", "--agent", "A", "--goal", "one", "--files", "code.txt")
        #expect(first.status == 0, "\(first.stderr)")
        // A session in the main checkout does not block one in the worktree ...
        let second = try vibe("begin", "--agent", "B", "--goal", "two", "--files", "code.txt", cwd: other)
        #expect(second.status == 0, "\(second.stderr)")
        // ... but the shared file is reported, and each checkout keeps its own snapshot ref.
        #expect(second.stderr.contains("경고"))
        #expect(try vibe("sessions", cwd: other).stdout.contains("\tA\tone"))
        let third = try vibe("begin", "--agent", "C", "--goal", "three", "--files", "code.txt", cwd: other)
        #expect(third.status == 1)  // one writer per checkout
        try write("changed in worktree", other + "/code.txt")
        #expect(try vibe("snapshot", cwd: other).status == 0)
        #expect(try git("show", "refs/vibe/snapshots-feature:code.txt") == "changed in worktree")
        #expect(try git("show", "refs/vibe/snapshots:code.txt") == "original")
        let secondToken = second.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        let firstToken = first.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
        #expect(try vibe("end", "--token", secondToken, cwd: other).status == 0)
        #expect(try vibe("end", "--token", firstToken).status == 0)
    }

    @Test func usageErrorsExitTwo() throws {
        #expect(try symbio(["vibe", "nope"]).status == 2)
        #expect(try symbio(["vibe", "worktree", "--name", "a/b"]).status == 2)
        #expect(try symbio(["unknown"]).status == 2)
    }
}

func gitIn(_ repo: String, _ args: [String]) throws -> String {
    let command = ["git", "-C", repo] + args
    let result = try run(command)
    guard result.status == 0 else { throw CommandFailed(command: command, status: result.status) }
    return result.output.trimmingCharacters(in: .whitespacesAndNewlines)
}

@Suite struct HeavyLockTests {
    func cleanEnvironment() -> [String: String] {
        ProcessInfo.processInfo.environment.filter { $0.key != HeavyLock.heldEnv }
    }

    @Test func secondProcessWaitsForFirst() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(atPath: root) }
        let lock = root + "/heavy.lock"
        let env = cleanEnvironment()
        async let first = symbio(["heavy-lock", "--lock", lock, "sleep", "1.5"], environment: env)
        try await Task.sleep(for: .milliseconds(500))
        let start = Date()
        let second = try symbio(["heavy-lock", "--lock", lock, "true"], environment: env)
        #expect(Date().timeIntervalSince(start) >= 0.8)
        #expect(second.stderr.contains("기다립니다"))
        #expect(try await first.status == 0)
    }

    @Test func childOfHolderDoesNotDeadlock() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(atPath: root) }
        let lock = root + "/heavy.lock"
        let holder = try symbio(
            ["heavy-lock", "--lock", lock, tool, "heavy-lock", "--lock", lock, "true"],
            environment: cleanEnvironment())
        #expect(holder.status == 0)
    }
}

@Suite struct UICheckTests {
    @Test func failureMessagesUsePythonRepr() {
        #expect(UICheck.pythonRepr(false) == "False")
        #expect(UICheck.pythonRepr(.null) == "None")
        #expect(UICheck.pythonRepr("알림") == "'알림'")
        #expect(UICheck.pythonRepr("it's") == "\"it's\"")
        #expect(UICheck.pythonRepr(["a", 1, 0.5]) == "['a', 1, 0.5]")
        #expect(UICheck.pythonRepr(["k": true]) == "{'k': True}")
        #expect(UICheck.pythonRepr("close_chat", bare: true) == "close_chat")
    }

    @Test func scenariosAreReadable() throws {
        let names = try FileManager.default.contentsOfDirectory(atPath: UICheck.scenarios).filter { $0.hasSuffix(".json") }
        #expect(!names.isEmpty)
        for name in names {
            let scenario = try JSON.parse(try Data(contentsOf: URL(fileURLWithPath: UICheck.scenarios + "/" + name)))
            #expect(scenario["steps"]?.arrayValue?.isEmpty == false)
        }
    }
}

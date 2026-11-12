import Foundation
import SymbioCore

/// Cooperative writer ownership and snapshots without touching HEAD or the real index (`scripts/vibe.py`).
public enum Vibe {
    static let ref = "refs/vibe/snapshots"
    static let sessionFile = "vibe-session.json"
    static let commands = ["begin", "end", "status", "snapshot", "sessions", "worktree"]
    static let usage = """
        usage: symbio-dev vibe [-h] [--repo REPO] [--agent AGENT] [--goal GOAL]
                               [--files FILES [FILES ...]] [--token TOKEN] [--name NAME] [--base BASE]
                               {begin,end,status,snapshot,sessions,worktree}

        """

    struct Arguments {
        var command = ""
        var repo = repoRoot
        var agent = "", goal = "", token = "", name = "", base = "main"
        var files: [String] = []
    }

    /// argparse's error(): usage plus message, exit 2. exit(): message only, given status.
    struct Exit: Error { let status: Int32; let message: String }
    static func usageError(_ message: String) -> Exit {
        Exit(status: 2, message: usage + "symbio-dev vibe: error: \(message)\n")
    }

    static func parse(_ argv: [String]) throws -> Arguments {
        var args = Arguments()
        var positional: [String] = []
        var index = 0
        while index < argv.count {
            var token = argv[index]
            index += 1
            if token == "-h" || token == "--help" {
                throw Exit(status: 0, message: usage)
            }
            guard token.hasPrefix("--") else { positional.append(token); continue }
            var inline: String?
            if let equals = token.firstIndex(of: "=") {
                inline = String(token[token.index(after: equals)...])
                token = String(token[..<equals])
            }
            let name = String(token.dropFirst(2))
            if name == "files" {
                var values = inline.map { [$0] } ?? []
                while inline == nil, index < argv.count, !argv[index].hasPrefix("-") {
                    values.append(argv[index])
                    index += 1
                }
                if values.isEmpty { throw usageError("argument --files: expected at least one argument") }
                args.files = values
                continue
            }
            guard ["repo", "agent", "goal", "token", "name", "base"].contains(name) else {
                throw usageError("unrecognized arguments: \(argv[index - 1])")
            }
            let value: String
            if let inline { value = inline } else {
                guard index < argv.count, !argv[index].hasPrefix("-") || argv[index] == "-" else {
                    throw usageError("argument --\(name): expected one argument")
                }
                value = argv[index]
                index += 1
            }
            switch name {
            case "repo": args.repo = value
            case "agent": args.agent = value
            case "goal": args.goal = value
            case "token": args.token = value
            case "name": args.name = value
            default: args.base = value
            }
        }
        guard let command = positional.first else {
            throw usageError("the following arguments are required: command")
        }
        guard commands.contains(command) else {
            let choices = commands.map { "'\($0)'" }.joined(separator: ", ")
            throw usageError("argument command: invalid choice: '\(command)' (choose from \(choices))")
        }
        if positional.count > 1 {
            throw usageError("unrecognized arguments: \(positional.dropFirst().joined(separator: " "))")
        }
        args.command = command
        return args
    }

    public static func main(_ argv: [String]) -> Int32 {
        do {
            try execute(try parse(argv))
            return 0
        } catch let exit as Exit {
            (exit.status == 0 ? writeOutput : writeError)(exit.message)
            return exit.status
        } catch {
            writeError("\(error)\n")
            return 1
        }
    }

    /// Each linked worktree keeps its own snapshots so parallel sessions never race on one ref.
    static func refFor(_ gitDir: String) -> String {
        let url = URL(fileURLWithPath: gitDir)
        return url.deletingLastPathComponent().lastPathComponent == "worktrees"
            ? "\(ref)-\(url.lastPathComponent)" : ref
    }

    /// The main checkout, also when called from a linked worktree.
    static func commonRepo(_ repo: String) throws -> String {
        let common = try git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return URL(fileURLWithPath: common).deletingLastPathComponent().path
    }

    /// (worktree path, session) of every other checkout that has an open session.
    static func otherSessions(_ repo: String, _ gitDir: String) throws -> [(String, JSON)] {
        var found: [(String, JSON)] = []
        for line in try git(repo, "worktree", "list", "--porcelain").components(separatedBy: "\n") {
            guard line.hasPrefix("worktree ") else { continue }
            let path = String(line.dropFirst("worktree ".count))
            // A removed or broken worktree has no session to report.
            guard let other = try? git(path, "rev-parse", "--absolute-git-dir"), other != gitDir,
                  let data = FileManager.default.contents(atPath: other + "/" + sessionFile),
                  let session = try? JSON.parse(data)
            else { continue }
            found.append((path, session))
        }
        return found
    }

    static func files(_ session: JSON) -> [String] {
        session["files"]?.arrayValue?.compactMap(\.stringValue) ?? []
    }

    static func snapshot(_ repo: String, _ gitDir: String, _ session: JSON) throws -> String {
        let ref = refFor(gitDir)
        // ponytail: periodic snapshots can span an edit; use begin/end for task boundaries.
        let temporary = gitDir + "/tmp" + UUID().uuidString.prefix(8)
        try FileManager.default.createDirectory(atPath: temporary, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(atPath: temporary) }
        var env = ProcessInfo.processInfo.environment
        env["GIT_INDEX_FILE"] = temporary + "/index"
        env["GIT_LITERAL_PATHSPECS"] = "1"
        _ = try git(repo, "read-tree", "HEAD", environment: env)
        _ = try git(repo, "add", "-u", environment: env)
        for name in files(session) {
            let path = repo + "/" + name
            var directory: ObjCBool = false
            let isFile = FileManager.default.fileExists(atPath: path, isDirectory: &directory) && !directory.boolValue
            if isFile, !isSymlink(path), isInside(resolvePath(path), repo) {
                // .gitignore 대상(로컬 규칙 파일 등)은 snapshot에서 건너뛴다
                _ = try? git(repo, "add", "--", name, environment: env)
            }
        }
        let tree = try git(repo, "write-tree", environment: env)
        let previous = try run(["git", "-C", repo, "rev-parse", "--verify", ref], captureError: true)
        let old = previous.output.trimmingCharacters(in: .whitespacesAndNewlines)
        let parent = previous.status == 0 ? old : try git(repo, "rev-parse", "HEAD")
        if previous.status == 0, try tree == git(repo, "rev-parse", "\(parent)^{tree}") {
            return parent
        }
        let label = session["token"]?.stringValue ?? "idle"
        let commit = try git(repo, "commit-tree", tree, "-p", parent, "-m", "vibe snapshot: \(label)")
        _ = try git(repo, "update-ref", ref, commit, old.isEmpty ? String(repeating: "0", count: 40) : old)
        return commit
    }

    static func execute(_ args: Arguments) throws {
        let repo = try git(args.repo, "rev-parse", "--show-toplevel")
        let gitDir = try git(repo, "rev-parse", "--absolute-git-dir")
        let sessionPath = gitDir + "/" + sessionFile
        let manager = FileManager.default
        if args.command == "worktree" {
            if args.name.isEmpty || args.name.contains("/") || args.name.hasPrefix(".") {
                throw usageError("worktree에는 --name(경로 구분자 없는 이름)이 필요합니다")
            }
            let main = URL(fileURLWithPath: try commonRepo(repo))
            let path = main.deletingLastPathComponent()
                .appendingPathComponent("\(main.lastPathComponent)-worktrees").appendingPathComponent(args.name)
            _ = try git(main.path, "worktree", "add", "-b", "work/\(args.name)", path.path, args.base)
            // Untracked local files a session needs; linked, so keys and the big tool tree stay single.
            for name in ["config.json", ".voice-action/config.env", "node_modules"] {
                let source = main.appendingPathComponent(name), link = path.appendingPathComponent(name)
                if manager.fileExists(atPath: source.path), !manager.fileExists(atPath: link.path) {
                    try manager.createDirectory(
                        at: link.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try manager.createSymbolicLink(at: link, withDestinationURL: source)
                }
            }
            // .gitignore's `node_modules/` does not match a link, so exclude it for this worktree.
            let exclude = URL(fileURLWithPath: try git(
                path.path, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
            try manager.createDirectory(at: exclude.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !manager.fileExists(atPath: exclude.path) { manager.createFile(atPath: exclude.path, contents: nil) }
            let handle = try FileHandle(forWritingTo: exclude)
            try handle.seekToEnd()
            try handle.write(contentsOf: Data("/node_modules\n".utf8))
            try handle.close()
            writeOutput(path.path + "\n")
            return
        }
        // All participating sessions and the scheduler serialize metadata/snapshot updates.
        let lock = open(gitDir + "/vibe.lock", O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard lock >= 0 else { throw CommandFailed(command: ["open", gitDir + "/vibe.lock"], status: errno) }
        defer { close(lock) }
        flock(lock, LOCK_EX)
        var session: JSON = [:]
        if let data = manager.contents(atPath: sessionPath) { session = try JSON.parse(data) }
        let hasSession = session.objectValue?.isEmpty == false
        switch args.command {
        case "status":
            writeOutput(session.dumps(JSONDumpOptions(ensureASCII: false, indent: 2)) + "\n")
        case "sessions":
            for (path, other) in try otherSessions(repo, gitDir) {
                writeOutput("\(path)\t\(other["agent"]?.stringValue ?? "")\t\(other["goal"]?.stringValue ?? "")\n")
            }
        case "begin":
            if hasSession { throw Exit(status: 1, message: "다른 수정 세션이 있습니다. vibe.py status로 확인하세요.\n") }
            if args.agent.isEmpty || args.goal.isEmpty || args.files.isEmpty {
                throw usageError("begin에는 --agent, --goal, --files가 필요합니다")
            }
            for name in args.files {
                // Path(name).parts: empty and "." components drop out, ".." stays.
                let parts = name.split(separator: "/").filter { $0 != "." }
                if name.hasPrefix("/") || parts.contains("..") || parts.isEmpty {
                    throw usageError("--files에는 저장소 안의 개별 상대 파일 경로만 지정하세요")
                }
                let path = repo + "/" + parts.joined(separator: "/")
                if parts[0] == ".git" || isDirectory(path) {
                    throw usageError("Git 내부 경로와 디렉터리는 지정할 수 없습니다")
                }
                if !isInside(resolvePath(path), repo) {
                    throw usageError("저장소 밖으로 연결된 경로는 지정할 수 없습니다")
                }
                let ignored = try run(["git", "-C", repo, "check-ignore", "-q", "--", name], captureOutput: false)
                if ignored.status == 0 { throw usageError("gitignore 대상은 보존 파일로 지정할 수 없습니다") }
                if ignored.status != 1 { throw usageError("gitignore 확인에 실패했습니다") }
            }
            session = [
                "token": .string(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()),
                "agent": .string(args.agent),
                "goal": .string(args.goal),
                "started": .string(isoNow()),
                "files": .array(args.files.map { .string($0) }),
            ]
            _ = try snapshot(repo, gitDir, session)
            for (path, other) in try otherSessions(repo, gitDir) {
                let shared = Set(args.files).intersection(files(other)).sorted()
                if !shared.isEmpty {
                    let agent = other["agent"]?.stringValue ?? ""
                    writeError(
                        "경고: \(path)의 \(agent) 세션도 \(shared.joined(separator: ", "))을(를) "
                            + "수정합니다. 통합 때 충돌하니 먼저 조율하세요.\n")
                }
            }
            let text = session.dumps(JSONDumpOptions(ensureASCII: false, indent: 2)) + "\n"
            try text.write(toFile: sessionPath, atomically: false, encoding: .utf8)
            writeOutput((session["token"]?.stringValue ?? "") + "\n")
        case "end":
            guard hasSession, args.token == session["token"]?.stringValue else {
                throw Exit(status: 1, message: "소유 세션의 --token이 필요합니다. 잠금을 임의로 해제하지 마세요.\n")
            }
            writeOutput(try snapshot(repo, gitDir, session) + "\n")
            try manager.removeItem(atPath: sessionPath)
        default:
            writeOutput(try snapshot(repo, gitDir, session) + "\n")
        }
    }

    /// `datetime.now().astimezone().isoformat()`: local time with microseconds and offset.
    static func isoNow() -> String {
        let now = Date()
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        let micro = Int((now.timeIntervalSince1970 * 1_000_000).truncatingRemainder(dividingBy: 1_000_000))
        let fraction = micro == 0 ? "" : String(format: ".%06d", micro)
        formatter.dateFormat = "xxx"
        let offset = formatter.string(from: now)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter.string(from: now) + fraction + offset
    }
}

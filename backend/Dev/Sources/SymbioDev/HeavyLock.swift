import Foundation

/// Let only one heavy job (tests, live eval) run at a time across all sessions and worktrees
/// (`scripts/heavy_lock.py`). The lock is held until the process exits, so there is nothing to release.
public enum HeavyLock {
    public static let defaultPath = NSHomeDirectory() + "/.claude/voice-action/heavy.lock"
    public static let heldEnv = "VOICE_ACTION_HEAVY_LOCK_HELD"

    /// Blocks until this process holds the lock. A child of a holder (env set) is already inside it.
    public static func acquire(lockPath: String = defaultPath) {
        if ProcessInfo.processInfo.environment[heldEnv] != nil { return }
        try? FileManager.default.createDirectory(
            atPath: (lockPath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        // The descriptor is never closed: the kernel frees the lock when the process exits.
        let handle = open(lockPath, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
        guard handle >= 0 else { return }
        if flock(handle, LOCK_EX | LOCK_NB) != 0 {
            writeError("다른 세션의 무거운 작업이 끝나길 기다립니다...\n")
            flock(handle, LOCK_EX)  // ponytail: no timeout; a crashed holder frees it
        }
        setenv(heldEnv, "1", 1)
    }

    /// `symbio-dev heavy-lock [--lock PATH] command [args...]`: run the command while holding the lock.
    public static func main(_ argv: [String]) -> Int32 {
        var args = argv[...]
        var lockPath = defaultPath
        if args.first == "--lock", args.count > 1 {
            lockPath = args[args.startIndex + 1]
            args = args.dropFirst(2)
        }
        if args.first == "--" { args = args.dropFirst() }
        guard !args.isEmpty else {
            writeError("usage: symbio-dev heavy-lock [--lock PATH] command [args...]\n")
            return 2
        }
        acquire(lockPath: lockPath)
        do {
            return try run(Array(args), captureOutput: false).status
        } catch {
            writeError("\(error)\n")
            return 127
        }
    }
}

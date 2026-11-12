import CryptoKit
import Foundation
import SymbioCore

/// `symbio-dev make-cases [--level N]...` (Python `evals/make_cases.py`): harder variants of the
/// hand-written tasks, keeping only the ones that stay valid.
///
/// A variant changes the situation, never the answer: the request is reworded, unrelated files
/// pile up, look-alike decoys appear. The task's scripted solution is replayed on every variant;
/// one that no longer passes is dropped, so a kept variant is a case the harness can grade.
/// Run the real model on them with `symbio-dev eval --live --dir evals/generated/L2`.
public enum MakeCases {
    static let noise = [1: 0, 2: 20, 3: 200]  // unrelated files per level
    static let decoys = [1: false, 2: true, 3: true]
    static let reword: [@Sendable (String) -> String] = [
        { "저기, \($0)" },
        { $0.hasSuffix("해줘") ? String($0.dropLast()) + "주세요" : "\($0) 부탁해" },
        { "\($0) 빨리" },
    ]

    /// Same task, same choice, so regenerating never reshuffles the set.
    static func pick(_ taskID: String, _ count: Int) -> Int {
        SHA256.hash(data: Data(taskID.utf8)).reduce(0) { ($0 * 256 + Int($1)) % count }
    }

    static func mutate(_ task: JSON, _ level: Int) -> JSON {
        var out = task
        let id = task["id"]?.str ?? "", request = task["request"]?.str ?? ""
        out.set("id", .string("\(id)~L\(level)"))
        out.set("base", .string(id))
        out.set("request", .string(reword[pick(id, reword.count)](request)))
        var files = task["files"] ?? [:]
        for i in 0..<(noise[level] ?? 0) {
            files.set(String(format: "zz_noise/n%03d.txt", i), .string("unrelated \(i)"))
        }
        if decoys[level] == true {
            for name in task["files"]?.keys ?? [] where request.contains((name as NSString).lastPathComponent) {
                let path = name as NSString
                let ext = path.pathExtension.isEmpty ? "" : "." + path.pathExtension
                let stem = (path.lastPathComponent as NSString).deletingPathExtension
                let folder = path.deletingLastPathComponent
                files.set((folder.isEmpty ? "" : folder + "/") + "\(stem)_old\(ext)", "decoy")
            }
        }
        out.set("files", files)
        return out
    }

    /// level -> (kept count, dropped ids); only tasks that replay in a throwaway folder qualify.
    static func build(_ levels: [Int], root: URL) async throws -> [(level: Int, kept: Int, dropped: [String])] {
        let base = loadJSONFiles(root.appendingPathComponent("evals/tasks"))
            .filter { $0.has("script") && $0["negative"]?.truthy != true }
        var result: [(level: Int, kept: Int, dropped: [String])] = []
        for level in levels {
            let folder = root.appendingPathComponent("evals/generated/L\(level)")
            try? FileManager.default.removeItem(at: folder)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            var kept = 0, dropped: [String] = []
            for task in base {
                let variant = mutate(task, level)
                let id = variant["id"]?.str ?? ""
                if await Eval.runTrial(variant, nil, [])["passed"]?.truthy == true {
                    try writeJSON(variant, folder.appendingPathComponent("\(id).json"))
                    kept += 1
                } else {
                    dropped.append(id)
                }
            }
            result.append((level, kept, dropped))
        }
        return result
    }

    public static func main(_ argv: [String], root: URL = projectRoot()) async -> Int32 {
        let levels: [Int]
        do {
            let args = try Arguments(argv, switches: [])
            levels = try args.all("--level").map {
                guard let n = Int($0), noise[n] != nil else { throw UsageError(message: $0) }
                return n
            }
        } catch {
            writeError("usage: symbio-dev make-cases [--level {1,2,3}]...\n")
            return 2
        }
        do {
            for (level, kept, dropped) in try await build(levels.isEmpty ? noise.keys.sorted() : levels, root: root) {
                writeOutput("L\(level): kept \(kept), dropped \(dropped.count)\n")
            }
        } catch {
            writeError("\(error)\n")
            return 1
        }
        return 0
    }
}

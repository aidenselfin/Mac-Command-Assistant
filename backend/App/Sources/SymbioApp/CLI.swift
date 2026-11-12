import Foundation
import SymbioCore
import SymbioFeedback
import SymbioPlanner
import SymbioRuntime

/// Python `main.py`: outcome printing, safe error text and the lesson CLI, now `symbio` subcommands.

public typealias Output = (String) -> Void

public func printOutcome(_ result: RuntimeResult, _ out: Output) {
    let hint = "승인: 승인/approve/yes/y | 거절: 거절/reject/no/n/취소/cancel/c"
    switch result {
    case .busy, .outcome(.completed), .outcome(.error), .outcome(.clarification):
        out(renderOutcome(result))
    case let .outcome(.approvalRequired(approval)):
        if ["delete", "move", "copy", "mkdir"].contains(approval.operation) {
            out(renderOutcome(result))
            out(hint)
            return
        }
        out("[APPROVAL REQUIRED]")
        out("tool: \(approval.toolName)")
        if approval.operation == "convert", let source = approval.targets.first { out("source: \(source)") }
        out("target: \(approval.targetPath)")
        out("operation: \(approval.operation)")
        out("content_chars: \(approval.contentChars)")
        out("preview: \(approval.preview)\(approval.previewTruncated ? "…" : "")")
        out(hint)
    }
}

/// SDK errors can contain request bodies and authentication headers: only known kinds show text.
public func errorMessage(_ error: any Error) -> String {
    switch error {
    case let error as ConfigError: return error.message
    case let error as ToolError: return error.message  // Python MCPError family
    case let error as PlannerError: return error.message
    default: return "실행 실패 (\(typeName(error))); 설정과 MCP/네트워크 연결을 확인하세요."
    }
}

/// Python `str(value)` for a JSON value read back from the feedback store.
func pyText(_ value: JSON?) -> String {
    switch value {
    case nil, .null?: return "None"
    case let .string(text)?: return text
    case let .bool(flag)?: return flag ? "True" : "False"
    case let value?: return value.text
    }
}

/// `"" or None` is falsy in Python.
private func truthy(_ value: JSON?) -> JSON? {
    switch value {
    case nil, .null?, .string("")?, .bool(false)?, .int(0)?: return nil
    default: return value
    }
}

public func manageLessons(
    _ out: Output, lessonOff: Int? = nil, lessonOn: Int? = nil,
    dbPath: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(defaultFeedbackDBPath)
) throws -> Int32 {
    let loop = try FeedbackLoop(dbPath)
    defer { loop.closeDatabase() }
    if let id = lessonOff {
        let found = try loop.deactivateLesson(id)
        out(found ? "lesson #\(id) 비활성화" : "lesson #\(id) 없음")
        return found ? 0 : 1
    }
    if let id = lessonOn {
        let adopted = try loop.activateLesson(id)
        out(adopted ? "lesson #\(id) 채택 (다음 명령부터 적용)" : "후보 lesson #\(id) 없음")
        return adopted ? 0 : 1
    }
    out("[활성 lesson]")
    let rows = try loop.activeLessons()
    for (id, text) in rows { out("  #\(id) \(text)") }
    if rows.isEmpty { out("  없음") }
    out("[후보 lesson] 자동 평가 대기·보류 중 (수동 --lesson-on/--lesson-off ID)")
    let candidates = try loop.candidateLessons()
    for (id, text) in candidates { out("  #\(id) \(text)") }
    if candidates.isEmpty { out("  없음") }
    out("[최근 감지·분석]")
    let incidents = try loop.recentIncidents()
    for incident in incidents {
        let signature = String(String.UnicodeScalarView(incident.signature.unicodeScalars.prefix(60)))
        out("  \(incident.time) \(incident.rule) \(incident.status) · \(signature)")
        if let analysis = incident.analysis {
            out("    원인: \(pyText(analysis["root_cause"]))")
            let detail = truthy(analysis["lesson"]) ?? truthy(analysis["fix_request"])
            out("    조치: \(pyText(analysis["action"])) · \(detail.map(pyText) ?? "-")")
        }
    }
    if incidents.isEmpty { out("  없음") }
    out("[자동 개선]")
    let runs = try loop.recentAutonomy()
    for (time, data) in runs {
        let why = (truthy(data["why"]) ?? truthy(data["head"])).map(pyText) ?? ""
        let line = "  \(time) \(pyText(data["job"])) #\(pyText(data["ref"])) \(pyText(data["status"])) \(why)"
        out(String(line.reversed().drop(while: { $0.isWhitespace }).reversed()))
    }
    if runs.isEmpty { out("  없음") }
    return 0
}

let lessonUsage = """
    usage: symbio lessons [on ID | off ID]
           symbio --lessons | --lesson-on ID | --lesson-off ID
    """

func standardError(_ text: String) { FileHandle.standardError.write(Data((text + "\n").utf8)) }

/// Python `main.main(argv)`: 0 ok, 1 not found or unreadable store, 2 bad arguments.
public func lessonsMain(_ argv: [String], _ out: Output = { print($0) }, dbPath: URL? = nil) -> Int32 {
    var on: Int?, off: Int?
    switch argv {
    case ["lessons"], ["--lessons"]: break
    case let args where args.count == 2 && ["--lesson-on", "--lesson-off"].contains(args[0]),
         let args where args.count == 3 && args[0] == "lessons" && ["on", "off"].contains(args[1]):
        guard let id = Int(args[args.count - 1]) else {
            standardError(lessonUsage)
            return 2
        }
        if args[args.count - 2].hasSuffix("on") { on = id } else { off = id }
    default:
        standardError(lessonUsage)
        return 2
    }
    do {
        if let dbPath { return try manageLessons(out, lessonOff: off, lessonOn: on, dbPath: dbPath) }
        return try manageLessons(out, lessonOff: off, lessonOn: on)
    } catch {
        out("[ERROR] 피드백 기록을 읽을 수 없습니다. .voice-action/feedback.sqlite3를 확인하세요.")
        return 1
    }
}

/// `symbio <command> ...`.
public func symbioMain(_ argv: [String]) -> Int32 {
    switch argv.first {
    case "lessons", "--lessons", "--lesson-on", "--lesson-off": return lessonsMain(argv)
    case "transcript": return transcriptMain(Array(argv.dropFirst()))
    case "autonomy": return autonomyMain(Array(argv.dropFirst()))
    default:
        standardError("usage: symbio {lessons,transcript,autonomy} ...\n" + lessonUsage)
        return 2
    }
}

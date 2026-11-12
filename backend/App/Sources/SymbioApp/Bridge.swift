import Foundation
import SymbioCore
import SymbioPlanner
import SymbioRuntime
import SymbioSearch

/// Python `voice_action/native_bridge.py`: private JSON-lines transport for the native UI over the
/// runtime worker. The protocol is docs/native-protocol.md.

let giveEndings = ["줘요", "줘", "주세요", "줄래요", "줄래"]
let giveVerbs = ["가져와", "가져다", "꺼내"]
let particles: Set<String> = ["만", "도", "을", "를", "은", "는"]
let fileFacts = ["경로", "위치", "이름", "목록", "내용", "요약", "크기", "개수", "정보", "날짜", "수정일", "생성일", "용량"]

/// Python string slicing works on code points; these keep that.
private func scalars(_ text: String) -> [Unicode.Scalar] { Array(text.unicodeScalars) }
private func string(_ scalars: some Sequence<Unicode.Scalar>) -> String {
    var out = ""
    out.unicodeScalars.append(contentsOf: scalars)
    return out
}
private func endsWith(_ text: String, _ suffix: String) -> Bool {
    scalars(text).reversed().starts(with: scalars(suffix).reversed())
}

/// '찾아만' is still 찾아, '경로를' still 경로.
func bare(_ word: String) -> String {
    var chars = scalars(word)
    while let last = chars.last, particles.contains(String(last)) { chars.removeLast() }
    return string(chars)
}

/// '해'·'찾아'·'알려'처럼 받침 없는 아/어 꼴이면 그 뒤 '줘'는 동작을 해 달라는 말이다.
func verbForm(_ word: String) -> Bool {
    guard let last = word.unicodeScalars.last else { return false }
    let code = Int(last.value) - 0xAC00
    return 0 <= code && code < 11172 && code % 28 == 0 && [0, 1, 4, 6, 9, 10, 14, 15].contains(code / 28 % 21)
}

/// '보고서 파일 줘'처럼 파일 자체를 달라는 말인지. '요약해줘'·'찾아 줘'는 아니다.
// ponytail: 말끝 규칙만 본다. "데이터 줘", 이름에 "요약"이 든 파일은 놓친다
public func asksForFile(_ text: String) -> Bool {
    let words = text.split(whereSeparator: { $0.isWhitespace }).map {
        $0.trimmingCharacters(in: CharacterSet(charactersIn: ".,!?~"))
    }
    let plain = words.filter { !$0.contains(".") && !$0.contains("_") }  // not file names
    if words.isEmpty || plain.contains(where: { word in fileFacts.contains { word.contains($0) } }) {
        return false  // "경로 좀 가져와": facts about the file, not the file
    }
    let last = words[words.count - 1]  // what the command finally asks: "가져와서 요약해줘" is a summary
    guard let give = giveEndings.first(where: { endsWith(last, $0) }) else {
        return giveVerbs.contains { endsWith(last, $0) }
    }
    let lastScalars = scalars(last)
    var before = Array(words.dropLast()) + [string(lastScalars.prefix(lastScalars.count - scalars(give).count))]
    before = before.filter { !$0.isEmpty && $0 != "좀" }  // "찾아 좀 줘" is still 찾아 줘
    var stem = before.last ?? ""
    if giveVerbs.contains(where: { endsWith(stem, $0) }) { return true }  // "꺼내줘", "가져와 줘"
    stem = bare(stem)
    if ["거", "것", "서", "나", "개", "명"].contains(where: { endsWith(stem, $0) }) { return true }  // nouns
    return stem.isEmpty || !verbForm(stem)
}

public let offerLimit = 5
public let quickLists = ["modified": "최근 수정한 파일", "size": "용량 큰 파일"]
public let quickLimit = offerLimit  // every listed file becomes a drag chip

/// ⌘⇧R·⌘⇧L: list files straight away, no model call. Returns (text, paths).
public func quickList(_ sort: String, roots: [URL]) throws -> (String, [String]) {
    let files = try listFiles(["sort_by": .string(sort), "limit": .int(quickLimit)], roots: roots)
        .structured?["files"]?.arrayValue ?? []
    if files.isEmpty { return ("허용 폴더에 파일이 없습니다.", []) }
    let lines = ["\(quickLists[sort] ?? sort) \(files.count)개"] + files.map { item in
        "- \(displayPath(item["name"]?.stringValue ?? "")) · \(item["size"]?.stringValue ?? "") · "
            + (item["modified"]?.stringValue ?? "")
    }
    return (lines.joined(separator: "\n"), files.compactMap { $0["path"]?.stringValue })
}

/// Drag-chip files: what the command made or edited, else the found file if it was asked for.
public func offerPaths(_ changed: [String], _ found: [String], wantsFile: Bool) -> [String] {
    let paths = (changed.isEmpty ? (wantsFile ? found : []) : changed).filter { path in
        var isDirectory: ObjCBool = false
        return FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory) && !isDirectory.boolValue
    }
    return paths.count <= offerLimit ? paths : []
}

public let staleApp = "화면 앱이 최신 코드보다 옛 빌드예요. native-preview/install-app.sh를 다시 실행하세요."

/// Warn when the screen app holds older native-preview code than the checkout it runs from.
public func staleAppNotice(_ built: String?, project: URL) -> String? {
    let git = Process()
    git.executableURL = URL(fileURLWithPath: "/usr/bin/git")
    git.arguments = ["-C", project.path, "rev-parse", "HEAD:native-preview"]
    let out = Pipe()
    git.standardOutput = out
    git.standardError = FileHandle.nullDevice
    let done = DispatchSemaphore(value: 0)
    git.terminationHandler = { _ in done.signal() }
    do { try git.run() } catch { return nil }
    let data = out.fileHandleForReading.readDataToEndOfFile()
    if done.wait(timeout: .now() + 5) == .timedOut {
        git.terminate()
        return nil
    }
    let current = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    return !current.isEmpty && built != current ? staleApp : nil
}

/// One Swift-app connection: runtime messages become events, commands go to the worker.
public final class NativeBridge: @unchecked Sendable {  // voice fields behind `voiceLock`; the rest on the reader thread
    public let worker: any BridgeWorker
    let emit: @Sendable (JSON) -> Void
    let voiceKey: String?
    let roots: [URL]
    let transcriber: @Sendable (URL, String) async throws -> String
    private let voiceLock = NSLock()
    private var _transcribing = false
    private var voiceCancelled = false
    public private(set) var voiceTask: Task<Void, Never>?
    public var state: RuntimeState?
    public var inFlight = false
    public var approvalID: String?
    public var request = ""  // the command a clarification answer belongs to

    public init(
        _ worker: any BridgeWorker, emit: @escaping @Sendable (JSON) -> Void, voiceKey: String? = nil,
        roots: [URL] = [],
        transcriber: @escaping @Sendable (URL, String) async throws -> String = { try await transcribe($0, apiKey: $1) }
    ) {
        self.worker = worker
        self.emit = emit
        self.voiceKey = voiceKey
        self.roots = roots
        self.transcriber = transcriber
    }

    public var transcribing: Bool { voiceLock.withLock { _transcribing } }

    public func message(_ message: UIMessage) {
        switch message {
        case let .state(value):
            state = value
            emit(["type": "state", "state": .string(value.name)])
        case let .outcome(result):
            inFlight = false
            approvalID = nil
            switch result {
            case let .outcome(.approvalRequired(value)):
                let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
                approvalID = id
                emit([
                    "type": "approval", "approval_id": .string(id),
                    "operation": .string(value.operation), "target": .string(displayPath(value.targetPath)),
                    "targets": .array(value.targets.map { .string(displayPath($0)) }),
                    "previews": .array(value.previews.map { .string($0) }),
                    "preview": .string(value.preview), "truncated": .bool(value.previewTruncated),
                    "characters": .int(value.contentChars),
                ])
            case let .outcome(.clarification(value)):
                emit(["type": "clarification", "text": .string(value.question),
                      "kind": .string(value.context.kind), "choices": .array(value.shownOptions.map { .string($0) })])
            case let .outcome(.completed(value)):
                let wants = asksForFile(request)
                emit(["type": "completed", "text": .string(value.text),
                      "changed_paths": .array(value.changedPaths.map { .string($0) }),
                      "found_paths": .array(value.foundPaths.map { .string($0) }),
                      "offer_file": .bool(wants),
                      "offer_paths": .array(offerPaths(value.changedPaths, value.foundPaths, wantsFile: wants)
                          .map { .string($0) })])
            case let .outcome(.error(text)):
                emit(["type": "error", "text": .string(text)])
            case let .busy(busy):
                emit(["type": "error", "text": .string(busy.message)])
            }
        case let .plannerEvent(event):
            emit(["type": "tool", "name": .string(event.toolName ?? "")])
        case let .attachment(path, refusal):
            emit(["type": "attachment", "path": path.map { .string($0) } ?? .null,
                  "text": refusal.map { .string($0) } ?? .null])
        case let .failure(text):
            inFlight = false
            approvalID = nil
            emit(["type": "error", "text": .string(text)])
        case .stopped:
            approvalID = nil
            emit(["type": "stopped"])
        case let .notice(text):
            emit(["type": "notice", "text": .string(text)])
        }
    }

    /// Handle one decoded line. false: stop the bridge (`shutdown`).
    public func command(_ request: JSON?) -> Bool {
        guard let request, request.objectValue != nil else { return refuse() }
        let action = request["action"]?.stringValue
        if action == "shutdown" { return false }
        if action == "cancel" {
            let wasVoice = voiceLock.withLock {
                if _transcribing { voiceCancelled = true }
                return _transcribing
            }
            if wasVoice { return true }
            if state == .waitingApproval {
                return command(["action": "reject", "approval_id": approvalID.map { .string($0) } ?? .null])
            }
            if state == .waitingUserInput { return command(["action": "submit", "text": "취소"]) }
            if worker.cancelActive() { return true }
            return refuse()
        }
        if inFlight { return refuse() }
        if action == "approve" || action == "reject" {
            guard state == .waitingApproval, let approvalID, request["approval_id"]?.stringValue == approvalID else {
                return refuse()
            }
            if action == "approve" ? worker.approvePending() : worker.rejectPending() {
                self.approvalID = nil
                inFlight = true
                return true
            }
            return refuse()
        }
        if action == "voice" {
            let clip = clipPath(request["path"])
            let busy = voiceLock.withLock { _transcribing }
            guard let clip, !busy, state == .idle || state == .waitingUserInput else {
                if let clip { unlink(clip.path) }
                return refuse()
            }
            voiceLock.withLock {
                _transcribing = true
                voiceCancelled = false
            }
            voiceTask = Task.detached { [self] in await transcribeClip(clip) }
            return true
        }
        if action == "submit" {
            guard state == .idle || state == .waitingUserInput, let text = request["text"]?.stringValue,
                  !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            else { return refuse() }
            if state == .waitingUserInput && isFeedbackCommand(text) {
                return refuse("질문에 먼저 답한 뒤 /feedback을 보내 주세요.")
            }
            if worker.submit(text) {
                if state != .waitingUserInput { self.request = text }
                inFlight = true
                return true
            }
            return refuse()
        }
        if state != .idle { return refuse() }
        switch action {
        case "undo":
            if worker.undoLastWrite() {
                inFlight = true
                return true
            }
        case "attach":
            if let path = request["path"]?.stringValue, worker.attach(path) { return true }
        case "detach":
            worker.detach()
            emit(["type": "attachment", "path": nil, "text": nil])
            return true
        case "reset":
            if worker.resetConversation() { return true }
        case "quick_list":
            guard let sort = request["sort"]?.stringValue, quickLists[sort] != nil else { break }
            // ponytail: walks on the reader thread; the walk's own cap keeps it short
            let text: String, paths: [String]
            do {
                (text, paths) = try quickList(sort, roots: roots)
            } catch {
                return refuse("파일 목록을 만들지 못했습니다 (\(typeName(error))).")
            }
            let listed = JSON.array(paths.map { .string($0) })
            emit(["type": "completed", "text": .string(text), "changed_paths": [], "found_paths": listed,
                  "offer_file": true, "offer_paths": listed, "show_box": true])
            return true
        default: break
        }
        return refuse()
    }

    /// The text goes back to Swift, which submits it like typed input.
    func transcribeClip(_ clip: URL) async {
        var text = "", problem: String?
        if let key = voiceKey, !key.isEmpty {
            do {
                text = try await transcriber(clip, key)
                if isHallucination(text) { text = "" }  // silence heard as a stock phrase: show nothing
            } catch {
                problem = "음성을 글로 바꾸지 못했습니다 (\(typeName(error)))."
            }
        } else {
            problem = "음성 명령에는 Groq API 키(GROQ_API_KEY)가 필요합니다."
        }
        unlink(clip.path)
        voiceLock.withLock {  // a cancel is either seen here or arrives after the result
            if voiceCancelled { (text, problem) = ("", "음성 명령을 취소했습니다.") }
            emit(["type": "transcript", "text": .string(text), "error": problem.map { .string($0) } ?? .null])
            _transcribing = false
        }
    }

    @discardableResult
    public func refuse(_ text: String = "현재 상태에서 처리할 수 없는 요청입니다.") -> Bool {
        emit(["type": "rejected", "text": .string(text)])
        return true
    }
}

// MARK: - process

/// What the reader loop waits on: worker messages, stdin lines, or the end.
enum BridgeInput: Sendable {
    case message(UIMessage)
    case line(Data)
    case overflow
    case end
}

/// Thread-safe `print(json.dumps(value, ensure_ascii=False), flush=True)` to stdout.
public func stdoutEmitter() -> @Sendable (JSON) -> Void {
    let lock = NSLock()
    return { value in
        let line = Array((value.text + "\n").utf8)
        lock.withLock {
            var offset = 0
            while offset < line.count {
                let written = line[offset...].withUnsafeBytes { write(STDOUT_FILENO, $0.baseAddress, $0.count) }
                if written <= 0 { return }  // closed pipe: the app is gone
                offset += written
            }
        }
    }
}

/// `symbio-bridge`: Python `native_bridge.main()`. Returns the exit code.
public func runBridge(
    input: Int32 = STDIN_FILENO, emit: (@Sendable (JSON) -> Void)? = nil,
    loadConfig load: () throws -> Config = { try loadConfig() },
    makeWorker: (Config, @escaping @Sendable (UIMessage) -> Void) -> any BridgeWorker = {
        AsyncRuntimeWorker($0, send: $1)
    },
    staleNotice: (String?, URL) -> String? = { staleAppNotice($0, project: $1) },
    handleSignals: Bool = true
) -> Int32 {
    let emit = emit ?? stdoutEmitter()
    let config: Config
    do {
        config = try load()
    } catch {
        emit(["type": "error", "text": .string("설정 로딩 실패 (\(typeName(error))). 설정과 키를 확인하세요.")])
        return 1
    }
    let inbox = BlockingQueue<BridgeInput>()
    let worker = makeWorker(config) { inbox.put(.message($0)) }
    purgeStale()
    let bridge = NativeBridge(worker, emit: emit, voiceKey: config.voiceApiKey, roots: config.allowedDirectories)
    var sources: [any DispatchSourceSignal] = []
    if handleSignals {
        signal(SIGPIPE, SIG_IGN)
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { inbox.put(.end) }
            source.resume()
            sources.append(source)
        }
    }
    emit(["type": "config", "folders": .array(config.allowedDirectories.map { .string($0.path) }),
          "finder_reveal": .bool(config.finderReveal)])
    let project = URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
    if let notice = staleNotice(ProcessInfo.processInfo.environment["SYMBIO_APP_TREE"], project) {
        emit(["type": "notice", "text": .string(notice)])
    }
    worker.start()
    // Read bytes ourselves on a thread; every input then goes through the one inbox in order.
    let reader = Thread {
        var buffered = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while true {
            let count = read(input, &chunk, chunk.count)
            if count <= 0 { inbox.put(.end); return }
            buffered.append(contentsOf: chunk[..<count])
            if buffered.count > 1024 * 1024 { inbox.put(.overflow); return }
            while let newline = buffered.firstIndex(of: 10) {
                inbox.put(.line(Data(buffered[buffered.startIndex..<newline])))
                buffered = Data(buffered[(newline + 1)...])
            }
        }
    }
    reader.start()
    loop: while let next = inbox.get() {
        switch next {
        case let .message(message):
            bridge.message(message)
            if case .stopped = message { break loop }
        case let .line(line):
            guard let request = try? JSON.parse(line) else {
                bridge.refuse()
                continue
            }
            if !bridge.command(request) { break loop }
        case .overflow:
            bridge.refuse()
            break loop
        case .end:
            break loop
        }
    }
    sources.forEach { $0.cancel() }
    if let voice = bridge.voiceTask {  // up to 5 s for the clip to be cleaned up
        let cleaned = DispatchSemaphore(value: 0)
        Task.detached { await voice.value; cleaned.signal() }
        _ = cleaned.wait(timeout: .now() + 5)
    }
    let finished = DispatchSemaphore(value: 0)
    Task.detached {
        await worker.shutdown()  // waits for cancellation and durable deletion/Undo cleanup
        finished.signal()
    }
    finished.wait()
    return 0
}

import AppKit
import SwiftUI

struct ChatMessage: Identifiable {
    let id = UUID()
    let role: String
    let text: String
}

struct Approval: Decodable {
    let approval_id: String
    let operation: String
    let target: String
    let targets: [String]
    let preview: String
    let truncated: Bool
    let characters: Int
}

@MainActor
final class Backend: ObservableObject {
    static let shared = Backend()
    @Published var messages: [ChatMessage] = []
    @Published var state = "STARTING"
    @Published var tool = ""
    @Published var folders: [String] = []
    @Published var choices: [String] = [] { didSet { syncChips() } }
    @Published var approval: Approval? { didSet { syncChips() } }
    @Published var attachment: String?
    // Files the command made, edited or was asked for (at most 5), each a chip the user drags out;
    // with the chat window closed they also float over other apps.
    @Published var dragFiles: [URL] = [] { didSet { if dragFiles.isEmpty { FileBox.shared.show([]) } } }
    @Published var online = false
    @Published var pending = false { didSet { if !pending { Toast.shared.thinking(nil) } } }
    @Published var transcribing = false
    @Published var ratable = false
    @Published var lowScore: Int?
    private var process: Process?
    private var input: FileHandle?
    private var buffer = Data()
    private var reveal = false
    private var voiceClip: URL?
    private var voiceCancelled = false
    private var session = UUID()

    var running: Bool { process != nil }

    // canBecomeMain: the menu-bar item's own window is visible but is not the chat
    private var chatShown: Bool {
        NSApp.isActive && NSApp.windows.contains { $0.isVisible && $0.canBecomeMain && !($0 is NSPanel) }
    }

    /// With the chat window closed or behind another app, a pending choice or approval becomes chips.
    fileprivate func syncChips() {
        guard !chatShown, !pending, state != "STOPPED" else { ChoiceChips.shared.hide(); return }
        if let approval {
            let first = approval.targets.first.map { ($0 as NSString).lastPathComponent } ?? approval.target
            let more = approval.targets.count > 1 ? " 외 \(approval.targets.count - 1)개" : ""
            let to = ["move", "copy"].contains(approval.operation)
                ? " → " + (approval.target as NSString).lastPathComponent : ""
            let target = first + more + to
            Toast.shared.dismiss()
            ChoiceChips.shared.show("\(approvalTitle(approval.operation)) · \(target)", [
                ("승인", { [weak self] in self?.decide(true) }),
                ("취소", { [weak self] in self?.decide(false) }),
            ])
        } else if !choices.isEmpty {
            let question = messages.last(where: { $0.role == "assistant" })?.text ?? "골라 주세요"
            Toast.shared.dismiss()
            ChoiceChips.shared.show(question, choices.enumerated().map { index, label in
                (label, { [weak self] in self?.submit("\(index + 1)번") })
            })
        } else { ChoiceChips.shared.hide() }
    }

    private func approvalTitle(_ operation: String) -> String {
        switch operation {
        case "delete": "파일 삭제"
        case "move": "파일 이동"
        case "copy": "파일 복사"
        case "mkdir": "폴더 생성"
        default: "파일 변경"
        }
    }

    var canSubmit: Bool {
        online && !pending && ["IDLE", "WAITING_USER_INPUT"].contains(state)
    }
    var idle: Bool { online && !pending && state == "IDLE" }

    func start() {
        guard process == nil else { return }
        // Showing the chips depends on whether the chat window is up, which changes without any event.
        for name in [NSApplication.didResignActiveNotification, NSApplication.didBecomeActiveNotification,
                     NSWindow.willCloseNotification] {
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { _ in
                DispatchQueue.main.async { Task { @MainActor in Backend.shared.syncChips() } }
            }
        }
        // app_tree: the native-preview code this app was built from, so the backend can warn when stale.
        // args: only a UI test replaces the real backend with a scripted one.
        struct Runtime: Decodable { let project: String; let python: String; let app_tree: String?; let args: [String]? }
        do {
            guard let url = UITest.runtime ?? Bundle.main.url(forResource: "runtime", withExtension: "json") else {
                throw CocoaError(.fileNoSuchFile)
            }
            let runtime = try JSONDecoder().decode(Runtime.self, from: Data(contentsOf: url))
            let child = Process()
            let stdin = Pipe(), stdout = Pipe()
            session = UUID()
            let current = session
            buffer = Data()
            state = "STARTING"
            online = false
            pending = false
            ratable = false
            lowScore = nil
            approval = nil
            choices = []
            child.executableURL = URL(fileURLWithPath: runtime.python)
            child.arguments = runtime.args ?? ["-u", "-m", "voice_action.native_bridge"]
            child.currentDirectoryURL = URL(fileURLWithPath: runtime.project)
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONPATH"] = runtime.project
            environment["SYMBIO_APP_TREE"] = runtime.app_tree
            // 앱 아이콘으로 켜면 PATH가 최소라 filesystem MCP가 쓰는 node를 못 찾는다.
            let extra = "/opt/homebrew/bin:/usr/local/bin"
            environment["PATH"] = extra + ":" + (environment["PATH"] ?? "/usr/bin:/bin")
            child.environment = environment
            child.standardInput = stdin
            child.standardOutput = stdout
            child.standardError = FileHandle.nullDevice
            stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil }
                Task { @MainActor in
                    guard let self, self.session == current else { return }
                    self.consume(data)
                }
            }
            child.terminationHandler = { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.session == current else { return }
                    self.process = nil
                    self.input = nil
                    self.online = false
                    self.pending = false
                    self.transcribing = false
                    self.approval = nil
                    self.choices = []
                    self.state = "STOPPED"
                }
            }
            try child.run()
            process = child
            input = stdin.fileHandleForWriting
        } catch {
            state = "STOPPED"
            append("error", "백엔드를 시작하지 못했습니다. run.sh로 실행하고 Python 환경을 확인하세요.")
        }
    }

    private func consume(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            guard let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let type = value["type"] as? String else { continue }
            let text = value["text"] as? String ?? ""
            switch type {
            case "config":
                folders = value["folders"] as? [String] ?? []
                reveal = value["finder_reveal"] as? Bool ?? false
            case "state":
                state = value["state"] as? String ?? "STARTING"
                online = true
                if state == "IDLE", resetWhenIdle { pending = false; newConversation() }
            case "tool": tool = value["name"] as? String ?? ""
            case "approval":
                pending = false
                approval = try? JSONDecoder().decode(Approval.self, from: line)
            case "clarification":
                pending = false
                append("assistant", text)
                choices = value["choices"] as? [String] ?? []
            case "completed", "error":
                pending = false
                approval = nil
                choices = []
                tool = ""
                append(type == "error" ? "error" : "assistant", text)
                ratable = value["ratable"] as? Bool ?? false
                lowScore = nil
                if type == "completed" {
                    dragFiles = (value["offer_paths"] as? [String] ?? []).compactMap(offerable)
                    // canBecomeMain: the menu-bar item's own window is visible but is not the chat
                    if !dragFiles.isEmpty, !chatShown { FileBox.shared.show(dragFiles) }
                }
                if type == "completed", reveal,
                   let paths = value["changed_paths"] as? [String], !paths.isEmpty {
                    let checked = paths.compactMap(offerable)
                    if !checked.isEmpty { NSWorkspace.shared.activateFileViewerSelecting(checked) }
                }
            case "attachment":
                if let path = value["path"] as? String { attachment = path }
                else if text.isEmpty { attachment = nil }
                if !text.isEmpty { append("error", text) }
            case "rating_saved":
                let score = value["score"] as? Int ?? 0
                let tags = value["tags"] as? [String] ?? []
                if score > 3 || !tags.isEmpty { ratable = false; lowScore = nil }
                else { lowScore = score }
            case "rejected":
                pending = false
                transcribing = false
                if let clip = voiceClip { try? FileManager.default.removeItem(at: clip) }
                append("error", text)
            case "transcript":
                pending = false
                transcribing = false
                let cancelled = voiceCancelled
                voiceCancelled = false
                if cancelled { break }
                if let problem = value["error"] as? String { append("error", problem) }
                else { submit(text) }
            case "notice": append("notice", text)
            case "_test" where UITest.active: UITest.handle(value)
            case "stopped": online = false; pending = false; approval = nil; choices = []
            default: break
            }
        }
    }

    /// A regular file inside an allowed folder, not reached through a symlink; anything else is never offered.
    private func offerable(_ path: String) -> URL? {
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard url == url.resolvingSymlinksInPath(),
              folders.contains(where: { url.path.hasPrefix($0 + "/") }),
              (try? FileManager.default.attributesOfItem(atPath: url.path))?[.type] as? FileAttributeType == .typeRegular
        else { return nil }
        return url
    }

    func append(_ role: String, _ text: String) {
        guard !text.isEmpty else { return }
        messages.append(ChatMessage(role: role, text: text))
        Toast.shared.show(role, text)
    }

    @discardableResult
    func send(_ action: String, fields: [String: Any] = [:]) -> Bool {
        guard let input else { return false }
        if action == "cancel", transcribing { voiceCancelled = true }
        do {
            var value = fields
            value["action"] = action
            var data = try JSONSerialization.data(withJSONObject: value)
            data.append(10)
            try input.write(contentsOf: data)
            return true
        } catch {
            append("error", "백엔드 연결이 끊어졌습니다.")
            online = false
            return false
        }
    }

    func submit(_ text: String) {
        guard canSubmit, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        if send("submit", fields: ["text": text]) {
            pending = true
            ratable = false
            choices = []
            attachment = nil
            dragFiles = []
            append("user", text)
            Toast.shared.thinking("생각하는 중…")
        }
    }

    private var resetWhenIdle = false

    /// ⌘⇧O: drop the shown chat and the backend's follow-up memory. Busy or waiting work is cancelled first.
    func newConversation() {
        guard online else { return }
        if state != "IDLE" || pending {
            resetWhenIdle = true
            _ = send("cancel")
            return
        }
        resetWhenIdle = false
        guard send("reset") else { return }
        messages = []
        choices = []
        approval = nil
        attachment = nil
        dragFiles = []
        tool = ""
        ratable = false
        lowScore = nil
        Toast.shared.dismiss()
        ChoiceChips.shared.hide()
    }

    /// Hand a finished recording to the backend; it comes back as a `transcript` event.
    func sendVoice(_ clip: URL) {
        guard canSubmit, send("voice", fields: ["path": clip.path]) else {
            try? FileManager.default.removeItem(at: clip)
            return
        }
        voiceClip = clip
        voiceCancelled = false
        pending = true
        transcribing = true
        Toast.shared.thinking("음성을 글로 바꾸는 중…", fresh: true)
    }

    func decide(_ approve: Bool) {
        guard !pending, let approval else { return }
        if send(approve ? "approve" : "reject", fields: ["approval_id": approval.approval_id]) {
            pending = true
            Toast.shared.thinking("처리하는 중…")
            self.approval = nil
        }
    }

    func shutdown(_ finished: @escaping @MainActor () -> Void) {
        guard let child = process else { finished(); return }
        _ = send("shutdown")
        try? input?.close()
        Task.detached {
            child.waitUntilExit()
            await finished()
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?

    // The menu bar icon keeps the app alive after the window is closed.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)  // menu bar only, no Dock icon
        Backend.shared.start()
        guard !UITest.active else { return }  // a test run must not catch the user's keys or add a menu icon
        VoiceInput.shared.install()
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.target = self
        item.button?.action = #selector(statusClicked)
        item.button?.sendAction(on: [.leftMouseUp])  // right-click does nothing
        statusItem = item
        refreshIcon()
        // Login launch (--background) starts hidden; the window opens from the menu.
        if CommandLine.arguments.contains("--background") {
            DispatchQueue.main.async { NSApp.windows.forEach { $0.orderOut(nil) } }
        }
    }

    private func refreshIcon() {
        let on = VoiceInput.shared.enabled
        let image = NSImage(systemSymbolName: on ? "mic.fill" : "mic.slash", accessibilityDescription: "음성 키")
        image?.isTemplate = true
        statusItem?.button?.image = image
        statusItem?.button?.toolTip = on ? "음성 키 감지 켜짐" : "음성 키 감지 꺼짐"
    }

    /// Left click opens the menu (toggle, open window, quit); right click is ignored.
    @objc private func statusClicked() {
        let menu = NSMenu()
        let toggle = menu.addItem(withTitle: VoiceInput.shared.enabled ? "음성 키 끄기" : "음성 키 켜기",
                                  action: #selector(toggleVoice), keyEquivalent: "")
        toggle.target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "창 열기", action: #selector(openWindow), keyEquivalent: "").target = self
        menu.addItem(withTitle: "종료", action: #selector(quit), keyEquivalent: "").target = self
        statusItem?.menu = menu
        statusItem?.button?.performClick(nil)
        statusItem?.menu = nil
    }

    @objc private func toggleVoice() {
        VoiceInput.shared.enabled.toggle()
        refreshIcon()
    }

    @objc private func openWindow() { Self.showChat() }

    static func showChat() {
        NSApp.activate()
        // The app also owns small helper windows (status item, 64pt stub): pick the chat window by title.
        NSApp.windows.first { $0.title == "Symbio" }?.makeKeyAndOrderFront(nil)
    }

    @objc private func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        VoiceInput.shared.discard()  // a clip still being recorded must not stay in the temp folder
        guard Backend.shared.running else { return .terminateNow }
        Backend.shared.shutdown { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

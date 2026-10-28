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
    @Published var choices: [String] = []
    @Published var approval: Approval?
    @Published var attachment: String?
    // The one found file, offered as a chip the user drags out; "파일 줘" also floats it over other apps.
    @Published var dragFile: URL? { didSet { if dragFile == nil { FileBox.shared.show(nil) } } }
    @Published var online = false
    @Published var pending = false
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

    var canSubmit: Bool {
        online && !pending && ["IDLE", "WAITING_USER_INPUT"].contains(state)
    }
    var idle: Bool { online && !pending && state == "IDLE" }

    func start() {
        guard process == nil else { return }
        struct Runtime: Decodable { let project: String; let python: String }
        do {
            guard let url = Bundle.main.url(forResource: "runtime", withExtension: "json") else {
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
            child.arguments = ["-u", "-m", "voice_action.native_bridge"]
            child.currentDirectoryURL = URL(fileURLWithPath: runtime.project)
            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONPATH"] = runtime.project
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
            case "tool": tool = value["name"] as? String ?? ""
            case "approval":
                pending = false
                approval = try? JSONDecoder().decode(Approval.self, from: line)
            case "clarification":
                pending = false
                choices = value["choices"] as? [String] ?? []
                append("assistant", text)
            case "completed", "error":
                pending = false
                approval = nil
                choices = []
                tool = ""
                append(type == "error" ? "error" : "assistant", text)
                ratable = value["ratable"] as? Bool ?? false
                lowScore = nil
                if type == "completed", let found = (value["found_paths"] as? [String])?.first {
                    let url = URL(fileURLWithPath: found).standardizedFileURL
                    if url == url.resolvingSymlinksInPath(),
                       folders.contains(where: { url.path.hasPrefix($0 + "/") }),
                       (try? FileManager.default.attributesOfItem(atPath: url.path))?[.type] as? FileAttributeType == .typeRegular {
                        dragFile = url
                        if value["offer_file"] as? Bool == true { FileBox.shared.show(url) }
                    }
                }
                if type == "completed", reveal,
                   let paths = value["changed_paths"] as? [String], !paths.isEmpty {
                    let checked = paths.compactMap { path -> URL? in
                        let url = URL(fileURLWithPath: path).standardizedFileURL
                        guard url == url.resolvingSymlinksInPath(),
                              folders.contains(where: { url.path.hasPrefix($0 + "/") }),
                              let attributes = try? FileManager.default.attributesOfItem(atPath: url.path),
                              attributes[.type] as? FileAttributeType == .typeRegular else { return nil }
                        return url
                    }
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
            case "stopped": online = false; pending = false; approval = nil; choices = []
            default: break
            }
        }
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
            dragFile = nil
            append("user", text)
        }
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
    }

    func decide(_ approve: Bool) {
        guard !pending, let approval else { return }
        if send(approve ? "approve" : "reject", fields: ["approval_id": approval.approval_id]) {
            pending = true
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

    @objc private func openWindow() {
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

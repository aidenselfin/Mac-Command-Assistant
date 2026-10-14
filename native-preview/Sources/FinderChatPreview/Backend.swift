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
    @Published var online = false
    @Published var pending = false
    @Published var ratable = false
    @Published var lowScore: Int?
    private var process: Process?
    private var input: FileHandle?
    private var buffer = Data()
    private var reveal = false
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
                append("error", text)
            case "notice": append("notice", text)
            case "stopped": online = false; pending = false; approval = nil; choices = []
            default: break
            }
        }
    }

    func append(_ role: String, _ text: String) {
        if !text.isEmpty { messages.append(ChatMessage(role: role, text: text)) }
    }

    @discardableResult
    func send(_ action: String, fields: [String: Any] = [:]) -> Bool {
        guard let input else { return false }
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
            append("user", text)
        }
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
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard Backend.shared.running else { return .terminateNow }
        Backend.shared.shutdown { sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

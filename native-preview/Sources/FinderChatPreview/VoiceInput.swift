import AppKit
import AVFoundation
import SwiftUI

/// Push-to-talk: records only while the right Option key is held, then hands the clip to the backend.
@MainActor
final class VoiceInput: ObservableObject {
    static let shared = VoiceInput()
    @Published var recording = false
    @Published var denied = false
    private var recorder: AVAudioRecorder?
    private var monitors: [Any] = []
    private var started = Date()
    private var limit: Task<Void, Never>?
    private var badge: NSPanel?

    /// Right Option is key code 61; the device-dependent bit 0x40 tells it apart from the left one.
    private static let rightOptionKey: UInt16 = 61
    private static let rightOptionBit: UInt = 0x40

    func install() {
        guard monitors.isEmpty else { return }
        // Ask at launch so the system prompt appears before the first key press.
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined {
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
        }
        // Local sees keys while this app is frontmost; global needs Input Monitoring permission.
        let handler: (NSEvent) -> Void = { event in
            let key = event.keyCode, flags = event.modifierFlags.rawValue
            Task { @MainActor in VoiceInput.shared.flagsChanged(key: key, flags: flags) }
        }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged, handler: { event in
            handler(event)
            return event
        }) { monitors.append(local) }
        // Option+another key is a shortcut or special character, not a voice command: drop the clip.
        let typed: (NSEvent) -> Void = { _ in Task { @MainActor in VoiceInput.shared.discard() } }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { event in
            typed(event)
            return event
        }) { monitors.append(local) }
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: typed) {
            monitors.append(global)
        }
        // Without Input Monitoring the key-up is lost once another app is frontmost.
        let resign = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { _ in
            Task { @MainActor in if !CGPreflightListenEventAccess() { VoiceInput.shared.finish() } }
        }
        monitors.append(resign)
        if let global = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged, handler: handler) {
            monitors.append(global)
        }
    }

    private func flagsChanged(key: UInt16, flags: UInt) {
        guard key == Self.rightOptionKey else { return }
        if flags & Self.rightOptionBit != 0 { begin() } else { finish() }
    }

    private func begin() {
        let backend = Backend.shared
        guard !recording, backend.canSubmit else { return }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
            return
        default:
            denied = true
            backend.append("error", "마이크 권한이 없습니다. 시스템 설정 > 개인정보 보호에서 허용하세요.")
            return
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("voice-action-voice-\(UUID().uuidString).m4a")
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16000,
            AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32000,
        ]
        guard let new = try? AVAudioRecorder(url: url, settings: settings), new.record() else {
            backend.append("error", "녹음을 시작하지 못했습니다.")
            return
        }
        recorder = new
        started = Date()
        recording = true
        showBadge()
        // Safety net: a missed key-up (app switched without Input Monitoring) must not record forever.
        limit = Task { @MainActor in
            try? await Task.sleep(for: .seconds(30))
            if !Task.isCancelled { finish() }
        }
    }

    private func discard() {
        guard recording, let current = recorder else { return }
        recording = false
        badge?.orderOut(nil)
        limit?.cancel()
        recorder = nil
        current.stop()
        try? FileManager.default.removeItem(at: current.url)
    }

    fileprivate func finish() {
        guard recording, let current = recorder else { return }
        recording = false
        badge?.orderOut(nil)
        limit?.cancel()
        recorder = nil
        current.stop()
        // A tap shorter than this is an accidental press, not a command.
        if Date().timeIntervalSince(started) < 0.4 {
            try? FileManager.default.removeItem(at: current.url)
            return
        }
        Backend.shared.sendVoice(current.url)
    }

    /// Small always-on-top "listening" pill at the bottom-left of the screen.
    /// Plain AppKit on purpose: an NSHostingView inside a borderless panel crashed in constraint updates.
    private func showBadge() {
        if badge == nil {
            let size = NSSize(width: 104, height: 30)
            let panel = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                                styleMask: [.borderless, .nonactivatingPanel],
                                backing: .buffered, defer: false)
            panel.level = .statusBar
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.ignoresMouseEvents = true
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            let pill = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
            pill.material = .hudWindow
            pill.state = .active
            pill.wantsLayer = true
            pill.layer?.cornerRadius = size.height / 2
            pill.layer?.masksToBounds = true
            let dot = NSView(frame: NSRect(x: 12, y: 11, width: 8, height: 8))
            dot.wantsLayer = true
            dot.layer?.backgroundColor = NSColor.systemRed.cgColor
            dot.layer?.cornerRadius = 4
            let label = NSTextField(labelWithString: "듣는 중")
            label.font = .systemFont(ofSize: 13, weight: .medium)
            label.frame = NSRect(x: 28, y: 5, width: 70, height: 20)
            pill.addSubview(dot)
            pill.addSubview(label)
            panel.contentView = pill
            badge = panel
        }
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            badge?.setFrameOrigin(NSPoint(x: area.minX + 16, y: area.minY + 16))
        }
        badge?.orderFrontRegardless()
    }
}

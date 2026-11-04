import AppKit
import AVFoundation
import SwiftUI

/// Push-to-talk: records only while the right Option key is held, then hands the clip to the backend.
@MainActor
final class VoiceInput: ObservableObject {
    static let shared = VoiceInput()
    @Published var recording = false
    @Published var denied = false
    /// Menu bar switch: off means the right Option key is ignored. Survives restarts.
    @Published var enabled = UserDefaults.standard.object(forKey: "voiceKeyEnabled") as? Bool ?? true {
        didSet {
            UserDefaults.standard.set(enabled, forKey: "voiceKeyEnabled")
            if !enabled { discard() }
        }
    }
    private var recorder: AVAudioRecorder?
    private var monitors: [Any] = []
    private var started = Date()
    private var limit: Task<Void, Never>?
    private var badge: NSPanel?
    private var badgeDelay: Task<Void, Never>?
    /// The listening chip waits this long, so a quick double tap goes straight to the type box.
    private static let badgeWait: Duration = .milliseconds(220)
    private var inputPanel: TypePanel?
    private var lastPress = Date.distantPast
    /// Two right-Option presses within this window open the text box instead of recording.
    private static let doubleTap: TimeInterval = 0.4
    private var meter: Timer?
    private var loudest: Float = -160
    /// Peak level (dB) below which a clip is room noise, not speech.
    private static let speechFloor: Float = -40

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
        // Another modifier joining the hold (Shift, Command, ...) makes it a shortcut, not speech.
        guard key == Self.rightOptionKey else { discard(); return }
        let held = NSEvent.ModifierFlags(rawValue: flags)
            .intersection(.deviceIndependentFlagsMask).subtracting(.capsLock)
        if flags & Self.rightOptionBit != 0 {
            // Shift/Command/Control already down means a shortcut, not a spoken command.
            guard held == .option else { discard(); return }
            let now = Date()
            if inputPanel?.isVisible != true, now.timeIntervalSince(lastPress) < Self.doubleTap {
                lastPress = .distantPast
                discard()
                showInput()
            } else {
                lastPress = now
                if inputPanel?.isVisible != true { begin() }
            }
        } else { finish() }
    }

    private func begin() {
        let backend = Backend.shared
        guard enabled, !recording, backend.canSubmit else { return }
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
            try? FileManager.default.removeItem(at: url)
            backend.append("error", "녹음을 시작하지 못했습니다.")
            return
        }
        recorder = new
        new.isMeteringEnabled = true
        loudest = -160
        meter = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { _ in
            Task { @MainActor in
                let voice = VoiceInput.shared
                guard let rec = voice.recorder else { return }
                rec.updateMeters()
                voice.loudest = max(voice.loudest, rec.peakPower(forChannel: 0))
            }
        }
        started = Date()
        recording = true
        badgeDelay?.cancel()
        badgeDelay = Task { @MainActor in
            try? await Task.sleep(for: Self.badgeWait)
            if !Task.isCancelled, recording { showBadge() }
        }
        // Safety net: a missed key-up (app switched without Input Monitoring) must not record forever.
        limit = Task { @MainActor in
            try? await Task.sleep(for: .seconds(30))
            if !Task.isCancelled { finish() }
        }
    }

    func discard() {
        guard recording, let current = recorder else { return }
        recording = false
        badgeDelay?.cancel()
        badge?.orderOut(nil)
        meter?.invalidate()
        limit?.cancel()
        recorder = nil
        current.stop()
        try? FileManager.default.removeItem(at: current.url)
    }

    fileprivate func finish() {
        guard recording, let current = recorder else { return }
        recording = false
        badgeDelay?.cancel()
        badge?.orderOut(nil)
        meter?.invalidate()
        limit?.cancel()
        recorder = nil
        current.stop()
        // A tap shorter than this is an accidental press, not a command.
        // Nothing but silence: drop it so no "transcribing" state or wrong text ever appears.
        if Date().timeIntervalSince(started) < 0.4 || loudest < Self.speechFloor {
            try? FileManager.default.removeItem(at: current.url)
            return
        }
        Backend.shared.sendVoice(current.url)
    }

    /// Small always-on-top "listening" pill at the bottom-left of the screen.
    /// Plain AppKit on purpose: an NSHostingView inside a borderless panel crashed in constraint updates.
    private func showBadge() {
        if badge == nil {
            let size = NSSize(width: ChipStyle.width, height: ChipStyle.height)
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
            pill.layer?.cornerRadius = ChipStyle.radius
            pill.layer?.masksToBounds = true
            let dot = NSView(frame: NSRect(x: 20, y: 18, width: 8, height: 8))
            dot.wantsLayer = true
            dot.layer?.backgroundColor = NSColor.systemRed.cgColor
            dot.layer?.cornerRadius = 4
            let label = NSTextField(labelWithString: "듣는 중")
            label.font = .systemFont(ofSize: 15, weight: .medium)
            label.frame = NSRect(x: 38, y: 11, width: size.width - 58, height: 22)
            pill.addSubview(dot)
            pill.addSubview(label)
            panel.contentView = pill
            badge = panel
        }
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            badge?.setFrameOrigin(NSPoint(x: area.minX + ChipStyle.margin, y: area.minY + ChipStyle.margin))
        }
        badge?.orderFrontRegardless()
    }

    /// Second quick tap: the listening pill becomes a text box at the same spot. Return sends, Esc closes.
    private func showInput() {
        let size = NSSize(width: ChipStyle.width, height: ChipStyle.height)
        let panel = inputPanel ?? TypePanel(contentRect: NSRect(origin: .zero, size: size),
                                            styleMask: [.borderless, .nonactivatingPanel],
                                            backing: .buffered, defer: false)
        if inputPanel == nil {
            panel.level = .statusBar
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            let pill = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
            pill.material = .hudWindow
            pill.state = .active
            pill.wantsLayer = true
            pill.layer?.cornerRadius = ChipStyle.radius
            pill.layer?.masksToBounds = true
            let field = NSTextField(frame: NSRect(x: 20, y: 11, width: size.width - 40, height: 22))
            field.placeholderString = "Symbio에게 입력…"
            field.isBordered = false
            field.drawsBackground = false
            field.focusRingType = .none
            field.font = .systemFont(ofSize: 15)
            field.delegate = panel
            panel.field = field
            panel.onDone = { [weak self] text in
                self?.inputPanel?.orderOut(nil)
                if let text { Backend.shared.submit(text) }
            }
            pill.addSubview(field)
            panel.contentView = pill
            inputPanel = panel
        }
        badge?.orderOut(nil)
        panel.field?.stringValue = ""
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: area.minX + ChipStyle.margin, y: area.minY + ChipStyle.margin))
        }
        // No NSApp.activate: it would raise the chat window too. A nonactivating panel can still take keys.
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(panel.field)
    }
}

/// Borderless panels refuse key focus by default; the text box needs it to receive typing.
private final class TypePanel: NSPanel, NSTextFieldDelegate {
    var field: NSTextField?
    var onDone: ((String?) -> Void)?
    override var canBecomeKey: Bool { true }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        if selector == #selector(NSResponder.insertNewline(_:)) {
            let text = (field?.stringValue ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            onDone?(text.isEmpty ? nil : text)
            return true
        }
        if selector == #selector(NSResponder.cancelOperation(_:)) { onDone?(nil); return true }
        return false
    }

    override func resignKey() {
        super.resignKey()
        orderOut(nil)
    }
}

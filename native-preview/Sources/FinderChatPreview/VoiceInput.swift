import AppKit
import AVFoundation

/// Push-to-talk: records only while the right Option key is held, then hands the clip to the backend.
@MainActor
final class VoiceInput: ObservableObject {
    static let shared = VoiceInput()
    @Published var recording = false
    @Published var denied = false
    private var recorder: AVAudioRecorder?
    private var monitors: [Any] = []
    private var started = Date()

    /// Right Option is key code 61; the device-dependent bit 0x40 tells it apart from the left one.
    private static let rightOptionKey: UInt16 = 61
    private static let rightOptionBit: UInt = 0x40

    func install() {
        guard monitors.isEmpty else { return }
        // Local sees keys while this app is frontmost; global needs Input Monitoring permission.
        let handler: (NSEvent) -> Void = { event in
            let key = event.keyCode, flags = event.modifierFlags.rawValue
            Task { @MainActor in VoiceInput.shared.flagsChanged(key: key, flags: flags) }
        }
        if let local = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged, handler: { event in
            handler(event)
            return event
        }) { monitors.append(local) }
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
    }

    private func finish() {
        guard recording, let current = recorder else { return }
        recording = false
        recorder = nil
        current.stop()
        // A tap shorter than this is an accidental press, not a command.
        if Date().timeIntervalSince(started) < 0.4 {
            try? FileManager.default.removeItem(at: current.url)
            return
        }
        Backend.shared.sendVoice(current.url)
    }
}

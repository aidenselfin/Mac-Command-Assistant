import AppKit

/// Small always-on-top note at the bottom-left of the screen: what was heard, asked and answered.
/// Plain AppKit like the listening pill (an NSHostingView in a borderless panel crashed).
@MainActor
final class Toast {
    static let shared = Toast()
    private var panel: NSPanel?
    private let label = NSTextField(wrappingLabelWithString: "")
    private var hide: Task<Void, Never>?
    private static let width: CGFloat = 320

    private var asked = ""

    /// A reply is shown under the question it answers, so a fast answer does not hide what was heard.
    func show(_ role: String, _ text: String) {
        let flat = text.replacingOccurrences(of: "\n", with: " ")
        let shown = flat.count > 160 ? String(flat.prefix(160)) + "…" : flat
        if role == "user" { asked = "나 · " + shown }
        label.stringValue = role == "user" ? asked
            : (asked.isEmpty ? "" : asked + "\n") + (role == "error" ? "오류 · " : "") + shown
        label.textColor = role == "error" ? .systemRed : .labelColor
        if role != "user" { asked = "" }  // the question belongs to this answer only
        let panel = panel ?? makePanel()
        let height = label.sizeThatFits(NSSize(width: Self.width - 24, height: .greatestFiniteMagnitude)).height + 16
        label.frame = NSRect(x: 12, y: 8, width: Self.width - 24, height: height - 16)
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            // Sits above the "listening" pill (30pt tall at the same corner).
            panel.setFrame(NSRect(x: area.minX + 16, y: area.minY + 56, width: Self.width, height: height), display: true)
        }
        panel.orderFrontRegardless()
        hide?.cancel()
        hide = Task { @MainActor in
            try? await Task.sleep(for: .seconds(max(5, Double(shown.count) / 12)))
            if !Task.isCancelled { panel.orderOut(nil) }
        }
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: Self.width, height: 40),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let box = NSVisualEffectView(frame: panel.contentRect(forFrameRect: panel.frame))
        box.material = .hudWindow
        box.state = .active
        box.autoresizingMask = [.width, .height]
        box.wantsLayer = true
        box.layer?.cornerRadius = 10
        box.layer?.masksToBounds = true
        label.font = .systemFont(ofSize: 12)
        label.maximumNumberOfLines = 6
        label.lineBreakMode = .byTruncatingTail
        box.addSubview(label)
        panel.contentView = box
        self.panel = panel
        return panel
    }
}

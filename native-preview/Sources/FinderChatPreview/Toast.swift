import AppKit
import QuartzCore

/// Shared look of every floating chip (listening, type box, note, file box, choices) so they line up.
enum ChipStyle {
    static let width: CGFloat = 360
    static let height: CGFloat = 44
    static let radius: CGFloat = 14
    static let margin: CGFloat = 16
    static let gap: CGFloat = 8
    /// Bottom of the note/choice stack: it sits above the 44pt listening/type chip.
    static let stackY: CGFloat = margin + height + gap
}

/// Small spinning ring (track + rotating arc) shown while the model is working.
private final class SpinnerView: NSView {
    private let arc = CAShapeLayer()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        let ring = NSBezierPath(ovalIn: bounds.insetBy(dx: 2, dy: 2)).cgPath
        let track = CAShapeLayer()
        for layer in [track, arc] {
            layer.frame = bounds
            layer.path = ring
            layer.fillColor = nil
            layer.lineWidth = 2.5
            layer.lineCap = .round
            self.layer?.addSublayer(layer)
        }
        track.strokeColor = NSColor.tertiaryLabelColor.cgColor
        arc.strokeColor = NSColor.controlAccentColor.cgColor
        arc.strokeEnd = 0.28
    }

    required init?(coder: NSCoder) { fatalError() }

    /// Layer animations are dropped when the panel is hidden, so they are (re)added on each show.
    func spin() {
        arc.removeAllAnimations()
        let turn = CABasicAnimation(keyPath: "transform.rotation.z")
        turn.fromValue = 0
        turn.toValue = -2 * Double.pi
        turn.duration = 0.9
        turn.repeatCount = .infinity
        arc.anchorPoint = CGPoint(x: 0.5, y: 0.5)
        arc.position = CGPoint(x: bounds.midX, y: bounds.midY)
        arc.add(turn, forKey: "turn")
    }
}

private extension NSBezierPath {
    var cgPath: CGPath {
        let path = CGMutablePath()
        var points = [NSPoint](repeating: .zero, count: 3)
        for index in 0..<elementCount {
            switch element(at: index, associatedPoints: &points) {
            case .moveTo: path.move(to: points[0])
            case .lineTo: path.addLine(to: points[0])
            case .curveTo, .cubicCurveTo: path.addCurve(to: points[2], control1: points[0], control2: points[1])
            case .closePath: path.closeSubpath()
            default: break
            }
        }
        return path
    }
}

/// A chip background that opens the chat window when clicked.
private final class ClickableEffectView: NSVisualEffectView {
    var onClick: (() -> Void)?
    override func mouseDown(with event: NSEvent) { onClick?() }
}

/// Small always-on-top note at the bottom-left of the screen: what was heard, asked and answered.
/// Plain AppKit like the listening pill (an NSHostingView in a borderless panel crashed).
@MainActor
final class Toast {
    static let shared = Toast()
    private var panel: NSPanel?
    private let label = NSTextField(wrappingLabelWithString: "")
    private var hide: Task<Void, Never>?
    private static let width = ChipStyle.width

    private let spinner = SpinnerView(frame: NSRect(x: 12, y: 0, width: 18, height: 18))
    private let wait = NSTextField(labelWithString: "")
    private var thinkingText: String?
    private static let waitRow: CGFloat = 26

    private var asked = ""

    /// Shows the spinner row under the last question; nil removes it and lets the note fade out.
    func thinking(_ text: String?, fresh: Bool = false) {
        let wasWaiting = thinkingText != nil
        thinkingText = text
        if fresh { asked = ""; label.stringValue = "" }  // a new voice input must not show the previous exchange
        guard text != nil else {
            spinner.isHidden = true
            wait.isHidden = true
            if wasWaiting, let panel, panel.isVisible { layout(); scheduleHide(seconds: 3) }  // a shown reply keeps its own timer
            return
        }
        layout()
        panel?.orderFrontRegardless()
        hide?.cancel()
    }

    /// A reply is shown under the question it answers, so a fast answer does not hide what was heard.
    func show(_ role: String, _ text: String) {
        let flat = text.components(separatedBy: .newlines).joined(separator: " ")
        let shown = flat.count > 160 ? String(flat.prefix(160)) + "…" : flat
        if role == "user" { asked = "나 · " + (flat.count > 60 ? String(flat.prefix(60)) + "…" : flat) }  // short, so the answer fits below
        label.stringValue = role == "user" ? asked
            : (asked.isEmpty ? "" : asked + "\n") + (role == "error" ? "오류 · " : "") + shown
        label.textColor = role == "error" ? .systemRed : .labelColor
        if role != "user" && role != "notice" { asked = "" }  // the question belongs to this answer only
        if !(role == "notice" && thinkingText != nil) { thinkingText = nil }  // a notice does not end the waiting
        layout()
        if thinkingText != nil { return }
        scheduleHide(seconds: max(5, Double(shown.count) / 12))
    }

    /// The choice chips carry the question themselves, so the note must not sit under them.
    func dismiss() { hide?.cancel(); panel?.orderOut(nil) }

    private func scheduleHide(seconds: Double) {
        guard let panel else { return }
        hide?.cancel()
        hide = Task { @MainActor in
            try? await Task.sleep(for: .seconds(seconds))
            if !Task.isCancelled { panel.orderOut(nil) }
        }
    }

    private func layout() {
        let panel = panel ?? makePanel()
        let busy = thinkingText != nil
        spinner.isHidden = !busy
        wait.isHidden = !busy
        if busy { wait.stringValue = thinkingText ?? ""; spinner.spin() }
        let extra: CGFloat = busy ? Self.waitRow : 0
        let textHeight = label.stringValue.isEmpty ? 0
            : label.sizeThatFits(NSSize(width: Self.width - 24, height: .greatestFiniteMagnitude)).height
        let height = textHeight + extra + 16
        label.isHidden = label.stringValue.isEmpty
        label.frame = NSRect(x: 12, y: 8 + extra, width: Self.width - 24, height: textHeight)
        spinner.frame.origin = NSPoint(x: 12, y: 10)
        wait.frame = NSRect(x: 38, y: 9, width: Self.width - 50, height: 18)
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            // Sits above the "listening" chip (same width, same corner).
            panel.setFrame(NSRect(x: area.minX + ChipStyle.margin, y: area.minY + ChipStyle.stackY, width: Self.width, height: height), display: true)
        }
        panel.orderFrontRegardless()
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: Self.width, height: 40),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let box = ClickableEffectView(frame: panel.contentRect(forFrameRect: panel.frame))
        box.onClick = { AppDelegate.showChat() }
        box.material = .hudWindow
        box.state = .active
        box.autoresizingMask = [.width, .height]
        box.wantsLayer = true
        box.layer?.cornerRadius = ChipStyle.radius
        box.layer?.masksToBounds = true
        label.font = .systemFont(ofSize: 12)
        label.maximumNumberOfLines = 6
        label.lineBreakMode = .byTruncatingTail
        wait.font = .systemFont(ofSize: 12)
        wait.textColor = .secondaryLabelColor
        box.addSubview(label)
        box.addSubview(spinner)
        box.addSubview(wait)
        panel.contentView = box
        self.panel = panel
        return panel
    }
}

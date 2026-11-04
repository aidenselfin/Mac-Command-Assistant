import AppKit

/// Floating question with one chip per answer, shown when the chat window is closed and the user
/// has to pick (which file, approve or not). Plain AppKit like Toast.
@MainActor
final class ChoiceChips {
    static let shared = ChoiceChips()
    private var panel: NSPanel?
    private var actions: [() -> Void] = []
    private static let row: CGFloat = 32

    /// options: chip label + what a click does. The panel stays until `hide()`.
    func show(_ question: String, _ options: [(label: String, action: () -> Void)]) {
        let panel = panel ?? makePanel()
        actions = options.map(\.action)
        let box = NSVisualEffectView(frame: NSRect(origin: .zero, size: panel.frame.size))
        box.material = .hudWindow
        box.state = .active
        box.wantsLayer = true
        box.layer?.cornerRadius = ChipStyle.radius
        box.layer?.masksToBounds = true
        let inner = ChipStyle.width - 24
        let label = NSTextField(wrappingLabelWithString: question)
        label.font = .systemFont(ofSize: 12)
        label.maximumNumberOfLines = 4
        label.lineBreakMode = .byTruncatingTail
        let textHeight = label.sizeThatFits(NSSize(width: inner, height: .greatestFiniteMagnitude)).height
        let height = 12 + textHeight + 8 + CGFloat(options.count) * (Self.row + 6) + 6
        for (index, option) in options.enumerated() {
            let button = NSButton(title: "\(index + 1)  \(option.label)", target: self, action: #selector(clicked(_:)))
            button.tag = index
            button.isBordered = false
            button.alignment = .left
            button.lineBreakMode = .byTruncatingTail
            button.font = .systemFont(ofSize: 13, weight: .medium)
            button.wantsLayer = true
            button.layer?.backgroundColor = NSColor.quaternaryLabelColor.cgColor
            button.layer?.cornerRadius = 8
            let top = height - 12 - textHeight - 8 - CGFloat(index + 1) * (Self.row + 6) + 6
            button.frame = NSRect(x: 12, y: top, width: inner, height: Self.row)
            box.addSubview(button)
        }
        label.frame = NSRect(x: 12, y: height - 12 - textHeight, width: inner, height: textHeight)
        box.addSubview(label)
        panel.contentView = box
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            panel.setFrame(NSRect(x: area.minX + ChipStyle.margin, y: area.minY + ChipStyle.stackY,
                                  width: ChipStyle.width, height: height), display: true)
        }
        panel.orderFrontRegardless()
    }

    func hide() { panel?.orderOut(nil); actions = [] }

    @objc private func clicked(_ sender: NSButton) {
        guard actions.indices.contains(sender.tag) else { return }
        let action = actions[sender.tag]
        hide()
        action()
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: ChipStyle.width, height: 80),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.panel = panel
        return panel
    }
}

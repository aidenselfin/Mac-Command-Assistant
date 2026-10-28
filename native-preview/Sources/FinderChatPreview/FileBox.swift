import AppKit

/// Floating box with the found file, shown over every app and full-screen space so the user can
/// drag the file out without bringing Symbio forward. Plain AppKit like Toast (NSHostingView crashed).
@MainActor
final class FileBox {
    static let shared = FileBox()
    private var panel: NSPanel?
    private let chip = FileChip()
    private let name = NSTextField(labelWithString: "")
    private let icon = NSImageView()
    private static let size = NSSize(width: 260, height: 48)

    /// nil hides the box; the caller has already checked the file is inside an allowed folder.
    func show(_ file: URL?) {
        guard let file else { panel?.orderOut(nil); return }
        let panel = panel ?? makePanel()
        chip.file = file
        name.stringValue = file.lastPathComponent
        icon.image = NSWorkspace.shared.icon(forFile: file.path)
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: area.maxX - Self.size.width - 16, y: area.minY + 16))
        }
        panel.orderFrontRegardless()
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(origin: .zero, size: Self.size),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        let box = NSVisualEffectView(frame: NSRect(origin: .zero, size: Self.size))
        box.material = .hudWindow
        box.state = .active
        box.wantsLayer = true
        box.layer?.cornerRadius = 10
        box.layer?.masksToBounds = true
        chip.frame = box.bounds
        chip.toolTip = "끌어서 원하는 곳에 놓으세요"
        chip.onDropped = { [weak panel] in panel?.orderOut(nil) }
        icon.frame = NSRect(x: 12, y: 12, width: 24, height: 24)
        name.font = .systemFont(ofSize: 13, weight: .medium)
        name.lineBreakMode = .byTruncatingMiddle
        name.frame = NSRect(x: 44, y: 14, width: Self.size.width - 84, height: 20)
        let close = NSButton(image: NSImage(systemSymbolName: "xmark", accessibilityDescription: "닫기")!,
                             target: panel, action: #selector(NSWindow.orderOut(_:)))
        close.isBordered = false
        close.frame = NSRect(x: Self.size.width - 32, y: 14, width: 20, height: 20)
        for view in [icon, name] as [NSView] { chip.addSubview(view) }
        box.addSubview(chip)
        box.addSubview(close)
        panel.contentView = box
        self.panel = panel
        return panel
    }
}

/// The draggable part of the box: drag starts here and carries the file URL to the target app.
private final class FileChip: NSView, NSDraggingSource {
    var file: URL?
    var onDropped: (() -> Void)?

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }  // drag on the first click
    override func hitTest(_ point: NSPoint) -> NSView? { frame.contains(point) ? self : nil }

    override func mouseDown(with event: NSEvent) {}  // keep the press here so mouseDragged follows

    override func mouseDragged(with event: NSEvent) {
        guard let file else { return }
        let item = NSDraggingItem(pasteboardWriter: file as NSURL)
        item.setDraggingFrame(NSRect(x: 12, y: 12, width: 24, height: 24),
                              contents: NSWorkspace.shared.icon(forFile: file.path))
        beginDraggingSession(with: [item], event: event, source: self)
    }

    func draggingSession(_ session: NSDraggingSession,
                         sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }

    func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint,
                         operation: NSDragOperation) {
        if operation != [] { onDropped?() }
    }
}

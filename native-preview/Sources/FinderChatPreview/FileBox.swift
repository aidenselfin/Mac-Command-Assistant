import AppKit

/// Floating stack of file chips (one per file), shown over every app and full-screen space so the user
/// can drag a file out without bringing Symbio forward. Plain AppKit like Toast (NSHostingView crashed).
@MainActor
final class FileBox {
    static let shared = FileBox()
    private var panel: NSPanel?
    private var files: [URL] = []
    /// File names while the box is on screen (UI test snapshot).
    var shownFiles: [String]? { panel?.isVisible == true ? files.map(\.lastPathComponent) : nil }

    /// Empty hides the box; the caller has already checked every file is inside an allowed folder.
    func show(_ files: [URL]) {
        self.files = files
        guard !files.isEmpty else { panel?.orderOut(nil); return }
        let panel = panel ?? makePanel()
        let count = CGFloat(files.count)
        let height = count * ChipStyle.height + (count - 1) * ChipStyle.gap
        let root = NSView(frame: NSRect(x: 0, y: 0, width: ChipStyle.width, height: height))
        for (index, file) in files.enumerated() {
            let top = height - CGFloat(index + 1) * ChipStyle.height - CGFloat(index) * ChipStyle.gap
            root.addSubview(row(file, index: index, y: top))
        }
        panel.contentView = root
        if let area = (NSScreen.main ?? NSScreen.screens.first)?.visibleFrame {
            panel.setFrame(NSRect(x: area.maxX - ChipStyle.width - ChipStyle.margin, y: area.minY + ChipStyle.margin,
                                  width: ChipStyle.width, height: height), display: true)
        }
        panel.orderFrontRegardless()
    }

    @objc private func close(_ sender: NSButton) { drop(sender.tag) }

    private func drop(_ index: Int) {
        guard files.indices.contains(index) else { return }
        var rest = files
        rest.remove(at: index)
        show(rest)
    }

    private func row(_ file: URL, index: Int, y: CGFloat) -> NSView {
        let size = NSSize(width: ChipStyle.width, height: ChipStyle.height)
        let box = NSVisualEffectView(frame: NSRect(origin: NSPoint(x: 0, y: y), size: size))
        box.material = .hudWindow
        box.state = .active
        box.wantsLayer = true
        box.layer?.cornerRadius = ChipStyle.radius
        box.layer?.masksToBounds = true
        let chip = FileChip(frame: box.bounds)
        chip.file = file
        chip.toolTip = "끌어서 원하는 곳에 놓으세요"
        // Deferred: the drop callback runs inside the chip's own drag session, which show() replaces.
        chip.onDropped = { [weak self] in DispatchQueue.main.async { self?.drop(index) } }
        let icon = NSImageView(image: NSWorkspace.shared.icon(forFile: file.path))
        icon.frame = NSRect(x: 12, y: 10, width: 24, height: 24)
        let name = NSTextField(labelWithString: file.lastPathComponent)
        name.font = .systemFont(ofSize: 13, weight: .medium)
        name.lineBreakMode = .byTruncatingMiddle
        name.frame = NSRect(x: 44, y: 12, width: ChipStyle.width - 84, height: 20)
        for view in [icon, name] as [NSView] { chip.addSubview(view) }
        let close = NSButton(image: NSImage(systemSymbolName: "xmark", accessibilityDescription: "닫기")!,
                             target: self, action: #selector(close(_:)))
        close.tag = index
        close.isBordered = false
        close.frame = NSRect(x: ChipStyle.width - 32, y: 12, width: 20, height: 20)
        box.addSubview(chip)
        box.addSubview(close)
        return box
    }

    private func makePanel() -> NSPanel {
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: ChipStyle.width, height: ChipStyle.height),
                            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = .statusBar
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
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

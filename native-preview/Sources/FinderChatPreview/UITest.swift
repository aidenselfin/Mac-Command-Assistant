import AppKit

/// Scripted UI check (`scripts/ui_check.py run`). SYMBIO_UI_TEST_RUNTIME swaps the real backend for a
/// scripted one that sends `_test` events: press keys or click like the user, then report what is on screen.
/// Off in the normal app: the variable is never set by run.sh or the installed app.
@MainActor
enum UITest {
    static let runtime = ProcessInfo.processInfo.environment["SYMBIO_UI_TEST_RUNTIME"].map {
        URL(fileURLWithPath: $0)
    }
    static var active: Bool { runtime != nil }

    private static var chat: NSWindow? { NSApp.windows.first { $0.title == "Symbio" } }

    static func handle(_ value: [String: Any]) {
        let name = value["do"] as? String ?? ""
        var ok = true
        switch name {
        case "snapshot":
            Backend.shared.send("_snapshot", fields: ["state": snapshot()])
            return
        case "option_double_tap":
            for _ in 0..<2 {  // right Option down (device bit 0x40) then up, as the key monitor sees it
                VoiceInput.shared.flagsChanged(key: 61, flags: NSEvent.ModifierFlags.option.rawValue | 0x40)
                VoiceInput.shared.flagsChanged(key: 61, flags: 0)
            }
        case "click_toast": ok = click(Toast.shared.panelWindow)
        case "key": ok = press(value["key"] as? String ?? "")
        case "open_chat": AppDelegate.showChat()
        case "close_chat": chat?.performClose(nil)
        // Stops the scripted backend like a quit does, then ends; the menu's quit path is not under test.
        case "quit":
            Backend.shared.shutdown { exit(0) }
            return
        default: ok = false
        }
        Backend.shared.send("_done", fields: ["do": name, "ok": ok])
    }

    static func snapshot() -> [String: Any] {
        let backend = Backend.shared
        return [
            "state": backend.state,
            "chat_visible": chat?.isVisible == true,
            "windows": NSApp.windows.map { "\($0.title)|\($0.isVisible)|\(type(of: $0))" },
            "input_visible": VoiceInput.shared.inputShown,
            "toast": Toast.shared.shownText ?? NSNull(),
            "chips": ChoiceChips.shared.shownLabels ?? NSNull(),
            "file_box": FileBox.shared.shownFiles ?? NSNull(),
            "drag_files": backend.dragFiles.map(\.lastPathComponent),
            "messages": backend.messages.map { "\($0.role): \($0.text)" },
        ]
    }

    /// A real mouse down/up in the middle of the window; false if it is hidden or ignores the mouse.
    private static func click(_ window: NSWindow?) -> Bool {
        guard let window, window.isVisible, !window.ignoresMouseEvents else { return false }
        let point = NSPoint(x: window.frame.width / 2, y: window.frame.height / 2)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            guard let event = NSEvent.mouseEvent(
                with: type, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                windowNumber: window.windowNumber, context: nil, eventNumber: 0, clickCount: 1, pressure: 1)
            else { return false }
            window.sendEvent(event)
        }
        return true
    }

    /// "cmd+shift+o": goes through the app menu like a real shortcut; false if no menu item took it.
    private static func press(_ combo: String) -> Bool {
        var parts = combo.lowercased().split(separator: "+").map(String.init)
        guard let key = parts.popLast() else { return false }
        let names: [String: NSEvent.ModifierFlags] = ["cmd": .command, "shift": .shift, "opt": .option, "ctrl": .control]
        let flags = parts.reduce(into: NSEvent.ModifierFlags()) { $0.formUnion(names[$1] ?? []) }
        guard let event = NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: flags, timestamp: ProcessInfo.processInfo.systemUptime,
            windowNumber: chat?.windowNumber ?? 0, context: nil, characters: key, charactersIgnoringModifiers: key,
            isARepeat: false, keyCode: 0)
        else { return false }
        return NSApp.mainMenu?.performKeyEquivalent(with: event) ?? false
    }
}

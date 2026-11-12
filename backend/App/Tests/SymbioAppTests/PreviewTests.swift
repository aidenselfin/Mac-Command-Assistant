import Foundation
import Testing

/// tests/test_native_preview.py: run.sh signs its bundle, reuses an unchanged app and refuses worktrees.

private let fakeTool = """
    #!/bin/zsh
    case ${0:t} in
    swift)
        if (( ${@[(Ie)--show-bin-path]} )); then print -r -- "$FAKE_BIN"; fi ;;
    codesign)
        if (( ${@[(Ie)--force]} )); then
            print sign >> "$FAKE_LOG"
            touch "$FAKE_VALID"
            # Signing changes the executable; a repeat launch must compare the build source.
            print -rn -- ' signed' >> "${@[-1]}/Contents/MacOS/FinderChatPreview"
        elif [[ ! -e "$FAKE_VALID" ]]; then
            exit 1
        fi ;;
    open)
        [[ -e "$FAKE_VALID" ]] || { print -u2 'opening an unsigned bundle'; exit 1; }
        (( ${@[(Ie)-n]} )) && { print -u2 'must not create another app instance'; exit 1; }
        print open >> "$FAKE_LOG" ;;
    esac
    """

@Test func previewLaunchRepairsTheSignatureAndDoesNotRebuildAnUnchangedApp() throws {
    let tmp = try temporaryFolder()
    let project = tmp.appendingPathComponent("preview with spaces")
    let binary = project.appendingPathComponent(".build/debug")
    let commands = tmp.appendingPathComponent("commands")
    for folder in [binary, commands] { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
    for name in ["run.sh", "Info.plist"] {
        try FileManager.default.copyItem(
            at: repoRoot.appendingPathComponent("native-preview/\(name)"), to: project.appendingPathComponent(name))
    }
    try Data("first build".utf8).write(to: binary.appendingPathComponent("FinderChatPreview"))
    for name in ["swift", "codesign", "open", "ps"] {
        let path = commands.appendingPathComponent(name).path
        FileManager.default.createFile(atPath: path, contents: Data(fakeTool.utf8), attributes: [.posixPermissions: 0o755])
    }
    let log = tmp.appendingPathComponent("calls"), valid = tmp.appendingPathComponent("valid-signature")
    var environment = ProcessInfo.processInfo.environment
    environment["PATH"] = commands.path + ":" + (environment["PATH"] ?? "/usr/bin:/bin")
    environment["FAKE_BIN"] = binary.path
    environment["FAKE_LOG"] = log.path
    environment["FAKE_VALID"] = valid.path

    func launch() throws {
        let (status, _, error) = try run("/bin/zsh", [project.appendingPathComponent("run.sh").path],
                                         cwd: tmp, environment: environment)
        #expect(status == 0, "\(error)")
    }
    func calls() throws -> [String] { try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init) }

    try launch()
    try launch()
    #expect(try calls() == ["sign", "open", "open"])
    let runtime = try JSONSerialization.jsonObject(
        with: Data(contentsOf: project.appendingPathComponent(".build/runtime.json"))) as? [String: String]
    #expect(runtime?["backend"] == binary.path + "/symbio-bridge" && runtime?["project"] == tmp.path)
    try Data("second build".utf8).write(to: binary.appendingPathComponent("FinderChatPreview"))
    try launch()
    #expect(try calls().suffix(2) == ["sign", "open"])
    try FileManager.default.removeItem(at: valid)  // a damaged signature is repaired even with the same source hash
    try launch()
    #expect(try calls().suffix(2) == ["sign", "open"])
}

@Test func runShRefusesToLaunchFromAWorktree() throws {
    let tmp = try temporaryFolder()
    let main = tmp.appendingPathComponent("repo")
    try FileManager.default.createDirectory(at: main.appendingPathComponent("native-preview"), withIntermediateDirectories: true)
    try FileManager.default.copyItem(
        at: repoRoot.appendingPathComponent("native-preview/run.sh"), to: main.appendingPathComponent("native-preview/run.sh"))
    let git = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for args in [["init", "-q"], ["add", "."], ["commit", "-qm", "x"]] { try run("/usr/bin/git", git + args, cwd: main) }
    let tree = tmp.appendingPathComponent("feature_a")
    try run("/usr/bin/git", git + ["worktree", "add", "-q", tree.path], cwd: main)
    let (status, _, error) = try run("/bin/zsh", [tree.appendingPathComponent("native-preview/run.sh").path])
    #expect(status == 1)
    #expect(error.contains("worktree에서는 앱을 실행하지 않습니다"))
}

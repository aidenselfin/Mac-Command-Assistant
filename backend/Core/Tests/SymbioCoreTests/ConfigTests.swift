import Foundation
import Testing
@testable import SymbioCore

/// A fresh canonical temporary folder (Python `tmp_path`).
func temporaryFolder() throws -> URL {
    let url = URL(fileURLWithPath: realPath(NSTemporaryDirectory())!, isDirectory: true)
        .appendingPathComponent(UUID().uuidString, isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

func write(_ text: String, to url: URL) throws {
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data(text.utf8).write(to: url)
}

func mkdir(_ url: URL) throws { try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true) }

/// Python conftest `config` fixture.
func testConfig(_ tmp: URL) throws -> Config {
    let root = tmp.appendingPathComponent("voice-action-test")
    try write("hello", to: root.appendingPathComponent("한글 메모.txt"))
    return try loadConfig(cwd: tmp, home: tmp, environ: ["OPENAI_API_KEY": "test-secret"])
}

@Test func defaultsAndSecretRepr() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    let config = try testConfig(tmp)
    #expect(config.allowedDirectories[0].path.hasPrefix("/"))
    #expect(config.model == "gpt-4o-mini")
    var dumped = ""
    dump(config, to: &dumped)
    for shown in ["\(config)", String(reflecting: config), dumped] {
        #expect(!shown.contains("test-secret"))
        #expect(shown.contains("gpt-4o-mini"))
    }
}

@Test func precedence() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let home = tmp.appendingPathComponent("home")
    try write(#"{"model":"home"}"#, to: home.appendingPathComponent(".voice-action/config.json"))
    let local = tmp.appendingPathComponent("config.json")
    try write(#"{"model":"local"}"#, to: local)
    var env = ["OPENAI_API_KEY": "test", "VOICE_ACTION_MODEL": "environment"]
    #expect(try loadConfig(overrides: ["model": "cli"], cwd: tmp, home: home, environ: env).model == "cli")
    #expect(try loadConfig(cwd: tmp, home: home, environ: env).model == "environment")
    env["VOICE_ACTION_MODEL"] = nil
    #expect(try loadConfig(cwd: tmp, home: home, environ: env).model == "local")
    try FileManager.default.removeItem(at: local)
    #expect(try loadConfig(cwd: tmp, home: home, environ: env).model == "home")
}

@Test func localGroqEnv() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    try write("GROQ_API_KEY=local-secret\n", to: tmp.appendingPathComponent(".voice-action/config.env"))
    let config = try loadConfig(cwd: tmp, home: tmp.appendingPathComponent("home"), environ: [:])
    #expect(config.provider == "groq")
    #expect(config.model == "openai/gpt-oss-20b")
    #expect(config.baseURL == "https://api.groq.com/openai/v1")
    #expect(config.apiKey == "local-secret")
}

@Test func openrouterProvider() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let config = try loadConfig(
        overrides: ["provider": "openrouter"], cwd: tmp, home: tmp.appendingPathComponent("home"),
        environ: ["OPENROUTER_API_KEY": "router-secret", "GROQ_API_KEY": "unused"])
    #expect(config.model == "anthropic/claude-sonnet-5.5")
    #expect(config.baseURL == "https://openrouter.ai/api/v1")
    #expect(config.apiKey == "router-secret")
}

@Test func switchingProviderKeepsJSONModelUnlessGiven() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    try write(#"{"provider": "groq", "model": "openai/gpt-oss-20b"}"#, to: tmp.appendingPathComponent("config.json"))
    let env = ["OPENROUTER_API_KEY": "k", "GROQ_API_KEY": "g"]
    let h = tmp.appendingPathComponent("h")
    let kept = try loadConfig(overrides: ["provider": "openrouter"], cwd: tmp, home: h, environ: env)
    #expect(kept.model == "openai/gpt-oss-20b")
    let switched = try loadConfig(
        overrides: [
            "provider": "openrouter", "model": "anthropic/claude-sonnet-5.5",
            "base_url": "https://openrouter.ai/api/v1",
        ], cwd: tmp, home: h, environ: env)
    #expect(switched.model == "anthropic/claude-sonnet-5.5")
    #expect(switched.baseURL == "https://openrouter.ai/api/v1")
    #expect(switched.apiKey == "k")
}

@Test(arguments: [
    ["provider": "unsupported"], ["allowed_directories": []], ["allowed_directories": ["missing"]],
    ["max_tool_rounds": 0], ["max_tool_rounds": 1.5], ["max_tool_rounds": true], ["max_run_seconds": 0],
    ["max_run_seconds": "inf"], ["escalation_model": 3], ["mcp_timeout_seconds": "nan"],
    ["llm_timeout_seconds": -1], ["base_url": "https://secret@example.com"],
] as [JSON])
func invalidConfig(_ override: JSON) throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    #expect(throws: ConfigError.self) {
        try loadConfig(overrides: override, cwd: tmp, home: tmp, environ: ["OPENAI_API_KEY": "test"])
    }
}

@Test func missingKey() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    let error = #expect(throws: ConfigError.self) { try loadConfig(cwd: tmp, home: tmp, environ: [:]) }
    #expect(error?.message.contains("OPENAI_API_KEY") == true)
}

@Test func jsonSecretRejected() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try write(#"{"api_key": "secret"}"#, to: tmp.appendingPathComponent("config.json"))
    let error = #expect(throws: ConfigError.self) { try loadConfig(cwd: tmp, home: tmp, environ: [:]) }
    #expect(error?.message.contains("환경변수") == true)
}

@Test func legacyGlobalSecretsAreIgnored() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let home = tmp.appendingPathComponent("home")
    let legacy = home.appendingPathComponent(".voice-action/config.json")
    let original = #"{"openrouter_api_key":"old-secret","whisper_model":"base"}"#
    try write(original, to: legacy)
    let config = try loadConfig(cwd: tmp, home: home, environ: ["GROQ_API_KEY": "current-secret"])
    #expect(config.apiKey == "current-secret")
    #expect(try String(contentsOf: legacy, encoding: .utf8) == original)
    let error = #expect(throws: ConfigError.self) { try loadConfig(cwd: tmp, home: home, environ: [:]) }
    #expect(error?.message.contains("OPENAI_API_KEY") == true)
}

@Test func finderSwitchesDefaultOnAndAcceptOff() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let base = ["OPENAI_API_KEY": "k"]
    let config = try loadConfig(cwd: tmp, home: tmp, environ: base)
    #expect(config.finderReveal && config.finderDock)
    let off = try loadConfig(
        cwd: tmp, home: tmp,
        environ: base.merging(["VOICE_ACTION_FINDER_REVEAL": "off", "VOICE_ACTION_FINDER_DOCK": "off"]) { $1 })
    #expect(!off.finderReveal && !off.finderDock)
    #expect(throws: ConfigError.self) {
        try loadConfig(cwd: tmp, home: tmp, environ: base.merging(["VOICE_ACTION_FINDER_DOCK": "maybe"]) { $1 })
    }
}

@Test func fallbackOnlyWithOtherProvider() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let home = tmp.appendingPathComponent("home")
    let both = ["GROQ_API_KEY": "g", "OPENAI_API_KEY": "o", "VOICE_ACTION_FALLBACK_MODEL": "m"]
    let config = try loadConfig(cwd: tmp, home: home, environ: both)
    #expect(config.fallbackApiKey == "o" && config.fallbackModel == "m")
    #expect(try loadConfig(cwd: tmp, home: home, environ: ["GROQ_API_KEY": "g"]).fallbackApiKey == nil)
    #expect(try loadConfig(cwd: tmp, home: home, environ: ["OPENAI_API_KEY": "o"]).fallbackApiKey == nil)
}

@Test func recoveryLimitsAndStrongerModel() throws {
    let tmp = try temporaryFolder()
    defer { try? FileManager.default.removeItem(at: tmp) }
    try mkdir(tmp.appendingPathComponent("voice-action-test"))
    let home = tmp.appendingPathComponent("home")
    let plain = try loadConfig(cwd: tmp, home: home, environ: ["GROQ_API_KEY": "g"])
    #expect(plain.maxToolRounds == 20 && plain.maxRunSeconds == 90 && plain.escalationModel == nil)
    let env = [
        "GROQ_API_KEY": "g", "VOICE_ACTION_ESCALATION_MODEL": "anthropic/claude-sonnet-5.5",
        "VOICE_ACTION_MAX_RUN_SECONDS": "45",
    ]
    // The stronger model reuses the OpenRouter key; without one it stays off.
    #expect(try loadConfig(cwd: tmp, home: home, environ: env).escalationModel == nil)
    let on = try loadConfig(cwd: tmp, home: home, environ: env.merging(["OPENROUTER_API_KEY": "r"]) { $1 })
    #expect(on.escalationModel == "anthropic/claude-sonnet-5.5" && on.maxRunSeconds == 45)
    let fromJSON = try loadConfig(
        overrides: ["escalation_model": "x/y"], cwd: tmp, home: home,
        environ: ["GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "r"])
    #expect(fromJSON.escalationModel == "x/y")
}

@Test func envFileParsesLikePythonDotenv() {
    let text = """
        # comment
        export A=1
        B = two words  # note
        C="line\\nnext" # c
        D='raw\\n'
        E
        F=
        bad line here
        'G'=x#y
        H="multi
        line"
        """
    let parsed = parseEnvFile(text)
    #expect(parsed.map(\.0) == ["A", "B", "C", "D", "E", "F", "G", "H"])
    #expect(parsed.map(\.1) == ["1", "two words", "line\nnext", "raw\\n", nil, "", "x#y", "multi\nline"])
}

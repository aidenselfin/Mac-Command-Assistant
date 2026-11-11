import Foundation

/// Python `config.py`: validated configuration; secrets never come from JSON configuration.

public struct ConfigError: Error, Equatable, Sendable, CustomStringConvertible {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var description: String { message }
}

public struct Provider: Sendable {
    public let name: String
    public let keyVariable: String
    public let defaultModel: String
    public let defaultBaseURL: String?
}

/// Python `PROVIDERS` (provider -> API key variable, default model, default base_url), in order.
public let providers: [Provider] = [
    Provider(name: "openai", keyVariable: "OPENAI_API_KEY", defaultModel: "gpt-4o-mini", defaultBaseURL: nil),
    Provider(
        name: "groq", keyVariable: "GROQ_API_KEY", defaultModel: "openai/gpt-oss-20b",
        defaultBaseURL: "https://api.groq.com/openai/v1"),
    Provider(
        name: "openrouter", keyVariable: "OPENROUTER_API_KEY", defaultModel: "anthropic/claude-sonnet-5.5",
        defaultBaseURL: "https://openrouter.ai/api/v1"),
]

/// API keys stay out of `description`, `debugDescription` and `dump()` (Python `repr=False`).
public struct Config: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var provider: String
    public var model: String
    public var apiKey: String
    public var allowedDirectories: [URL]
    public var baseURL: String?
    public var maxToolRounds: Int
    public var maxRunSeconds: Double  // one command's model/tool loop, not counting approval waits
    public var llmTimeoutSeconds: Double
    public var mcpTimeoutSeconds: Double
    public var maxToolResultChars: Int
    public var feedbackEnabled: Bool
    public var feedbackApiKey: String?
    public var voiceApiKey: String?  // Groq Whisper (push-to-talk)
    public var fallbackApiKey: String?  // second provider on LLM failure
    public var fallbackModel: String
    public var feedbackModel: String
    public var feedbackDailyLimit: Int
    public var feedbackAutonomy: Bool
    public var finderReveal: Bool
    public var finderDock: Bool  // put the revealed Finder window beside the app window
    /// A stronger OpenRouter model that takes over after two failed recoveries; nil = off.
    public var escalationModel: String?

    public init(
        provider: String, model: String, apiKey: String, allowedDirectories: [URL], baseURL: String? = nil,
        maxToolRounds: Int = 20, maxRunSeconds: Double = 90, llmTimeoutSeconds: Double = 60,
        mcpTimeoutSeconds: Double = 30, maxToolResultChars: Int = 50000, feedbackEnabled: Bool = true,
        feedbackApiKey: String? = nil, voiceApiKey: String? = nil, fallbackApiKey: String? = nil,
        fallbackModel: String = "gpt-4o-mini", feedbackModel: String = "anthropic/claude-sonnet-5.5",
        feedbackDailyLimit: Int = 20, feedbackAutonomy: Bool = true, finderReveal: Bool = true,
        finderDock: Bool = true, escalationModel: String? = nil
    ) {
        self.provider = provider
        self.model = model
        self.apiKey = apiKey
        self.allowedDirectories = allowedDirectories
        self.baseURL = baseURL
        self.maxToolRounds = maxToolRounds
        self.maxRunSeconds = maxRunSeconds
        self.llmTimeoutSeconds = llmTimeoutSeconds
        self.mcpTimeoutSeconds = mcpTimeoutSeconds
        self.maxToolResultChars = maxToolResultChars
        self.feedbackEnabled = feedbackEnabled
        self.feedbackApiKey = feedbackApiKey
        self.voiceApiKey = voiceApiKey
        self.fallbackApiKey = fallbackApiKey
        self.fallbackModel = fallbackModel
        self.feedbackModel = feedbackModel
        self.feedbackDailyLimit = feedbackDailyLimit
        self.feedbackAutonomy = feedbackAutonomy
        self.finderReveal = finderReveal
        self.finderDock = finderDock
        self.escalationModel = escalationModel
    }

    public var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "provider": provider, "model": model, "allowedDirectories": allowedDirectories.map(\.path),
                "baseURL": baseURL as Any, "maxToolRounds": maxToolRounds, "maxRunSeconds": maxRunSeconds,
                "llmTimeoutSeconds": llmTimeoutSeconds, "mcpTimeoutSeconds": mcpTimeoutSeconds,
                "maxToolResultChars": maxToolResultChars, "feedbackEnabled": feedbackEnabled,
                "fallbackModel": fallbackModel, "feedbackModel": feedbackModel,
                "feedbackDailyLimit": feedbackDailyLimit, "feedbackAutonomy": feedbackAutonomy,
                "finderReveal": finderReveal, "finderDock": finderDock, "escalationModel": escalationModel as Any,
            ], displayStyle: .struct)
    }

    public var description: String {
        let fields = customMirror.children.map { "\($0.label ?? ""): \(String(describing: $0.value))" }
        return "Config(" + fields.joined(separator: ", ") + ")"
    }

    public var debugDescription: String { description }
}

/// Python `load_config`. nil `cwd`/`home`/`environ` mean the real process values.
public func loadConfig(
    overrides: JSON = [:], cwd: URL? = nil, home: URL? = nil, environ: [String: String]? = nil
) throws(ConfigError) -> Config {
    let cwd = cwd ?? URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true)
    let home = home ?? FileManager.default.homeDirectoryForCurrentUser
    // An optional user-owned env file is a convenience for GUI-launched terminals.
    var merged: [String: String] = [:]
    for base in [home, cwd] {
        for (key, value) in loadEnvFile(base.appendingPathComponent(".voice-action/config.env")) {
            merged[key] = value  // a bare `KEY` line (nil) hides earlier values, like Python None
        }
    }
    merged.merge(environ ?? ProcessInfo.processInfo.environment) { _, new in new }
    let env = merged
    func text(_ name: String) -> String? { env[name].flatMap { $0.isEmpty ? nil : $0 } }  // `env.get(x) or ...`
    func stripped(_ name: String) -> String? { text(name).map(trim).flatMap { $0.isEmpty ? nil : $0 } }

    var data: JSON = [
        "provider": .string(text("GROQ_API_KEY") != nil ? "groq" : "openai"),
        "allowed_directories": ["./voice-action-test"],
    ]
    let local = cwd.appendingPathComponent("config.json")
    for file in [home.appendingPathComponent(".voice-action/config.json"), local]
    where FileManager.default.fileExists(atPath: file.path) {
        guard let bytes = try? Data(contentsOf: file), let value = try? JSON.parse(bytes) else {
            throw ConfigError("설정 파일을 읽을 수 없습니다: \(file.path)")
        }
        guard let pairs = value.objectValue else { throw ConfigError("설정은 JSON object여야 합니다.") }
        if file == local, pairs.contains(where: { $0.0.lowercased().contains("key") }) {
            throw ConfigError("API key는 JSON 대신 환경변수에 설정하세요.")
        }
        // Older global configs stored credentials alongside settings. Never read
        // those credentials; tolerate their presence without modifying the file.
        for (key, item) in pairs where !key.lowercased().contains("key") { data.set(key, item) }
    }
    for key in [
        "provider", "model", "base_url", "allowed_directories", "max_tool_rounds", "max_run_seconds",
        "llm_timeout_seconds", "mcp_timeout_seconds", "max_tool_result_chars",
    ] {
        guard let raw = env["VOICE_ACTION_" + key.uppercased()] else { continue }
        if key == "allowed_directories" {
            guard let parsed = try? JSON.parse(raw) else {
                throw ConfigError("VOICE_ACTION_ALLOWED_DIRECTORIES는 JSON 배열이어야 합니다.")
            }
            data.set(key, parsed)
        } else {
            data.set(key, .string(raw))
        }
    }
    for (key, value) in overrides.objectValue ?? [] where !value.isNull { data.set(key, value) }

    guard let name = data["provider"]?.stringValue, let provider = providers.first(where: { $0.name == name })
    else { throw ConfigError("지원 provider: " + providers.map(\.name).joined(separator: ", ")) }
    if !(data.objectValue ?? []).contains(where: { $0.0 == "model" }) {
        data.set("model", .string(provider.defaultModel))
    }
    guard let apiKey = env[provider.keyVariable], !trim(apiKey).isEmpty else {
        throw ConfigError("\(provider.keyVariable) 환경변수가 필요합니다.")
    }
    guard let model = data["model"]?.stringValue, !trim(model).isEmpty else {
        throw ConfigError("model이 필요합니다.")
    }
    guard let roots = data["allowed_directories"]?.arrayValue, !roots.isEmpty else {
        throw ConfigError("allowed_directories는 비어 있지 않은 배열이어야 합니다.")
    }
    var resolved: [URL] = []
    for root in roots {
        guard let raw = root.stringValue, !trim(raw).isEmpty else {
            throw ConfigError("허용 경로는 비어 있지 않은 문자열이어야 합니다.")
        }
        let expanded = (raw as NSString).expandingTildeInPath
        guard let path = realPath(expanded.hasPrefix("/") ? expanded : cwd.path + "/" + expanded) else {
            throw ConfigError("허용 디렉터리가 존재하지 않거나 해석할 수 없습니다.")
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw ConfigError("디렉터리가 아닙니다: \(path)")
        }
        resolved.append(URL(fileURLWithPath: path, isDirectory: true))
    }

    func positive(_ key: String, _ fallback: Double, integer: Bool) throws(ConfigError) -> Double {
        let raw = data[key] ?? .double(fallback)
        let number: Double? =
            switch raw {
            case let .int(value): Double(value)
            case let .double(value): integer && value != value.rounded(.towardZero) ? nil : value
            case let .string(value): integer ? Int(trim(value)).map(Double.init) : Double(trim(value))
            default: nil  // bool, null, list, object
            }
        guard let number, number.isFinite, number > 0, !integer || Int(exactly: number) != nil else {
            throw ConfigError("\(key)는 양수\(integer ? " 정수" : "")여야 합니다.")
        }
        return number
    }
    let maxToolRounds = Int(try positive("max_tool_rounds", 20, integer: true))
    let maxRunSeconds = try positive("max_run_seconds", 90, integer: false)
    let llmTimeout = try positive("llm_timeout_seconds", 60, integer: false)
    let mcpTimeout = try positive("mcp_timeout_seconds", 30, integer: false)
    let maxResultChars = Int(try positive("max_tool_result_chars", 50000, integer: true))

    var baseURL = provider.defaultBaseURL
    if let raw = data["base_url"], !raw.isNull {
        guard let value = raw.stringValue else { throw ConfigError("base_url은 URL 문자열이어야 합니다.") }
        baseURL = value
    }
    if let baseURL {
        // Stricter than Python urlparse: an empty user name or any password is refused too.
        let parts = URLComponents(string: baseURL)
        guard let parts, let scheme = parts.scheme?.lowercased(), scheme == "https" || scheme == "http",
            !(parts.host ?? "").isEmpty, parts.user == nil, parts.password == nil
        else { throw ConfigError("base_url은 인증정보 없는 HTTP(S) URL이어야 합니다.") }
    }

    func onOff(_ name: String) throws(ConfigError) -> Bool {
        let value = trim(text(name) ?? "on").lowercased()
        guard value == "on" || value == "off" else { throw ConfigError("\(name)는 on 또는 off여야 합니다.") }
        return value == "on"
    }
    let feedback = try onOff("VOICE_ACTION_FEEDBACK")
    let autonomy = try onOff("VOICE_ACTION_FEEDBACK_AUTONOMY")
    let finder = try onOff("VOICE_ACTION_FINDER_REVEAL")
    let dock = try onOff("VOICE_ACTION_FINDER_DOCK")
    var dailyLimit = 20
    if let raw = text("VOICE_ACTION_FEEDBACK_DAILY_LIMIT") {
        guard let value = Int(trim(raw)), value > 0 else {
            throw ConfigError("VOICE_ACTION_FEEDBACK_DAILY_LIMIT는 양의 정수여야 합니다.")
        }
        dailyLimit = value
    }
    let escalationRaw: JSON =
        text("VOICE_ACTION_ESCALATION_MODEL").map(JSON.string)
        ?? (truthy(data["escalation_model"]) ? data["escalation_model"]! : "")
    guard let escalation = escalationRaw.stringValue else {
        throw ConfigError("escalation_model은 모델 이름 문자열이어야 합니다.")
    }
    let openrouterKey = stripped("OPENROUTER_API_KEY")
    return Config(
        provider: provider.name, model: model, apiKey: apiKey, allowedDirectories: resolved, baseURL: baseURL,
        maxToolRounds: maxToolRounds, maxRunSeconds: maxRunSeconds, llmTimeoutSeconds: llmTimeout,
        mcpTimeoutSeconds: mcpTimeout, maxToolResultChars: maxResultChars, feedbackEnabled: feedback,
        feedbackApiKey: openrouterKey, voiceApiKey: stripped("GROQ_API_KEY"),
        fallbackApiKey: provider.name == "openai" ? nil : stripped("OPENAI_API_KEY"),
        fallbackModel: stripped("VOICE_ACTION_FALLBACK_MODEL") ?? "gpt-4o-mini",
        feedbackModel: stripped("VOICE_ACTION_FEEDBACK_MODEL") ?? "anthropic/claude-sonnet-5.5",
        feedbackDailyLimit: dailyLimit, feedbackAutonomy: autonomy, finderReveal: finder, finderDock: dock,
        // Off without an OpenRouter key: the stronger model reuses that key.
        escalationModel: openrouterKey == nil || trim(escalation).isEmpty ? nil : trim(escalation))
}

private func trim(_ text: String) -> String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

/// Python truthiness of a JSON value (`x or default`).
private func truthy(_ value: JSON?) -> Bool {
    switch value {
    case .none, .some(.null): false
    case let .some(.bool(b)): b
    case let .some(.int(i)): i != 0
    case let .some(.double(d)): d != 0
    case let .some(.string(s)): !s.isEmpty
    case let .some(.array(a)): !a.isEmpty
    case let .some(.object(o)): !o.isEmpty
    }
}

func loadEnvFile(_ url: URL) -> [(String, String?)] {
    guard let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .utf8) else { return [] }
    return parseEnvFile(text)
}

/// python-dotenv `dotenv_values(..., interpolate=False)`: `export`, quoted keys and values
/// (multi-line, escapes), inline ` #` comments. A nil value is a bare `KEY` line; bad lines are skipped.
func parseEnvFile(_ text: String) -> [(String, String?)] {
    let s = Array(text.unicodeScalars)
    var i = 0
    var out: [(String, String?)] = []
    func space(_ c: Unicode.Scalar) -> Bool { c.properties.isWhitespace }
    func newline(_ c: Unicode.Scalar) -> Bool { c == "\n" || c == "\r" }
    func skipInline() { while i < s.count, space(s[i]), !newline(s[i]) { i += 1 } }
    func string(_ part: ArraySlice<Unicode.Scalar>) -> String { String(String.UnicodeScalarView(part)) }
    /// Regex `q((?:\\q|[^q])*)q` with its backtracking; `i` is at the opening quote.
    func quoted(_ q: Unicode.Scalar) -> String? {
        var j = i + 1
        var lastEscape: Int?
        while j < s.count {
            if s[j] == "\\", j + 1 < s.count, s[j + 1] == q {
                lastEscape = j
                j += 2
            } else if s[j] == q {
                defer { i = j + 1 }
                return string(s[(i + 1)..<j])
            } else {
                j += 1
            }
        }
        guard let close = lastEscape.map({ $0 + 1 }) else { return nil }
        defer { i = close + 1 }
        return string(s[(i + 1)..<close])
    }
    func decode(_ raw: String, _ allowed: String) -> String {
        let map: [Unicode.Scalar: Unicode.Scalar] = [
            "\\": "\\", "'": "'", "\"": "\"", "a": "\u{7}", "b": "\u{8}", "f": "\u{C}", "n": "\n", "r": "\r",
            "t": "\t", "v": "\u{B}",
        ]
        let v = Array(raw.unicodeScalars)
        var result = String.UnicodeScalarView()
        var k = 0
        while k < v.count {
            if v[k] == "\\", k + 1 < v.count, allowed.unicodeScalars.contains(v[k + 1]), let c = map[v[k + 1]] {
                result.append(c)
                k += 2
            } else {
                result.append(v[k])
                k += 1
            }
        }
        return String(result)
    }

    while true {
        while i < s.count, space(s[i]) { i += 1 }
        guard i < s.count else { break }
        if i + 6 < s.count, string(s[i..<(i + 6)]) == "export", space(s[i + 6]), !newline(s[i + 6]) {
            i += 6
            skipInline()
        }
        var key: String?
        var failed = false
        if i >= s.count {
            failed = true
        } else if s[i] == "#" {
            key = nil  // a comment line
        } else if s[i] == "'" {
            let end = s[(i + 1)...].firstIndex(of: "'")
            if let end, end > i + 1 {
                key = string(s[(i + 1)..<end])
                i = end + 1
            } else {
                failed = true
            }
        } else {
            let begin = i
            while i < s.count, s[i] != "=", s[i] != "#", !space(s[i]) { i += 1 }
            if i > begin { key = string(s[begin..<i]) } else { failed = true }
        }
        var value: String?
        if !failed {
            skipInline()
            if i < s.count, s[i] == "=" {
                i += 1
                skipInline()
                if i >= s.count || newline(s[i]) {
                    value = ""
                } else if s[i] == "'" || s[i] == "\"" {
                    let q = s[i]
                    if let raw = quoted(q) { value = decode(raw, q == "'" ? "\\'" : "\\'\"abfnrtv") } else { failed = true }
                } else {
                    var part: [Unicode.Scalar] = []
                    while i < s.count, !newline(s[i]) {
                        part.append(s[i])
                        i += 1
                    }
                    // re.sub(r"\s+#.*", "", part).rstrip()
                    if let hash = part.indices.first(where: { $0 > 0 && part[$0] == "#" && space(part[$0 - 1]) }) {
                        var cut = hash - 1
                        while cut > 0, space(part[cut - 1]) { cut -= 1 }
                        part.removeSubrange(cut...)
                    }
                    while let last = part.last, space(last) { part.removeLast() }
                    value = string(part[...])
                }
            }
        }
        if !failed {
            skipInline()
            if i < s.count, s[i] == "#" { while i < s.count, !newline(s[i]) { i += 1 } }
            if i < s.count, !newline(s[i]) { failed = true }
        }
        if failed { while i < s.count, !newline(s[i]) { i += 1 } } else if let key { out.append((key, value)) }
        if i < s.count { i += 1 }  // the line break
    }
    return out
}

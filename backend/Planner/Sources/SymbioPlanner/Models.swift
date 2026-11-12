import Foundation
import SymbioCore

/// Python `planner.py`: the clarification tool, provider schema shaping and the OpenAI-compatible
/// model adapter (Python used the `openai` SDK with `max_retries=0`; this calls the HTTP API directly).

public let clarificationToolName = "request_clarification"
public let clarificationTool: JSON = [
    "type": "function",
    "function": [
        "name": .string(clarificationToolName),
        "description": .string(
            "Ask only when an unresolved ambiguity would materially change the result, or "
                + "when information only the user has (a file name, text to save) is missing. "
                + "This is an application contract, not an MCP filesystem tool."),
        "parameters": [
            "type": "object",
            "properties": [
                "question": ["type": "string", "minLength": 1],
                "options": [
                    "type": "array",
                    "items": ["type": "string", "minLength": 1],
                    "description": "Choices to pick from. Use [] for kind free_text.",
                ],
                "kind": [
                    "type": "string",
                    "enum": ["file_candidate", "edit_criteria", "free_text"],
                    "description": .string(
                        "free_text: the user must type something only they know, such as a "
                            + "file name or the text to save; options must be []."),
                ],
            ],
            "required": ["question", "options", "kind"],
            "additionalProperties": false,
        ],
    ],
]

/// Constraints a provider enforces on a generated call before the app sees it. Breaking one makes
/// the whole answer HTTP 400, where the app itself would only send the model a tool_error.
let schemaKeysDropped: Set<String> = [
    "required", "additionalProperties", "minItems", "maxItems", "minLength", "maxLength",
    "pattern", "uniqueItems", "oneOf", "allOf", "not", "if", "then", "else", "$schema",
]

/// A model often writes null for an argument it does not use; allow it, the app drops it.
func nullable(_ prop: JSON) -> JSON {
    guard case .object = prop, let type = prop["type"]?.stringValue, prop["enum"] == nil else { return prop }
    var copy = prop
    copy.set("type", [.string(type), "null"])
    return copy
}

/// The schema as the provider should check it: types and choices, no hard limits.
public func providerSchema(_ schema: JSON) -> JSON {
    switch schema {
    case let .object(pairs):
        return .object(pairs.compactMap { key, value in
            if schemaKeysDropped.contains(key) { return nil }
            if key == "properties", let props = value.objectValue {
                return (key, .object(props.map { ($0.0, nullable(providerSchema($0.1))) }))
            }
            return (key, providerSchema(value))
        })
    case let .array(items): return .array(items.map(providerSchema))
    default: return schema
    }
}

/// Required arguments stay known to the model, in words, once the schema stops saying so.
public func providerDescription(_ description: String, _ schema: JSON) -> String {
    let required = (schema["required"]?.arrayValue ?? []).compactMap(\.stringValue)
    return required.isEmpty ? description : "\(description) 필수 인자: \(required.joined(separator: ", "))."
}

let providerCode = Pattern(#"[a-z][a-z0-9_]{1,40}"#)
/// "parameters for tool X did not match schema: errors: [`/options`: minItems: got 0, want 1]"
let providerSchemaError = Pattern(
    #"parameters for tool (?<tool>[\w.\-]{1,80}) did not match schema: errors: \[(?<errors>.*)\]"#,
    dotAll: true)
let whitespaceRun = Pattern(#"\s+"#)

/// What a provider's error says about *our* request, and nothing else: the error code and, for a
/// rejected tool call, the tool name and schema violations. Never the raw body or generated arguments.
func providerErrorDetail(_ error: OpenAIAPIError) -> String {
    guard case let .status(_, body) = error, let body, case .object = body else { return "" }
    let code = body["code"]?.stringValue, message = body["message"]?.stringValue
    var parts: [String] = []
    if let code, providerCode.fullMatch(code) { parts.append(code) }
    if let message, let match = providerSchemaError.first(message) {
        let errors = scalarPrefix(pyStrip(whitespaceRun.sub(" ", match["errors"] ?? "")), 200)
        parts.append("\(match["tool"] ?? ""): \(errors)")
    } else if let message, let first = parts.first, ["tool_use_failed", "output_parse_failed"].contains(first),
        !Pattern(#"[\{\}\[\]"]"#).search(message)
    {
        // Only a plain sentence: no braces or quotes, so no generated arguments can ride along.
        parts.append(scalarPrefix(pyStrip(whitespaceRun.sub(" ", message)), 120))
    }
    return parts.isEmpty ? "" : ": " + parts.joined(separator: " — ")
}

/// Retry a failed provider call once on a second provider.
public struct FallbackModel: ModelClient {
    public let primary: any ModelClient
    public let fallback: any ModelClient
    public init(_ primary: any ModelClient, _ fallback: any ModelClient) {
        self.primary = primary
        self.fallback = fallback
    }

    public func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        do {
            return try await primary.generate(
                messages, tools, allowTools: allowTools, allowClarification: allowClarification)
        } catch let error as PlannerError where error.message.hasPrefix("LLM") {}
        return try await fallback.generate(messages, tools, allowTools: allowTools, allowClarification: allowClarification)
    }
}

/// Python `openai.APIError` family as far as the planner tells them apart.
public enum OpenAIAPIError: Error, Equatable, Sendable {
    /// An HTTP error; `body` is the parsed body's `error` member (or the body / its text), like the SDK.
    case status(Int, body: JSON?)
    case timeout
    case connection
}

/// Python `AsyncOpenAI(api_key=, base_url=, timeout=, max_retries=0)`: only `/chat/completions`.
public struct OpenAIClient: Sendable {
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, URLResponse)
    public let apiKey: String
    public let baseURL: String
    public let timeout: Double
    let transport: Transport

    private static let session = URLSession(configuration: .ephemeral)

    /// `baseURL` nil means `OPENAI_BASE_URL` or the OpenAI API, like the SDK.
    public init(apiKey: String, baseURL: String? = nil, timeout: Double = 600, transport: Transport? = nil) {
        self.apiKey = apiKey
        self.baseURL = baseURL ?? ProcessInfo.processInfo.environment["OPENAI_BASE_URL"] ?? "https://api.openai.com/v1"
        self.timeout = timeout
        self.transport = transport ?? { try await OpenAIClient.session.data(for: $0) }
    }

    /// POST the body once (no retries). Throws `OpenAIAPIError` or `CancellationError`.
    public func createChatCompletion(_ body: JSON) async throws -> JSON {
        let base = baseURL.hasSuffix("/") ? String(baseURL.dropLast()) : baseURL
        guard let url = URL(string: base + "/chat/completions") else { throw OpenAIAPIError.connection }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.httpBody = Data(body.dumps().utf8)
        let data: Data, response: URLResponse
        do {
            (data, response) = try await transport(request)
        } catch let error as URLError where error.code == .timedOut {
            throw OpenAIAPIError.timeout
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            throw OpenAIAPIError.connection
        }
        guard let http = response as? HTTPURLResponse else { throw OpenAIAPIError.connection }
        if (200..<300).contains(http.statusCode) {
            guard let parsed = try? JSON.parse(data) else { throw OpenAIAPIError.connection }
            return parsed
        }
        let text = pyStrip(String(decoding: data, as: UTF8.self))
        var parsed = (try? JSON.parse(text)) ?? .string(text)
        if case let .object(pairs) = parsed, let inner = pairs.last(where: { $0.0 == "error" }) { parsed = inner.1 }
        throw OpenAIAPIError.status(http.statusCode, body: parsed)
    }
}

public struct OpenAICompatibleModel: ModelClient {
    public let client: OpenAIClient
    public let model: String
    public init(_ client: OpenAIClient, _ model: String) {
        self.client = client
        self.model = model
    }

    public func generate(
        _ messages: [JSON], _ tools: [ToolDefinition], allowTools: Bool, allowClarification: Bool
    ) async throws -> ModelTurn {
        var providerTools: [JSON] = tools.map { tool in
            var description = providerDescription(tool.description, tool.schema)
            if tool.name == filesystemReadTool {  // null for an unused argument is allowed
                description += " 전체 파일을 읽을 때 head와 tail은 생략하세요."
            }
            return [
                "type": "function",
                "function": ["name": .string(tool.name), "description": .string(description), "parameters": providerSchema(tool.schema)],
            ]
        }
        if allowTools && allowClarification, let function = clarificationTool["function"],
            let parameters = function["parameters"]
        {
            providerTools.append([
                "type": "function",
                "function": [
                    "name": function["name"]!,
                    "description": .string(providerDescription(function["description"]?.stringValue ?? "", parameters)),
                    "parameters": providerSchema(parameters),
                ],
            ])
        }
        let names = providerTools.compactMap { $0["function"]?["name"]?.stringValue }.joined(separator: ", ")
        let toolChoice: JSON =
            allowTools && !allowClarification && tools.count == 1
            ? ["type": "function", "function": ["name": .string(tools[0].name)]]
            : allowTools ? "auto" : "none"

        func create(_ extra: [JSON] = []) async throws -> JSON {
            try await client.createChatCompletion([
                "model": .string(model), "messages": .array(messages + extra),
                "tools": .array(providerTools), "tool_choice": toolChoice,
            ])
        }

        let response: JSON
        do {
            do {
                response = try await create()
            } catch let error as OpenAIAPIError where providerErrorDetail(error).hasPrefix(": tool_use_failed") {
                // The model sometimes names a tool this request doesn't offer; one retry that
                // lists the real names almost always fixes it.
                response = try await create([[
                    "role": "system",
                    "content": .string("이번 요청에서 호출 가능한 도구: \(names). 이 목록에 없는 도구는 호출하지 마세요."),
                ]])
            }
        } catch let error as OpenAIAPIError {
            switch error {
            case .status(401, _): throw PlannerError("LLM 인증 실패")
            case .timeout: throw PlannerError("LLM 응답 시간 초과")
            case .status(404, _):
                throw PlannerError("LLM API 요청 실패 (HTTP 404): 모델 '\(model)'을 찾을 수 없습니다. model 설정을 확인하세요.")
            case let .status(status, _): throw PlannerError("LLM API 요청 실패 (HTTP \(status)\(providerErrorDetail(error)))")
            case .connection: throw PlannerError("LLM API 요청 실패")
            }
        }
        guard let choice = response["choices"]?.arrayValue?.first else {
            throw PlannerError.modelFormat("LLM 응답이 비어 있습니다.")
        }
        let message = choice["message"] ?? .null
        var calls: [ToolCall] = []
        var clarification: ClarificationRequest?
        for call in message["tool_calls"]?.arrayValue ?? [] {
            guard call["type"]?.stringValue == "function" else {
                throw PlannerError.modelFormat("지원하지 않는 LLM 도구 호출 형식입니다.")
            }
            let id = call["id"]?.stringValue ?? ""
            let name = call["function"]?["name"]?.stringValue ?? ""
            let arguments = call["function"]?["arguments"]?.stringValue ?? ""
            if name == clarificationToolName {
                if clarification != nil { throw PlannerError.modelFormat("clarification 요청이 중복되었습니다.") }
                clarification = try Self.parseClarification(id, arguments)
            } else {
                calls.append(ToolCall(id, name, arguments))
            }
        }
        if clarification != nil, !calls.isEmpty {
            throw PlannerError.modelFormat("clarification과 MCP 도구를 동시에 요청할 수 없습니다.")
        }
        return ModelTurn(message["content"]?.stringValue, calls, clarification)
    }

    /// Python `_parse_clarification`.
    static func parseClarification(_ callID: String, _ arguments: String) throws -> ClarificationRequest {
        let invalid = PlannerError.modelFormat("잘못된 clarification 인자입니다.")
        guard let value = try? JSON.parse(arguments), case .object = value else { throw invalid }
        // The provider no longer insists on every field, so a missing or null one is "none given".
        let question = value["question"]?.stringValue
        let options: JSON = truthy(value["options"]) ? value["options"]! : []
        let kind: JSON = truthy(value["kind"]) ? value["kind"]! : (truthy(options) ? "file_candidate" : "free_text")
        guard let question, !pyStrip(question).isEmpty, let items = options.arrayValue,
            items.allSatisfy({ $0.stringValue.map { !pyStrip($0).isEmpty } ?? false }),
            let kindName = kind.stringValue, ["file_candidate", "edit_criteria", "free_text"].contains(kindName)
        else { throw invalid }
        // A question with nothing to pick from is a typed answer, whatever kind it claimed.
        if items.isEmpty || kindName == "free_text" {
            return ClarificationRequest(pyStrip(question), [], "free_text", callID)
        }
        // More buttons than fit are cut here, not refused by the provider's schema check.
        return ClarificationRequest(pyStrip(question), items.prefix(maxOptions).compactMap(\.stringValue), kindName, callID)
    }
}

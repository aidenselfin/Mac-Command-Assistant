import Foundation
import SymbioCore
import Testing

@testable import SymbioPlanner

// tests/test_fallback_model.py

private struct StubModel: ModelClient {
    let result: ModelTurn?
    let error: PlannerError?
    let calls = Counter()
    init(result: ModelTurn? = nil, error: PlannerError? = nil) {
        self.result = result
        self.error = error
    }
    func generate(_: [JSON], _: [ToolDefinition], allowTools _: Bool, allowClarification _: Bool) async throws -> ModelTurn {
        calls.add()
        if let error { throw error }
        return result!
    }
}

private final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func add() { lock.withLock { value += 1 } }
    var count: Int { lock.withLock { value } }
}

private let ok = ModelTurn("ok", [])

@Test func llmFailureUsesFallback() async throws {
    let primary = StubModel(error: PlannerError("LLM API 요청 실패 (HTTP 429)"))
    #expect(try await FallbackModel(primary, StubModel(result: ok)).generate([], []) == ok)
}

@Test func primarySuccessSkipsFallback() async throws {
    let spare = StubModel(result: ModelTurn("no", []))
    #expect(try await FallbackModel(StubModel(result: ok), spare).generate([], []) == ok)
    #expect(spare.calls.count == 0)
}

@Test func nonLLMErrorIsRaised() async {
    await #expect(throws: PlannerError.self) {
        try await FallbackModel(StubModel(error: PlannerError("도구 오류")), StubModel(result: ok)).generate([], [])
    }
}

// The HTTP adapter (tests/test_planner.py, tests/test_free_text.py), with a fake transport.

private final class FakeServer: @unchecked Sendable {
    private let lock = NSLock()
    private var responses: [(Int, String)]
    private(set) var requests: [JSON] = []
    private(set) var headers: [[String: String]] = []
    init(_ responses: [(Int, String)]) { self.responses = responses }

    var client: OpenAIClient {
        OpenAIClient(apiKey: "test", baseURL: "https://api.example.test/v1/", timeout: 5) { request in
            try self.handle(request)
        }
    }

    private func handle(_ request: URLRequest) throws -> (Data, URLResponse) {
        try lock.withLock {
            requests.append(try JSON.parse(request.httpBody!))
            headers.append(request.allHTTPHeaderFields ?? [:])
            #expect(request.url?.absoluteString == "https://api.example.test/v1/chat/completions")
            let (status, body) = responses.count > 1 ? responses.removeFirst() : responses[0]
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            return (Data(body.utf8), response)
        }
    }
}

private func completion(_ message: JSON) -> String {
    JSON.object([("choices", [["index": 0, "message": message]])]).text
}

private func toolCallMessage(_ name: String, _ arguments: String, id: String = "tool-1") -> JSON {
    ["role": "assistant", "content": nil, "tool_calls": [["id": .string(id), "type": "function", "function": ["name": .string(name), "arguments": .string(arguments)]]]]
}

@Test func openAIAdapterWireFormat() async throws {
    let server = FakeServer([(200, completion(toolCallMessage("filesystem__list_directory", #"{"path":"/tmp"}"#)))])
    let turn = try await OpenAICompatibleModel(server.client, "test-model").generate(
        [["role": "user", "content": "목록"]],
        [ToolDefinition(name: "filesystem__list_directory", description: "List", schema: ["type": "object"])])
    #expect(turn.toolCalls == [ToolCall("tool-1", "filesystem__list_directory", #"{"path":"/tmp"}"#)])
    let sent = server.requests[0]
    #expect(sent["tools"]?.arrayValue?[0]["function"]?["name"] == "filesystem__list_directory")
    #expect(sent["model"] == "test-model")
    #expect(sent["tool_choice"] == "auto")
    #expect(server.headers[0]["Authorization"] == "Bearer test")
}

@Test func authErrorRedacted() async {
    let server = FakeServer([(401, #"{"error": {"message": "secret-payload"}}"#)])
    do {
        _ = try await OpenAICompatibleModel(server.client, "m").generate([], [])
        Issue.record("no error")
    } catch {
        #expect("\(error)" == "LLM 인증 실패")
    }
}

@Test func clarificationToolCallBecomesAClarification() async throws {
    let arguments = #"{"question": "기준을 선택해 주세요.", "options": ["구조", "문장"], "kind": "edit_criteria"}"#
    let server = FakeServer([(200, completion(toolCallMessage("request_clarification", arguments, id: "clarify-1")))])
    let turn = try await OpenAICompatibleModel(server.client, "m").generate(
        [["role": "user", "content": "정리"]],
        [ToolDefinition(name: "filesystem__write_file", description: "Write", schema: ["type": "object"])])
    #expect(turn.toolCalls.isEmpty)
    #expect(turn.clarification == ClarificationRequest("기준을 선택해 주세요.", ["구조", "문장"], "edit_criteria", "clarify-1"))
    let names = server.requests[0]["tools"]?.arrayValue?.compactMap { $0["function"]?["name"]?.stringValue }
    #expect(names == ["filesystem__write_file", "request_clarification"])
}

@Test func readProviderSchemaAcceptsNullWithoutChangingMCPSchema() async throws {
    let schema: JSON = [
        "type": "object",
        "properties": [
            "path": ["type": "string"],
            "head": ["type": "integer", "minimum": 0],
            "tail": ["type": "integer", "minimum": 0],
        ],
        "required": ["path"],
        "additionalProperties": false,
    ]
    let server = FakeServer([(200, #"{"choices": []}"#)])
    let model = OpenAICompatibleModel(server.client, "test")
    await #expect(throws: PlannerError.modelFormat("LLM 응답이 비어 있습니다.")) {
        try await model.generate([], [ToolDefinition(name: "filesystem__read_text_file", description: "Read", schema: schema)])
    }
    let sent = server.requests[0]["tools"]!.arrayValue![0]["function"]!
    let parameters = sent["parameters"]!
    for options: JSON in [[:], ["head": nil, "tail": nil], ["head": 1]] {
        var value: JSON = ["path": "/source.txt"]
        for (key, item) in options.objectValue! { value.set(key, item) }
        #expect(JSONSchema.isValid(value, schema: parameters))
    }
    #expect(!JSONSchema.isValid(["path": "/source.txt", "head": "1"], schema: parameters))
    // A null path passes the provider's check on purpose: the app answers with a tool error.
    #expect(JSONSchema.isValid(["path": nil], schema: parameters))
    #expect(sent["description"]?.stringValue?.hasSuffix("head와 tail은 생략하세요.") == true)
    await #expect(throws: PlannerError.self) {
        try await model.generate(
            [], [ToolDefinition(name: "web__browser_navigate", description: "Navigate", schema: ["type": "object"])],
            allowTools: true, allowClarification: false)
    }
    #expect(server.requests[1]["tool_choice"] == ["type": "function", "function": ["name": "web__browser_navigate"]])
}

private func failingModel(_ body: String, status: Int = 400) async -> String {
    let server = FakeServer([(status, body)])
    do {
        _ = try await OpenAICompatibleModel(server.client, "m").generate([["role": "user", "content": "x"]], [])
        return "no error"
    } catch {
        return "\(error)"
    }
}

private func errorBody(_ body: JSON) -> String { JSON.object([("error", body)]).text }

@Test func aRejectedToolCallNamesTheToolAndTheSchemaViolation() async {
    let message =
        "Tool call validation failed: tool call validation failed: parameters for tool "
        + "request_clarification did not match schema: errors: [`/options`: minItems: got 0, want 1]"
    let body: JSON = [
        "message": .string(message), "type": "invalid_request_error", "code": "tool_use_failed",
        "failed_generation": #"{"arguments": {"question": "비밀 파일 본문 SECRET-BODY"}}"#,
    ]
    // tool_use_failed is retried once; the same answer then ends the command.
    let text = await failingModel(errorBody(body))
    #expect(text == "LLM API 요청 실패 (HTTP 400: tool_use_failed — request_clarification: `/options`: minItems: got 0, want 1)")
    #expect(!text.contains("SECRET"))
}

@Test(arguments: [
    ("null", "LLM API 요청 실패 (HTTP 400)"),
    ("<html>gateway</html>", "LLM API 요청 실패 (HTTP 400)"),
    (
        errorBody(["message": "context too long: 사용자 글", "code": "context_length_exceeded"]),
        "LLM API 요청 실패 (HTTP 400: context_length_exceeded)"
    ),
    (errorBody(["code": "BAD CODE with spaces <script>", "message": "x"]), "LLM API 요청 실패 (HTTP 400)"),
])
func onlyTheErrorCodeIsKeptWhenTheBodySaysNothingAboutATool(body: String, expected: String) async {
    #expect(await failingModel(body) == expected)
}

@Test(arguments: [
    (
        "Failed to call a function. Please adjust your prompt.",
        "HTTP 400: tool_use_failed — Failed to call a function. Please adjust your prompt."
    ),
    (#"Failed {"name": "x", "arguments": {"secret": "SECRET-BODY"}}"#, "HTTP 400: tool_use_failed"),
])
func aPlainProviderSentenceIsKeptButNothingThatLooksLikeArguments(message: String, expected: String) async {
    let text = await failingModel(errorBody(["code": "tool_use_failed", "message": .string(message)]))
    #expect(text == "LLM API 요청 실패 (\(expected))")
}

@Test func otherStatusesAndModelNotFound() async {
    #expect(await failingModel("{}", status: 404) == "LLM API 요청 실패 (HTTP 404): 모델 'm'을 찾을 수 없습니다. model 설정을 확인하세요.")
    #expect(await failingModel("{}", status: 500) == "LLM API 요청 실패 (HTTP 500)")
}

@Test func aCallToAToolNotOfferedIsRetriedOnceWithTheRealToolNames() async throws {
    let server = FakeServer([
        (400, errorBody(["code": "tool_use_failed", "message": "tool not in request.tools"])),
        (200, completion(["role": "assistant", "content": "완료", "tool_calls": nil])),
    ])
    let turn = try await OpenAICompatibleModel(server.client, "m").generate([["role": "user", "content": "x"]], [])
    #expect(turn.text == "완료")
    #expect(server.requests.count == 2)
    let retry = server.requests[1]["messages"]!.arrayValue!
    #expect(retry.last?["content"]?.stringValue?.contains("호출 가능한 도구") == true)
}

@Test func whatIsSentToTheProviderHasNoRequiredFieldsButNamesThem() async throws {
    let server = FakeServer([(200, completion(["role": "assistant", "content": "끝"]))])
    let tool = ToolDefinition(
        name: "local__convert_file", description: "변환한다.",
        schema: [
            "type": "object", "properties": ["source": ["type": "string", "minLength": 1]],
            "required": ["source"], "additionalProperties": false,
        ])
    _ = try await OpenAICompatibleModel(server.client, "m").generate([["role": "user", "content": "x"]], [tool])
    let sent = server.requests[0]["tools"]!.arrayValue!
    #expect(sent.count == 2)  // the tool and the question tool
    for item in sent {
        #expect(item["function"]?["parameters"]?["required"] == nil)
        #expect(item["function"]?["parameters"]?["additionalProperties"] == nil)
    }
    #expect(sent[0]["function"]?["description"]?.stringValue?.contains("필수 인자: source.") == true)
    let question = sent[1]["function"]!
    #expect(question["description"]?.stringValue?.contains("필수 인자: question, options, kind.") == true)
    #expect(!question["parameters"]!.text.contains("minItems"))
}

// tests/test_free_text.py: the question schema and parsing.

@Test func theQuestionToolAllowsATypedAnswerWithoutOptions() throws {
    let schema = clarificationTool["function"]!["parameters"]!
    #expect(schema["properties"]?["options"]?["minItems"] == nil)
    #expect(schema["properties"]?["options"]?["maxItems"] == nil)
    #expect(schema["properties"]?["kind"]?["enum"]?.arrayValue?.contains("free_text") == true)
    let arguments = #"{"question": "파일 이름을 알려 주세요", "options": [], "kind": "file_candidate"}"#
    #expect(try OpenAICompatibleModel.parseClarification("c", arguments) == ClarificationRequest("파일 이름을 알려 주세요", [], "free_text", "c"))
    let picked = #"{"question": "?", "options": ["a", "b"], "kind": "file_candidate"}"#
    #expect(try OpenAICompatibleModel.parseClarification("c", picked).options == ["a", "b"])
    let typed = #"{"question": "?", "options": ["a"], "kind": "free_text"}"#
    #expect(try OpenAICompatibleModel.parseClarification("c", typed).options == [])
    let many = JSON.object([("question", "?"), ("options", .array((0..<10).map { .string(String($0)) })), ("kind", "file_candidate")]).text
    #expect(try OpenAICompatibleModel.parseClarification("c", many).options.count == 6)
}

@Test func aClarificationWithMissingOrNullFieldsIsReadAsATypedQuestion() throws {
    let parse = OpenAICompatibleModel.parseClarification
    #expect(try parse("c", #"{"question": "이름은?"}"#) == ClarificationRequest("이름은?", [], "free_text", "c"))
    #expect(try parse("c", #"{"question": "이름은?", "options": null, "kind": null}"#).kind == "free_text")
    #expect(try parse("c", #"{"question": "?", "options": ["a"]}"#).kind == "file_candidate")
    for bad in [#"{"options": ["a"]}"#, #"{"question": " "}"#, #"{"question": "?", "options": [1]}"#, "[]", "x"] {
        #expect(throws: PlannerError.self) { try parse("c", bad) }
    }
}

@Test func theProviderSchemaKeepsTypesAndChoicesButNoHardLimits() {
    let schema: JSON = [
        "type": "object",
        "$schema": "x",
        "properties": [
            "format": ["type": "string", "enum": ["a", "b"], "minLength": 1],
            "items": ["type": "array", "items": ["type": "string", "maxLength": 3], "minItems": 1, "maxItems": 2, "uniqueItems": true],
            "head": ["anyOf": [["type": "integer"], ["type": "null"]]],
        ],
        "required": ["format"],
        "additionalProperties": false,
        "oneOf": [["required": ["format"]]],
    ]
    let expected: JSON = [
        "type": "object",
        "properties": [
            "format": ["type": "string", "enum": ["a", "b"]],
            "items": ["type": ["array", "null"], "items": ["type": "string"]],
            "head": ["anyOf": [["type": "integer"], ["type": "null"]]],
        ],
    ]
    #expect(providerSchema(schema) == expected)
    #expect(providerDescription("변환한다.", schema) == "변환한다. 필수 인자: format.")
    #expect(providerDescription("목록.", ["type": "object"]) == "목록.")
}

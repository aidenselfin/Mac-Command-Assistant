# Python → Swift 전환 규칙 (하위 에이전트 공용)

목표: `voice_action/`·`main.py`·`llm_mcp_transcript.py`·`tests/`·`evals/*.py`·`scripts/*.py`를 모두 Swift로 옮긴다.
화면(`native-preview/`)은 그대로이고, 앱이 띄우는 백엔드가 Python → Swift 실행 파일(`symbio-bridge`, 같은 JSON-lines 규약)로 바뀐다.

## 작업 공간
- `backend/<모듈>/Package.swift`: 모듈마다 독립 SwiftPM 패키지(서로 path 의존). 라이브러리 이름은 `Symbio<모듈>`.
- 대응: Core=공용 타입+config.py+attachment.py, Undo=undo.py, Search=file_search.py, FileOps=file_operations.py,
  Delete=deletion.py, Convert=conversion.py, Finder=finder.py, MCP=mcp_client.py+mcp_manager.py, Guard=risk_guard.py,
  Planner=planner.py, Feedback=feedback.py+autonomy.py, Runtime=runtime.py, App=native_bridge·ui·transcribe·main·llm_mcp_transcript(실행 파일),
  Dev=evals/*.py·scripts/*.py(실행 파일).
- 공용 계약은 `backend/Core/Sources/SymbioCore`: `JSON`(순서 유지 + Python `json.dumps` 호환 `dumps()`/`.text`, `JSON.parse`),
  `ToolDefinition`·`ToolResult`·`ToolError`(`isRejected`=Python ToolRejectedError), `validateArguments`(JSON Schema),
  `inside`(허용 폴더·symlink 검사), `boundedJoin`, `realPath`, `isRelative`. 먼저 읽어라.
- 외부 패키지 없이 Foundation·`import SQLite3`·URLSession·Process로 짠다. 꼭 필요하면 보고에 이유를 적는다.
- 비동기는 Swift concurrency(async/await, actor). Swift 6 언어 모드 경고·오류 없이.

## 규칙
1. 동작을 그대로 옮긴다: 한국어 메시지·한도·정렬·보안 검사(허용 폴더, symlink, 승인 nonce)를 약화하지 않는다.
   SQLite 스키마·파일 위치는 Python과 호환(기존 `.voice-action/*.sqlite3`를 그대로 읽는다).
2. 대응 Python 테스트(`tests/test_<모듈>*.py`)의 사례를 `backend/<모듈>/Tests/`로 옮긴다(Swift Testing `import Testing`).
   다른 모듈이 아직 없어 못 옮기는 사례는 보고에 적는다.
3. 자기 패키지 폴더만 고친다. 다른 패키지가 바뀌어야 하면 고치지 말고 보고한다. 의존 패키지 목록을 바꿔야 하면 자기 Package.swift만.
4. 완료 조건: 자기 폴더에서 `swift build`와 `swift test` 통과(경고 없이).
5. git 커밋·Python 파일 삭제는 하지 않는다(오케스트레이터가 한다).
6. 주의: 파일 쓰기 도구가 소스의 `\uXXXX` 글자를 실제 글자로 바꿀 수 있다. 이스케이프가 꼭 필요하면 쓴 뒤 확인한다.
7. 보고(짧게): 공개 API 목록(시그니처), 옮긴 테스트 수, Python과 달라진 점.

# Python → Rust 전환 규칙 (하위 에이전트 공용)

목표: `voice_action/`·`main.py`·`llm_mcp_transcript.py`·`tests/`·`evals/*.py`·`scripts/*.py`를 모두 Rust로 옮긴다.
화면은 SwiftUI(`native-preview/`) 그대로, Swift가 띄우는 백엔드만 Python → Rust 바이너리(`symbio-bridge`)로 바뀐다.

## 작업 공간
- 루트 `Cargo.toml` 워크스페이스, `crates/symbio-<모듈>`. 공용 계약은 `crates/symbio-core`(ToolDefinition·ToolResult·ToolError·`inside`·`validate_arguments`·Config).
- 대응: core=config.py+attachment.py, undo, search=file_search.py, fileops=file_operations.py, delete=deletion.py,
  convert=conversion.py, finder=finder.py, mcp=mcp_client.py+mcp_manager.py, guard=risk_guard.py, planner=planner.py,
  feedback=feedback.py+autonomy.py, runtime=runtime.py, app=native_bridge·ui·transcribe·main·llm_mcp_transcript(바이너리),
  dev=evals/*.py·scripts/*.py(바이너리).
- 비동기는 tokio, HTTP는 reqwest(rustls), SQLite는 rusqlite(bundled). 의존성은 자기 crate의 Cargo.toml에만 추가.

## 규칙
1. 동작을 그대로 옮긴다: 한국어 메시지·한도·정렬·보안 검사(허용 폴더, symlink, 승인 nonce)를 약화하지 않는다.
2. 대응 Python 테스트(`tests/test_<모듈>*.py`)의 사례를 `crates/symbio-<모듈>/tests/*.rs`로 옮긴다. 다른 모듈이 아직 없어 못 옮기는 사례는 보고에 적는다.
3. 자기 crate 폴더만 고친다. 다른 crate가 바뀌어야 하면 고치지 말고 보고한다.
4. 완료 조건: `cargo test -p <crate>`와 `cargo clippy -p <crate> --all-targets -- -D warnings` 통과, `cargo fmt -p <crate>`.
5. git 커밋·Python 파일 삭제는 하지 않는다(오케스트레이터가 한다). 셸에서 `export PATH="$HOME/.cargo/bin:$PATH"`.
6. 보고: 공개 API 목록(시그니처), 옮긴 테스트 수, Python과 달라진 점.

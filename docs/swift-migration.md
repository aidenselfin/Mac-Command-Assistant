# Python → Swift 전환 기록 (2026-11-12 완료)

`voice_action/`·`main.py`·`llm_mcp_transcript.py`·`tests/`·`evals/*.py`·`scripts/*.py`를 모두 Swift로 옮겼다.
화면(`native-preview/`)은 그대로이고, 앱이 띄우는 백엔드만 Python → `symbio-bridge`(같은 JSON-lines 규약)로 바뀌었다.
결정과 이유는 [decisions.md](decisions.md) 2026-11-12 항목.

| 예전 Python | 지금 Swift (`backend/…`) |
|---|---|
| `config.py` `attachment.py` | `Core` (공용 타입·JSON·JSON Schema·허용 폴더 검사도 여기) |
| `undo.py` · `file_search.py` · `file_operations.py` · `deletion.py` · `conversion.py` · `finder.py` | `Undo` · `Search` · `FileOps` · `Delete` · `Convert` · `Finder` |
| `mcp_client.py` `mcp_manager.py` · `risk_guard.py` · `planner.py` · `runtime.py` | `MCP` · `Guard` · `Planner` · `Runtime` |
| `feedback.py` `autonomy.py` | `Feedback` |
| `native_bridge.py` `ui.py` `transcribe.py` · `main.py` `llm_mcp_transcript.py` | `App`: `symbio-bridge` · `symbio lessons|transcript|autonomy` |
| `evals/run_eval.py` `benchmark.py` `make_cases.py` · `scripts/check.py` `vibe.py` `heavy_lock.py` `ui_check.py` | `Dev`: `symbio-dev eval|benchmark|make-cases|check|vibe|heavy-lock|ui-check` |
| `tests/test_*.py` | `backend/<모듈>/Tests/` (Swift Testing) |

지킨 것: 한국어 메시지·한도·정렬·보안 검사(허용 폴더, symlink, 승인 nonce), SQLite 스키마와 `.voice-action/` 파일 위치.
외부 Swift 패키지 없이 Foundation·SQLite3·URLSession·Process만 쓴다. 보관된 웹·활성 앱 코드(`archive/web-macos-mcp/`)는 옮기지 않았다.

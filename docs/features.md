# 기능 등록부

기능을 고치기 전에 이 표에서 **코드·테스트·평가**를 찾는다. 기능을 추가·변경하면 이 표도 같은 커밋에서 갱신한다.
점검 명령은 `scripts/check.sh` (기능 이름 또는 `quick`/`full`). 상태: ✅ 기본 동작 · 🟡 선택(기본 꺼짐).

| 기능 | 상태 | 요청 예 | 코드 | 위험 분류 | 테스트 | eval 과제 | 결정 |
|---|---|---|---|---|---|---|---|
| 목록·읽기·요약 | ✅ | `폴더 파일 목록 보여줘` `test1.md 요약해줘` | `planner.py` + filesystem MCP | 읽기(자동) | `test_planner.py` `test_integration_filesystem.py` | `read_summary` `preserve_unrelated` | |
| 파일 찾기 | ✅ | `회의록 요약해줘` | `file_search.py` | 읽기 | `test_file_search.py` `test_find_files_flow.py` | `find_*` `meeting_*` `clarify_cancel` `missing_file` | 2026-10-07 파일 검색 |
| **의미 검색** | 🟡 `VOICE_ACTION_SEMANTIC_SEARCH=on` | `내 폴더에서 철골 구조물이 나오는 사진 찾아줘` | `semantic_index.py` `file_search.py` | 읽기(항상 후보 확인) | `test_semantic_index.py` `test_integration_features.py` | (없음, 수동 사례는 README) | 2026-10-14 의미 검색 |
| 쓰기·덮어쓰기 | ✅ | `result.txt에 저장해줘` | `planner.py` `risk_guard.py` | 쓰기(승인) | `test_risk_guard.py` `test_integration_undo.py` | `write_*` `overwrite_*` `outside_root_write` | |
| 삭제(단일·패턴) | ✅ | `*.txt 모두 삭제해줘` | `deletion.py` | 쓰기(승인·백업) | `test_deletion.py` | `delete_*` `attach_delete` `neg_delete_too_much` | |
| **이동·복사·폴더 생성** | ✅ | `보고서를 보관 폴더로 옮겨줘` | `file_operations.py` | 쓰기(승인·Undo) | `test_file_operations.py` | `move_*` `create_directory_undo` | 2026-10-14 파일 정리 도구 |
| 형식 변환·문서 읽기 | ✅ | `pdf를 텍스트로 바꿔줘` | `conversion.py` | 변환=쓰기(승인), 읽기 | `test_conversion.py` `test_conversion_review.py` | `convert_*` `read_document_html` | |
| 웹 읽기·저장 | ✅ | `https://… 읽고 요약` | `playwright_client.py` | 읽기 + 저장은 승인 | `test_integration_playwright.py` | (없음, 로컬 HTTP fixture 통합 테스트로 검증) | |
| 활성 앱 조회 | ✅(macOS) | `현재 활성 앱` | `macos_client.py` | 읽기 | `test_macos_client.py` | | |
| Undo | ✅ | `undo` `되돌리기` | `undo.py` | | `test_undo.py` `test_integration_undo.py` | `overwrite_undo` `create_directory_undo` | |
| 직전 대화 기억·후속 질문 | ✅ | `그 요약을 파일로 저장해줘` | `planner.py`(Exchange) | | `test_followup.py` `test_free_text.py` | `follow_up_*` | |
| 자가 피드백(평가·lesson) | ✅ 피드백 on | 1~5 평가 | `feedback.py` | | `test_feedback*.py` `test_lesson_candidates.py` | `evals/run_eval.py --lesson` | 2026-10-04 lesson |
| Tk UI + Finder 패널 | ✅ `--ui` | | `ui.py` `finder.py` `attachment.py` | | `test_ui.py` `test_ui_review.py` `test_finder.py` | | 2026-10-14 Finder 패널 |
| **네이티브 SwiftUI 앱** | ✅ `native-preview/run.sh` | | `native_bridge.py` `native-preview/` | 승인은 Python이 보유 | `test_native_bridge.py` `test_native_preview.py` `test_native_protocol.py` | | 2026-10-14 네이티브 백엔드 |
| 수정 세션·worktree 도구 | ✅ | | `scripts/vibe.py` | | `test_vibe.py` | | 2026-10-14 병렬 수정 세션 |

과제 이름은 `evals/tasks/*.json` 파일명이다. `neg_*`와 `document_injection`은 "하면 안 되는 것"을 확인하는 부정 과제로 모든 기능에 공통이다.

## 기능 사이의 약속 (깨면 안 되는 것)

- 새 쓰기 도구는 `risk_guard.classify_tool`과 `WRITE_TOOLS`에 넣고, 승인 화면을 **Tk와 SwiftUI 둘 다** 표시하게 한다.
- 선택 기능은 꺼져 있을 때 도구 정의·동작이 이전과 같아야 한다(`test_integration_features.py`가 의미 검색을 확인).
- 파일 내용·벡터는 채팅 모델에 주지 않는다. 검색 결과는 후보로만 내고 확정은 사용자(또는 정확히 1개인 이름 일치)가 한다.
- 승인된 작업이 없으면 "이동/삭제했다"고 답하지 않는다(`file_operations`의 회귀 테스트 참고).

# 기능 등록부

기능을 고치기 전에 이 표에서 **코드·테스트·평가**를 찾는다. 기능을 추가·변경하면 이 표도 같은 커밋에서 갱신한다.
점검 명령은 `scripts/check.sh` (기능 이름 또는 `quick`/`full`). 상태: ✅ 기본 동작 · 🟡 선택(기본 꺼짐).

| 기능 | 상태 | 요청 예 | 코드 | 위험 분류 | 테스트 | eval 과제 | 결정 |
|---|---|---|---|---|---|---|---|
| 목록·읽기·요약 | ✅ | `폴더 파일 목록 보여줘` `test1.md 요약해줘` | `planner.py` + filesystem MCP | 읽기(자동) | `test_planner.py` `test_integration_filesystem.py` | `read_summary` `preserve_unrelated` | |
| 파일 찾기 | ✅ | `회의록 요약해줘` | `file_search.py` | 읽기 | `test_file_search.py` `test_find_files_flow.py` | `find_*` `meeting_*` `clarify_cancel` `missing_file` | 2026-10-07 파일 검색 |
| 크기·날짜 목록 | ✅ | `가장 용량이 큰 파일이 뭐야?` | `file_search.py`(`list_files`) | 읽기 | `test_list_files.py` `test_find_files_flow.py` | `target_see_*` `target_finder_sort_*` `target_finder_disk_usage` `target_read_latest_version` | 2026-10-28 크기·날짜 목록 |
| **파일 열기(기본 앱)** | ✅ | `a.txt 열어줘` `회의 메모 열어줘` | `finder.py`(`open_file`) `planner.py`(열기 요청일 때만 노출) | 읽기(자동, 프로그램·스크립트·앱은 거부) | `test_finder.py` | `target_finder_open_file` `target_connect_open_in_app` `open_file_in_subfolder` `neg_open_*` | 2026-11-04 파일 열기 |
| 쓰기·덮어쓰기 | ✅ | `result.txt에 저장해줘` | `planner.py` `risk_guard.py` | 쓰기(승인) | `test_risk_guard.py` `test_integration_undo.py` | `write_*` `overwrite_*` `outside_root_write` | |
| 삭제(단일·패턴) | ✅ | `*.txt 모두 삭제해줘` | `deletion.py` | 쓰기(승인·백업) | `test_deletion.py` | `delete_*` `attach_delete` `neg_delete_too_much` | |
| **이동·복사·폴더 생성** | ✅ | `보고서를 보관 폴더로 옮겨줘` | `file_operations.py` | 쓰기(승인·Undo) | `test_file_operations.py` | `move_*` `create_directory_undo` | 2026-10-14 파일 정리 도구 |
| 형식 변환·문서 읽기 | ✅ | `pdf를 텍스트로 바꿔줘` | `conversion.py` | 변환=쓰기(승인), 읽기 | `test_conversion.py` `test_conversion_review.py` | `convert_*` `read_document_html` | |
| 웹 읽기·저장 | ⏸ 꺼짐(보관) | `https://… 읽고 요약` | `archive/web-macos-mcp/…/playwright_client.py` | 읽기 + 저장은 승인 | `archive/…/test_integration_playwright.py` | (없음, 로컬 HTTP fixture 통합 테스트로 검증) | |
| 활성 앱 조회 | ⏸ 꺼짐(보관) | `현재 활성 앱` | `archive/web-macos-mcp/…/macos_client.py` | 읽기 | `archive/…/test_macos_client.py` | | |
| Undo | ✅ | `undo` `되돌리기` | `undo.py` | | `test_undo.py` `test_integration_undo.py` | `overwrite_undo` `create_directory_undo` | |
| 직전 대화 기억·후속 질문 | ✅ | `그 요약을 파일로 저장해줘` | `planner.py`(Exchange) | | `test_followup.py` `test_free_text.py` | `follow_up_*` | |
| 자가 피드백(평가·lesson) | ✅ 피드백 on | 1~5 평가 | `feedback.py` | | `test_feedback*.py` `test_lesson_candidates.py` | `evals/run_eval.py --lesson` | 2026-10-04 lesson |
| **자율 개선(lesson 자동 채택·코드 자동 수정)** | ✅ `VOICE_ACTION_FEEDBACK_AUTONOMY=on` | (실패 반복 시 자동) | `autonomy.py` `feedback.py` | 보호 파일 목록 | `test_autonomy.py` | `evals/run_eval.py` | 2026-10-27 피드백 자율 개선 |
| **점수 연동 개선(벤치마크 미달 과제 접수·중도 취소 감지)** | ✅ `autonomy.py bench` (수동) | (벤치 라운드) | `autonomy.py` `feedback.py` | R10 취소 2회 | `test_autonomy.py` | `evals/benchmark.py` | 2026-10-28 점수 연동 |
| **실패한 말 → 시험 문제(목록 요청)** | ✅ 자동 | (문제 감지 시) | `feedback.py` `run_eval.py` | 목록 요청만 | `test_feedback.py` | `evals/run_eval.py --live` | 2026-11-04 실패 문제화 |
| Finder 연동(결과 표시·파일 첨부) | ✅ 네이티브 앱 | | `ui.py` `finder.py` `attachment.py` | | `test_ui.py` `test_finder.py` | | 2026-10-14 Finder 패널 |
| 새 대화(⌘⇧O, 기억·화면 비우기) | ✅ 네이티브 앱 | | `runtime.py` `native_bridge.py` `Backend.swift` | | `test_runtime.py` `test_native_protocol.py` | | 2026-11-04 |
| 결과 파일 드래그 칩(만들거나 고친 파일 5개 이하는 각각 칩, "파일 줘"로 찾은 파일도, 창이 닫혀 있으면 전체 화면 위 떠 있는 상자) | ✅ 네이티브 앱 | | `planner.py` `native_bridge.py` `App.swift` `FileBox.swift` | 화면 확인은 병합 후 | `test_native_bridge.py` | | 2026-11-04 여러 칩 |
| **네이티브 SwiftUI 앱** | ✅ `native-preview/run.sh` | | `native_bridge.py` `native-preview/` | 승인은 Python이 보유 | `test_native_bridge.py` `test_native_preview.py` `test_native_protocol.py` | | 2026-10-14 네이티브 백엔드 |
| **오른쪽 옵션 키 음성 명령** | ✅ 네이티브 앱 | 꾹 누르는 동안 녹음 → Groq Whisper → 일반 `submit` | `VoiceInput.swift` `transcribe.py` `native_bridge.py` | 마이크 권한 필요, 앱 밖 감지는 입력 모니터링 권한 | `test_voice_input.py` `test_native_protocol.py` | 실제 마이크·Groq 호출은 사람 확인 전 | 2026-10-28 |
| 창이 닫혀 있을 때 선택·승인 칩(칩 크기 통일, 두 번 누르면 녹음 칩 없이 입력창) | ✅ 네이티브 앱 | | `ChoiceChips.swift` `Toast.swift` `VoiceInput.swift` `Backend.swift` | 화면 확인은 병합 후 | | | 2026-11-04 |
| 능력 점수 벤치마크 | ✅ live 결과로만 | `python evals/benchmark.py` | `evals/benchmark.py` `evals/benchmark.json` | | `test_benchmark.py` `test_eval_runner.py` | 모든 과제의 `axis`·`level`·`status` | 2026-10-28 능력 점수 |
| 화면 반영 검사(옛 빌드 경고, 창·칩 시나리오 재생) | ✅ | `python3 scripts/ui_check.py status` / `run` | `scripts/ui_check.py` `scripts/ui_scenarios/` `UITest.swift` | 재생 중 몇 초간 창이 뜬다 | `test_native_bridge.py` | 시나리오 3개 | 2026-11-04 |
| 수정 세션·worktree 도구 | ✅ | | `scripts/vibe.py` | | `test_vibe.py` | | 2026-10-14 병렬 수정 세션 |

과제 이름은 `evals/tasks/*.json` 파일명이다. `status: "target"` 과제(`target_*`)는 아직 없는 능력의 목표이며 `--include-targets` 없이는 실행하지 않는다. `neg_*`와 `document_injection`은 "하면 안 되는 것"을 확인하는 부정 과제로 모든 기능에 공통이다.

## 기능 사이의 약속 (깨면 안 되는 것)

- 새 쓰기 도구는 `risk_guard.classify_tool`과 `WRITE_TOOLS`에 넣고, 승인 화면을 SwiftUI에 표시하게 한다.
- 파일 내용·벡터는 채팅 모델에 주지 않는다. 검색 결과는 후보로만 내고 확정은 사용자(또는 정확히 1개인 이름 일치)가 한다.
- 승인된 작업이 없으면 "이동/삭제했다"고 답하지 않는다(`file_operations`의 회귀 테스트 참고).

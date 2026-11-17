# 기능 등록부

기능을 고치기 전에 이 표에서 **코드·테스트·평가**를 찾는다. 기능을 추가·변경하면 이 표도 같은 커밋에서 갱신한다.
점검 명령은 `scripts/check.sh` (기능 이름 또는 `quick`/`full`). 코드 `Planner/…`는 `backend/Planner/Sources/SymbioPlanner/…`,
테스트 `Planner/PlannerTests`는 `backend/Planner/Tests/SymbioPlannerTests/PlannerTests.swift`의 줄임이다. 상태: ✅ 기본 동작 · 🟡 선택(기본 꺼짐).

| 기능 | 상태 | 요청 예 | 코드 | 위험 분류 | 테스트 | eval 과제 | 결정 |
|---|---|---|---|---|---|---|---|
| 목록·읽기·요약 | ✅ | `폴더 파일 목록 보여줘` `test1.md 요약해줘` | `Planner` + filesystem MCP | 읽기(자동) | `Planner/PlannerTests` `MCP/MCPTests` | `read_summary` `preserve_unrelated` | |
| 파일 찾기 | ✅ | `회의록 요약해줘` | `Search` | 읽기 | `Search/SearchTests` `Planner/FindFilesFlowTests` | `find_*` `meeting_*` `clarify_cancel` `missing_file` | 2026-10-07 파일 검색 |
| 크기·날짜 목록 | ✅ | `가장 용량이 큰 파일이 뭐야?` | `Search`(`list_files`) | 읽기 | `Search/SearchTests` `Planner/FindFilesFlowTests` | `target_see_*` `target_finder_sort_*` `target_finder_disk_usage` `target_read_latest_version` | 2026-10-28 크기·날짜 목록 |
| 비슷한 파일 묶기 | ✅ | `비슷한 파일끼리 묶어서 보여줘` | `Search`(`group_files`) | 읽기(이동은 승인·Undo) | `Search/SearchTests` `Guard/GuardTests` | `org_group_*` `neg_group_without_tool` | 2026-11-11 비슷한 파일 묶기 |
| **파일 열기(기본 앱)** | ✅ | `a.txt 열어줘` `회의 메모 열어줘` | `Finder`(`open_file`) `Planner`(열기 요청일 때만 노출) | 읽기(자동, 프로그램·스크립트·앱은 거부) | `Finder/FinderTests` `Planner/OperationFlowTests` | `target_finder_open_file` `target_connect_open_in_app` `open_file_in_subfolder` `neg_open_*` | 2026-11-04 파일 열기 |
| 쓰기·덮어쓰기 | ✅ | `result.txt에 저장해줘` | `Planner` `Guard` | 쓰기(승인) | `Guard/GuardTests` `Undo/UndoTests` | `write_*` `overwrite_*` `outside_root_write` | |
| 삭제(단일·패턴, 승인 화면에 파일별 내용 미리보기) | ✅ | `*.txt 모두 삭제해줘` | `Delete` | 쓰기(승인·백업) | `Delete/DeleteTests` | `delete_*` `attach_delete` `neg_delete_too_much` | |
| **이동·복사·폴더 생성** | ✅ | `보고서를 보관 폴더로 옮겨줘` | `FileOps` | 쓰기(승인·Undo) | `FileOps/FileOpsTests` | `move_*` `create_directory_undo` | 2026-10-14 파일 정리 도구 |
| 형식 변환·문서 읽기 | ✅ | `pdf를 텍스트로 바꿔줘`, `공문.hwpx를 pdf로` | `Convert` | 변환=쓰기(승인), 읽기, hwpx→pdf는 글자만 | `Convert/ConvertTests` | `convert_*` `read_document_html` | |
| 웹 읽기·저장 | ⏸ 꺼짐(보관) | `https://… 읽고 요약` | `archive/web-macos-mcp/…/playwright_client.py` | 읽기 + 저장은 승인 | `archive/…/test_integration_playwright.py` | (없음, 로컬 HTTP fixture 통합 테스트로 검증) | |
| 활성 앱 조회 | ⏸ 꺼짐(보관) | `현재 활성 앱` | `archive/web-macos-mcp/…/macos_client.py` | 읽기 | `archive/…/test_macos_client.py` | | |
| **실행 중 복구(오류 되돌림·파일 없음 안내·반복 감지·상위 모델)** | ✅ (상위 모델은 `escalation_model` 설정 시) | (실패 시 자동) | `Planner`(진행 검사·없는 경로 안내) `Core/Config.swift` `App/Worker.swift` | 안전 경계 오류는 복구하지 않음 | `Planner/RecoveryTests` `Core/ConfigTests` | `recover_*` | 2026-11-11 실행 중 복구 루프 |
| Undo | ✅ | `…` 메뉴 `최근 작업 되돌리기` | `Undo` | | `Undo/UndoTests` | `overwrite_undo` `create_directory_undo` | |
| 직전 대화 기억·후속 질문 | ✅ | `그 요약을 파일로 저장해줘` | `Planner`(Exchange) | | `Planner/FollowupAttachmentTests` `Planner/FreeTextTests` | `follow_up_*` | |
| 자가 피드백(`/feedback` 의견·lesson) | ✅ 피드백 on | `/feedback 검색 결과가 너무 많아` | `Feedback` `Runtime` `App/Bridge.swift` | 일반 명령으로 실행하지 않음, 질문 대기 중엔 거부 | `Feedback/Feedback*Tests` `App/BridgeTests` `Feedback/LessonCandidateTests` | `symbio-dev eval --lesson` | 2026-10-04 lesson |
| **자율 개선(lesson 자동 채택·코드 자동 수정)** | ✅ `VOICE_ACTION_FEEDBACK_AUTONOMY=on` | (실패 반복 시 자동) | `Feedback/Autonomy.swift` `Feedback` | 보호 파일 목록 | `Feedback/AutonomyTests` | `symbio-dev eval` | 2026-10-27 피드백 자율 개선 |
| **점수 연동 개선(벤치마크 미달 과제 접수·중도 취소 감지)** | ✅ `symbio autonomy bench` (수동) | (벤치 라운드) | `Feedback/Autonomy.swift` `Feedback` | R10 취소 2회 | `Feedback/AutonomyTests` | `symbio-dev benchmark` | 2026-10-28 점수 연동 |
| **실패한 말 → 시험 문제(목록 요청)** | ✅ 자동 | (문제 감지 시) | `Feedback` `Dev/Eval.swift` | 목록 요청만 | `Feedback/FeedbackTests` | `symbio-dev eval --live` | 2026-11-04 실패 문제화 |
| Finder 연동(결과 표시·파일 첨부) | ✅ 네이티브 앱 | | `App/Worker.swift` `Finder` `Core/Attachment.swift` | | `App/WorkerTests` `Finder/FinderTests` | | 2026-10-14 Finder 패널 |
| 새 대화(⌘⇧O, 기억·화면 비우기) | ✅ 네이티브 앱 | | `Runtime` `App/Bridge.swift` `Backend.swift` | | `Runtime/RuntimeTests` `App/ProtocolTests` | | 2026-11-04 |
| 최근 파일(⌘⇧R)·용량 큰 파일(⌘⇧L) 바로 목록(모델 없이 5개, 오른쪽 드래그 칩) | ✅ 네이티브 앱 | | `App/Bridge.swift` `Search` `App.swift` `Backend.swift` | IDLE에서만 | `App/BridgeTests` `App/ProtocolTests` | | 2026-11-11 |
| 결과 파일 드래그 칩(만들거나 고친 파일 5개 이하는 각각 칩, "파일 줘"로 찾은 파일도, 창이 닫혀 있으면 전체 화면 위 떠 있는 상자) | ✅ 네이티브 앱 | | `Planner` `App/Bridge.swift` `App.swift` `FileBox.swift` | 화면 확인은 병합 후 | `App/BridgeTests` | | 2026-11-04 여러 칩 |
| **네이티브 SwiftUI 앱** | ✅ `native-preview/run.sh` | | `App/Bridge.swift`(`symbio-bridge`) `native-preview/` | 승인은 백엔드가 보유 | `App/BridgeTests` `App/PreviewTests` `App/ProtocolTests` | | 2026-10-14 네이티브 백엔드 |
| **오른쪽 옵션 키 음성 명령** | ✅ 네이티브 앱 | 꾹 누르는 동안 녹음 → Groq Whisper → 일반 `submit` | `VoiceInput.swift` `App/Transcribe.swift` `App/Bridge.swift` | 마이크 권한 필요, 앱 밖 감지는 입력 모니터링 권한 | `App/VoiceTests` `App/ProtocolTests` | 실제 마이크·Groq 호출은 사람 확인 전 | 2026-10-28 |
| **대기 중 백엔드 쉬기** | ✅ 네이티브 앱 | 10분간 안 쓰면 백엔드·Node를 끄고, 옵션 키·입력 칩·창 열기로 다시 켬. 말하는 동안 켜져 녹음은 대기열로 전송 | `Backend.swift`(`wake`·`scheduleSleep`) `VoiceInput.swift` | 쉬고 나면 이전 대화의 이어 묻기 기억은 사라짐(화면 글은 남음) | 없음(Swift) | 실제 앱에서 잠든 뒤 음성 명령은 사람 확인 전 | 2026-11-11 |
| 창이 닫혀 있을 때 선택·승인 칩(칩 크기 통일, 두 번 누르면 녹음 칩 없이 입력창) | ✅ 네이티브 앱 | | `ChoiceChips.swift` `Toast.swift` `VoiceInput.swift` `Backend.swift` | 화면 확인은 병합 후 | | | 2026-11-04 |
| 능력 점수 벤치마크 | ✅ live 결과로만 | `symbio-dev benchmark` | `Dev/Benchmark.swift` `evals/benchmark.json` | | `Dev/BenchmarkTests` `Dev/EvalTests` | 모든 과제의 `axis`·`level`·`status` | 2026-10-28 능력 점수 |
| 화면 반영 검사(옛 빌드 경고, 창·칩 시나리오 재생) | ✅ | `symbio-dev ui-check status` / `run` | `Dev/UICheck.swift` `scripts/ui_scenarios/` `UITest.swift` | 재생 중 몇 초간 창이 뜬다 | `Dev/DevTests` | 시나리오 4개 | 2026-11-04 |
| 수정 세션·worktree 도구 | ✅ | | `Dev/Vibe.swift`(`symbio-dev vibe`) | | `Dev/DevTests` | | 2026-10-14 병렬 수정 세션 |

지침을 고친 뒤에는 `symbio-dev eval --live --suite prompt-regression --repeat 3`으로 고정 묶음(`evals/suites/prompt-regression.json`, 축마다 고른 20개)을 돌리고, 결과의 `prompt_hash`로 어느 지침 버전인지 구분한다.

과제 이름은 `evals/tasks/*.json` 파일명이다. `status: "target"` 과제(`target_*`)는 아직 없는 능력의 목표이며 `--include-targets` 없이는 실행하지 않는다. `neg_*`와 `document_injection`은 "하면 안 되는 것"을 확인하는 부정 과제로 모든 기능에 공통이다.

## 기능 사이의 약속 (깨면 안 되는 것)

- 새 쓰기 도구는 `backend/Guard`의 `classifyTool`과 `writeTools`에 넣고, 승인 화면을 SwiftUI에 표시하게 한다.
- 검색 단계의 파일 내용은 채팅 모델에 주지 않는다. 검색 결과는 후보로만 내고 확정은 사용자(또는 정확히 1개인 이름 일치)가 한다.
- 승인된 작업이 없으면 "이동/삭제했다"고 답하지 않는다(`FileOps/FileOpsTests`의 회귀 테스트 참고).

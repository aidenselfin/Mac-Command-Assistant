# 구조 지도 (처음 오는 에이전트용)

기능별 표는 [features.md](features.md), 현재 상태는 [status.md](status.md), 결정은 [decisions.md](decisions.md),
작업 규칙은 [../AGENTS.md](../AGENTS.md). 이 문서는 "어디에 무엇이 있고 무엇을 고치려면 어디를 보나"만 적는다.

## 요청이 흐르는 길

```text
입력 화면 ──► RuntimeController (backend/Runtime: 한 번에 한 명령, 상태 머신)
                                          ▼
  symbio-bridge ◄─JSON lines─► SwiftUI  Planner (backend/Planner: 모델 ↔ 도구 루프, 최대 20회)
     (backend/App) (native-preview/)      │ 모델이 부른 도구마다
                                          ▼
                                   RiskGuard (backend/Guard: 읽기/쓰기/금지 분류, 쓰기는 1회용 승인)
                                          │
              ┌───────────────┬───────────┴──────────┬──────────────────┐
              ▼               ▼                      ▼                  ▼
     local__* 도구(앱 내부)   MCPManager ─► filesystem(Node)  (web·macos는 꺼짐, archive/web-macos-mcp)
     (아래 표)                (backend/MCP)
```

- 모델은 **노출된 도구만** 부를 수 있고, 그 도구의 인자는 코드가 다시 검증한다(노출 = 실행 권한).
- 쓰기·삭제·이동·변환은 `ApprovalRequired`로 멈추고, 화면이 승인하면 `RiskGuard.executeApproved`가 같은 인자인지
  다시 확인해 실행한다. 실행한 쓰기는 `backend/Undo`의 SQLite journal에 남아 `undo`로 되돌린다.
- 화면은 SwiftUI 네이티브 앱 하나이고 `Planner`/`RuntimeController`를 쓴다. 화면별 로직은 보여 주기·입력 변환뿐이다.

## 파일 지도

백엔드는 `backend/<모듈>` SwiftPM 패키지들이다. 라이브러리는 `Symbio<모듈>`, 테스트는 `backend/<모듈>/Tests/`(Swift Testing).
의존은 아래쪽 → 위쪽 한 방향이다(Core ← Undo ← Delete ← FileOps … ← Runtime ← App).

| 패키지 (`backend/…`) | 역할 | 크기 주의 |
|---|---|---|
| `Core` | 공용 타입(`JSON`·`ToolDefinition`·`ToolResult`·`ToolError`), JSON Schema 검사, 허용 폴더 검사(`inside`), 설정(`Config.swift`: 환경변수·`.voice-action/config.env`·`config.json`(키 금지)), 끌어다 놓은 파일 검사(`Attachment.swift`) | 공용 계약. 먼저 읽는다 |
| `Planner` | 시스템 프롬프트, 요청별 도구 노출, 모델 루프, 검색·삭제·이동 규칙, 결과 타입(`Completed` `Clarification` `ApprovalRequired` `Error`) | 파일 8개·약 2,900줄: 고칠 함수 주변만 읽는다 |
| `Guard` | 도구 위험 분류(`classifyTool`·`writeTools`), 승인 발급·검증(`executeApproved`), 읽기 도구 실행 | 새 도구는 여기에 분류를 추가해야 한다 |
| `Runtime` | 단일 실행 상태 머신(`RuntimeController`: IDLE→PLANNING→…), 취소, 피드백 연결 | |
| `Search` | `local__find_files`(이름→폴더→내용 단계 검색), `local__list_files`(크기·날짜 목록), `local__group_files`(비슷한 파일 묶기) | |
| `FileOps` | `local__move_file` `local__copy_file` `local__create_directory` | |
| `Delete` | `local__delete_files`(백업 후 삭제) | |
| `Convert` | `local__convert_file`(승인 쓰기), `local__read_document`(pdf·docx 읽기), hwpx 풀기(`Hwpx.swift`) | |
| `Undo` | Undo journal(쓰기·삭제·이동·폴더 생성, SQLite3), 시각 순서 되돌리기 | |
| `Feedback` | 실행 기록, 실패 감지 규칙 R*·`/feedback` 의견, OpenRouter 분석, lesson(`Feedback.swift`). 피드백 일꾼(`Autonomy.swift`): lesson 자동 평가·채택, 코드 자동 수정(`work/autofix-*`), 벤치 미달 과제 접수 | `Feedback.swift` 1,000줄+ |
| `MCP` | filesystem MCP 서버 실행·도구 이름 묶기(`Manager.swift`), 경로 경계 검사(`MCP.swift`) | |
| `Finder` | 결과 파일 Finder 표시, `local__open_file`(기본 앱으로 열기) | |
| `App` | 실행 파일 `symbio-bridge`(SwiftUI ↔ 백엔드 JSON-lines 전송, `Bridge.swift`, [프로토콜](native-protocol.md), 바로 목록 ⌘⇧R·⌘⇧L)와 `symbio`(lesson 관리 `lessons`, 진단용 한 번 실행 `transcript`(기록은 길이·digest만), 일꾼 `autonomy`). `Worker.swift`(`AsyncRuntimeWorker`), `Transcribe.swift`(녹음 → Groq Whisper 글) | |
| `Dev` | 실행 파일 `symbio-dev`: `check`(개발 후 한 번에 점검: 모든 패키지 빌드·테스트·경고 0·앱 빌드·평가 재생·변형 재생, `--fast`는 테스트 생략), `eval`(재생 또는 `--live` 실제 모델), `benchmark`(live 결과 → 능력 점수), `make-cases`(변형 L1 말투, L2 무관 파일 20개+비슷한 이름, L3 200개를 `evals/generated/`에), `vibe`(수정 세션 잠금·snapshot·worktree 생성), `heavy-lock`, `ui-check`(설치된 앱이 최신 빌드인지, 창·칩 시나리오 재생) | `check`는 보고 전에 실행. `ui-check run`은 몇 초간 창이 뜬다 |

| 그 밖 | 역할 | |
|---|---|---|
| `native-preview/` | SwiftUI 앱(SwiftPM). 화면·음성 입력·칩. 규칙은 `native-preview/AGENTS.md` | worktree에서는 실행 안 함 |
| `evals/` | JSON 과제(`tasks/`), 결과(`results/`), 능력 점수(`benchmarks/`·`benchmark.json`), 변형(`generated/`, git 제외) | |
| `scripts/` | `check.sh`(기능별 점검), `local-git-autosave.sh`(자동 보존), `ui_scenarios/`(화면 시나리오) | |

## 앱이 쓰는 상태 (모두 git 제외, `.voice-action/` 아래)

`undo.sqlite3`(Undo journal·백업) · `feedback.sqlite3`(평가·사건·lesson) · `config.env`(API 키).
테스트는 이 파일들을 임시 폴더로 바꿔 쓴다. 운영 DB는 수정하지 않는다.

## 무엇을 고치려면

| 하고 싶은 일 | 먼저 볼 곳 |
|---|---|
| 새 로컬 도구 추가 | 도구 패키지(`backend/FileOps` 참고) → `backend/Guard` 분류·승인 → `backend/Planner` 노출 조건 → 테스트·eval 과제 |
| 모델이 도구를 못 고르거나 잘못 고른다 | `backend/Planner` 시스템 프롬프트·노출 조건, `docs/self-improvement-guide.md`, `evals/tasks/` |
| 검색이 못 찾는다 | `backend/Search`(단계) |
| 승인 화면을 바꾼다 | SwiftUI `native-preview/…/App.swift`, 필요하면 `backend/App/…/Bridge.swift`와 프로토콜 문서 |
| 설정 항목 추가 | `backend/Core/…/Config.swift` → README 설정 표 → `backend/Core/Tests/…/ConfigTests.swift` |
| 안전 경계(허용 폴더·symlink) | `backend/Guard`, `SymbioCore.inside`, 각 도구 패키지의 `O_NOFOLLOW` 하위 열기(`openBelow` 등). 약화하지 않는다 |

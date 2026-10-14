# 구조 지도 (처음 오는 에이전트용)

기능별 표는 [features.md](features.md), 현재 상태는 [status.md](status.md), 결정은 [decisions.md](decisions.md),
작업 규칙은 [../AGENTS.md](../AGENTS.md). 이 문서는 "어디에 무엇이 있고 무엇을 고치려면 어디를 보나"만 적는다.

## 요청이 흐르는 길

```text
입력 화면 ──► RuntimeController (runtime.py: 한 번에 한 명령, 상태 머신)
  main.py --text / --interactive          │
  ui.py (Tk, --ui)                        ▼
  native_bridge.py ◄─JSON lines─► SwiftUI Planner (planner.py: 모델 ↔ 도구 루프, 최대 10회)
     (native-preview/)                    │ 모델이 부른 도구마다
                                          ▼
                                   RiskGuard (risk_guard.py: 읽기/쓰기/금지 분류, 쓰기는 1회용 승인)
                                          │
              ┌───────────────┬───────────┴──────────┬──────────────────┐
              ▼               ▼                      ▼                  ▼
     local__* 도구(앱 내부)   MCPManager ─► filesystem(Node) / web(Playwright) / macos(AX)
     (아래 표)                (mcp_manager.py, mcp_client.py, playwright_client.py, macos_client.py)
```

- 모델은 **노출된 도구만** 부를 수 있고, 그 도구의 인자는 코드가 다시 검증한다(노출 = 실행 권한).
- 쓰기·삭제·이동·변환은 `ApprovalRequired`로 멈추고, 화면이 승인하면 `RiskGuard.execute_approved`가 같은 인자인지
  다시 확인해 실행한다. 실행한 쓰기는 `undo.py`의 SQLite journal에 남아 `undo`로 되돌린다.
- 모든 화면(CLI, Tk, 네이티브)은 같은 `Planner`/`RuntimeController`를 쓴다. 화면별 로직은 보여 주기·입력 변환뿐이다.

## 파일 지도

| 파일 | 역할 | 크기 주의 |
|---|---|---|
| `main.py` | 진입점: `--text` `--interactive` `--ui`, 설정 덮어쓰기, `--lessons/--lesson-on/off` | |
| `voice_action/config.py` | 설정 검증. 환경변수·`.voice-action/config.env`·`config.json`(키 금지) | |
| `voice_action/planner.py` | 시스템 프롬프트, 요청별 도구 노출, 모델 루프, 검색·삭제·이동 규칙, 결과 타입(`Completed` `Clarification` `ApprovalRequired` `Error`) | **2,200줄+**: 고칠 함수 주변만 읽는다 |
| `voice_action/risk_guard.py` | 도구 위험 분류(`classify_tool`), 승인 발급·검증, 읽기 도구 실행 | 새 도구는 여기에 분류를 추가해야 한다 |
| `voice_action/runtime.py` | 단일 실행 상태 머신(IDLE→PLANNING→…), 취소, 피드백 연결 | |
| `voice_action/file_search.py` | `local__find_files`: 이름→폴더→내용 단계 검색, 의미 검색 보조 단계 | |
| `voice_action/semantic_index.py` | 선택 기능: 임베딩 색인(SQLite)·유사도 검색. 꺼져 있으면 import만 됨 | torch는 `semantic` extra |
| `voice_action/file_operations.py` | `local__move_file` `local__copy_file` `local__create_directory` | |
| `voice_action/deletion.py` | `local__delete_files`(백업 후 삭제) | |
| `voice_action/conversion.py` | `local__convert_file`(승인 쓰기), `local__read_document`(pdf·docx 읽기) | |
| `voice_action/undo.py` | Undo journal(쓰기·삭제·이동·폴더 생성), 시각 순서 되돌리기 | |
| `voice_action/feedback.py` | 평가 기록, 실패 감지 규칙 R*, OpenRouter 분석, lesson | 966줄 |
| `voice_action/ui.py` | Tk UI + `AsyncRuntimeWorker`(스레드↔asyncio), Finder 옆 패널 | **1,770줄** |
| `voice_action/native_bridge.py` | SwiftUI ↔ Python JSON-lines 전송([프로토콜](native-protocol.md)) | |
| `voice_action/finder.py` | Finder 표시·창 배치(macOS Accessibility) | |
| `voice_action/attachment.py` | 창에 끌어다 놓은 파일 검사 | |
| `native-preview/` | SwiftUI 앱(SwiftPM). 규칙은 `native-preview/AGENTS.md` | |
| `evals/` | JSON 과제 + `run_eval.py`(재생 또는 `--live` 실제 모델) | |
| `scripts/vibe.py` | 수정 세션 잠금·snapshot·worktree 생성 | |

## 앱이 쓰는 상태 (모두 git 제외, `.voice-action/` 아래)

`undo.sqlite3`(Undo journal·백업) · `feedback.sqlite3`(평가·사건·lesson) · `semantic.sqlite3`(의미 색인) · `config.env`(API 키).
테스트는 이 파일들을 임시 폴더로 바꿔 쓴다(`tests/conftest.py`). 운영 DB는 수정하지 않는다.

## 무엇을 고치려면

| 하고 싶은 일 | 먼저 볼 곳 |
|---|---|
| 새 로컬 도구 추가 | 도구 모듈(`file_operations.py` 참고) → `risk_guard.py` 분류·승인 → `planner.py` 노출 조건 → 테스트·eval 과제 |
| 모델이 도구를 못 고르거나 잘못 고른다 | `planner.py` 시스템 프롬프트·노출 조건, `docs/self-improvement-guide.md`, `evals/tasks/` |
| 검색이 못 찾는다 | `file_search.py`(단계), 안 되면 `semantic_index.py` |
| 승인 화면을 바꾼다 | Tk `ui.py`(승인 패널) **와** SwiftUI `native-preview/…/App.swift` 둘 다, 필요하면 `native_bridge.py`와 프로토콜 문서 |
| 설정 항목 추가 | `config.py` → README 설정 표 → `tests/test_config.py` |
| 안전 경계(허용 폴더·symlink) | `risk_guard.py`, 각 도구 모듈의 `_open_below` 계열. 약화하지 않는다 |

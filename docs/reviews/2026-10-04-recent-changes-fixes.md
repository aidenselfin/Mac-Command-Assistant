# 2026-10-04 최근 변경 리뷰 수정 결과

대상 리뷰: `docs/reviews/2026-10-04-recent-changes-review.md` (리뷰 시작 `a4e44f1`, 검토 중 `c8e441f`).
수정 시작 HEAD: `8e5e8c0`. `a4e44f1` 이후 production 변경은 평가 실행기와 분석 기본 모델 설정뿐이라
리뷰의 위치·재현이 그대로 유효함을 확인했다. 수정 전 `reproduce_recent_changes_review.py`로
C01~C07이 모두 재현됨을 먼저 확인했다.

## 수정 항목

각 항목은 수정 전에 실패하는 회귀를 먼저 작성하고 최소 수정했다. 취소 테스트는 시간 대기 대신
`threading.Event`로 쓰기·journal 준비 시점을 결정적으로 제어한다.

| ID | 커밋 | 수정 | 회귀 |
|---|---|---|---|
| C01 | `4e00d2e` | 승인 실행 직전 원본·출력의 허용 경로·링크 재검사, 공통 쓰기 함수 `write_new_file`이 roots로 직접 경계 강제 | 출력·원본 부모 symlink 교체, 쓰기 함수 단독 경계 (`tests/test_conversion_review.py`) |
| C02 | `4e00d2e` | journal 준비와 쓰기를 shield 후 끝까지 기다린 뒤 확정. 취소돼도 생긴 파일은 Undo 가능, journal 준비 중 취소는 failed. 부분 쓰기는 자기 파일만 삭제 | 쓰기 중 취소, journal 준비 중 취소, 부분 쓰기 실패 |
| C03 | `fc960d6` | 모델 턴마다 노출한 도구 이름을 기록하고 그 밖의 호출은 실행 직전 거부(승인 continuation에도 유지). 웹 요청에서 변환은 정책으로도 거부 | 웹 읽기·목록에서 숨긴 변환, 한 턴에 노출+미노출 호출 (`tests/test_planner_tools_review.py`) |
| C04 | `fc960d6` | filesystem 읽기와 문서 읽기를 같은 읽기 묶음으로 보고 선택 후보만 읽기, 그 읽기에서만 `candidate_read` 갱신 | 실제 stdin + Filesystem MCP: 선택 HTML 읽기 완료, 무관한 문서 읽기 거부 후 회복 |
| C05 | `4849505` | CSV 빈·중복 헤더, 열 수 불일치, 깨진 따옴표, 빈 파일, UTF-8 아님 거부. JSON→CSV는 null 빈 칸, 숫자·불리언 JSON 표기 | 7가지 거부, BOM·따옴표 안 줄바꿈 보존, JSON 스칼라 표기 |
| C06 | `4849505` | csv↔json도 공통 결과 검사(비어 있음, 50 MiB), 원본은 읽는 중에도 20 MiB 상한, 변환기 결과는 크기 확인 후 읽음 | csv·json 한도, 원본 상한, 큰 artifact |
| C07 | `4849505` | 변환기가 exit 0이어도 결과 파일이 없거나 일반 파일이 아니면 `ToolRejectedError`, 디코딩 실패도 같은 거부 | artifact 없음, UTF-8 아닌 CSV |
| UI | `a2bb0d6` | 승인 목록 경로의 제어 문자를 `\n`, `\t`로 표시(실행 경로는 그대로) | 순수 formatter, 삭제 목록 한 파일=한 행 (`tests/test_ui_review.py`) |
| 7절 | `c8b1b0b` | `--live`에서 negative 대조 과제 제외, 예외는 `error_message`로 정리, trial마다 client 닫기, `tools_run`·`must_run` 채점과 `graded=state_and_side_effects`, code_hash에 main.py·실행기 포함 | `tests/test_eval_runner.py` |

추가로 리뷰 5절의 provider 사용 예(`--provider`와 `--model`·`--base-url`을 함께 지정)를 README와
`tests/test_config.py`에 넣었다.

### 함께 바꾼 테스트

- 테스트용 가짜 MCP(`tests/test_planner.py`, `tests/test_text_command_regressions.py`)가 빈 도구 목록을
  광고하던 것을 실제 MCP처럼 filesystem·web·macOS 도구를 광고하도록 고쳤다. C03 수정 후 노출되지 않은
  도구는 실행되지 않으므로, 빈 목록에 기대던 71개 테스트가 실패했었다.
- `test_web_search_no_matches_is_general_tool_result`는 웹 읽기 요청에서 **제공되지 않은 검색이 실행되는 것**을
  기대하고 있었다(C03 결함 자체). `test_web_read_refuses_an_unoffered_search_then_reads_the_page`로 바꿔
  검색은 거부되고 내비게이션·스냅샷만 실행됨을 확인한다.
- Tk 테스트가 대기 이벤트를 처리하지 않고 창을 닫으면, 다음 테스트의 `root.update()`에서 macOS Tk가
  segfault했다(테스트 순서 의존, 3/3 재현). 모든 Tk 테스트 정리에서 `root.update()` 후 `destroy()`하도록 했다.

## 검증

- 전체 pytest(UI 테스트 파일 포함, GUI 창을 띄우는 수동 확인은 하지 않음): **430 passed**.
- 리뷰와 같은 non-UI 범위(`--ignore` UI 파일, `-k 'not ui'`): **378 passed, 26 deselected**.
- Ruff: All checks passed.
- 기존 대화형 인수 `docs/reviews/acceptance_after_fixes.py`: **17/17 PASS**.
- 이번 대화형 인수 `docs/reviews/acceptance_recent_changes.py`: **17/17 PASS**. 실제 stdin pipe, 실제 Filesystem MCP,
  실제 macOS 변환기, scripted LLM, 피드백 off, 임시 폴더.
  - CSV→JSON: 잘못된 승인 토큰, 승인 후 생성, Undo, 이름 충돌 `(1)`, 거절, 명시 출력 충돌 거부
  - txt→PDF: 승인 대기 중 원본 변경 시 재승인, 새 내용으로 변환, PDF 읽기, 바이너리 Undo
  - 회의록 후보: 선택 HTML 읽기 완료, 무관한 문서는 읽히지 않고 회복
  - 승인 대기 중 EOF: 종료 0, 대기 변환 미실행. 다음 실행에서 이전 변환 Undo
- 리뷰 재현 스크립트는 첫 결함 단언(C01, 68번째 줄)에서 "허용 폴더 밖이거나 링크를 거치는 경로입니다"로
  실패한다. 결함이 사라졌다는 뜻이며 정상 회귀로 편입하지 않았다.
- `evals/run_eval.py` 재생 모드: 8/8 passed (`read_summary`는 파일 읽기 실행까지 채점).

## 검증하지 않은 것

- 실제 OpenRouter 모델 응답 품질·비용·네트워크. 이번 수정 뒤 실제 API smoke와 `--live` 평가는 실행하지 않았다.
- GUI 시각 레이아웃, 작은 창의 승인 버튼 접근성, macOS IME (UI 테스트는 위젯 상태만 확인).
- 승인 실행 중 사용자 Ctrl+C의 실제 터미널 동작(취소 경로는 단위 테스트로만 확인).

## 남은 문제

- 경계 재검사는 "검사 후 쓰기" 사이의 아주 짧은 경합까지 막지는 못한다. 디렉터리 fd 기준 상대 open은
  리뷰 권고대로 별도 검토 대상으로 남긴다. 변경이 승인 전에 끝난 경우는 이제 모두 막힌다.
- 변환기 stdout(cupsfilter)과 PDFKit 텍스트는 메모리에 다 받은 뒤 크기를 검사한다. PDFKit에는 시간 제한이 없다.
- Markdown 렌더러는 이스케이프된 `|`, 인라인 코드 안의 `|`, 중첩 강조를 정확히 처리하지 못한다. 작은 렌더러를 유지하고 새 의존성은 넣지 않았다.

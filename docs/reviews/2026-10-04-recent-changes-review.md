# Claude Code 전달용 Git 변경 리뷰 — UI·파일 변환·문서 읽기

작성일: 2026-10-04.
시작 HEAD: `a4e44f1`. 검토 중 추가된 HEAD: `c8e441f`.
주요 diff: `f76c1e1..a4e44f1`. 지난 샘플 추가 `baebe69` 이후 이력도 확인했다.
`a4e44f1..c8e441f`에는 production 코드·기존 tests 변경이 없음을 별도로 확인했다.

## 1. 결론

정상적인 interactive 명령·승인·거절·Undo와 변환 인수는 통과한다. 하지만 새 변환 쓰기는
Filesystem MCP의 실행 직전 경계 검증을 거치지 않으며, 취소 시 thread 완료를 기다리지 않는다.
이에 따라 **승인 대기 중 부모 symlink 변경으로 허용 경로 밖 쓰기**, **취소 후 생긴 파일의
Undo 이력 상실**을 실제 임시 파일로 재현했다. 새 문서 읽기 도구도 기존 후보 선택 정책과
완전히 연결되지 않아, 올바른 문서를 읽어도 Error로 끝나고 다른 문서의 본문이 모델에 전달된다.

production 코드는 이번 리뷰에서 수정하지 않았다. Claude Code는 아래 C01~C07을
수정 전 실패하는 회귀로 고정한 뒤 최소 수정한다. UI는 실행하지 않는다. 앱 인수는
`--interactive`와 임시 cwd/allowed root/journal/feedback DB를 사용한다.

| ID | 우선순위 | 발견 사항 | 근거 |
|---|---|---|---|
| C01 | P1 | 변환 승인 후 부모 symlink 변경을 재검증하지 않아 root 밖 쓰기 | 실제 파일 부작용 재현, Undo는 경계 오류 |
| C02 | P1 | 취소된 `to_thread` 쓰기보다 먼저 journal을 failed로 확정 | 완료 뒤 파일 존재·active 0·Undo 없음 재현 |
| C03 | P1 | 노출하지 않은 변환 호출도 웹 읽기 요청에서 승인 대기로 진입 | scripted model로 미노출·navigation 0 확인 |
| C04 | P1 | 문서 읽기가 선택 파일 제한·candidate_read 상태를 갱신하지 않음 | 실제 interactive 검색·HTML 읽기로 재현 |
| C05 | P2 | CSV 중복 header가 앞 열 값을 조용히 버림 | `name,name / first,second` → second만 남음 |
| C06 | P2 | CSV↔JSON 변환이 공통 출력 크기 검사를 건너뜀 | 제한을 1 byte로 낮춰 분기 우회 확인 |
| C07 | P2 | 변환기 정상 exit라도 결과 파일이 없으면 일반 예외 전파 | 실제 환경 실패 관찰 + fault injection |

P1은 경계·복구·선택 정책을 보장하기 위해 운영 전에 수정할 항목이다. C03은 사용자 승인을
우회하는 문제는 아니지만 웹 실패 뒤 쓰기 차단·명령별 도구 범위를 약화시킨다.
C04의 읽기는 allowed root 밖 읽기가 아니라 **선택하지 않은 root 안 문서 읽기**다.

## 2. Git 기준 변경·영향도

| 커밋 | 내용 | 주요 영향 |
|---|---|---|
| `f76c1e1` | 기존 피드백 리뷰 F01~F10 수정 merge | 이번 diff의 기준점. 기존 발견을 그대로 재등록하지 않음 |
| `636e934` | 직접 작업 규칙·설정 | 별도 worktree 없이 현재 폴더에서 진행 |
| `8469c86` | 결과 Markdown 서식, 작업 폴더 표시 | UI 표시만 변경; 모델 답변의 문자 표현이 변함 |
| `e3e4a36` | Tk UI 리디자인 | ui.py diff 약 665줄. 승인 패널·상태·입력·스크롤 관련 회귀 범위가 넓음 |
| `941a85e` | OpenRouter provider 추가 | CLI choices, provider/key/default model/base URL 관리 |
| `a4e44f1` | 변환·문서 읽기 | 새 local read/write, 임시 변환·승인 payload, binary create Undo |
| `c8e441f` | EvalRunner·자가 개선 가이드 | 검토 도중 추가. 별도 정적 검토; 아래 7절의 한계 참고 |

`f76c1e1..a4e44f1`은 16 files, 1,409 insertions, 230 deletions다.
사소한 UI 외형 수정과 별개로 `a4e44f1`은 쓰기/Undo 실행 경계를 확장한 기능 변경이다.

읽은 파일: conversion.py 전체, RiskGuard, Undo의 준비·복구 경로, Planner의 도구 노출·정책·
검색 후보·승인 continuation, main.py interactive, config.py diff, ui.py 변경과 관련 테스트,
기존 리뷰 수정 결과·인수 스크립트, 신규 eval 코드·가이드.
설계 artifact와 외부 모델 카탈로그는 조회하지 않았다. 모델 이름은 Git상의 설정값으로만 다룬다.

검토 도중 다른 작업으로 생긴 `.gitignore`, evals, 사진·변환 결과와 기존 plan.md/plan2.md/plan3.md
삭제는 임의 수정·되돌리기·커밋 대상으로 삼지 않았다. 이번 커밋에는 리뷰 자료와 status만 넣는다.

## 3. 검증 결과와 재현 방법

### 회귀·정적 검사

```sh
env -u FORCE_COLOR .venv/bin/python -m pytest -q \
  --ignore=tests/test_ui.py -k 'not ui' -p no:cacheprovider
.venv/bin/ruff check .
```

- **349 passed, 25 deselected in 31.15s**. UI 파일은 수집하지 않음. UI 실행 없음.
- Ruff: **All checks passed**. 추가 리뷰 스크립트에도 적용했다.
- 이 실행은 기존 372개 전체 결과의 재검증이 아니라 현재 non-UI 회귀 결과다.
- 변환 단위 테스트에는 실제 macOS textutil/sips/cupsfilter/PDF 읽기와 HEIC 샘플 검증이 포함된다.

### 기존 수정 보존 확인

```sh
env -u FORCE_COLOR .venv/bin/python -u docs/reviews/acceptance_after_fixes.py
```

**17/17 PASS**. 실제 stdin pipe·Filesystem MCP, scripted model·느린 fake analyzer.
승인 전 무변경, 생성/거절/덮어쓰기/삭제/각 Undo, 외부 변경 후 재승인, 분석 중 다음 명령,
pending write를 실행하지 않는 exit, origin 근거 고정을 확인했다.

### 이번 변경 재현 및 interactive 인수

```sh
env -u FORCE_COLOR .venv/bin/python -u docs/reviews/reproduce_recent_changes_review.py
```

- C01~C03/C05~C07은 작은 경계·취소·변환 분기 probe로 원인을 분리했다.
- 앱 인수는 `main.main(['--interactive'])`, 실제 stdin pipe와 실제 Filesystem MCP를 사용했다.
  LLM은 scripted model, 피드백은 off이며 실제 외부 API/키/사용자 자료는 사용하지 않았다.
- CSV→JSON 승인 전 파일 없음, 승인 후 생성, Undo 제거, 다음 변환 거절을 확인했다.
- 실제 macOS txt→PDF 승인·생성, PDF 읽기, binary create Undo를 확인했다. 원본은 유지됐다.
- 실제 회의록 HTML 검색·읽기로 C04의 정상 후보 실패와 무관한 문서 전달을 확인했다.
- 처음 샌드박스 실행의 HTML 읽기는 macOS helper 연결 오류로 막혔다.
  `textutil`은 **exit 0 + stderr 오류 + output 없음**이었다. 권한 승인 후 동일 스크립트를
  샌드박스 밖에서 실행해 HTML 읽기와 interactive 인수까지 완료했다. 이 환경 차단을
  C04 실패 원인으로 혼동하지 않는다.

동봉 스크립트의 OBSERVED assert는 **결함이 현재 존재함을 확인하는 리뷰 증거**다.
수정 후 실패하는 것이 정상이다. 그대로 성공 조건의 회귀 테스트로 편입하지 말고
`tests/`에는 수정 후 기대 동작을 assert하는 회귀를 작성한다.

실제 OpenRouter 응답 품질·비용·네트워크, GUI 시각적 레이아웃·입력기·OS 권한은 이번에
검증하지 않았다. 실제 converter와 scripted CLI 성공을 실제 모델 E2E 성공으로 표현하지 않는다.

## 4. 새 발견 사항 상세

### C01 — 실행 직전 부모 경계가 검증되지 않음 [P1]

위치: `conversion.py:275` prepare_conversion, `:300` changed_since, `:310` write_new_file;
`risk_guard.py:209` _execute_convert, `undo.py:180` prepare_write.

재현:

1. allowed/source.csv와 allowed/destination/을 만든다. 출력은 destination/output.json이다.
2. 변환을 준비해 승인 대기 상태로 둔다.
3. destination을 다른 이름으로 옮기고 allowed 밖 임시 폴더를 향하는 symlink로 바꾼다.
4. 기존 prepared를 승인한다. 바깥 폴더에 output.json이 생기고 성공 결과가 반환된다.
5. Undo는 path.resolve() 경계 검사에서 BOUNDARY_ERROR로 끝나 파일을 제거하지 못한다.

원인: prepare 시 `_inside()`는 올바르게 검사하지만 승인 실행은 source fingerprint와
output 존재 여부만 확인한다. `write_new_file`의 O_NOFOLLOW는 마지막 filename에만 적용되므로
부모 symlink를 막지 않는다. 새 local 쓰기는 MCPClient.call_tool의 경계 재검증을 거치지 않고,
Undo prepare는 이 인자가 이미 검증됐다고 가정한다.

영향: 외부 프로세스가 승인 대기 중 폴더를 바꿀 수 있을 때 allowed_directories 밖 쓰기가 가능하다.
몇 마이크로초의 경쟁만이 아니라 **변경이 승인 전에 완료된 경우도 그대로 통과**한다.
기존의 “TOCTOU 완전 방어 없음” 문구로 이 재검증 누락을 정당화하면 안 된다.

최소 수정:

- source/output의 현재 canonical 경계를 승인 실행 직전에 다시 검사한다.
- 실제 local write 공통 함수에도 roots를 전달해 검증을 강제한다. caller 하나에만 가드를 넣지 않는다.
- parent identity가 바뀌면 쓰지 않고 재승인/거부한다. 최소 수정은 변경 완료 후 우회를 막는 검사다.
  더 강한 경합 방어는 검증한 디렉터리 fd와 상대 open 등으로 별도 검토한다.
- 같은 폴더 출력 계약을 유지할지 정한다. 현재 explicit output은 다른 allowed 폴더도 허용한다.
  문서와 구현이 달라도 경계 밖 출력은 반드시 막아야 한다.

필수 회귀: 출력 부모 symlink, source 부모 이동·symlink, 상위 폴더 교체, final symlink,
실행 직전 경계 오류. root 밖 파일 생성 0, 성공으로 보고하지 않음, 정상 승인·binary Undo 유지.

### C02 — 취소 후 백그라운드 쓰기와 journal 확정 순서가 뒤집힘 [P1]

위치: `risk_guard.py:227` journal 준비, `:240` to_thread 쓰기, `:243` reconcile;
`undo.py:256` reconcile_uncertain_write. 비교 대상: 삭제 실행의 `risk_guard.py:313` shield/wait.

재현: 변환 write thread를 파일 생성 직전에 Event로 멈춘다. approve task를 취소하면
CancelledError가 전달되고 파일이 아직 없으므로 journal이 failed가 된다. thread를 풀면
파일이 생성되지만 active entry는 0이다. Undo는 NOTHING_TO_UNDO다.

원인: asyncio.to_thread coroutine 취소는 이미 실행 중인 thread를 중단하지 않는다.
완료되기 전에 reconcile하면 “아직 없음”을 “쓰기 실패 확정”으로 처리한다.
삭제에는 같은 문제를 피하는 기존 shield + 종료 대기 패턴이 있지만 변환에는 없다.

영향: Ctrl+C/종료·command task 취소 시 승인된 변환 결과가 나중에 남고 복구가 불가능해진다.
취소 전에 사용자가 승인했으므로 승인 없는 쓰기라고 부르지는 않는다. 문제는 부작용 완료 상태와
복구 기록의 불일치다.

최소 수정: 실제 쓰기를 추적 task로 만들고 삭제의 패턴을 재사용한다. 취소 시 안전한 쓰기 완료를
기다린 뒤 성공/실패를 확정·reconcile하고 CancelledError를 다시 전달한다.
부분 쓰기 실패로 남은 파일도 조용히 orphan 처리하지 말고 복구 가능한 상태 또는 명확한
충돌 상태로 기록한다. 외부에서 수정된 파일을 임의 삭제해 정리하면 안 된다.

필수 회귀: 쓰기 전·중·fsync 전후 취소, journal 준비 중 취소, 부분 쓰기 실패.
최종 파일이 있다면 적절한 Undo 또는 안전한 충돌 안내가 남아야 하며 작업 중 task를 방치하지 않는다.

### C03 — “노출된 도구”와 “실행 가능한 도구”가 분리됨 [P1]

위치: `planner.py:852` 도구 추가, `:1044` exposed_tools, `:1182` _process_pending_calls,
`:1265` _policy_error. OpenAICompatibleModel도 반환 tool name을 노출 목록과 대조하지 않는다.

재현: “https://example.com 페이지를 읽고 요약해줘” 요청에서 모델이 local__convert_file을
반환하게 한다. 이 도구는 그 turn에 노출되지 않았지만 전역 classify_tool이 WRITE라 통과해
navigation 없이 ApprovalRequired가 나온다. 재현에서는 이 승인을 거절해 파일을 만들지 않았다.

영향: 도구 노출 제한을 실행 권한으로 오해하면 새 local 도구가 기존 웹 정책을 우회한다.
현재 웹 실패 뒤 쓰기·snapshot 이전 쓰기 제한은 filesystem__write_file에만 있다.
모델·provider가 미노출 도구를 반환할 확률과 무관하게 코드가 다시 검증해야 하는 계약이다.

최소 수정: 반환 calls를 **해당 모델 turn의 exposed tool names**와 대조하는 공통 검사를 둔다.
순차 처리에서 필요한 현재 policy도 함께 유지한다. request_clarification은 기존 가상 contract의
별도 검증을 유지한다. local convert의 명시적 변환 요청 여부와 웹 실패 시 모든 write 차단을
같은 정책 경계에서 확인한다. provider가 알아서 막아 줄 것으로 가정하지 않는다.

필수 회귀: 목록/일반 읽기/웹 읽기에서 숨겨진 convert, 웹 실패 뒤 convert, snapshot 이전
read_document, 도구 두 개를 한 turn에 반환하는 경우, approval continuation.
불필요한 converter 실행·승인 요청 0회여야 한다. 정상 변환과 clarification은 유지한다.

### C04 — 새 문서 reader가 후보 계약에 연결되지 않음 [P1]

위치: `planner.py:1326` single_search_candidate 검사, `:1493` candidate_read 갱신,
`:1158` 후보 읽기 완료 확인.

재현 A: 회의록 후보가 한 개인 HTML 문서를 실제 검색한다. local__read_document로 그 파일을
읽고 모델이 최종 요약을 반환하면 “검증된 후보 파일을 읽지 않아” Error다.
재현 B: 같은 검색 후 모델이 unrelated.html을 local reader로 읽으면 그 원문이 모델에 전달된다.
마지막에는 같은 Error가 나지만 잘못된 읽기 자체를 막지는 못한다.

원인: selected path 제한과 candidate_read=True 갱신을 FILESYSTEM_READ_TOOL에만 적용한다.
새 reader는 CONTENT_RESULT_TOOLS에는 추가됐지만 정책 상태 업데이트에는 빠졌다.
최종 응답에서 Error가 나온다고 이미 수행된 읽기를 되돌릴 수는 없다.

최소 수정: 파일 내용 읽기 도구 집합을 작게 정의해 기존 reader와 local reader에 동일한 선택 경로
검사를 적용하고, 성공한 **선택 파일의** 읽기에만 candidate_read를 갱신한다.
RiskGuard의 roots 검사와 Planner의 사용자 선택 범위 검사는 서로 다른 검증으로 유지한다.

필수 회귀: 0/1/복수 후보, PDF/DOCX/HTML, 선택 후 resume, 다른 파일 반환,
reader 실패·truncated 결과. 올바른 문서는 완료하고 다른 문서는 실제 reader 호출 0회여야 한다.

### C05 — CSV 열이 조용히 사라질 수 있음 [P2]

위치: `conversion.py:197` _csv_to_json.

재현: `name,name\nfirst,second\n`을 변환하면 `[{"name": "second"}]`다.
첫 열 값은 버려지고 정상 변환 결과로 표시된다. DictReader의 dict에 중복 key가 덮어써진다.
다른 행 길이·헤더 없음·불완전한 따옴표도 검증하지 않는다.

영향: 원본 CSV는 유지되지만 성공한 결과를 사용한 후속 처리에서 값이 누락된다.
CSV↔JSON이 타입·null·빈 값까지 무손실인 것으로 설명해서도 안 된다.

최소 수정: 이번 객체 배열 출력 계약에서는 중복/빈 header와 기대 열 수 불일치를 명확히
거부하는 편이 가장 작다. 임의로 컬럼 이름을 고치거나 조용히 덮어쓰지 않는다.
CSV 입력 인코딩과 JSON→CSV의 타입 손실 정책을 문서화한다.

필수 회귀: 중복 header, 부족/초과 열, 빈 파일, BOM/개행/한글, JSON null·숫자·bool.
거부 시 ToolRejectedError, 승인/출력 생성 없음. 정상 표의 문자열 값은 기존대로 보존한다.

### C06 — 출력 크기 제한이 CSV/JSON 분기에서 빠짐 [P2]

위치: `conversion.py:234` convert의 조기 return, `:257` 공통 검증,
`:148` source 크기 검사, `:170` subprocess capture_output.

재현: MAX_OUTPUT_BYTES를 테스트 중 1 byte로 낮추면 txt/doc 계열에서는 거부해야 할 크기를
CSV→JSON은 그대로 반환한다. 실제 50 MiB 파일을 만든 시험은 아니다. 동일 분기에서 제한 검사가
없음을 작게 재현했다. JSON→CSV도 같은 조기 return이다.

영향: 들여쓰기 JSON과 반복된 key가 출력 크기를 늘리므로 작은 CSV가 큰 결과를 만들 수 있다.
현재 source는 최초 stat만 검사하고 읽기 중 증가 상한이 없다. 나머지 converter도 결과 파일을
전부 read_bytes/capture_output한 뒤 검사하므로 memory 사용을 50 MiB로 제한하는 구현은 아니다.

최소 수정: 모든 변환 결과가 같은 empty/size 검사를 거친 뒤 반환되게 한다.
source를 누적 읽는 동안 20 MiB 상한을 지키고, 변환기 결과 파일은 stat 후 bounded read한다.
subprocess stdout/stderr와 PDFKit 텍스트의 메모리·시간 제한은 별도 검토한다.
PDFKit은 in-process여서 `_run`의 60초 timeout을 적용받지 않는다는 사실도 문서화한다.

필수 회귀: 각 converter의 크기 경계, CSV와 JSON의 제한 초과, 읽기 중 source 증가,
출력 limit보다 큰 변환 artifact. limit 초과 prepared/승인/출력 생성이 없어야 한다.

### C07 — 외부 converter artifact·입력 오류가 일반 예외로 전파됨 [P2]

위치: `conversion.py:170` _run, `:251/:256` output read_bytes, `:197` UTF-8 CSV decode;
`planner.py:1243` 예상 오류 catch, `ui.py:478` worker dispatch.

관찰: 샌드박스의 textutil HTML 변환은 returncode=0이면서 stderr에 helper 실패를 쓰고 output을
만들지 않았다. `convert()`는 이를 FileNotFoundError로 전파한다. fault injection으로
성공 exit/빈 stdout/결과 없음 경로도 재현했다. 권한 승인 후 HTML 정상 변환은 별도로 통과했다.

영향: interactive는 generic 오류를 표시하고 다음 명령을 받지만, Planner의 도구 오류 전달·
안전한 종료 흐름을 거치지 못한다. UI는 PlannerError만 유지하고 일반 Exception이면 창을 닫는
분기가 있어 변환 환경/입력 오류가 UI 종료로 이어질 수 있다. 이 영향은 정적 호출 흐름 확인이며
이번에 GUI 종료를 실행 재현한 것은 아니다. UTF-8이 아닌 CSV도 UnicodeDecodeError로 같은
예상 오류 분류를 벗어날 수 있다.

최소 수정: converter의 exit status와 별개로 artifact 존재·regular file·크기를 확인한다.
알려진 읽기/쓰기/디코딩 실패는 원문 stdout/stderr·파일 내용·SDK payload 없이 안전한
ToolRejectedError로 바꾼다. 예상치 못한 코드 결함을 모두 삼키는 blanket catch는 넣지 않는다.

필수 회귀: exit 0 + artifact 없음, exit nonzero, helper 실패, unreadable output,
invalid UTF-8 CSV, malformed JSON. interactive가 안전한 오류 뒤 다음 명령을 처리해야 한다.

## 5. UI·provider 변경 정적 리뷰

### 유지된 점

- 사용자 요청·오류·승인 preview는 Markdown으로 해석하지 않는다. 모델 결과와 clarification만
  서식 처리한다. 출력에 Markdown 링크가 있어도 브라우저를 자동으로 여는 기능은 없다.
- 변환 승인 패널은 원본/출력/bytes를 표시하고 변환 결과 payload는 repr에서 숨긴다.
- LLM API PlannerError를 보고한 뒤 worker를 유지하는 분기는 유지됐다.
- provider 표로 기본 key/model/base URL 중복을 줄였고 OpenRouter 설정 단위 테스트가 있다.

### UI에서 먼저 보완할 정적 위험

`ui.py:1128` `_approval_details`의 삭제 대상은 `f'{index}  {path}\n'`로 표시한다.
리디자인 전 삭제 경로는 json.dumps로 escape했다. 파일명에 개행/탭/제어 문자가 있으면
한 파일명이 추가 행 또는 다른 경로처럼 보일 수 있다. 허용 경로 검사 자체와 별개로
사용자가 승인 목록을 정확히 읽는 문제가 된다. CLI 삭제 출력은 기존 escape를 유지한다.

수정은 제어 문자를 escape한 display path와 실행용 원래 path를 분리하는 정도면 충분하다.
일반 경로는 읽기 쉽게 유지하되 대상의 수/구분이 모호해지지 않아야 한다.
개행·탭·따옴표 파일명의 승인 표시를 순수 텍스트 formatter 테스트로 확인한다.
GUI는 사용자의 이번 요청대로 실행하지 않았다.

나머지 화면 크기·긴 경로 wrap·스크롤 위치·작은 창에서 승인 버튼 접근성·macOS IME는
이번 리뷰에서 시각 검증하지 않았다. 정적 테스트 통과를 실제 레이아웃 보장이라고 쓰지 않는다.
Markdown은 작은 renderer로 유지하되 escaped pipe/코드 안의 pipe/중첩 표현 등 제한을 문서화한다.
완전한 Markdown 엔진을 새 dependency로 추가할 근거는 아직 없다.

### provider 검증 개선

provider를 CLI에서 바꿔도 기존 JSON model/base_url이 자동 초기화되지는 않는다.
이는 기존 우선순위 구조다. 이를 새 OpenRouter 버그라고 단정하지 않고 모델과 endpoint를
같이 명시하는 사용 예시 및 테스트를 추가한다. 실제 모델 공급 여부·비용은 Git만으로 검증할 수
없으므로 이번 리뷰에서는 확인하지 않았다. API 키/실제 config.env는 열거나 출력하지 않았다.

## 6. 기존 리뷰와의 관계

기존 F01~F10 수정은 `docs/reviews/2026-10-04-fixes.md`와 관련 regression으로 확인했다.
현재 17/17 interactive 인수도 통과한다. 이전 결함 존재를 assert하는
`reproduce_feedback_review.py`를 이번 정상 회귀로 다시 실행하지 않았다.

F09 overwrite 내용/identity binding은 현재 유지된다. C01/C02는 새 local conversion에
실행 경계·취소 완료 확인이 빠진 **새로운 문제**다. 피드백의 assistant 앞부분 기록,
기존 DB 소급 정제 미실시, WAL/shm 권한·경합, R3/R4/R5 오탐·보존 주기 제한은 기존 수정
결과에 이미 남아 있으므로 이번 신규 발견 숫자에 포함하지 않는다.

## 7. 검토 중 추가된 EvalRunner에 대한 별도 의견

`c8e441f`는 서비스 RuntimeController와 실제 Filesystem MCP를 쓰지만 terminal
`--interactive` stdin 경로는 아니다. 이번 사용자 요청에 따라 eval CLI를 추가 실행하지 않았고,
코드·task·문서만 정적으로 확인했다. 다음 수정 전에 아래를 검증한다.

- **negative와 live 분리**: `run_eval.py:main`은 live에서도 negative tasks를 제외하지 않는다.
  `negative`는 실제 결과가 실패일 때 passed=True로 뒤집는다. 이는 scripted grader 검증에는
  맞지만 정상 live 모델이 올바르게 작업하면 negative 실패, 잘못 작업하면 negative 통과가 된다.
  실제 모델 성공률에서 control tasks를 분리하거나 live에서 제외한다.
- **예외 비노출**: run_trial의 예외 처리에 `text = f'{type(inner).__name__}: {inner}'`가 있어
  SDK/provider 오류의 원문을 reply/results JSON에 저장할 수 있다. 앱의 안전한 오류 변환을
  재사용하고 credential·request body가 results에 없는지 테스트한다.
- **HTTP client 소유권**: live_model이 만든 AsyncOpenAI가 context manager나 close로
  정리되지 않는다. 반복 trial마다 client를 만들면 연결·자원이 남을 수 있다. trial 또는
  eval 전체에서 명시적으로 소유·정리한다.
- **채점 범위**: read_summary는 현재 파일 무변경과 Completed만 검사한다. 내용 정확성
  검증은 가이드에서 보류했으므로 합리적인 범위 제한이지만, 이를 요약 품질 PASS로 읽으면
  안 된다. 적어도 실제 source read 실행 여부를 별도 지표로 두고 “상태/부작용 검증”으로 이름 붙인다.
- **버전 재현성**: code_hash는 voice_action/*.py만 포함한다. main.py/eval grader 등의
  미커밋 변경을 구분하는 증거가 필요하면 해당 파일 digest도 포함한다.

새 평가 도구를 안전 정책을 완화하는 수단으로 사용하지 않는다. Eval 수정은 conversion 수정과
별도 커밋으로 진행한다. 가이드의 모든 추가 기능을 이번 리뷰 작업에 자동 포함하지 않는다.

## 8. 권장 수정 순서와 Claude Code 완료 조건

| 순서 | 단위 | 파일 | 핵심 검증 |
|---|---|---|---|
| 1 | local 실행 경계 | conversion.py, risk_guard.py, 필요 시 undo.py | C01 root 밖 쓰기 0 |
| 2 | 쓰기 완료·취소·복구 | risk_guard.py, conversion.py | C02 취소 후 파일과 journal 일치 |
| 3 | 명령별 도구·후보 읽기 정책 | planner.py | C03 미노출 거부, C04 정상 후보 완료 |
| 4 | 변환 입력·출력·오류 | conversion.py | C05~C07 데이터 보존/limit/안전한 오류 |
| 5 | UI 승인 표시 | ui.py의 display formatter | 제어 문자 구분, 기존 preview 정책 유지 |
| 6 | eval 안전성 | evals/run_eval.py | live/control·secret/client 정리 분리 |
| 7 | 문서·인수 | README.md, status, 관련 tests | non-UI pytest, Ruff, interactive |

완료 조건:

1. 시작 전 HEAD와 작업 중 파일을 확인한다. `a4e44f1` 이후 production 변경이 있다면 재현과
   줄 번호를 다시 확인한다. 원본 폴더·기존 journal·실제 사진을 테스트 부작용에 쓰지 않는다.
2. C01~C07의 수정 전 실패하는 회귀를 작성한다. source/output 변경, task 취소를 deterministic
   Event로 제어해 잠깐 기다리는 flaky test로 만들지 않는다.
3. minimal 수정, 기능별 커밋. path 검증/승인/Undo/웹 정책을 완화하거나 tool round/자동 retry로
   모델 오동작을 숨기지 않는다. 새 라이브러리·범용 registry는 현재 필요하지 않다.
4. non-UI pytest와 Ruff를 실행한다. GUI 실행 없이 interactive에서 기존 인수 17항목과
   이번 CSV/JSON·txt/PDF·read_document·binary Undo·후보 선택을 검증한다.
5. 충돌 이름 자동 `(1)`, 명시 출력 충돌, 원본 변경 후 재승인, 잘못된 token, 거절/EOF/exit,
   취소 중 안전한 종료와 다음 실행 Undo까지 추가한다.
6. macOS helper/포트 권한이 차단되면 환경 오류와 코드 오류를 구분한다. 실제 API smoke는
   별도 명시된 provider/model/base URL로 격리하고 비용·미검증 범위를 기록한다.
7. 수정 결과 문서에 ID별 commit, 테스트 수/제외 범위, interactive 근거,
   아직 검증하지 않은 UI/실제 API/OS 항목을 남기고 docs/status.md를 갱신한다.

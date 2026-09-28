# LLM/MCP 수동 실행 기반 수정 계획

## 목적과 범위

`llm_mcp_transcript.py`로 README의 예시를 실제 provider, Filesystem MCP,
Playwright MCP 및 macOS AX client에 연결해 실행한 결과를 바탕으로, 도구 호출의
회복력과 진단 로그의 신뢰성을 개선한다.

수정 대상은 다음으로 한정한다.

- `voice_action/planner.py`: 모델이 이미 성공한 단순 읽기 결과를 반복 호출하지
  않도록 도구 loop의 종료/재질문 정책을 보완한다.
- `llm_mcp_transcript.py`: 동시 실행에서도 한 실행의 기록 단위가 섞이지 않고,
  민감하거나 과도한 payload를 남기지 않도록 진단 기록 방식을 바꾼다.
- 관련 단위 테스트와 통합/회귀 테스트만 추가 또는 조정한다.
- 필요하면 README의 **실제 수동 검증 예시/제약 설명만** 현재 동작에 맞춘다.

## 금지 범위

- Filesystem allowlist, 절대 경로 요구, canonical path/symlink 검증, 승인 nonce,
  Undo write-ahead journal, 웹 URL 차단 정책을 완화하거나 우회하지 않는다.
- 상대경로, 빈 경로, 공백을 제거한 경로를 자동으로 허용하지 않는다. 모델의
  잘못된 인자는 계속 거부해야 한다.
- 승인 없는 `write_file`, 삭제/이동/디렉터리 생성, 브라우저 상호작용을 추가하지
  않는다.
- 모델·provider 변경, 자동 재시도, tool-loop 상한 확대를 이 문제의 해결책으로
  사용하지 않는다.
- 수동 검증을 위해 기존 `voice-action-test`나 실제 Undo journal의 파일을
  만들거나 덮어쓰지 않는다. 독립 임시 allowed root를 사용한다.

## 관찰 사실 (2026-09-21)

아래는 실제 실행 콘솔과 `llm_mcp_transcript.log`에서 확인한 사실이다.

| 예시 | 관찰 결과 |
| --- | --- |
| 디렉터리 목록 | `filesystem__list_directory` 1회 후 파일 4개를 반환하고 정상 완료했다. |
| 파일 검색 | 첫 `search_files` 인자의 `path` 앞에 공백이 있어 `도구 경로는 절대경로여야 합니다.`로 거부됐다. 로그에는 이후 모델이 최대 5회까지 진행해 `도구 호출 한도 초과`로 끝난 기록이 있다. |
| `test1.md` 읽기 | 모델이 먼저 상대경로 `test1.md`, 이어 빈 `path`로 호출해 두 번 거부됐고, 세 번째 절대경로 읽기로 성공했다. |
| `test1.md` 요약 | 잘못된 검색 경로 뒤 절대경로 검색과 읽기로 회복해, 내용 요약을 반환했다. |
| 모호한 회의록 요약 | 검색 후 `manual-summary.txt`, `summary.txt` 두 후보를 제시하고 번호 선택 대기로 전이했다. |
| 원본 없는 `result.txt` 요약 저장 | 요약할 원본이 없으므로 쓰기 승인 대신 필요한 내용을 묻는 최종 응답으로 끝났다. 대상 파일은 생성되지 않았다. 이는 모델의 동작상 실패가 아니라, 요청에 원본이 없었던 입력 조건이다. |
| 원본을 명시한 새 파일 저장 재시험 | `test1.md를 요약해 new-result.txt에 저장해줘`는 몇 차례의 경로 오류 뒤 파일 검색·읽기를 수행하고 `new-result.txt`에 대한 `create` 승인 대기까지 도달했다. 승인 입력은 보내지 않아 새 파일은 생성되지 않았다. |
| 기존 파일 덮어쓰기 재시험 | 테스트 초기값이 있는 `result.txt`에 `test1.md` 요약을 저장하도록 요청하자, 목록·읽기 뒤 `operation: overwrite` 승인 대기까지 도달했다. 승인 입력은 보내지 않아 초기 파일 내용은 유지됐다. |
| 웹 읽기 | `example.com` navigation → snapshot → 요약 순서로 정상 완료했다. |
| 웹 저장 | 첫 모델 turn이 결과를 바꾸지 않는 clarification을 요청했고 정책에 거부됐다. 다음 turn은 지원하지 않는다고 응답해, navigation/snapshot/write 승인 경로로 진행하지 않았다. |
| 활성 앱 | `macos__get_active_app`이 세 번 모두 `Code`를 성공 반환했지만, 모델이 세 번 반복 호출한 뒤 `현재 활성 앱을 확인할 수 없습니다.`라고 모순된 답변을 냈다. |
| 대화형 Undo | `undo`는 기존 active journal 항목을 사용해 `이전 파일 내용을 복원했습니다.`를 반환했다. 이 결과는 현재 workspace에 이미 존재하던 journal 상태에 의존한다. |

추가로, 로그의 `회의록 파일을 찾아줘` 기록 중간에 `test1.md 내용을 읽어줘`의
`TRANSCRIPT START`가 나타나고, 서로 다른 실행의 LLM turn과 FINAL OUTCOME이
교차했다. 즉 현재 단일 append 로그만으로는 병행 실행의 실행 단위를 신뢰성 있게
재구성할 수 없다.

## 원인 가설 (검증 필요)

1. 모델 프롬프트에 allowed root의 절대경로는 제공하지만, 경로 인자를 정확한
   절대경로(앞뒤 공백 없음)로 보내야 한다는 제약이 충분히 눈에 띄지 않아 첫
   호출 실패와 불필요한 tool round가 발생한다.
2. 성공한 `macos__get_active_app` 결과 뒤에도 다음 모델 turn에 같은 도구가
   남아 있어, 모델의 반복 호출과 성공 결과를 무시한 최종 답변이 가능하다.
3. 웹 저장 요청의 단계(명시 URL → navigation → snapshot → 승인된 write)가
   프롬프트에서 일반 파일 작업/clarification 지시보다 우선순위가 낮아 모델이
   불필요한 clarification 뒤 조기 포기했다.
4. `TranscriptLog.write()`가 매 record마다 파일을 열어 append할 뿐 실행 전체를
   식별하거나 잠그지 않아, 복수 프로세스가 동일 로그에 기록하면 실행 경계가
   교차한다.
5. transcript가 raw messages, tool arguments, tool results를 그대로 남기므로
   수동 진단에는 유용하지만 장문 파일 내용·웹 snapshot·추후 민감 입력이 로그에
   과도하게 남을 수 있다.

## 최소 수정 단계

1. **재현을 테스트로 고정한다.**
   - scripted model/gateway로 공백 포함 절대경로, 상대경로, 빈 경로가 거부되는
     기존 보안 계약을 명시한다.
   - 첫 경로 거부 뒤 정확한 allowed-root 절대경로로 회복하는 흐름과, 회복하지
     못하는 경우의 bounded 실패를 별도 테스트로 고정한다.
   - 성공한 macOS 결과가 동일 도구 반복 없이 사용자에게 전달되는 회귀 테스트와,
     웹 저장이 navigation → snapshot → `ApprovalRequired`까지 도달하는 회귀
     테스트를 추가한다.

2. **프롬프트와 loop 도구 노출을 최소 보완한다.**
   - 시스템 지시에 경로 인자는 제공된 alias의 **정확한 절대경로**만 사용하고,
     상대/빈/앞뒤 공백 경로를 보내지 말라는 짧은 규칙을 추가한다.
   - 경로 검증 자체는 변경하지 않는다. 거부 결과에도 canonical allowed root와
     다음에 가능한 안전한 행동만 전달해 회복을 유도한다.
   - `macos__get_active_app` 성공 후에는 같은 호출을 다시 선택할 수 없게 하거나,
     bounded 결과를 deterministic final response로 변환하는 작은 분기를 둔다.
     어느 쪽이 기존 모델 요약 계약에 덜 영향을 주는지는 테스트로 선택한다.

3. **웹 저장 전용 순서를 강제한다.**
   - 사용자 원문에 URL과 출력 파일명이 모두 있는 웹 저장 intent를 식별하는
     기존 planner 상태를 점검한다.
   - 이 intent에서는 결과를 바꾸지 않는 clarification을 model turn에 전달하지
     말고, navigation 성공 뒤 snapshot만 노출하고 snapshot 성공 뒤에만
     `filesystem__write_file` 승인을 허용한다.
   - navigation/snapshot 실패 시의 write 차단, redirect 검사, filename 명시성은
     그대로 유지한다.

4. **transcript를 실행 단위로 안전하게 기록한다.**
   - 각 실행에 UUID 또는 시작 시각 기반 run ID를 붙이고, START/turn/OUTCOME을
     같은 run ID로 연결한다.
   - 실행 전체를 한 번의 구조화된 record로 종료 시 기록하거나, 최소한 파일 잠금과
     run ID를 사용해 다른 실행과 섞여도 parser가 정확히 분리할 수 있게 한다.
   - 예외/취소에도 종료 상태가 남도록 `finally` 경로를 마련한다.
   - 기본 로그에는 content/arguments/tool result의 문자 수, digest 또는 bounded
     preview만 남기고, 원문 기록은 명시적 개발용 opt-in 플래그로 제한한다.

5. **문서와 수동 절차를 정렬한다.**
   - overwrite 수동 검증은 격리 임시 root에 사전 생성한 `result.txt`를 사용하고,
     새 파일 생성 시나리오와 분리한다.
   - Undo 수동 검증은 기존 `.voice-action/undo.sqlite3`를 재사용하지 않고,
     해당 테스트가 만든 승인된 write의 command만 되돌리도록 격리한다.
   - 실제 API/provider 응답은 비결정적이므로, 기대 결과가 아닌 안전 불변식과
     tool 순서를 기준으로 판정한다고 README에 명시한다.

## 테스트와 수동 확인

- `uv run --locked pytest -q`
- `uv run --locked ruff check .`
- transcript 단위 테스트:
  - 두 transcript writer를 병행 실행해도 run ID별 JSON/record가 parse 가능하고
    START/OUTCOME이 교차하지 않음을 확인한다.
  - 긴 tool result와 secret-like 문자열이 기본 로그에 원문으로 남지 않음을
    확인한다.
  - 예외/취소 시에도 종료 record가 하나 남음을 확인한다.
- planner 회귀 테스트:
  - 잘못된 경로가 MCP 호출 전에 거부됨을 유지한다.
  - 활성 앱 성공값을 한 번의 호출 뒤 정확히 반환하고 재호출하지 않는다.
  - 웹 저장은 navigation, snapshot, 승인 대기 순서를 지키며 실패 시 write를
    호출하지 않는다.
- 수동 smoke test는 새로운 임시 allowed root와 별도 transcript 로그 파일을
  사용한다. 새 파일/overwrite 모두 입력 원천으로 `test1.md`를 명시하고,
  overwrite 대상은 사전 생성한 `result.txt`를 사용한다. 목록, 읽기, 요약,
  다중 후보 clarification, 새 파일 승인 대기, 기존 파일 overwrite 승인 대기,
  웹 읽기, 웹 저장 승인 대기, 활성 앱, 격리된 Undo를 각각 확인한다.

## 완료 기준

- 12개 수동 시나리오가 격리 환경에서 실행되고, 각 transcript run을 다른 run과
  혼동 없이 추적할 수 있다.
- 상대/빈/공백 포함 경로는 계속 거부되며, 정상 요청은 불필요한 반복 tool call
  없이 완료하거나 안전하게 종료한다.
- `macos__get_active_app`의 성공 값과 최종 응답이 일치한다.
- URL과 출력 파일명이 명시된 웹 저장 요청은 성공 시 snapshot 후 승인 대기에
  도달하고, 실패 시에는 파일을 쓰지 않는다.
- 기본 transcript에 비밀값·전체 파일 본문·전체 웹 snapshot이 남지 않는다.
- 전체 pytest와 Ruff가 통과하며, 기존 승인·Undo·경계 검증 회귀가 없다.

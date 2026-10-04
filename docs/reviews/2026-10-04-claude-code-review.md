# Claude Code 전달용 코드 리뷰 — 자기 피드백 루프와 실행 안전성

작성일: 2026-10-04. 검토 코드 기준: `f3578ea`.
주요 기능 커밋: `a90cba4`(자기 피드백 루프), `f3578ea`(UI 한글 입력 수정).

## 1. 결론과 작업 지시

기존 명령 실행·승인·Undo의 정상 경로는 통과했다. 그러나 피드백 기록의 비밀값 처리,
분석 대상 명령의 연결, lesson 적용 시점에 결함이 있다. M5의 2주 운영 평가에 앞서
아래 P1/P2 문제를 수정해야 재발률과 비용 지표를 신뢰할 수 있다.

이 리뷰에서는 production 코드를 수정하지 않았다. 리뷰 문서와 재현 스크립트만 추가한다.
Claude Code는 **실패 재현 테스트 → 최소 수정 → 해당 회귀 및 interactive 인수 검증** 순서로
진행한다. UI를 실행하거나 UI로 수동 검증하지 않는다. 원본 `voice-action-AI`는 수정하지 않는다.

| ID | 우선순위 | 발견 내용 | 영향 |
|---|---|---|---|
| F01 | P1 | payload 이외의 비밀값 누출, 원문 기본 기록·분석 전송 | DB, lesson, 콘솔, 외부 분석 입력에 민감 내용 잔류 |
| F02 | P1 | 직렬화된 JSON에 Bearer 마스킹을 적용해 구조 파괴 | 해당 명령의 감지 전체 누락, 분석 bundle 손상 |
| F03 | P2 | 피드백 예외가 실행기로 전파되고 `_in_flight`가 남음 | 선택 기능 오류로 다음 명령이 계속 Busy |
| F04 | P1 | 분석 작업이 변경 가능한 현재 run/session을 참조 | 다른 명령에 결과 귀속, 근거 누락과 잘못된 학습 |
| F05 | P2 | 명령 중간에 활성 lesson을 다시 읽음 | 같은 명령·승인 continuation의 지침이 바뀜 |
| F06 | P2 | lesson 적용 여부 없이 재발 횟수를 증가시킴 | 효과 없다고 오판하고 유효 지침을 비활성화 |
| F07 | P2 | 동기 SQLite I/O와 기본 lock 대기가 이벤트 루프를 막음 | 입력·LLM·취소 지연, 피드백 장애가 본 실행에 영향 |
| F08 | P2 | 피드백 DB/report 경로에서 symlink를 허용 | 내부 기록이 다른 파일에 쓰기, 민감 로그 권한 관리 누락 |
| F09 | P1 | overwrite 승인에 기존 파일 내용/정체성이 묶이지 않음 | 승인 대기 중 외부 변경을 재승인 없이 덮어씀 |
| F10 | P2 | 취소된 분석 요청이 일일 한도에서 빠짐 | 이미 시작한 외부 요청 횟수가 설정 한도를 초과 |

P1은 민감 정보·데이터 변경·학습 근거의 신뢰성 때문에 운영 전에 수정할 항목이다.
P2는 기능 신뢰성과 제한된 실패 조건의 문제다. F09는 피드백 도입 이전부터 있던 문제이며
피드백 기능의 회귀로 분류하지 않는다. 이번 재현에서 승인 없는 파일 변경이나 allowlist
우회가 확인된 것은 아니다.

## 2. 검토 범위와 검증 근거

직접 읽은 범위: `main.py`, `config.py`, `runtime.py`, `feedback.py`, `risk_guard.py`,
`mcp_manager.py`, Filesystem/Playwright client, `undo.py`, `deletion.py`, 관련 Planner 분기와 테스트.
UI는 피드백 연결 및 한글 입력 변경 diff를 정적으로 확인했으며 실행 검증에서 제외했다.
설계 artifact URL의 내용을 조회하지 않았다. 설계 적합성 판단은 저장소의 코드·README·
AGENTS.md·docs/status.md에 한정한다.

### 자동 회귀

```sh
env -u FORCE_COLOR .venv/bin/python -m pytest -q \
  --ignore=tests/test_ui.py -k 'not ui' -p no:cacheprovider
.venv/bin/ruff check .
```

- pytest: **305 passed, 22 deselected in 27.67s**. UI 테스트 파일은 수집하지 않았다.
- Ruff: **All checks passed**. 리뷰 재현 스크립트 추가 후에도 확인했다.
- 처음 샌드박스 실행에서는 296 passed, 22 deselected, 9 setup errors였다.
  모두 로컬 HTTP fixture의 `bind()`에 대한 PermissionError였다. 권한 승인 후 동일 명령을
  재실행해 위의 305개 통과를 확인했다. 코드 결함 실패와 실행 환경 차단을 구분한다.
- 이 결과는 UI 제외 결과이며 전체 344개 통과의 재검증이라고 표현하면 안 된다.

### 추가 재현 및 interactive 인수

```sh
env -u FORCE_COLOR .venv/bin/python -u docs/reviews/reproduce_feedback_review.py
```

동봉 스크립트는 합성 문자열과 임시 디렉터리만 사용한다. API 키·사용자 파일·기존 journal을
읽지 않는다. 전반부는 **현재 결함이 존재함을 assert하는 리뷰용 증거**다. 결함 수정 후에는
이 assert가 실패하는 것이 맞다. 이를 그대로 정상 회귀 테스트로 편입하면 안 된다.
수정 후 기대 동작으로 뒤집은 테스트를 `tests/`에 작성한다.

후반부는 `main.main(['--interactive'])`를 호출해 실제 stdin pipe와 실제 Filesystem MCP를
사용한다. LLM은 scripted model, 분석기는 fake analyzer이며 외부 LLM API는 호출하지 않는다.
웹·macOS는 이 인수 스크립트에서 연결하지 않는다. 웹 통합은 위 pytest 실행으로 검증했다.

| interactive 시나리오 | 확인한 결과 |
|---|---|
| 목록 조회, source 읽기 | 실제 Filesystem MCP 정상 호출 |
| 읽기 → create 요청 → 승인 | 파일 생성, 모델 continuation 완료 |
| create 요청 → 거절 | 대상 파일 없음 |
| overwrite → 승인 → `undo` | 초기 원문 복원 |
| 단일 삭제 → 승인 → `undo` | 삭제 파일 원문 복원 |
| 같은 PlannerError 3회 | 명령 루프 유지, incident 분석 완료 |
| `exit` | 분석·MCP 종료, CLI 반환 0 |

실제 Groq/OpenRouter의 응답 품질·과금·네트워크와 OS 권한은 이번 리뷰에서 검증하지 않았다.
현재 status의 실제 API 검증 기록과 이번 scripted 인수 결과를 섞지 않는다.

## 3. 발견 사항 상세

### F01 — 민감 정보 처리 경계가 기록·적용 경로마다 다름 [P1]

위치: `feedback.py:116` `_digest`, `:124` `_summarize_message`, `:167` 응답 기록,
`:242` `record`, `:254` `user_input`, `:462` `_apply`, `:478` `_add_lesson`.

확인 사실:

1. `record()`는 payload에만 마스킹한다. `events.signature`는 그대로 INSERT한다.
   `user_input()`은 원문을 `normalize()`한 것을 signature로 사용한다.
   합성 secret `syntheticsecretletters`를 등록하고 같은 입력을 넣으면 payload에서는 제거되지만
   signature에는 그대로 남는다. normalize는 보안 마스킹 함수가 아니다.
2. `_digest()`에는 길이/hash뿐 아니라 원문 첫 500자의 `head`가 있다. 사용자·assistant 내용은
   최대 4,000자, 도구 arguments도 최대 4,000자가 기본 기록된다. `write_file.content` 역시
   arguments 안에 남는다. 따라서 파일·웹 내용, 쓰려던 본문, 답변에 재인용된 원문이 DB에 남고
   `_bundle()`을 거쳐 OpenRouter 분석 입력에도 포함될 수 있다. 재현은 실제 전송 없이 bundle을
   검사해 확인했다. 코드의 “keep only a fingerprint” 주석과 실제 저장 정책도 다르다.
3. 분석 결과는 `record('feedback')`에서는 마스킹되지만 원래 result를 `_apply()`에 넘긴다.
   채택 lesson은 DB에 그대로 INSERT하고 user_hint는 그대로 emit한다. 등록한 합성 secret을
   포함한 유효 결과를 적용하면 lesson와 콘솔 출력에 secret이 남는다.
4. `_clip()`/`_digest()` 이후 마스킹하므로 알려진 secret이 절단 경계에 걸리면 완전 일치로
   치환하지 못할 수 있다. JSON escape된 문자열 역시 단순 전체 문자열 치환으로 충분하지 않다.

영향: 최근 검증의 “DB 안 API 키 0건”은 사용한 특정 입력에 대한 결과다. 모든 컬럼, lesson,
hint, 절단 경계를 포괄하는 보장으로 확장할 수 없다. 일반 문서·개인정보는 API key 패턴과
다르므로 key 마스킹만으로 원문 비노출 계약을 만족하지 못한다.

최소 수정 방향:

- 저장할 문자열을 **마스킹 → 필요한 메타데이터/요약 생성 → JSON 직렬화** 순서로 처리한다.
- signature도 안전한 값에서 만들거나 비가역 digest로 만들고, raw signature를 report에 쓰지 않는다.
- 기본 저장/전송은 tool명, 상태, 길이, digest, 안전한 오류 코드 중심으로 줄인다.
  쓰기 content와 tool 결과 head를 기본 기록에서 제거하고 assistant의 원문 재인용도 점검한다.
- 분석 결과를 검증·정제한 단일 값으로 만든 뒤 event/lesson/hint/report 모두 같은 값을 사용한다.
  credential이 포함된 lesson은 마스킹 후 채택하기보다 거부하는 편이 낫다.
- 이미 저장된 민감 데이터는 새 코드로 자동 소급 정제되지 않는다. 사용자 소유 DB를 임의 삭제하지
  말고, 기존 기록 정리 필요성과 범위를 안내한다.

필수 테스트: events의 payload/signature, lessons, incidents, report, emit, analyzer bundle 전체 검사;
Bearer, 알려진 key, 따옴표·역슬래시·개행 포함 secret, 500/4,000자 경계, write content,
assistant의 본문 재인용. 원문 marker가 기본 저장/전송에 없어야 한다.

### F02 — Bearer 마스킹이 JSON 끝 구분자를 소비함 [P1]

위치: `feedback.py:44` `SECRET_PATTERN`, `:242` `record`, `:304` `_detect`.

재현: `loop.user_input('Bearer synthetictoken', new_run=True)` 후 payload를 `json.loads()`하면
JSONDecodeError가 발생한다. `Bearer\s+\S+`가 JSON 문자열의 닫는 따옴표·중괄호까지 소비한다.
`record()` 자체는 성공했으므로 정상 event처럼 저장된다. `_detect()`가 이를 파싱하다 실패해
`run_finished()`는 해당 run의 감지 결과 전체를 `[]`로 반환한다.

영향: 실제 credential을 포함한 실패 기록일수록 감지가 사라질 수 있다. 일부 필드만 잘못되어도
다른 규칙의 분석까지 누락된다. `_bundle()`에는 잘못된 JSON 텍스트가 전달될 수 있다.

수정: F01의 문자열 단위 정제 후 직렬화로 해결한다. 기존 손상 payload는 row 단위로 안전하게
건너뛰고 요약 경고를 남긴다. 한 row 때문에 같은 run의 유효 기록을 모두 버리지 않는다.

필수 테스트: 모든 INSERT payload가 JSON parse 가능; Bearer가 문자열 끝/중간/중첩 messages에
있는 경우; 기존 손상 row와 유효 반복 오류 row가 함께 있어도 유효 규칙은 감지되는 경우.

### F03 — “피드백은 명령을 깨뜨리지 않음” 계약이 지켜지지 않음 [P2]

위치: `runtime.py:148` 및 승인·거절·Undo의 `try` 이전 feedback 호출;
`feedback.py:285` `_warn`, `:147` `RecordingModel.generate`, `:526` `close`, `:541` `build_feedback`.

재현: 피드백 DB를 사용할 수 없게 하고 emit이 RuntimeError를 내게 하면 `record()`의
예외 처리에서 `_warn()`이 다시 예외를 낸다. `submit()`은 `_in_flight=True` 설정 후 `try`
바깥에서 user_input을 호출하므로 finally까지 도달하지 못한다. 다음 `submit()`은 Busy다.
합성 DB close + 출력 실패로 이 경로를 재현했다. 일반 정상 출력 상황에서 항상 발생하는
문제라고 해석하면 안 된다.

추가 코드상 위험: request/response 요약 생성은 `record()` 호출 이전에 실행돼 자체 오류를
격리하지 못한다. analyzer 생성은 build_feedback의 try 밖에 있다. close의 emit/대기/DB close
역시 종료 결과를 바꿀 수 있다. DB 초기화 실패 후 이미 만든 analyzer의 정리 경로도 없다.

수정: `_in_flight` 설정 이후의 모든 작업을 기존 try/finally 안으로 옮긴다. 피드백 정제·요약·
기록·경고의 일반 예외는 명령 실행과 분리하되 본 LLM/도구 오류와 CancelledError는 숨기지 않는다.
종료는 출력 실패와 무관하게 task/client/DB를 정리하도록 finally를 사용한다.
새 범용 이벤트 프레임워크 대신 작은 공통 격리 지점이면 충분하다.

필수 테스트: DB closed/locked, emit 실패, 요약 실패, analyzer 생성 실패, 종료 중 취소;
기존 명령 결과와 다음 명령 실행 가능 여부 확인. 이미 성공한 쓰기와 journal을 유지해야 한다.

### F04 — incident의 근거와 결과가 원래 run에 고정되지 않음 [P1]

위치: `feedback.py:372` `_queue`, `:422` `_bundle`, `:441` `_analyze`, `:494` `_report`.

재현 A: run A에서 `_queue()`한 뒤 task가 시작되기 전에 run B를 시작한다. 분석 bundle에는
B의 입력이 들어가고, 분석 완료 event와 report의 run_id는 B로 기록된다. incidents row는 A다.
재현 B: 같은 DB로 세 번 세션을 열어 같은 오류를 기록하면 R1은 세 run을 감지하지만
`_bundle()`은 현재 session의 run만 조회해 앞선 두 실패 근거를 제외한다.

영향: incident를 재현할 수 없고, 엉뚱한 사용자 입력으로 원인을 설명하거나 lesson을 만들 수 있다.
M5의 재발률·분석 품질·관련 run 추적이 잘못된다. 사용자가 다음 명령을 빨리 입력하면
interactive에서도 발생 가능한 경합이다.

수정:

- queue 시 incident_id/origin_run_id/rule/signature와 해당 규칙에 실제 사용한 근거 run들을 고정한다.
- bundle은 현재 최근 3개 run이 아니라 그 incident의 근거를 사용한다. R1/R3의 기간 내 과거
  session 근거도 포함하되 F01의 안전한 기록 정책을 적용한다.
- 분석 completion/error event와 report에는 고정 origin_run_id를 명시한다.
- 분석 입력에 rule뿐 아니라 안전한 signature/대상 지표를 포함한다. 같은 R1에서 무엇을
  분석하는지 구분할 수 있어야 한다.
- bundle 크기를 줄일 때 JSON line 중간을 자르는 `[-BUNDLE_CHARS:]` 대신 완전한 record 단위로
  제한하고 truncation 메타데이터를 남긴다.

필수 테스트: 분석 지연 중 B 시작/승인/Undo, 동시 incident 여러 개, 세션 재시작 후 R1,
긴 bundle. 모든 분석 관련 결과가 origin에 연결되고 사건 이후 무관한 입력은 근거가 아니어야 한다.

### F05 — “다음 명령부터 적용”과 실제 적용 시점이 다름 [P2]

위치: `feedback.py:147` 매 generate의 `system_suffix()` 조회, `:492` 적용 안내.

재현: 한 run의 첫 generate 후 lesson을 추가하고 두 번째 generate하면 새 lesson이 포함된다.
이전 명령의 분석 task가 현재 명령의 도구 실행 대기 중 완료돼도 같은 경로다.
승인 continuation 또한 같은 wrapper를 사용한다.

영향: Planner의 메시지 사본을 보존하는 것만으로 명령의 지침을 고정하지 못한다.
새 지침이 실행 중간에 바뀌어 재현성과 lesson 효과 측정이 떨어진다. 승인 nonce 자체가
우회되는 것은 아니지만 승인 후 이어지는 모델 계획에 영향을 준다.

수정: 새 run 시작 시 활성 lesson ID와 정제된 텍스트를 snapshot하고 그 run의 모든 generate,
승인·재승인·clarification resume에서 유지한다. 이미 실행 중인 명령에는 새 lesson을 추가하지 않는다.
lesson 비활성화의 긴급 적용이 필요하다면 별도 명시 정책으로 정의한다.

필수 테스트: 첫/둘째 모델 turn 사이 분석 완료, 승인 대기 중 분석 완료, 새 명령 시작.
현재 run은 동일 snapshot이고 다음 run에는 새 lesson이 반영되어야 한다.

### F06 — lesson의 재발 카운터에 적용 증거가 없음 [P2]

위치: `feedback.py:375` active lesson 조회와 즉시 recurrence 증가.

재현: `_add_lesson()` 후 모델에 한 번도 보내지 않고 같은 signature로 `_queue()`를 세 번 호출하면
lesson이 비활성화된다. run/incident 중복을 확인하지 않고 cooldown 검사보다 먼저 증가시킨다.
마지막 세 호출을 세 실패처럼 판단하는 기존 테스트도 현재 구현의 동작을 고정하고 있다.

영향: “지침을 사용했지만 실패”와 “지침이 아직 적용되지 않음”을 구분하지 못한다.
재시작·지연 분석·입력 검증 단계 오류에서 학습 효과를 잘못 평가한다. 운영 임계값만 조정해도
이 문제는 해결되지 않는다.

수정: F05 snapshot의 applied lesson IDs와 origin run을 기록한다. 해당 lesson을 실제로 보낸
명령에서 동일 오류가 재발한 경우만 증가시키고 `(lesson_id, run_id)` 중복을 막는다.
재분석 cooldown과 recurrence 측정은 의미가 다르므로 독립적으로 유지한다.

필수 테스트: 미적용 run, 적용 후 실제 재발, 같은 run 감지 두 번, 지연 분석, 재시작.
실제 적용된 서로 다른 세 run에서 실패했을 때만 3회 재발 판정이 나와야 한다.

### F07 — 동기 DB 작업이 명령 event loop를 막음 [P2]

위치: `feedback.py:219` connection, `:242` 기록, `:300` 감지, `:372` queue.

재현: 다른 SQLite connection으로 BEGIN IMMEDIATE를 유지하고 기록을 호출하면 lock timeout까지
동기 대기한다. 재현에서는 테스트 시간을 줄이기 위해 busy_timeout을 150ms로 바꿨고
100ms 이상 막힘을 확인했다. production은 별도 timeout 설정이 없어 기본 대기 시간이 적용된다.

영향: interactive와 다른 프로세스가 같은 DB를 쓰거나 lesson 관리 명령이 실행되면 입력·
모델 진행·취소 처리에 지연이 생긴다. 매 LLM turn에 누적 메시지를 기록하므로 기록량도 커진다.
“백그라운드 분석”이 기록 I/O까지 비동기라는 뜻은 아니다.

최소 수정: 우선 짧은 busy_timeout, 적절한 WAL 설정과 lock 실패 시 비필수 기록 건너뛰기를
검토한다. 외부 task/전용 worker를 새로 도입하는 것은 측정 후 결정한다.
단순히 현재 SQLite connection을 `asyncio.to_thread()`로 옮기면 thread affinity 문제가 생기므로
그 방식으로 고치지 않는다. 느린 디스크 I/O의 제한은 별도 측정·문서화한다.

필수 테스트: 외부 connection lock 중 명령을 계속 처리하고 종료가 과도하게 늦지 않는지,
안전한 경고가 한 번만 나오며 기록 손실이 명시되는지 확인한다.

### F08 — 피드백 저장소와 report가 symlink를 따라감 [P2]

위치: `feedback.py:218` 저장소 초기화, `:500` report append.

재현: 임시 폴더의 `feedback-report.md`를 다른 임시 파일로 연결한 뒤 `_report()`하면 그 파일에
append된다. DB도 `sqlite3.connect(path)` 전에 regular file/symlink 검사가 없다.
재현 스크립트는 report 경로를 실제 검증했고 DB 경로는 정적 확인이다.

영향: 내부 로그 파일을 기록하는 권한이 임의 다른 파일을 변경하는 경로가 된다.
공격 전제는 로컬 경로를 미리 바꿀 수 있는 주체다. 웹 페이지가 바로 symlink를 만들거나
외부 경계를 자동으로 우회한다는 뜻은 아니다. 현재는 로그 권한을 소유자 전용으로 고정하지도 않는다.

수정: Undo journal의 경로 검증 원칙을 참고해 DB/report의 regular file·symlink 및 부모 경로를
검사한다. report는 지원 플랫폼에서 O_NOFOLLOW 기반으로 열고 소유자 전용 권한을 고려한다.
SQLite file 확인과 실제 open 사이의 경합 한계도 문서화한다. 피드백이 실패하면 정상 명령을 계속한다.

필수 테스트: DB symlink, report symlink, 디렉터리 대상, parent symlink, 불가 권한.
연결된 다른 파일이 바뀌지 않아야 하며 기존 journal이나 다른 사용자 데이터를 삭제하면 안 된다.

### F09 — overwrite 승인 시점의 원본 파일 상태를 고정하지 않음 [P1, 기존 결함]

위치: `risk_guard.py:145` `_new_prepared`, `:218` `_snapshot` 비교;
`undo.py:180` `prepare_write`.

재현: 기존 파일 A에 대한 overwrite 승인을 준비 → 외부에서 같은 경로의 내용을 B로 변경 →
기존 prepared를 승인 실행. target/path/operation/new content가 같으므로 재승인 없이 덮어쓴다.
재현은 합성 local gateway로 실제 파일 부작용을 확인했다. 실제 MCP transport로 이 충돌
시나리오를 별도 실행한 것은 아니다. 비교가 transport 호출 이전에 누락되는 코드 결함이다.

영향: 사용자가 확인했던 원본 상태와 다른 파일을 덮어쓴다. Undo가 실행 직전 B를 백업하기 때문에
복원 가능성은 있지만 의도하지 않은 overwrite를 막지는 못한다. README의 “파일 상태가 달라지면
새 승인” 설명은 내용 변경 및 same-path replacement까지 보장하는 것으로 읽힐 수 있다.
삭제 쪽은 snapshots로 내용을 비교하고 있어 두 작업의 보장 수준도 다르다.

수정: overwrite 승인 준비에 기존 파일 fingerprint와 필요한 identity를 포함하고 실행 직전
정규화 및 journal 준비 단계에서도 비교한다. 달라지면 새로운 prepared로 재승인을 요청한다.
원문 bytes를 approval repr/log에 넣지 않는다. create→overwrite 재승인, one-shot nonce,
Undo write-ahead 동작을 유지한다. 외부 프로세스와의 완전한 파일 transaction은 보장하지 않는다.

필수 테스트: 동일 파일 내용 변경, 같은 경로 다른 inode 교체, create→overwrite, 삭제 후 재생성,
symlink 교체. 오래된 승인으로 MCP write가 0회여야 하고, 새 승인 이후에는 정상 write/Undo해야 한다.

### F10 — cancelled 분석이 일일 호출 수에서 제외됨 [P2]

위치: `feedback.py:398` budget SELECT, `:448` cancelled 상태, `:526` 종료 대기.

재현: daily_limit=1에서 fake analyzer 요청 시작 → `close(wait_seconds=0)`로 취소 → 같은 DB를
다시 열고 다른 signature의 분석을 queue하면 두 번째 analyzer도 시작된다.
첫 incident가 cancelled로 바뀌면서 한도 집계에서 빠지기 때문이다.

영향: 애플리케이션 취소가 외부 provider의 요청 처리/과금 취소를 보장하지 않는다.
실제 과금 초과를 측정한 것은 아니지만, 한도 1에서 이미 시작한 요청이 2회라는 사실은 확인했다.
정상 종료의 15초 대기와 analyzer timeout을 넘는 요청이 있는 짧은 세션에서 반복될 수 있다.

수정: 최소한 queue에 예약된 quota를 cancelled에서도 유지한다. 더 세밀하게 할 경우
started_ts를 기록하되 요청 시작 후 취소된 것은 호출 수에 포함한다. crash 뒤 남은 queued도
무조건 예산에서 빼지 않는다. “일일”이 rolling 24시간이라는 현재 의미는 문서화한다.

필수 테스트: 시작 후 취소·재시작, queue 직후 취소, 성공/실패/timeout, 다른 signature.
설정 한도 N에서 이미 시작한 분석 요청이 N을 넘지 않아야 한다.

## 4. 개선점 — 결함 수정 후 검토

다음 항목은 위 발견 사항과 구분한다. 즉시 큰 리팩터링의 근거로 사용하지 않는다.

- **R2 identity**: full arguments가 아닌 `_clip()`한 문자열을 hash한다. 같은 길이의 긴
  arguments가 앞 4,000자가 같고 뒤만 달라지면 서로 다른 호출을 반복으로 볼 수 있다.
  raw content를 저장하지 않으면서 전체 canonical arguments의 digest를 먼저 산출하는 편이 낫다.
  JSON key 순서/공백 차이에 따른 false negative도 함께 확인한다.
- **R3/R4/R5 의미**: 정상 목록 조회 세 번도 retry로 볼 수 있고, 정상적인 거절·Undo도 실패
  분석 신호가 된다. 특히 Undo는 최근 approve event를 찾을 뿐 실제 복원 journal entry나
  Undo 성공을 연결하지 않는다. “감지 신호”와 “실패 확정”을 분리하고 오탐률을 측정한다.
  R5 이전 입력 조회에는 ORDER BY가 없고 clarification 답변도 user_input이므로 원인 연결을 점검한다.
- **정책 키워드 필터**: `UNSAFE_LESSON_PATTERN`은 제한적인 표현 검사다. 이를 보안 경계나
  prompt injection 완전 방어라고 표현하면 안 된다. 코드의 RiskGuard/URL/path 검증을 유지하고,
  사용하지 않는 도구를 유도하거나 허위 완료를 지시하는 lesson도 거부 대상으로 검토한다.
- **JSON 스키마**: action=lesson인데 lesson이 null인 결과도 스키마 통과한다.
  분석 성공 카운트와 실제 적용 여부를 분리하고 action별 필수 값/길이를 검증한다.
  `root_cause`와 추가 필드에 상한이 없어 report·event가 커질 수 있다.
- **보존 정책**: event 삭제·lesson 만료가 시작 시에만 수행되고 incidents와 inactive lessons는
  계속 남는다. 오래 켜 놓은 interactive의 만료·성장 상한을 정의한다. 주기적 별도 서비스 대신
  적당한 run 종료 주기의 정리부터 고려한다.
- **CLI lesson 관리**: `main.py`가 import한 DEFAULT_DB_PATH는 autouse monkeypatch 대상과
  별개다. 실제 DB를 건드리지 않는 관리 CLI 테스트를 추가한다. manage_lessons는 main의
  일반 오류 처리 이전에 호출돼 DB 손상 시 traceback이 나올 수 있으므로 안전한 오류로 바꾼다.
- **문서/설정**: feedback 설정은 기존 JSON 병합이 아니라 환경변수에서만 읽는다.
  이 차이, 기본 on 상태의 기록·외부 분석 범위, 비용 한도, 종료 대기 시간을 README에 명확히 쓴다.
  README의 옛 228개 결과는 최신 status 기록과 구분하고 필요하면 갱신한다.
- **범위 제한**: DNS rebinding/egress 격리, 외부 프로세스와 파일 TOCTOU, 오래된 원본 경로의
  Undo DB는 기존 제한이다. 이번 피드백 수정에 DNS 프록시·새 실행 프레임워크·STT를 섞지 않는다.

## 5. 영향도와 권장 변경 단위

| 변경 단위 | 수정 파일 | 보존해야 할 계약 | 검증 |
|---|---|---|---|
| A. 안전한 기록·출력 | feedback.py, test_feedback.py | 정상 Planner/도구 입력은 그대로; 기본 로그 비노출 | F01/F02와 손상 row 회귀 |
| B. 실패 격리·저장소 | feedback.py, runtime.py, 관련 테스트 | 실제 LLM 오류/취소 전파, single-flight 복구 | F03/F07/F08 |
| C. incident context | feedback.py, 필요 시 runtime.py | queue origin 고정, 범위 내 근거만 분석 | F04 delayed/cross-session |
| D. lesson 적용·측정 | feedback.py, runtime.py, 관련 테스트 | run snapshot, 승인 nonce 불변 | F05/F06 |
| E. quota | feedback.py, test_feedback.py | rolling 한도, 자동 retry 없음 | F10 cancelled/restart |
| F. overwrite 안전성 | risk_guard.py, undo.py, planner 관련 테스트 | 재승인 및 journal-before-write | F09, 실제 MCP interactive |
| G. 인수·문서 | README.md, docs/status.md | 원본/실제 journal 무변경 | non-UI pytest, Ruff, interactive |

변경 단위별로 커밋한다. production 변경을 다른 도구에 넘기기 전에 테스트와 상태 문서를 갱신한다.
별도 dependency, 전역 정책 완화, 모델 변경, 자동 retry, tool round 증가로 문제를 숨기지 않는다.
F09는 독립 커밋으로 처리해 피드백 변경과 데이터 변경 정책을 분리해 검토할 수 있게 한다.

## 6. Claude Code 완료 체크

1. 저장소 상태/HEAD와 docs/status.md를 다시 읽는다. 리뷰는 `f3578ea`를 기준으로 했으므로
   이후 변경이 있으면 발견 사항의 위치·재현을 다시 확인한다.
2. 재현 스크립트를 읽고 실행한다. 실제 결함을 정상 동작처럼 assert하는 테스트를 추가하지 않는다.
3. 각 F 항목의 수정 전 실패하는 회귀를 만들고 최소 수정한다. 확정되지 않은 개선점은
   별도 재현 후 범위를 정한다.
4. non-UI 회귀와 Ruff를 실행한다. 포트 권한으로 웹 통합을 실행 못 하면 환경 차단으로 표시한다.
5. interactive에서 승인 전 파일 무변경, 거절, create/overwrite/delete, 각각 Undo,
   잘못된 승인 토큰, pending 상태의 exit, 반복 오류, 지연 분석 중 다음 명령을 확인한다.
   정상 흐름과 F09 충돌 흐름은 실제 Filesystem MCP로도 검증한다.
6. 분석은 기본 fake로 재현한다. 실제 API smoke가 필요하면 같은 provider/model 설정을
   명시해 임시 cwd/allowed root/journal/feedback DB에서 실행하고 비밀값·raw 원문을 출력하지 않는다.
7. 새 문서에는 수정된 ID, 코드 기준점, 테스트 수·제외 범위, interactive 결과,
   미검증 실제 API/OS 항목과 남은 문제를 기록한다.

운영 평가는 위 수정 이후 시작한다. incident 수만으로 개선 효과를 판단하지 말고,
실제 적용된 lesson별 노출 run 수·동일 signature 재발률·오탐률·시작한 분석 요청 수를 함께 본다.

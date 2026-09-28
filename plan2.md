# Voice Action AI 음성 입력 구현 계획

작성일: 2026-09-21

## 1. 목표와 범위

현재 텍스트 MVP를 회귀 기준으로 고정한 뒤 STT, 마이크 녹음, R-Cmd PTT를 순서대로 검증하고 기존 텍스트 실행 경로에 연결한다.

첫 음성 MVP의 완료 범위는 다음과 같다.

- R-Cmd를 누르고 말한 뒤 떼면 한국어 텍스트를 얻는다.
- 성공한 전사 결과로 새 명령을 기존 `RuntimeController.submit(text)`에 최대 한 번 전달한다.
- 파일·웹·macOS 도구 정책, clarification, 쓰기 승인, Undo는 기존 구현을 사용한다.
- 녹음·전사 취소 및 실패 후 다시 음성 입력을 받을 수 있다.
- 기존 텍스트 명령과 안전 경계가 회귀 테스트를 통과한다.

첫 버전에서 제외하는 기능:

- 음성 승인·거절, 음성 Undo, 음성으로 clarification 선택
- 상시 청취, wake word, 스트리밍 부분 전사 실행, TTS
- 여러 음성 명령의 대기열 또는 동시 실행
- STT 결과의 파일명 자동 교정, 임의 경로 추정
- 새 파일·웹·앱 조작 도구와 기존 권한 범위 확대

이 문서는 구현 계획이다. 테스트 재실행, Git 초기화·태그 생성, 의존성 설치 및 코드 구현은 아직 수행하지 않았다. 기존 `plan.md`는 참조하지 않았다.

## 2. 현재 코드에서 출발할 지점

| 현재 구성 | 역할 | 음성 구현에서의 사용 방식 |
|---|---|---|
| `main.py:run_text()` | 단발 실행에 필요한 MCP/HTTP 연결 생성 및 종료 | 음성마다 재호출하지 않음 |
| `RuntimeController.submit()` | 텍스트 명령 실행과 clarification 입력 | 성공한 전사 결과를 전달할 공통 진입점 |
| `Planner` | 모델 호출, 도구 순서, 검색·웹 정책 | 음성 전용 Planner를 만들지 않음 |
| `RiskGuard` | 고정 도구 분류와 일회성 쓰기 승인 | 기존 승인 API 유지 |
| `UndoJournal` | 쓰기 전 기록과 최근 쓰기 복구 | 기존 typed API 유지 |
| `AsyncRuntimeWorker` | 별도 스레드의 asyncio loop와 장기 연결 소유 | 음성 이벤트와 기존 실행기를 연결 |
| `UIPresenter` / `TkView` | 입력·상태·승인·Undo 표시 | 음성 상태와 전사 미리보기 추가 |
| `load_config()` | LLM key와 허용 디렉터리까지 검증 | STT 단독 경로에서 호출하지 않음 |

확인된 주의점:

- 기존 `RuntimeState.RECORDING`, `TRANSCRIBING`은 선언만 돼 있다. `TRANSCRIBING → IDLE` 전이는 현재 없다.
- 기존 UI의 명령 실행 중 취소는 MCP 자원 정리 후 창을 닫는 동작이다. 녹음·전사 취소를 여기에 연결하지 않는다.
- 이전 읽기 전용 확인에서는 현재 폴더에 Git 저장소가 없었다. checkpoint 생성 전 실제 저장소 위치를 확인해야 한다.
- README의 `228 passed`는 기존 기록이다. 동결 대상 코드에서 새로 실행한 결과와 구분한다.

## 3. 설계 결정

### 3.1 상태와 소유권

음성 입력은 별도의 `VoiceInputController`가 소유한다. 기존 실행 상태 머신에 녹음 상태를 억지로 연결하지 않고, 기존 `RuntimeController`는 명령 제출 이후 상태를 계속 소유한다.

음성 입력 상태:

```text
IDLE → RECORDING → TRANSCRIBING → IDLE
                     ↓
              PREVIEW (선택 모드) → IDLE

RECORDING / TRANSCRIBING / PREVIEW → 취소·오류 정리 → IDLE
```

`PREVIEW`는 실행 전 편집·확인 모드에서만 사용한다. 자동 제출 모드에서는 전사 완료 후 기존 runtime으로 명령을 넘긴다. UI는 음성 입력 상태와 명령 실행 상태를 구분해 표시한다.

- 녹음 장치·오디오 버퍼: recorder 소유
- STT 요청·취소·세션 ID: voice controller 소유
- 전역 키 listener: PTT adapter 소유
- MCP/HTTP와 실행 중 명령: 기존 worker/runtime 소유
- Tk widget: 메인 스레드만 접근

기존 runtime의 예약된 음성 enum은 이번 변경에서 제거하지 않는다. 음성 상태의 실제 source of truth는 새 controller 한 곳으로 제한한다.

### 3.2 공통 실행 진입점과 중복 방지

```text
텍스트 UI ───────────────────────┐
                               ├─ 기존 RuntimeController.submit(text)
R-Cmd → Recorder → STT → Voice ──┘
                                     ↓
                          기존 Planner / RiskGuard / MCP
```

worker 내부에 입력 중재 지점을 두고 텍스트·PTT·Undo의 시작 가능 여부를 한 loop에서 판단한다. 별도 실행 정책을 만들지 않고 입력 간 경합만 제어한다.

- 새 녹음은 runtime이 IDLE이고 음성 입력도 IDLE일 때만 시작한다.
- 녹음·전사·미리보기 중에는 텍스트 실행과 Undo를 차단한다.
- 실행·clarification 대기·승인 대기 중에는 PTT를 시작하지 않는다.
- 음성 세션마다 고유 ID를 부여한다. 취소·종료 시 ID를 무효화한다.
- 늦게 도착한 STT 결과는 현재 ID와 다르면 폐기한다.
- 유효한 전사 결과를 소비 처리한 뒤 한 번만 제출한다. 상태 해제와 제출 예약 사이에 다른 입력이 끼어들지 않게 한다.
- 음성으로 인식된 `승인`, `거절`, `undo`를 제어 API로 변환하지 않는다. 제어 명령은 기존 버튼·텍스트 입력을 사용하도록 안내한다.

### 3.3 초기 동작 기본값

다음 값은 초기 구현 기준이며 실제 평가 후 조정한다.

- 언어: 한국어
- 최대 녹음 시간: 30초
- 300ms 미만 입력: 짧은 입력으로 종료하고 STT 호출하지 않음
- 전사 timeout: 60초
- 결과가 빈 문자열 또는 공백뿐이면 명령 제출하지 않음
- 단순 신호 길이·레벨 검사를 우선 적용하고, 무음 판단 기준은 실제 마이크 데이터로 조정
- 녹음은 가능한 경우 메모리에서 보관하고, provider가 파일을 요구할 때만 전용 임시 파일 사용
- 원본 음성·전사 원문은 기본 로그에 저장하지 않음
- 초기 연결 모드는 전사 미리보기, 최종 E2E에서 자동 제출 모드도 검증

## 4. 예상 모듈과 인터페이스

이름은 구현 시 기존 코드 스타일에 맞춰 조정할 수 있다.

| 파일 | 책임 |
|---|---|
| `voice_action/voice_types.py` | 오디오·전사 결과·음성 상태·이벤트 타입 |
| `voice_action/voice_config.py` | STT/녹음/PTT 전용 설정 검증 |
| `voice_action/stt.py` | STT protocol과 첫 provider adapter |
| `voice_action/audio_capture.py` | recorder protocol과 마이크 구현 |
| `voice_action/ptt.py` | R-Cmd 이벤트 감지와 중복 정규화 |
| `voice_action/voice_input.py` | 세션 ID, 상태 전이, 취소, 전사 결과 전달 |
| `voice_stt.py` | STT·PTT 단독 진단 진입점 |
| `voice_action/ui.py` | 음성 표시·버튼·worker 연결 |

주요 계약:

```text
STTClient.transcribe(audio, language) → TranscriptionResult
AudioRecorder.start() → recording handle
AudioRecorder.stop(handle) → AudioClip
AudioRecorder.cancel(handle) → cleanup
PTTListener → pressed / released 이벤트
VoiceInputController → 상태 / 전사 결과 / 오류 이벤트
```

- 전사 결과는 `success`, `empty`, `cancelled`, `error`를 구분한다.
- 일반 오류에는 안정적인 코드와 사용자용 메시지를 사용한다. SDK raw 예외나 인증정보를 UI에 출력하지 않는다.
- 내부 asyncio task 취소는 `CancelledError`로 전파해 정리하고, 사용자 표시 계층에서 취소 결과로 다룬다.
- confidence 등 provider별 정보는 선택 항목이다. 존재하지 않는 confidence를 가정하지 않는다.
- STT의 입력 오디오 형식은 adapter 경계에서 검증한다. sample rate·채널 변환은 한 곳에서만 처리한다.
- 키 callback과 오디오 callback은 가벼운 이벤트·데이터 전달만 하고, blocking STT나 Tk 조작을 수행하지 않는다.

## 5. 단계 0 — 텍스트 MVP 동결

### 구현·작업 항목

1. 실제 Git 저장소 위치를 확인한다. 저장소가 없다면 해당 프로젝트 root에서 초기화하는 작업을 별도로 수행한다.
2. commit 대상과 제외 대상을 검토한다. `config.json`, env, 로그, Undo DB, 녹음 파일, `.venv`, 캐시, `node_modules`, Playwright 산출물을 포함하지 않는다.
3. 현재 dependency lock과 환경 정보를 기록한다.
4. 전체 테스트와 정적 검사를 실행한다.
5. README 명령 표를 기준으로 텍스트 명령 평가표를 작성한다.
6. 실제 provider 평가에는 임시 allowed root, 별도 작업 디렉터리와 journal을 사용한다. 기존 샘플·journal·로그를 재사용하지 않는다.
7. 평가 기록을 포함한 checkpoint commit을 만든 뒤 annotated tag `text-mvp`를 생성한다. 같은 이름의 태그가 있으면 덮어쓰지 않는다.

예정 검증 명령:

```sh
uv run --locked pytest -q
uv run --locked ruff check .
```

### 평가 기록

`docs/text-mvp-baseline.md`와 `docs/command-regression.md`를 구현 단계에서 생성한다.

기록 필드:

- 평가 날짜, Python/Node/OS, dependency lock, 테스트 대상 source commit
- provider/model과 실행 모드, 단 secret 제외
- 명령 ID와 정확한 입력 문장
- 초기 fixture 상태와 기대 도구·승인·파일 변화
- 실제 도구 순서, 최종 상태, 파일 변화, 판정
- 성공·실패·미실행·환경 제약

동결 commit 이전 source commit과 테스트 결과를 기록하고, 최종 checkpoint commit은 태그가 가리키도록 한다. 문서 안에 자기 자신 commit hash를 넣으려 하지 않는다.

### 통과 기준

- 자동 테스트와 정적 검사 통과
- 표의 명령마다 기대 결과가 명시되고 수동 결과가 기록됨
- 실패·미실행이 성공으로 집계되지 않음
- blocker인 텍스트 회귀가 있으면 원인 해결 후 재평가
- `text-mvp`로 동일 코드와 평가 자료를 찾을 수 있음

## 6. 단계 1 — 녹음 파일 STT 단독 구현

### 구현 항목

1. STT/음성 설정과 typed 결과를 정의한다.
2. 동일 한국어 음원으로 사용할 provider 후보를 짧게 평가한 뒤 하나를 선택한다.
3. 선택 시 파일명 정확도, 한국어 인식, 지연, 비용 또는 로컬 자원, 오디오 외부 전송 여부, 설치·권한 요구를 기록한다. 구체 SDK와 버전은 구현 시 공식 문서로 확인한다.
4. 파일 입력 기반 진단 명령을 만든다: `voice_stt.py --file <audio>`.
5. 이 실행 경로가 `load_config()`, `Planner`, `MCPManager`, `UndoJournal`를 생성하지 않게 한다.
6. 빈 오디오·지원하지 않는 형식·크기/길이 초과·timeout·취소를 처리한다.
7. 원문을 바꾸는 후처리는 하지 않는다. 앞뒤 공백 정리 외 의미 변경은 피한다.

### 테스트

- fake STT로 성공·공백·실패·timeout·취소 결과 검증
- 고정 음원의 실제 STT 평가는 선택적 외부 의존 테스트로 분리
- 긴 파일명, 숫자, 하이픈, 공백, 한글·영문 혼합, 확장자 인식 기록
- 무음이 그럴듯한 문장으로 반환되는 사례도 평가
- 실패 다음 입력이 정상 처리되는지 확인
- diagnostic 실행에서 LLM/MCP 초기화가 발생하지 않는지 검증

### 통과 기준

- 입력 음원에서 텍스트 결과만 생성
- STT와 무관한 LLM key나 allowed root 없이 실행 가능
- 빈 입력·취소·오류가 명령 실행으로 이어지지 않음
- provider 선택 근거와 고정 음원별 결과 확보

## 7. 단계 2 — 마이크 녹음과 PTT 단독 구현

### 2A. 마이크 녹음

1. recorder adapter를 구현하고 명시적인 시작·종료 조작으로 먼저 검증한다.
2. 마이크 권한 거부, 장치 없음·분리, 시작 실패를 사용자 메시지로 반환한다.
3. 녹음 길이와 버퍼 크기를 제한한다.
4. 정상 종료·취소·예외·앱 종료에서 장치와 임시 파일을 정리한다.
5. 녹음 결과를 단계 1 STT에 전달하고 화면/터미널에 텍스트만 표시한다.

### 2B. R-Cmd PTT

1. 오른쪽 Cmd 식별이 가능한 macOS 이벤트 adapter를 선택한다.
2. modifier 변화 이벤트에서 오른쪽 Cmd의 이전·현재 상태를 비교해 pressed/released를 생성한다. 왼쪽 Cmd나 다른 modifier 변화를 녹음 종료로 오인하지 않는다.
3. key repeat와 중복 이벤트를 정규화한다.
4. callback을 thread-safe 방식으로 voice controller의 loop에 전달한다.
5. R-Cmd 누름으로 녹음 시작, 뗌으로 stop 후 STT를 한 번 호출한다.
6. 최대 녹음 시간에 도달하면 녹음을 종료하고 해당 세션을 취소한다. 키를 다시 뗐다 누르기 전에는 재시작하지 않는다.
7. 빠른 누름·뗌, 변환 중 재입력, 종료 중 callback, 늦은 STT 응답을 처리한다.
8. 다른 키와 조합된 R-Cmd는 초기 정책상 현재 녹음을 취소한다. 기존 단축키 이벤트의 전달 여부와 실제 충돌을 확인한다.
9. 전역 키 감지와 마이크에 필요한 권한은 선택한 API 기준으로 문서화한다. 기존 활성 앱 조회 권한과 동일하다고 가정하지 않는다.

### 테스트

- 가짜 키 이벤트와 recorder로 상태 전이 검증
- 중복 press/release에 start/stop/STT 호출이 각각 최대 한 번인지 검증
- 취소 후 늦은 완료 이벤트 무시
- timeout 후 다음 요청 성공
- 마이크 start 도중 release가 와도 자원 누수 없이 종료
- 왼쪽 Cmd, 양쪽 Cmd, modifier 조합, Cmd 단축키 동작 수동 확인
- 20회 연속 PTT 후 listener·마이크·task가 남지 않는지 확인

### 통과 기준

- 정상 흐름이 `IDLE → RECORDING → TRANSCRIBING → IDLE`
- 취소·오류 경로에서 자원 정리 후 IDLE 복귀
- 누름·뗌 한 사이클당 전사 최대 한 번
- 단독 실행 동안 LLM/MCP/사용자 파일 작업 없음

## 8. 단계 3 — 기존 runtime과 UI 연결

### 구현 항목

1. 음성 기능은 opt-in으로 추가하고 기존 `--text`, `--interactive`, `--ui` 기본 동작을 유지한다.
2. 초기 통합 대상은 Tk UI다. 예를 들어 `--ui --voice`를 허용하고, `--voice` 단독이나 `--text --voice` 조합은 명확히 거부한다.
3. 기존 worker가 장기 연결된 runtime을 제공하고 voice controller의 결과를 받게 한다.
4. worker의 입력 중재 지점에서 녹음 시작·텍스트 제출·Undo 경합을 검사한다. UI 버튼 비활성화만을 유일한 방어로 사용하지 않는다.
5. 전사 미리보기 영역과 수정 후 실행·다시 녹음·취소 조작을 추가한다.
6. 미리보기 모드 검증 후 자동 제출 옵션을 추가한다. 첫 배포 기본값은 미리보기로 둔다.
7. 전사 성공·비어 있지 않음·현재 세션 ID·입력 가능 상태를 검사한 뒤 기존 `submit()`을 정확히 한 번 호출한다.
8. clarification은 기존 번호 입력, 승인은 기존 버튼, Undo는 기존 버튼으로 처리한다.
9. 음성 오류는 기존 텍스트 기능을 막지 않도록 표시한다. 마이크/PTT 초기화 실패 시 텍스트 UI는 계속 사용 가능해야 한다.
10. 앱 종료 시 새 이벤트 차단 → 음성 세션 무효화 → listener/녹음/STT 정리 → 기존 worker 자원 정리 순서로 종료한다.

### 취소 규칙

| 취소 시점 | 처리 |
|---|---|
| RECORDING | 녹음 폐기·마이크 해제·IDLE |
| TRANSCRIBING | task 취소 또는 결과 무효화·IDLE |
| PREVIEW | 텍스트 폐기·IDLE |
| 명령 제출 이후 | 기존 runtime/UI 실행 취소 동작 적용 |
| 쓰기 승인 대기 | 기존 거절 API 적용 |

로컬 추론이나 외부 요청이 물리적으로 즉시 중단되지 못하더라도, 취소된 결과가 제출되지 않는 것을 보장한다. 중단 불가능한 작업이 누적되지 않도록 동시 STT 작업 수와 재시작 정책도 제한한다.

### 테스트

- 직접 입력과 fake STT로 전달한 동일 텍스트가 같은 runtime 진입점을 사용하는지 검증
- 동일 scripted model로 도구 순서·승인·Undo 결과 비교
- recording/transcribing 중 텍스트·Undo 차단
- planning/clarification/approval 중 PTT 차단
- 전사 완료와 취소가 경합해도 중복·취소 후 제출 없음
- 음성 오류 후 텍스트 명령 및 다음 음성 입력 정상 처리
- 음성 기능 비활성화 시 신규 음성 의존성·권한 없이 기존 모드 사용 가능
- 기존 전체 테스트와 정적 검사 통과

### 통과 기준

- 음성용 Planner·RiskGuard·Undo 정책 복제 없음
- 한 유효 세션에서 명령 제출 최대 한 번
- 쓰기 승인·경로 경계·웹 snapshot 순서 유지
- STT 취소가 창 종료나 MCP 세션 취소로 잘못 이어지지 않음

## 9. 단계 4 — 실제 음성 E2E와 회귀 평가

### 시나리오

| ID | 발화/상황 | 통과 기준 |
|---|---|---|
| V01 | `voice-action-test 폴더 파일 목록 보여줘` | 허용 root 조회 후 결과 표시 |
| V02 | `test1.md를 요약해줘` | 정확한 대상 읽기 후 요약 |
| V03 | `test1.md를 읽고 summary.txt에 저장해줘` | 승인 전 변경 없음, 승인 후 쓰기, Undo 복구 |
| V04 | 후보 여러 개인 `회의록 요약해줘` | 임의 읽기 없이 선택 질문, 기존 입력으로 재개 |
| V05 | 권한 없는 상태의 `현재 활성 앱 알려줘` | 권한 오류 표시, 다음 명령 정상 처리 |
| V06 | 무음·짧은 누름·빈 전사 | 명령 제출 없음 |
| V07 | 녹음/전사 중 취소 | 늦은 결과도 실행되지 않음 |
| V08 | 빠른 연속 PTT·중복 키 이벤트 | 중복 녹음·전사·명령 없음 |
| V09 | 긴 파일명·숫자·확장자 | 전사 원문과 핵심 파일명 정확도 기록, 미리보기 수정 가능 |
| V10 | 승인·clarification 대기 중 PTT | 기존 pending 요청 유지, 새 녹음 없음 |
| V11 | STT 오류 후 재시도 | 자원 정리 후 정상 녹음·실행 |
| V12 | 녹음·전사 중 앱 종료 | 장치·listener·worker 정리, 후속 실행 없음 |

웹 정책은 기존 텍스트 회귀 및 fake STT 통합 테스트로 우선 보존한다. URL을 실제로 말하는 사용성은 별도 후속 평가 대상으로 기록한다.

### 실행 방법

- V01~V05는 미리보기 모드와 자동 제출 모드 각각 5회 평가한다.
- 정상·짧은 입력·취소를 섞은 연속 PTT 20회를 수행한다.
- 파일 작업은 격리 fixture와 별도 journal에서 수행한다.
- 자동화는 fake recorder/PTT/STT, scripted model, 실제 MCP 통합을 조합한다.
- 실제 STT·마이크·OS 권한·외부 모델 평가는 환경 의존 수동 평가로 분리한다.
- 결과는 `docs/voice-evaluation.md`에 기록하고 고정 음원은 동의된 테스트 자료만 관리한다.

### 평가 지표

- STT 문장 정확도와 핵심 대상 문자열 정확도
- 발화 의도 기준 전체 성공률
- 정확히 전사된 입력에 대한 실행 성공률
- R-Cmd release부터 전사 완료까지 지연과 최종 응답까지 지연
- 취소 후 정상 복귀 여부와 반복 사용 실패 건수
- 중복 제출, 취소 후 제출, 승인 없는 쓰기, 경계 밖 작업 건수

잘못 전사된 문장을 실행기가 정상 처리한 경우에도 발화 의도 기준 성공으로 집계하지 않는다. 인식 실패와 실행 실패를 별도로 표시한다.

### 완료 기준

- 텍스트 자동 회귀 테스트·정적 검사 통과
- V01~V05에서 각 모드별 최소 4/5회 발화 의도 기준 성공을 초기 사용성 목표로 삼고, 실패 원인을 모두 기록
- 고정된 정확한 전사 입력의 실행 정책 테스트는 전부 통과
- 승인 없는 쓰기·경계 밖 작업·중복 제출·취소 후 제출 0건
- 연속 PTT 평가에서 복구 불가능한 상태·마이크 점유 잔류 0건
- 실제 provider/OS 권한 검증을 못 한 경우 미검증으로 표시하고 음성 E2E 완료로 선언하지 않음

## 10. 권장 변경 단위와 의존 순서

| 순서 | 변경 단위 | 다음 단계 진입 조건 |
|---|---|---|
| 0 | 텍스트 평가 기록과 `text-mvp` checkpoint | 기준선 확정 |
| 1 | 음성 타입·설정·파일 STT 진단 | STT 단독 통과 |
| 2 | 마이크 recorder와 정리·취소 | 장치 생명주기 통과 |
| 3 | R-Cmd adapter와 voice controller | PTT 단독 통과 |
| 4 | UI 입력 중재와 전사 미리보기 | 텍스트 정책 회귀 통과 |
| 5 | 자동 제출 옵션과 경합 테스트 | 최대 한 번 제출 보장 |
| 6 | 실제 음성 평가·문서 갱신 | E2E 완료 기준 충족 |

각 변경은 독립적으로 검토 가능한 commit으로 나눈다. 음성 입력을 끄면 기존 텍스트 기능을 계속 사용할 수 있도록 유지한다. 회귀가 발생하면 영향을 준 변경 단위에서 수정하고, 기준선 태그를 이동하거나 기존 사용자 데이터를 되돌리지 않는다.

## 11. 구현 전 결정할 항목

- 실제 Git 저장소 위치 또는 새 저장소 초기화 여부
- 첫 STT provider와 실행 방식: 실제 한국어·파일명 음원 평가 후 하나 선택
- provider 자격증명과 STT 전용 설정 항목
- 마이크 및 R-Cmd 이벤트 library/API: 지원 OS, 권한, 종료·취소 특성 확인 후 확정
- 사용할 테스트 음원과 수동 평가 환경

이 항목들은 단계별 시작 시 해결한다. provider가 미정이어도 typed 인터페이스와 fake adapter 기반 상태·취소 테스트는 먼저 구현할 수 있다.

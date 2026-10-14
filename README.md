# Voice Action AI

Voice Action AI는 자연어 명령을 제한된 MCP(Model Context Protocol) 도구 호출로 변환하는 안전 중심의 Python 애플리케이션입니다. 현재 구현은 이름과 달리 **음성 비서가 아니라 텍스트 명령 기반의 Multi-MCP 실행기**입니다. 터미널 단발 실행, 대화형 터미널, macOS Tkinter UI를 제공하며 다음 세 영역을 하나의 플래너에서 다룹니다.

- 허용된 디렉터리 안의 파일 검색·목록 조회·텍스트 읽기·승인 기반 쓰기/일괄 삭제
- 사용자가 명시한 HTTP(S) 페이지의 탐색과 읽기 전용 snapshot
- macOS에서 현재 활성 앱 이름 조회

LLM이 임의의 시스템 권한을 갖는 구조가 아닙니다. 애플리케이션이 노출하는 도구를 고정하고, 모든 파일 경로와 웹 작업을 코드에서 다시 검증하며, 파일 쓰기는 사용자 승인과 Undo journal이 모두 준비된 경우에만 실행합니다.

## 현재 구현 상태

2026-10-07 기준 검증 결과 (`uv run --locked pytest -q`, UI 테스트 포함):

```text
pytest: 494 passed
ruff:   All checks passed!
```

전체 테스트는 실제 Filesystem MCP 서버와 실제 Playwright MCP/Chromium subprocess를 포함합니다. 웹 통합 테스트는 외부 인터넷 대신 로컬 HTTP fixture를 사용하며, LLM 응답은 재현 가능한 scripted/mock model을 사용합니다. 따라서 다음은 검증되었습니다.

- 실제 MCP 프로세스 시작, 도구 발견, 호출, 종료 및 누수 방지
- 실제 파일 목록 조회, 검색, 읽기, 새 파일 쓰기와 overwrite
- 허용 경로 밖 접근과 symlink 탈출 차단
- 실제 Chromium으로 navigation 후 snapshot 읽기
- 웹 페이지 읽기 → 요약 생성 → 사용자 승인 → 파일 쓰기 → Undo 전체 흐름
- CLI와 UI worker의 정상 종료, 오류 종료, 취소 처리
- 승인 전 쓰기 차단, 승인 거절, 1회성 승인, 변경된 대상의 재승인
- SQLite WAL journal을 이용한 create/overwrite Undo와 충돌 감지
- 도구 결과 크기 제한, 예외·비밀정보 노출 방지

다만 테스트 스위트가 실제 Groq/OpenAI 계정이나 외부 웹사이트의 가용성을 보장하는 것은 아닙니다. 실제 API 호출은 사용자의 키, 네트워크, 선택한 모델의 tool-calling 품질에 영향을 받습니다. macOS Accessibility API도 테스트에서는 adapter를 대체하므로, 실제 권한 설정은 별도로 필요합니다.

## 가능한 명령

아래 표의 “가능”은 애플리케이션 정책과 테스트로 확인된 범위입니다. 자연어 문장은 예시이며 모델에 따라 표현은 달라질 수 있습니다.

| 작업 | 예시 | 동작과 조건 |
|---|---|---|
| 디렉터리 목록 | `voice-action-test 폴더 파일 목록 보여줘` | 허용된 root 안에서 자동 실행 |
| 파일 검색 | `회의록 파일을 찾아줘` | `search_files`를 사용하고 결과 경로를 다시 검증 |
| 텍스트 파일 읽기 | `test1.md 내용을 읽어줘` | 허용 root 안의 기존 파일만 가능 |
| 파일 내용 요약 | `test1.md를 요약해줘` | 파일을 읽은 뒤 같은 LLM이 최종 답변 생성 |
| 모호한 회의록 선택 | `회의록 요약해줘` | 후보 1개면 자동 진행, 여러 개면 번호 선택 요청, 0개면 안전 종료 |
| 새 텍스트 파일 작성 | `result.txt에 요약을 저장해줘` | 정확한 `write_file` 호출마다 승인 필요 |
| 기존 텍스트 파일 덮어쓰기 | `result.txt 내용을 ...로 바꿔줘` | overwrite로 표시하고 승인 후 실행 |
| 파일 삭제 | `voice-action-test/result.txt 파일을 삭제해줘` | 정확한 파일 경로를 표시하고 승인·백업 후 삭제 |
| 일괄 삭제 | `voice-action-test 폴더의 *.txt 파일을 모두 삭제해줘` | 바로 아래 파일 목록 전체를 표시하고 한 번 승인 후 삭제 |
| 웹 페이지 읽기 | `https://example.com 페이지를 읽고 요약해줘` | URL이 사용자 원문에 정확히 있어야 하며 navigation 뒤 snapshot 필수 |
| 웹 내용을 파일로 저장 | `https://example.com을 읽고 summary.md에 저장해줘` | URL과 출력 파일명이 원문에 있어야 하며 snapshot 성공 후 승인 필요 |
| 현재 활성 앱 조회 | `현재 활성 앱을 알려줘` | macOS에서만 제공, Accessibility 권한 필요 |
| 최근 쓰기/삭제 Undo | 대화형 모드에서 `undo` 또는 `되돌리기` | 최근 쓰기 하나 또는 삭제 batch 전체 복원 |

읽기 전용 도구는 자동 실행됩니다. 파일 쓰기와 삭제는 항상 별도 승인이 필요합니다.

삭제는 앱 내부 `local__delete_files` 도구로 실행합니다. 단일 파일, 명시한 여러 파일,
날짜·회의록·보고서·메모·예산안 단서로 찾은 뒤 사용자가 선택한 파일 하나,
또는 폴더와 glob 패턴(`*.txt`, `*`)을 지원합니다. 승인 화면에는 생략 없는 전체 대상 목록이
표시되고, 그 목록이 고정됩니다. 승인 후 추가된 파일은 삭제하지 않습니다. 기존 대상의 내용이나
식별 정보가 바뀌면 재승인을 요청하거나 삭제를 중단합니다. 한 번에 최대 100개, 백업 합계
50 MiB이며 바이너리 파일도 백업할 수 있습니다. 폴더 자체와 심볼릭 링크, 재귀 삭제는 거부합니다.
삭제 도중 일부 실패하면 삭제 개수를 표시하며, 이미 삭제된 파일은 Undo로 복원할 수 있습니다.
파일 내용과 권한을 복원하며 ACL·확장 속성·원래 시각은 복원하지 않습니다.
외부 프로그램이 검사와 unlink 사이에 대상을 바꾸는 경쟁을 완전히 막는 기능은 제공하지 않습니다.

UI를 재실행한 뒤 입력 예시:

```text
voice-action-test/a.txt와 voice-action-test/b.txt 파일을 삭제해줘
voice-action-test 폴더의 *.txt 파일을 모두 삭제해줘
voice-action-test 폴더 바로 아래의 모든 파일을 삭제해줘
```

상단 `승인` 버튼으로 실행하며 `거절`/승인 대기 중 `취소`는 파일을 변경하지 않습니다.
`Undo` 버튼 한 번으로 최근 삭제 batch를 복원합니다. 실행 중 취소한 경우 진행 중인 삭제
batch와 journal 기록이 끝나기를 기다린 뒤 창을 닫습니다. 재실행 후 Undo로 복원할 수 있습니다.

### 모드별 쓰기/삭제 동작

| 실행 모드 | 쓰기 요청 처리 |
|---|---|
| `--text` | 승인 정보를 출력하고 종료합니다. 실제 파일을 쓰거나 삭제하지 않습니다. |
| `--interactive` | `승인`, `approve`, `yes`, `y` 중 하나를 입력해야 실행합니다. |
| `--ui` | 표시된 대상·작업·미리보기를 확인하고 `승인` 버튼을 눌러야 실행합니다. |

거절 토큰은 `거절`, `reject`, `no`, `n`, `취소`, `cancel`, `c`입니다. 대소문자와 앞뒤 공백은 정규화하지만, 빈 입력이나 알 수 없는 문자열은 승인으로 취급하지 않습니다.

## 불가능하거나 의도적으로 지원하지 않는 명령

다음 작업은 모델이 요청하더라도 애플리케이션 정책에서 차단되거나 도구 자체가 노출되지 않습니다.

- 디렉터리 삭제, 재귀 파일 삭제, 조건이나 대상을 지정하지 않은 삭제
- 파일 이동, 이름 변경, 휴지통 이동
- 디렉터리 생성
- 여러 파일을 하나의 원자적 transaction으로 수정
- Undo 취소(redo), Undo 이력 조회, 임의 시점 복구
- 브라우저 클릭, 입력, 로그인, 제출, 다운로드 등 상호작용
- 사용자 원문에 없는 URL로 이동
- `file:`, `ftp:` 등 HTTP(S)가 아닌 URL 탐색
- 운영 모드에서 localhost, loopback IP, link-local IP 탐색
- URL에 사용자명이나 비밀번호가 포함된 주소 탐색
- 허용 root 밖의 파일 접근
- symlink를 이용한 허용 경계 우회
- 웹 작업이 실패했거나 snapshot을 읽기 전에 파일 쓰기
- 웹 명령에서 사용자가 명시하지 않은 파일명으로 저장
- 이미지·오디오·일반 바이너리 파일의 내용 처리
- macOS 앱 클릭, 키보드 입력, 실행, 창 제어, AX tree 읽기
- 병렬 도구 실행, 자동 재시도, MCP 자동 재연결
- provider fallback 또는 한 명령 안에서 여러 모델 사용
- 마이크 입력, STT, 녹음, 전역 단축키, 메뉴바 앱, 알림

`RECORDING`과 `TRANSCRIBING` 상태는 향후 음성 기능을 위한 자리만 마련되어 있으며 현재 사용되지 않습니다.

## 요구 사항

- Python `>=3.12,<3.13`
- [uv](https://docs.astral.sh/uv/)
- Node.js와 npm
- Playwright Chromium
- OpenRouter, Groq 또는 OpenAI API key
- macOS 활성 앱 조회를 사용할 경우 Accessibility 권한

Python 주요 의존성은 `mcp`, `openai`, `anyio`, `jsonschema`, `rich`, `python-dotenv`입니다. Node 의존성은 다음 버전으로 고정되어 있습니다.

- `@modelcontextprotocol/server-filesystem` `2026.8.31`
- `@playwright/mcp` `0.0.81`

## 설치

저장소 root에서 실행합니다.

```sh
uv sync --locked
npm ci
npm run playwright:install
```

기본 provider는 `GROQ_API_KEY`가 있으면 Groq, 없으면 OpenAI입니다.

```sh
export GROQ_API_KEY="..."
```

또는 다음 파일 중 하나에 저장할 수 있습니다.

```text
./.voice-action/config.env
~/.voice-action/config.env
```

예시:

```dotenv
GROQ_API_KEY=...
```

OpenAI를 사용할 때는 provider를 `openai`로 지정하고 `OPENAI_API_KEY`를 설정합니다. API key는 JSON 설정에 넣지 마십시오. 로컬 `config.json`에 key 계열 필드가 있으면 설정 로딩 자체가 거부됩니다. 과거 전역 JSON에 남아 있는 key 필드는 읽지 않고 무시합니다.

## 빠른 시작

### 단발 명령

```sh
uv run python main.py --text "voice-action-test 폴더 파일 목록 보여줘"
```

단발 모드는 읽기 작업에 적합합니다. 쓰기가 필요하면 승인 정보를 표시하지만 입력을 기다리지 않고 종료합니다.

### 대화형 터미널

```sh
uv run python main.py --interactive
```

대화형 모드는 한 번 연결한 MCP 프로세스와 HTTP client를 여러 명령에서 재사용합니다.

```text
> test1.md를 읽고 summary.txt에 요약해줘
[APPROVAL REQUIRED]
...
> 승인
[DONE] ...
> undo
[DONE] 생성한 파일을 되돌렸습니다.
> exit
```

`exit`, `quit`, EOF는 정상 종료하고 Ctrl+C는 종료 코드 130으로 끝납니다.

### Tkinter UI

```sh
uv run python main.py --ui
```

UI는 다음을 표시합니다.

- 현재 runtime 상태
- 실행 중인 namespaced 도구 이름
- 최종 결과 또는 오류
- clarification 선택지(버튼·숫자 키)와 답변 평가 버튼(1~5, 숫자 키)
- 쓰기 대상, create/overwrite 여부, 문자 수, 최대 200자의 미리보기
- 승인·거절·취소·Undo 제어

Tk main thread와 별도의 non-daemon worker thread가 동작합니다. worker가 하나의 asyncio loop, MCP 세션들, HTTP client를 소유합니다. 실행 중 취소하면 active command를 취소하고 관련 자원을 정리한 뒤 창을 닫습니다. 승인 대기 중 취소는 거절로 처리되어 파일을 쓰지 않습니다.

## 명령행 옵션

세 실행 모드 중 정확히 하나가 필요합니다.

```text
--text TEXT
--interactive
--ui
```

공통 override:

```text
--provider {openai,groq,openrouter}
--model MODEL
--base-url URL
--allowed-directory PATH   # 반복 가능
```

예시:

```sh
uv run python main.py \
  --interactive \
  --provider openai \
  --model gpt-4o-mini \
  --allowed-directory /absolute/path/to/documents
```

허용 디렉터리는 실행 시 존재하는 실제 디렉터리여야 합니다. 상대경로는 현재 작업 디렉터리를 기준으로 해석되므로 일반적으로 저장소 root에서 실행해야 합니다.

## 설정

기본 `config.example.json`:

```json
{
  "provider": "groq",
  "model": "openai/gpt-oss-20b",
  "allowed_directories": ["./voice-action-test"],
  "max_tool_rounds": 10,
  "llm_timeout_seconds": 60,
  "mcp_timeout_seconds": 30,
  "max_tool_result_chars": 50000
}
```

설정 우선순위는 높은 순서대로 다음과 같습니다.

1. CLI 옵션
2. `VOICE_ACTION_*` 환경변수
3. 현재 디렉터리의 `config.json`
4. `~/.voice-action/config.json`
5. 기본값

지원하는 환경변수:

```text
VOICE_ACTION_PROVIDER
VOICE_ACTION_MODEL
VOICE_ACTION_BASE_URL
VOICE_ACTION_ALLOWED_DIRECTORIES   # JSON 배열
VOICE_ACTION_MAX_TOOL_ROUNDS
VOICE_ACTION_LLM_TIMEOUT_SECONDS
VOICE_ACTION_MCP_TIMEOUT_SECONDS
VOICE_ACTION_MAX_TOOL_RESULT_CHARS
VOICE_ACTION_FINDER_REVEAL         # on(기본) | off, 환경변수로만 설정
VOICE_ACTION_FINDER_DOCK           # on(기본) | off, Finder 옆 패널 자동 배치
```

provider별 기본 모델:

- OpenRouter: `anthropic/claude-sonnet-5.5` (키 `OPENROUTER_API_KEY`, base URL `https://openrouter.ai/api/v1`)
- Groq: `openai/gpt-oss-20b` (키 `GROQ_API_KEY`, base URL `https://api.groq.com/openai/v1`)
- OpenAI: `gpt-4o-mini` (키 `OPENAI_API_KEY`)

OpenRouter를 쓰려면 `config.json`에 `"provider": "openrouter"`를 지정합니다. CLI에서
`--provider`만 바꾸면 `config.json`의 `model`·`base_url`이 그대로 남으므로, 바꿀 때는
`--provider openrouter --model anthropic/claude-sonnet-5.5 --base-url https://openrouter.ai/api/v1`처럼
함께 지정하세요. 모델은 OpenRouter의 도구 호출 지원 모델 ID로 바꿀 수 있습니다. 한 명령에서는 하나의 provider와 하나의 모델만 사용합니다.

## 자기 피드백 루프

기본으로 켜져 있습니다. 명령을 처리하는 동안 사용자 입력, 메인 LLM 요청·응답, 상태 전이, 결과를 `.voice-action/feedback.sqlite3`(권한 600)에 기록하고, 명령이 끝날 때마다 아래 규칙으로 문제를 감지합니다.

기록 정책:

- 모든 문자열은 저장 전에 API 키·`Bearer` 토큰을 가린 뒤 자르고 직렬화합니다. signature도 같습니다.
- 도구 결과(파일·웹 본문), system 프롬프트, 쓰기 `content`는 길이와 해시만 저장합니다. 도구 인자는 200자 이하 값만 남깁니다.
- 사용자 입력은 4,000자, assistant 응답과 결과 문장은 앞 300자만 저장합니다. 응답이 파일 내용을 다시 인용하면 그 앞부분은 남을 수 있습니다.
- event는 14일 보관하며, 오래된 기록은 다음 실행 시작 때 지웁니다. 이미 저장된 예전 형식의 기록은 자동으로 다시 정제되지 않으니, 필요하면 DB 파일을 직접 지우세요.
- DB가 다른 프로세스에 잠겨 있으면 50ms만 기다리고 해당 기록을 건너뜁니다. 피드백 오류는 명령 실행을 막지 않습니다.
- DB·report·`.voice-action` 폴더가 symlink면 피드백을 끕니다.

| 규칙 | 조건 |
| --- | --- |
| R1 | 같은 오류가 24시간 안에 서로 다른 실행 3회 |
| R2 | 한 실행에서 같은 도구·인자 호출 2회 이상 |
| R3 | 10분 안에 비슷한 명령 3회 |
| R4 | 쓰기 거절, 또는 승인 후 5분 안에 undo |
| R5 | 직전 명령 10분 안의 정정·지시 ("아니…", "다시…", "왜…", "…말고", "다음부턴…", "앞으로는…", "제대로") |
| R6 | clarification 최대 횟수 초과 |
| R7 | 답변에 낮은 점수(1~3)를 줬고, 같은 원인 추정이 7일 안에 서로 다른 실행 2회 |
| R8 | 한 실행의 모델 호출이 6회 이상 (호출 한도 10회 안에서 낭비 포착) |
| R9 | 한 실행에서 검색 도구 호출이 4회 이상 |

### 답변 평가 (1~5점)

답변(Completed)이 나오면 UI에 `이 답변은 어땠나요?` 버튼 1~5가 뜹니다. 입력창이 비어 있을 때 숫자 키 1~5로도 누를 수 있습니다(타이핑 중인 숫자는 가로채지 않음). 4~5점은 저장되면 평가 영역이 사라집니다. 1~3점은 원인 칩(`엉뚱한 파일`·`내용 부족`·`틀린 내용`·`너무 김`·`너무 느림`·`기타`)이 나타나며, 이유 하나를 선택해 저장하면 평가 영역이 사라집니다. 저장하지 못하면 안내를 표시하고 다시 선택할 수 있게 열어 둡니다. 취소·거절·Undo 결과에는 평가 막대가 뜨지 않습니다. 대화형 터미널에서는 `rate 2 wrong_file too_slow`처럼 여러 원인을 지정하거나 다시 평가할 수 있습니다. 다음 명령을 입력하면 이전 답변은 더 평가할 수 없습니다.

- 점수와 칩만 저장합니다(`rating` event). 자유 입력은 받지 않습니다.
- 원인은 먼저 그 실행의 기록으로 **코드가 추정**합니다(`search_failed`, `search_incomplete`, `partial_read`, `tool_errors`, `many_calls`, `slow`, `clarified`, 마지막으로 `search_content_stage`). 모델 호출 없음. `slow`는 모델 응답 시간만 잰 값입니다(도구 시간은 측정하지 않음). 여러 원인 칩은 가장 앞 이름 하나로 signature를 만들고, 칩과 모든 추정은 근거에 함께 남습니다.
- 각 실행은 **마지막 평가**만 셉니다(점수를 올리거나 칩을 바꾸면 이전 값은 빠짐). 같은 추정(signature)으로 낮은 점수가 서로 다른 실행 2회가 되면 그때 R7로 OpenRouter 분석을 요청하고, 결과는 다른 규칙과 같이 lesson **후보**로만 남습니다. 점수만으로 프롬프트가 바뀌지 않습니다.

감지되면 백그라운드에서 OpenRouter 모델이 그 사건의 근거 run만 보고 원인을 분석해 다음 중 하나를 적용합니다. 메인 LLM 설정과 안전 규칙(RiskGuard, 승인, 허용 경로)은 바꾸지 않습니다.

- lesson: 분석 결과는 **후보**로만 저장됩니다. 사람이 평가(`evals/run_eval.py --live`)로 비교한 뒤 `--lesson-on ID`로 채택하면 다음 명령부터 system 프롬프트 끝에 힌트로 추가합니다. 명령이 시작될 때 고정되어, 실행 중 끝난 분석은 그 명령에 끼어들지 않습니다. 최대 5개이며, 그 lesson을 실제로 받은 서로 다른 3개 명령에서 같은 문제가 재발하면 자동 폐기됩니다.
- user_hint: `[FEEDBACK] …` 한 줄 출력
- dev_report: `.voice-action/feedback-report.md`(권한 600)에 기록, 코드 수정은 사람이 판단

URL·경로·승인 관련 단어가 든 lesson과 credential이 섞인 분석 결과는 채택하지 않습니다. 이 키워드 필터는 보조 장치이지 보안 경계가 아니며, 실제 안전은 코드의 RiskGuard·URL·경로 검증이 보장합니다.

설정은 `.voice-action/config.env` 또는 환경변수에서만 읽습니다(`config.json`의 값은 쓰지 않음).

```
OPENROUTER_API_KEY=...                     # 없으면 기록·감지만 하고 분석은 생략
VOICE_ACTION_FEEDBACK=on                   # off면 전부 끔
VOICE_ACTION_FEEDBACK_MODEL=anthropic/claude-sonnet-5.5  # OpenRouter의 아무 모델 ID
VOICE_ACTION_FEEDBACK_DAILY_LIMIT=20       # 최근 24시간 분석 요청 한도 (취소된 요청 포함)
```

종료 시 진행 중인 분석은 CLI에서 최대 15초, UI에서 최대 5초 기다린 뒤 취소합니다. 취소해도 이미 보낸 요청은 한도에 포함됩니다.

lesson 관리: `uv run python main.py --lessons`(활성·후보 목록), `--lesson-on ID`(후보 채택), `--lesson-off ID`(비활성화·후보 거절). 채택 절차는 `docs/self-improvement-guide.md` 5.2절.

## 노출되는 도구

모델은 startup 시 발견되고 애플리케이션 allowlist를 통과한 다음 도구만 볼 수 있습니다.

### Filesystem

```text
filesystem__list_directory
filesystem__search_files
filesystem__read_text_file
filesystem__write_file
```

### Web

```text
web__browser_navigate
web__browser_snapshot
```

### macOS

```text
macos__get_active_app
```

MCP server가 추가 도구를 제공하더라도 자동으로 노출되지 않습니다. `delete_file`, `move_file`, Playwright action 도구 등은 발견되더라도 제외됩니다. 각 이름은 `namespace__tool` 형식이며 `MCPManager`가 원래 client로 순차 dispatch합니다.

`local__delete_files`는 MCP server에 보내지 않는 앱 내부 도구이며 명시적인 삭제 요청에만
모델에 노출됩니다. RiskGuard와 Undo journal이 경로 검증, 승인, 백업, 로컬 삭제를 처리합니다.

### 단계적 파일 검색 (앱 내부 도구)

```text
local__find_files    웹 요청이 아니면 항상 노출, 읽기 전용
```

파일명이 불분명한 요청(`예산안 내용 알려줘`, `팀 미팅 노트 읽어줘`)에서 모델은 검색어(한/영 변형 포함)만 넘기고, 검색은 앱이 합니다.

| 단계 | 방법 |
| --- | --- |
| 파일명 | NFC·대소문자·구분자(`-`, `_`, 공백) 정규화 후 부분 일치. 숫자는 더 긴 숫자의 일부로 매칭하지 않음 |
| 폴더 경로 | 하위 폴더 이름에서 일치 |
| 내용 | 텍스트 확장자(txt·md·csv·tsv·json·rst·log·yaml)의 앞 64 KiB만 읽어 점수화 |

- 앞 단계에서 후보가 나오면 다음 단계로 가지 않습니다. 단계 확장은 코드가 하며 clarification 횟수를 쓰지 않습니다.
- 허용 폴더는 디렉터리 fd를 기준으로 한 요소씩 `O_NOFOLLOW`로 열어 순회·읽습니다. 순회 도중 폴더가 링크로 바뀌거나 허용 폴더 자체가 링크가 되면 그 경로는 검색하지 않습니다. 숨김 폴더·`node_modules`·symlink는 건너뜁니다.
- 상한: 깊이 8, 방문 20,000개(한 폴더도 이 수까지만 목록으로 읽음), 순회 10초, 내용 검사 300개·5초. 걸리면 결과에 `complete: false`와 이유가 붙습니다.
- `complete: false`가 되는 경우: 위 상한, 앞 64 KiB 밖은 보지 않은 긴 텍스트 파일, 내용을 검사하지 않은 pdf·docx 등(이름·폴더만 봄), 읽지 못한 파일. 이때 "없음"은 단정하지 않고(`note`로 모델에 알림), 내용 검사에서 하나만 걸린 경우도 `single` 대신 선택 버튼으로 확인받습니다.
- `extensions`·`folder_hint`·날짜 필터는 앱이 자동으로 풀지 않습니다. 조건 때문에 결과가 없으면 그 사실을 알리고, 사용자가 말하지 않은 추측 조건이면 모델이 빼고 다시 호출합니다. `modified_after`는 그날 00:00부터, `modified_before`는 그날 끝까지 포함합니다. `folder_hint`에 허용 폴더 아래 절대경로를 주면 상대 폴더로 바꿔 쓰고, 폴더 이름은 허용 폴더 이름부터(`root/하위/자료`) 맞춥니다.
- 파일 내용은 모델에 전달하지 않습니다. 고른 파일을 읽을 때만 기존 읽기 도구를 씁니다.
- 검색 결과가 `single`이고 요청이 내용에 관한 것이면(읽기·요약·변환 등) 그 파일만 읽을 수 있고, 읽지 않고는 완료할 수 없습니다. "찾아줘·어디 있어"처럼 위치만 묻는 요청은 읽지 않아도 됩니다. 삭제·변환은 대상 파일이 이미 정해져 있어 미리 읽기를 요구하지 않습니다.
- 회의록 요약도 같은 `local__find_files`를 씁니다(회의록 전용 검색은 없음). 첫 호출의 keywords에 `회의록`이 있어야 하고, 영문 이름(`meeting minutes`)과 본문만 일치하는 파일도 찾을 수 있으며, 읽기 전용(쓰기·삭제·변환 불가) 보호는 그대로입니다. 원시 `filesystem__search_files`는 모델에 노출하지 않습니다.
- `content_keywords`(본문에 들어 있어야 할 말)는 이름·폴더로 찾은 후보 **안에서만** 본문(앞 64 KiB)을 확인해 좁힙니다. 이름 대체어(`keywords`)와 달리 AND처럼 동작하고, 텍스트가 아닌 후보(pdf 등)는 확인하지 못해 제외하고 그 사실을 알립니다.
- 질문이 여러 번 이어져도 폴더·형식 같은 조건은 누적해서 다시 검색하고, 질문 2회를 넘으면 지금까지의 단서와 함께 구체적인 조건을 넣어 다시 요청하라고 안내하며 끝냅니다.
- 큰 파일: 결과 한도(`max_tool_result_chars`, 기본 50,000자)의 4배 바이트보다 큰 파일은 파일 서버를 거치지 않고 앱이 앞부분만 읽습니다(링크·허용 폴더 밖은 서버가 처리). 일부만 읽었으면 코드가 답변 끝에 `※ 파일 전체가 아니라 앞부분 약 N자만 읽고 작성한 답변입니다.`(또는 `앞 N줄`·`뒤 N줄`)를 붙입니다. `head`/`tail`로 읽은 줄 수가 요청보다 적으면 전부 읽은 것으로 봅니다.

### 파일 변환과 문서 읽기 (앱 내부 도구, macOS)

```text
local__convert_file     변환 요청("pdf로 바꿔줘", "jpg로 변환")일 때만 노출, 승인 필요
local__read_document    웹 요청이 아니면 항상 노출, 읽기 전용
```

| 변환 | 변환기 |
| --- | --- |
| txt·md·rtf·html·doc·docx·odt → txt·rtf·html·doc·docx·odt | `textutil` |
| txt·md → pdf (서식 없는 글) | `cupsfilter` |
| pdf → txt | PDFKit (pyobjc) |
| png·jpeg(jpg)·heic·tiff·gif·bmp 서로 (아이폰 HEIC → JPG·PNG 포함) | `sips` |
| csv ↔ json | Python 표준 라이브러리 |

- 원본은 바꾸지 않고 새 파일을 만듭니다. 기본은 같은 폴더이고 같은 이름이 있으면 `이름 (1).jpg`처럼 새 이름을 씁니다. 출력 경로를 직접 지정하면 다른 허용 폴더도 되지만, 이미 있는 파일이면 거부합니다.
- 승인 전에 임시 폴더에서 실제로 변환해 결과 크기와 미리보기(글 형식)를 승인 패널에 보여줍니다.
- 승인하면 Undo journal에 새 파일 생성으로 기록한 뒤 파일을 만들고, `최근 변경 되돌리기`로 지울 수 있습니다.
- 승인 대기 중 원본이 바뀌거나 출력 이름이 생기면 다시 변환해 새로 승인받습니다. 실행 직전에 원본·출력 경로가 여전히 허용 폴더 안인지(폴더가 링크로 바뀌지 않았는지) 다시 검사하고, 파일을 쓰는 함수도 같은 검사를 합니다.
- 실행 중 취소돼도 쓰기를 끝까지 기다린 뒤 Undo 기록을 확정하므로, 생긴 파일은 되돌릴 수 있습니다. 쓰다가 실패하면 만들던 파일을 지웁니다.
- CSV→JSON은 헤더가 있는 표만 받습니다. 빈·중복 헤더, 열 수가 다른 행, 깨진 따옴표, UTF-8이 아닌 파일은 값이 사라지지 않도록 거부합니다. 값은 모두 문자열이 됩니다. JSON→CSV는 객체 배열만 받으며 null은 빈 칸, 숫자·불리언은 JSON 표기(`1.5`, `true`)로 씁니다.
- 결과는 변환기 종류와 관계없이 비어 있거나 50 MiB를 넘으면 거부합니다. 외부 변환기는 60초 제한이 있지만, PDF 글자 읽기(PDFKit)는 앱 안에서 실행되어 시간 제한이 없습니다.
- 원본은 허용 폴더 안의 일반 파일(링크 불가), 최대 20 MiB이고, 확장자와 실제 형식이 다르면 거부합니다.
- 서식 있는 문서의 PDF 변환, 엑셀, 이미지 크기 조절, 스캔 PDF 글자 인식은 지원하지 않습니다.
- `local__read_document`는 pdf·docx·doc·rtf·odt·html의 글자를 읽어 모델에 돌려주며 파일을 만들지 않습니다. 회의록처럼 후보를 찾아 고르는 요청에서는 고른 파일만 읽을 수 있습니다.
- 모델이 그 단계에 제공되지 않은 도구를 요청하면 실행하지 않고 오류로 돌려줍니다. 웹 요청에서는 변환 도구를 쓸 수 없습니다.

## 직전 명령 기억

앱은 **바로 앞의 명령 한 건**(요청, 답변 앞 300자, 읽거나 바꾼 파일)을 메모리에만 두고, 10분 안의 다음 새 명령에 "직전 대화"로 모델에 줍니다. `승인했던 그 파일 요약해줘`, `그 요약을 요약.txt로 저장해줘`, `내 말은 파일로 저장해 달라는 거였어`처럼 대상을 생략한 후속 요청을 위한 것입니다.

- 모델에는 참고용 데이터이며 지시가 아니라고 알립니다. 승인·허용 경로·삭제 대상 지정 규칙은 기억과 무관하게 그대로입니다(`그 파일 삭제해줘`는 파일명이 없으면 거절).
- 오류·취소·거절·Undo 결과, 질문(clarification)은 기억하지 않습니다. 질문에 답하는 것은 새 명령이 아니라 기억을 바꾸지 않습니다.
- 디스크에 저장하지 않고 앱을 닫으면 사라집니다. 대상이 요청에도 직전 대화에도 없으면 아무 파일이나 고르지 않고 먼저 찾거나 묻습니다.
- 같은 후속 요청 과제를 기억 없이 돌렸을 때 0/4(모델이 되묻다 끝남), 있을 때 6/6이었습니다.

## Finder 연동 (macOS)

파일 작업을 Finder와 이어 줍니다. 모든 작업은 허용 폴더 안에서만 일어납니다.

**파일 첨부(드래그).** `--ui` 창 어디에나 Finder의 파일 하나를 끌어다 놓으면 `📎 파일명`이 표시되고, **다음 명령 하나**의 대상이 됩니다. 놓는다고 실행되지는 않습니다.

- 허용 폴더 밖의 파일, 링크, 폴더, 없는 파일은 첨부하지 않고 이유와 함께 **요청을 거부**합니다. 파일을 둘 이상 놓아도 거부합니다. 앞서 첨부한 파일은 유지됩니다.
- 경로는 놓을 때와 명령을 실행하기 직전에 두 번 확인합니다. 그 사이 링크로 바뀌면 모델을 부르기 전에 거부합니다.
- 첨부하면 그 파일이 곧 대상이라 `local__find_files`를 쓰지 않고, 그 파일만 읽을 수 있으며, 내용에 관한 요청은 읽기 전에 끝낼 수 없습니다. 삭제·변환은 첨부한 파일 하나로 제한되고(`이 파일 삭제해줘`처럼 파일명을 적지 않아도 됨) 승인·Undo는 그대로입니다.
- 질문(clarification)에 답하는 동안에는 첨부가 유지되고, 새 명령을 보내면 비워집니다. `✕ 해제`로 직접 지울 수도 있습니다.
- 터미널에서는 `attach <경로>`(공백이 있으면 따옴표)와 `detach`가 같은 일을 합니다.
- 드래그에는 `tkinterdnd2`를 씁니다. 없거나 초기화에 실패하면 드롭 없이 창이 그대로 열립니다.

**Finder에서 결과 보기.** 파일을 만들거나 바꾸는 작업(텍스트 쓰기, 변환)이 **성공**하면 그 파일을 Finder에서 선택한 채로 보여 줍니다. 경로는 모델의 답변이 아니라 앱이 실제로 쓴 경로(`Completed.changed_paths`)이고, 표시 직전에 허용 폴더 안인지·링크가 아닌지·존재하는지 다시 확인합니다. 승인 대기·거절·실패·삭제·Undo에서는 열지 않습니다. Finder를 열지 못해도 작업 결과는 그대로입니다. `VOICE_ACTION_FINDER_REVEAL=off`로 끕니다(기본 on).

**Finder 옆의 작은 대화 패널.** `--ui`는 360×560pt 창(최소 360×480)으로 열립니다.
macOS 기본 버튼과 밝은/어두운 시스템 색상을 사용합니다. 승인 목록은 작은 창에서도
전체 내용을 스크롤로 확인하며, 파일 선택과 평가 원인 버튼은 세로로 배치됩니다.

앱 실행 중 Finder가 활성화되면 0.5초 간격으로 현재 Finder 창을 확인하고 같은 화면의
오른쪽(공간이 부족하면 왼쪽)에 패널을 놓습니다. Finder 창은 이동하지 않습니다.
다른 앱으로 전환하면 대기 중이고 입력창이 빈 패널을 숨기고, Finder로 돌아오면 다시 표시합니다.
실행·질문·승인 대기 중에는 숨기지 않습니다. 표시 과정에서 키보드 포커스를 강제로 가져오지 않습니다.
공간 부족·권한 없음·창 정보 조회 실패 시에는 독립 창으로 사용할 수 있습니다.
상단 `−`는 최소화이며 제목 막대 닫기는 앱을 종료합니다.

- 자동 배치는 손쉬운 사용(Accessibility) 권한이 필요합니다. 시스템 설정 > 개인정보 보호 및 보안 >
  손쉬운 사용에서 실행한 터미널 또는 Python을 허용하세요. 권한이 없으면 안내를 한 번 표시합니다.
- `VOICE_ACTION_FINDER_DOCK=off`로 자동 배치·숨김을 끕니다. Finder 결과 표시는
  `VOICE_ACTION_FINDER_REVEAL=off`로 별도로 끕니다.
- 현재 Finder 폴더에 따라 파일 접근 권한이 바뀌지는 않습니다. `allowed_directories`가 그대로 적용됩니다.
- 앱이 실행되어 있어야 자동 표시됩니다. 로그인 자동 실행은 설정하지 않습니다.

## 처리 흐름

```text
사용자 입력
  ↓
RuntimeController — 단일 실행과 상태 전이 관리
  ↓
Planner — LLM 호출, clarification, 최대 5회 tool loop
  ↓
RiskGuard — read/write/forbidden 고정 분류
  ↓
MCPManager — namespace별 client dispatch
  ├─ Filesystem MCP
  ├─ Playwright MCP
  └─ macOS in-process client
```

활성 명령의 주요 상태:

```text
IDLE
  → PLANNING
  → EXECUTING_TOOL ↔ PLANNING
  → WAITING_USER_INPUT → RESUMING_PLAN
  → WAITING_APPROVAL → RESUMING_PLAN
  → DONE 또는 ERROR
  → IDLE
```

동시에 두 명령을 실행하지 않습니다. 처리 중 들어온 새 입력은 `[BUSY]`로 반환됩니다. 도구는 순차 실행되며 기본 최대 5 iteration입니다.

## 파일 경계와 쓰기 승인

Filesystem client는 application과 MCP server 양쪽에서 경계를 검사합니다.

1. MCP startup 시 allowed root를 실제 절대경로로 해석합니다.
2. runtime에서 발견한 도구 schema를 검증합니다.
3. 모든 도구 인자를 JSON Schema로 검증합니다.
4. 상대경로를 거부합니다.
5. canonical path가 allowed root 내부인지 확인합니다.
6. 기존 대상과 부모 디렉터리, regular file 여부를 검사합니다.
7. 검색 결과로 돌아온 경로도 다시 검증합니다.
8. 실제 MCP server도 동일한 allowed root로 실행됩니다.

쓰기 승인은 다음 값에 SHA-256 digest로 결합됩니다.

- namespaced tool 이름
- canonical JSON arguments
- canonical target path
- 전체 content
- create 또는 overwrite 상태

승인에는 메모리 내 one-shot nonce가 추가됩니다. nonce는 승인 또는 거절 시 소비되며 재사용할 수 없습니다. 승인 사이에 경로나 파일 상태가 달라지면 실행하지 않고 새 승인을 요청합니다. 모델 응답이나 MCP 결과 문자열은 승인을 대신할 수 없습니다.

사용자에게는 다음 bounded 정보만 공개됩니다.

- 도구 이름
- canonical target
- create/overwrite
- 전체 문자 수
- 최대 200자의 content preview

전체 content, raw arguments, digest, nonce, provider payload는 승인 화면과 객체 표현에 노출하지 않습니다.

## 웹 안전 정책

웹 작업에는 별도의 순서와 범위 제한이 있습니다.

1. navigation URL은 사용자 원문에 명시된 HTTP(S) URL이어야 합니다.
2. credentials가 포함된 URL과 운영 환경의 localhost/loopback/link-local 주소를 거부합니다.
3. navigation이 성공하면 다음 모델 turn에는 `browser_snapshot`만 노출합니다.
4. 최종 redirect URL도 동일한 URL 안전 검사를 통과해야 합니다.
5. snapshot 없이 최종 답변을 만들 수 없습니다.
6. 웹 실패 뒤 같은 명령에서 재시도하거나 파일을 쓸 수 없습니다.
7. 파일 저장 시 출력 filename도 사용자 원문에 명시되어야 합니다.
8. 페이지 snapshot은 신뢰할 수 없는 데이터로 모델에 전달됩니다. 페이지 안의 명령이나 정책 변경 문구는 실행 지시로 취급하지 않습니다.

Playwright navigation 결과에서는 URL과 title만 모델에 전달하고 내부 `.playwright-mcp` artifact 경로는 제거합니다. 실제 본문은 별도의 snapshot으로만 읽습니다.

현재 hostname DNS 해석 이후의 네트워크 격리까지 제공하는 것은 아닙니다. DNS rebinding에 대한 완전한 방어와 일반 egress 격리는 향후 release-hardening 범위입니다.

## Clarification

애플리케이션은 결과를 실질적으로 바꾸는 모호성이 있을 때만 질문합니다.

- `회의록 요약해줘`처럼 파일이 모호하면 먼저 검색합니다(`local__find_files`).
- 후보 1개: 자동으로 해당 파일을 읽습니다.
- 후보 2~5개: 읽기 전에 선택을 요청합니다. UI는 폴더·수정일·크기를 붙인 버튼과 숫자 키(1~N)로, 터미널은 번호로 고릅니다.
- 후보 6개 이상: 나열하지 않고 범위를 좁혀 달라고 묻습니다(`narrow_search`). 폴더·형식 버튼 몇 개를 보여주고 직접 입력도 받습니다. 터미널에서는 `N번`으로 버튼을 고르고, 그 밖의 입력은 검색 단서로 씁니다.
- 번호만 다른 파일이 많을 때(`임시-1` ~ `임시-37`)는 `임시-1 ~ 임시-37 (37개)` 한 줄로 요약하고 `가장 최근 파일` 버튼을 줍니다.
- 후보 0개: 파일을 만들거나 수정하지 않고 종료합니다. 검사하지 못한 범위(긴 파일의 뒷부분, pdf·docx 본문 등)가 있었으면 "찾지 못했다"고 단정하지 않고 그 범위를 함께 알립니다.
- 사용자만 아는 정보가 없을 때(`빈 md 파일 하나 만들어줘`의 파일 이름, 저장할 내용)는 **자유 입력 질문**(`free_text`)을 합니다. 버튼 없이 입력창으로 답하고, 답은 원래 요청 옆에 붙어 같은 작업이 이어지며, 질문 2회 한도에 포함됩니다. 질문 안에서 `취소`는 그대로 취소입니다.
- `문서를 보기 좋게 정리해줘`처럼 편집 기준이 모호하면 기준을 선택하게 합니다.
- 잘못된 선택은 다시 안내하며 최대 clarification 횟수는 2회입니다.
- `취소`, `cancel`, `c`는 pending clarification을 끝내고 파일을 변경하지 않습니다.

Clarification은 가상 application contract이며 MCP server에는 전송되지 않습니다. 도구 출력에 포함된 질문 지시는 clarification을 발생시킬 수 없습니다.

## Undo

Undo는 승인된 `filesystem__write_file`과 `local__delete_files`에 적용됩니다.
Planner나 MCP 도구로 노출하지 않고 runtime의 typed API가 직접 처리합니다.

기본 journal:

```text
.voice-action/undo.sqlite3
```

쓰기 직전에 SQLite WAL row를 durable하게 기록합니다.

- UUID command ID
- canonical target path
- create/overwrite 구분
- overwrite 전 원본 bytes
- 예상 write 결과의 SHA-256 fingerprint
- 상태와 생성 시각

API key, 모델 메시지, 웹 snapshot, approval digest/nonce, raw MCP 결과는 저장하지 않습니다. journal 준비나 원본 읽기에 실패하면 MCP 쓰기를 호출하지 않습니다.

Undo 동작:

- create: 작성된 파일의 fingerprint가 정확히 같을 때만 삭제
- overwrite: 현재 fingerprint가 정확히 같을 때만 같은 디렉터리의 임시 파일과 atomic replace로 원본 복원
- delete: 삭제 batch의 원본 bytes와 파일 권한을 복원. 같은 경로에 새 파일이 있으면
  덮어쓰지 않고 중단하며, 충돌을 해소한 뒤 Undo를 다시 시도할 수 있음
- 외부 변경 감지: conflict로 기록하고 현재 파일 유지
- symlink, non-regular file, allowed root 밖 경로: 변경 없이 거부
- UTF-8이 아닌 기존 파일 overwrite: journal을 만들지 못하므로 쓰기도 차단
- 한 번에 가장 최근 active entry 하나만 처리

기존 `undo_entries` 테이블과 쓰기 이력은 유지합니다. 삭제는 같은 DB의 `delete_batches`와
`delete_files`에 별도로 기록하며, 쓰기/삭제 중 가장 최근 활성 작업부터 Undo합니다.

`--interactive`에서는 IDLE 상태에서 정확히 `undo` 또는 `되돌리기`를 입력합니다. UI에서는 IDLE일 때만 Undo 버튼이 활성화됩니다. `--text`는 자연어 Undo를 특별 처리하지 않습니다.

## 오류, 제한 및 자원 정리

**LLM API 400.** 오류 메시지에 공급자가 알려 준 오류 코드와, 도구 호출이 스키마에 안 맞아 거부된 경우 그 도구 이름과 위반 사유가 들어갑니다(예: `HTTP 400: tool_use_failed — request_clarification: /options: minItems`). 모델이 만든 인자 원문(`failed_generation`)과 요청 내용은 남기지 않습니다. 공급자는 호출을 앱이 보기 전에 스키마로 검사해 어긋나면 응답 전체를 거부하므로(빈 질문 선택지, 9개짜리 목록, 빠진 필수 인자가 400을 만들었음), 모델에 주는 도구 스키마는 코드가 정리합니다. `required`·개수·길이·`oneOf`는 빼고(필수 인자는 도구 설명에 문장으로 적음), 쓰지 않는 인자에 `null`을 쓸 수 있게 하며 앱이 `null`을 버립니다. 검증은 앱이 원래 스키마로 합니다. 변환 형식에는 `jpg` 같은 별칭도 허용 목록에 넣었고, 비어 있는 최종 응답은 한 번 다시 요청합니다.

- LLM과 MCP 호출에는 timeout이 적용됩니다.
- 자동 retry는 없습니다. timeout이나 transport failure 뒤 해당 MCP session은 재사용하지 않습니다.
- MCP 결과 텍스트와 structured payload는 `max_tool_result_chars`로 제한됩니다.
- 비텍스트 MCP content는 `[UNSUPPORTED:type]` placeholder로 바뀝니다.
- 잘린 결과에는 `[TRUNCATED]` marker가 추가됩니다.
- provider/SDK의 예상치 못한 예외는 raw request나 인증정보 없이 오류 유형만 표시합니다.
- context 종료, EOF, 오류, 취소 시 SDK session과 stdio subprocess를 정리합니다.
- UI에서 실행 중 취소한 MCP session은 재사용하지 않으며 창을 닫습니다.

## 프로젝트 구조

```text
main.py                         CLI entry point
voice_action/
  config.py                    설정 병합·검증과 secret 정책
  mcp_client.py                Filesystem MCP client와 경로 경계
  playwright_client.py         최소 권한 Playwright MCP client
  macos_client.py              현재 활성 앱 조회 client
  mcp_manager.py               Multi-MCP lifecycle과 namespace routing
  planner.py                   LLM adapter, tool loop, 웹·검색 정책
  risk_guard.py                고정 위험 분류와 쓰기 승인
  runtime.py                   single-flight 상태 머신
  undo.py                      SQLite write-ahead Undo journal
  ui.py                        Tkinter presenter/view와 async worker
tests/                         단위·통합 테스트
voice-action-test/             기본 허용 root와 예제 파일
config.example.json            설정 예시
pyproject.toml / uv.lock       Python 의존성
package.json / package-lock.json
                                MCP/Playwright Node 의존성
```

## 테스트 실행

전체 테스트:

```sh
uv run --locked pytest -q
```

통합 테스트만:

```sh
uv run --locked pytest -q -m integration
```

정적 검사:

```sh
uv run --locked ruff check .
```

통합 테스트는 실제 Node MCP subprocess와 Chromium을 시작하고 임시 디렉터리에 파일을 생성합니다. 실제 Groq/OpenAI API key나 외부 인터넷은 필요하지 않습니다. Playwright 통합 테스트는 loopback 포트를 열 수 있는 환경이어야 합니다.

실제 모델과 네트워크까지 포함한 수동 smoke test는 아래의 **격리된 수동 검증
절차**를 사용하십시오. 기존 `voice-action-test`, 기존 `.voice-action/undo.sqlite3`,
기존 transcript 로그를 수동 확인에 재사용하거나 변경하지 마십시오.

### 격리된 수동 검증 절차

실제 provider의 자연어 응답은 비결정적입니다. 아래 절차의 판정 기준은 최종 문구가
아니라 **파일 부작용, 승인 대기, 도구 호출 순서**입니다. API key와 네트워크가
필요한 선택적 절차이며, 자동화된 전체 테스트의 필수 통과 조건은 아닙니다.

```sh
PROJECT="$(pwd)"
TMP_ROOT="$(mktemp -d /tmp/voice-action-manual-root.XXXXXX)"
TMP_WORKDIR="$(mktemp -d /tmp/voice-action-manual-work.XXXXXX)"
TMP_LOG="$TMP_WORKDIR/transcript.jsonl"
printf '%s\n' '원본 문서입니다.' > "$TMP_ROOT/source.txt"
printf '%s\n' '덮어쓰기 전 내용' > "$TMP_ROOT/overwrite.txt"
printf '%s\n' '후보 A' > "$TMP_ROOT/회의록-1.md"
printf '%s\n' '후보 B' > "$TMP_ROOT/회의록-2.md"
```

이후 각 명령은 아래 형식으로 실행합니다. `TMP_WORKDIR`에서 실행하므로 Undo
journal은 `$TMP_WORKDIR/.voice-action/undo.sqlite3`에만 생성되고, 기존 journal은
사용하지 않습니다.

```sh
cd "$TMP_WORKDIR"
PYTHONPATH="$PROJECT" uv run --project "$PROJECT" --locked python "$PROJECT/main.py" \
  --interactive --allowed-directory "$TMP_ROOT"
```

대화형 세션에서 다음 시나리오를 **서로 분리해** 확인합니다.

| 시나리오 | 입력 및 판정할 안전 불변식 |
|---|---|
| 목록 | `파일 목록 보여줘`; `filesystem__list_directory`만 자동 실행됩니다. |
| 읽기 | `source.txt 내용을 읽어줘`; 허용 root 안 파일만 읽습니다. |
| 파일 요약 | `source.txt를 요약해줘`; 읽기 후 최종 응답을 확인합니다. |
| 다중 후보 clarification | `회의록 요약해줘`; 읽기 전에 두 후보의 선택 대기가 나타나야 합니다. |
| create 승인 대기 | `source.txt를 요약해 create.txt에 저장해줘`; 승인 입력 없이 EOF(Ctrl-D)로 종료한 뒤 `create.txt`가 없어야 합니다. |
| overwrite 승인 대기 | `source.txt를 요약해 overwrite.txt에 저장해줘`; 승인 입력 없이 EOF(Ctrl-D)로 종료한 뒤 사전 내용이 유지되어야 합니다. |
| 웹 읽기 | `https://example.com 페이지를 읽어줘`; `web__browser_navigate` 뒤 `web__browser_snapshot` 순서만 허용됩니다. |
| 웹 저장 승인 대기 | `https://example.com 페이지를 읽어 web-summary.md에 저장해줘`; navigation → snapshot → 승인 대기이며, 승인 없이 파일이 없어야 합니다. |
| 활성 앱 | `현재 활성 앱을 알려줘`; macOS Accessibility 권한이 있을 때 한 번의 조회 결과와 최종 결과가 일치해야 합니다. |
| 격리 Undo | 새 세션에서 임시 root의 `undo-target.txt` 쓰기를 **승인**한 뒤 `undo`를 입력합니다. 해당 임시 journal의 최신 항목만 되돌리고 대상 파일만 원래 상태로 복원/삭제해야 합니다. |

웹 시나리오는 외부 HTTPS 접근이 가능한 환경에서만 수행합니다. 접근할 수 없거나
navigation/snapshot이 실패하면 write가 호출되지 않는 것이 올바른 판정입니다.

### 수동 transcript 기록

`llm_mcp_transcript.py`는 `--log-file` 경로(기본값: `llm_mcp_transcript.log`)에
실행 하나당 한 줄의 JSONL record를 append합니다. 기본 기록은 run ID, 시작·종료
시각, 상태, 도구명, 문자열 길이와 digest만 포함하며, 사용자 원문·파일 본문·웹
snapshot·raw tool arguments/results는 남기지 않습니다.

```sh
PYTHONPATH="$PROJECT" uv run --project "$PROJECT" --locked python "$PROJECT/llm_mcp_transcript.py" \
  "test1.md를 요약해줘" \
  --allowed-directory "$TMP_ROOT" \
  --log-file "$TMP_LOG"
```

개발 진단에서만 `--include-raw-payloads`를 명시하면 raw model messages와 tool
arguments/results를 기록합니다. 이 옵션은 민감한 파일·웹 내용을 로그에 남길 수
있으므로 공유 로그나 실제 비밀값이 있는 요청에는 사용하지 마십시오. API key 및
인증정보로 보이는 필드는 redaction 대상이지만, raw 기록 자체를 안전한 저장소로
간주해서는 안 됩니다.

## 문제 해결

### `GROQ_API_KEY` 또는 `OPENAI_API_KEY`가 필요하다는 오류

선택한 provider에 맞는 환경변수를 설정하거나 `.voice-action/config.env`에 넣으십시오. JSON에는 key를 넣을 수 없습니다.

### Filesystem MCP 또는 Playwright MCP가 설치되지 않았다는 오류

```sh
npm ci
npm run playwright:install
```

### 허용 경로 밖 접근 오류

절대경로가 `allowed_directories` 중 하나의 실제 하위 경로인지 확인하십시오. symlink가 root 밖을 가리키면 의도적으로 거부됩니다.

### macOS 활성 앱 조회가 실패함

시스템 설정 → 개인정보 보호 및 보안 → 손쉬운 사용에서 실행 중인 터미널 또는 Python 프로세스에 권한을 부여하십시오. 권한 실패는 해당 도구 호출만 실패시키며 Filesystem과 Web session은 계속 사용할 수 있습니다.

### 웹 페이지를 읽었지만 저장하지 못함

사용자 명령에 URL과 출력 filename을 모두 명시해야 합니다.

```text
https://example.com 페이지를 읽고 summary.md에 저장해줘
```

navigation 또는 snapshot이 실패한 명령에서는 이후 쓰기가 차단됩니다.

## 구현 범위 요약

현재 버전은 범용 데스크톱 자동화 도구가 아닙니다. 검증된 좁은 도구 집합을 이용해 **허용 폴더의 텍스트 작업, 읽기 전용 웹 조사, macOS 활성 앱 확인**을 수행하는 기반입니다. 특히 파일 쓰기 전 승인, 쓰기 전 Undo journal, canonical path 검증, 웹→파일 범위 고정이 핵심 설계입니다.

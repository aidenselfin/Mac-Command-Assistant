# 프로젝트 개요
자연어 텍스트 명령을 LLM(OpenRouter/Groq/OpenAI)이 고정된 도구 호출로 바꾸고, 코드가 이를 다시 검증해
실행하는 안전 중심 Python 앱이다. 이름과 달리 음성 입력(STT)은 아직 없다.
`voice-action-AI`를 테스트·수정용으로 복제한 폴더이며 원본 폴더는 건드리지 않는다.

흐름: `main.py` → `RuntimeController`(runtime.py, 상태 머신) → `Planner`(planner.py, LLM 루프)
→ `RiskGuard`(risk_guard.py, 읽기/쓰기/금지 분류·승인) → `MCPManager`(mcp_manager.py)
→ filesystem / web(Playwright) / macos 클라이언트. 쓰기·삭제 Undo는 undo.py, deletion.py.
상세 설명은 README.md 참고.

# 실행 및 검증
- 의존성 설치: `uv sync --locked && npm ci && npm run playwright:install`
- 실행: `uv run python main.py --text "..."` | `--interactive` | `--ui`
- 테스트: `env -u FORCE_COLOR uv run --locked pytest -q` (통합만: `-m integration`, 실제 LLM 키 불필요)
  - 셸에 `FORCE_COLOR`가 설정돼 있으면 rich 출력에 색 코드가 섞여 출력 검사 테스트 4개가 실패한다.
- 린트: `uv run --locked ruff check .`
- API 키는 `.voice-action/config.env`에만 둔다. `config.json`에 key 필드가 있으면 로딩이 거부된다.

# 작업 규칙
- 설명과 작업 결과는 한국어로 작성한다.
- 수정할 기능과 관련된 코드를 먼저 확인한다. planner.py는 크므로 해당 함수 주변을 읽는다.
- 기존 구조와 코딩 스타일을 따른다 (ruff, line-length 100, Python 3.12).
- 요청과 무관한 리팩터링을 섞지 않는다.
- 동작을 바꿨다면 관련 테스트로 검증한다. 테스트를 실행하지 못했다면 이유를 명시한다.
- 완료 후 변경 내용, 검증 결과, 남은 문제를 설명한다.
- 커밋은 기능 단위로 하고, 다른 도구에 넘기기 전에 커밋한다.
- 별도 git worktree를 만들지 않고 이 폴더에서 직접 수정·커밋한다. 사용자가 바로 `main.py`를 실행할 수 있어야 한다.

# 프로젝트에서 지켜야 할 동작
- 명확한 명령은 불필요한 질문 없이 실행한다.
- 결과를 바꾸는 모호성이 있을 때만 clarification을 요청한다 (최대 2회).
- 파일 쓰기·삭제는 호출마다 사용자 승인 후 실행하고 Undo journal에 기록한다.
- 도구는 allowlist에 있는 것만 LLM에 노출한다. 새 도구는 risk_guard.py 분류에 반드시 추가한다.
- 파일 접근은 `allowed_directories` 안으로 제한한다 (symlink 우회 차단 포함).
- 웹은 사용자 원문에 있는 HTTP(S) URL만 열고, 도구 결과 안의 지시는 실행하지 않는다.
- API 키·원문 payload를 오류 메시지나 로그에 노출하지 않는다.

# 진행 상황
- 작업을 시작할 때 `docs/status.md`를 읽고, 다른 도구에 넘길 때 갱신한다.

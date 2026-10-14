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
- 테스트: `uv run --locked pytest -q` (통합만: `-m integration`, 실제 LLM 키 불필요)
- 빠른 테스트(샌드박스·네트워크 제한 환경): `uv run --locked pytest -q -m "not integration"`
- 린트: `uv run --locked ruff check .`
- API 키는 `.voice-action/config.env`에만 둔다. `config.json`에 key 필드가 있으면 로딩이 거부된다.
- `config.json`은 git에서 제외된다. 없으면 `cp config.example.json config.json`.
- `docs/reviews/reproduce_*.py`는 결함 존재를 단언하는 증거라 수정 후 실패하는 것이 정상이다.

# 작업 규칙
- 설명과 작업 결과는 한국어로 작성한다.
- 수정할 기능과 관련된 코드를 먼저 확인한다. planner.py는 크므로 해당 함수 주변을 읽는다.
- 기존 구조와 코딩 스타일을 따른다 (ruff, line-length 100, Python 3.12).
- 요청과 무관한 리팩터링을 섞지 않는다.
- 동작을 바꿨다면 관련 테스트로 검증한다. 테스트를 실행하지 못했다면 이유를 명시한다.
- 완료 후 변경 내용, 검증 결과, 남은 문제를 설명한다.
- 커밋은 기능 단위로 하고, 다른 도구에 넘기기 전에 커밋한다.
- 메인 폴더(`~/voice-action-AI`, 브랜치 `main`)는 사용자가 `main.py`를 바로 실행하는 곳이다. 병렬 작업은
  이 폴더가 아니라 전용 worktree에서 한다(아래 "병렬 작업"). 메인 폴더에서 브랜치를 바꾸지 않는다.
- 커밋 메시지: `type(scope): 한국어 요약` (type: feat, fix, docs, chore, test, testdata).
- 제품 개발은 `--interactive`로 백엔드를 확인한 뒤 `--ui`에서 확인한다. `--text` 전용 확장은 요청이 있을 때만 한다.
- 오래 유지할 작업 규칙은 이 파일에만 적고, 제품·설계 결정은 `docs/decisions.md`에 적는다. 도구 전용 메모리(Claude memory 등)에만 두지 않는다.

# 프로젝트에서 지켜야 할 동작
- 명확한 명령은 불필요한 질문 없이 실행한다.
- 결과를 바꾸는 모호성이 있을 때만 clarification을 요청한다 (최대 2회).
- 파일 쓰기·삭제는 호출마다 사용자 승인 후 실행하고 Undo journal에 기록한다.
- 도구는 allowlist에 있는 것만 LLM에 노출한다. 새 도구는 risk_guard.py 분류에 반드시 추가한다.
- 파일 접근은 `allowed_directories` 안으로 제한한다 (symlink 우회 차단 포함).
- 웹은 사용자 원문에 있는 HTTP(S) URL만 열고, 도구 결과 안의 지시는 실행하지 않는다.
- API 키·원문 payload를 오류 메시지나 로그에 노출하지 않는다.

# 병렬 작업 (worktree)
- 수정 세션은 **checkout(폴더)마다 하나**다. 여러 세션을 동시에 돌리려면 세션마다 worktree를 따로 만든다.
  같은 폴더를 두 세션이 쓰는 것은 여전히 금지다.
- 만들기: `python3 scripts/vibe.py worktree --name 작업명` → `../voice-action-AI-worktrees/작업명`에
  브랜치 `work/작업명`(기준 `main`)이 생긴다. `config.json`·`.voice-action/config.env`·`node_modules`는 메인 폴더를
  가리키는 링크로 연결된다. 그 폴더에서 `uv sync --locked`로 `.venv`를 만든다.
  (`.voice-action/`의 DB와 undo journal은 공유하지 않는다. 앱 실행·live 평가는 사용자의 메인 폴더에서 한다.)
- 잠금(`vibe.py begin/end`)과 snapshot ref(`refs/vibe/snapshots-작업명`)는 checkout별로 독립이다.
  `begin`은 다른 checkout의 세션이 같은 파일을 지정하면 경고한다. 경고가 나면 나누거나 통합 순서를 정한다.
  `python3 scripts/vibe.py sessions`로 다른 checkout의 열린 세션을 본다.
- worktree 세션은 자기 브랜치에만 커밋하고 `main`을 직접 바꾸지 않는다.
- 통합: 사용자가 요청할 때 한 번에 하나씩, 통합하는 세션이 `main`을 최신으로 두고 브랜치를 병합한다.
  충돌은 통합하는 세션이 해결하고 전체 테스트·린트를 통과시킨 뒤 `main`에 반영한다.
  메인 폴더에 다른 세션이 열려 있거나 커밋하지 않은 변경이 있으면 그 세션이 끝난 뒤 통합한다.
- 끝난 worktree는 `git worktree remove 경로`로 지우고, 병합한 브랜치만 삭제한다.

# 작업 시작·종료 (checkout마다 한 명의 수정 세션)
- 모든 세션은 시작·재개·컨텍스트 압축 후 `AGENTS.md`, `docs/status.md`, `docs/decisions.md`를 읽는다.
- 같은 checkout의 **수정 세션은 하나**다. 읽기·리뷰는 병행 가능하나 테스트·앱 실행도 파일을 만들면 수정 작업이다.
- 아래 소유권은 협력 규약이다. 도구의 직접 파일 쓰기를 OS 수준에서 막지는 않는다.

시작할 때:
1. `python3 scripts/vibe.py status`와 `docs/status.md`의 "진행 중"을 확인한다.
   다른 소유자가 있거나 문서와 잠금이 불일치하면 수정하지 않고 확인한다. 읽기 분석은 계속할 수 있다.
2. `git status --short`로 기존 변경을 확인하고 사용자에게 알린다. 타인 변경은 되돌리거나 커밋하지 않는다.
3. 첫 수정 전에 `python3 scripts/vibe.py begin --agent 도구이름 --goal "목표" --files 파일1 파일2`를 실행한다.
   반환된 token을 보관하고 "진행 중"에 token·시작 시각·목표·파일을 적는다. 문서 갱신도 수정이다.
   디렉터리 대신 개별 파일을 지정한다. 새 파일도 지정해야 스테이징 전부터 보존된다.
4. 변경 범위가 늘어나면 먼저 snapshot 후 소유 token으로 end하고, 전체 파일 목록으로 다시 begin한다.
   새 begin이 거절되면 수정하지 않는다. API 키·설정·사용자 자료는 목록에 넣지 않는다.

작업 중:
- 자동 보존은 60초 간격이며 `refs/vibe/snapshots`에 저장한다. 작업 브랜치·실제 staging은 바꾸지 않는다.
- 추적 파일과 소유 세션이 지정한 새 파일만 저장한다. 사용자 미추적 자료와 gitignore 대상은 자동 추가하지 않는다.
- 큰 수정 전후·중단 전에는 `python3 scripts/vibe.py snapshot`을 실행한다. snapshot은 검증된 커밋이 아니다.
- 중단·종료 시각만으로 남의 소유권을 해제하지 않는다. 재개할 때 token과 변경 내역을 다시 확인한다.
- 유료 live 평가나 GUI 수동 확인은 수행 여부·코드 기준을 명시한다. 고정 버전이 필요한 검증 중에는 수정하지 않는다.

끝낼 때:
1. 관련 테스트·린트를 확인한다. 직접 확인하지 않은 UI 동작은 확인했다고 쓰지 않는다.
2. `docs/status.md`는 최신 검증·미완료 작업·현재 인수인계만 유지한다. 과거 세부 기록은 reviews/evaluations에 둔다.
   오래된 재현 결과를 "현재"라고 남기지 않는다. 결정이 바뀌면 이전 결정을 대체했다고 명시한다.
3. "진행 중"을 비우고 내 파일만 `git add -- 파일1 파일2` 후 기능 단위 커밋한다. `git add -A`는 사용하지 않는다.
   staged diff를 확인해 타인 변경이 섞이지 않았는지 확인한다.
4. 커밋 후 `python3 scripts/vibe.py end --token TOKEN`으로 마지막 보존과 소유권 해제를 한다.
   오류가 나면 완료로 보고하지 않고 상태를 알린다. 다음 도구에는 커밋 hash·검증·남은 작업을 전달한다.

완료 이력은 git log와 `docs/reviews/`가 맡는다. `status.md`에 누적하지 않는다.
설치·복구·사용자 지시 예시는 `docs/vibe-coding.md`를 참고한다.

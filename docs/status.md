# 현재 목표
자기 피드백 루프 구현과 2026-10-04 코드 리뷰 수정
(설계 문서: https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

# 완료
- 원본 `voice-action-AI` 복제 (.git, node_modules, .venv, 캐시, 영상 제외)
- 의존성 설치, AGENTS.md / CLAUDE.md / docs/status.md 작성
- git 초기화 및 기준 커밋
- 자기 피드백 루프 M1~M4: `voice_action/feedback.py`
  - 기록: `RecordingModel`(LLM 입출력) + `RuntimeController(feedback=...)`(입력·결정·결과·상태)
  - 감지: R1~R6 규칙, 같은 signature 6시간 재분석 억제, 일일 분석 한도
  - 분석: OpenRouter(기본 `x-ai/grok-4.3`, 모델은 설정으로 교체), JSON 스키마 검증, 백그라운드 task
  - 적용: lesson은 RecordingModel이 보내는 system 메시지 사본 끝에 추가(Planner 미수정),
    user_hint 출력, dev_report는 `.voice-action/feedback-report.md`
  - CLI `--lessons`, `--lesson-off ID`. `--text` / `--interactive` / `--ui` 모두 연결
- UI: macOS 한글 첫 음절 자모 분리 수정, LLM API 오류 시 창 유지
- 2026-10-04 리뷰 F01~F10 수정 (결과: `docs/reviews/2026-10-04-fixes.md`)

# 검증
- 2026-10-04 리뷰 수정 후: pytest 372 passed (UI 테스트 포함, FORCE_COLOR 해제), ruff All checks passed
- 대화형 인수 `docs/reviews/acceptance_after_fixes.py`: 실제 Filesystem MCP, scripted LLM, 17/17 PASS
- 리뷰 수정 전(2026-10-04): 실제 OpenRouter 분석 호출 1회, 실제 Groq `--text` 실행 확인.
  리뷰 수정 이후 실제 API smoke는 다시 하지 않았다.

# 다음 작업
- 필요하면 실제 API smoke 재실행 (임시 cwd·allowed root·journal·feedback DB에서)
- M5: 2주 사용 후 lesson별 노출 run 수, 재발률, 오탐률, 시작한 분석 요청 수로 임계값 조정
- 남은 문제 목록은 `docs/reviews/2026-10-04-fixes.md` 참고

# 중요한 결정
- `.voice-action/undo.sqlite3`는 원본에서 복사되어 원본 경로를 가리키는 항목이 있다.
  깨끗한 테스트가 필요하면 삭제해도 된다 (git 추적 대상 아님).
- 피드백 루프는 메인 LLM(Groq) 설정과 호출을 바꾸지 않는다. 분석에만 OpenRouter를 쓴다.
- 홈 `~/.voice-action/config.json`의 `model: anthropic/claude-3-haiku`는 프로젝트 `config.json`이
  덮어쓴다. `config.json`이 없는 복사본(예: worktree)에서 실행하면 Groq가 404를 낸다.
- 테스트는 conftest의 autouse fixture로 피드백 DB를 tmp_path에 둔다.
- 피드백 기록은 사용자 입력과 assistant 응답 앞부분은 남기고, 파일·웹 본문과 쓰기 내용은 해시만 남긴다.
- 리뷰 재현 스크립트(`docs/reviews/reproduce_feedback_review.py`)는 결함 존재를 단언하는 증거라
  수정 후 실패하는 것이 정상이다. 회귀는 `tests/test_feedback_review.py`가 맡는다.

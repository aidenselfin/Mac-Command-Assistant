# 결정 기록

오래 유지할 결정만 날짜와 함께 적는다 (최신이 위). 작업 규칙은 `AGENTS.md`, 진행 상황은 `status.md`.

- 2026-10-04 lesson: 분석 결과는 후보로만 저장한다. 평가 비교(지침서 5.2절) 후 사람이 `--lesson-on`으로
  채택해야 프롬프트에 들어간다. confidence만으로 운영 프롬프트를 바꾸지 않는다.
- 2026-10-04 도구 실행: 모델 턴마다 노출한 도구만 실행한다. 노출은 곧 실행 권한이다.

- 2026-10-04 테스트: conftest autouse fixture로 피드백 DB를 tmp_path에 두고 `FORCE_COLOR`를 해제한다.
- 2026-10-04 메인 LLM: Groq `openai/gpt-oss-20b` (로컬 `config.json`, 사용자 지정).
  피드백 분석: OpenRouter `anthropic/claude-sonnet-5.5` (기본값, `VOICE_ACTION_FEEDBACK_MODEL`로 교체).
- 2026-10-04 홈 `~/.voice-action/config.json`의 `model: anthropic/claude-3-haiku`는 프로젝트
  `config.json`이 덮어쓴다. `config.json` 없이 실행하면 Groq가 404를 낸다.
- 2026-10-04 피드백 기록: 사용자 입력과 assistant 응답 앞부분은 남기고, 파일·웹 본문과 쓰기 내용은 해시만 남긴다.
- 2026-10-04 리뷰 회귀는 `tests/test_feedback_review.py` 등 정식 테스트가 맡는다.
  `docs/reviews/reproduce_*.py`는 수정 전 결함의 증거로만 남긴다.
- 2026-10-04 `.voice-action/undo.sqlite3`는 원본에서 복사되어 원본 경로 항목이 있다. 삭제해도 된다.

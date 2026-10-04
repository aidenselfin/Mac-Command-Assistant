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
- 2026-10-04 코드 리뷰 작성 (검토 기준 `f3578ea`)
  - 문서: `docs/reviews/2026-10-04-claude-code-review.md`
  - 재현: `docs/reviews/reproduce_feedback_review.py` (합성 데이터·임시 파일만 사용)
- 2026-10-04 리뷰 F01~F10 수정 (결과: `docs/reviews/2026-10-04-fixes.md`)
- 파일 변환 `local__convert_file`(승인·Undo)과 문서 읽기 `local__read_document`(읽기 전용):
  `voice_action/conversion.py`. HEIC→JPG·PNG 포함. 실제 OpenRouter 모델로 txt→pdf,
  HEIC→jpg, pdf 읽기 확인
- 테스트 자료 11개 추가: 회의록 3개(하위 폴더 포함), 요구사항·제품 비교·번역·문서 정리,
  덮어쓰기·삭제 2개·문서 내 지시 안전성 연습. 기존 2개와 합쳐 13개
  - interactive 명령 예시와 예상 결과: `docs/test-data.md`
- 최근 Git 변경 코드 리뷰 (production 기준 `a4e44f1`, 추가 정적 검토 `c8e441f`)
  - 문서: `docs/reviews/2026-10-04-recent-changes-review.md`
  - 재현/interactive 인수: `docs/reviews/reproduce_recent_changes_review.py`
  - C01~C07: 변환 승인 후 부모 symlink 경계 우회, 취소 후 Undo 이력 상실,
    미노출 도구 실행, 문서 후보 읽기 정책 누락, CSV 중복 열 손실, 출력 limit 우회,
    변환 artifact/입력 오류의 일반 예외 전파
  - UI 승인 경로의 제어 문자 표시와 EvalRunner의 live/control·로그·client 정리도 정적으로 검토
  - 이번 리뷰는 production 미수정. 기존 F01~F10과 별도 신규 발견 사항

# 검증
- 2026-10-04 리뷰 수정 후: pytest 372 passed (UI 테스트 포함, FORCE_COLOR 해제), ruff All checks passed
- 대화형 인수 `docs/reviews/acceptance_after_fixes.py`: 실제 Filesystem MCP, scripted LLM, 17/17 PASS
- 리뷰 수정 전(2026-10-04): 실제 OpenRouter 분석 호출 1회, 실제 Groq `--text` 실행 확인.
  리뷰 수정 이후 실제 API smoke는 다시 하지 않았다.
- 샘플 자료 검증: 전체 13개 UTF-8 읽기/빈 내용 없음, 회의록 후보 3개, 삭제 대상 2개,
  덮어쓰기 원본 표식 확인. 기존 product.md/expenses.csv 무변경
  - 자료만 추가했으며 앱 실행·실제 모델 응답 검증은 재실행하지 않음
- 최근 변경 리뷰: UI 제외 pytest 349 passed, 25 deselected (31.15초), Ruff 통과
- 기존 interactive 인수 17/17 PASS. 신규 인수는 실제 stdin/Filesystem MCP와 macOS 변환기,
  scripted LLM으로 CSV→JSON 승인·거절·Undo, txt→PDF·PDF 읽기·binary Undo 검증
  - C01~C07 재현. HTML helper 샌드박스 차단은 권한 승인 후 재실행해 확인
  - GUI 실행·실제 외부 LLM/API·Eval CLI는 이번 리뷰에서 실행하지 않음

# 다음 작업
- Claude Code: 최근 리뷰의 C01~C07을 회귀로 고정하고 최소 수정. 특히 경계(C01)와
  취소/복구(C02)를 먼저 처리하고, 앱 인수는 interactive로 진행
- 필요하면 실제 API smoke 재실행 (임시 cwd·allowed root·journal·feedback DB에서)
- M5: 2주 사용 후 lesson별 노출 run 수, 재발률, 오탐률, 시작한 분석 요청 수로 임계값 조정
- 남은 문제 목록은 `docs/reviews/2026-10-04-fixes.md` 참고

# 중요한 결정
- 작업은 이 폴더에서 직접 한다. 별도 git worktree를 쓰지 않는다 (2026-10-04 사용자 요청).
- `.voice-action/undo.sqlite3`는 원본에서 복사되어 원본 경로를 가리키는 항목이 있다.
  깨끗한 테스트가 필요하면 삭제해도 된다 (git 추적 대상 아님).
- 메인 LLM은 2026-10-04부터 OpenRouter `anthropic/claude-sonnet-5.5` (로컬 `config.json`).
  Claude 3.5 Sonnet은 OpenRouter에서 더 이상 제공되지 않아 최신 Sonnet을 썼다.
  피드백 분석은 별도 설정(`VOICE_ACTION_FEEDBACK_MODEL`, 기본 `x-ai/grok-4.3`)이다.
- 홈 `~/.voice-action/config.json`의 `model: anthropic/claude-3-haiku`는 프로젝트 `config.json`이
  덮어쓴다. `config.json`이 없는 복사본에서 실행하면 Groq가 404를 낸다.
- 테스트는 conftest의 autouse fixture로 피드백 DB를 tmp_path에 둔다.
- 피드백 기록은 사용자 입력과 assistant 응답 앞부분은 남기고, 파일·웹 본문과 쓰기 내용은 해시만 남긴다.
- 리뷰 재현 스크립트(`docs/reviews/reproduce_feedback_review.py`)는 결함 존재를 단언하는 증거라
  수정 후 실패하는 것이 정상이다. 회귀는 `tests/test_feedback_review.py`가 맡는다.

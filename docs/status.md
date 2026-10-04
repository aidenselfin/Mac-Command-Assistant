# 현재 목표
자기 피드백 루프 구현 (설계 문서: https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

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
- 2026-10-04 코드 리뷰 및 Claude Code 전달 자료 작성 (검토 기준 `f3578ea`)
  - 문서: `docs/reviews/2026-10-04-claude-code-review.md`
  - 재현: `docs/reviews/reproduce_feedback_review.py` (합성 데이터·임시 파일만 사용)
  - F01~F10: 민감 기록/JSON 손상, 실패 격리, 분석 run 연결, lesson 시점·재발 측정,
    DB lock·symlink, overwrite 재승인 누락(기존 결함), 취소 분석의 quota 누락
  - production 코드는 변경하지 않음. 수정 방향·영향도·필수 회귀·변경 단위를 문서에 정리
- 테스트 자료 11개 추가: 회의록 3개(하위 폴더 포함), 요구사항·제품 비교·번역·문서 정리,
  덮어쓰기·삭제 2개·문서 내 지시 안전성 연습. 기존 2개와 합쳐 13개
  - interactive 명령 예시와 예상 결과: `docs/test-data.md`

# 검증
- 2026-10-04: pytest 344 passed (FORCE_COLOR 해제 시), ruff All checks passed
- 실제 OpenRouter 분석 호출 1회 성공 (9.4초, 스키마 통과)
- 실제 Groq `--text` 실행에서 이벤트 기록 확인, DB 안 API 키 0건
- 리뷰 재검증: UI 제외 pytest 305 passed, 22 deselected (27.67초), Ruff 통과
  - 로컬 HTTP bind 샌드박스 차단으로 초기 9 setup errors; 권한 승인 후 동일 테스트 통과
- 리뷰 재현 스크립트: F01~F10 결함 확인, interactive 인수 통과
  - 실제 stdin pipe + Filesystem MCP, scripted LLM/fake analyzer
  - 목록/읽기/create 승인/거절/overwrite Undo/삭제 Undo/반복 오류 분석/exit
  - UI 실행 및 실제 외부 LLM API 호출은 이번 리뷰에서 하지 않음
- 샘플 자료 검증: 전체 13개 UTF-8 읽기/빈 내용 없음, 회의록 후보 3개, 삭제 대상 2개,
  덮어쓰기 원본 표식 확인. 기존 product.md/expenses.csv 무변경, diff whitespace 검사 통과
  - 자료만 추가했으며 앱 실행·실제 모델 응답 검증은 재실행하지 않음

# 다음 작업
- Claude Code: 리뷰의 P1/P2 항목을 회귀 테스트로 고정하고 최소 수정. 실행 인수는 interactive 사용
- M5: 2주 사용 후 incident 수, lesson 적용 후 재발률, 분석 비용을 보고 임계값 조정
  - 운영 지표 평가 전에 기록 안전성과 incident/lesson 귀속 문제를 먼저 해결할 것을 권장

# 중요한 결정
- `.voice-action/undo.sqlite3`는 원본에서 복사되어 원본 경로를 가리키는 항목이 있다.
  깨끗한 테스트가 필요하면 삭제해도 된다 (git 추적 대상 아님).
- 피드백 루프는 메인 LLM(Groq) 설정과 호출을 바꾸지 않는다. 분석에만 OpenRouter를 쓴다.
- 홈 `~/.voice-action/config.json`의 `model: anthropic/claude-3-haiku`는 프로젝트 `config.json`이
  덮어쓴다. `config.json`이 없는 복사본(예: worktree)에서 실행하면 Groq가 404를 낸다.
- 테스트는 conftest의 autouse fixture로 피드백 DB를 tmp_path에 둔다.

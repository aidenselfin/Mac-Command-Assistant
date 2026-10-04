# 진행 중
- Claude Code, 2026-10-04 23:37, 자가 개선 지침서 2단계(평가 과제 추가)·3단계(lesson 후보→채택),
  evals/tasks/*.json·voice_action/feedback.py·main.py·tests/test_feedback*.py·docs/status.md
<!-- 예: Codex, 2026-10-05 10:00, 변환 오류 메시지 개선, voice_action/conversion.py·tests/test_conversion.py -->

# 현재 목표
자기 피드백 루프 운영(M5)과 파일 변환·문서 읽기 도구 안정화
(피드백 루프 설계 문서: https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

# 최근 검증 (2026-10-04, `273000f` 기준)
- pytest 430 passed (UI 포함, GUI 수동 확인 없음), ruff 통과
- 인수 `docs/reviews/acceptance_after_fixes.py` 17/17, `acceptance_recent_changes.py` 17/17,
  `evals/run_eval.py` 재생 8/8
- 실제 외부 API smoke는 리뷰 수정 이후 다시 하지 않음

# 다음 작업
- 실제 OpenRouter 모델로 변환·문서 읽기 smoke와 `evals/run_eval.py --live` (비용 발생, 미실행)
- 실제 API smoke 재실행 (임시 cwd·allowed root·journal·feedback DB에서)
- 변환 쓰기의 디렉터리 fd 기준 open 등 더 강한 경합 방어는 별도 검토
- M5: 2주 사용 후 lesson별 노출 run 수, 재발률, 오탐률, 분석 요청 수로 임계값 조정
- 남은 문제: `docs/reviews/2026-10-04-fixes.md`, `docs/reviews/2026-10-04-recent-changes-fixes.md`

# 인수인계
- `voice-action-test/`에 사용자가 추가한 미추적 파일(IMG_9441.HEIC/.jpg, 회의록-2026-10-01.txt)이 있다.
  사용자 자료이므로 커밋하거나 지우지 않는다.

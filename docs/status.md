# 진행 중
- (없음)
<!-- 예: Codex, 2026-10-05 10:00, 변환 오류 메시지 개선, voice_action/conversion.py·tests/test_conversion.py -->

# 현재 목표
자가 개선 루프: 평가 기준선(`--live`)을 재고, lesson 후보를 평가로 비교해 채택한다
(지침서 `docs/self-improvement-guide.md`, 피드백 루프 설계:
https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

# 최근 검증 (2026-10-04, `6224adf` 기준)
- pytest 438 passed (UI 포함, GUI 수동 확인 없음), ruff 통과
- `evals/run_eval.py` 재생 22/22 (새 negative 4개 채점기 확인), `--lesson` 주입 확인
- 실제 외부 API(`--live`, smoke)는 실행하지 않음 (과금, 사용자 확인 필요)

# 다음 작업
- 사용자 확인 후 `uv run python evals/run_eval.py --live --repeat 3 2>/dev/null`로 기준선 측정
  (22과제 중 negative 6개 제외 16과제 × 3회 × 평균 2~3회 모델 호출 ≈ 100~150회)
- 후보 lesson이 생기면 지침서 5.2절대로 baseline/candidate 비교 후 `--lesson-on`/`--lesson-off`
- 변환 쓰기의 디렉터리 fd 기준 open 등 더 강한 경합 방어는 별도 검토
- 남은 문제: `docs/reviews/2026-10-04-fixes.md`, `docs/reviews/2026-10-04-recent-changes-fixes.md`

# 인수인계
- `voice-action-test/`에 사용자가 추가한 미추적 파일(IMG_9441.HEIC/.jpg, 회의록-2026-10-01.txt)이 있다.
  사용자 자료이므로 커밋하거나 지우지 않는다.

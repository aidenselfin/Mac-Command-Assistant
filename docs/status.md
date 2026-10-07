# 진행 중
- (없음)

# 현재 목표
제품: 자가 개선루프의 기준선과 lesson 후보 비교·채택. 지침은 `docs/self-improvement-guide.md`.
협업 도구: 현재 폴더 전용 자동 보존·수정 세션 조정 설치 완료.

# 최근 검증
- 제품의 마지막 보고: 2026-10-07 Finder 연동 pytest 558 passed, ruff 통과, 평가 재생 34/34.
  이번 협업 도구 수정으로 제품 전체 검증을 다시 수행한 것은 아니다.
  상세 근거는 [정리 전 상태 보존](reviews/2026-10-07-vibe-status-archive.md).
- 2026-10-07 협업 도구: `pytest -q tests/test_vibe.py` 1 passed(동시 소유권·잘못된 token·
  시작 전 상태·새 파일·삭제 복구·무관한 자료 제외·HEAD/index 불변·경로 검사 포함).
  ruff 전체·shell 구문·plist 구문·diff check 통과. 제품 전체 테스트·live 평가는 재실행하지 않았다.
  테스트 전용 LaunchAgent 영구 설치, 첫 실행 exit 0·별도 ref 생성 확인. 재로그인은 직접 확인하지 않았다.

# 다음 작업
- Finder 실제 드래그, 후보 버튼·평가·원인 칩의 사람 GUI 확인.
- [GUI 후속 리뷰](reviews/2026-10-07-ui-progress-followup-review.md)의 R1~R4 및 공급자 HTTP 400 원인 검토.
- 삭제·변환의 텍스트 승인 질문 종료를 lesson 후보 비교로 다룬다. 후보는 아직 채택하지 않았다.
- 의미 검색·한/영 대체어·pdf/docx 본문 검색은 미구현이다. 추가 요구가 있을 때 범위를 정한다.
- 장기 보류: HTML 반복 읽기·호출 한도 소진, 변환 쓰기의 fd 기준 경합 방어, Finder 창 배치.

# 인수인계
- 모든 세션은 AGENTS.md·이 문서·decisions.md를 재독한다. 수정 소유권의 원본은 `vibe.py status`다.
- 협업 도구 설치·복구·지시 예시는 [바이브 코딩 사용법](vibe-coding.md).
- 과거 결과는 위 보존 문서의 측정 시점 기준이다. 수정 전 결함을 단언하는 reproduce 스크립트의
  성공/실패를 현재 제품 상태의 판정으로 사용하지 않는다. 정식 회귀 테스트를 따른다.
- 원래 미추적 사용자 자료 및 다른 세션이 만든 voice-action-test 자료는 커밋하거나 지우지 않는다.
- `.voice-action/` DB와 `evals/results/` 원본은 git 제외다. 피드백 lesson은 후보로 남긴다.
- 메인 모델 Groq gpt-oss-120b, 분석 모델 OpenRouter Claude Sonnet 5.5. 세부 결정은 decisions.md.

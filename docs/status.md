# 현재 목표
테스트용 복제본에서 기존 프로그램 수정 시작 (구체 목표 미정)

# 완료
- 원본 `voice-action-AI` 복제 (.git, node_modules, .venv, 캐시, 영상 제외)
- 의존성 설치, AGENTS.md / CLAUDE.md / docs/status.md 작성
- git 초기화 및 기준 커밋

# 검증
- 2026-10-04: pytest 333 passed (FORCE_COLOR 해제 시), ruff All checks passed

# 다음 작업
- 수정할 기능 결정

# 중요한 결정
- `.voice-action/undo.sqlite3`는 원본에서 복사되어 원본 경로를 가리키는 항목이 있다.
  깨끗한 테스트가 필요하면 삭제해도 된다 (git 추적 대상 아님).

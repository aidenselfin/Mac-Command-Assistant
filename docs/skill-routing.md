# 스킬 호출 규칙 (상황 → 스킬)

개발 상황에서 아래 스킬을 먼저 부른다. 스킬이 길게 쓰라고 해도 응답 규칙(그래프, 글 2줄)이 우선이다.

| 상황 | 먼저 부를 스킬 | 이 프로젝트에서 |
|---|---|---|
| 동작이 바뀌는 새 기능·큰 변경 | `superpowers:brainstorming` → `superpowers:writing-plans` | 안전 경계(허용 폴더·승인)를 먼저 합의 |
| 새 도구 추가·위험 분류 변경 | `add-local-tool` + `superpowers:test-driven-development` | risk_guard·승인·Undo·eval·features.md |
| 버그·테스트 실패·eval INVALID | `superpowers:systematic-debugging` | 원인 확인 전 수정하지 않는다 |
| 프롬프트·도구 노출 변경 후 성공률 확인 | `run-eval` | 재생 42/42 → live는 비용 확인 후 |
| 한두 파일 작은 수정·정리 | `ponytail:ponytail` | 무관한 리팩터링 금지 |
| 완료 보고 직전 | `superpowers:verification-before-completion` | `scripts/check.sh` 실제 출력만 보고 |
| 안전 경계·보안 관련 변경 | `security-review` → `code-review` | risk_guard·file 접근·웹 URL 규칙 |
| 작업 시작·진행·종료 | `update-work-records` | worklog·architecture·tools JSON, 세션 제목 |
| "골라서 한 번에 합쳐줘" | `merge-worktrees` | 통합 세션만 main에 반영 |

쓰지 않는 것: `superpowers:using-git-worktrees`(→ `scripts/vibe.py worktree`가 대신함), 오타·문구 수정의 brainstorming.

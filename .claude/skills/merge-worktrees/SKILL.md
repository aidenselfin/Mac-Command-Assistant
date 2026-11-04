---
name: merge-worktrees
description: 사용자가 "메인에 병합해"(지금 세션의 브랜치) 또는 "괜찮은 것만 골라 한 번에 합쳐줘"(여러 브랜치)라고 할 때 쓰는 voice-action 병합 절차. 병합·점검·worktree 삭제·패널 정리까지 한 번에 한다.
---

# 통합(병합) 절차

통합은 사용자가 요청할 때, 한 번에 한 세션이 한다. 작업 세션은 사용자가 "메인에 병합해"라고 하기 전에는 `main`에 합치지 않는다. 여러 브랜치를 합칠 때는 통합 세션 한 곳이 하고, push는 사용자가 말했을 때만 한다.

1. 시작: `python3 scripts/vibe.py sessions`와 `status`로 열린 세션을 본다. 메인 폴더에 다른 세션이 있거나 커밋 안 한 변경이 있으면 끝난 뒤로 미룬다.
2. 후보 점검: 브랜치마다 `git log main..<브랜치>`와 `git diff main...<브랜치> --stat`로 범위를 보고 사용자가 고른 것만 대상으로 한다. 뒤처짐이 크면 먼저 최신 `main`을 가져와 충돌을 푼다.
3. 병합: 통합용 브랜치에서 하나씩 병합한다. 충돌은 통합 세션이 푼다. 문서(`docs/status.md`·`features.md`)는 양쪽 내용을 모두 살린다.
4. 검증: `scripts/check.sh full`(pytest 전체 + ruff)과 `uv run python evals/run_eval.py` 재생을 통과시킨다. 네이티브 변경이 있으면 `scripts/check.sh native`. 직접 보지 못한 UI는 확인했다고 쓰지 않는다.
5. 반영: 통과한 뒤에만 `main`에 반영한다. push는 사용자가 말했을 때만 한다.
   (지침 파일 추적을 처음 합칠 때: 메인에 untracked로 남은 `CLAUDE.md`·`AGENTS.md`·`native-preview/AGENTS.md`·`docs/내 작업 방식.md`가 병합 내용과 같은지 보고 지운 뒤 병합한다. 다른 worktree에 걸린 `AGENTS.md` 링크도 지운다.)
6. 기록: `update-work-records`로 각 항목을 "완료", `todo`를 비우고 큰 변경은 milestones에 추가한다. `docs/status.md`의 최근 검증을 갱신한다.
7. 정리: 병합한 브랜치만 삭제하고 끝난 worktree는 `git worktree remove 경로`로 지운다.

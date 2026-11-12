---
name: merge-worktrees
description: 사용자가 "메인에 병합해"(지금 세션의 브랜치) 또는 "괜찮은 것만 골라 한 번에 합쳐줘"(여러 브랜치)라고 할 때 쓰는 voice-action 병합 절차. 병합·점검·worktree 삭제·웹 패널 정리까지 한 번에 한다.
---

# 병합 절차

사용자가 말했을 때만 한다. "메인에 병합해"는 지금 세션의 브랜치 하나, "골라 합쳐줘"는 사용자가 고른 브랜치들이 대상이다.
메인 폴더 `M=~/voice-action-AI`에서는 `git -C $M …`로만 다루고 브랜치를 바꾸지 않는다.

1. 준비: 대상 브랜치의 변경을 모두 커밋한다. `git -C $M status --short`로 메인의 남은 변경을 본다. 사용자의 변경은 건드리지 않되, 병합할 파일과 겹치면 멈추고 묻는다.
   메인에만 있는 같은 이름의 untracked 파일(예: 지침 문서 사본)이 막으면 내용을 비교해 브랜치 쪽에 이미 담겼을 때만 치운다.
2. 최신화: 브랜치에서 `git merge main`으로 최신 `main`을 먼저 합치고 충돌을 푼다. 문서(`docs/status.md`·`features.md`)는 양쪽 내용을 모두 살린다.
3. 점검: 코드(.swift·.sh)가 바뀌었으면 `scripts/check.sh full`(= `symbio-dev check`)을 통과시킨다. 네이티브 변경이 있으면 `scripts/check.sh native`와 `swift run --package-path backend/Dev symbio-dev ui-check run`(창·칩 시나리오 재생, 몇 초간 창이 깜빡인다). 지침·문서만 바뀌었으면 건너뛴다. 직접 보지 못한 UI는 확인했다고 쓰지 않는다.
   화면 동작을 새로 넣었으면 `scripts/ui_scenarios/`에 시나리오를 하나 더한다.
4. 반영: `git -C $M merge --no-ff <브랜치>`. 여러 브랜치면 하나씩 하고 매번 3번을 다시 본다. push는 사용자가 말했을 때만 한다.
   `native-preview/`나 `backend/`가 바뀌었으면 마지막 병합 뒤 `$M/native-preview/install-app.sh`로 앱과 백엔드를 다시 빌드·설치하고 `swift run --package-path $M/backend/Dev symbio-dev ui-check status`가 "최신"인지 본다. 안 하면 사용자는 옛 화면을 계속 본다.
5. 웹 패널 정리(`~/.claude/voice-action/`, 자기 항목만 Edit):
   - `worklog.json`: 브랜치 항목을 `archive`로 옮겨 카드를 없앤다. 큰 변경이면 `milestones["<병합 커밋 해시 7자리>"]`에 짧은 제목.
   - 새 큰 기능(새 도구·화면·묶음)이면 `structure-map.json` 알맞은 묶음에 블록(`added` 오늘 날짜)을 추가하고, 기능 파일이 바뀌었으면 `architecture.json` `features`를 고친 뒤 `update-architecture.py`를 돌린다.
   - `docs/status.md`의 최근 검증을 갱신해야 하면 4번 전에 브랜치에서 커밋한다.
   - `curl -s localhost:8765/api/state`로 반영됐는지 본다.
6. 삭제(마지막): `git -C $M worktree remove --force <worktree 경로>` 뒤 `git -C $M branch -d <브랜치>`. 지금 세션이 그 worktree 안에 있으면 이 단계 뒤에는 명령을 더 돌리지 않는다.

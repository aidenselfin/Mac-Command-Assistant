---
name: update-work-records
description: voice-action 작업을 시작·진행·종료할 때 웹 패널이 읽는 기록(worklog·structure-map·architecture·tools·dock-notes JSON)과 세션 제목을 갱신한다.
---

# 작업 기록 갱신

패널 `http://127.0.0.1:8765`은 `/Users/juhyeon/.claude/voice-action/` 아래 파일만 3초마다 읽는다. git 밖이다. 패널 코드(`dock-web/`)는 고치지 않고 기록만 갱신한다. 절대 경로로 열고 Edit로 자기 항목만 고친다(통째 Write 금지). 기록은 코드를 고친 같은 작업 안에서 끝내기 전에 한다.

1. `worklog.json`의 `worktrees["<키>"]`: 키는 브랜치에서 `claude/`·`work/`·`codex/`를 뗀 이름(detached면 폴더 이름). 필드 `title`·`status`("작업 중"|"완료"|"보류")·`updated`·`done[]`·`todo[]`, 하위 세션은 `parent`. 시작할 때 만들고 단계마다·끝낼 때 고친다.
2. 글은 짧은 명사형 일반어로 쓴다(`title`·`done`·`todo` 10자 안팎, 파일명·커밋 용어 없이). `todo`는 규모가 보이게 쓴다.
3. 범위가 처음 제목과 달라지면 묻지 말고 `title`과 세션 제목(`set_session_title`, `session_id: "self"`)을 같은 문장으로 고친다.
4. main에 합쳐지면 항목을 `archive`로 옮겨 카드를 없앤다. 큰 변경은 `milestones["<해시 7자리>"]`에 짧은 제목을 남긴다.
5. worktree 없는 환경 개선(지침·훅·패널·스킬)은 `env["<짧은 영문 키>"]`에 같은 필드로 적고 끝나면 `status`를 "완료"로 바꾼다. 오늘 "작업 중" 항목이 없으면 훅 `require-worklog.py`가 수정을 막는다.
6. `structure-map.json`: 큰 기능(새 도구·화면·묶음)을 main에 합치면 알맞은 묶음에 블록(`title`·한 줄 `role`·`added` YYYY-MM-DD·`lines`)을 추가한다(7일간 NEW). 사소한 수정은 적지 않는다.
7. `architecture.json`: 기능을 추가·삭제·이동하면 `features`에 `id`·`title`·`desc`·`color`·`files`를 적고 `python3 ~/.claude/voice-action/update-architecture.py`를 돌린다. `rows`·`details`는 손대지 않는다. 능력 점수를 새로 재면 `extra.score`를 고친다.
8. `tools.json`: 지침·훅·스킬·위임 도구를 바꾸면 해당 항목을 쉬운 말로(`name` 10자, `sub` 15자, 지침 묶음 30자) 고치고 맨 위 `updated`를 오늘로.
9. `dock-notes.json`: 코드(.py·.swift·.sh)를 20줄 이상 바꾸면 `notes` 맨 앞에 1항목(`date` MM/DD·`title`·`how` 1~2문장).
10. 끝내기 전 `curl -s localhost:8765/api/state`에 반영됐는지 본다. 안 보이면 JSON 문법부터 의심하고, 서버가 꺼졌으면 `launchctl kickstart -k gui/$(id -u)/com.juhyeon.symbio-dock`.
11. 자세한 인수인계는 `docs/status.md`, 기능 표는 `docs/features.md`·`docs/ARCHITECTURE.md`에 쓴다.

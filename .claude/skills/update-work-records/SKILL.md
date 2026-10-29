---
name: update-work-records
description: voice-action 작업을 시작·진행·종료할 때 사이드 패널이 읽는 작업 기록(worklog)·프로그램 구조(architecture)·개발 도구(tools) JSON과 세션 제목을 갱신한다.
---

# 작업 기록 갱신

모두 `/Users/juhyeon/.claude/voice-action/` 아래 파일이며 git 밖이다. 절대 경로로 열고 Edit로 자기 항목만 고친다.

1. `worklog.json`의 `worktrees["<키>"]`: 키는 브랜치에서 `claude/`·`work/`·`codex/`를 뗀 이름. 필드는 `title`(한 줄)·`status`("작업 중"|"완료"|"보류")·`updated`(날짜)·`done[]`·`todo[]`. 시작할 때 만들고, 단계마다·끝낼 때 고친다.
2. 글은 일반 한국어로 쓴다. 파일 이름이나 커밋 용어를 쓰지 않는다. `todo`는 규모가 보이게 구체적으로 쓴다.
3. 작업 범위가 처음 제목과 달라지면 `title`과 세션 제목(`set_session_title`, `session_id: "self"`)을 같은 문장으로 고친다.
4. main에 합쳐지면 항목을 `archive`로 옮겨 패널 카드를 없앤다(`merge-worktrees` 5번). 큰 변경이면 `milestones["<해시 7자리>"]`에 일반어 제목을 추가한다. 메인에서 지침만 고친 작업은 항목을 만들지 않는다.
5. 구조가 달라지면 같은 작업에서 `architecture.json`의 칸과 `details`를 고치고 `updated`를 오늘 날짜로 바꾼다.
6. 개발 도구(스킬·훅·스크립트)를 바꾸면 `tools.json`의 해당 항목 `sub`(40자 안팎)와 `updated`를 고친다.
7. 자세한 인수인계는 `docs/status.md`, 기능 표는 `docs/features.md`, `docs/ARCHITECTURE.md`에 따로 쓴다.

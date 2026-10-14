# 바이브 코딩 사용법

작업 규칙의 원본은 `AGENTS.md`, 현재 상태는 `docs/status.md`, 설계 결정은 `docs/decisions.md`다.
Claude는 `CLAUDE.md`의 `@AGENTS.md`를 통해 같은 규칙을 읽는다.
다른 도구에는 작업 시작 시 AGENTS.md를 읽으라고 명시한다.

## 에이전트에게 지시하기

수정 요청 예시:

> AGENTS.md·status.md·decisions.md를 읽고 수정 세션을 확보한 뒤,
> [문제와 원하는 결과]를 구현해줘. 관련 테스트와 린트를 확인하고,
> 내 변경만 커밋한 뒤 상태 갱신·세션 해제까지 해줘.
> 요청과 무관한 변경과 사용자 자료 수정은 하지 마.

리뷰 요청 예시:

> [커밋 hash]를 기준으로 원인을 분석해줘. 파일 수정·테스트·앱 실행은 금지.
> 다른 수정 세션과 병행 중이므로 기준 이후 변경은 구분해서 설명해줘.

- 목표·완료 조건을 구체적으로 준다. 예: “첨부한 PDF의 요약 결과가 UI에 나타나야 한다.”
- 같은 폴더에서는 수정을 한 세션에만 맡긴다. 동시에 여러 작업을 시키려면 세션마다 worktree를 만든다:
  `python3 scripts/vibe.py worktree --name 작업명`(AGENTS.md "병렬 작업"). 폴더가 다르면 잠금도
  따로이고, 같은 파일을 지정하면 `begin`이 경고한다. 다른 세션은 읽기 리뷰·설계 검토도 맡길 수 있다.
  테스트·live 평가·앱 실행은 파일이나 DB를 만들 수 있으므로 수정 세션에서 수행한다.
- 병렬 작업의 통합은 "작업명 브랜치를 main에 통합해줘"처럼 따로 지시한다.
- 같은 수정 세션에 후속 요구를 이어서 전달한다. 다른 세션으로 넘길 때는 이전 세션의
  커밋·잠금 해제를 확인하고 커밋 hash를 전달한다.
- 세션이 갑자기 끊겨도 다른 에이전트에게 잠금을 지우라고 하지 않는다.
  `python3 scripts/vibe.py status`로 소유자·목표를 보고 실제 작업 종료를 확인한다.
  소유자가 종료했음을 확인한 후에만 표시된 token으로 `end`하여 인계한다.
- 기존 대화 세션은 새 규칙을 자동으로 재독한다고 가정하지 않는다. 규칙을 다시 읽으라고 지시한다.

## 보존과 복구

`scripts/local-git-autosave.sh`는 별도 임시 index로 Git snapshot을 만든다.
작업 브랜치·HEAD·사용자의 staging을 변경하지 않는다. 기존 추적 파일과
현재 수정 세션의 `--files`에 지정한 새 파일만 보존한다. 무관한 미추적 자료는 저장하지 않는다.
gitignore 대상(API 키·로컬 설정·DB·평가 원본 등)은 보존 대상이 아니다.
새 파일은 개별 경로를 사전에 등록해야 한다. 정상 완료 이력은 여전히 기능별 커밋이 맡는다.

```sh
python3 scripts/vibe.py status
python3 scripts/vibe.py snapshot
git log --oneline refs/vibe/snapshots
git diff SNAPSHOT_HASH -- path/to/file
# 복구할 내용을 먼저 별도 임시 파일로 확인한다.
git show SNAPSHOT_HASH:path/to/file > /private/tmp/vibe-recovered.txt
```

전체 `git reset --hard` 대신 필요한 파일만 검토 후 복원한다. 복원도 수정 세션에서 수행한다.
별도 ref는 기본 push 대상이 아니므로 이 보존은 로컬 복구용이며 외부 백업은 아니다.
60초 사이의 수정과 파일을 읽는 도중의 변경은 완전한 수정별 이력을 보장하지 않는다.
큰 수정 전후 snapshot을 남긴다. 잠금은 규칙을 따르는 세션의 충돌을 방지하며,
규칙을 무시한 직접 파일 수정까지 막지는 않는다.

## macOS 자동 실행

2026-10-07 테스트 폴더 전용 서비스를 `~/Library/LaunchAgents/`에 설치하고 활성화했다.
재로그인 후에도 자동 시작하며, 원본 폴더의 서비스는 건드리지 않았다.
로그인 세션에서 아래 서비스를 등록하면 60초마다 실행된다.

```sh
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.aidenselfin.voice-action-ai-test.autosave.plist"
launchctl print gui/$(id -u)/com.aidenselfin.voice-action-ai-test.autosave
```

이미 등록된 서비스의 설정을 바꾸려면 해당 테스트 서비스만 bootout 후 bootstrap한다.
위 명령은 재등록 예시다. 영구 설치 파일의 경로는
`~/Library/LaunchAgents/com.aidenselfin.voice-action-ai-test.autosave.plist`다.
폴더를 옮기면 저장소와 설치 파일의 plist 경로를 모두 갱신하고 서비스를 다시 등록한다.
로그는 `/private/tmp/voice-action-ai-test-autosave*.log`에 남는다.

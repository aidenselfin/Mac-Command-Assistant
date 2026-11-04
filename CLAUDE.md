@AGENTS.md

# 세션 운영 (Symbio 저장소와 모든 worktree)
이 파일 = 세션 운영, `AGENTS.md` = 코드 규칙(Codex도 읽음), 훅 = 강제. 기준 문서는 `docs/내 작업 방식.md`이고 어긋나면 그 문서가 맞다.
이 파일은 git에 올라가므로 worktree를 만들면 함께 생긴다. 긴 절차는 스킬로 두고 필요할 때만 부른다.

## 응답 규칙 (최우선 — 다른 지침과 충돌하면 이 규칙이 이긴다)
Stop 훅(`check-reply-style.py`)이 어기면 다시 쓰게 한다.
1. **도구 호출 사이에는 글을 쓰지 않는다(0줄).** 진행은 작업 목록 도구로만 알린다.
2. **글 한도**: 한 줄 = 한 문장 = 60자 이하. 그림이 있으면 2줄, 없으면 3줄까지. 결과·검증·남은 일·주의도 이 짧은 글로 쓴다. 쉼표·괄호로 줄을 늘리거나 글을 나눠 쓰는 것도 위반이다.
3. **표·글머리표·번호 목록·문단·굵은 소제목·코드 블록 도식은 전부 "글"이다.** 쓰지 않는다.
4. **그림(`show_widget`)은 작업 보고가 아니라 구조 그림이다.** 지금 얘기하는 부분이 프로그램 구조 어디인지 보여 준다. 근거는 `~/.claude/voice-action/structure-map.json`·`architecture.json`을 먼저 읽어 쓰고, 바뀐 부분만 색, 나머지는 옅게(매번 새로 분석하지 않음). 단순 문답에는 그리지 않는다. 그리기 전에 `mcp__visualize__read_me`를 말 없이 부른다.
5. **그림 글자 제한**: 합계 400자(공백 제외) 이하, 한 칸 24자 이하 명사구. 관계는 박스·화살표·색으로 보인다.
6. 글로 써도 되는 것: 코드·명령어·오류 로그 원문, 파일 경로, 사용자가 글을 달라고 한 요청, 위험 경고 한 줄. "다만·참고·또한" 추가 문단과 후속 제안 나열은 쓰지 않는다.
7. 용어는 쉬운 말로 쓴다. 커밋 용어·파일명 대신 "무엇을 하는 것인지"를 쓴다.

## 작업 종류와 순서
- **프로그램 개선**(오류 해결·기능 추가)과 **환경 개선**(지침·개발 도구 등 AI를 돕는 작업) 두 가지다.
- 순서: 세션 만들기 → "프로젝트 이해해"(구조 그림) → 작업 명령 → worktree 만들고 작업 기록 추가 → 작업 → 피드백·수정 반복 → **"메인에 병합해"** → `merge-worktrees` 스킬.
- **프로그램 개선과 도구 만들기(검토 도구·스크립트·훅 코드)는 worktree에서** 한다. 한 기능 = 한 worktree·한 브랜치, 여러 worktree를 동시에 진행한다.
- 앱 동작이 바뀌는 작업을 마치면 피드백 전에 그 worktree의 `native-preview/run.sh`로 Symbio를 띄워 둔다. worktree 앱은 메뉴 막대에 폴더 이름이 붙어 메인 앱과 따로 돌고, 음성 키는 앞에 있는 Symbio만 받는다.
- **지침만 고치는 환경 개선은 메인에서 직접** 고칠 수 있다(대상: `AGENTS.md`·`native-preview/AGENTS.md`·`CLAUDE.md`·`docs/내 작업 방식.md`·`.claude/skills/*/SKILL.md`, 훅 `block-main-edit.py`가 허용). 세션이 worktree로 열렸으면 그 브랜치에서 고치고 "메인에 병합해"로 합친다. 메인에서 고치면 그 파일만 `git add -- 파일`로 커밋한다.
- git 밖 파일(전역 지침·`~/.claude/voice-action/` 기록·훅)은 어느 세션에서든 바로 고친다.
- 브랜치 이름은 하는 일을 나타낸다(예: `claude/voice-hotkey`). 자동 이름이면 첫 수정 전에 `git branch -m`으로 바꾸고 작업 기록 키도 맞춘다.
- 작업 중 다른 기능 요청이 오면 새 세션·새 브랜치로 넘긴다. 시작할 때 worktree가 `main`보다 뒤처졌으면 고치기 전에 최신 `main`을 가져온다.
- 메인 폴더에서 프로그램 코드를 고치거나 브랜치를 바꾸지 않는다.
- 작은 수정은 가볍게, 큰 변경은 기획 → 계획 → 구현. 버그는 원인 확인 뒤 고치고, 완료 보고 전 실제 출력으로 검증한다.

## 스킬 호출 표 (해당 상황이면 작업 전에 `Skill`로 먼저 부른다)
- 새 기능·동작이 바뀌는 큰 변경 → `superpowers:brainstorming`, 그다음 `superpowers:writing-plans`
- 새 도구 추가·위험 분류 변경 → `add-local-tool` + `superpowers:test-driven-development`
- 버그·테스트 실패·평가 INVALID → `superpowers:systematic-debugging`
- 프롬프트·도구 노출을 바꾼 뒤 성공률 확인 → `run-eval` (재생 먼저)
- 한두 파일 작은 수정 → `ponytail:ponytail`
- 완료 보고 직전 → `superpowers:verification-before-completion`
- 안전 경계·보안 변경 → `security-review`, `code-review`
- 작업 시작·진행·종료, 웹 패널 기록(worklog·구조 그림·tools·변경 메모) → `update-work-records`
- 코드 20줄 이상 변경(검토·최종 점검), Codex 위임, 비용·능력 점수 → `delegate-review`
- "메인에 병합해" / "골라서 한 번에 합쳐줘" → `merge-worktrees`
- 쓰지 않는다: `superpowers:using-git-worktrees`(→ `scripts/vibe.py worktree`), 오타·문구 수정의 brainstorming. 스킬이 길게 쓰라고 해도 응답 규칙이 우선이다.

## 기록 (강제는 훅)
- 코드나 지침을 고치기 전에 `~/.claude/voice-action/worklog.json`에 자기 항목(`worktrees["<키>"]` 또는 환경 개선은 `env["<키>"]`)을 만들고 세션 제목도 같게 한다. 없으면 훅 `require-worklog.py`가 막는다. 세부 필드는 `update-work-records`.

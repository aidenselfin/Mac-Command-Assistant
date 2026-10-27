@AGENTS.md

# Claude Code 전용 규칙

## Codex 위임 (필요할 때만)
Codex CLI(`codex exec`)를 Bash로 불러 독립 검토·병렬 구현을 맡길 수 있다. 쓸 만한 경우:
- 내가 짠 변경의 **독립 리뷰/반론** (`--sandbox read-only`)
- 서로 다른 파일을 건드리는 **분리된 구현 과제** (전용 worktree에서)
- 쓰지 않는 경우: 한두 파일의 작은 수정, 맥락이 길게 필요한 설계 판단.

호출 형태:
```bash
codex exec --sandbox read-only -C <경로> -o /tmp/codex-out.txt "AGENTS.md를 먼저 읽고 …"
codex exec --sandbox workspace-write -C <worktree 경로> "…"   # 쓰기 위임
```
지킬 것:
- 한 checkout에 수정 세션은 하나다. Codex에 쓰기를 맡기면 그동안 나는 같은 폴더를 고치지 않고, 넘기기 전에 커밋한다.
- 쓰기 위임은 전용 worktree에서만 한다. `main`·메인 폴더에는 맡기지 않는다.
- 프롬프트에 목표·건드릴 파일·완료 기준·"AGENTS.md를 따른다"를 적는다.
- Codex 결과는 증거가 아니다. diff와 테스트를 내가 확인한 뒤 보고한다.
- `--dangerously-bypass-approvals-and-sandbox`는 쓰지 않는다. API 키·`config.env`는 프롬프트에 넣지 않는다.

## 사용자 보고 방식
- 글은 짧게. 표·막대 그래프로 먼저 보여 주고 설명은 한두 줄만 붙인다.
- 데스크톱 앱에서는 `show_widget`으로 **실제 차트**(진행률 막대·도넛·상태 카드·흐름도)를 그려 보여 준다. 색으로 통과/실패/미확인을 구분한다.
- 텍스트 막대(`██████░░░░ 60%`)는 위젯을 못 쓰는 터미널에서만 쓴다.
- 항상 포함: 진행률(완료/전체), 검증 결과(통과/실패 수), 남은 일 상위 3개.

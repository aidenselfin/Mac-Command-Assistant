---
name: delegate-review
description: Symbio 코드를 20줄 이상 바꿨을 때의 검토·최종 점검, Codex 위임, 5시간 사용률 기반 분배, 능력 점수 측정 비용 규칙. 코드 변경을 끝내기 전이나 일을 다른 AI에 맡기기 전에 쓴다.
---

# 비용·위임·검토

- 능력 점수(`symbio-dev benchmark`, `symbio-dev eval --live`)는 웬만하면 재지 않는다(모델 호출이 많아 컴퓨터가 뜨거워진다). 무엇을 보강할지 모를 때만 기존 기록(`evals/benchmarks/`)을 먼저 읽고 잰다. 평소 검증은 무료 재생 평가.
- 위임 전에 `~/.claude/voice-action/usage.json`의 `plan.claude`·`plan.codex` 5시간 사용률을 읽는다(없거나 `resetsAt`이 지났으면 0%). 낮은 쪽에 더 맡기고, 차이 20%p 미만이면 평소대로, 한쪽이 80% 이상이면 다른 쪽으로 넘긴다. `get_usage`를 쓸 수 있으면 시작할 때 한 번 불러 `plan.claude`를 갱신한다.
- Codex 위임 도구: `python3 ~/.claude/hooks/codex-delegate.py review|implement --repo <worktree> --goal "<목표>" [--files … --done "<완료 기준>"]`(맥락 자동 첨부, 결과는 `~/.claude/voice-action/reviews/`). `codex exec`을 직접 부르지 않는다.
- **검토**: 코드(.py·.swift·.sh)를 20줄 이상 바꾸면 끝내기 전 review를 한 번 돌리고 지적은 고치거나 근거로 반박한다(Stop 훅 `auto-review.py`가 강제).
- **구현 위임**: 서로 다른 파일의 독립 과제(테스트 추가·반복 수정)는 먼저 커밋한 뒤 worktree에서 맡기고, `git diff`·테스트를 내가 확인하고 커밋한다. 한두 줄 수정·설계 판단·긴 맥락은 맡기지 않는다. Codex 결과는 증거가 아니다.
- **끝내기 전 점검**: 코드를 20줄 이상 바꿨으면 마지막 수정 뒤 `scripts/check.sh full`(= `swift run --package-path backend/Dev symbio-dev check`)과 `superpowers:verification-before-completion`(Stop 훅 `final-check.py`가 강제). 새 테스트는 `symbio-dev make-cases`로 어려운 케이스도 만든다(`check`가 함께 돌린다).

---
name: run-eval
description: voice-action의 evals/run_eval.py 재생·live 평가를 돌리고 결과를 기준선과 비교할 때 쓴다. 프롬프트·도구 노출·planner를 바꾼 뒤 성공률을 확인하는 절차.
---

# 평가 실행과 비교

1. 재생 평가(무료): `uv run python evals/run_eval.py`. 현재 기준은 42/42이고 이게 깨지면 live 전에 고친다.
2. live 평가(유료, 실제 모델): `uv run python evals/run_eval.py --live --repeat 3`. 실행 전에 사용자에게 비용을 알리고 확인받는다. API 키는 `.voice-action/config.env`에만 있고, worktree에서는 앱 실행·live 평가를 사용자의 메인 폴더에서 한다.
3. 변동이 크다. 같은 코드로 2회 이상 재고, 과제당 1~3건 차이는 근거로 쓰지 않는다. 기준선은 `docs/evaluations/`의 보존 JSON과 `docs/status.md`를 본다.
4. INVALID(모델이 되묻기를 남발)와 FAIL(상태가 틀림)을 나눠 보고한다. 이동 과제의 절대 경로 되묻기는 알려진 문제다(`docs/status.md` "다음 작업" 2).
5. 결과 JSON(`evals/results/`)은 git 제외다. 남길 가치가 있으면 `docs/evaluations/`에 요약을 보존한다.
6. 프롬프트를 바꿨다면 `docs/self-improvement-guide.md`를 먼저 읽고, 바꾼 전후를 같은 조건으로 비교한다. 보고는 통과/실패/INVALID 수와 직접 확인하지 못한 것을 구분한다.

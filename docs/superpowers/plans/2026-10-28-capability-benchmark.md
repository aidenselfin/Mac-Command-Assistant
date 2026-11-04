# 능력 점수 벤치마크 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** live 평가 결과를 능력 축별·종합 점수(1~100)와 개발 안내(약한 축, 막힌 과제, 이전 점수 비교)로 바꾸는 `evals/benchmark.py`를 만든다.

**Architecture:** 과제 JSON에 `axis`·`level`·`status`를 추가하고, `run_eval.py`는 결과에 `axis`·`level`을 남기며 `target` 과제를 기본 실행에서 뺀다. 새 `evals/benchmark.py`는 결과 JSON과 과제 JSON을 읽어 점수를 순수 함수로 계산하고 보고·기록한다. 모델은 직접 부르지 않는다.

**Tech Stack:** Python 표준 라이브러리(`json`, `argparse`, `pathlib`), pytest, 기존 `evals/run_eval.py`.

**Spec:** `docs/superpowers/specs/2026-10-28-capability-benchmark-design.md`

## Global Constraints

- 새 의존성을 추가하지 않는다(표준 라이브러리만).
- 점수는 live 결과로만 계산한다. 재생(replay) 결과로는 점수를 내지 않고 오류로 알린다(`versions.model == "scripted"`).
- 통과 기준 기본 0.8, `--repeat` 권장 3회. 단계 범위 1~100, 점수는 정수 반올림.
- INVALID(`valid == false`)는 통과율 분모에서 뺀다. 한 과제의 유효 시도가 0개면 그 과제는 점수에서 뺀다.
- 기존 42개 과제의 동작·채점(`script`, `expect`)은 바꾸지 않는다.
- 응답·문서는 한국어, 코드 식별자는 영어. 실제 모델을 부르는 시험은 만들지 않는다.
- worktree에서만 작업한다. `main`에 합치지 않는다.

## Review Focus

- 통과율이 정확히 79%/80%인 경계 → 80%는 통과, 79%는 실패 (Task 2)
- 낮은 단계가 실패했는데 높은 단계가 전부 통과 → 낮은 단계에서 멈춘다 (Task 2)
- 과제가 하나도 없는 축 → "측정 안 됨", 종합에서 제외, 0점 아님 (Task 2)
- 재생 결과 파일을 넘김 → 점수를 내지 않고 이유를 알림 (Task 3)
- `axis`/`level`이 없는 과제나 범위 밖 `level` → 시험이 실패한다 (Task 4)

## File Structure

- Modify: `evals/run_eval.py` — `target` 필터, `--include-targets`, 결과에 `axis`·`level`
- Create: `evals/benchmark.py` — 점수 계산(순수 함수) + 보고 + CLI
- Create: `evals/benchmark.json` — 축 목록과 가중치, 통과 기준
- Create: `evals/benchmarks/` — 보존할 점수 기록(git 추적)
- Modify: `evals/tasks/*.json` — 42개에 `axis`·`level`, 새 `target` 과제 추가
- Create: `tests/test_benchmark.py`; Modify: `tests/test_eval_runner.py`
- Modify: `docs/features.md`, `docs/status.md`, `.claude/skills/run-eval` 설명(있으면)

---

### Task 1: run_eval가 axis·level·target을 다룬다

**Files:**
- Modify: `evals/run_eval.py:243-258`(결과 dict), `evals/run_eval.py:301-317`(`main`)
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: 기존 `run_trial(task, config, lessons) -> dict`, `main(argv)`.
- Produces: 결과 trial dict에 `"axis": task.get("axis")`, `"level": task.get("level")` 키. 새 CLI 플래그 `--include-targets`. 과제의 `"status": "target"`은 이 플래그 없이는 실행 목록에서 빠진다.

- [ ] **Step 1: 실패하는 시험 쓰기** — `test_trial_result_carries_axis_and_level`(과제에 `axis:"see", level:10`을 주면 결과에 같은 값), `test_main_skips_target_tasks_by_default`(`status:"target"` 과제 하나와 일반 과제 하나를 `TASKS` 임시 폴더에 두고 `main(["--live"])`가 일반 과제만 실행), `test_main_include_targets_runs_them`(`--include-targets`면 둘 다). 기존 `fake_trial` monkeypatch 방식(`tests/test_eval_runner.py:90-94`)을 따른다.
- [ ] **Step 2: 실행해 실패 확인** — `uv run --locked pytest tests/test_eval_runner.py -q` → 새 3개 FAIL.
- [ ] **Step 3: 구현** — 결과 dict에 두 키를 추가. `main`의 `parser`에 `--include-targets`(store_true)를 추가하고, 과제 목록을 읽은 직후 `if not args.include_targets: tasks = [t for t in tasks if t.get("status") != "target"]`.
- [ ] **Step 4: 통과 확인** — 같은 명령 → 전부 PASS(기존 시험 포함).
- [ ] **Step 5: 커밋** — `git add evals/run_eval.py tests/test_eval_runner.py && git commit -m "feat(eval): 결과에 axis·level을 남기고 target 과제를 분리"`

### Task 2: 점수 계산 핵심

**Files:**
- Create: `evals/benchmark.py`(계산 함수만), `evals/benchmark.json`
- Test: `tests/test_benchmark.py`(`tests/test_eval_runner.py:13-15`처럼 `importlib`로 `evals/benchmark.py`를 불러온다)

**Interfaces:**
- Produces(모두 순수 함수, 입출력은 일반 dict/list):
  - `task_rates(tasks: list[dict], trials: list[dict]) -> dict[str, float | None]` — 과제 id별 통과율(유효 시도 중 통과 비율). 유효 시도 0개면 `None`. 결과에 없는 `target` 과제는 `0.0`, 결과에 없는 `active` 과제는 키 자체를 넣지 않는다(= 미실행).
  - `axis_score(rates: list[tuple[int, float]], threshold: float = 0.8) -> int` — `(level, rate)` 목록에서, level 오름차순으로 같은 level까지 누적 평균 통과율이 `threshold` 이상인 가장 높은 level. 첫 단계부터 못 넘으면 0. 처음 실패한 단계에서 멈춘다.
  - `overall(axis_scores: dict[str, int | None], weights: dict[str, float]) -> int | None` — `None`(측정 안 됨)을 뺀 가중 평균을 반올림, 측정된 축이 없으면 `None`.
  - `evaluate(tasks, trials, config) -> dict` — `{"axes": {축: {"score", "measured", "tasks": n}}, "overall", "not_run": [id], "unclassified": [id], "invalid_only": [id]}`. `negative` 과제와 `axis`가 없는 과제는 `unclassified`(negative는 제외)로 분리.
- `evals/benchmark.json` 형식: `{"threshold": 0.8, "axes": {"see": 1, "read": 1, "summarize": 1, "organize": 1, "connect": 1, "finder": 1}}`(값은 가중치).

- [ ] **Step 1: 실패하는 시험 쓰기** — `test_axis_score_threshold_boundary`(rate 0.8 → 통과 단계 인정, 0.79 → 인정 안 함), `test_axis_score_stops_at_first_failing_level`(`[(10,1.0),(20,0.0),(30,1.0)]` → 10), `test_axis_score_first_level_fails_is_zero`, `test_overall_skips_unmeasured_axes`(`{"a":50,"b":None}` → 50), `test_overall_none_when_nothing_measured`, `test_task_rates_excludes_invalid_trials`(통과1·INVALID2 → 1.0), `test_task_rates_missing_target_is_zero_missing_active_is_not_run`, `test_evaluate_is_deterministic`(같은 입력 두 번 → 같은 dict), `test_evaluate_axis_without_tasks_is_unmeasured`.
- [ ] **Step 2: 실행해 실패 확인** — `uv run --locked pytest tests/test_benchmark.py -q` → FAIL(모듈 없음).
- [ ] **Step 3: 구현** — 위 시그니처대로 `evals/benchmark.py`와 `evals/benchmark.json`을 만든다. 누적 평균은 level이 같은 과제를 한 단계로 묶어서 계산한다.
- [ ] **Step 4: 통과 확인** — 같은 명령 → 전부 PASS.
- [ ] **Step 5: 커밋** — `git add evals/benchmark.py evals/benchmark.json tests/test_benchmark.py && git commit -m "feat(eval): 능력 점수 계산 함수 추가"`

### Task 3: 보고·개발 안내·기록·비교 CLI

**Files:**
- Modify: `evals/benchmark.py`
- Create: `evals/benchmarks/.gitkeep`
- Test: `tests/test_benchmark.py`

**Interfaces:**
- Consumes: Task 2의 `evaluate`, `task_rates`; `evals/run_eval.py`의 결과 JSON(`{"versions": {...}, "trials": [...]}`)과 `RESULTS` 폴더 규칙(`evals/results/*.json`).
- Produces:
  - `guidance(report: dict, tasks: list[dict], trials: list[dict]) -> dict` — `{"weakest_axis": 축 id 또는 None, "blocked": {축: {"task": id, "level": n, "rate": r, "problems": [문자열]}}}`. 막힌 과제는 축에서 통과율이 기준 미만인 가장 낮은 level의 과제.
  - `compare(new: dict, old: dict) -> dict` — `{"overall_delta": int, "axis_delta": {축: int}}`(새 − 이전, 한쪽이 측정 안 됨이면 키 생략).
  - `render(report, guide, delta=None) -> str` — 터미널 보고(축별 `█░` 막대 20칸, 종합, 약한 축, 막힌 과제, 변화). 한 줄 60자 안팎.
  - `main(argv=None) -> int` — `--result <파일>`(기본: `evals/results/`의 가장 최근 파일), `--compare <기록 파일>`, `--include-targets` 없음(결과에 있는 것만 본다). 점수를 `evals/benchmarks/YYYYMMDD-HHMMSS.json`에 저장하고 경로를 출력.
- 재생 결과(`versions.model == "scripted"`)면 `main`은 `"재생 결과는 능력을 재지 못합니다. --live 결과를 쓰세요"`를 출력하고 종료 코드 2, 기록을 쓰지 않는다.

- [ ] **Step 1: 실패하는 시험 쓰기** — `test_guidance_picks_lowest_failing_level_per_axis`, `test_guidance_weakest_axis_is_lowest_measured_score`, `test_compare_reports_signed_deltas`(올라가면 양수), `test_render_shows_each_measured_axis_and_unmeasured_marker`, `test_main_refuses_scripted_results`(임시 결과 파일의 `versions.model="scripted"` → 코드 2, `benchmarks/`에 파일 없음), `test_main_writes_record_and_prints_path`(`tmp_path`로 폴더를 monkeypatch).
- [ ] **Step 2: 실행해 실패 확인** — `uv run --locked pytest tests/test_benchmark.py -q` → 새 시험 FAIL.
- [ ] **Step 3: 구현** — 위 시그니처대로. 과제 JSON은 `run_eval.TASKS`와 같은 폴더(`evals/tasks`)에서 읽는다. 결과가 없으면 "결과 없음, 먼저 `run_eval.py --live --repeat 3`" 안내 후 코드 2.
- [ ] **Step 4: 통과 확인** — 같은 명령 → 전부 PASS.
- [ ] **Step 5: 커밋** — `git add evals/benchmark.py evals/benchmarks tests/test_benchmark.py && git commit -m "feat(eval): 점수 보고·개발 안내·비교 추가"`

### Task 4: 기존 과제 분류와 목표(target) 과제

**Files:**
- Modify: `evals/tasks/*.json`(42개)
- Create: `evals/tasks/target_*.json`(5개)
- Test: `tests/test_benchmark.py`

**Interfaces:**
- Consumes: Task 1의 `status: "target"` 처리, Task 2의 `benchmark.json` 축 목록.
- Produces: 모든 비-negative 과제가 `axis`(benchmark.json 축 중 하나)·`level`(1~100 정수)·`status`(`active`|`target`)를 가진다. negative(`"negative": true`) 과제는 이 세 필드를 안 가져도 된다.

- [ ] **Step 1: 실패하는 시험 쓰기** — `test_every_task_is_classified`(`evals/tasks/*.json` 중 negative가 아닌 모든 과제에 `axis`가 축 목록 안, `level`이 1~100 정수, `status`가 `active`/`target`), `test_target_tasks_have_expect_but_may_lack_script`, `test_task_ids_unique_and_match_filename`.
- [ ] **Step 2: 실행해 실패 확인** — FAIL(필드 없음).
- [ ] **Step 3: 기존 과제에 필드 추가** — 스펙 §8 구간을 기준으로 `family`와 요청 문장을 읽고 붙인다(동작 필드는 건드리지 않는다). 기본 대응: `file.find*`·`file.read.missing`(이름 검색) → `see` 5~15; 내용 검색(`find_by_content`, `find_content_clue_narrows`)과 `file.read*` → `read` 20~35; 요약 요청(`read_summary`, `attach_summary`, `meeting_*`) → `summarize` 40~50; 쓰기·이동·복사·폴더·삭제·되돌리기·`file.followup` → `organize` 55~70; `file.convert*`·`safety.injection`·웹 관련 → `connect` 72~85. 모두 `status: "active"`.
- [ ] **Step 4: target 과제 5개 추가** — 파일 상태·`must_run`으로 결정적으로 채점 가능한 것만, 모두 `status:"target"`, `script` 없음, 지금은 실패가 정상: ① 확장자별 폴더 자동 분류(`organize`, 72) ② 중복 파일 찾아 하나만 남기기(`organize`, 75) ③ 폴더를 zip으로 압축(`finder`, 88, `must_run: ["local__compress_files"]`) ④ 여러 파일 이름 일괄 변경 규칙 적용(`organize`, 78) ⑤ 파일에 태그 달기(`finder`, 90). 각 `files`·`request`·`expect`를 `evals/tasks/move_approve.json` 형식으로 쓴다.
- [ ] **Step 5: 통과 확인** — `uv run --locked pytest tests/test_benchmark.py tests/test_eval_runner.py -q` PASS, 그리고 `uv run --locked python evals/run_eval.py`(재생) → 기존과 같이 전부 PASS이며 target 과제는 실행되지 않음.
- [ ] **Step 6: 커밋** — `git add evals/tasks tests/test_benchmark.py && git commit -m "feat(eval): 기존 과제 분류와 목표 과제 추가"`

### Task 5: 문서·기록 갱신과 최종 검증

**Files:**
- Modify: `docs/features.md`(벤치마크 항목: 코드·시험 위치), `docs/status.md`(최근 검증·다음 작업), `docs/self-improvement-guide.md`(점수 도구 한 단락), `/Users/juhyeon/.claude/voice-action/tools.json`("점수 벤치마크 도구" 항목, `updated`), `/Users/juhyeon/.claude/voice-action/worklog.json`(done/todo)

**Interfaces:** 없음(문서).

- [ ] **Step 1: 문서 갱신** — 사용법 한 줄: `uv run python evals/run_eval.py --live --repeat 3` 다음 `uv run python evals/benchmark.py`. 재생은 점수를 못 낸다는 제약을 명시.
- [ ] **Step 2: 전체 검증** — `uv run --locked pytest -q`(전부 통과), `uv run --locked ruff check .`(통과), `git diff --check`.
- [ ] **Step 3: 점수 계산 시연(모델 호출 없음)** — 기존 `evals/results/`의 live 결과 하나(있으면)로 `uv run python evals/benchmark.py --result <파일>`을 돌려 보고서가 나오는지만 확인한다. 없으면 건너뛰고 상태 문서에 "실제 점수 미측정"이라고 적는다. 유료 live 실행은 사용자 확인 뒤에만 한다.
- [ ] **Step 4: 커밋** — `git add docs && git commit -m "docs: 능력 점수 벤치마크 사용법과 상태 갱신"`

---

## Self-Review 결과

- 스펙 §3 점수 규칙 → Task 2, §4 재생 제약 → Task 3, §5 구성 → Task 1·3, §6 필드 → Task 1·4, §7 오류 처리 → Task 2·3, §8 사다리 → Task 4, §9 범위 밖은 어느 Task에도 넣지 않음.
- 이름 일관성: `task_rates`·`axis_score`·`overall`·`evaluate`(Task 2)를 Task 3이 같은 이름으로 쓴다.
- 비례: 계획서는 스펙의 약 1.6배이며 코드 본문은 넣지 않았다.
- 열린 질문(스펙 §10)의 값은 기본값(80%, 3회, 6개 축)으로 두고 `benchmark.json`에서 바꿀 수 있다.

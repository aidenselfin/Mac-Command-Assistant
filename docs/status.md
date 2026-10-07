# 진행 중
- (없음)
<!-- 예: Codex, 2026-10-05 10:00, 변환 오류 메시지 개선, voice_action/conversion.py·tests/test_conversion.py -->

# 현재 목표
자가 개선 루프: 측정한 실제 모델 기준선과 lesson 후보를 비교해 채택한다
(지침서 `docs/self-improvement-guide.md`, 피드백 루프 설계:
https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

# 최근 검증
- 2026-10-07 Codex 코드 리뷰 (`9070351` 기준): 재현 결함 F1~F7 확인.
  관련 테스트 125 passed·6 skipped, 비통합 전체 450 passed·7 skipped·35 deselected·2 failed.
  실패한 HEIC 변환 2건은 샌드박스 밖 재실행에서 2 passed. ruff 통과.
  [리뷰·Claude Code 전달 지시](reviews/2026-10-07-search-rating-review.md).
  운영 코드 변경 없음, 통합 전체·live·사람 GUI QA 미실행.
- 2026-10-07, `507f027` 기준 실제 모델 Groq `openai/gpt-oss-120b`, 호출 한도 10회, 새 기준선:
  `uv run --locked python evals/run_eval.py --live --repeat 3 2>/dev/null`.
  21과제 × 3회: **60/63 통과**, **INVALID 0**, 모델 호출 153회(평균 2.4). 결과 `evals/results/20261007-174217.json`(git 제외).
  기존 16과제만 보면 45/48(이전 42/48이나 호출 한도가 달라 직접 비교는 하지 않는다).
  실패 3건: 일괄/단일 삭제 `delete_approve` 2건, 변환 `convert_approve` 1건. 모두 도구 대신 텍스트로 승인을 묻고 끝난 기존 유형.
  새 검색 과제 5개(`find_*`)는 15/15. 직전 두 측정에서 INVALID가 많았던 원인(공급자 400)은 아래 인수인계 참고.
- pytest 전체 494 passed(통합·UI 포함), ruff 통과. `--interactive`로 실제 모델 확인(모호한 파일 → 번호 선택, `rate 4`).
  GUI(`--ui`) 버튼·단축키는 Tk 위젯 테스트로만 확인했고 사람이 직접 눌러 보지는 않았다.
- 2026-10-04, `18e3916` 기준 실제 모델: Groq `openai/gpt-oss-120b`,
  `uv run --locked python evals/run_eval.py --live --repeat 3 2>/dev/null`.
  정상 16과제 × 3회: **42/48 통과(87.5%)**, **INVALID 0**, 기록된 위반 0건, 모델 호출 117회.
  변환·일괄 삭제 1/3, 단일 삭제·HTML 읽기 2/3, 나머지 12과제 3/3.
  허용 루트 밖 테스트 파일은 실행 전후 모두 없음. lesson 주입 없음, 코드·설정 변경 없음.
  [보고서](evaluations/2026-10-04-groq-baseline.md)·[보존 JSON](evaluations/2026-10-04-groq-baseline.json).
- 이전 검증(2026-10-04, `6224adf`): pytest 438 passed (UI 포함, GUI 수동 확인 없음), ruff 통과.
  이번 기준선 측정에서 pytest 전체·GUI·interactive 입출력을 다시 검증하지 않았다.
- 2026-10-05 재검증: 평가 재생 22/22, INVALID 0
  (`evals/results/20261005-000100.json`), ruff 통과, `git diff --check` 통과.
  이전 `--lesson` 주입 검증 결과도 유지된다.

# 다음 작업
- 리뷰의 P1부터 수정: 검색 경계, 날짜·명시 조건, 회의록 후보 경로, single 후보 고정,
  평가 태그 변경 시 최신 점수 집계. 상세 재현·회귀 조건은 위 리뷰 참고.
- 새 기능(검색·평가·R7~R9)은 구현됨. `--ui`에서 후보 버튼·1~5 평가·원인 칩을 직접 눌러 확인하고 필요하면 UI를 다듬는다.
- 모델이 `request_clarification`을 `options: []`로 부르면 공급자가 400으로 거부해 명령이 끝난다(기존 스키마 minItems 1).
  못 찾은 파일을 되묻는 경우에 나타난다. 옵션 없는 자유 질문을 허용할지 결정한다.
- 평가 과제 `delete_approve`·`convert_approve`의 텍스트 질문 종료는 기준선과 같은 유형이다. lesson 후보 비교는 이 새 기준선으로 한다.
- 지침서 2단계 기준선 완료. 3단계는 후보 비교·채택만 남음.
- 우선 변환·삭제에서 실제 도구 승인 대신 텍스트 질문으로 종료하는 실패를 겨냥한 후보를 비교한다.
  보고서의 제안 문장은 아직 DB에 추가하거나 채택하지 않았다.
- 같은 모델·설정·코드/과제 hash로 후보 평가 후 지침서 5.2에 따라 채택/거절/보류 근거 기록.
  양쪽 INVALID ≤1, 3/3 과제 회귀 없음, 목표 과제 개선 +2 이상이어야 채택한다.
- HTML 반복 읽기·호출 한도 소진을 별도 검토. 후속 수동 재현은 `--interactive`로 한다.
- 변환 쓰기의 디렉터리 fd 기준 open 등 더 강한 경합 방어는 별도 검토.
- 다른 남은 문제: `docs/reviews/2026-10-04-fixes.md`, `docs/reviews/2026-10-04-recent-changes-fixes.md`.

# 인수인계
- `docs/reviews/reproduce_2026_10_07_search_rating.py`는 결함 존재를 단언하는 증거다.
  현재 F1~F7 확인 후 종료 코드 0이며 수정 후 실패하는 것이 정상이다. 올바른 기대값은 tests에 추가한다.
- 새 검색 도구 `local__find_files`(voice_action/file_search.py). 설계·결정은 `docs/decisions.md` 2026-10-07 항목과 README.
  공급자(Groq)는 도구 스키마를 호출 전에 검증한다. 모델이 빈 문자열·null·남는 필드를 채우므로 선택 인자 스키마를 느슨하게 두었다.
- 피드백 DB(`.voice-action/feedback.sqlite3`)에 수동 확인 중 생긴 R3·R8 분석 결과와 후보 lesson이 있을 수 있다. 채택하지 않았다.
- 다른 에이전트(Codex)가 같은 폴더에서 평가·변환 테스트를 돌려 `voice-action-test/*.pdf`와 `evals/results/20261007-173626.json`이 생겼다. 건드리지 않았다.
- 메인 LLM은 Groq `openai/gpt-oss-120b`(사용자 지정 로컬 `config.json`, git 제외),
  피드백 분석은 OpenRouter `anthropic/claude-sonnet-5.5`(코드 기본값).
- 기준선 원본 `evals/results/20261004-235617.json`은 git 제외이므로 보존 JSON을 함께 커밋한다.
  측정 코드 hash `858df12f82bf`, lesson 없음. 원본 응답은 보존 JSON에서 제외했다.
  종료 코드 1은 에이전트 과제 실패 6건이며 INVALID/외부 API 오류가 아니다.
- 실패 6건: 변환 2·일괄 삭제 2·단일 삭제 1건은 도구 호출 없이 승인 질문으로 종료,
  HTML 읽기 1건은 모델 호출 한도 5회 소진. 승인 방어를 해제하는 수정은 하지 않는다.
- 이전 단발 결과 `evals/results/20261004-234856.json`은 14/16, INVALID 0.
  이번 3회 결과에 합산하지 않았다.
- `voice-action-test/` 사용자 미추적 자료 IMG_9441.HEIC/.jpg, 회의록-2026-10-01.txt는
  커밋하거나 지우지 않았다.

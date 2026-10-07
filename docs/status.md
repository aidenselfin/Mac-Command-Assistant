# 진행 중
- (없음)
<!-- 예: Codex, 2026-10-05 10:00, 변환 오류 메시지 개선, voice_action/conversion.py·tests/test_conversion.py -->

# 현재 목표
자가 개선 루프: 측정한 실제 모델 기준선과 lesson 후보를 비교해 채택한다
(지침서 `docs/self-improvement-guide.md`, 피드백 루프 설계:
https://claude.ai/code/artifact/a22182b1-8532-4966-ad89-087119b17a7b)

# 최근 검증
- 2026-10-07 GUI 진행 보고서 후속 리뷰 (`0754d99` 기준): 관련 테스트 117 passed·6 skipped,
  ruff·diff check 통과. 내용 단서 추가에도 후보 불변, 불완전 none 안내 누락, 표식 포함 문자 수를 로컬 재현.
  [수정 필요성·최소 대안·후속 검증](reviews/2026-10-07-ui-progress-followup-review.md).
  운영 코드 수정 없음. 새 GUI 조작·HTTP 400 공급자 재현·R7 live 분석은 실행하지 않았다.
- 2026-10-07 Codex 코드 리뷰 (`9070351` 기준): 재현 결함 F1~F7 확인.
  관련 테스트 125 passed·6 skipped, 비통합 전체 450 passed·7 skipped·35 deselected·2 failed.
  실패한 HEIC 변환 2건은 샌드박스 밖 재실행에서 2 passed. ruff 통과.
  [리뷰·Claude Code 전달 지시](reviews/2026-10-07-search-rating-review.md).
  운영 코드 변경 없음, 통합 전체·live·사람 GUI QA 미실행.
- 2026-10-07, `162707b` 기준 실제 모델 Groq `openai/gpt-oss-120b`, 호출 한도 10회, 리뷰 수정 반영 후 새 기준선:
  `uv run --locked python evals/run_eval.py --live --repeat 3 2>/dev/null`.
  23과제 × 3회: **65/69 통과**, **INVALID 0**, 모델 호출 176회(평균 2.6). 결과 `evals/results/20261007-181342.json`(git 제외, 측정 때 dirty=true였음).
  실패 4건은 모두 도구 대신 텍스트로 승인을 묻고 끝난 기존 유형: `delete_approve` 1, `delete_pattern` 2, `convert_approve` 1.
  검색 과제 7개(`find_*`, `meeting_*`)는 21/21. 직전 기준선(60/63, 21과제)과 과제 수가 달라 점수만 비교하지 않는다.
- 리뷰(`docs/reviews/2026-10-07-search-rating-review.md`) 조치 후 pytest 전체 518 passed(통합·Tk 위젯 포함), ruff 통과,
  평가 재생 30/30. 리뷰어가 본 `test_conversion` HEIC 2건은 이 환경에서 통과했다.
  `--ui`를 사람이 직접 눌러 확인하지는 않았다(후보 버튼·1~5 평가·원인 칩·저장 실패 안내는 위젯 테스트까지만).
- 이전 검증(2026-10-04, `6224adf`): pytest 438 passed (UI 포함, GUI 수동 확인 없음), ruff 통과.
  이번 기준선 측정에서 pytest 전체·GUI·interactive 입출력을 다시 검증하지 않았다.
- 2026-10-05 재검증: 평가 재생 22/22, INVALID 0
  (`evals/results/20261005-000100.json`), ruff 통과, `git diff --check` 통과.
  이전 `--lesson` 주입 검증 결과도 유지된다.

# 다음 작업
- GUI 후속 리뷰 R1~R4 검토: 내용 조건 분리, 부분 검사 안내 보존, 자동 필터 완화 설명 제거,
  읽기 문자 수 안내 보정. HTTP 400은 원인을 구분한 후 수정하며 자유 질문 schema만 완화하지 않는다.
- `--ui`에서 후보 버튼·1~5 평가·원인 칩·저장 실패 안내를 직접 눌러 확인하고 필요하면 UI를 다듬는다.
- (리뷰 5절, 미조치) 모델이 `request_clarification`을 `options: []`로 부르면 공급자가 400으로 거부해 명령이 끝난다(기존 스키마 minItems 1).
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
- GUI 진행 보고서의 한계는 결함·검증 공백·선택 기능을 구분한 후속 리뷰로 판단한다.
  기존 후보 고정·조건 이력·최신 평가 집계는 재구현보다 고정 버전 GUI 검증을 우선한다.
- 리뷰 F1~F7·A1~A4와 설계 제안 중 평가 대상 제외는 처리했다. 남긴 것: 한/영 번역 사전·의미 검색, 이름 키워드와 내용 조건을 구분하는
  2단계 검색(keywords는 지금도 모두 OR 대체어), pdf/docx 본문 검색(미검사임을 표시만 함), 옵션 없는 모델 질문 처리.
- 검색 경계는 fd 기준 순회로 바꿨고 root를 다시 resolve하지 않는다. RiskGuard가 넘기는 root는 config에서 이미 canonical이어야 한다.
- 평가 집계는 run별 마지막 평가만 센다. 이미 실행된 유료 분석은 평가를 바꿔도 취소되지 않는다.
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

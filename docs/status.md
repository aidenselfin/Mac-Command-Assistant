# 진행 중
- (없음)

# 현재 목표
제품: 자가 개선루프의 기준선과 lesson 후보 비교·채택. 지침은 `docs/self-improvement-guide.md`.
협업 도구: 현재 폴더 전용 자동 보존·수정 세션 조정 설치 완료.

# 최근 검증
- 2026-10-14 UI 평가 닫기: 대화형 CLI·Runtime·피드백 회귀 97 passed, UI 회귀 51 passed
  (실제 Tk 평가 위젯 숨김·숫자 키·worker 저장 성공 알림 포함), ruff 전체·diff check 통과.
  4~5점 저장 성공 또는 1~3점의 이유 하나 저장 성공 후 평가 영역을 닫고, 저장 실패 시 열어 둔다.
  제품 전체 테스트·유료 live 평가·`--ui` 앱의 사람 수동 확인은 재실행하지 않았다.
- 2026-10-07 밤, 코드 기준 `84e1c7b`: pytest 전체 632 passed(통합·Tk 위젯 포함), ruff 통과, 평가 재생 39/39.
  실제 모델(Groq gpt-oss-120b, 호출 한도 10회) 최종 코드 측정: **30과제×3 90/90, INVALID 0, 호출 266**
  ([보존 JSON](evaluations/2026-10-07-baseline-90of90.json), 원본 `evals/results/20261007-221117.json`은 git 제외). **이것이 현재 기준선.**
  같은 날 변동 폭: 코드가 바뀌기 전 같은 코드에서 89/90과 80/90(INVALID 9)로 갈렸고, 개선 뒤에는 87·84·85·90(INVALID 0~4).
  과제당 3회의 1~3건 차이는 근거로 쓰지 않는다. lesson 비교는 [평가 기록](evaluations/2026-10-07-lesson-4-ab.md) 참고.
  기억 기능 대조: follow_up 과제를 기억 없이 0/4, 있을 때 6/6. 사람이 `--ui`에서 눈으로 확인한 것은 없다.
- 2026-10-07 협업 도구: `pytest -q tests/test_vibe.py` 1 passed(동시 소유권·잘못된 token·
  시작 전 상태·새 파일·삭제 복구·무관한 자료 제외·HEAD/index 불변·경로 검사 포함).
  ruff 전체·shell 구문·plist 구문·diff check 통과. 제품 전체 테스트·live 평가는 재실행하지 않았다.
  테스트 전용 LaunchAgent 영구 설치, 첫 실행 exit 0·별도 ref 생성 확인. 재로그인은 직접 확인하지 않았다.

# 다음 작업
1. (완료) 확인 질문 넛지·공급자 스키마 정리 후 기준선 측정 → 90/90.
2. (완료·정정) lesson 후보 #4 A/B는 변동 범위 안이라 **보류**(미적용). 활성 lesson #1은 해가 없어 유지. 후보 #2·#3은 겨냥한 실패가 재현되지
   않아 평가하지 않았다(폐기 가능). 새 lesson 후보를 비교할 때는 같은 코드로 기준선을 2회 이상 재 변동 폭부터 확인한다.
3. **사람이 `--ui`로 확인**: 자유 입력 질문(버튼 없이 입력창), 후보 선택 버튼, 1~5 평가·원인 칩, Finder 파일 드래그 첨부,
   Finder 창이 앱 옆에 뜨는지(손쉬운 사용 권한), 직전 대화 기억(`요약해줘` → `그 요약을 파일로 저장해줘`).
4. 보류: [Claude Code 조사·개선 설계](reviews/2026-10-07-claude-code-research.md) 구현(재개 시 현재 코드와 대조, 계측 P0 먼저),
   의미 검색·한/영 대체어·pdf/docx 본문 검색, HTML 반복 읽기, 변환 쓰기의 fd 기준 경합 방어.

# 인수인계
- 이번 작업의 핵심 원인: 반복된 HTTP 400은 공급자가 도구 호출을 스키마로 먼저 검사해 거부한 것이었다. 새 400은 이제 메시지에
  `HTTP 400: tool_use_failed — <도구>: <위반>`으로 보이므로, 보이면 해당 도구 스키마에서 상한·`oneOf`를 빼고 코드에서 적용한다.
- 모델이 질문을 남발하면 평가가 INVALID가 된다. free_text는 확인 질문·검색 전 질문을 앱이 돌려보내는 규칙(`_free_text_refusal`)이 있어야 한다.
- 피드백 DB의 lesson 후보 #2~#4는 미채택이다. 운영 `.voice-action/` DB는 수정하지 않았다.
- 알려진 불안정: `tests/test_ui.py`의 붙여넣기 테스트는 테스트 중 사람이 키보드를 치면 가끔 실패한다(`d` 같은 글자가 섞임). 코드 결함 아님.
- 이번 세션 커밋은 `2a74527`~`2959471`(+ 이 문서 커밋). README에 free_text·기억·400 진단·content_keywords 설명을 반영했다.
- Claude Code 조사 보고서 보관은 문서만 변경했다. 코드·설정·live 평가·GUI 실행은 하지 않았다.
  보고서의 과거 평가·재현을 현재 제품 검증으로 해석하지 않는다.
- Finder 창 배치는 `finder.arrange`(Accessibility)로 제목이 폴더 이름과 같은 Finder 창만 옮긴다. 권한이 없으면 안내 한 번 후 표시만 한다.
  기본 on, `VOICE_ACTION_FINDER_DOCK=off`로 끈다. 이전 "배치 보류" 결정은 decisions.md 항목으로 대체했다.
- 모든 세션은 AGENTS.md·이 문서·decisions.md를 재독한다. 수정 소유권의 원본은 `vibe.py status`다.
- 협업 도구 설치·복구·지시 예시는 [바이브 코딩 사용법](vibe-coding.md).
- 과거 결과는 위 보존 문서의 측정 시점 기준이다. 수정 전 결함을 단언하는 reproduce 스크립트의
  성공/실패를 현재 제품 상태의 판정으로 사용하지 않는다. 정식 회귀 테스트를 따른다.
- 원래 미추적 사용자 자료 및 다른 세션이 만든 voice-action-test 자료는 커밋하거나 지우지 않는다.
- `.voice-action/` DB와 `evals/results/` 원본은 git 제외다. 피드백 lesson은 후보로 남긴다.
- 메인 모델 Groq gpt-oss-120b, 분석 모델 OpenRouter Claude Sonnet 5.5. 세부 결정은 decisions.md.

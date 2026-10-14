# 진행 중
- (없음)

# 현재 목표
`work/integration` worktree에서 파일 정리·의미 검색·네이티브 UI 세 갈래를 통합 중이다(main 미반영).

# 최근 검증
- 2026-10-14 파일 정리 도구: 전체 pytest **687 passed**(실제 MCP·Tk UI 포함),
  대화형 이동 승인·Undo 자동 테스트 통과, UI 회귀 54 passed, 평가 재생 **42/42, INVALID 0**
  (`evals/results/20261014-172638.json`, git 제외), ruff 전체·diff check 통과.
  이동·이름 변경·복사·폴더 생성에 개별 승인과 Undo를 연결했다. 존재하는 삭제 도구를 없다고
  답하는 경우도 교정한다. [원인·구현·제한](reviews/2026-10-14-file-operations.md).
  유료 live 평가·사람 GUI 수동 확인은 생략. 과제가 늘었으므로 과거 90/90은 새 코드 기준선이 아니다.
- 2026-10-14 dev_report 후속 수정: 전체 pytest **649 passed**(실제 MCP·Tk UI 포함),
  대화형 CLI·Planner·Runtime 관련 회귀 292 passed, ruff 전체·diff check 통과.
  질문 답변을 파일 선택 뒤에도 보존하고, 설명으로 지정한 삭제는 검색·선택·승인으로 이어진다.
  [10건 분석·수정 범위](reviews/2026-10-14-dev-report-analysis.md).
  유료 live 평가·사람 GUI 수동 확인은 생략. 운영 피드백 DB·lesson·사용자 파일은 수정하지 않았다.
- 2026-10-14 파일 삭제 기능 확인(제품 코드 `9370fab`): 기존 `local__delete_files`의 모델 노출,
  승인 전 변경 없음·거절·재승인·백업·Undo·허용 경계 회귀와 대화형 CLI/RiskGuard 71 passed,
  실제 Tk 삭제 승인 패널 1 passed. 제품 코드 변경 없음. 유료 live 평가·사람 GUI 수동 확인은 생략.
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
- 2026-10-14 네이티브 SwiftUI 앱(`codex/finder-chat-panel` 단독 검증, 통합 전):
  - 최종 전체 pytest 668 passed, ruff 전체·Swift 빌드·plist·zsh·diff check 통과.
  - 네이티브 통신/launcher 회귀 11 passed, CLI/UI/Runtime 회귀 115 passed.
  - 실제 --interactive 연결/exit, 네이티브 앱 연결/창 닫기/백엔드 종료/재실행 확인.
  - 실제 모델로 native-ui-smoke.txt 승인 대기→거절→재요청→승인→내용 확인→Undo 제거 확인.
    유료 메인 모델·자동 피드백 분석을 포함한 소규모 smoke이며 전체 live 평가는 아니다.
  - 승인 ID 재사용·잘못된 입력·허용 밖 첨부 차단, 질문 답변→실제 MCP 쓰기→Undo 회귀 확인.
  - 실행 오류 162는 앱 전체 ad-hoc 서명으로 수정했다. strict 검사·반복 실행 통과.

# 다음 작업
1. (완료) 확인 질문 넛지·공급자 스키마 정리 후 기준선 측정 → 90/90.
2. (완료·정정) lesson 후보 #4 A/B는 변동 범위 안이라 **보류**(미적용). 활성 lesson #1은 해가 없어 유지. 후보 #2·#3은 겨냥한 실패가 재현되지
   않아 평가하지 않았다(폐기 가능). 새 lesson 후보를 비교할 때는 같은 코드로 기준선을 2회 이상 재 변동 폭부터 확인한다.
3. **사람이 `--ui`로 확인**: 자유 입력 질문(버튼 없이 입력창), 후보 선택 버튼, 1~5 평가·원인 칩, Finder 파일 드래그 첨부,
   Finder 창이 앱 옆에 뜨는지(손쉬운 사용 권한), 직전 대화 기억(`요약해줘` → `그 요약을 파일로 저장해줘`),
   새 이동·복사·폴더 생성 승인과 Undo. 실행 중인 앱은 재시작해야 새 도구를 제공한다.
4. 보류: [Claude Code 조사·개선 설계](reviews/2026-10-07-claude-code-research.md) 구현(재개 시 현재 코드와 대조, 계측 P0 먼저),
   의미 검색·한/영 대체어·pdf/docx 본문 검색, HTML 반복 읽기, 변환 쓰기의 fd 기준 경합 방어.
- 사람이 검색/요약/선택/첨부/평가/한글 입력을 시험하고 main 병합 여부를 결정한다.
- 네이티브 Finder 자동 배치·독립 배포 패키징은 아직 구현하지 않았다.

# 인수인계
- native-preview/run.sh로 실행한다. 기존 Runtime/Planner/RiskGuard/Undo/피드백을 재사용한다.
- 승인 context는 Python에만 보관하고 UI 승인 ID는 한 번만 사용한다.
- 창 닫기와 코드 갱신은 기존 백엔드 정리를 기다린다. 실행 취소 후 메뉴에서 재연결한다.
- worktree 로컬 설정은 voice-action-test만 허용한다. 원본 코드/자료는 변경하지 않았다.
- 실제 앱 smoke DB는 worktree .voice-action에 기록했고 회귀 테스트 DB는 격리했다.
- 실제 드래그·한글 IME·네이티브 100개 삭제 목록은 사람이 확인하지 않았다.

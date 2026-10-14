# 진행 중
- (없음)

# 현재 목표
파일 정리·의미 검색·네이티브 UI 세 갈래를 `main`에 통합했다. 다음은 사람이 직접 써 보며 확인하는 일과 이동 과제의 live 성공률 개선(아래 "다음 작업").

# 최근 검증
- 2026-10-14 **통합**(파일 정리 + 의미 검색 + 네이티브 SwiftUI, 브랜치 `work/integration`): 전체 pytest **722 passed**
  (실제 MCP·Tk UI 포함), ruff·diff check·Swift 빌드 통과, 평가 재생 **42/42**. 병합은 `main` → 의미 검색(병렬 세션 도구 포함) → 네이티브 순이며
  충돌은 문서 3개뿐이었다. 상세·비교는 [통합 보고서](reviews/2026-10-14-integration.md).
  **live 평가**(Groq gpt-oss-120b, 과제 33×3): 통합 코드 90/99(INVALID 8)와 90/99(INVALID 7), 통합 전 `main` 95/99(INVALID 3).
  FAIL은 각 1~2건이고 나머지는 모델이 이동 대상 폴더의 절대 경로를 되묻는 INVALID(아래 "다음 작업" 2). 과거 90/90과 직접 비교하지 않는다(과제 수가 달라짐).
  의미 검색: 실제 모델·사진 7장+문서로 질의 13건 **13/13**(없는 파일 요청은 후보 없음), 터미널 `--interactive`에서 의미 검색 → 후보 선택 →
  이동 승인 화면 표시까지 확인. 승인 후 실행·Undo까지 이어지는 한 번의 대화는 모델이 경로를 되물어 끝까지 확인하지 못했다(파일 정리 단독 테스트는 통과).
  네이티브 앱은 이동·복사·폴더 생성 승인 화면을 새로 만들고 빌드·이벤트 테스트만 했다. **사람이 앱을 띄워 눈으로 확인하지 않았다.**
- (통합 전 기록)
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
1. 사람이 확인: 네이티브 앱(`./native-preview/run.sh`)의 이동·복사·폴더 생성 승인 화면, 의미 검색 후보 선택, `--ui`의 Finder 패널,
   의미 검색을 켠 상태의 사용(`.voice-action/config.env`에 `VOICE_ACTION_SEMANTIC_SEARCH=on`, `uv sync --locked --extra semantic`).
2. 이동 과제의 live 성공률 개선: "…를 보관 폴더로 옮겨줘"에서 모델이 폴더의 절대 경로를 되묻는다(`move_*`·`create_directory_undo`가 INVALID).
   프롬프트가 허용 폴더 기준 상대 이름을 해석하도록 보강하고 같은 live 평가로 비교한다. 변동 폭이 크므로 같은 코드로 2회 이상 잰다.
3. 의미 검색 임계값(글 0.69·사진 0.62)은 파일 20개 시험값이다. 실제 폴더에서 엉뚱한 후보가 나오면 `semantic_index.py`를 조정한다.
4. `planner.py`(2,200줄+)·`ui.py`(1,700줄) 분할은 동작 고정 테스트를 늘린 뒤 별도 작업으로.
5. 보류: [Claude Code 조사·개선 설계](reviews/2026-10-07-claude-code-research.md) 구현, 한/영 대체어·pdf/docx 본문 검색, HTML 반복 읽기,
   변환 쓰기의 fd 기준 경합 방어, 네이티브 앱의 Finder 자동 배치·독립 배포.

# 인수인계
- 먼저 [AGENTS.md](../AGENTS.md) → [ARCHITECTURE.md](ARCHITECTURE.md) → [features.md](features.md). 기능을 바꾸면 features.md도 갱신한다.
- 모델이 질문을 남발하면 평가가 INVALID가 된다. free_text는 확인 질문·검색 전 질문을 앱이 돌려보내는 규칙(`_free_text_refusal`)이 있어야 한다.
- 공급자가 도구 호출을 스키마로 먼저 검사해 HTTP 400을 내므로 도구 스키마에는 상한·`oneOf`를 넣지 않고 코드에서 적용한다(메시지에 `tool_use_failed — <도구>: <위반>`).
- 의미 검색은 꺼짐이 기본이고, 꺼져 있으면 도구 정의가 이전과 같다. 켜면 모델이 `query`에 찾는 파일의 설명만 적도록 안내한다.
- 네이티브 앱은 승인 context를 Python에만 두고 Swift에는 1회용 승인 ID만 준다. 메시지를 바꾸면 [native-protocol.md](native-protocol.md)·Swift·`native_bridge.py`를 함께 고친다.
- 피드백 DB의 lesson 후보 #2~#4는 미채택이다. 운영 `.voice-action/` DB와 사용자 시험 자료(`voice-action-test/`)는 커밋하거나 지우지 않는다.
- 알려진 불안정: `tests/test_ui.py`의 붙여넣기 테스트는 테스트 중 사람이 키보드를 치면 가끔 실패한다.
- 메인 모델 Groq gpt-oss-120b, 분석 모델 OpenRouter Claude Sonnet 5.5. 세부 결정은 decisions.md.
- 병렬 작업은 worktree로 한다(AGENTS.md "병렬 작업"). 메인 폴더는 사용자가 실행하는 `main` 전용이다.

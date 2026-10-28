# 진행 중
- (없음)

# 현재 목표
(2026-10-27) 피드백 루프가 lesson 자동 채택과 코드 자동 수정까지 스스로 한다(2026-10-28 main에 병합). 조언·개발자 기록은 제거했다.
검사 도구 전체 통과(pytest 685, 재생 42/42·변형 L1~L3 34/34). 가짜 에이전트로 코드 수정 일꾼을 임시 복제본에서 실제 git으로 끝까지 실행해 병합·태그·정리 확인. **실제 live 평가(유료)와 진짜 에이전트 수정은 돌려 보지 않았다.**
파일 정리·의미 검색·네이티브 UI 세 갈래를 `main`에 통합했다. 다음은 사람이 직접 써 보며 확인하는 일과 이동 과제의 live 성공률 개선(아래 "다음 작업").

# 최근 검증
- 2026-10-28 **크기·날짜 목록 도구**(`local__list_files`): 실제 모델 기준선(변경 전 코드, 시도 318회 중 INVALID 43)은 종합 **30**
  (보기 28·읽기 0·요약 22·정리 70·연결 58·파인더 0). 읽기 0은 `read_whole_file`(5단계) 통과 33%, 파인더 0은 8단계 `파일 열기` 도구 부재 때문.
  목표 과제 10개(보기 75~100, 파인더 32~80, 읽기 92)를 정식 과제로 올림: 실제 모델로 각 2~3회 통과. 전체 pytest **759**, 재생 **94/94**, 변형 L1~L3 86/86.
  변경 뒤 전체 실제 측정은 하지 않았다(순차 실행 33분). 다음: 읽기 5단계·요약 28단계 실패, 파일 열기·Finder 표시 도구.
- 2026-10-28 **피드백 불편 감지 강화**(main 병합 baf8478): 전체 pytest **752 passed**, ruff 통과, 평가 재생 **84/84**. 실제 기록 43건에서 놓친 재요청·며칠 걸친 반복·감사 인사 오탐을 고침(R3 완화, R10 취소, R11 반복, B1 벤치 접수, `autonomy.py bench`). 실모델 점수(30, INVALID 30회)는 동시 평가 탓에 믿을 수 없어 기록하지 않았다. 새 기준을 실제 앱에서 써 보지는 않았다.
- 2026-10-28 **능력 점수 벤치마크 통합**(브랜치 `claude/project-understanding-0a3020`, main 40여 개 병합 뒤): 전체 pytest **742 passed**, ruff 통과,
  평가 재생 **84/84**, Swift 빌드 통과. 과제 117개(목표 41개), `evals/benchmark.py`·`scripts/check.sh bench`. 새 사다리의 실제 모델 점수는 아직 안 쟀다.
- 2026-10-28 능력 점수 벤치마크(`evals/benchmark.py`, 설계·계획: `docs/superpowers/`): 시험 25개 추가, 평가 재생 42/42,
  ruff 통과. 과제에 axis·level을 붙였다. **실제 모델 점수는 아직 재지 않았다**
  (`run_eval.py --live --repeat 3` 후 `benchmark.py`). 재생 결과로는 점수를 내지 않는다.
- 2026-10-28 live 평가(Groq gpt-oss-120b, 과제 33×3, 코드 `work/feedback-autonomy`): **95/99, FAIL 3, INVALID 1**(`evals/results/20261028-140733.json`, git 제외).
  FAIL은 `create_directory_undo` 2건(도구 호출 없이 "실행하지 못했습니다")·`move_approve` 1건(되묻기 초과). 통합 직후 90/99(INVALID 7~8)·통합 전 95/99와 같은 변동 범위. 원인 분석은 하지 않았다.
- 2026-10-28 개발용 검사 도구(`scripts/check.py`)와 변형 케이스 생성기(`evals/make_cases.py`): 전체 pytest **676 passed**, ruff 통과,
  평가 재생 **42/42**, 변형(L1~L3) 재생 각 34/34. live(Groq gpt-oss-120b) L2 변형 33×3: **94/99**, 남은 실패는 `create_directory_undo~L2`
  (폴더 생성 뒤에도 절대 경로를 되묻는 현상, 원인 미확인). 기본 과제 3회 반복 기준선은 아직 없다. 네이티브 앱은 확인하지 않았다.
- 2026-10-27 터미널 대화형·Tk UI·`--text` 제거(브랜치 `work/remove-cli-tk`): 전체 pytest **672 passed**, ruff 통과.
  지운 것: `--interactive`·`--ui`·`--text`, Tk 뷰(`ui.py` 1,082줄), Finder 패널 함수, `tkinterdnd2`, Tk·대화형 테스트(15+12개).
  `test_planner_tools_review`의 후보 선택 통합 테스트는 대화형 대신 Planner를 직접 두 번 불러 다시 짰다.
  평가 재생 **42/42**. 네이티브 앱 실행·live 평가는 하지 않았다. 아래 기록의 `--ui`·`--interactive`·Tk는 제거 전 기준이다.
- 2026-10-21 의미 검색 체험 자료: `voice-action-test/embedding-demo/`에 공개 사진 3장과 가상 txt 3개 추가.
  PNG 디코딩·해상도·12 MiB 제한·UTF-8·2000자 제한 확인, 대표 검색어 6묶음의 일반 검색 0건 확인.
  기존 `.venv`에서 검색·의미 색인 회귀 52 passed, ruff 전체·diff check 통과.
  uv 실행은 캐시 접근 제한으로 실패하여 설치된 `.venv`를 직접 사용했다.
  선택 의존성 설치·실제 임베딩 순위·유료 LLM·운영 색인은 확인/변경하지 않았다.
  활성화·예시 명령·기대 후보·출처는 [체험 안내](semantic-demo.md). 기존 사용자 자료는 보존했다.
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
0. 능력 점수 첫 측정: `uv run python evals/run_eval.py --live --repeat 3` 다음 `uv run python evals/benchmark.py`(유료, 확인 후).
1. 사람이 확인: 네이티브 앱(`./native-preview/run.sh`)의 이동·복사·폴더 생성 승인 화면, 의미 검색 후보 선택,    의미 검색을 켠 상태의 사용(`.voice-action/config.env`에 `VOICE_ACTION_SEMANTIC_SEARCH=on`, `uv sync --locked --extra semantic`).
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

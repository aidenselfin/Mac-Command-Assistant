# 보류된 Claude Code 조사·개선 설계

- 2026-10-07 사용자 결정: 개선 구현은 다음으로 미루고 조사·설계만 보관한다. 코드·설정 변경은 승인되지 않았다.
- 아래 본문은 f417855 기준의 조사 기록이다. 이후 Finder·협업 도구 등 변경을 반영한 현재 상태 판정이 아니다.
- 재개 시 AGENTS.md, docs/status.md, docs/decisions.md와 현재 코드를 먼저 확인하고 이미 해결된 항목을 제외한다.
- 권장 순서: usage·캐시·종료 사유 계측(P0), 명확한 삭제·변환의 완료 조건(P1). 다른 개선은 별도로 비교한다.
- 첫 개발 프롬프트와 변경별 검증·중단·롤백 기준은 아래에 있다. 계측도 아직 구현하지 않았다.
- 본문에서 프로젝트 문서를 수정하지 않았다는 설명은 원래 조사 단계에 해당한다. 이번 보관 작업은 이 보고서와 status·decisions 문서만 갱신했다.

---

**Claude Code 조사와 voice-action-AI-test 개선 설계 — 2026-10-07**

가장 먼저 바꿀 것은 모델 응답의 usage·finish_reason을 보존하고 평가 결과에 집계하는 작은 계측 변경이다. 그 다음 첫 동작 변경은 명확한 삭제·변환 요청이 실제 변경 도구를 호출하지 않고 Completed로 끝나는 경로를 막는 것이다. 병렬 실행·전체 대화 압축·멀티에이전트는 지금 우선하지 않는다.

이 보고서는 조사·설계 결과다. 프로그램 코드·설정·운영 DB·프로젝트 문서를 수정하지 않았다. 보고서만 프로젝트 밖에 작성했다.

**조사 기준과 근거의 경계**

- 사용자가 허용한 고정 조사 기준은 f417855. 운영 코드는 git show로 이 커밋에서 읽었다. 해당 실행 코드 hash는 9d08968cda3e이며 보존된 20261007-181342 평가와 일치한다.
- 조사 중 다른 Claude가 Finder 작업을 커밋해 HEAD는 14e3f05로 이동했다. 새 Finder·첨부 구현은 이번 성능 비교에 합산하지 않았다. 아래 함수·동작 설명은 f417855 기준이다. 현재 파일 링크의 줄 번호는 달라질 수 있어 함수 이름으로 위치를 식별한다.
- 실행 환경·실효 설정·설치 패키지는 읽기 전용으로 조회했다. API 키 값과 파일 본문은 출력하지 않았다.
- SQLite는 mode=ro&immutable=1로 읽었다. 조회 시 feedback DB의 WAL 파일 크기는 0이었다. DB 기록은 특정 실행/버전/사용자의 자료이며 통제된 성능 비교가 아니다.
- 기존 테스트의 코드와 보존 평가를 조사했다. 전체 pytest·린트·새 live 호출·Claude Code 실행은 하지 않았다. 운영 코드가 동시에 수정되는 상황이고 이번 범위가 조사이기 때문이다.
- 대신 f417855의 코드를 메모리에 로드하고 /private/tmp의 일회성 합성 파일·mock 모델로 두 결함을 직접 확인했다. 임시 자료는 종료 때 제거했다.

외부 근거는 다음 세 가지로 구분한다.

1. **공식 설명:** 제품 문서·Anthropic 엔지니어링 글·공식 changelog. 문서가 설명하는 기능과 적용 조건의 근거다.
2. **공개 보관본 관찰 / 연구자의 분석:** 논문은 v2.1.88을 분석한다. 연결된 저장소는 스스로 npm 추출·재구성 소스와 유출 소스 보관본이라고 설명한다. 공식적으로 지원되는 오픈소스 제품 코어와 동일하다고 판단하지 않는다. [논문 v1](https://arxiv.org/html/2604.14228v1), [보관본 설명](https://github.com/chauncygu/collection-claude-code-source-code).
3. **본 보고서의 추론·설계:** 우리 코드·기록을 바탕으로 유효성을 예상하는 부분. 실제 개선율과 비용 절감률은 아직 측정하지 않았다.

공식 저장소에서 이번에 열어 본 changelog의 최상단은 2.1.292였다. 이것을 로컬 Claude의 설치 버전이라고 주장하지 않는다. 보관본 package.json에는 2.1.88이 있으나 main의 개별 파일이 당시 배포물과 byte 단위로 같은지는 확인하지 못했다. [공식 changelog](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md), [보관본 package.json](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/package.json).

**1. 실제 프로그램의 현재 구조**

| 영역 | 실제 구현과 코드 위치 |
|---|---|
| 진입점 | [main.py](/Users/juhyeon/voice-action-AI-test/main.py)의 main, run_text, _interactive_loop. 단발·대화형·Tkinter 모드. 대화형은 연결을 재사용하지만 완료된 이전 명령의 모델 대화까지 재사용하지는 않는다. |
| 실행 상태 | [runtime.py](/Users/juhyeon/voice-action-AI-test/voice_action/runtime.py)의 RuntimeController.submit, _finish, _on_planner_event. 단일 실행, Busy 반환, 질문·승인 대기·재개·오류·완료 관리. |
| 모델 입력 조립 | [planner.py](/Users/juhyeon/voice-action-AI-test/voice_action/planner.py)의 Planner._run_loop. system 지침·허용 root·원래 사용자 요청·질문 답변을 조립하고 _LoopState를 만든다. |
| 모델 API | 같은 파일의 OpenAICompatibleModel.generate. OpenAI 호환 Chat Completions로 messages와 도구 이름·설명·JSON Schema를 보낸다. 일반 단계 tool_choice=auto, 웹 필수 단계의 단일 도구는 강제 선택한다. stream은 사용하지 않는다. |
| 실행 정책 | Planner._process_pending_calls, _policy_error. 노출 도구·위험 분류·JSON·웹 순서·선택 파일·삭제 범위를 확인한다. 모델이 여러 호출을 반환할 수 있지만 실행은 순차적이다. |
| 승인·실행 | [risk_guard.py](/Users/juhyeon/voice-action-AI-test/voice_action/risk_guard.py)의 RiskGuard.prepare_write, execute_approved. 정확한 인자·내용·대상 상태에 digest와 일회성 nonce를 결합하고 승인 후 다시 검사한다. |
| 외부 도구 | [mcp_manager.py](/Users/juhyeon/voice-action-AI-test/voice_action/mcp_manager.py)의 MCPManager.connect, call_tool. filesystem/web/macos namespace 라우팅. manager와 각 stdio client 모두 호출 lock을 둔다. |
| 내부 도구 | local__find_files는 file_search.py, local__read_document·local__convert_file은 conversion.py, local__delete_files는 deletion.py. 외부 MCP를 통하지 않는 실행도 RiskGuard에서 관리한다. |
| 최종 응답 | Planner._continue. 웹 절차·필수 검색·선택 파일 읽기를 검사한 뒤 도구 없는 텍스트를 Completed로 반환한다. 일반 삭제·변환의 성공 여부는 이 완료 조건에서 필수로 검사하지 않는다. |
| 기록·학습 | [feedback.py](/Users/juhyeon/voice-action-AI-test/voice_action/feedback.py)의 RecordingModel, FeedbackLoop. 개인정보를 줄인 실행 기록, R1~R9 문제 감지, 평가, lesson 후보→사람 채택. |
| 평가 | [evals/run_eval.py](/Users/juhyeon/voice-action-AI-test/evals/run_eval.py)의 run_trial, manifest, diff, versions. 격리된 초기 파일, scripted 승인/선택, 파일 상태 및 일부 필수 도구 검사. |

승인과 질문은 모델이 기억하는 텍스트만으로 관리하지 않는다. 앱의 typed 상태가 실행을 통제한다. 이것은 이미 구현된 중요한 기반이며 다시 만들 이유가 없다.

**모델 호출마다 실제로 보내는 것**

- _run_loop의 system 메시지는 문서 정확성, 파일·웹·삭제·변환 도구 사용법, 질문 규칙, 절대경로와 도구 출력 불신 지침을 함께 담는다. 모든 작업에서 이 지침을 기본으로 사용한다.
- 도구는 연결 때 schema를 발견하고 allowlist로 거른다. _run_loop에서 요청 종류에 따라 로컬 도구를 추가하고 원시 filesystem__search_files를 제거한다.
- _continue가 웹 단계에 맞춰 노출 목록을 좁히고, 활성 앱 조회가 성공하면 그 도구를 이후 목록에서 제거한다.
- OpenAICompatibleModel.generate에서 스키마를 provider 형식으로 변환한다. read_text_file의 선택 head/tail에 null을 허용하는 사본을 만들고, 조건에 따라 request_clarification을 추가한다. 원본 MCP 스키마는 유지한다.
- messages에는 이전 assistant의 tool_calls와 각 tool_call_id에 대응하는 tool 텍스트가 계속 붙는다. _LoopState 전체가 모델에게 구조화되어 보내지는 것은 아니다.
- RecordingModel.generate는 run 시작 때 고정한 활성 lesson을 system 메시지 사본 끝에 붙인다. Planner의 원래 messages에는 lesson을 넣지 않는다.
- usage, cached_tokens, finish_reason은 현재 ModelTurn에 보존하지 않는다. SDK 응답이 있어도 이 정보는 다음 계층으로 전달되지 않는다.

따라서 “메시지·모델 입력·피드백 기록 분리”가 전혀 없다고 판단하면 틀리다. 사본과 안전한 기록은 이미 있다. 부족한 것은 입력용 가공 계층, 복구 가능한 읽기 결과 저장소, 실제 API 사용량의 계측이다.

**도구 결과·문맥·재사용**

- [mcp_client.py](/Users/juhyeon/voice-action-AI-test/voice_action/mcp_client.py)의 normalize_result는 text와 structured payload의 크기를 제한하고 잘림 표식을 붙인다. 비텍스트는 placeholder가 된다. 문자 제한이며 모델 토큰 예산은 아니다.
- RiskGuard._read_bounded는 큰 텍스트 파일의 앞부분만 읽는다. conversion.read_document는 문서를 텍스트로 변환한 뒤 앞부분을 제한한다.
- Planner._accept_tool_result는 웹 navigation 결과를 URL·제목 중심으로 줄이고, 본문은 별도 snapshot으로 받는다. 파일·웹 본문을 콘솔에 그대로 출력하지 않는 처리도 있다.
- 읽은 본문은 현재 명령의 messages에 남고 매 모델 호출에 다시 전달된다. 결과별 크기 제한 외에 누적 예산·오래된 결과 제거·참조화·자동 요약은 없다.
- active_app_name은 한 명령 안에서 재사용한다. 관련 회귀 테스트가 있다. 파일 읽기 결과·디렉터리 목록·검색 결과를 상태 확인 후 재사용하는 일반 캐시는 없다.
- 동일 도구·인자 반복은 경고하고 R2로 감지하지만 실행을 생략하지 않는다. 캐시와 반복 감지는 다른 기능이다.
- head/tail 읽기는 있다. 텍스트의 중간 줄을 지정하는 offset/limit 인터페이스는 없다. 큰 파일에서 잘려 나간 부분을 안정적으로 이어 읽는 기능도 없다.
- 현재 기존 파일 수정 도구는 전체 content를 보내는 write_file이다. 읽기 도구는 부분 읽기가 가능하지만, 수정 도구는 부분 교체가 아니다.

**질문·승인·종료 처리의 차이**

ApprovalContext는 대화, 도구 호출 묶음, 다음 호출 위치, 선택 파일, 웹 상태, model_calls 등을 보존한다. _approval_required에서 deepcopy하고 _restore_state에서 복원한다. 이미 완료한 앞 도구를 승인 후 다시 실행하는 구조가 아니다.

ClarificationContext는 원래 요청, 질문 종류, options, 횟수, conditions, 마지막 검색 인자만 보존한다. Planner.resume은 이를 이용해 _run_loop를 새로 시작한다. 이전 모델 대화·도구 결과·model_calls는 이어받지 않는다. 질문 최대 2회는 유지되지만 모델 호출 10회는 재개 구간별로 다시 시작할 수 있다. 사용자 명령 전체의 누적 호출은 feedback run으로 따로 집계해야 한다. 전체 대화 보존을 도입하면 이전 후보를 다시 읽을 위험도 있어, 지금의 축약 재시작 자체를 결함으로 단정하지 않는다.

PlannerTiming은 _run_loop에서 reset한다. 같은 사용자 명령이라도 clarification 재개 전 timing은 이 객체의 최종 값에서 사라질 수 있다. main.ExecutionTiming.lines는 모델 시간을 3회까지만 표시하고, PlannerTiming.record_tool은 filesystem read/write만 따로 합산한다. 검색·문서 읽기·변환 준비 시간까지 완전한 계측을 제공하지 않는다.

API SDK는 max_retries=0이다. MCP timeout·취소·transport 오류는 세션을 failed로 표시하고 자동 재연결하지 않는다. 모델이 validation 오류를 받고 다음 턴에서 고치는 것은 있으나 transport retry와 같지 않다. MCP teardown은 shield하고, 삭제·변환은 취소 때 부작용과 journal 확정이 끝나기를 기다리는 처리가 있다. UI는 별도 worker thread/asyncio loop를 사용하며 모델 출력 streaming과는 별개다.

**확인한 환경**

| 항목 | 확인값 |
|---|---|
| 호스트 | macOS 26.6.2, arm64 |
| 프로젝트 Python | .venv: 3.12.14. 시스템 조회용 Python은 3.14.6으로 서로 다름 |
| Node | v26.5.0 |
| Python 설치 패키지 | openai 2.54.0, mcp 1.30.0, anyio 4.15.1, jsonschema 4.26.0, pytest 9.1.1 |
| 메인 모델/API | Groq openai/gpt-oss-120b, https://api.groq.com/openai/v1 |
| 실효 제한 | max_tool_rounds=10, LLM 60초, MCP 30초, max_tool_result_chars=50,000 |
| 허용 폴더 | /Users/juhyeon/voice-action-AI-test/voice-action-test |
| 설치 MCP | filesystem 2026.8.31, Playwright MCP 0.0.81 |
| 브라우저 구성 | headless·isolated·이미지 응답 omit·codegen none. navigate/snapshot만 allowlist |
| macOS client | 현재 활성 앱 조회를 위한 in-process client |
| 피드백 | enabled, OpenRouter 분석 모델 설정 anthropic/claude-sonnet-5.5, 하루 분석 제한 20. 키 존재 여부만 확인 |
| 운영 lesson | DB 조회 시 active 1개, candidate 3개. 내용은 이 보고서에 옮기지 않음 |

Groq 공식 사양의 현재 context window는 131,072 tokens다. 이것은 입력을 항상 그 크기까지 채워도 정확하다는 뜻이 아니다. [Groq 모델 문서](https://console.groq.com/docs/model/openai/gpt-oss-120b).

**실측 기록과 한계**

보존 파일 evals/results/20261007-181342.json은 23과제×3회, 65/69 통과, INVALID 0, 모델 호출 합계 176회, trial 시간 중앙값 3.05초·최대 18.69초다. 실패는 delete_approve 1회, delete_pattern 2회, convert_approve 1회다. 각 실패에서 해당 변경 도구는 실행되지 않았고 파일 상태 채점이 실패했다.

이 평가의 lessons는 빈 목록이다. 운영 feedback DB에는 활성 lesson이 있으므로 이 점수를 현재 UI의 lesson 포함 성능이라고 일반화할 수 없다. 결과 JSON에는 전체 provider·timeout·한도·실험 옵션 hash가 없어 과거 환경 전체가 동일했는지는 코드 hash만으로 증명할 수 없다.

feedback DB에서는 llm_request 88개, llm_response 83개, 최대 20메시지, 도구 목록 최대 9개를 확인했다. 9개에는 adapter가 나중에 추가하는 가상 질문 도구가 포함되지 않는다. 성공 응답 시간의 중앙값은 930.1ms였지만 여러 작업·버전이 섞여 있어 통제된 성능 수치가 아니다. 같은 인자 digest로 반복한 도구는 한 run의 list_directory였다. 예전 HTML 반복 읽기는 별도 보존 평가에서 확인된다. 이런 기록만으로 현재 “파일 캐시가 가장 큰 병목”이라고 주장할 근거는 부족하다.

평가기의 read_summary는 도구가 실행됐는지를 확인하고 본문 요약의 의미까지 채점하지 않는다. preserve_unrelated도 작은 b.txt 전체 교체 과제다. 긴 문서의 일부만 수정하며 나머지를 byte 단위로 보존하는 품질은 별도 과제가 필요하다. 관련 테스트: tests/test_eval_runner.py의 test_required_tool_must_actually_run, tests/test_find_files_flow.py, tests/test_text_command_regressions.py의 active_app 캐시 테스트, tests/test_planner.py의 one-shot 승인·웹 강제 도구·head/tail null 테스트.

README 일부의 5회 tool loop와 494 passed 같은 설명은 코드·최신 status와 일치하지 않는다. 이번 조사에서는 코드 기본 한도 10과 실제 보존 JSON을 기준으로 했다.

**2. Claude Code에서 확인한 세부 기법**

공식 설명에 따르면 모델 문맥과 로컬 JSONL 세션 기록을 관리하고, 문맥이 차면 오래된 tool 출력 제거 후 대화 요약으로 진행한다. 따라서 원본 기록과 모델이 현재 보는 입력을 같게 유지할 필요는 없다. [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works).

엔지니어링 글은 경로·질의 같은 가벼운 참조를 두고 필요할 때 정보를 가져오는 방식, 최소한의 유효한 문맥, 손실을 고려한 compaction을 설명한다. 이는 일반 엔지니어링 원칙이며 모든 내부 구현과 수치를 증명하지 않는다. [Effective context engineering, 2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents).

공개 보관본에서 다음 함수까지 읽었다.

| 확인한 구현 | 실제 관찰과 해석 한계 |
|---|---|
| query.ts의 모델 입력 준비 | messagesForQuery를 별도로 만들고 tool result budget, HISTORY_SNIP, microcompact, CONTEXT_COLLAPSE, autocompact 순으로 처리한다. HISTORY_SNIP·CACHED_MICROCOMPACT·CONTEXT_COLLAPSE는 조건부이며 현재 배포 활성 여부는 모른다. [query.ts](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/src/query.ts) |
| toolResultStorage.ts | provisionContentReplacementState는 tengu_hawthorn_steeple을 조회하며 fallback=false다. applyToolResultBudget는 state가 없으면 그대로 반환한다. seenIds·replacement 기록으로 이미 전달한 결과의 치환 결정을 안정적으로 재적용한다. 논문의 “budget 항상 활성”이라는 요약만으로 이 별도 aggregate 예산도 모든 배포에서 켜졌다고 말할 수 없다. [toolResultStorage.ts](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/src/utils/toolResultStorage.ts) |
| FileReadTool.call | 같은 범위·mtime·이전 읽기 상태를 확인하고 기존 내용이 문맥에 있다는 전제에서 file_unchanged를 반환하는 중복 읽기 억제. partial view와 Edit/Write가 만든 상태는 제외하며 killswitch가 있다. 이는 단순 “경로 같으면 캐시”보다 좁다. [FileReadTool.ts](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/src/tools/FileReadTool/FileReadTool.ts) |
| StreamingToolExecutor | schema 파싱 후 isConcurrencySafe를 판단하고 독립 호출을 겹친다. getCompletedResults는 필요한 순서를 유지하지만, 앞의 안전한 호출이 아직 실행 중이면 뒤 완료 결과를 먼저 내보낼 수 있다. 논문의 “모든 결과가 요청 순서”라는 표현을 엄격한 보장으로 옮기면 과장이다. [StreamingToolExecutor.ts](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/src/services/tools/StreamingToolExecutor.ts) |
| FileEditTool | validateInput·call과 읽기 상태·변경 검사·교체 처리를 보관본에서 확인했다. 세부 동작은 더 최신 공식 문서의 조건과 구분한다. [FileEditTool.ts](https://raw.githubusercontent.com/chauncygu/collection-claude-code-source-code/main/claude-code-source-code/src/tools/FileEditTool/FileEditTool.ts) |

공식 문서의 MCP 결과 참조화는 별도로 확인된다. 기본 output token 한도는 25,000이며, 성공한 큰 text 결과는 세션 tool-results 파일로 저장하고 경로 참조로 바꾼다. foreground text의 기본 50,000자 참조화 기준과 token 한도는 별개다. 도구별 annotation으로 기준을 바꿀 수 있다. 우리 앱의 50,000자 앞부분 자르기와 동작이 다르다. [MCP output limits](https://code.claude.com/docs/en/mcp#mcp-output-limits-and-warnings).

Tool Search도 버전을 구분해야 한다. 조사 시 공식 문서는 기본 on-demand와 일부 provider 예외를 설명하고, ENABLE_TOOL_SEARCH=auto는 10% 임계 방식이다. 논문 시기의 임계 설명을 현재 기본값이라고 쓰지 않는다. Groq Chat Completions에 Anthropic tool_reference를 그대로 넣을 수 있다는 근거도 없다. [MCP tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search).

Read는 line 범위·부분 읽기 고지, Edit는 exact old_string/new_string와 유일성 검사를 제공한다. 최신 공식 Edit의 일부 모델은 읽지 않은 파일도 조건부로 수정할 수 있지만, 우리 프로젝트에 그 완화를 가져올 이유는 없다. [Read / Edit 공식 동작](https://code.claude.com/docs/en/tools-reference).

Stop hook은 종료를 막고 이유를 모델에 돌려줄 수 있다. 확장 기능을 설정했을 때의 계약이지 모든 Claude Code 작업의 의미적 완료를 기본 검증한다는 뜻은 아니다. [Stop hook](https://code.claude.com/docs/en/hooks#stop-decision-control).

Claude Code의 usage 화면은 API 시간·wall 시간·입출력·캐시를 구분하며, 현재 문서상 캐시 통계 화면은 v2.1.251 이후, miss 원인 설명은 v2.1.260 이후다. [비용·캐시 계측](https://code.claude.com/docs/en/costs).

**3. 후보 기법과 현재 코드의 비교**

아래 효과는 가설이다. A=정확도, C=모델 호출 수, T=토큰/비용, L=지연, S=긴 작업 안정성. ↑/↓는 개선 가능성, =는 직접 변화 없음, ?는 불확실하다.

| 기법·작동 원리 | Claude 근거·조건 | 현재 코드·판정 | 실질적 차이·해결할 병목 | 예상 효과 A/C/T/L/S |
|---|---|---|---|---|
| API usage와 cache 계측 | 공식 usage 화면. 응답 메타 필요 | generate가 usage를 버림. timing·Counting은 존재. **부분 구현** | 호출 수만으로 비용을 추정하고 CPU/MCP/API 병목을 구분 못 함 | =/= /=/= /진단↑ |
| 종료 시 작업 계약 검사 | 설정된 Stop hook이 미완료 종료를 차단 | _continue의 웹·선택파일 완료 검사 있음. 삭제·변환은 없음. **부분 구현** | 텍스트 승인 질문으로 완료 처리되는 4/69 실패 | ↑/실패 경로↑ 가능/?/?/↑ |
| exact string 부분 편집 | 공식 Edit. old/new·유일성·현재 파일 검사 | write_file만 노출. 승인·상태 검사 자체는 이미 있음. **미구현** | 작은 변경에도 전체 파일 재생성, 관련 없는 내용 손상·출력 토큰 증가 | ↑/유사/↓ 가능/↓ 가능/↑ |
| 파일 범위와 본문 조건 분리 | 경로 범위를 가진 내용 탐색, 작고 명확한 도구 인터페이스 | _search가 이름 적중이면 내용 단계 종료. conditions는 프롬프트에만 누적. **부분 구현** | 이름 후보에 새 본문 단서를 넣어도 후보 불변 | ↑/↓ 가능/?/↓ 가능/↑ |
| offset/limit 범위 읽기 | 공식 Read. 과대 range는 제한·고지 | head/tail·앞부분 제한 있음, 중간 범위 없음. **부분 구현** | 긴 파일 중간 수정·후반 근거 회수 불가 | ↑/↑ 가능/↓ 또는↑/?/↑ |
| 결과 참조화와 모델 입력 projection | 공식 MCP 참조화, 보관본 budget 함수는 조건부 | normalize_result가 잘라 버림; messages 계속 누적. **미구현** | 여러 큰 결과와 재사용 가능한 원문 부재 | ?/↑ 가능/↓ 가능/?/↑ |
| 단계적 tool clearing→요약 | 공식 context 관리, 보관본 일부 단계 flag | 명령별 새 대화와 결과당 제한은 있음. 누적 압축 없음. **부분 구현** | 장시간 대화 가능성. 현재 평균 2.6회에서는 필요성 약함 | ?/요약 호출↑/?/?/↑ 가능 |
| 캐시를 고려한 안정된 입력 prefix | 공식 prompt caching·Groq prefix cache | 고정 system 선행·lesson run 고정·도구 발견 재사용 있음. 웹 단계 tool 목록 변경. **부분 구현** | prefix 깨짐·실제 hit 관찰 부재. 로컬 도구 정의 캐시와 API prefix cache는 별개 | =/=/↓ 가능/↓ 가능/= |
| 도구·지침 점진 로드 | 공식 Tool Search/skills; API 지원 조건 있음 | delete/convert 조건 노출·웹 단계 축소 있음. **부분 구현** | 9개 정도 도구에 discovery 호출 추가하면 오히려 손해일 수 있음 | ?/↑ 위험/?/?/? |
| 동일 범위 읽기 재사용 | 보관본 Read의 상태·range·문맥 조건·killswitch | active app만 캐시, 파일은 없음. **부분 구현** | 예전 HTML 반복 읽기. 현재 반복 빈도 근거는 약함 | ?/= 주로/↓ 가능/↓ 가능/? |
| 병렬 도구와 streaming 조기 실행 | 보관본 concurrency-safe 분류, 입력 완성 뒤 실행 | 다중 tool_calls 파싱은 있음, _process_pending_calls와 manager/client lock은 직렬. **부분 구현** | 여러 독립 읽기의 지연. 상태 변경 read-only인 browser_navigate는 병렬 안전하지 않음 | ?/=/유사/↓ 가능/? |
| 실패 유형별 복구·재시도 | 공식 changelog의 특정 오류 복구; 플랫폼 종속 | 도구 validation 재수정 있음, API retry·MCP reconnect 없음. **부분 구현** | 일시 429/5xx 가능. 현재 4/69 실패는 이 유형이 아님 | ↑ 가능/↑/↑/?/↑ 가능 |
| 탐색 문맥을 subagent로 격리 | 공식 subagents 별도 context | 이미 짧은 명령별 격리. 내부 탐색 LLM 없음. **현재 적용 불필요** | 수십 파일 탐색이 아닌 1~수개 파일 작업에 추가 모델·요약 손실 | ?/↑/↑/↑/? |

내용 탐색 인터페이스·명확한 파라미터·작은 반환값을 설계하는 근거는 Anthropic의 도구 엔지니어링 글이다. 단, content_keywords라는 이름이나 아래 필터 구현은 Claude Code 내부 구현을 복제한 것이 아니라 우리 프로젝트에 맞춘 설계다. [Writing tools, 2025-09-11](https://www.anthropic.com/engineering/writing-tools-for-agents).

범위 밖 후보의 비용·부작용·확인 실험은 다음과 같다.

| 보류 후보 | 도입 비용·부작용·API 의존 | 비교 실험과 도입 조건 |
|---|---|---|
| 원문 참조화·projection | 저장소와 재조회 도구, 원문 개인정보 관리, 만료·권한·문맥 연결을 추가해야 함. 작은 모델은 참조를 무시할 수 있음 | 여러 50k 결과 작업에서 정보 회수 정확도·총 token·호출 수 비교. 먼저 range 읽기를 준비하고 실제 압력이 확인될 때 |
| 전체 대화 요약 | 추가 LLM 비용, 편집 조건·승인 이력 누락 위험. 약한 모델이 잘못 요약하면 복구 어려움 | 10회 가까운 작업에서 요약 전후 동일 목표·선택 파일·거절·본문 사실 유지 검사. 기존 짧은 작업은 skip |
| prefix 안정화 | schema 정렬/정적·동적 입력 분리 비용은 낮으나, schema 고정 때문에 필요 없는 도구를 계속 노출하면 정책 회귀 | P0 이후 cold/warm AB. 실제 cache hit·총 비용·p50/p95 측정. 먼저 정렬만 단독 변경, tool 노출 정책은 유지 |
| Tool Search·지침 점진 로드 | Anthropic API 기능 그대로 Groq에 이식 불가. 자체 discovery는 호출 1회 이상·선택 실패 위험 | 도구 수가 실제로 증가하고 schema token 비중이 클 때만. 작은 모델 별도 비교 |
| Read dedup | 명령 단위 state·정확한 파일/range 식별·승인 재개 보존·변경 검증 필요. mtime만으로 안전성을 과장하면 안 됨 | 동일 파일·range 반복, 같은 mtime 내용 변경, 교체·symlink, 이미 문맥에서 제거된 원문 검사. 현재 실사용 반복 빈도부터 계측 |
| 병렬/streaming | lock 3개 계층과 Runtime 단일 tool 상태를 변경해야 함. JSON 조각 실행·취소·중복 부작용 위험 | 독립 읽기 3개를 모은 작업에서만 먼저 직렬 대비. write와 navigate/snapshot·선택 파일 정책은 계속 직렬. TTFT와 전체 시간 분리 |
| retry | 예산·backoff·오류 분류·중복 방지 필요. 잘못된 400을 반복하면 비용만 낭비 | timeout/429/5xx fault injection. 400/401/404·schema 오류·불확실한 쓰기·MCP 오류는 재실행하지 않음 |
| subagent 탐색 | 별도 모델 호출·요약 비용·파일 후보 범위 손실 | 수십 파일 탐색의 문맥 폭증이 재현될 때. 지금 도입하지 않음 |

Groq는 현재 gpt-oss-120b의 prefix 캐시를 자동 지원한다. 따라서 “앱에 캐시 코드가 없으니 API 캐시도 없다”는 결론은 틀리다. 동일 prefix·tool 정의 유지와 usage.prompt_tokens_details.cached_tokens를 측정해야 한다. API의 자동 cache hit는 보장되지 않는다. [Groq prompt caching](https://console.groq.com/docs/prompt-caching). Anthropic의 tools→system→messages 캐시 계층을 Groq의 정확한 내부 구현이라고 단정하지 않는다. [Anthropic cache 구조](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

**4. 우선순위와 작은 변경별 실행 계획**

**P0 — 계측을 먼저 보강한다. 비용 절감 기능은 아직 넣지 않는다.**

목표와 필요성: API 응답에서 이미 오는 숫자를 버리지 않고 사용한다. 입력·출력·캐시·종료 이유를 알아야 프롬프트 축소, Edit 도구, 캐시 안정화의 효과를 비교할 수 있다.

모듈·인터페이스: planner.py의 ModelTurn에 선택적 ModelUsage 메타데이터를 기본값과 함께 추가하고 OpenAICompatibleModel.generate에서 숫자와 제한된 finish_reason만 추출한다. feedback.py의 llm_response, evals/run_eval.py의 Counting·run_trial·versions에 집계한다. 기존 ModelTurn(text, calls, clarification) 호출은 유지한다.

최소 범위: prompt/completion/cached token과 가능한 reasoning token, 실제 응답 model, finish_reason, 모델 호출 횟수, API wall 시간. 지원하지 않는 필드는 null로 둔다. 캐시 token이 없다는 사실과 실제 0을 구분한다. payload·HTTP header·raw usage object·파일 내용은 저장하지 않는다. 평가 결과에 비밀값을 제외한 실효 설정과 실험 플래그 hash를 남긴다. DB 테이블 추가나 SDK 교체는 필요 없다. MCP·승인 대기·정리 시간을 분리하는 추가 계측은 다음 작은 변경으로 한다.

유지할 동작: 프롬프트·schema·도구 선택·승인·재시도·최종 답변이 완전히 같아야 한다. 계측 실패는 명령 실패로 만들지 않는다.

대표 검증: usage가 있는/없는 응답, Groq extension, 같은 명령의 clarification 재개와 승인 재개, provider 오류. 합성 secret을 넣어 결과 JSON·feedback에 노출되지 않는지 확인한다.

비교·성공: 같은 mock 응답의 숫자가 call별 및 run별로 정확히 합산되고 기존 scripted 평가 결과가 같으면 계측 변경 성공. 비용/속도 개선을 성공 조건으로 삼지 않는다. 추가 모델 호출·응답 재요청·본문 저장·평가 결과 변경이 생기면 중단하고 되돌린다.

복사 가능한 첫 개발 프롬프트:

~~~
/Users/juhyeon/voice-action-AI-test의 AGENTS.md와 docs/status.md를 먼저 읽어라.
진행 중인 다른 작업과 미커밋 변경을 확인하고, 그 변경을 덮거나 커밋하지 마라.

이번 변경은 모델 usage 계측 하나다. 조사 기준은 f417855이지만 실제 개발 전에 현재 코드를 다시 확인하라.
프롬프트, 도구 노출, tool_choice, 승인, Undo, retry 동작은 바꾸지 마라.

planner.py의 OpenAICompatibleModel.generate에서 응답의 prompt_tokens,
completion_tokens, cached_tokens, 가능한 reasoning_tokens와 finish_reason을
좁고 검증된 메타데이터로 보존하라. 없는 값은 null로 두고 raw 응답은 저장하지 마라.
ModelTurn의 기존 위치 인자와 scripted/mock 모델은 그대로 동작해야 한다.

feedback.py의 안전한 llm_response 기록과 evals/run_eval.py의 Counting/run_trial에
선택적 메타데이터 집계를 연결하라. clarification으로 Planner loop가 새로 시작돼도
한 trial/run의 실제 호출은 모두 합산하라. versions에 비밀값을 제외한 실효 설정과
실험 옵션의 식별 정보를 추가하라.

API key, 원문 messages, 파일 본문, 쓰기 content, HTTP header는 추가 기록하지 마라.
DB 테이블, 새 의존성, cache 구현, pricing 계산기는 만들지 마라.
계측 오류 때문에 사용자 명령이 실패하지 않게 하라.

usage 있음/없음, 기존 생성자 호환, 합계, 비밀정보 비노출을 작은 관련 테스트로 검증하라.
기존 재생 평가와 관련 테스트·ruff를 실행하고 AGENTS.md 방식으로 기능 단위 커밋하라.
live API 비교는 별도 과금 승인 전까지 실행하지 마라.
무엇을 측정할 수 있게 됐는지와 아직 측정하지 못하는 항목을 보고하라.
~~~

**P1 — 명확한 삭제·변환의 조기 종료를 막는다. 첫 동작 개선이다.**

목표: 모델이 텍스트로 승인 질문을 쓰는 것과 실제 ApprovalRequired를 구분한다. 기존 _continue의 웹 필수 절차와 같은 경계에서 완료를 검사한다. hook framework를 새로 만들지 않는다.

근거: 보존 평가 4/69와 pinned mock 재현에서 도구 0회·Completed를 확인했다. 검증·권한을 더하는 문제가 아니라 완료 계약이 작업 종류별로 불균일한 문제다.

모듈·인터페이스: Planner._LoopState·ApprovalContext의 작은 필드, _continue·_accept_tool_result·복원 경로. 위험 검증은 그대로 둔다. 기존 write_completed는 어떤 변경이든 성공하면 true이므로 삭제와 변환 완료를 구분하는 데 단독 사용하지 않는다.

최소 구현: 명확한 대상/범위를 갖는 지원 삭제·변환 요청에만 필요한 operation을 기록한다. 실제 해당 도구 성공으로 충족한다. 미충족 상태에서 final text가 오면 앱 승인은 해당 도구 호출 후에 받는다는 짧은 교정 1회만 반환한다. 전체 모델 호출 한도 안에서 처리하고 다시 텍스트 종료하면 성공으로 포장하지 않는다. 대상 없음·지원하지 않는 형식·정책 거부는 재실행하지 않고 검증된 미완료 이유로 끝낸다. 질문·설명·부정·인용 요청은 대상에서 제외한다. 최초 실험 플래그는 완료 교정만 on/off하며 schema 강제·프롬프트 대개편을 함께 하지 않는다.

유지: 승인 없이 파일 변경 금지, 승인 거절 즉시 종료, Undo, 웹 실패 뒤 쓰기 차단, clarification 최대2, hidden tool 거부. 이전에 실행한 쓰기를 다시 실행하지 않는다.

작업·까다로운 사례: delete_approve/delete_pattern/convert_approve, 삭제 거절, 변환 거절, “삭제 방법 설명”, “삭제하지 마”, 인용문 내 변환 명령, 없는 파일, unsupported conversion, 이미 성공한 뒤 모델의 추가 호출, 승인 후 대상 변경.

비교: AB 각각 기존23과제와 미사용 표현/대상 holdout, 반복5회 이상. 실제 도구 실행·승인 대기·부작용·false Completed를 별도로 센다. 목표 가족 통과가 기존 채택 기준처럼 +2 이상 증가하고 기존 안전·정상 동작 회귀가 없어야 채택한다. 실패 경로의 호출1회 증가와 성공률 개선을 구분한다. 추가 호출이 성공 없이 한도 소진만 늘면 중단한다.

개발 프롬프트:

~~~
P0 계측 이후, 명확한 삭제·변환 요청의 조기 Completed만 수정하라.
Planner._continue의 기존 웹 완료 조건과 _LoopState/ApprovalContext를 재사용하라.
필수 operation은 해당 도구의 실제 성공으로만 충족시켜라.
미완료 final text는 최대1회 교정하되 기존 모델 호출 한도를 지켜라.
없는 파일, unsupported 형식, 정책 거부, 인용·부정·설명 요청을 실행 의도로 강제하지 마라.
승인·Undo·노출 정책·재시도는 바꾸지 마라. 실험 플래그로 구방식과 비교하라.
delete/convert 승인 누락과 거절·실패·재승인·비명령 사례를 검증하라.
프롬프트 대개편이나 tool_choice 강제는 이번 변경에 섞지 마라.
~~~

**P2 — 이름 후보 집합 안에서 본문 조건으로 좁힌다.**

목표: 사용자가 준 “예산 승인 내용이 있는 문서”라는 추가 조건이 실제 검색에서 적용되게 한다. 상태를 기억하는 것과 검색 의미를 실행하는 것을 분리한다.

근거: proposal-1~7.txt 중 3번만 “예산 승인”을 넣은 pinned 재현에서 keywords=[proposal]과 [proposal, 예산] 모두 name/narrow/7이었다. [기존 후속 리뷰](/Users/juhyeon/voice-action-AI-test/docs/reviews/2026-10-07-ui-progress-followup-review.md)의 R1과 같은 문제이며 새 발견으로 포장하지 않는다.

모듈·인터페이스: file_search.py의 FIND_FILES_DEFINITION·_search·find_files, Planner.resume의 조건 전달. RiskGuard의 기존 로컬 검색 분류와 fd 기반 읽기를 재사용한다.

최소 구현: 선택적 content_keywords 한 그룹. 기존 keywords는 OR 대체어, content_keywords 내부도 OR, 두 그룹 사이는 AND다. 기존 폴더·형식·날짜 필터와 이름/폴더 후보를 유지한 채 그 후보의 bounded text만 검사한다. 내용 조건이 없으면 기존 동작 그대로다. 후보가 없다고 범위를 자동으로 풀지 않는다. 문서 미검사·시간/파일/앞부분 한도에 걸리면 complete=false를 유지한다. 새 임베딩·전체 index·정규식 검색 언어·본문 결과 노출은 넣지 않는다.

유지: 후보 하나라도 불완전 검색이면 확인, 선택 파일 고정, 최대 질문2, symlink와 허용 경계 차단. clarification resume에서 명시적 본문 조건을 누적한다. 모델이 content_keywords를 빼먹는지 live로 별도 확인한다. 사용자 조건을 임의로 여러 AND 그룹으로 확장하지 않는다.

검증: 이름 같은7개/본문 한개, 폴더→본문의 두 질문, 한영 이름, OR 대체어, 명시 PDF 조건과 MD만 있는 경우, 본문 단서가 파일 뒤쪽에 있는 경우, 읽기 불가·symlink·시간 초과. PDF 본문 검색은 이번 변경 대상이 아니다.

비교·채택: 후보 집합과 선택 정확도, 질문 수, 검색 호출 수, model_calls, token, scan 시간. 기본 검색 회귀가 없고 해당 재현 및 heldout이 정확히 좁혀져야 성공이다. 모델이 새 필드를 지속적으로 오용하거나 한도 위반·묵시적 범위 확장이 있으면 중단한다.

개발 프롬프트:

~~~
file_search.py의 이름 적중 시 내용 조건이 무시되는 문제만 수정하라.
선택적 content_keywords 한 그룹을 추가하고 기존 keywords OR 결과와 AND로 적용하라.
기존 fd 기반 bounded text 읽기, 폴더·형식·날짜 필터를 재사용하라.
내용 조건이 없으면 기존 결과가 같아야 한다.
Planner.resume의 본문 조건 누적 전달도 확인하라.
미검사 PDF·docx와 한도 초과는 complete=false로 유지하고 조건을 자동 완화하지 마라.
proposal7개 중 본문3번만 일치하는 재현, 두 차례 narrowing, symlink, 부분검사를 테스트하라.
임베딩·인덱스·범위 읽기·새 문서 변환은 함께 추가하지 마라.
실험 플래그로 기존 결과와 비교 가능하게 하라.
~~~

**P3 — 기존 텍스트 파일에 exact replacement를 제공한다.**

목표: 한 문장을 바꾸려고 긴 문서 전체를 모델이 다시 쓰지 않게 한다. 정확한 old_text/new_text를 코드가 적용하고 전체 결과를 기존 write 승인·Undo 경로로 넘긴다.

모듈·인터페이스: 작은 local__replace_text 도구 정의/인자 검증, RiskGuard WRITE 분류 및 준비 경로, Planner 도구 노출·결과 처리, 기존 UndoJournal. 가능한 파일 수를 줄이되 별도의 generalized patch framework는 만들지 않는다.

최소 범위: UTF-8 일반 파일 하나, 비어 있지 않은 old_text, 정확히1회 일치, new_text(삭제이면 빈 문자열 허용). replace_all·regex·fuzzy·여러 edit batch는 제외한다. 줄바꿈·BOM·원본 나머지 bytes를 보존한다. 결과 전체 content·원본 fingerprint·operation을 승인에 결합하고 승인 후 원본이 바뀌면 다시 준비하고 재승인한다. bytes 기반 검사로 보존성을 확인하고 기존 UTF-8 overwrite journal을 재사용한다. 코드 주입을 하는 모델용 shell은 추가하지 않는다.

도구 사용 권한과 읽기 전제를 구분한다. 현재 명확한 전체 overwrite는 원본을 읽지 않고 수행할 수 있다. 새 부분 편집만 필요한 원문 확인/상태 binding을 갖게 하고 기존 write_file 행동은 바꾸지 않는다. selected candidate·read-only 요청·웹 요청 정책에서 새 WRITE 도구가 기존 제한을 우회하지 않게 한다.

검증: 10KB/100KB 문서 한 문장, 동일 문구 2회, 일치 없음, Unicode 정규화 차이, CRLF·BOM, 내용 변경·inode 교체·symlink, 승인 거절·취소·Undo, 다른 파일 byte 보존. 현재 preserve_unrelated 과제는 이것을 검증하기에 충분하지 않다.

비교·성공: 정확한 최종 bytes/Undo가 우선이다. 동일 작업에서 모델 출력 token과 누적 input token, 총 latency를 측정한다. 예컨대 부분 수정 묶음에서 output token 중앙값30% 이상 감소를 사전 목표로 둘 수 있으나 이것은 측정 결과가 아니다. total latency는 schema/추가 읽기 때문에 늘 수 있다. 어떤 권한 회귀도 즉시 rollback. match 실패가 많고 실질 비용 이득이 없으면 구방식 유지한다. 작은 로컬 모델에서는 정확한 문자열 복사가 어려워 실패할 수 있으므로 별도 모델 실험 후 활성화한다.

개발 프롬프트:

~~~
새 도구 local__replace_text만 작은 변경으로 추가하라.
UTF-8 일반 파일 하나에서 old_text가 정확히1번 일치할 때만 new_text로 교체하라.
regex, fuzzy, replace_all, batch는 지원하지 마라.
나머지 bytes와 CRLF/BOM을 보존하고 전체 결과를 기존 승인·Undo overwrite 경로로 연결하라.
원본 fingerprint를 승인에 결합하고 승인 후 원본 변경은 재승인하라.
새 WRITE 도구가 read-only/선택파일/웹 제한을 우회하지 않게 전체 caller를 확인하라.
중복·불일치·Unicode·CRLF·BOM·symlink·승인거절·Undo를 검증하라.
기능 플래그로 전체 write 방식과 동일 작업의 정확도·output token을 비교하라.
범위 읽기나 캐시 기능은 이번에 넣지 마라.
~~~

**P4 — 긴 텍스트를 offset/limit로 이어 읽는다. 긴 파일 작업이 필요할 때 채택한다.**

목표: 기존 head/tail 기능을 재구현하지 않고, 현재 빠진 중간 범위와 잘린 뒤의 재조회만 제공한다. 자동으로 모든 조각을 LLM에 보내는 전체 문서 요약기는 만들지 않는다.

모듈·인터페이스: 작은 local__read_text_range(path,start_line,line_count), RiskGuard READ_ONLY 분류, Planner.CONTENT_READ_TOOLS와 후보 읽기/고지 처리. 기존 fd 기반 no-follow 경계 기능을 재사용한다. UTF-8만, 시작1부터, 줄수·byte·시간 한도를 둔다.

결과에는 반환 범위, 다음 시작 줄, eof, 제한 발생 여부, content를 담는다. 전체 줄수를 알기 위해 파일 끝까지 읽지 않는다. 큰 단일 줄은 byte 상한에서 멈추고 줄 내부가 부분적임을 고지한다. 전체 읽기 완료와 파일 일부 확인을 혼동하지 않게 metadata를 처리한다. 기존 head/tail과 read_document는 유지하고 PDF page 읽기는 제외한다.

검증: 원문50k 밖의 중간 근거, 정확히 EOF, EOF 밖, 빈 파일, 매우 긴 단일 줄, 잘린 UTF-8 경계, 파일 교체와 symlink, 선택한 파일 이외 거부. 검색 조건은 바꾸지 않는다.

비교: 긴 문서의 특정 사실/부분 수정에서 expected fact·수정 bytes를 채점한다. 전체 요약은 별도 평가해 누락을 잡는다. 더 읽는 만큼 호출 수와 total input이 증가할 수 있으므로 호출 감소를 목표로 잡지 않는다. 범위 고지·조회 정확도가 충족돼도 holdout에 필요가 없다면 계속 off로 둔다. 한도 초과·불완전 읽기를 전체 읽기로 보고·경계 회귀는 rollback 조건이다.

개발 프롬프트:

~~~
기존 head/tail을 유지하고 local__read_text_range 하나만 추가하라.
path/start_line/line_count로 UTF-8 텍스트의 중간 범위를 bounded하게 읽어라.
기존 fd/no-follow 허용경계를 재사용하고 READ_ONLY allowlist에 명시하라.
반환 범위·next line·eof·부분 상태를 metadata로 제공하고 전체 줄수 계산을 위해 끝까지 스캔하지 마라.
큰 단일 줄과 UTF-8 경계도 byte 한도 내에서 처리하라.
Planner의 selected-file 정책과 부분읽기 안내를 동일하게 적용하라.
긴 파일 중간·EOF·빈파일·거대 한 줄·symlink·선택 밖 파일을 검증하라.
파일 캐시·자동 분할요약·PDF paging·부분 편집 변경을 함께 넣지 마라.
실험 플래그로 긴 파일 작업 정확도와 누적 tokens/호출 수를 비교하라.
~~~

**5. 공통 평가 절차**

각 변경은 별도 커밋·별도 AB다. P0는 먼저 양쪽에 동일하게 적용한 계측 기반으로 고정한다. 그 뒤 P1~P4는 직전 안정 버전에서 기능 하나만 on/off한다. 온갖 feature flag를 동시에 켜서 이득을 측정하지 않는다.

- 같은 provider·model ID·API endpoint·temperature/기타 요청 옵션·한도·timeout·schema/프롬프트 버전·lesson 목록·초기 파일 bytes를 사용한다. 값이 기본이면 기본이라는 사실도 기록한다.
- 기존 trial 임시 폴더·전용 journal·manifest 검증을 재사용한다. 운영 파일과 DB는 평가에 쓰지 않는다. 실험별 fingerprint·전체 설정 식별 정보·과제 hash를 저장한다.
- 개발용 과제와 별도 heldout을 둔다. 각 개선안마다 최소2개 새로운 표현/파일 구성의 과제를 사전에 정해 구현에 사용하지 않는다. 데이터가 작으므로 일반화 효과를 과장하지 않는다.
- 순서를 교차하거나 무작위로 배치하고 최소5회씩 비교한다. cold/warm cache는 따로 보고한다. 한번의 warm 결과를 구버전 cold 결과와 비교하지 않는다.
- 기존 scripted negative controls로 채점기 보존을 확인한다. live negative는 기존대로 제외한다.
- 정확도는 파일 manifest·승인 전 변경0·기대 도구·허용 경계·작업 상태에 더해 각 변경의 의미적 정답을 확인한다. 요약 사실 정확성은 합성 원문의 기대 사실을 사전 지정하고 필요하면 블라인드 사람 검토를 사용한다.
- 속도는 API wall time·도구 준비/실행·MCP startup·사용자 승인/질문 대기·정리 시간을 구분한다. p50/p95는 소표본에서 불안정하므로 개별 trial도 제공한다.
- 비용은 prompt/completion/reasoning/cached tokens를 분리한다. 토큰을 줄인 것과 캐시 할인받은 것을 구분한다. 가격 추정은 측정일 provider 가격 기준의 별도 계산이며 청구액으로 확정하지 않는다.
- INVALID 원인을 provider/harness/timeout으로 분리한다. 기존 규칙의 양쪽 INVALID≤1·기존3/3 과제 회귀 시 중단·목표 +2 기준은 초기 판정에 사용하되 5회 비교의 기준은 실행 전에 명시한다.
- 결과가 소표본 변동 범위거나 목표 이득이 없으면 보류한다. 승인·경로·Undo·본문 누출 회귀는 통계와 관계없이 즉시 중단한다.

작은 모델에서는 Tool Search, 참조 결과, compact summary, 여러 병렬 tool call의 해석이 추가 부담이 될 수 있다. 현재 Groq 120B 결과와 작은 로컬 모델 결과를 합산하지 않는다. “강한 Claude에서 잘 됐으니 같은 설계가 약한 모델도 좋다”는 가정은 실험하지 않고 채택하지 않는다.

**6. 남은 확인 사항**

- Claude Code의 전체 공식 코어 소스·프로덕션 feature flag·실제 실행 trace는 확인하지 못했다. 공개 보관본을 빌드/실행하거나 배포 binary와 대조하지 않았다. 특정 함수가 존재하는 것과 기본 활성화는 구분한다.
- source 파일 commits 페이지는 접근 오류가 있어 개별 main 파일의 고정 source SHA를 확보하지 못했다. 보관본의 버전 표기만으로 모든 관찰을 2.1.88 배포 기본값으로 확정할 수 없다.
- 논문 v1은 요청된 버전 그대로 읽었다. arXiv에는 2026-07-02 v2도 있으나 이번 v1 분석과 섞지 않았다.
- 제공된 네 출발 URL은 모두 접근했다. 별도로 시도한 /engineering/prompt-caching와 /docs/en/streaming-output은 접근 오류였다. 캐시 결론은 접근 가능한 공식 costs·API cache·Groq 문서로 확인했고 streaming 내부는 보관본 관찰로만 제한했다.
- 현재 Groq 계정의 실제 cache hit·rate limit·청구액·모델 backend revision은 미확인이다. API 키의 존재는 연결 성공을 증명하지 않는다.
- 오류 기록의 raw provider payload를 수집하지 않아 기존 HTTP400을 특정 schema 하나 탓으로 확정하지 않았다. empty clarification options 문제는 별도 schema/오류 재현으로 다뤄야 하며 retry나 cache로 고칠 문제가 아니다.
- 현재 로그로 누적 token·tool schema token 비중·provider prefill과 queue 시간을 계산할 수 없다. 이것이 P0를 먼저 하는 이유다.
- 다른 에이전트가 새로 커밋한 Finder·첨부 기능은 다음 개발 시작 때 다시 읽어 인터페이스 충돌 여부를 확인해야 한다.

먼저 P0의 usage 보존·run 집계를 작은 변경으로 완료한다. 이후 사용자 기능 개선의 첫 대상은 P1의 삭제·변환 완료 계약이다. 파일 편집 도구로서의 품질을 올리는 다음 순서는 P2의 실제 본문 조건 검색, P3의 exact replacement다. P4는 긴 파일 작업 수요에 맞춰 채택한다. 현재 증거로는 Claude Code의 다섯 단계 압축·Tool Search·멀티에이전트 전체를 이식할 이유가 없다.

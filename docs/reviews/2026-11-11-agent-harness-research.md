# 실패해도 끝까지 해내는 루프: 기업 하네스 조사와 Symbio 개선안 (2026-11-11)

## 1. 지금 Symbio에서 실제로 일어나는 일

운영 피드백 DB(`.voice-action/feedback.sqlite3`, 2026-10-28 ~ 11-11)를 읽기 전용으로 집계했다.

| 항목 | 값 |
|---|---|
| 사용자 입력 | 110건 |
| 완료(Completed) | 73건 |
| 되묻기(Clarification) | 20건 |
| 오류로 끝남 | 13건 (오류 결과 11, 예외 2) |
| 같은 말을 10분 안에 다시 함(R3) | 16건 |
| 한 실행에서 같은 도구를 같은 인자로 반복(R2) | 11건 |
| 모델 호출이 많음(R8) | 11건 |

"완료"로 기록된 것 중에도 사용자가 바로 다시 요청한 것이 많다. 한 번에 원하는 결과를 받은 비율은 체감대로 절반을 조금 넘는 수준이다.

대표 실패 사례(자율 분석 기록 69·70번): 한글 파일 이름이 macOS 분해형(NFD)이라 모델이 경로를 옮겨 적다 글자가 깨졌다. `read_document`가 "파일 없음"을 반복했고, 모델은 **같은 깨진 경로로 같은 호출을 되풀이하다** 10회 한도에 걸렸다.

코드에서 확인한 구조적 원인은 세 가지다.

1. **오류를 모델에게 돌려주지 않고 그 자리에서 끝낸다.** `planner.py`에는 `return Error(...)`가 약 25곳, `raise PlannerError(...)`가 약 10곳 있다. 예를 들어 "clarification과 MCP 도구를 동시에 요청할 수 없습니다"는 모델이 형식만 틀린 것인데도 명령 전체가 끝난다. 모델에게 "형식이 틀렸으니 다시 하라"고 한 번 알려 주면 고칠 수 있는 종류다.
2. **도구 오류 메시지가 다음 행동을 알려 주지 않는다.** "파일 없음"만 돌려주니 모델은 같은 경로를 다시 시도한다. 비슷한 실제 파일 이름을 함께 주면 바로 고칠 수 있다.
3. **반복을 감지하지 않고 횟수로만 막는다.** 같은 호출, 같은 결과가 세 번 나와도 루프는 모른다. 10회를 다 쓰고 "모델 호출 한도 초과"로 끝난다. 반복 감지(R2)는 실행이 끝난 뒤 피드백 단계에만 있다.

## 2. 기업과 연구가 쓰는 방식

### 2-1. 오류는 끝이 아니라 다음 입력이다 (Anthropic, OpenAI)
- Claude API는 도구가 실패하면 결과에 `is_error: true`를 붙여 **오류 내용을 모델에게 그대로 돌려준다.** 모델이 보고 다시 시도하거나, 다른 도구를 쓰거나, 사용자에게 알린다.
- OpenAI Agents SDK도 도구에서 예외가 나면 기본 동작으로 오류 글을 모델에게 도구 결과로 돌려준다(`failure_error_function`으로 문구를 바꿀 수 있다). 루프를 끊는 것은 `max_turns` 초과나 안전 장치(guardrail) 위반뿐이다.
- Claude Code 내부를 분석한 글들에 따르면, 복구 가능한 오류는 루프 안에서 단계적으로 처리하고 모든 복구가 실패했을 때만 사용자에게 보인다. 권한 거절도 도구 결과로 받아 다른 방법을 찾는다.
- Manus의 원칙은 "실패를 지우지 마라"다. 실패한 행동과 오류를 맥락에 남겨 두면 모델이 같은 실수를 덜 반복한다.

### 2-2. 도구는 모델이 실수하기 어렵게 만든다 (SWE-agent, Anthropic 도구 설계 글)
- SWE-agent 논문은 모델이 쓰는 명령 인터페이스(ACI)를 다듬는 것만으로 성공률이 크게 올랐다고 보고한다. 가장 효과가 컸던 것은 **잘못된 편집을 받기 전에 검사하고, 무엇이 틀렸는지 알려 주고, 다시 하게 하는 장치**였다. 이것을 빼자 해결률이 18.0%에서 10.3%로 떨어졌다.
- Anthropic의 "에이전트용 도구 작성" 글은 오류 메시지를 "무엇이 틀렸고 어떻게 고치면 되는지" 알려 주게 쓰라고 권한다. 같은 잘못된 인자 오류가 자주 나오면 도구 설명이나 도구 자체를 고쳐야 한다는 신호로 본다.

### 2-3. 무한 반복 감지 (여러 하네스 공통)
- OpenRouter Agent SDK, pydantic-deep, PraisonAI, OpenClaw, Hugging Face ML-intern 등이 "doom loop" 감지기를 둔다.
- 공통 설계: 인자를 정규화해 비교한다. 호출뿐 아니라 **결과도 같은지** 본다(정상적인 반복 조회와 구분하기 위해). 보통 3회부터 1차 경고(바로잡는 지시를 넣음), 그다음 중단의 두 단계로 처리한다.

### 2-4. 행동한 뒤 확인한다 (Claude Agent SDK)
- Anthropic은 Claude Code의 루프를 "맥락 모으기 → 행동 → 결과 확인 → 반복"으로 설명한다. 확인은 마지막 한 번이 아니라 루프 전체에 걸친 성질이다. 규칙 검사, 화면 확인, 다른 모델의 판정 같은 방법을 쓴다.
- 장시간 작업용 하네스 글에서는 에이전트가 자기 결과를 과대평가하기 쉬워 별도의 평가 역할을 둔다.

### 2-5. 실행 사이의 학습 (Reflexion)
- Reflexion은 모델 가중치를 바꾸지 않고, 실패 뒤 "왜 실패했나"를 글로 써서 기억에 넣은 다음 다음 시도에 쓴다. 성공과 실패가 분명히 판정되는 과제에서 효과가 컸다(AlfWorld에서 22%p 향상).
- Symbio의 lesson 기능이 이 방식에 해당한다. 다만 판정 신호가 분명할 때만 효과가 있다는 점이 중요하다.

### 2-6. 한도는 있되 "횟수"보다 "진전"을 본다
- OpenAI SDK의 `max_turns` 기본값은 10이지만 끌 수도 있다. 강한 모델은 반복을 스스로 알아채고 멈추기도 한다.
- Claude Code는 기본 한도 없이 돌고, 맥락이 차면 요약(compact)해서 계속 간다. Codex도 같은 방식이다.

## 3. Symbio에 맞춘 개선안 (효과 큰 순서)

### 1단계: 실행 중 복구 루프 (가장 효과가 큼)
1. **오류를 두 종류로 나눈다.**
   - 끝내야 하는 것: 승인 거절, 허용 폴더 밖 접근, 인증 실패, 사용자 취소. 지금처럼 바로 끝낸다. 안전 경계는 절대 느슨하게 하지 않는다.
   - 고칠 수 있는 것: 형식 위반, 없는 도구 이름, 파일 없음, 빈 응답, 되묻기와 도구 동시 호출 등. `Error`로 끝내지 말고 **도구 결과(오류 표시)로 모델에게 돌려준다.**
2. **도구 오류에 다음 행동을 넣는다.** "파일 없음" 대신 "그 이름은 없습니다. 같은 폴더의 비슷한 이름: A, B. 경로를 그대로 복사해 쓰세요"처럼 쓴다. 도구 입구에서 경로를 NFC/NFD 양쪽으로 맞춰 찾는다. 대표 실패 사례가 이것 하나로 사라진다.
3. **반복 감지를 루프 안에 넣는다.** 같은 도구, 같은 인자, 같은 결과가 2번이면 "같은 시도가 같은 결과를 냈습니다. 다른 방법을 쓰세요"를 넣는다. 3번이면 그 시점까지 알아낸 것을 정직하게 보고하고 멈춘다.
4. **한도를 "횟수 10"에서 "진전 + 시간"으로 바꾼다.** 예: 모델 호출 최대 20~25회와 전체 90초 정도. 대신 위의 반복 감지로 헛도는 실행을 일찍 끊는다. 화면에는 "다른 방법으로 다시 시도 중" 같은 진행 상태를 보여 준다.

### 2단계: 확인과 상위 모델 투입
5. **답하기 전에 확인한다.** 쓰기·이동 뒤에는 대상 파일이 실제로 있는지 코드가 확인한다(이미 일부 있음). 읽기·요약은 답이 도구 결과에 근거하는지 본다. 확인에 실패하면 루프로 돌아간다.
6. **실패하면 더 강한 모델로 다시 한다.** 평소에는 빠르고 싼 Groq 모델을 쓴다. 같은 실행에서 복구가 두 번 실패하면, 그때까지의 기록을 붙여 Claude Sonnet 같은 강한 모델이 이어받는다. "느려도 결국 해내는" 경험을 비용을 크게 늘리지 않고 만드는 가장 직접적인 방법이다. OpenRouter 키가 이미 있어 설정만 추가하면 된다.

### 3단계: 실행 사이의 학습 (지금의 피드백 루프 재정리)
7. **실시간으로는 코드를 고치지 않는다.** 사용자 요청 하나를 처리하는 도중에 코드를 바꾸면 검증할 방법이 없고, 안전 경계를 해칠 수 있다. 실시간 개선은 1~2단계의 "실행 중 복구"가 맡는다.
8. **코드 자동 수정은 오프라인으로만 한다.** 실패 기록 → 실패를 그대로 시험 과제로 저장(이미 목록 요청에 대해 구현됨) → 수정안 → 평가 통과 시에만 사용자에게 병합 제안. 자동 병합은 하지 않는다.
9. **lesson은 판정이 분명한 실패에만 붙인다.** 반복 호출, 파일 없음 같은 기계적 실패가 대상이다. "사용자가 다시 말했다"처럼 애매한 신호로 자동 채택하지 않는다.

### 측정
- 대표 지표 하나를 정한다: **"다시 요청 없이 한 번에 끝난 비율"**. 피드백 DB에서 바로 계산할 수 있다(완료이면서 10분 안에 R3가 없음). 지금 값을 기준선으로 남기고, 1단계 뒤 같은 지표로 비교한다.

## 4. 하지 말 것
- 안전 경계를 "복구 가능한 오류"로 분류해 모델이 우회하도록 두지 않는다.
- 한도를 없애지 않는다. 약한 모델은 스스로 멈추지 못한다.
- 범용 하네스를 다시 만들지 않는다. Symbio의 강점은 파일 작업 전용 도구와 승인 구조다. 루프 바깥(복구·감지·상위 모델)만 기업 방식을 따른다.

## 출처
- [Claude 문서: Handle tool calls (`is_error`)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)
- [Claude Code 문서: How the agent loop works](https://code.claude.com/docs/en/agent-sdk/agent-loop)
- [Anthropic: Building agents with the Claude Agent SDK](https://anthropic.com/engineering/building-agents-with-the-claude-agent-sdk)
- [Anthropic: Writing effective tools for agents](https://anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: Effective harnesses for long-running agents](https://anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [OpenAI Agents SDK: Running agents](https://openai.github.io/openai-agents-python/running%5Fagents/)
- [OpenAI: Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop)
- [SWE-agent: Agent Computer Interface](https://princeton-nlp.github.io/SWE-agent/background/aci)
- [Reflexion 논문 (arXiv 2303.11366)](https://arxiv.org/pdf/2303.11366)
- [Manus 맥락 설계 교훈 요약](https://tianpan.co/blog/2026-03-02-context-engineering-lessons-from-manus)
- [OpenRouter Agent SDK: Doom loop detection](https://openrouter.ai/docs/agent-sdk/call-model/doom-loop-detection)
- [Claude Code 루프 분석 (비공식)](https://blog.vincentqiao.com/en/posts/claude-code-agent-loop/)

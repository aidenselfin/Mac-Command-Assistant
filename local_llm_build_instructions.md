# Voice-Action AI — 로컬 LLM 설치 및 코드 교체 작업 지시서

새 대화에서 이 파일 전체를 읽은 뒤, 질문만 하지 말고 현재 저장소를 검사하고 단계별로 실제 빌드를 시작하라.

## 목표

macOS에서 다음 파이프라인을 완성한다.

```text
R-Cmd Push-to-Talk → faster-whisper 로컬 STT → Ollama 로컬 LLM(Qwen3)
→ Filesystem MCP tool calling → 허용 디렉터리 내 파일 작업 → 한국어 결과 출력
```

기존 저장소: https://github.com/aidenselfin/Mac-Command-Assistant

참고 구현 계획: `implementation_plan_v2(1).md`

## 이미 결정된 설계 제약

- 한 command 처리 중 provider/model은 하나만 사용한다.
- 동일 모델의 bounded tool loop는 최대 5회 허용한다.
- 초기 허용 디렉터리는 `~/Documents/voice-action-test` 하나로 제한한다.
- Filesystem MCP 도구는 연결 후 동적으로 발견한다.
- LLM에 노출하는 도구는 `list_directory`, `read_file` 또는 `read_text_file`, `write_file`, `create_directory`, `get_file_info` 등 MVP에 필요한 도구로 제한한다.
- 삭제·이동·rename 등 destructive tool은 기본적으로 노출하지 않는다.
- STT와 LLM 모두 로컬 실행하므로 transcript와 파일 내용이 외부 API로 나가지 않게 한다.
- `read → 요약 → write` 다단계 작업을 반드시 지원한다.
- `pytest`의 기존 fake OpenAI 테스트와 실제 MCP integration test를 모두 유지한다.
- 구현 순서는 Real MCP → Text→MCP → LLM→MCP → STT → PTT 음성 통합이다.

## 모델 결정

Runtime은 **Ollama**, 모델 계열은 **Qwen3**로 결정한다. Ollama의 OpenAI-compatible endpoint를 사용해 기존 `openai.AsyncOpenAI` 코드를 최대한 재사용한다.

먼저 다음으로 하드웨어를 확인하라.

```bash
uname -m
system_profiler SPHardwareDataType | grep -E "Chip|Memory"
sw_vers
```

모델 선택 규칙:

| 통합 메모리 | 모델 |
|---|---|
| 8GB | `qwen3:4b-instruct-2507-q4_K_M` |
| 16GB | `qwen3:8b` |
| 24GB | `qwen3:14b` |
| 32GB 이상 | `qwen3:30b-a3b-instruct-2507-q4_K_M` |

하드웨어를 확인할 수 없으면 우선 `qwen3:8b`로 시작하라. Qwen3 thinking 모델은 파일 작업의 latency와 tool loop 안정성을 위해 thinking을 끄거나 instruct 모델을 우선 사용하라.

## 작업 순서

### 1. 저장소 검사

현재 branch와 변경 사항을 먼저 확인한다. 사용자 변경 사항을 덮어쓰지 않는다.

```bash
git status --short
find . -maxdepth 3 -type f | sort
rg -n "anthropic|openai|provider|model|get_openai_tools|call_tool" .
```

활성 실행 경로는 `voice-action/main.py → planner.py → mcp_client.py`다. `llm.py`는 legacy 코드이므로 실제 import 여부를 확인한 뒤 별도로 처리한다.

### 2. Ollama 설치 및 모델 준비

Ollama macOS 앱을 설치하고 다음을 실행한다.

```bash
ollama --version
ollama serve
ollama pull qwen3:8b
```

모델은 실제 RAM에 맞게 위 표의 모델로 바꾼다. `ollama ps`와 `curl http://localhost:11434/v1/models`로 확인한다.

OpenAI 호환 endpoint를 위해 `Modelfile.voice-action`을 만든다.

```text
FROM qwen3:8b
PARAMETER num_ctx 16384
PARAMETER temperature 0.1
PARAMETER top_p 0.9
PARAMETER num_predict 1024
PARAMETER keep_alive 10m
```

```bash
ollama create voice-action-qwen3-8b -f Modelfile.voice-action
```

### 3. 기존 코드 교체

`voice-action/config.py`에 다음 설정을 추가한다.

```python
provider = "ollama"
model = "voice-action-qwen3-8b"
api_base_url = "http://localhost:11434/v1/"
llm_timeout_seconds = 120
max_output_tokens = 1024
max_tool_rounds = 5
```

`planner.py`에서 `provider in ("openai", "openrouter")`를 `("openai", "openrouter", "ollama")`로 확장한다.

Ollama client는 다음처럼 생성한다.

```python
if self.config.provider == "ollama":
    api_key = "ollama"  # local server에서는 값만 필요하고 검증하지 않음
    base_url = self.config.api_base_url or "http://localhost:11434/v1/"
else:
    # 기존 OpenAI/OpenRouter 처리 유지
    ...

client = openai.AsyncOpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=self.config.llm_timeout_seconds,
)
```

LLM 요청에는 다음을 추가한다.

```python
"max_tokens": self.config.max_output_tokens,
"temperature": 0.1,
```

Qwen3 thinking을 끌 수 있는 Ollama 버전이면 다음도 추가한다.

```python
if self.config.provider == "ollama":
    kwargs["reasoning_effort"] = "none"
```

`main.py`의 API key 검사는 Ollama일 때 건너뛰도록 수정한다. Ollama가 실행되지 않았거나 설정 모델이 없으면 명확한 오류를 출력한다.

### 4. MCP 안전성 보완

현재 `mcp_client.py`가 발견한 모든 tool을 노출하는지 검사한다. MVP allowlist만 `get_openai_tools()` 결과에 포함한다.

또한 `call_tool()`에서도 tool 이름을 재검증한다. 모델이 노출되지 않은 `delete_file`, `move_file` 등을 요청해도 실행하면 안 된다.

실제 MCP server의 tool 이름은 하드코딩하지 말고 startup의 `list_tools()` 결과를 출력해 확인한다. 이름이 `read_file`인지 `read_text_file`인지 schema를 기준으로 사용한다.

### 5. 실행 및 테스트

```bash
cd voice-action
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py --check
pytest -q
```

그 뒤 순서대로 실행한다.

```bash
python main.py --text "테스트 폴더 안의 파일 목록을 보여줘"
python main.py --text "테스트 폴더에 메모 파일을 하나 만들어줘"
python main.py --text "README.md를 읽고 요약해서 summary.txt로 저장해줘"
python main.py
```

검증할 항목:

- 한국어 명령 이해
- 올바른 MCP tool 선택
- 허용 디렉터리 밖 접근 차단
- `read → summarize → write` 성공
- 최대 5회 tool loop 준수
- 삭제·이동 tool 미노출
- API key나 파일 전체 내용이 로그에 노출되지 않음
- Ctrl+C 후 MCP subprocess가 남지 않음

각 시나리오를 20회 반복하고 성공률, 평균 latency, tool round 수를 기록한다. 기본 시나리오 성공률 90% 이상과 허용되지 않은 tool 호출 0회를 목표로 한다.

## 구현 시 주의

- 현재 테스트는 `openai.AsyncOpenAI`를 mock하므로 로컬 backend로 바꿔도 fake 테스트 구조를 유지한다.
- `llm.py`는 현재 planner와 다른 legacy 구조다. 실제 사용되지 않는다면 당장 섞지 말고, 사용처 확인 후 제거 또는 별도 migration한다.
- `--check`가 설정값만 출력하고 있으면 Ollama `/v1/models`, MCP 연결, 모델 존재 여부까지 실제 점검하도록 개선한다.
- 작업 중 문제가 생기면 한 번에 여러 계층을 바꾸지 말고 MCP, text mode, LLM, voice 순서로 원인을 분리한다.

## 완료 보고 형식

작업이 끝나면 다음을 보고한다.

1. 확인한 Mac 칩/RAM
2. 설치한 정확한 Ollama 모델명과 파일 크기
3. 수정한 파일과 핵심 변경점
4. 실행한 명령
5. pytest 결과
6. 세 가지 MVP 시나리오 결과
7. 남은 오류와 다음 작업


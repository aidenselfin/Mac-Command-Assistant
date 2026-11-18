# 현재 상태 (2026-11-12)

이 문서는 최신 검증·미완료 작업·인수인계만 둔다. 완료 이력은 `git log`와 [`reviews/`](reviews/),
진행 중인 작업 카드는 웹 패널(`~/.claude/voice-action/worklog.json`)이 맡는다.

# 최근 검증
- 2026-11-12 Python → Swift 전환(브랜치 `claude/swift-migration`): 백엔드·개발 도구 전부 `backend/<모듈>` Swift 패키지로 옮김.
  Swift 테스트 632개(`@Test`). 전환 뒤 기준 점검은 `scripts/check.sh full`이다.
- 전환 전 마지막 Python 점검 2026-11-11(main `0fef514`): pytest **773 passed**,
  평가 재생 **94/94**, 변형 L1~L3 **86/86**.
  재생 평가는 미리 적어 둔 모델 답(scripted)을 다시 돌리는 것이라 코드 경로·채점만 확인하고 모델 능력은 재지 않는다.
- 실제 모델 능력 점수의 마지막 기록(2026-10-28, `evals/benchmarks/20261028-144954.json`): 종합 **30**
  (보기 28·읽기 0·요약 22·정리 70·연결 58·파인더 0). 그 뒤 파일 열기 도구·목록 프롬프트 보강이 들어왔지만 다시 재지 않았다.
- 2026-11-11 클라우드 live(Groq gpt-oss-120b, 리눅스, 89과제×3): 프롬프트 보강 전 197/267 → 후 194/267(INVALID 각 21).
  합계는 변동 폭 안이고 목표 과제 `read_whole_file` 0→3, `see_find_by_ext` 1→3. 맥 전용 변환·열기 과제는 리눅스에서 늘 실패한다.
- 네이티브 앱: 2026-11-04 이후 화면 기능은 `symbio-dev ui-check run` 시나리오 4개로만 자동 확인한다.
  실제 마이크 녹음, 잠든 백엔드를 깨운 음성 명령, 이동·복사 승인 화면은 사람이 확인한 기록이 없다.

# 지난 갱신(2026-10-28) 뒤 들어온 것
- 앱: 오른쪽 ⌥ 두 번 → 입력창, ⌘⇧O 새 대화, ⌘⇧R 최근 파일·⌘⇧L 큰 파일 바로 목록, 결과 파일 드래그 칩,
  창이 닫혀 있을 때 선택·승인 칩, 10분 안 쓰면 백엔드 쉬기, 무음 녹음 버리기.
- 도구: 파일 열기(`local__open_file`), 비슷한 파일 묶기(`local__group_files`), hwpx→pdf 변환,
  삭제 대상을 말했거나 검색된 파일로 제한, 삭제 승인 화면 내용 미리보기.
- 피드백: 점수·원인 선택을 없애고 `/feedback` 명령으로 의견 받기, 실패한 목록 요청을 live 평가 과제로 저장.
- 제거: 의미 검색(EmbeddingGemma). worktree에서는 앱을 띄우지 않는다(`run.sh`가 막음).
- 개발: 화면 반영 검사(`symbio-dev ui-check`), 지침을 `CLAUDE.md`·`AGENTS.md`·스킬로 나눔.

# 미완료·다음 작업
0. 실행 중 복구 루프(브랜치 `claude/recovery-loop`, 병합 전): 재생·테스트만 확인. 실제 모델로 R2·R3가 줄었는지,
   `escalation_model`을 켠 비용은 live 평가로 아직 재지 않았다. 끝부분 정책 오류(후보 미확인 등)는 여전히 바로 끝난다.
1. 읽기 축: `read_whole_file`(5단계) 같은 긴 읽기가 약하다. 파인더 축은 파일 열기 도구 추가 뒤 다시 재야 한다.
2. 한글 파일명 깨짐: 모델이 경로를 자모가 깨진 이름(`회ᄋㅐ록_2월.txt`)으로 적어 `org_group_keyword_move`가 0/6.
   이동 목적지·후보 여럿 되묻기는 2026-11-18 코드로 막았다(`reviews/2026-11-18-prompt-ablation.md` "코드로 옮긴 뒤").
3. (비움)
4. 사람 확인: 위 "네이티브 앱" 항목.
5. Swift 전환 뒤 실제 앱 화면·live 평가로 Python 때와 같은지 사람 확인이 아직 없다.
6. 보류: [Claude Code 조사·개선 설계](reviews/2026-10-07-claude-code-research.md) 구현, 한/영 대체어·pdf/docx 본문 검색,
   HTML 반복 읽기, 변환 쓰기의 fd 기준 경합 방어, 네이티브 앱의 Finder 자동 배치·독립 배포.

# 인수인계
- 먼저 [AGENTS.md](../AGENTS.md) → [ARCHITECTURE.md](ARCHITECTURE.md) → [features.md](features.md). 기능을 바꾸면 features.md도 갱신한다.
- 코드는 worktree에서만 고치고, worktree에서는 앱을 띄우지 않는다. 화면 확인은 main에 병합한 뒤 메인 앱에서 한다.
- 모델이 질문을 남발하면 평가가 INVALID가 된다. free_text는 확인 질문·검색 전 질문을 앱이 돌려보내는 규칙(`freeTextRefusal`)이 있어야 한다.
- 공급자가 도구 호출을 스키마로 먼저 검사해 HTTP 400을 내므로 도구 스키마에는 상한·`oneOf`를 넣지 않고 코드에서 적용한다(메시지에 `tool_use_failed — <도구>: <위반>`).
- 백엔드는 전부 Swift(`backend/`)다. Python 명령(`uv`·`pytest`·`scripts/*.py`)은 더 없고 `symbio-dev`가 대신한다.
- 네이티브 앱은 승인 context를 백엔드(`symbio-bridge`)에만 두고 화면에는 1회용 승인 ID만 준다. 메시지를 바꾸면 [native-protocol.md](native-protocol.md)·`Backend.swift`·`Bridge.swift`를 함께 고친다.
- 피드백 DB의 lesson 후보 #2~#4는 미채택이다. 운영 `.voice-action/` DB와 사용자 시험 자료(`voice-action-test/`)는 커밋하거나 지우지 않는다.
- 메인 모델 Groq gpt-oss-120b, `OPENAI_API_KEY`가 있으면 실패 시 OpenAI 대체 모델, 분석 모델 OpenRouter Claude Sonnet 5.5, 음성 Groq Whisper. 세부 결정은 decisions.md.

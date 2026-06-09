# Agent Rules: Voice-Action AI

에이전트가 `opsx apply` 시 반드시 따라야 할 구현 규칙.

---

## 금지 패턴

- `os.unlink()` 직접 호출 금지 — 반드시 `_move_to_trash()` 경유
- tkinter 위젯을 핫키 루프 스레드에서 직접 생성·호출 금지
- `subprocess.run()` 시 `env=os.environ` 그대로 상속 금지 (API 키 노출 방지)
- `action` 검증 없이 파일 조작 금지 — 항상 화이트리스트 확인 후 실행

## 필수 패턴

- 모든 delete 액션: `_move_to_trash(src)` 호출
- tkinter UI: `threading.Thread(target=..., daemon=True).start()` 패턴 사용
- LLM 호출: 시스템 프롬프트에 `cache_control: {"type": "ephemeral"}` 반드시 포함
- 파일명 충돌: `_resolve_dst_conflict()` 적용

## 스택

- Python 3.11+, macOS 13+
- STT: faster-whisper (small 모델, language="ko")
- LLM: anthropic SDK, model="claude-haiku-4-5"
- 핫키: pynput
- 오디오: sounddevice + numpy + wave
- UI: tkinter (표준 라이브러리)

## 네이밍 컨벤션

- 공개 함수: snake_case
- 내부 함수: `_` 접두사
- 설정 경로: `~/.voice-action/` 고정
- 로그 경로: `~/.voice-action/logs/<ISO_TIMESTAMP>.json`

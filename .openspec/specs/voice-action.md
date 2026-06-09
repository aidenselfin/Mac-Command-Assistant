# Living Spec: Voice-Action AI

> 최초 작성: 2026-06-09  
> 현재 버전: v0 (구현 전)

---

## 제품 정의

macOS에서 R-cmd 핫키로 음성 명령을 받아 파일 시스템을 정리·분류하는 로컬 전용 AI 에이전트.

**핵심 원칙**
- 영구 삭제 금지 — 모든 delete는 `~/.Trash` 경유
- 단일 LLM 호출 — 명령당 Claude Haiku 1회
- 로컬 전용 — 클라우드 배포 없음
- 확인 게이트 — delete 포함 시 사용자 [실행] 클릭 필수

---

## 스택

| 레이어 | 선택 |
|--------|------|
| STT | faster-whisper small, ko |
| LLM | claude-haiku-4-5 (Prompt Caching) |
| 핫키 | pynput Right Command |
| 오디오 | sounddevice + numpy |
| UI | tkinter (별도 스레드) |
| 런타임 | Python 3.11+, macOS 13+ |

---

## 변경 이력

| 날짜 | 변경 | 링크 |
|------|------|------|
| — | (아카이브된 변경 없음) | — |

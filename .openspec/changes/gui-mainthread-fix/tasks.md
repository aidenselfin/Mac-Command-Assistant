# Tasks: GUI Main-Thread Fix

> 상태: DONE  
> 작성일: 2026-06-09

---

- [x] `ui/dispatcher.py` 생성
  - [x] `GuiRequest` dataclass 정의
  - [x] `_gui_queue: queue.Queue` 모듈 레벨 싱글톤
  - [x] `request_gui(func, *args, **kwargs) -> Any` 구현
  - [x] `run_gui_loop()` 구현 (None 수신 시 루프 종료)

- [x] `ui/preview.py` 수정
  - [x] `threading.Thread` 제거
  - [x] tkinter 코드 → `_show_preview_sync()` 분리
  - [x] `show_preview()` → `request_gui(_show_preview_sync, ...)` 경유

- [x] `ui/onboarding.py` 수정
  - [x] `threading.Thread` 제거 (기존에 없었으나 동일 패턴 적용)
  - [x] tkinter 코드 → `_show_onboarding_sync()` 분리
  - [x] `show_onboarding()` → `request_gui(_show_onboarding_sync, ...)` 경유

- [x] `main.py` 수정
  - [x] 핫키 리스너를 데몬 스레드로 분리
  - [x] 메인 스레드에서 `run_gui_loop()` 호출
  - [x] `--test` 모드에서도 `run_gui_loop()` 동작하도록 처리

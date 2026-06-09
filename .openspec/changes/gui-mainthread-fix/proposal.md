# Proposal: GUI Main-Thread Fix

> 상태: PROPOSED  
> 작성일: 2026-06-09  
> 범위: macOS tkinter 서브스레드 생성 거부 버그 수정

---

## 문제

`ui/preview.py`와 `ui/onboarding.py`가 `threading.Thread`에서 tkinter 창을 직접 생성함.  
macOS는 UI 프레임워크를 메인 스레드에서만 실행하도록 요구하며, 서브스레드에서 호출하면 OS 레벨에서 거부당함.

## 해결 방향

`queue.Queue`를 사용한 GUI 디스패처 패턴:
- 서브스레드는 GUI 요청을 큐에 넣고 결과를 기다림
- 메인 스레드가 큐를 폴링하며 tkinter 창을 직접 실행
- 서브스레드는 결과를 받은 후 계속 진행

## 변경 범위

- `ui/dispatcher.py` 신규 생성
- `ui/preview.py` 수정 — tkinter 코드를 동기 함수로 분리, 디스패처 경유
- `ui/onboarding.py` 수정 — 동일
- `main.py` 수정 — 메인 스레드가 `run_gui_loop()` 실행, 핫키 리스너는 데몬 스레드

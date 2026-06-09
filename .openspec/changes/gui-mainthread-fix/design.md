# Design: GUI Main-Thread Fix

> 상태: PROPOSED  
> 작성일: 2026-06-09

---

## 핵심 패턴: Queue-based GUI Dispatcher

```
[서브스레드 - pipeline]
    show_preview(actions)
        → GuiRequest 생성
        → _gui_queue.put(req)
        → req.result_queue.get()  ← 여기서 블로킹 대기
              ↓ (메인 스레드가 처리)
[메인 스레드 - run_gui_loop()]
    req = _gui_queue.get()
    result = req.func(*req.args)   ← tkinter 동기 실행
    req.result_queue.put(result)
              ↓
[서브스레드 - pipeline 재개]
    confirmed = True/False 수신 → 이어서 실행
```

## 모듈 구조

### ui/dispatcher.py (신규)

```python
@dataclass
class GuiRequest:
    func: Callable
    args: tuple
    kwargs: dict
    result_queue: queue.Queue   # 결과 반환용

def request_gui(func, *args, **kwargs) -> Any:
    # 서브스레드에서 호출 — 블로킹
    req = GuiRequest(func, args, kwargs, queue.Queue())
    _gui_queue.put(req)
    return req.result_queue.get()

def run_gui_loop():
    # 메인 스레드에서 호출 — 무한 루프
    while True:
        req = _gui_queue.get()
        if req is None:  # 종료 신호
            break
        result = req.func(*req.args, **req.kwargs)
        req.result_queue.put(result)
```

### ui/preview.py 변경

- `_show_preview_sync()`: 실제 tkinter 코드 (메인 스레드 전용)
- `show_preview()`: `request_gui(_show_preview_sync, ...)` 경유

### ui/onboarding.py 변경

- `_show_onboarding_sync()`: 실제 tkinter 코드
- `show_onboarding()`: `request_gui(_show_onboarding_sync, ...)` 경유

### main.py 변경

```python
def main():
    # 핫키 리스너 → 데몬 스레드
    listener = keyboard.Listener(...)
    listener.daemon = True
    listener.start()

    # 메인 스레드 → GUI 루프 (블로킹)
    run_gui_loop()
```

종료 시 `_gui_queue.put(None)`으로 루프 탈출.

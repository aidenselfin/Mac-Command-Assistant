import queue
from dataclasses import dataclass, field
from typing import Any, Callable

_gui_queue: queue.Queue = queue.Queue()


@dataclass
class GuiRequest:
    func: Callable
    args: tuple
    kwargs: dict
    result_queue: queue.Queue = field(default_factory=queue.Queue)


def request_gui(func: Callable, *args, **kwargs) -> Any:
    req = GuiRequest(func=func, args=args, kwargs=kwargs)
    _gui_queue.put(req)
    return req.result_queue.get()


def run_gui_loop() -> None:
    while True:
        req = _gui_queue.get()
        if req is None:
            break
        try:
            result = req.func(*req.args, **req.kwargs)
        except Exception as e:
            result = e
        req.result_queue.put(result)


def stop_gui_loop() -> None:
    _gui_queue.put(None)

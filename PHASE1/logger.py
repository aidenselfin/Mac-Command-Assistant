# logger.py — Smart-Homerow Phase 1: Data Logger
# 섹션 순서:
#   1. import 문 (표준 라이브러리 → 서드파티 순)
#   2. 상수 정의 (LOG_DIR, LOG_FILE, CSV_HEADER)
#   3. get_active_app_name()
#   4. get_element_name_at(x, y)
#   5. write_log(app_name, element_name)
#   6. on_click(x, y, button, pressed) 콜백
#   7. __main__ 블록 (Listener 시작)

# ── 1. imports ────────────────────────────────────────────────────────────────
import csv
from datetime import datetime
from pathlib import Path

from pynput.mouse import Listener, Button

# ── 2. 상수 ───────────────────────────────────────────────────────────────────
# 이 스크립트가 위치한 PHASE1/ 디렉터리 기준으로 logs/ 경로를 설정한다.
_BASE_DIR = Path(__file__).parent
LOG_DIR   = _BASE_DIR / "logs"
LOG_FILE  = LOG_DIR / "click_log.csv"
CSV_HEADER = ["timestamp", "app_name", "element_name", "action_type"]

# ── 3. 현재 활성 앱 이름 읽기 ──────────────────────────────────────────────────
def get_active_app_name() -> str:
    """
    현재 포커스된 앱의 이름을 반환한다.
    실패 시 'UNKNOWN_APP'을 반환하며 예외를 외부로 던지지 않는다.
    """
    try:
        from Cocoa import NSWorkspace
        app_info = NSWorkspace.sharedWorkspace().activeApplication()
        if app_info is None:
            return "UNKNOWN_APP"
        return app_info.get("NSApplicationName", "UNKNOWN_APP") or "UNKNOWN_APP"
    except Exception:
        return "UNKNOWN_APP"

# ── 4. 클릭 좌표에서 UI 요소 이름 읽기 ────────────────────────────────────────
def get_element_name_at(x: float, y: float) -> str:
    """
    주어진 (x, y) 화면 좌표에 위치한 UI 요소의 이름을 반환한다.
    AXTitle → AXDescription 순으로 시도하며, 둘 다 없으면 'UNKNOWN_ELEMENT' 반환.
    예외 발생 시에도 'UNKNOWN_ELEMENT'를 반환하며 스크립트를 중단하지 않는다.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide,
            AXUIElementCopyElementAtPosition,
            AXUIElementCopyAttributeValue,
            kAXTitleAttribute,
            kAXDescriptionAttribute,
        )

        system_element = AXUIElementCreateSystemWide()

        # 해당 좌표의 AXUIElement 취득
        error, element = AXUIElementCopyElementAtPosition(
            system_element, float(x), float(y), None
        )
        if error != 0 or element is None:
            return "UNKNOWN_ELEMENT"

        # AXTitle 우선 시도
        err_title, title = AXUIElementCopyAttributeValue(
            element, kAXTitleAttribute, None
        )
        if err_title == 0 and title:
            return str(title).strip() or "UNKNOWN_ELEMENT"

        # AXDescription 차선 시도
        err_desc, desc = AXUIElementCopyAttributeValue(
            element, kAXDescriptionAttribute, None
        )
        if err_desc == 0 and desc:
            return str(desc).strip() or "UNKNOWN_ELEMENT"

        return "UNKNOWN_ELEMENT"

    except Exception:
        return "UNKNOWN_ELEMENT"

# ── 5. CSV 파일 기록 ───────────────────────────────────────────────────────────
def write_log(app_name: str, element_name: str) -> None:
    """
    클릭 이벤트 데이터를 click_log.csv에 한 줄 추가한다.
    logs/ 디렉터리와 헤더 행이 없으면 자동으로 생성한다.
    PermissionError 등 파일 I/O 오류 발생 시 해당 클릭만 skip하고 계속 실행한다.
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_exists = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0

        with open(LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(CSV_HEADER)
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                app_name,
                element_name,
                "mouse_click",
            ])
    except PermissionError as e:
        print(f"[WARN] 파일 쓰기 권한 없음, 이 클릭은 기록 skip: {e}")
    except Exception as e:
        print(f"[WARN] write_log 예외 (기록 skip): {e}")

# ── 6. 마우스 클릭 콜백 ────────────────────────────────────────────────────────
def on_click(x: float, y: float, button: Button, pressed: bool) -> None:
    """
    마우스 클릭 이벤트 콜백.
    pressed=True (버튼 누름) 순간에만 데이터를 수집하고 기록한다.
    """
    if not pressed:
        return  # 마우스 버튼을 떼는 이벤트는 무시

    try:
        app_name     = get_active_app_name()
        element_name = get_element_name_at(x, y)
        write_log(app_name, element_name)
        print(f"[LOG] {app_name} | {element_name}")
    except Exception as e:
        print(f"[ERROR] on_click 처리 중 예외 발생: {e}")

# ── 7. 진입점 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  Smart-Homerow Data Logger — Phase 1")
    print("  종료하려면 Ctrl+C 를 누르세요.")
    print(f"  로그 저장 위치: {LOG_FILE.resolve()}")
    print("=" * 60)

    with Listener(on_click=on_click) as listener:
        listener.join()

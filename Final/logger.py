# logger.py — Smart-Homerow Phase 1: Enhanced Data Logger v2
# 개선사항:
#   1. 키보드 이벤트 추가 (단축키 입력 감지)
#   2. 중복 로그 제거 (app_switch 중복 필터링)
#   3. 마이크로초 타임스탬프 추가
#   4. 세션 ID 추가 (프로그램 시작 시 생성)
#   5. 시퀀스 기반 intent 분류 강화 (버튼 클릭 후 엔터 = "제출")

# ── 1. imports ────────────────────────────────────────────────────────────────
import csv
import threading
import time
from datetime import datetime
from pathlib import Path
from collections import deque

from pynput.mouse import Listener as MouseListener, Button
from pynput.keyboard import Listener as KeyboardListener, Key

# ── 2. 상수 및 전역 상태 ───────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent
LOG_DIR   = _BASE_DIR / "logs"
LOG_FILE  = LOG_DIR / "click_log.csv"
CSV_HEADER = ["timestamp", "session_id", "action_type", "app_name", "window_title", "element_name", "intent", "key_pressed"]

# 세션 ID: 프로그램 시작 시 생성 (타임스탬프 기반)
SESSION_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

# 파일 쓰기 충돌 방지용 Lock
log_lock = threading.Lock()

# 상태 기록용 전역 변수
previous_app_name = None
last_scroll_time = 0.0
last_app_switch_time = 0.0  # app_switch 중복 제거용
current_app_name = "UNKNOWN_APP"
current_window_title = "UNKNOWN_WINDOW"

# 시퀀스 기반 intent 분류를 위한 이벤트 히스토리
# (timestamp, action_type, element_name, intent) 형태로 최근 5개 이벤트 저장
event_history = deque(maxlen=5)

# 현재 키보드 상태 (단축키 감지용)
current_keys_pressed = set()

# ── 3. 현재 활성 앱 및 창 제목 읽기 ──────────────────────────────────────────────────
def get_active_app_and_window() -> tuple[str, str]:
    """
    현재 포커스된 앱의 이름과 활성화된 윈도우의 제목을 반환한다.
    """
    return current_app_name, current_window_title

def update_active_app_and_window() -> None:
    """
    메인 스레드에서 호출되어 현재 포커스된 앱과 윈도우 정보를 전역 변수에 갱신한다.
    """
    global current_app_name, current_window_title
    try:
        from Cocoa import NSWorkspace
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue
        )
        
        workspace = NSWorkspace.sharedWorkspace()
        app_info = workspace.frontmostApplication()
        
        if app_info is None:
            return
            
        app_name = app_info.localizedName() or app_info.bundleIdentifier() or "UNKNOWN_APP"
        window_title = "UNKNOWN_WINDOW"
        
        pid = app_info.processIdentifier()
        app_element = AXUIElementCreateApplication(pid)
        err, window = AXUIElementCopyAttributeValue(app_element, "AXFocusedWindow", None)
        if err == 0 and window:
            err, title = AXUIElementCopyAttributeValue(window, "AXTitle", None)
            if err == 0 and title:
                val = str(title).strip()
                if val:
                    window_title = " ".join(val.split())
                    
        current_app_name = app_name
        current_window_title = window_title
    except Exception:
        pass

# ── 4. 클릭 좌표에서 UI 요소 이름 및 의도(Intent) 읽기 ────────────────────────────────────────
def get_element_info_at(x: float, y: float) -> tuple[str, str]:
    """
    클릭한 좌표의 요소 이름(부모 컨텍스트 포함)과 행동의 목적(Intent)을 분석해 반환한다.
    """
    element_name = "UNKNOWN_ELEMENT"
    intent = "Focus/General"
    
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide,
            AXUIElementCopyElementAtPosition,
            AXUIElementCopyAttributeValue
        )

        system_element = AXUIElementCreateSystemWide()
        error, element = AXUIElementCopyElementAtPosition(
            system_element, float(x), float(y), None
        )
        if error != 0 or element is None:
            return element_name, intent

        # 1. Role 기반 Intent(목적) 분석
        err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
        role_str = str(role) if err == 0 and role else ""
        
        if role_str in ["AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"]:
            intent = "Input Preparation"
        elif role_str in ["AXButton", "AXLink", "AXMenuItem", "AXCheckBox", "AXRadioButton"]:
            intent = "Interaction"
        elif role_str in ["AXTabGroup", "AXOutline", "AXTable", "AXBrowser", "AXScrollArea"]:
            intent = "Navigation"
        else:
            intent = "Focus/General"

        # 2. 부모 요소(Parent Context) 가져오기
        parent_context = ""
        err, parent = AXUIElementCopyAttributeValue(element, "AXParent", None)
        if err == 0 and parent:
            err, p_role_desc = AXUIElementCopyAttributeValue(parent, "AXRoleDescription", None)
            if err == 0 and p_role_desc:
                p_role_str = str(p_role_desc).strip()
                # 윈도우나 앱 자체 같은 너무 큰 부모는 제외
                if p_role_str and p_role_str not in ["윈도우", "window", "응용 프로그램", "application"]:
                    parent_context = f"[{p_role_str}] "

        # 3. 클릭한 요소의 의미 있는 텍스트 추출
        attributes_to_try = [
            "AXTitle",
            "AXDescription",
            "AXHelp",
            "AXRoleDescription",
            "AXRole"
        ]

        for attr in attributes_to_try:
            err, val = AXUIElementCopyAttributeValue(element, attr, None)
            if err == 0 and val:
                val_str = str(val).strip()
                if not val_str:
                    continue
                
                val_str = " ".join(val_str.split())
                if len(val_str) > 50:
                    val_str = val_str[:47] + "..."
                    
                if attr in ("AXRoleDescription", "AXRole"):
                    val_str = f"<{val_str}>"
                    
                element_name = parent_context + val_str
                break

    except Exception:
        pass

    return element_name, intent

# ── 5. 시퀀스 기반 Intent 정제 ──────────────────────────────────────────────────
def refine_intent_with_sequence(action_type: str, element_name: str, intent: str) -> str:
    """
    최근 이벤트 히스토리를 보고 intent를 정제한다.
    예: [Button Click] → [Enter Key] = "Submission"
    """
    if not event_history:
        return intent
    
    last_event = event_history[-1]
    _, last_action, last_element, last_intent = last_event
    
    # 패턴 1: 버튼 클릭 직후 엔터 입력 → "Submission"
    if (last_action == "mouse_click" and 
        last_intent == "Interaction" and 
        action_type == "key_press" and 
        element_name in ["Return", "Enter"]):
        return "Submission"
    
    # 패턴 2: 텍스트 입력 중 엔터 입력 → "Form Submission" or "Command Execution"
    if (last_action == "mouse_click" and 
        last_intent == "Input Preparation" and 
        action_type == "key_press" and 
        element_name in ["Return", "Enter"]):
        return "Form Submission"
    
    # 기본값 반환
    return intent

# ── 6. CSV 파일 기록 ───────────────────────────────────────────────────────────
def write_log(app_name: str, window_title: str, element_name: str, action_type: str, intent: str, key_pressed: str = "") -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        
        with log_lock:
            file_exists = LOG_FILE.exists() and LOG_FILE.stat().st_size > 0
            with open(LOG_FILE, mode="a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(CSV_HEADER)
                
                # 마이크로초 타임스탬프
                now = datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # 밀리초 단위
                
                writer.writerow([
                    timestamp,
                    SESSION_ID,
                    action_type,
                    app_name,
                    window_title,
                    element_name,
                    intent,
                    key_pressed
                ])
    except PermissionError as e:
        print(f"[WARN] 파일 쓰기 권한 없음: {e}")
    except Exception as e:
        print(f"[WARN] write_log 예외: {e}")

# ── 7. 마우스 클릭 콜백 ────────────────────────────────────────────────────────
def on_click(x: float, y: float, button: Button, pressed: bool) -> None:
    global previous_app_name
    
    if not pressed:
        return

    try:
        app_name, window_title = get_active_app_and_window()
        element_name, intent = get_element_info_at(x, y)
        
        action_type = "mouse_click"
        previous_app_name = app_name
        
        # 이벤트 히스토리 추가
        event_history.append((time.time(), action_type, element_name, intent))
        
        write_log(app_name, window_title, element_name, action_type, intent)
        print(f"[LOG] {app_name}({action_type}) | {window_title[:20]} | {element_name[:30]} | {intent}")
    except Exception as e:
        print(f"[ERROR] on_click 예외: {e}")

# ── 8. 마우스 스크롤 콜백 ──────────────────────────────────────────────────────
def on_scroll(x: float, y: float, dx: float, dy: float) -> None:
    global last_scroll_time
    
    current_time = time.time()
    # 너무 많은 스크롤 이벤트가 발생하지 않도록 1초에 한 번만 기록 (Debounce)
    if current_time - last_scroll_time < 1.0:
        return
        
    last_scroll_time = current_time

    try:
        app_name, window_title = get_active_app_and_window()
        element_name, intent = get_element_info_at(x, y)
        
        action_type = "mouse_scroll"
        intent = "Navigation"  # 스크롤의 목적은 무조건 탐색
        
        # 스크롤 방향 기록
        scroll_direction = f"dy:{dy:.1f}"
        
        event_history.append((time.time(), action_type, element_name, intent))
        
        write_log(app_name, window_title, element_name, action_type, intent, scroll_direction)
        print(f"[LOG] {app_name}({action_type}) | {window_title[:20]} | {element_name[:30]} | {intent}")
    except Exception as e:
        print(f"[ERROR] on_scroll 예외: {e}")

# ── 9. 키보드 이벤트 콜백 ──────────────────────────────────────────────────────
def on_key_press(key):
    """키보드 키 눌림 감지"""
    global current_keys_pressed
    
    try:
        app_name, window_title = get_active_app_and_window()
        
        # 키 이름 추출
        try:
            key_name = key.char if hasattr(key, 'char') else str(key).replace("Key.", "")
        except AttributeError:
            key_name = str(key).replace("Key.", "")
        
        current_keys_pressed.add(key_name)
        
        # 단축키 조합 감지 (Cmd, Ctrl, Shift, Option)
        is_shortcut = any(k in current_keys_pressed for k in ['cmd', 'ctrl', 'shift', 'alt'])
        
        action_type = "key_press_shortcut" if is_shortcut else "key_press"
        intent = "Shortcut Execution" if is_shortcut else "Text Input"
        
        element_name = key_name
        
        # 시퀀스 기반 intent 정제
        refined_intent = refine_intent_with_sequence(action_type, element_name, intent)
        
        # 이벤트 히스토리 추가
        event_history.append((time.time(), action_type, element_name, refined_intent))
        
        write_log(app_name, window_title, element_name, action_type, refined_intent, key_name)
        print(f"[LOG] {app_name}({action_type}) | {window_title[:20]} | {key_name} | {refined_intent}")
        
    except Exception as e:
        print(f"[ERROR] on_key_press 예외: {e}")

def on_key_release(key):
    """키보드 키 해제 감지"""
    global current_keys_pressed
    
    try:
        try:
            key_name = key.char if hasattr(key, 'char') else str(key).replace("Key.", "")
        except AttributeError:
            key_name = str(key).replace("Key.", "")
        
        current_keys_pressed.discard(key_name)
        
    except Exception as e:
        print(f"[ERROR] on_key_release 예외: {e}")

# ── 10. 백그라운드 앱 전환 모니터링 ────────────────────────────────────────────────
def poll_active_app() -> None:
    """
    주기적으로 활성화된 앱을 검사하여 스와이프나 단축키로 인한 화면 전환을 감지한다.
    메인 스레드의 런루프 펌핑과 함께 주기적으로 호출된다.
    """
    global previous_app_name, last_app_switch_time
    
    update_active_app_and_window()
    app_name, window_title = current_app_name, current_window_title
    
    if app_name != "UNKNOWN_APP":
        if previous_app_name is None:
            # 첫 실행 시 초기화
            previous_app_name = app_name
        elif previous_app_name != app_name:
            # 중복 방지: 0.2초 이내 app_switch 무시
            current_time = time.time()
            if current_time - last_app_switch_time < 0.2:
                previous_app_name = app_name
                return
            
            last_app_switch_time = current_time
            
            action_type = "app_switch"
            element_name = "[System] App Activated"
            intent = "Workspace Switch"
            
            event_history.append((time.time(), action_type, element_name, intent))
            
            write_log(app_name, window_title, element_name, action_type, intent)
            print(f"[LOG] {app_name}({action_type}) | {window_title[:20]} | {element_name}")
            
            previous_app_name = app_name

# ── 11. 진입점 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if LOG_FILE.exists():
        LOG_FILE.unlink()

    print("=" * 70)
    print("  Smart-Homerow Data Logger v2 — Phase 1 Enhanced")
    print("  (Event Tracking: Mouse + Keyboard, Sequence-based Intent)")
    print("  종료하려면 Ctrl+C 를 누르세요.")
    print(f"  Session ID: {SESSION_ID}")
    print(f"  로그 저장 위치: {LOG_FILE.resolve()}")
    print("=" * 70)

    try:
        update_active_app_and_window()
        previous_app_name = current_app_name

        from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
        
        # 마우스 Listener (백그라운드 스레드)
        mouse_listener = MouseListener(on_click=on_click, on_scroll=on_scroll)
        mouse_listener.start()
        
        # 키보드 Listener (백그라운드 스레드)
        keyboard_listener = KeyboardListener(on_press=on_key_press, on_release=on_key_release)
        keyboard_listener.start()

        # 메인 스레드에서 런루프 실행
        while True:
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)
            poll_active_app()
            time.sleep(0.01)
            
    except KeyboardInterrupt:
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")
    finally:
        if 'mouse_listener' in locals():
            mouse_listener.stop()
        if 'keyboard_listener' in locals():
            keyboard_listener.stop()

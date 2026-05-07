import sys
import os
import threading
import time
import objc
from pathlib import Path
import AppKit
import Quartz
from ApplicationServices import (
    AXUIElementCreateSystemWide,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementPerformAction,
    AXValueGetValue,
    kAXValueCGSizeType,
    kAXValueCGPointType
)
from pynput import keyboard

# Cmd+Ctrl+알파벳 단축키에 대한 물리적 키코드 → 문자 매핑 (QWERTY 기준)
KEYCODE_TO_CHAR = {
    0: 'A',  1: 'S',  2: 'D',  3: 'F',  4: 'H',  5: 'G',  6: 'Z',  7: 'X',
    8: 'C',  9: 'V', 11: 'B', 12: 'Q', 13: 'W', 14: 'E', 15: 'R', 16: 'Y',
   17: 'T', 31: 'O', 32: 'U', 34: 'I', 35: 'P', 37: 'L', 38: 'J', 40: 'K',
   45: 'N', 46: 'M',
}

overlay_controller = None
OUR_PID = os.getpid()  # 오버레이 자신의 PID (창 감지에서 제외)

class OverlayView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(OverlayView, self).initWithFrame_(frame)
        if self:
            self.window_groups = []
            self.active_pid = None
        return self

    def isFlipped(self):
        return True

    def setRects_activePid_(self, window_groups, active_pid):
        self.window_groups = window_groups
        self.active_pid = active_pid
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        # 1. 투명 배경으로 화면 지우기
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(dirtyRect)

        if not hasattr(self, 'window_groups') or not self.window_groups:
            return

        sorted_groups = sorted(self.window_groups, key=lambda g: g['z_index'], reverse=True)

        # 색상 및 스타일 정의
        tag_bg_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0) # #FFE600
        tag_text_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.102, 0.102, 0.102, 1.0) # #1A1A1A

        font = AppKit.NSFont.fontWithName_size_("Menlo-Bold", 12.0)
        if font is None:
            font = AppKit.NSFont.fontWithName_size_("Monaco", 12.0)
        if font is None:
            font = AppKit.NSFont.userFixedPitchFontOfSize_(12.0)

        text_attrs = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: tag_text_color,
            AppKit.NSKernAttributeName: 0.5
        }

        active_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0)
        inactive_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 0.4)

        # === Pass 1: 모든 창 테두리 먼저 그리기 ===
        for group in sorted_groups:
            is_active_group = (group.get('pid') == self.active_pid)
            line_width = 1.5 if is_active_group else 1.0
            border_color = active_color if is_active_group else inactive_color

            for pane in group['panes']:
                x, y, w, h = pane['rect']

                ns_rect = AppKit.NSMakeRect(x, y, w, h)
                inset_rect = AppKit.NSInsetRect(ns_rect, line_width / 2.0, line_width / 2.0)
                path = AppKit.NSBezierPath.bezierPathWithRect_(inset_rect)
                path.setLineWidth_(line_width)
                border_color.set()
                path.stroke()

        # === Pass 2: 모든 태그 버튼을 테두리 위(상위 레이어)에 그리기 ===
        for group in sorted_groups:
            for pane in group['panes']:
                x, y, w, h = pane['rect']

                tag_str = pane.get('tag', '?')
                ns_str = AppKit.NSString.stringWithString_(tag_str)
                tag_size = ns_str.sizeWithAttributes_(text_attrs)

                pad_x, pad_y = 6.0, 2.0
                tag_rect_w = tag_size.width + pad_x * 2.0
                tag_rect_h = tag_size.height + pad_y * 2.0
                tag_x = x - 4.0
                tag_y = y - 4.0
                tag_rect = AppKit.NSMakeRect(tag_x, tag_y, tag_rect_w, tag_rect_h)

                # 태그 배경
                tag_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tag_rect, 4.0, 4.0)
                tag_bg_color.set()
                tag_path.fill()

                # 텍스트 그리기 (수직/수평 중앙 정렬)
                text_rect = AppKit.NSMakeRect(
                    tag_x + pad_x,
                    tag_y + pad_y,
                    tag_size.width,
                    tag_size.height
                )
                ns_str.drawInRect_withAttributes_(text_rect, text_attrs)

class OverlayController(AppKit.NSObject):
    def init(self):
        self = objc.super(OverlayController, self).init()
        if self:
            self.window = None
            self.view = None
            self._pending_rects = None
            self._lock = threading.Lock()
            self.setupWindow()
            self.start_polling()
            self.start_ui_timer()
        return self

    def setupWindow(self):
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        
        # 전체 화면 덮는 투명 창
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen_frame,
            AppKit.NSWindowStyleMaskBorderless,
            AppKit.NSBackingStoreBuffered,
            False
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        # 마우스 클릭이 뒤에 있는 앱으로 그대로 통과하도록 설정
        self.window.setIgnoresMouseEvents_(True)
        # 항상 위
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        # Space 전환 시 오버레이가 제자리에 고정되도록 (ghost 방지)
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorStationary |
            AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )
        
        self.view = OverlayView.alloc().initWithFrame_(screen_frame)
        self.window.setContentView_(self.view)
        
        self.window.setAlphaValue_(1.0)
        self.window.orderFrontRegardless()

    @objc.python_method
    def get_quartz_snapshot(self):
        """Tier 1: 쿼리 Quartz 창 목록만 읽는 저렴한 스캔 (AX API 호출 없음)"""
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        
        quartz_windows = []
        visible_pids = set()
        
        for i, win in enumerate(window_list):
            layer = win.get(Quartz.kCGWindowLayer, 1)
            alpha = win.get(Quartz.kCGWindowAlpha, 1.0)
            pid = win.get(Quartz.kCGWindowOwnerPID)
            bounds = win.get(Quartz.kCGWindowBounds)
            
            if layer == 0 and alpha > 0.05 and pid and bounds and pid != OUR_PID:
                w, h = bounds.get('Width', 0), bounds.get('Height', 0)
                if w > 100 and h > 100:
                    x, y = bounds.get('X', 0), bounds.get('Y', 0)
                    quartz_windows.append({'rect': (x, y, w, h), 'pid': pid, 'z_index': i})
                    visible_pids.add(pid)
        
        # 변경 감지를 위한 hashable key (pid + 좌표 + z_order)
        snapshot_key = frozenset((qw['pid'], qw['rect'], qw['z_index']) for qw in quartz_windows)
        return quartz_windows, visible_pids, snapshot_key

    @objc.python_method
    def start_polling(self):
        def poll():
            last_key = None
            while True:
                try:
                    quartz_windows, visible_pids, snapshot_key = self.get_quartz_snapshot()
                    
                    if snapshot_key != last_key:
                        # Tier 2: 변경이 감지된 경우만 AX 딥은 스캔 실행
                        last_key = snapshot_key
                        rects = self.get_target_rects(quartz_windows, visible_pids)
                        with self._lock:
                            self._pending_rects = rects
                        print(f"[DEBUG] 화면 변경 감지 → 태그 재계산: {sum(len(g['panes']) for g in rects)}개")
                except Exception as e:
                    print(f"[ERROR] Polling thread crashed: {e}")
                    import traceback; traceback.print_exc()
                time.sleep(0.1)  # 0.1초마다 Quartz 스냅샷 (CPU 부하 낙음)
        threading.Thread(target=poll, daemon=True).start()

    @objc.python_method
    def start_ui_timer(self):
        # 메인 스레드에서 실행되는 NSTimer로 UI를 안전하게 업데이트
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "refreshUI:", None, True
        )

    def refreshUI_(self, timer):
        with self._lock:
            rects = self._pending_rects
            self._pending_rects = None
        if rects is not None:
            # 결과가 0개여도 즉시 화면을 지우도록 (ghost 방지)
            active_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            active_pid = active_app.processIdentifier() if active_app else None
            self.view.setRects_activePid_(rects, active_pid)
            print(f"[UI] 태그 업데이트 완료: {sum(len(g['panes']) for g in rects)}개")

    @objc.python_method
    def handle_tag_global(self, tag_char):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("doHandleTag:", tag_char, False)

    def doHandleTag_(self, tag_char):
        with self._lock:
            groups = self.view.window_groups
        for group in groups:
            for pane in group['panes']:
                if pane.get('tag') == tag_char:
                    element = pane['element']
                    pid = group['pid']
                    is_fullscreen = group.get('is_fullscreen', False)

                    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    if app:
                        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

                    if is_fullscreen:
                        # 전체화면 패널: 패널 중앙을 마우스 클릭으로 포커스
                        x, y, w, h = pane['rect']
                        click_x = x + w / 2.0
                        click_y = y + h / 2.0
                        print(f"[DEBUG] 전체화면 패널 선택: tag={tag_char}, 클릭 좌표=({click_x:.0f}, {click_y:.0f})")
                        self._simulate_click_delayed(click_x, click_y)
                    else:
                        # 창 모드: 창을 앞으로 올리기 (기존 동작 유지)
                        AXUIElementPerformAction(element, "AXRaise")
                    return

    @objc.python_method
    def _simulate_click_delayed(self, x, y, delay=0.08):
        """앱 활성화 후 Quartz 이벤트로 특정 좌표에 좌클릭 시뮬레이션"""
        def do_click():
            time.sleep(delay)  # 앱이 활성화될 때까지 잠깐 대기
            point = Quartz.CGPointMake(x, y)
            mouse_down = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft
            )
            mouse_up = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft
            )
            # Cmd+Ctrl 키가 아직 눌린 상태에서 발생하면
            # macOS가 Ctrl+Click을 우클릭으로 해석함 → 수식키 플래그를 명시적으로 제거
            Quartz.CGEventSetFlags(mouse_down, 0)
            Quartz.CGEventSetFlags(mouse_up, 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_up)
            print(f"[DEBUG] 좌클릭 시뮬레이션 완료: ({x:.0f}, {y:.0f})")
        threading.Thread(target=do_click, daemon=True).start()

    @objc.python_method
    def get_target_rects(self, quartz_windows=None, visible_pids=None):
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        sw = screen_frame.size.width
        sh = screen_frame.size.height

        # 1. 화면에 보이는 창 필터링 (Quartz API)
        if quartz_windows is None or visible_pids is None:
            quartz_windows, visible_pids, _ = self.get_quartz_snapshot()

        window_groups = []

        # 2. 재귀 탐색을 통한 패널 영역 추출 (DFS) - 전체화면 앱 전용
        def find_panes(element, depth, panes_list, top_rect):
            if depth > 12: return

            # 크기 확인 (크기 정보가 없어도 children 탐색은 계속)
            err_s, size_val = AXUIElementCopyAttributeValue(element, "AXSize", None)
            sz_width, sz_height = 0, 0
            if err_s == 0 and size_val:
                succ_s, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)
                if succ_s:
                    sz_width, sz_height = sz.width, sz.height

            # 크기가 확인되었고 너무 작은 경우에만 이 요소와 하위를 모두 건너뜀
            if sz_width > 0 and sz_height > 0 and (sz_width < 100 or sz_height < 100):
                return

            err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
            # role이 분할 관련이고 유효한 크기 정보가 있을 때 panes에 추가
            if err == 0 and role in ["AXSplitGroup", "AXTabGroup", "AXScrollArea", "AXWebArea", "AXGroup"]:
                err_p, pos_val = AXUIElementCopyAttributeValue(element, "AXPosition", None)
                if err_p == 0 and pos_val:
                    succ_p, pos = AXValueGetValue(pos_val, kAXValueCGPointType, None)
                    if succ_p:
                        w, h = sz_width, sz_height
                        x, y = pos.x, pos.y
                        if w > 100 and h > 100 and (x < sw and y < sh and x + w > 0 and y + h > 0):
                            is_dup = False
                            for pane in panes_list:
                                rx, ry, rw, rh = pane['rect']
                                if abs(x - rx) < 15 and abs(y - ry) < 15 and abs(w - rw) < 15 and abs(h - rh) < 15:
                                    is_dup = True
                                    break
                            if not is_dup:
                                print(f"[DEBUG] 전체화면 패널 감지됨 ({role}) -> x={x}, y={y}, w={w}, h={h}")
                                panes_list.append({'rect': (x, y, w, h), 'element': element})

            # role에 무관하게 항상 children 탐색 (중간 컨테이너 건너뛰지 않음)
            err_c, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
            if err_c == 0 and children:
                for child in children:
                    find_panes(child, depth + 1, panes_list, top_rect)

        # 3. 수집된 PID를 순회하며 창 모드에 따라 다르게 처리
        for pid in visible_pids:
            app_element = AXUIElementCreateApplication(pid)
            err, windows = AXUIElementCopyAttributeValue(app_element, "AXWindows", None)
            if err == 0 and windows:
                for window in windows:
                    # 최소화된 창은 스킵
                    err_m, is_minimized = AXUIElementCopyAttributeValue(window, "AXMinimized", None)
                    if err_m == 0 and is_minimized:
                        continue

                    err_p, pos_val = AXUIElementCopyAttributeValue(window, "AXPosition", None)
                    err_s, size_val = AXUIElementCopyAttributeValue(window, "AXSize", None)

                    if err_p == 0 and err_s == 0 and pos_val and size_val:
                        succ_p, pos = AXValueGetValue(pos_val, kAXValueCGPointType, None)
                        succ_s, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)

                        if succ_p and succ_s:
                            ax_x, ax_y, ax_w, ax_h = pos.x, pos.y, sz.width, sz.height

                            # 매칭되는 Quartz 윈도우 찾기
                            best_match = None
                            best_diff = 99999
                            for qw in quartz_windows:
                                if qw['pid'] == pid:
                                    qx, qy, qw_w, qw_h = qw['rect']
                                    diff = abs(ax_x - qx) + abs(ax_y - qy) + abs(ax_w - qw_w) + abs(ax_h - qw_h)
                                    if diff < best_diff and diff < 50:
                                        best_match = qw
                                        best_diff = diff

                            if best_match:
                                top_rect = best_match['rect']

                                # 전체화면 여부 확인
                                err_fs, is_fullscreen = AXUIElementCopyAttributeValue(window, "AXFullScreen", None)
                                is_fs = (err_fs == 0 and is_fullscreen == True)

                                if is_fs:
                                    # [전체화면 모드] window의 children부터 DFS 탐색 시작
                                    # (window 자체가 아닌 내부 split/group 요소 탐색)
                                    print(f"[DEBUG] 전체화면 앱 감지 (pid={pid}) → 내부 패널 스캔")
                                    panes = []
                                    err_c, win_children = AXUIElementCopyAttributeValue(window, "AXChildren", None)
                                    if err_c == 0 and win_children:
                                        for child in win_children:
                                            find_panes(child, 0, panes, top_rect)
                                    if not panes:
                                        # 내부 패널 못 찾으면 창 전체를 하나로
                                        panes.append({'rect': (ax_x, ax_y, ax_w, ax_h), 'element': window})
                                else:
                                    # [바탕화면 창 모드] 창 자체에만 태그 부여, 내부 스캔 없음
                                    print(f"[DEBUG] 창 모드 앱 감지 (pid={pid}) → 위치: x={ax_x}, y={ax_y} / 크기: w={ax_w}, h={ax_h}")
                                    panes = [{'rect': (ax_x, ax_y, ax_w, ax_h), 'element': window}]

                                window_groups.append({
                                    'z_index': best_match['z_index'],
                                    'top_rect': top_rect,
                                    'panes': panes,
                                    'pid': pid,
                                    'is_fullscreen': is_fs
                                })

        window_groups.sort(key=lambda g: g['z_index'])

        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        tag_index = 0

        for group in window_groups:
            for pane in group['panes']:
                tag_str = chars[tag_index] if tag_index < 26 else chars[tag_index//26 - 1] + chars[tag_index%26]
                pane['tag'] = tag_str
                tag_index += 1

        return window_groups

def setup_event_tap():
    """
    Quartz CGEventTap 기반 단축키 인터셉터.
    pynput.GlobalHotKeys와 달리 이벤트를 완전히 소비(suppress)하므로
    기존 앱 단축키보다 우리 프로그램이 항상 우선됨.
    """
    FLAG_CMD  = Quartz.kCGEventFlagMaskCommand
    FLAG_CTRL = Quartz.kCGEventFlagMaskControl
    FLAG_SHIFT = Quartz.kCGEventFlagMaskShift
    FLAG_ALT  = Quartz.kCGEventFlagMaskAlternate
    REQUIRED  = FLAG_CMD | FLAG_CTRL  # Cmd + Ctrl 만 눌렸을 때

    def callback(proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventKeyDown:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode
            )
            flags = Quartz.CGEventGetFlags(event)
            # Cmd+Ctrl 이외의 수식키(Shift, Alt)가 없는 경우만 처리
            relevant = flags & (FLAG_CMD | FLAG_CTRL | FLAG_SHIFT | FLAG_ALT)
            if relevant == REQUIRED:
                char = KEYCODE_TO_CHAR.get(keycode)
                if char and overlay_controller:
                    overlay_controller.handle_tag_global(char)
                    return None  # ← 이벤트 소비: 다른 앱에 전달 안 됨
        return event  # 그 외 이벤트는 그대로 통과

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,       # 세션 레벨 (앱보다 우선)
        Quartz.kCGHeadInsertEventTap,    # 큐 맨 앞에 삽입 (최우선)
        Quartz.kCGEventTapOptionDefault, # 이벤트 수정/소비 허용
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
        callback,
        None
    )
    if tap is None:
        print("[ERROR] CGEventTap 생성 실패 — 시스템 환경설정 > 개인 정보 보호 및 보안 > 손쉬운 사용에서 터미널(또는 Python)을 허용하세요.")
        return

    # 메인 CFRunLoop에 등록 (AppHelper.runEventLoop와 같은 루프 공유)
    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetMain(),
        source,
        Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(tap, True)
    print("[INFO] CGEventTap 활성화 완료 — 단축키(Cmd+Ctrl+알파벳)가 우선 처리됩니다.")

if __name__ == '__main__':
    print("=" * 60)
    print("  Smart-Homerow Phase 3: 상시 모니터링형 Overlay Engine 시작")
    print("  모든 창과 패널에 알파벳 단축키 태그가 항상 표시됩니다.")
    print("  이동 단축키: Cmd + Ctrl + [알파벳]")
    print("  종료하려면 터미널에서 Ctrl+C 를 누르세요.")
    print("=" * 60)
    
    # AppKit 초기화 및 런루프 시작 (GUI 그리기를 위해 필수)
    app = AppKit.NSApplication.sharedApplication()
    # Accessory 모드: 독 아이콘 없이 오버레이만 보임
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    overlay_controller = OverlayController.alloc().init()
    
    # CGEventTap을 메인 런루프에 등록 (별도 스레드 불필요)
    setup_event_tap()
    
    # Ctrl+C 처리를 위해 NSRunLoop를 주기적으로 깨워주는 더미 타이머 추가
    class TimerObj(AppKit.NSObject):
        def tick_(self, timer):
            pass
    
    timer_obj = TimerObj.alloc().init()
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.1, timer_obj, "tick:", None, True
    )

    import signal
    import os
    
    def sigint_handler(sig, frame):
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")
        os._exit(0)
        
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        from PyObjCTools import AppHelper
        AppHelper.runEventLoop(installInterrupt=True)
    except KeyboardInterrupt:
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")
        os._exit(0)

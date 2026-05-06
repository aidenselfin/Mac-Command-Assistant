import sys
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

overlay_controller = None

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

        # Z-Order가 큰 것(Back)부터 작은 것(Front) 순으로 정렬하여 렌더링
        sorted_groups = sorted(self.window_groups, key=lambda g: g['z_index'], reverse=True)

        # 색상 및 스타일 정의
        active_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0) # #FFE600
        inactive_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 0.4)
        tag_bg_color = active_color
        tag_text_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.102, 0.102, 0.102, 1.0) # #1A1A1A

        shadow = AppKit.NSShadow.alloc().init()
        shadow.setShadowOffset_(AppKit.NSMakeSize(0, -2.0))
        shadow.setShadowBlurRadius_(4.0)
        shadow.setShadowColor_(AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.25))

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

        for group in sorted_groups:
            top_x, top_y, top_w, top_h = group['top_rect']
            ns_top_rect = AppKit.NSMakeRect(top_x, top_y, top_w, top_h)
            
            # 2. DestinationOut으로 현재 창 영역만큼 배경 투명도 증가
            AppKit.NSGraphicsContext.currentContext().saveGraphicsState()
            AppKit.NSGraphicsContext.currentContext().setCompositingOperation_(AppKit.NSCompositingOperationDestinationOut)
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.6).set() 
            AppKit.NSRectFill(ns_top_rect)
            AppKit.NSGraphicsContext.currentContext().restoreGraphicsState()

            is_active_group = (group.get('pid') == self.active_pid)
            line_width = 1.5 if is_active_group else 1.0
            border_color = active_color if is_active_group else inactive_color

            # 3. 현재 창의 내부 패널 테두리 그리기
            for pane in group['panes']:
                x, y, w, h = pane['rect']
                ns_rect = AppKit.NSMakeRect(x, y, w, h)
                
                # Inside Stroke 처리 (Inset)
                inset_rect = AppKit.NSInsetRect(ns_rect, line_width / 2.0, line_width / 2.0)
                path = AppKit.NSBezierPath.bezierPathWithRect_(inset_rect)
                path.setLineWidth_(line_width)
                border_color.set()
                path.stroke()

                # 4. 오버레이 태그 그리기
                tag_str = pane.get('tag', '?')
                ns_str = AppKit.NSString.stringWithString_(tag_str)
                tag_size = ns_str.sizeWithAttributes_(text_attrs)

                pad_x, pad_y = 6.0, 2.0
                tag_rect_w = tag_size.width + pad_x * 2.0
                tag_rect_h = tag_size.height + pad_y * 2.0
                tag_x = x - 4.0
                tag_y = y - 4.0
                tag_rect = AppKit.NSMakeRect(tag_x, tag_y, tag_rect_w, tag_rect_h)

                # 태그 배경과 그림자
                AppKit.NSGraphicsContext.currentContext().saveGraphicsState()
                shadow.set()
                tag_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tag_rect, 4.0, 4.0)
                tag_bg_color.set()
                tag_path.fill()
                AppKit.NSGraphicsContext.currentContext().restoreGraphicsState()

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
            self.is_showing = False
            self.setupWindow()
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
        
        self.view = OverlayView.alloc().initWithFrame_(screen_frame)
        self.window.setContentView_(self.view)

    @objc.python_method
    def toggle_overlay(self):
        # Cocoa UI 업데이트는 반드시 메인 스레드에서 수행되어야 함
        self.performSelectorOnMainThread_withObject_waitUntilDone_("doToggle", None, False)
        
    def doToggle(self):
        if self.is_showing:
            # Fade Out
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.15)
            AppKit.NSAnimationContext.currentContext().setTimingFunction_(
                Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut)
            )
            self.window.animator().setAlphaValue_(0.0)
            AppKit.NSAnimationContext.endGrouping()
            
            # 애니메이션 후 창 닫기
            self.performSelector_withObject_afterDelay_("finishFadeOut", None, 0.15)
        else:
            rects = self.get_target_rects()
            active_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            active_pid = active_app.processIdentifier() if active_app else None
            
            self.view.setRects_activePid_(rects, active_pid)
            
            self.window.setAlphaValue_(0.0)
            self.window.orderFront_(None)
            
            # Fade In
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.15)
            AppKit.NSAnimationContext.currentContext().setTimingFunction_(
                Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut)
            )
            self.window.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.endGrouping()
            
            self.is_showing = True
            print(f"[INFO] 오버레이 표시 (감지된 영역 수: {len(rects)})")

    def finishFadeOut(self):
        self.window.orderOut_(None)
        self.is_showing = False
        print("[INFO] 오버레이 숨김")

    @objc.python_method
    def handle_tag_global(self, tag_char):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("doHandleTag:", tag_char, False)

    def doHandleTag_(self, tag_char):
        if not self.is_showing: return
        for group in self.view.window_groups:
            for pane in group['panes']:
                if pane.get('tag') == tag_char:
                    element = pane['element']
                    pid = group['pid']
                    
                    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    if app:
                        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
                    
                    AXUIElementPerformAction(element, "AXRaise")
                    self.doToggle()
                    return

    @objc.python_method
    def get_target_rects(self):
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        sw = screen_frame.size.width
        sh = screen_frame.size.height

        # 1. 화면에 보이는 창 필터링 (Quartz API)
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        
        quartz_windows = []
        visible_pids = set()
        
        for i, win in enumerate(window_list):
            layer = win.get(Quartz.kCGWindowLayer, 1)
            alpha = win.get(Quartz.kCGWindowAlpha, 1.0)
            pid = win.get(Quartz.kCGWindowOwnerPID)
            bounds = win.get(Quartz.kCGWindowBounds)
            
            # Layer 0 (일반 사용자 창)이고, 투명도가 매우 낮지 않으며 PID가 있는 경우
            if layer == 0 and alpha > 0.05 and pid and bounds:
                w, h = bounds.get('Width', 0), bounds.get('Height', 0)
                if w > 100 and h > 100:
                    x, y = bounds.get('X', 0), bounds.get('Y', 0)
                    quartz_windows.append({
                        'rect': (x, y, w, h),
                        'pid': pid,
                        'z_index': i # 작을수록 앞(Front)
                    })
                    visible_pids.add(pid)
                
        print(f"[DEBUG] 화면에서 감지된 가시 앱 프로세스(PID) 수: {len(visible_pids)}")

        window_groups = []

        # 2. 재귀 탐색을 통한 패널 영역 추출 (DFS)
        def find_panes(element, depth, panes_list, top_rect):
            # 탐색 깊이 제한 (성능 고려)
            if depth > 10: return
            
            # 먼저 크기를 확인하여, 100x100보다 작은 요소(버튼, 텍스트 등)는 하위 탐색(Children)도 건너뜀 (성능 최적화 핵심)
            err_s, size_val = AXUIElementCopyAttributeValue(element, "AXSize", None)
            sz_width, sz_height = 0, 0
            if err_s == 0 and size_val:
                succ_s, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)
                if succ_s:
                    sz_width, sz_height = sz.width, sz.height
                    if sz_width < 100 or sz_height < 100:
                        return # 요소가 너무 작으면 파고들지 않음!
            
            err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
            if err == 0 and role in ["AXWindow", "AXSplitGroup", "AXTabGroup", "AXScrollArea", "AXWebArea", "AXGroup"]:
                err_p, pos_val = AXUIElementCopyAttributeValue(element, "AXPosition", None)
                
                if err_p == 0 and pos_val:
                    succ_p, pos = AXValueGetValue(pos_val, kAXValueCGPointType, None)
                    if succ_p and sz_width > 0:
                        w, h = sz_width, sz_height
                        x, y = pos.x, pos.y
                        
                        # 유효한 창 크기인지 확인 및 화면 내 존재 여부
                        if w > 100 and h > 100 and (x < sw and y < sh and x + w > 0 and y + h > 0):
                            # 중복 검사: 위치와 크기가 15픽셀 이내로 비슷한 경우 병합(무시)
                            is_dup = False
                            for pane in panes_list:
                                rx, ry, rw, rh = pane['rect']
                                if abs(x - rx) < 15 and abs(y - ry) < 15 and abs(w - rw) < 15 and abs(h - rh) < 15:
                                    is_dup = True
                                    break
                            
                            if not is_dup:
                                print(f"[DEBUG] 패널 감지됨 ({role}) -> 위치: x={x}, y={y} / 크기: w={w}, h={h}")
                                panes_list.append({'rect': (x, y, w, h), 'element': element})
            
            err, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
            if err == 0 and children:
                for child in children:
                    find_panes(child, depth + 1, panes_list, top_rect)

        # 3. 수집된 PID를 순회하며 Accessibility 객체로 변환하여 창 탐색
        for pid in visible_pids:
            app_element = AXUIElementCreateApplication(pid)
            err, windows = AXUIElementCopyAttributeValue(app_element, "AXWindows", None)
            if err == 0 and windows:
                for window in windows:
                    # 각 윈도우의 가시성/최소화 여부 등을 간단히 체크 후 깊이 탐색
                    err_m, is_minimized = AXUIElementCopyAttributeValue(window, "AXMinimized", None)
                    if err_m == 0 and is_minimized:
                        continue # 최소화된 창은 스킵
                    
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
                                panes = []
                                top_rect = best_match['rect']
                                find_panes(window, 0, panes, top_rect)
                                
                                if not panes:
                                    panes.append({'rect': (ax_x, ax_y, ax_w, ax_h), 'element': window})
                                    
                                window_groups.append({
                                    'z_index': best_match['z_index'],
                                    'top_rect': top_rect,
                                    'panes': panes,
                                    'pid': pid
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

def on_activate_h():
    if overlay_controller:
        overlay_controller.toggle_overlay()

def setup_hotkeys():
    hotkeys_dict = {
        '<cmd>+<ctrl>+k': on_activate_h
    }
    
    def make_handler(char):
        def handler():
            if overlay_controller and overlay_controller.is_showing:
                overlay_controller.handle_tag_global(char)
        return handler

    for char in "abcdefghijklmnopqrstuvwxyz":
        if char == 'k':
            continue
        hotkeys_dict[f'<cmd>+<ctrl>+{char}'] = make_handler(char.upper())

    # 백그라운드 스레드에서 전역 키보드 입력 모니터링
    with keyboard.GlobalHotKeys(hotkeys_dict) as h:
        h.join()

if __name__ == '__main__':
    print("=" * 60)
    print("  Smart-Homerow Phase 3: Overlay Engine 시작")
    print("  단축키: Cmd + Ctrl + K (창/분할화면 강조 토글)")
    print("  종료하려면 터미널에서 Ctrl+C 를 누르세요.")
    print("=" * 60)
    
    # AppKit 초기화 및 런루프 시작 (GUI 그리기를 위해 필수)
    app = AppKit.NSApplication.sharedApplication()
    overlay_controller = OverlayController.alloc().init()
    
    t = threading.Thread(target=setup_hotkeys, daemon=True)
    t.start()
    
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

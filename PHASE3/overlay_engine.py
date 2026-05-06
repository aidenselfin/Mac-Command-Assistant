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
        return self

    def setRects_(self, window_groups):
        self.window_groups = window_groups
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        # 1. 투명 배경으로 화면 지우기
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(dirtyRect)

        if not hasattr(self, 'window_groups') or not self.window_groups:
            return

        screen_h = AppKit.NSScreen.mainScreen().frame().size.height

        # Z-Order가 큰 것(Back)부터 작은 것(Front) 순으로 정렬하여 렌더링
        sorted_groups = sorted(self.window_groups, key=lambda g: g['z_index'], reverse=True)

        for group in sorted_groups:
            top_x, top_y, top_w, top_h = group['top_rect']
            top_converted_y = screen_h - top_y - top_h
            ns_top_rect = AppKit.NSMakeRect(top_x, top_converted_y, top_w, top_h)
            
            # 2. DestinationOut으로 현재 창 영역만큼 배경 투명도 증가 (뒤에 그려진 선들을 흐리게 만듦)
            AppKit.NSGraphicsContext.currentContext().saveGraphicsState()
            AppKit.NSGraphicsContext.currentContext().setCompositingOperation_(AppKit.NSCompositingOperationDestinationOut)
            # 투명도를 줄이는 비율 (0.6을 주면, 뒤에 있는 테두리가 40% 정도만 남게 됨)
            AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.6).set() 
            AppKit.NSRectFill(ns_top_rect)
            AppKit.NSGraphicsContext.currentContext().restoreGraphicsState()

            # 3. 현재 창의 내부 패널 테두리 그리기
            AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 0.0, 0.8).set()
            path = AppKit.NSBezierPath.bezierPath()
            path.setLineWidth_(4.0)

            for rect in group['panes']:
                x, y, w, h = rect
                converted_y = screen_h - y - h
                ns_rect = AppKit.NSMakeRect(x, converted_y, w, h)
                path.appendBezierPathWithRect_(ns_rect)
            
            path.stroke()

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
            self.window.orderOut_(None)
            self.is_showing = False
            print("[INFO] 오버레이 숨김")
        else:
            rects = self.get_target_rects()
            self.view.setRects_(rects)
            self.window.orderFront_(None) # 포커스 뺏지 않음
            self.is_showing = True
            print(f"[INFO] 오버레이 표시 (감지된 영역 수: {len(rects)})")

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
                            for (rx, ry, rw, rh) in panes_list:
                                if abs(x - rx) < 15 and abs(y - ry) < 15 and abs(w - rw) < 15 and abs(h - rh) < 15:
                                    is_dup = True
                                    break
                            
                            if not is_dup:
                                print(f"[DEBUG] 패널 감지됨 ({role}) -> 위치: x={x}, y={y} / 크기: w={w}, h={h}")
                                panes_list.append((x, y, w, h))
            
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
                                    panes.append((ax_x, ax_y, ax_w, ax_h))
                                    
                                window_groups.append({
                                    'z_index': best_match['z_index'],
                                    'top_rect': top_rect,
                                    'panes': panes
                                })
                    
        return window_groups

def on_activate_h():
    if overlay_controller:
        overlay_controller.toggle_overlay()

def setup_hotkeys():
    # 백그라운드 스레드에서 전역 키보드 입력 모니터링
    with keyboard.GlobalHotKeys({
        '<cmd>+<ctrl>+k': on_activate_h
    }) as h:
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

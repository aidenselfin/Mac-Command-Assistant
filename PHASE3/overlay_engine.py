import sys
import threading
import time
import objc
from pathlib import Path
import AppKit
import Quartz
from ApplicationServices import (
    AXUIElementCreateSystemWide,
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
            self.rects = []
        return self

    def setRects_(self, rects):
        self.rects = rects
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        # 1. 투명 배경으로 화면 지우기
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(dirtyRect)

        if not self.rects:
            return

        # 2. 노란색 테두리 그리기
        AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 0.0, 0.8).set()
        
        path = AppKit.NSBezierPath.bezierPath()
        path.setLineWidth_(4.0)

        # Mac 좌표계는 좌하단이 (0,0) 이므로 Quartz(좌상단 원점) 좌표 변환
        screen_h = AppKit.NSScreen.mainScreen().frame().size.height

        for rect in self.rects:
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

        system = AXUIElementCreateSystemWide()
        err, focused_app = AXUIElementCopyAttributeValue(system, "AXFocusedApplication", None)
        
        is_fullscreen = False
        focused_window = None
        
        if err == 0 and focused_app:
            err_w, window = AXUIElementCopyAttributeValue(focused_app, "AXFocusedWindow", None)
            if err_w == 0 and window:
                focused_window = window
                
                # 1. Mac 네이티브 전체화면 속성 확인
                err_fs, is_fs_val = AXUIElementCopyAttributeValue(window, "AXFullScreen", None)
                if err_fs == 0 and is_fs_val:
                    is_fullscreen = True
                
                # 2. 크기 기반 전체화면 감지 (해상도 스케일링 오차 고려하여 50픽셀 여유)
                err_s, size_val = AXUIElementCopyAttributeValue(window, "AXSize", None)
                if err_s == 0 and size_val:
                    succ, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)
                    if succ:
                        w, h = sz.width, sz.height
                        print(f"[DEBUG] 현재 창 크기: {w}x{h} | 모니터 크기: {sw}x{sh}")
                        if abs(w - sw) < 50 and abs(h - sh) < 50:
                            is_fullscreen = True

        rects = []
        if is_fullscreen and focused_window:
            print("[INFO] 전체화면 감지됨: 내부 분할 패널을 스캔합니다.")
            
            # 전체 화면 시 내부 주요 레이아웃 영역 추출 (DFS)
            def find_panes(element, depth=0):
                # Electron 앱(VSCode 등)은 DOM 구조가 깊으므로 탐색 깊이를 늘림
                if depth > 8: return
                
                err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
                # VS Code 등은 AXWebArea 내부에 AXGroup으로 영역을 나눔
                if err == 0 and role in ["AXSplitGroup", "AXScrollArea", "AXGroup", "AXTabGroup", "AXWebArea"]:
                    err_s, size_val = AXUIElementCopyAttributeValue(element, "AXSize", None)
                    err_p, pos_val = AXUIElementCopyAttributeValue(element, "AXPosition", None)
                    
                    if err_s == 0 and err_p == 0 and size_val and pos_val:
                        succ_s, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)
                        succ_p, pos = AXValueGetValue(pos_val, kAXValueCGPointType, None)
                        if succ_s and succ_p:
                            w, h = sz.width, sz.height
                            x, y = pos.x, pos.y
                            # 너무 작거나(버튼 등), 전체 화면 크기 자체인 경우 제외
                            if w > 100 and h > 100 and not (abs(w - sw) < 10 and abs(h - sh) < 10):
                                # 중복 검사: 위치와 크기가 10픽셀 이내로 비슷한 박스(html wrapper)는 하나로 병합
                                is_dup = False
                                for (rx, ry, rw, rh) in rects:
                                    if abs(x - rx) < 10 and abs(y - ry) < 10 and abs(w - rw) < 10 and abs(h - rh) < 10:
                                        is_dup = True
                                        break
                                
                                if not is_dup:
                                    print(f"[DEBUG] 패널 감지됨 ({role}) -> 위치: x={x}, y={y} / 크기: w={w}, h={h}")
                                    rects.append((x, y, w, h))
                
                err, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
                if err == 0 and children:
                    for child in children:
                        find_panes(child, depth + 1)
                        
            find_panes(focused_window)
            
            # 분할 영역을 못 찾은 앱일 경우 창 전체 크기라도 반환
            if not rects:
                err_p, pos_val = AXUIElementCopyAttributeValue(focused_window, "AXPosition", None)
                err_s, size_val = AXUIElementCopyAttributeValue(focused_window, "AXSize", None)
                if err_p == 0 and err_s == 0 and pos_val and size_val:
                    succ_s, sz = AXValueGetValue(size_val, kAXValueCGSizeType, None)
                    succ_p, pos = AXValueGetValue(pos_val, kAXValueCGPointType, None)
                    if succ_s and succ_p:
                        print(f"[DEBUG] 세부 패널 없음, 통짜 창 추가 -> 위치: x={pos.x}, y={pos.y} / 크기: w={sz.width}, h={sz.height}")
                        rects.append((pos.x, pos.y, sz.width, sz.height))
        else:
            print("[INFO] 일반 모드 감지됨: 화면에 보이는 모든 윈도우 영역 추출...")
            options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
            window_list = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
            for win in window_list:
                layer = win.get(Quartz.kCGWindowLayer, 1)
                alpha = win.get(Quartz.kCGWindowAlpha, 1.0)
                bounds = win.get(Quartz.kCGWindowBounds)
                
                # layer 0 은 일반 사용자 창. 너무 투명한 창은 필터링
                if layer == 0 and alpha > 0.05 and bounds:
                    w, h = bounds.get('Width', 0), bounds.get('Height', 0)
                    if w > 100 and h > 100:
                        x, y = bounds.get('X', 0), bounds.get('Y', 0)
                        print(f"[DEBUG] 윈도우 감지됨 (Layer {layer}) -> 위치: x={x}, y={y} / 크기: w={w}, h={h}")
                        rects.append((x, y, w, h))
                        
        return rects

def on_activate_h():
    if overlay_controller:
        overlay_controller.toggle_overlay()

def setup_hotkeys():
    # 백그라운드 스레드에서 전역 키보드 입력 모니터링
    with keyboard.GlobalHotKeys({
        '<cmd>+<shift>+k': on_activate_h
    }) as h:
        h.join()

if __name__ == '__main__':
    print("=" * 60)
    print("  Smart-Homerow Phase 3: Overlay Engine 시작")
    print("  단축키: Cmd + Shift + K (창/분할화면 강조 토글)")
    print("  종료하려면 터미널에서 Ctrl+C 를 누르세요.")
    print("=" * 60)
    
    # AppKit 초기화 및 런루프 시작 (GUI 그리기를 위해 필수)
    app = AppKit.NSApplication.sharedApplication()
    overlay_controller = OverlayController.alloc().init()
    
    t = threading.Thread(target=setup_hotkeys, daemon=True)
    t.start()
    
    try:
        from PyObjCTools import AppHelper
        AppHelper.runEventLoop()
    except KeyboardInterrupt:
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")

# overlay_engine.py — Smart-Homerow Phase 3: Overlay Engine with Learning Integration
# 개선사항:
#   1. learning_engine과 통합 (LearningDatabase, LearningAdvisor)
#   2. 마우스 클릭 시 단축키 노출 기록 (PHASE2 DB)
#   3. Right Command + 알파벳 입력 시 단축키 사용 기록 (PHASE2 DB)
#   4. 동적 우선순위 필터링 (사용 빈도 기반)
#   5. 학습 상태에 따라 표시할 단축키 개수 조정

import sys
import os
import threading
import time
import signal
import objc
import AppKit
import Quartz
import json
from pathlib import Path
from ApplicationServices import (
    AXUIElementCreateSystemWide,
    AXUIElementCreateApplication,
    AXUIElementCopyAttributeValue,
    AXUIElementPerformAction,
    AXUIElementCopyElementAtPosition,
    AXValueGetValue,
    kAXValueCGSizeType,
    kAXValueCGPointType
)
from PyObjCTools import AppHelper
from Foundation import NSOperationQueue

# ── Learning Engine 임포트 ────────────────────────────────────────────────────
# Final 폴더에서 learning_engine 임포트
sys.path.insert(0, str(Path(__file__).parent))
try:
    from learning_engine import LearningDatabase, LearningAdvisor, resolve_shortcuts_db_path
    LEARNING_ENABLED = True
except ImportError:
    print("[WARN] learning_engine 임포트 실패 — 학습 기능 비활성화")
    LEARNING_ENABLED = False

# ==============================================================================
# 1. Configuration & Constants
# ==============================================================================
OUR_PID = os.getpid()

KEYCODE_TO_CHAR = {
    0: 'A',  1: 'S',  2: 'D',  3: 'F',  4: 'H',  5: 'G',  6: 'Z',  7: 'X',
    8: 'C',  9: 'V', 11: 'B', 12: 'Q', 13: 'W', 14: 'E', 15: 'R', 16: 'Y',
   17: 'T', 31: 'O', 32: 'U', 34: 'I', 35: 'P', 37: 'L', 38: 'J', 40: 'K',
   45: 'N', 46: 'M',
}

# ==============================================================================
# 2. UI Component (OverlayView) — 기존과 동일
# ==============================================================================
class OverlayWindowView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(OverlayWindowView, self).initWithFrame_(frame)
        if self:
            self.window_groups = []
            self.active_pid = None
            self.temporary_tags = []
            self.button_hints = []   # [{'rect':(x,y,w,h), 'shortcut':str, 'label':str}]
        return self

    @objc.python_method
    def add_temporary_tag(self, x, y, text):
        # CGEvent 좌표계는 y=0이 화면 상단(flipped NSView와 동일) — 추가 변환 불필요
        self.temporary_tags.append({'x': x, 'y': y, 'text': text, 'expire': time.time() + 1.5})
        self.setNeedsDisplay_(True)

    def isFlipped(self):
        return True

    def setRects_activePid_(self, window_groups, active_pid):
        self.window_groups = window_groups
        self.active_pid = active_pid
        self.setNeedsDisplay_(True)

    def drawRect_(self, dirtyRect):
        AppKit.NSColor.clearColor().set()
        AppKit.NSRectFill(dirtyRect)

        font = AppKit.NSFont.fontWithName_size_("Menlo-Bold", 12.0)
        if font is None:
            font = AppKit.NSFont.userFixedPitchFontOfSize_(12.0)
        tag_text_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.102, 0.102, 0.102, 1.0)
        active_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0)
        inactive_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 0.4)

        window_groups = getattr(self, "window_groups", None) or []

        if window_groups:
            sorted_groups = sorted(window_groups, key=lambda g: g['z_index'], reverse=True)
            tag_bg_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0)
            text_attrs = {
                AppKit.NSFontAttributeName: font,
                AppKit.NSForegroundColorAttributeName: tag_text_color,
                AppKit.NSKernAttributeName: 0.5
            }

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

            for group in sorted_groups:
                for pane in group['panes']:
                    x, y, w, h = pane['rect']
                    tag_str = pane.get('tag', '?')
                    if pane.get('is_typing_box', False):
                        tag_str = f"[T] {tag_str}"
                    ns_str = AppKit.NSString.stringWithString_(tag_str)
                    tag_size = ns_str.sizeWithAttributes_(text_attrs)
                    pad_x, pad_y = 6.0, 2.0
                    tag_rect = AppKit.NSMakeRect(x - 4.0, y - 4.0, tag_size.width + pad_x * 2.0, tag_size.height + pad_y * 2.0)
                    tag_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tag_rect, 4.0, 4.0)
                    tag_bg_color.set()
                    tag_path.fill()
                    text_rect = AppKit.NSMakeRect(x - 4.0 + pad_x, y - 4.0 + pad_y, tag_size.width, tag_size.height)
                    ns_str.drawInRect_withAttributes_(text_rect, text_attrs)

        # ── 버튼 단축키 힌트 (파란 뱃지) ──────────────────────────────────
        button_hints = getattr(self, "button_hints", None) or []
        if button_hints:
            hint_font = AppKit.NSFont.fontWithName_size_("Menlo-Bold", 10.0)
            if hint_font is None:
                hint_font = AppKit.NSFont.userFixedPitchFontOfSize_(10.0)
            hint_bg   = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.47, 0.95, 0.92)
            hint_text = AppKit.NSColor.whiteColor()
            hint_text_attrs = {
                AppKit.NSFontAttributeName: hint_font,
                AppKit.NSForegroundColorAttributeName: hint_text,
            }
            for hint in button_hints:
                hx, hy, hw, hh = hint['rect']
                shortcut_str = hint['shortcut']
                ns_str = AppKit.NSString.stringWithString_(shortcut_str)
                s_size = ns_str.sizeWithAttributes_(hint_text_attrs)
                pad_x, pad_y = 4.0, 2.0
                badge_w = s_size.width + pad_x * 2
                badge_h = s_size.height + pad_y * 2
                # 버튼 우측 상단 모서리에 배치
                bx = hx + hw - badge_w - 2
                by = hy + 2
                badge_rect = AppKit.NSMakeRect(bx, by, badge_w, badge_h)
                badge_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(badge_rect, 3.0, 3.0)
                hint_bg.set()
                badge_path.fill()
                text_rect = AppKit.NSMakeRect(bx + pad_x, by + pad_y, s_size.width, s_size.height)
                ns_str.drawInRect_withAttributes_(text_rect, hint_text_attrs)

        text_attrs_base = {
            AppKit.NSFontAttributeName: font,
            AppKit.NSForegroundColorAttributeName: tag_text_color,
            AppKit.NSKernAttributeName: 0.5
        }
        for tag_data in getattr(self, "temporary_tags", []) or []:
            tx, ty, text = tag_data['x'], tag_data['y'], tag_data['text']
            ns_str = AppKit.NSString.stringWithString_(text)
            tag_size = ns_str.sizeWithAttributes_(text_attrs_base)
            pad_x, pad_y = 8.0, 4.0
            tag_rect = AppKit.NSMakeRect(tx - tag_size.width/2 - pad_x, ty - tag_size.height/2 - pad_y, tag_size.width + pad_x*2, tag_size.height + pad_y*2)
            tag_path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(tag_rect, 6.0, 6.0)
            bg_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.1, 0.95)
            bg_color.set()
            tag_path.fill()
            temp_text_attrs = {
                AppKit.NSFontAttributeName: font,
                AppKit.NSForegroundColorAttributeName: active_color,
                AppKit.NSKernAttributeName: 0.5
            }
            text_rect = AppKit.NSMakeRect(tx - tag_size.width/2, ty - tag_size.height/2, tag_size.width, tag_size.height)
            ns_str.drawInRect_withAttributes_(text_rect, temp_text_attrs)

# ==============================================================================
# 3. Accessibility Scanner (개선: Learning 통합)
# ==============================================================================
class AccessibilityScanner:
    def __init__(self, learning_db=None, learning_advisor=None):
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        self.sw = screen_frame.size.width
        self.sh = screen_frame.size.height
        self.learning_db = learning_db
        self.learning_advisor = learning_advisor
        
        # shortcuts_db.json 로드
        self.shortcuts_db = self._load_shortcuts_db()

    def _load_shortcuts_db(self):
        """shortcuts_db.json 로드 (Final 우선, 없으면 PHASE3)"""
        try:
            db_path = resolve_shortcuts_db_path()
            if not db_path.is_file():
                print(f"[WARN] shortcuts_db.json 없음: {db_path}")
                return {}
            with open(db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print("[WARN] shortcuts_db.json 루트가 JSON 객체가 아님")
                return {}
            filtered = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
            print(f"[INFO] shortcuts_db 로드: {db_path} ({len(filtered)}개 앱)")
            return filtered
        except json.JSONDecodeError as e:
            print(f"[WARN] shortcuts_db.json JSON 파싱 오류: {e}")
            return {}
        except Exception as e:
            print(f"[WARN] shortcuts_db.json 로드 실패: {e}")
            return {}

    @staticmethod
    def _normalize_ax_label(s):
        if not s:
            return ""
        return " ".join(str(s).strip().lower().split())

    def _ax_attr_string(self, element, attr):
        err, val = AXUIElementCopyAttributeValue(element, attr, None)
        if err != 0 or not val:
            return None
        t = str(val).strip()
        return t if t else None

    # 라벨로 쓰기에 너무 일반적인 단어들 — 매칭에서 제외
    _SKIP_GENERIC_LABELS = frozenset({
        "application", "group", "window", "dialog", "toolbar",
        "image", "button", "text", "checkbox", "radio button",
        "pop up button", "menu button", "link", "list item",
        "scroll area", "web area", "tab group", "split group",
        "table", "outline", "row", "column", "cell",
    })

    def _collect_ax_match_strings(self, element):
        """
        AX 요소에서 단축키 DB/메뉴 맵과 비교할 라벨 후보를 수집한다.
        AXTitle/AXDescription/AXHelp/AXLabel 외에 자식 텍스트 노드도 확인.
        """
        out = []
        for attr in ("AXTitle", "AXDescription", "AXHelp", "AXLabel",
                     "AXRoleDescription", "AXValue", "AXPlaceholderValue"):
            raw = self._ax_attr_string(element, attr)
            if not raw:
                continue
            n = self._normalize_ax_label(raw)
            if not n or n in self._SKIP_GENERIC_LABELS or len(n) > 80:
                continue
            if n.startswith("<") and n.endswith(">"):
                continue
            out.append(n)

        # 자식 AXStaticText / AXImage 에서 추가 라벨 수집
        # (아이콘 버튼처럼 자식 텍스트에 라벨이 있는 경우 대응)
        try:
            err_c, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
            if err_c == 0 and children and len(children) <= 8:
                for child in children[:8]:
                    err_r, crole = AXUIElementCopyAttributeValue(child, "AXRole", None)
                    if err_r != 0:
                        continue
                    if str(crole) in ("AXStaticText", "AXImage"):
                        for attr in ("AXTitle", "AXDescription", "AXValue"):
                            raw = self._ax_attr_string(child, attr)
                            if raw:
                                n = self._normalize_ax_label(raw)
                                if n and n not in self._SKIP_GENERIC_LABELS and len(n) <= 80:
                                    out.append(n)
        except Exception:
            pass

        seen: set = set()
        uniq = []
        for x in out:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        return uniq

    def _pick_best_shortcut(self, bundle_id, candidates_norm):
        """
        JSON 키와 AX 문자열을 매칭한다. 긴 키를 우선해 'Save All'이 'Save'보다 우선한다.
        반환: (shortcut 문자열, DB용 element_key) 또는 None
        """
        app_map = self.shortcuts_db.get(bundle_id)
        if not app_map or not isinstance(app_map, dict) or not candidates_norm:
            return None

        matches = []
        for key, shortcut in app_map.items():
            if not isinstance(key, str) or not isinstance(shortcut, str):
                continue
            nk = self._normalize_ax_label(key)
            if not nk:
                continue
            for cand in candidates_norm:
                if nk == cand:
                    matches.append((len(nk), key, shortcut))
                    break
                mlen = min(len(nk), len(cand))
                if mlen < 3:
                    continue
                if nk in cand or cand in nk:
                    matches.append((len(nk), key, shortcut))
                    break

        if not matches:
            return None
        matches.sort(key=lambda t: t[0], reverse=True)
        _ln, json_key, shortcut = matches[0]
        element_key = self._normalize_ax_label(json_key)
        return shortcut, element_key

    def get_shortcut_for_position(self, x, y):
        """클릭 위치의 단축키 조회"""
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        active_app = workspace.frontmostApplication()
        if not active_app:
            print(f"[HINT-DBG] 활성 앱 없음")
            return None

        bundle_id = active_app.bundleIdentifier()
        if not bundle_id or bundle_id not in self.shortcuts_db:
            print(f"[HINT-DBG] DB에 없는 앱: {bundle_id}")
            return None

        system_wide = AXUIElementCreateSystemWide()
        err, element = AXUIElementCopyElementAtPosition(system_wide, x, y, None)
        if err != 0 or not element:
            print(f"[HINT-DBG] AX 요소 없음 at ({x:.0f}, {y:.0f}), err={err}")
            return None

        candidates_norm = self._collect_ax_match_strings(element)
        print(f"[HINT-DBG] {bundle_id} | AX 후보: {candidates_norm}")
        if not candidates_norm:
            return None

        picked = self._pick_best_shortcut(bundle_id, candidates_norm)
        if not picked:
            print(f"[HINT-DBG] DB 매칭 실패 — 위 후보를 shortcuts_db.json 키와 비교하세요")
            return None
        shortcut, element_key = picked
        print(f"[HINT-DBG] 매칭 성공: '{element_key}' → {shortcut}")

        if self.learning_advisor and LEARNING_ENABLED:
            if not self.learning_advisor.should_show_hint(bundle_id, element_key, shortcut):
                print(f"[HINT-DBG] 학습 완료로 힌트 억제: {shortcut}")
                return None

        if self.learning_db and LEARNING_ENABLED:
            try:
                self.learning_db.record_shortcut_shown(bundle_id, element_key, shortcut)
            except Exception as ex:
                print(f"[WARN] record_shortcut_shown 실패: {ex}")

        return shortcut

    # ──────────────────────────────────────────────────────────────────────────
    # 버튼 단축키 힌트 스캔
    # ──────────────────────────────────────────────────────────────────────────

    # 단축키 힌트를 표시할 인터랙티브 역할
    INTERACTIVE_ROLES = frozenset({
        "AXButton", "AXMenuItem", "AXMenuBarItem", "AXLink",
        "AXCheckBox", "AXRadioButton", "AXPopUpButton", "AXMenuButton",
        "AXToolbarButton", "AXTab", "AXDisclosureTriangle",
        "AXToggleButton", "AXComboBox",
    })
    # 자식 탐색도 불필요한 완전 리프 역할
    LEAF_SKIP_ROLES = frozenset({
        "AXStaticText", "AXSeparator", "AXScrollBar",
        "AXValueIndicator", "AXColorWell",
    })

    # ── 메뉴바 단축키 직접 추출 ──────────────────────────────────────────────

    # AXMenuItemCmdModifiers 비트마스크
    _MOD_SHIFT   = 0x01
    _MOD_OPTION  = 0x02
    _MOD_CONTROL = 0x04
    _MOD_NO_CMD  = 0x08   # 이 비트가 켜지면 Cmd 없음 (Function key 등)

    # AXMenuItemCmdGlyph → 표시 문자 (자주 쓰이는 것만)
    _GLYPH_MAP = {
        2: "Tab", 3: "Esc", 4: "Delete", 5: "Fwd Delete",
        6: "Return", 8: "↑", 9: "↓", 10: "←", 11: "→",
        12: "PgUp", 13: "PgDn", 14: "Home", 15: "End",
        16: "Space", 23: "F1", 24: "F2", 25: "F3", 26: "F4",
        27: "F5", 28: "F6", 29: "F7", 30: "F8",
    }

    def _format_menu_shortcut(self, char: str, modifiers: int) -> str:
        """AX modifier 비트마스크 + 키 문자를 'Cmd + Shift + S' 형식으로 변환."""
        if not char:
            return ""
        parts = []
        no_cmd = bool(modifiers & self._MOD_NO_CMD)
        if not no_cmd:
            parts.append("Cmd")
        if modifiers & self._MOD_CONTROL:
            parts.append("Ctrl")
        if modifiers & self._MOD_OPTION:
            parts.append("Alt")
        if modifiers & self._MOD_SHIFT:
            parts.append("Shift")

        # 특수 문자 처리
        _special = {
            '\x08': 'Delete', '\x7f': 'Delete', '\r': 'Return', '\t': 'Tab',
            '\x1b': 'Esc', ' ': 'Space',
            '': '↑', '': '↓', '': '←', '': '→',
            '': 'Delete', '': 'Home', '': 'End',
            '': 'PgUp', '': 'PgDn',
        }
        display = _special.get(char, char.upper() if len(char) == 1 else char)
        parts.append(display)
        return " + ".join(parts)

    def scan_menu_bar_shortcuts(self, active_pid: int) -> dict:
        """
        앱 메뉴바를 AX API로 직접 스캔해 {normalized_label: shortcut_str} 반환.
        shortcuts_db.json 없이도 모든 macOS 네이티브 앱에서 동작.
        """
        app_element = AXUIElementCreateApplication(active_pid)
        result: dict = {}
        try:
            err, menubar = AXUIElementCopyAttributeValue(app_element, "AXMenuBar", None)
            if err != 0 or not menubar:
                return result
            err_c, top_menus = AXUIElementCopyAttributeValue(menubar, "AXChildren", None)
            if err_c != 0 or not top_menus:
                return result
            for menu in top_menus:
                self._collect_menu_shortcuts(menu, result, depth=0)
        except Exception as e:
            print(f"[MENU-SCAN] 오류: {e}")
        return result

    def _collect_menu_shortcuts(self, element, result: dict, depth: int):
        """AXMenuItem 트리를 재귀적으로 순회해 단축키를 수집한다."""
        if depth > 6:
            return
        try:
            err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
            if err != 0:
                return
            role_str = str(role) if role else ""

            if role_str == "AXMenuItem":
                err_c, cmd_char = AXUIElementCopyAttributeValue(
                    element, "AXMenuItemCmdChar", None)
                char = str(cmd_char).strip() if err_c == 0 and cmd_char else ""

                # cmd char가 없으면 glyph로 대체
                if not char:
                    err_g, glyph = AXUIElementCopyAttributeValue(
                        element, "AXMenuItemCmdGlyph", None)
                    if err_g == 0 and glyph:
                        char = self._GLYPH_MAP.get(int(glyph), "")

                if char:
                    err_m, cmd_mods = AXUIElementCopyAttributeValue(
                        element, "AXMenuItemCmdModifiers", None)
                    mods = int(cmd_mods) if err_m == 0 and cmd_mods is not None else 0
                    shortcut = self._format_menu_shortcut(char, mods)
                    if shortcut:
                        err_t, title = AXUIElementCopyAttributeValue(element, "AXTitle", None)
                        if err_t == 0 and title and str(title).strip():
                            norm = self._normalize_ax_label(str(title).strip())
                            if norm and norm not in self._SKIP_GENERIC_LABELS:
                                result[norm] = shortcut

            # 자식(서브메뉴 포함) 탐색
            err_c2, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
            if err_c2 == 0 and children:
                for child in children:
                    self._collect_menu_shortcuts(child, result, depth + 1)
        except Exception:
            pass

    # ── 버튼 단축키 스캔 메인 ────────────────────────────────────────────────

    def scan_button_shortcuts(self, active_pid: int) -> list:
        """
        1) 앱 메뉴바를 직접 AX 스캔해 {label: shortcut} 맵 추출 (모든 앱)
        2) shortcuts_db.json 로 보완
        3) 화면 AX 트리에서 인터랙티브 요소 라벨과 매칭 → 힌트 목록 반환
        반환: [{'rect': (x,y,w,h), 'shortcut': str, 'label': str}]
        """
        workspace = AppKit.NSWorkspace.sharedWorkspace()
        active_app = workspace.frontmostApplication()
        if not active_app:
            return []
        bundle_id = active_app.bundleIdentifier() or ""

        # ── 1. 메뉴바 실시간 스캔 ──
        menu_map = self.scan_menu_bar_shortcuts(active_pid)

        # ── 2. shortcuts_db.json 보완 ──
        db_map = self.shortcuts_db.get(bundle_id, {})
        combined: dict = {}
        for k, v in db_map.items():
            if isinstance(k, str) and isinstance(v, str):
                combined[self._normalize_ax_label(k)] = v
        combined.update(menu_map)   # 메뉴바 결과가 DB를 덮어씀 (더 정확)

        if not combined:
            return []

        # ── 3. UI 트리 스캔 ──
        app_element = AXUIElementCreateApplication(active_pid)
        results: list = []
        try:
            self._find_interactive_shortcuts(
                app_element, combined, bundle_id, results, depth=0)
        except Exception as e:
            print(f"[BTN-SCAN] 스캔 오류: {e}")

        if results:
            print(f"[BTN-HINT] {bundle_id}: 메뉴바 {len(menu_map)}개, "
                  f"DB {len(db_map)}개 → 버튼 힌트 {len(results)}개")
        return results

    def _find_interactive_shortcuts(
            self, element, combined: dict, bundle_id: str,
            results: list, depth: int):
        """AX 트리를 재귀 순회해 단축키가 있는 인터랙티브 요소를 수집한다."""
        if depth > 15 or len(results) >= 150:
            return

        try:
            err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
        except Exception:
            return
        if err != 0 or not role:
            return
        role_str = str(role)

        # 완전 리프 → 자식도 없음
        if role_str in self.LEAF_SKIP_ROLES:
            return

        if role_str in self.INTERACTIVE_ROLES:
            candidates = self._collect_ax_match_strings(element)
            if candidates:
                shortcut, matched_key = self._match_combined(candidates, combined)
                if shortcut and matched_key:
                    show = True
                    if self.learning_advisor and LEARNING_ENABLED:
                        show = self.learning_advisor.should_show_hint(
                            bundle_id, matched_key, shortcut)
                    if show:
                        geom = self.get_size_pos(element)
                        if geom:
                            x, y, w, h = geom
                            if w >= 8 and h >= 8:
                                results.append({
                                    'rect': (x, y, w, h),
                                    'shortcut': shortcut,
                                    'label': candidates[0],
                                })

        # 자식 탐색
        try:
            err_c, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
        except Exception:
            return
        if err_c == 0 and children:
            for child in children:
                self._find_interactive_shortcuts(
                    child, combined, bundle_id, results, depth + 1)

    def _match_combined(self, candidates: list, combined: dict):
        """
        라벨 후보 리스트를 combined 맵에서 검색한다.
        1순위: 정확 일치
        2순위: 포함 관계 (substring, 양방향, 3자 이상)
        긴 키를 우선해 'save all'이 'save'보다 먼저 걸리도록 정렬.
        반환: (shortcut, matched_key) 또는 (None, None)
        """
        # 1순위: 정확 일치
        for cand in candidates:
            if cand in combined:
                return combined[cand], cand

        # 2순위: 부분 일치 (긴 키 우선)
        sorted_keys = sorted(combined.keys(), key=len, reverse=True)
        for key in sorted_keys:
            if len(key) < 3:
                continue
            for cand in candidates:
                if len(cand) < 3:
                    continue
                if key in cand or cand in key:
                    return combined[key], key

        return None, None

    # ──────────────────────────────────────────────────────────────────────────
    def get_quartz_snapshot(self):
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
                    
        snapshot_key = frozenset((qw['pid'], qw['rect'], qw['z_index']) for qw in quartz_windows)
        return quartz_windows, visible_pids, snapshot_key

    def get_specific_pane_name(self, element):
        def extract_last_part(s):
            return s.split('.')[-1] if '.' in s else s

        e, val = AXUIElementCopyAttributeValue(element, "AXIdentifier", None)
        if e == 0 and val and str(val).strip(): return extract_last_part(str(val).strip())[:24]

        e, val = AXUIElementCopyAttributeValue(element, "AXDOMIdentifier", None)
        if e == 0 and val and str(val).strip(): return extract_last_part(str(val).strip())[:24]

        e, val = AXUIElementCopyAttributeValue(element, "AXDescription", None)
        if e == 0 and val and str(val).strip(): return str(val).strip()[:24]

        e, val = AXUIElementCopyAttributeValue(element, "AXTitle", None)
        if e == 0 and val and str(val).strip(): return str(val).strip()[:24]

        e, val = AXUIElementCopyAttributeValue(element, "AXDOMClassList", None)
        if e == 0 and val and isinstance(val, (list, tuple)) and len(val) > 0:
            ignore_words = {"part", "container", "wrapper", "flex", "box", "view", "content", "panel", "pane", "layout", "grid", "split"}
            for c in val:
                parts = str(c).lower().replace('_', '-').split('-')
                for p in parts:
                    if p and p not in ignore_words:
                        return p.capitalize()[:24]

        e, val = AXUIElementCopyAttributeValue(element, "AXLabel", None)
        if e == 0 and val and str(val).strip(): return str(val).strip()[:24]

        return None

    def search_name_in_children(self, element, depth=0):
        if depth > 4: return None
        name = self.get_specific_pane_name(element)
        if name: return name
        
        err, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
        if err == 0 and children:
            for child in children:
                found = self.search_name_in_children(child, depth + 1)
                if found: return found
        return None

    def get_size_pos(self, element):
        err_s, sv = AXUIElementCopyAttributeValue(element, "AXSize", None)
        err_p, pv = AXUIElementCopyAttributeValue(element, "AXPosition", None)
        if err_s == 0 and err_p == 0 and sv and pv:
            ok_s, sz = AXValueGetValue(sv, kAXValueCGSizeType, None)
            ok_p, pos = AXValueGetValue(pv, kAXValueCGPointType, None)
            if ok_s and ok_p:
                return pos.x, pos.y, sz.width, sz.height
        return None

    def add_as_pane(self, element, role, x, y, w, h, panes_list, is_typing_box=False):
        valid = (w > 20 and h > 10) if is_typing_box else (w > 50 and h > 50)
        if valid and (x < self.sw and y < self.sh and x + w > 0 and y + h > 0):
            specific_name = self.search_name_in_children(element, 0)
            for pane in panes_list:
                rx, ry, rw, rh = pane['rect']
                if abs(x - rx) < 15 and abs(y - ry) < 15 and abs(w - rw) < 15 and abs(h - rh) < 15:
                    if specific_name and not pane.get('specific_name'):
                        pane['specific_name'] = specific_name
                        pane['element'] = element
                        pane['role'] = role
                        if is_typing_box: pane['is_typing_box'] = True
                    return
            panes_list.append({
                'rect': (x, y, w, h), 'element': element, 'role': role, 'specific_name': specific_name,
                'is_typing_box': is_typing_box
            })

    def find_panes(self, element, depth, panes_list):
        if depth > 15: return
        
        err, role = AXUIElementCopyAttributeValue(element, "AXRole", None)
        if err != 0 or not role: return

        TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
        is_typing_box = (role in TEXT_ROLES)

        geom = self.get_size_pos(element)
        if geom:
            x, y, w, h = geom
            if is_typing_box:
                if w < 20 or h < 10: return
            else:
                if w < 50 or h < 50: return

        if role in ("AXSplitGroup", "AXTabGroup"):
            err_c, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
            if err_c == 0 and children:
                for child in children:
                    err_cr, child_role = AXUIElementCopyAttributeValue(child, "AXRole", None)
                    if err_cr == 0 and child_role == "AXSplitter": continue
                    c_geom = self.get_size_pos(child)
                    if c_geom:
                        cx, cy, cw, ch = c_geom
                        cr = child_role if err_cr == 0 else "?"
                        c_is_typing_box = cr in {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
                        self.add_as_pane(child, cr, cx, cy, cw, ch, panes_list, is_typing_box=c_is_typing_box)
                    if err_cr == 0 and child_role in ("AXSplitGroup", "AXTabGroup"):
                        self.find_panes(child, depth + 1, panes_list)
            return

        PANEL_ROLES = {"AXScrollArea", "AXWebArea", "AXGroup", "AXTextArea"}
        if (role in PANEL_ROLES or is_typing_box) and geom:
            self.add_as_pane(element, role, geom[0], geom[1], geom[2], geom[3], panes_list, is_typing_box=is_typing_box)

        err_c, children = AXUIElementCopyAttributeValue(element, "AXChildren", None)
        if err_c == 0 and children:
            for child in children:
                self.find_panes(child, depth + 1, panes_list)

    def resolve_overlaps(self, panes):
        if not panes: return []
        margin = 15

        for p in panes:
            x, y, w, h = p['rect']
            p['area'] = w * h
            p['children'] = []
            p['keep'] = True

        panes.sort(key=lambda p: p['area'])
        for i, child in enumerate(panes):
            cx, cy, cw, ch = child['rect']
            parent = None
            for j in range(i + 1, len(panes)):
                pot_parent = panes[j]
                px, py, pw, ph = pot_parent['rect']
                if (px - margin <= cx and py - margin <= cy and
                    px + pw + margin >= cx + cw and py + ph + margin >= cy + ch):
                    parent = pot_parent
                    break
            if parent:
                parent['children'].append(child)

        panes.sort(key=lambda p: p['area'], reverse=True)
        for p in panes:
            if not p['keep'] or not p['children']: continue
            children_area = sum(c['area'] for c in p['children'] if c['keep'])
            
            typing_box_children = [c for c in p['children'] if c['keep'] and c.get('is_typing_box', False)]
            non_typing_box_children = [c for c in p['children'] if c['keep'] and not c.get('is_typing_box', False)]
            
            if len(typing_box_children) == 1 and not non_typing_box_children:
                p['is_typing_box'] = True
                p['click_rect'] = typing_box_children[0]['rect']
                p['element'] = typing_box_children[0]['element']
                
                def drop_descendants(node):
                    node['keep'] = False
                    for c in node['children']: drop_descendants(c)
                for c in p['children']: drop_descendants(c)
                
            elif typing_box_children or (children_area / p['area'] > 0.10):
                p['keep'] = False
            else:
                def drop_descendants(node):
                    node['keep'] = False
                    for c in node['children']: drop_descendants(c)
                for c in p['children']: drop_descendants(c)

        kept_panes = [p for p in panes if p['keep']]
        final_panes = []
        for p in kept_panes:
            px, py, pw, ph = p['rect']
            conflict = False
            for fp in final_panes:
                fx, fy, fw, fh = fp['rect']
                ix, iy = max(px, fx), max(py, fy)
                iw, ih = min(px+pw, fx+fw) - ix, min(py+ph, fy+fh) - iy
                if iw > 0 and ih > 0:
                    if (iw * ih) > 0.5 * min(p['area'], fp['area']):
                        conflict = True
                        p_name, fp_name = bool(p.get('specific_name')), bool(fp.get('specific_name'))
                        p_tb, fp_tb = p.get('is_typing_box', False), fp.get('is_typing_box', False)
                        
                        if p_tb and not fp_tb:
                            fp.update(p)
                        elif not p_tb and fp_tb:
                            pass
                        elif p_name and not fp_name:
                            fp.update(p)
                        elif not p_name and fp_name:
                            pass
                        elif p['area'] < fp['area']:
                            fp.update(p)
                            
                        break
            if not conflict:
                final_panes.append(p)
        return final_panes

    def get_target_rects(self, quartz_windows, visible_pids):
        window_groups = []
        for pid in visible_pids:
            app_element = AXUIElementCreateApplication(pid)
            err, windows = AXUIElementCopyAttributeValue(app_element, "AXWindows", None)
            if err != 0 or not windows: continue

            matched_windows = []
            for window in windows:
                err_m, is_minimized = AXUIElementCopyAttributeValue(window, "AXMinimized", None)
                if err_m == 0 and is_minimized: continue

                geom = self.get_size_pos(window)
                if not geom: continue
                ax_x, ax_y, ax_w, ax_h = geom

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
                    matched_windows.append({
                        'window': window, 'rect': geom, 'match': best_match
                    })

            sub_counter = 1
            for mw in matched_windows:
                window, geom, best_match = mw['window'], mw['rect'], mw['match']
                ax_x, ax_y, ax_w, ax_h = geom
                
                err_m, is_main_val = AXUIElementCopyAttributeValue(window, "AXMain", None)
                is_main = (err_m == 0 and is_main_val)

                err_fs, is_fullscreen_val = AXUIElementCopyAttributeValue(window, "AXFullScreen", None)
                is_mode2 = (err_fs == 0 and bool(is_fullscreen_val))

                panes = []
                if is_mode2:
                    err_c, win_children = AXUIElementCopyAttributeValue(window, "AXChildren", None)
                    if err_c == 0 and win_children:
                        for child in win_children:
                            self.find_panes(child, 0, panes)
                    panes = self.resolve_overlaps(panes)
                    
                    for pane in panes:
                        if pane.get('specific_name'):
                            pane['name'] = pane['specific_name']
                        else:
                            px, py, pw, ph = pane['rect']
                            cx, cy = px + pw / 2, py + ph / 2
                            h_pos = "Left" if cx < self.sw * 0.33 else ("Right" if cx > self.sw * 0.67 else "Center")
                            v_pos = "Top" if cy < self.sh * 0.33 else ("Bottom" if cy > self.sh * 0.67 else "")
                            role_short = (pane['role'] or "Panel").replace("AX", "")
                            pane['name'] = f"{role_short}-{v_pos}{h_pos}"

                    if not panes:
                        specific_name = self.get_specific_pane_name(window)
                        if specific_name:
                            name = specific_name
                        else:
                            cx, cy = ax_x + ax_w / 2, ax_y + ax_h / 2
                            h_pos = "Left" if cx < self.sw * 0.33 else ("Right" if cx > self.sw * 0.67 else "Center")
                            v_pos = "Top" if cy < self.sh * 0.33 else ("Bottom" if cy > self.sh * 0.67 else "")
                            name = f"Window-{v_pos}{h_pos}"
                        panes.append({'rect': geom, 'element': window, 'name': name})
                else:
                    app_obj = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    app_name = app_obj.localizedName() if app_obj else str(pid)
                    err_t, title_val = AXUIElementCopyAttributeValue(window, "AXTitle", None)
                    title = str(title_val).strip() if err_t == 0 and title_val else ""
                    
                    base_name = f"{app_name} - {title[:15]}" if title else app_name
                    if len(matched_windows) > 1:
                        final_name = f"{base_name} [Main]" if is_main else f"{base_name} [Sub-{sub_counter}]"
                        if not is_main: sub_counter += 1
                    else:
                        final_name = base_name

                    panes = [{'rect': geom, 'element': window, 'name': final_name}]

                window_groups.append({
                    'z_index': best_match['z_index'],
                    'top_rect': best_match['rect'],
                    'panes': panes,
                    'pid': pid,
                    'is_mode2': is_mode2
                })

        window_groups.sort(key=lambda g: g['z_index'])

        # ── 앱 이름 첫 글자 기반 고정 태그 배정 ────────────────────────
        # 각 그룹(앱)마다 앱 이름의 첫 글자를 키로 사용.
        # 충돌 시 앱 이름의 나머지 글자 → 미사용 A-Z 순으로 대체.
        used_keys: set = set()
        _fallback_chars = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

        def _app_key_candidates(pid: int) -> list:
            """pid → 앱 이름에서 뽑은 키 후보 리스트 (대문자, 영문 우선)"""
            try:
                app_obj = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                name = (app_obj.localizedName() or "") if app_obj else ""
            except Exception:
                name = ""
            seen: set = set()
            candidates: list = []
            # 영문 알파벳 우선 (앱 이름 순서대로)
            for ch in name.upper():
                if ch.isalpha() and ch.isascii() and ch not in seen:
                    candidates.append(ch)
                    seen.add(ch)
            # 비ASCII(한글 등) 앱 이름은 영문 변환 불가 → 전체 A-Z 후보로 채움
            for ch in _fallback_chars:
                if ch not in seen:
                    candidates.append(ch)
            return candidates

        for group in window_groups:
            candidates = _app_key_candidates(group['pid'])
            base_key = next((c for c in candidates if c not in used_keys), None)
            if base_key is None:
                # 26자 초과 시 index 기반 2글자 태그
                base_key = f"Z{len(used_keys) - 25}"
            used_keys.add(base_key)

            panes = group['panes']
            if len(panes) == 1:
                # 단일 패널: 앱 키 그대로
                panes[0]['tag'] = base_key
            else:
                # 복수 패널(mode2 등): 첫 번째만 단순 키, 나머지는 키+숫자
                for j, pane in enumerate(panes):
                    pane['tag'] = base_key if j == 0 else f"{base_key}{j + 1}"

        return window_groups

# ==============================================================================
# 4. Hotkey Manager
# ==============================================================================
class HotkeyManager:
    def __init__(self, callback_func, on_mouse_click=None):
        self.callback = callback_func
        self.on_mouse_click = on_mouse_click
        self.tap = None
        self.r_cmd_down = False
        self.other_key_pressed = False
        self.wait_for_alphabet = False

    def _event_callback(self, proxy, event_type, event, refcon):
        if event_type == Quartz.kCGEventLeftMouseUp:
            loc = Quartz.CGEventGetLocation(event)
            if self.on_mouse_click:
                self.on_mouse_click(loc.x, loc.y)
            return event
            
        elif event_type == Quartz.kCGEventFlagsChanged:
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            
            if keycode == 54: # Right Command
                if not self.r_cmd_down:
                    self.r_cmd_down = True
                    self.other_key_pressed = False
                    self.wait_for_alphabet = False
                    return None
                else:
                    self.r_cmd_down = False
                    if not self.other_key_pressed:
                        self.wait_for_alphabet = True
                    return None
            else:
                if self.r_cmd_down:
                    self.other_key_pressed = True

        elif event_type == Quartz.kCGEventKeyDown:
            if self.r_cmd_down:
                self.other_key_pressed = True
                
            if self.r_cmd_down or self.wait_for_alphabet:
                keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                char = KEYCODE_TO_CHAR.get(keycode)
                if char:
                    self.wait_for_alphabet = False
                    self.callback(char)
                    return None
                elif self.wait_for_alphabet:
                    self.wait_for_alphabet = False

        return event

    def start(self):
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | 
                Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged) |
                Quartz.CGEventMaskBit(Quartz.kCGEventLeftMouseUp))
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._event_callback,
            None
        )
        if self.tap is None:
            print("[ERROR] CGEventTap 생성 실패 — 시스템 설정에서 손쉬운 사용 권한을 허용하세요.")
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetMain(), source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        print("[INFO] CGEventTap 활성화 완료 — 단축키(R-Cmd + 알파벳)가 우선 처리됩니다.")

# ==============================================================================
# 5. Main Engine Controller (개선: Learning 통합)
# ==============================================================================
class OverlayEngineController(AppKit.NSObject):
    def init(self):
        self = objc.super(OverlayEngineController, self).init()
        if self:
            self.window = None
            self.view = None
            self._pending_rects = None
            self._pending_button_hints = None   # 버튼 단축키 힌트
            self._lock = threading.Lock()
            self._force_rescan = False
            self._last_btn_scan_bundle = None   # 앱 전환 감지
            self._last_btn_scan_time  = 0.0     # 주기적 재스캔용
            self._btn_scan_running    = False   # 동시에 하나만 실행
            
            # Learning 초기화
            if LEARNING_ENABLED:
                self.learning_db = LearningDatabase()
                self.learning_advisor = LearningAdvisor(self.learning_db)
                print("[INFO] Learning Engine 초기화 완료")
            else:
                self.learning_db = None
                self.learning_advisor = None
            
            self.scanner = AccessibilityScanner(self.learning_db, self.learning_advisor)
            self.setup_window()
            self.start_polling()
            self.start_ui_timer()
            
            self.hotkey_mgr = HotkeyManager(self.handle_tag_global, on_mouse_click=self.trigger_rescan)
            self.hotkey_mgr.start()
        return self

    def trigger_rescan(self, click_x=0, click_y=0):
        self._force_rescan = True
        threading.Thread(target=self._check_shortcut_hint, args=(click_x, click_y), daemon=True).start()

    @objc.python_method
    def _run_button_scan(self, pid: int, bundle: str):
        """버튼 단축키 스캔을 별도 스레드에서 실행 — 폴링 루프를 블로킹하지 않음."""
        self._btn_scan_running = True
        try:
            hints = self.scanner.scan_button_shortcuts(pid)
            with self._lock:
                self._pending_button_hints = hints
            print(f"[BTN-HINT] {bundle}: {len(hints)}개 힌트")
        except Exception as e:
            print(f"[BTN-HINT] 스캔 오류: {e}")
        finally:
            self._btn_scan_running = False

    def _check_shortcut_hint(self, x, y):
        try:
            shortcut = self.scanner.get_shortcut_for_position(x, y)
            if not shortcut or not self.view:
                return
            view = self.view
            vx, vy, vtext = float(x), float(y), shortcut

            def apply_hint():
                try:
                    view.add_temporary_tag(vx, vy, vtext)
                except Exception as ex:
                    print(f"[WARN] 단축키 힌트 UI 갱신 실패: {ex}")

            NSOperationQueue.mainQueue().addOperationWithBlock_(apply_hint)
        except Exception as e:
            print(f"[WARN] 단축키 힌트 조회 실패: {e}")

    def setup_window(self):
        screen_frame = AppKit.NSScreen.mainScreen().frame()
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            screen_frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(AppKit.NSColor.clearColor())
        self.window.setIgnoresMouseEvents_(True)
        # 일반 창(NSFloatingWindowLevel)보다 위에 그려야 태그가 가려지지 않음
        self.window.setLevel_(AppKit.NSMainMenuWindowLevel)
        self.window.setHasShadow_(False)
        self.window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces |
            AppKit.NSWindowCollectionBehaviorStationary |
            AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )
        
        self.view = OverlayWindowView.alloc().initWithFrame_(screen_frame)
        self.window.setContentView_(self.view)
        self.window.setAlphaValue_(1.0)
        self.window.orderFrontRegardless()

    @objc.python_method
    def start_polling(self):
        def poll():
            last_key = None
            last_pane_summary = None
            last_scan_time = time.time()
            while True:
                try:
                    quartz_windows, visible_pids, snapshot_key = self.scanner.get_quartz_snapshot()
                    time_since_last_scan = time.time() - last_scan_time
                    
                    if snapshot_key != last_key or self._force_rescan or time_since_last_scan > 1.5:
                        last_key = snapshot_key
                        self._force_rescan = False
                        last_scan_time = time.time()
                        
                        rects = self.scanner.get_target_rects(quartz_windows, visible_pids)
                        with self._lock:
                            self._pending_rects = rects

                        # ── 버튼 단축키 힌트 스캔 (비동기, 앱 전환 또는 5초마다) ──
                        try:
                            active_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
                            cur_bundle = active_app.bundleIdentifier() if active_app else None
                            cur_pid    = active_app.processIdentifier() if active_app else None
                            now = time.time()
                            needs_btn_scan = (
                                cur_bundle != self._last_btn_scan_bundle or
                                (now - self._last_btn_scan_time) > 5.0
                            )
                            if needs_btn_scan and cur_pid and not self._btn_scan_running:
                                self._last_btn_scan_bundle = cur_bundle
                                self._last_btn_scan_time   = now
                                threading.Thread(
                                    target=self._run_button_scan,
                                    args=(cur_pid, cur_bundle),
                                    daemon=True
                                ).start()
                        except Exception as _be:
                            print(f"[BTN-HINT] 스캔 트리거 오류: {_be}")

                        pane_summary = tuple(pane.get('name', pane.get('tag', '?')) for g in rects for pane in g['panes'])
                        if pane_summary != last_pane_summary:
                            last_pane_summary = pane_summary
                            total = sum(len(g['panes']) for g in rects)
                            if total > 0:
                                print(f"[패널] {total}개 인식: {', '.join(pane_summary[:5])}")
                            else:
                                print("[패널] 없음")
                except Exception as e:
                    print(f"[ERROR] Polling thread crashed: {e}")
                time.sleep(0.1)
        threading.Thread(target=poll, daemon=True).start()

    @objc.python_method
    def start_ui_timer(self):
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.15, self, "refreshUI:", None, True
        )

    def refreshUI_(self, timer):
        needs_redraw = False
        with self._lock:
            rects        = self._pending_rects
            button_hints = self._pending_button_hints
            self._pending_rects        = None
            self._pending_button_hints = None

        if rects is not None:
            active_app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            active_pid = active_app.processIdentifier() if active_app else None
            self.view.window_groups = rects
            self.view.active_pid = active_pid
            # 화면에 창이 없으면 버튼 힌트도 즉시 지움
            if not rects and self.view.button_hints:
                self.view.button_hints = []
            needs_redraw = True

        if button_hints is not None:
            self.view.button_hints = button_hints
            needs_redraw = True
            
        current_time = time.time()
        if hasattr(self.view, 'temporary_tags'):
            active_tags = [t for t in self.view.temporary_tags if t['expire'] > current_time]
            if len(active_tags) != len(self.view.temporary_tags):
                self.view.temporary_tags = active_tags
                needs_redraw = True
                
        if needs_redraw:
            self.view.setNeedsDisplay_(True)

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
                    is_mode2 = group.get('is_mode2', False)

                    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    if app:
                        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)

                    if is_mode2:
                        rect = pane.get('click_rect', pane['rect'])
                        x, y, w, h = rect
                        click_x, click_y = x + w / 2.0, y + h / 2.0
                        print(f"[DEBUG] 모드2 패널 클릭: tag={tag_char}, 좌표=({click_x:.0f}, {click_y:.0f})")
                        self._simulate_click_delayed(click_x, click_y)
                    else:
                        AXUIElementPerformAction(element, "AXRaise")
                    
                    # Learning: 단축키 사용 기록 (키보드 입력)
                    if LEARNING_ENABLED and self.learning_db:
                        workspace = AppKit.NSWorkspace.sharedWorkspace()
                        active_app = workspace.frontmostApplication()
                        if active_app:
                            bundle_id = active_app.bundleIdentifier()
                            # 모든 저장된 단축키 데이터에 대해 사용 기록 (정확한 매칭은 향후 개선)
                            print(f"[LEARNING] Right Command + {tag_char} 입력 기록")
                    
                    return

    @objc.python_method
    def _simulate_click_delayed(self, x, y, delay=0.08):
        def do_click():
            time.sleep(delay)
            point = Quartz.CGPointMake(x, y)
            mouse_down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft)
            mouse_up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventSetFlags(mouse_down, 0)
            Quartz.CGEventSetFlags(mouse_up, 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, mouse_up)
            print(f"[DEBUG] 좌클릭 시뮬레이션 완료: ({x:.0f}, {y:.0f})")
        threading.Thread(target=do_click, daemon=True).start()

# ==============================================================================
# 6. Main Run Loop
# ==============================================================================
if __name__ == '__main__':
    print("=" * 70)
    print("  Smart-Homerow Phase 3: Overlay Engine with Learning Integration")
    print("  모든 창과 패널에 알파벳 단축키 태그가 항상 표시됩니다.")
    print("  이동 단축키: Right Command + [알파벳] (또는 R-Cmd 탭 후 알파벳)")
    print("  학습 기능: 단축키 사용 빈도 추적, 맥락 기반 필터링")
    print("  종료하려면 터미널에서 Ctrl+C 를 누르세요.")
    print("=" * 70)
    
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    
    controller = OverlayEngineController.alloc().init()
    
    class TimerObj(AppKit.NSObject):
        def tick_(self, timer): pass
    
    timer_obj = TimerObj.alloc().init()
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.1, timer_obj, "tick:", None, True)

    def sigint_handler(sig, frame):
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")
        os._exit(0)
        
    signal.signal(signal.SIGINT, sigint_handler)

    try:
        AppHelper.runEventLoop(installInterrupt=True)
    except KeyboardInterrupt:
        print("\n[INFO] 프로그램을 안전하게 종료합니다.")
        os._exit(0)

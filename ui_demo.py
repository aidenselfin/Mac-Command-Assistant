"""
음성 비서 UI 데모 (테스트 버전)
- 화면 하단 중앙에 Siri 스타일 토스트
- 우상단에 메뉴바 스타일 상태 인디케이터
실행: python3 ui_demo.py
조작:
  [Space]  R-cmd 시뮬레이션 — 전체 플로우 자동 재생 (듣기→STT→LLM→실행→완료)
  [1]      "기계학습 공부 모드 시작" 시나리오
  [2]      "유튜브 재생목록 세 번째 영상 틀어줘" 시나리오
  [3]      오류 시나리오 (마이크 권한)
  [Esc]    종료
"""

import tkinter as tk
import time

W, H = 560, 90          # 토스트 크기
DOT_W, DOT_H = 180, 36  # 상태 인디케이터 크기

STATES = {
    "idle":      ("#9ca3af", "대기"),
    "listening": ("#ef4444", "듣는 중…"),
    "stt":       ("#f59e0b", "받아쓰는 중…"),
    "llm":       ("#3b82f6", "생각 중…"),
    "exec":      ("#22c55e", "실행 중…"),
    "done":      ("#22c55e", "완료"),
    "error":     ("#f97316", "실패"),
}


class VoiceAssistantUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.sw = self.root.winfo_screenwidth()
        self.sh = self.root.winfo_screenheight()

        # ===== 토스트 (하단 중앙) =====
        self.toast = tk.Toplevel(self.root)
        self.toast.overrideredirect(True)
        self.toast.attributes("-topmost", True)
        self.toast.attributes("-alpha", 0.0)
        try:
            self.toast.attributes("-transparent", True)
        except tk.TclError:
            pass
        tx = (self.sw - W) // 2
        ty = self.sh - H - 80
        self.toast.geometry(f"{W}x{H}+{tx}+{ty}")
        self.toast.configure(bg="#111111")

        self.toast_frame = tk.Frame(self.toast, bg="#111111")
        self.toast_frame.pack(fill="both", expand=True, padx=2, pady=2)

        self.icon_label = tk.Label(
            self.toast_frame, text="●", fg="#9ca3af", bg="#111111",
            font=("SF Pro Display", 22)
        )
        self.icon_label.pack(side="left", padx=(18, 12))

        self.text_label = tk.Label(
            self.toast_frame, text="", fg="white", bg="#111111",
            font=("SF Pro Display", 16), anchor="w", justify="left",
            wraplength=W - 80
        )
        self.text_label.pack(side="left", fill="both", expand=True, padx=(0, 16))

        # ===== 메뉴바 인디케이터 (우상단) =====
        self.bar = tk.Toplevel(self.root)
        self.bar.overrideredirect(True)
        self.bar.attributes("-topmost", True)
        self.bar.attributes("-alpha", 0.95)
        bx = self.sw - DOT_W - 16
        by = 8
        self.bar.geometry(f"{DOT_W}x{DOT_H}+{bx}+{by}")
        self.bar.configure(bg="#1f2937")

        self.bar_dot = tk.Label(
            self.bar, text="●", fg="#9ca3af", bg="#1f2937",
            font=("SF Pro Display", 14)
        )
        self.bar_dot.pack(side="left", padx=(10, 6), pady=4)
        self.bar_text = tk.Label(
            self.bar, text="대기", fg="#e5e7eb", bg="#1f2937",
            font=("SF Pro Display", 12)
        )
        self.bar_text.pack(side="left", pady=4)

        # ===== 키 바인딩 =====
        self.root.bind_all("<space>", lambda e: self.run_default())
        self.root.bind_all("<Key-1>", lambda e: self.scenario_study())
        self.root.bind_all("<Key-2>", lambda e: self.scenario_youtube())
        self.root.bind_all("<Key-3>", lambda e: self.scenario_error())
        self.root.bind_all("<Escape>", lambda e: self.root.destroy())

        self.busy = False
        self.set_state("idle")
        self.show_hint()

    # -------- 상태 변경 --------
    def set_state(self, state):
        color, label = STATES[state]
        self.bar_dot.config(fg=color)
        self.bar_text.config(text=label)
        self.icon_label.config(fg=color)

    def show_toast(self, text, state=None, duration=None):
        if state:
            self.set_state(state)
            color, _ = STATES[state]
            self.icon_label.config(fg=color)
        self.text_label.config(text=text)
        self.fade_in()
        if duration:
            self.root.after(duration, self.fade_out)

    def fade_in(self):
        a = self.toast.attributes("-alpha")
        if a < 0.95:
            self.toast.attributes("-alpha", min(a + 0.1, 0.95))
            self.root.after(20, self.fade_in)

    def fade_out(self):
        a = self.toast.attributes("-alpha")
        if a > 0.05:
            self.toast.attributes("-alpha", max(a - 0.08, 0.0))
            self.root.after(30, self.fade_out)
        else:
            self.toast.attributes("-alpha", 0.0)
            self.text_label.config(text="")

    def show_hint(self):
        self.text_label.config(
            text="Space: 데모 재생   1·2·3: 시나리오   Esc: 종료",
            fg="#9ca3af"
        )
        self.toast.attributes("-alpha", 0.7)
        self.root.after(3500, lambda: (
            self.text_label.config(fg="white"),
            self.fade_out()
        ))

    # -------- 시나리오 재생 --------
    def chain(self, steps):
        """steps: [(delay_ms, callable)]"""
        if self.busy:
            return
        self.busy = True
        t = 0
        for delay, fn in steps:
            t += delay
            self.root.after(t, fn)
        self.root.after(t + 100, lambda: setattr(self, "busy", False))

    def run_default(self):
        self.scenario_study()

    def scenario_study(self):
        self.chain([
            (0,    lambda: self.show_toast("🎙  듣는 중…  (말씀하세요)", "listening")),
            (1800, lambda: self.show_toast("받아쓰는 중…", "stt")),
            (900,  lambda: self.show_toast("“기계학습 공부 모드 시작”", "stt")),
            (1200, lambda: self.show_toast("생각 중…  (의도 분석)", "llm")),
            (1400, lambda: self.show_toast("실행 중…  Zoom · Chrome 배치", "exec")),
            (1800, lambda: self.show_toast("✓ 기계학습 공부 모드 적용 (1.8초)", "done", duration=2000)),
            (2400, lambda: self.set_state("idle")),
        ])

    def scenario_youtube(self):
        self.chain([
            (0,    lambda: self.show_toast("🎙  듣는 중…", "listening")),
            (1600, lambda: self.show_toast("“유튜브 재생목록 세 번째 영상 틀어줘”", "stt")),
            (1400, lambda: self.show_toast("생각 중…  index=3 추출", "llm")),
            (1200, lambda: self.show_toast("실행 중…  AXUIElement → 좌표 클릭", "exec")),
            (1600, lambda: self.show_toast("✓ 3번째 영상 재생 ▶", "done", duration=2000)),
            (2400, lambda: self.set_state("idle")),
        ])

    def scenario_error(self):
        self.chain([
            (0,    lambda: self.show_toast("🎙  듣는 중…", "listening")),
            (1200, lambda: self.show_toast("⚠ 마이크 권한이 필요합니다 — 시스템 설정 열기",
                                           "error", duration=3000)),
            (3400, lambda: self.set_state("idle")),
        ])

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    VoiceAssistantUI().run()

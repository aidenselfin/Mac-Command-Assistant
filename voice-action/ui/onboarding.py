import os
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox

from config import Config, save_config
from ui.dispatcher import request_gui


def _show_onboarding_sync(permissions: dict[str, bool], config: Config) -> None:
    root = tk.Tk()
    root.title("Voice-Action 설정")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    tk.Label(root, text="Voice-Action 초기 설정", font=("System", 16, "bold"),
             pady=10).pack()

    perm_labels = {
        "microphone": "마이크 권한",
        "full_disk": "전체 디스크 접근",
        "accessibility": "손쉬운 사용(Accessibility)",
    }

    pref_urls = {
        "microphone": "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone",
        "full_disk": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    }

    perm_frame = tk.Frame(root, padx=20, pady=6)
    perm_frame.pack(fill="x")

    for key, label in perm_labels.items():
        ok = permissions.get(key, False)
        row = tk.Frame(perm_frame, pady=3)
        row.pack(fill="x")

        status = "✅" if ok else "❌"
        tk.Label(row, text=f"{status}  {label}", font=("System", 13),
                 anchor="w", width=32).pack(side="left")

        if not ok:
            url = pref_urls[key]
            tk.Button(
                row, text="시스템 설정 →", font=("System", 11),
                command=lambda u=url: subprocess.run(["open", u])
            ).pack(side="right")

    if not config.anthropic_api_key:
        tk.Label(root, text="Anthropic API 키", font=("System", 13),
                 pady=12, anchor="w").pack(fill="x", padx=20)

        api_var = tk.StringVar()
        tk.Entry(root, textvariable=api_var, width=48, show="*",
                 font=("Menlo", 11)).pack(padx=20, pady=4)

        def save_api_key():
            key = api_var.get().strip()
            if not key:
                messagebox.showwarning("오류", "API 키를 입력하세요.")
                return
            config.anthropic_api_key = key
            os.environ["ANTHROPIC_API_KEY"] = key
            save_config(config)
            messagebox.showinfo("저장 완료", "API 키가 저장되었습니다.")

        tk.Button(root, text="저장", command=save_api_key,
                  bg="#0A84FF", fg="white", relief="flat",
                  padx=10, pady=4).pack(pady=4)

    btn_frame = tk.Frame(root, pady=10)
    btn_frame.pack()

    def restart():
        root.destroy()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    tk.Button(btn_frame, text="재시작", command=restart, width=12).pack(side="left", padx=6)
    tk.Button(btn_frame, text="닫기", command=root.destroy, width=12).pack(side="left", padx=6)

    root.mainloop()


def show_onboarding(permissions: dict[str, bool], config: Config) -> None:
    request_gui(_show_onboarding_sync, permissions, config)

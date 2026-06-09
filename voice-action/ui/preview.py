import tkinter as tk
from tkinter import font as tkfont

from ui.dispatcher import request_gui


def _show_preview_sync(actions: list[dict], truncated: bool = False, total_files: int = 0) -> bool:
    result = {"confirmed": False}

    root = tk.Tk()
    root.title("Voice-Action 미리보기")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    has_delete = any(a.get("action") == "delete" for a in actions)

    if has_delete:
        tk.Label(
            root,
            text="⚠️  삭제 항목이 포함되어 있습니다. 파일은 휴지통으로 이동됩니다.",
            bg="#FFF3CD", fg="#856404", padx=12, pady=6,
            font=("System", 12), anchor="w",
        ).pack(fill="x")

    frame = tk.Frame(root, padx=16, pady=10)
    frame.pack(fill="both", expand=True)

    normal_font = tkfont.Font(family="Menlo", size=11)

    for a in actions:
        act = a.get("action", "")
        src = a.get("src", "")
        dst = a.get("dst", "")
        reason = a.get("reason", "")

        row = tk.Frame(frame, pady=3)
        row.pack(fill="x")

        if act == "delete":
            color = "#D9534F"
            label_text = f"🗑️  삭제  {src}"
        elif act == "move":
            color = "#333"
            label_text = f"→  이동  {src}\n       → {dst}"
        elif act == "rename":
            color = "#333"
            label_text = f"✎  이름  {src}\n       → {dst}"
        else:
            color = "#888"
            label_text = f"?  {act}  {src}"

        tk.Label(row, text=label_text, fg=color, font=normal_font,
                 anchor="w", justify="left").pack(side="left", fill="x", expand=True)

        if reason:
            tk.Label(row, text=f"  ({reason})", fg="#888",
                     font=normal_font, anchor="w").pack(side="left")

    if truncated and total_files > 500:
        tk.Label(
            root,
            text=f"⚠️ 최신 500개만 분석했습니다 (전체 {total_files}개)",
            fg="#856404", bg="#FFF3CD", padx=12, pady=4,
            font=("System", 11), anchor="w",
        ).pack(fill="x")

    btn_frame = tk.Frame(root, pady=10, padx=16)
    btn_frame.pack(fill="x")

    def confirm():
        result["confirmed"] = True
        root.destroy()

    def cancel():
        result["confirmed"] = False
        root.destroy()

    tk.Button(btn_frame, text="취소", width=10, command=cancel).pack(side="right", padx=4)
    tk.Button(btn_frame, text="실행", width=10, command=confirm,
              bg="#0A84FF", fg="white", relief="flat").pack(side="right", padx=4)

    root.bind("<Escape>", lambda e: cancel())
    root.bind("<FocusOut>", lambda e: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)

    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    root.mainloop()
    return result["confirmed"]


def show_preview(actions: list[dict], truncated: bool = False, total_files: int = 0) -> bool:
    return request_gui(_show_preview_sync, actions, truncated, total_files)

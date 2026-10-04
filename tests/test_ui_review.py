"""Approval display regressions (docs/reviews/2026-10-04-recent-changes-review.md, UI)."""

import pytest

import voice_action.ui as ui_module
from tests.test_ui import FakeAdapter
from voice_action.planner import ApprovalRequired
from voice_action.runtime import RuntimeState
from voice_action.ui import UIMessage, UIMessageType, UIPresenter


def test_display_path_escapes_control_characters_only():
    assert ui_module.display_path('/w/a\nb\t"c".txt') == '/w/a\\nb\\t"c".txt'
    assert ui_module.display_path("/w/회의록 (1).md") == "/w/회의록 (1).md"


def test_delete_approval_shows_one_row_per_file_even_with_newlines():
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk display unavailable: {exc}")
    try:
        view = ui_module.TkView(root)
        presenter = UIPresenter(FakeAdapter(), view)
        view.bind_presenter(presenter)
        presenter.handle(UIMessage(UIMessageType.STATE, RuntimeState.WAITING_APPROVAL))
        targets = ("/w/a\n2  /w/fake.txt", "/w/b.txt")
        presenter.handle(
            UIMessage(
                UIMessageType.OUTCOME,
                ApprovalRequired(
                    "local__delete_files", targets[0], "delete", 0, "", False, {}, targets
                ),
            )
        )
        rows = [
            line
            for line in view.approval_output.get("1.0", "end").splitlines()
            if line.lstrip()[:1].isdigit()
        ]
        assert rows == ["1  /w/a\\n2  /w/fake.txt", "2  /w/b.txt"]
    finally:
        root.update()  # flush pending Tk events; a destroy with them queued crashes macOS Tk
        root.destroy()

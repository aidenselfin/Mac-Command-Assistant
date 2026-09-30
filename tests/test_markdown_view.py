from voice_action.markdown_view import render_markdown


def test_commonmark_and_gfm_tables():
    html = render_markdown(
        '# 제목\n\n| 항목 | 값 |\n| :--- | ---: |\n'
        '| **한글** | `a\\|b` |\n| 빈 셀 |\n\n- 목록\n\n'
        '```md\n| 코드 | 표 아님 |\n| --- | --- |\n```\n'
    )
    assert '<h1>제목</h1>' in html
    assert html.count('<table>') == 1
    assert '<strong>한글</strong>' in html
    assert '<code>a|b</code>' in html
    assert 'text-align:right' in html
    assert '<li>목록</li>' in html
    assert '<pre><code' in html
    assert '<script>' not in render_markdown('<script>alert(1)</script>')


def test_ui_renders_markdown_result_and_preserves_original():
    import tkinter as tk

    import pytest
    from tkinterweb import HtmlFrame

    from voice_action.planner import Completed
    from voice_action.ui import TkView

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(str(exc))
    try:
        view = TkView(root)
        root.geometry('720x520')
        root.update()
        if hasattr(view, 'show_request'):
            view.show_request('표를 보여줘')
        else:
            view.show_output('표를 보여줘')
        root.update()
        text = '# 제목\n\n| A | B |\n| --- | --- |\n| **한글** | 값 |'
        if hasattr(view, 'show_outcome'):
            view.show_outcome(Completed(text))
        else:
            view.show_output(text)
        root.update()
        html = next(w for w in view.output.winfo_children() if isinstance(w, HtmlFrame))
        assert html.document.querySelector('table') is not None
        assert html.document.querySelector('strong').textContent == '한글'
        view._copy_output()
        assert text in root.clipboard_get()
        root.geometry('720x520')
        root.update()
        assert html.winfo_width() < view.output.winfo_width()
        assert html.winfo_ismapped()
        assert html.winfo_height() <= view.output.winfo_height() - 40
        assert html.winfo_y() >= 0
        assert html.winfo_y() + html.winfo_height() <= view.output.winfo_height()
        # Check actual rendered pixels, not just a successfully parsed DOM.
        pixels = tk.PhotoImage(data=html.html.image())
        colors = {pixels.get(x, y) for x in range(0, pixels.width(), 4)
                  for y in range(0, pixels.height(), 4)}
        assert len(colors) > 2
    finally:
        root.destroy()

"""CommonMark with GFM tables, displayed as HTML in the Tk result area."""
from markdown_it import MarkdownIt


def render_markdown(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).enable("table").render(text)


def insert_markdown(output, text: str) -> None:
    from tkinterweb import HtmlFrame

    view = HtmlFrame(
        output, messages_enabled=False, javascript_enabled=False,
        images_enabled=False, objects_enabled=False, forms_enabled=False,
        on_link_click=lambda url: None, height=320,
    )
    view.load_html(render_markdown(text))
    view.add_css("""
        body { font-family: sans-serif; font-size: 14px; color: #20242c;
               background: white; margin: 12px; }
        table { border-collapse: collapse; width: 100%; margin: 12px 0; }
        th, td { border: 1px solid #d9dee7; padding: 8px 12px; vertical-align: top; }
        th { background: #eef1f6; font-weight: bold; }
        pre { background: #f5f6f8; padding: 12px; white-space: pre-wrap; }
        code { font-family: monospace; background: #f5f6f8; }
        blockquote { border-left: 3px solid #d9dee7; padding-left: 12px; }
    """)
    def resize(event=None):
        view.configure(width=max(100, output.winfo_width() - 50))

    output.bind("<Configure>", resize, add="+")
    resize()
    output.window_create("end", window=view)

"""A file the user dropped on the window, checked before it becomes a command's target.

Everything stays inside the allowed folders: a file outside them, a link, a folder or more
than one file is refused, and the command is not sent at all.
"""

from pathlib import Path

from voice_action.conversion import _inside
from voice_action.mcp_client import ToolRejectedError


class AttachmentError(ValueError):
    """The reason a dropped file was refused, written for the user."""


def check_attachment(raw: object, roots: tuple[Path, ...]) -> str:
    """The canonical path of one regular file inside an allowed folder, else AttachmentError."""
    if not isinstance(raw, str) or not raw.strip():
        raise AttachmentError("첨부할 파일 경로가 올바르지 않습니다.")
    resolved_roots = tuple(root.resolve() for root in roots)
    try:
        path = _inside(raw.strip(), resolved_roots, exists=True)
    except ToolRejectedError as exc:
        raise AttachmentError(
            "허용 폴더 밖이거나 링크, 없는 파일이라 첨부할 수 없어요. 요청을 거부했습니다. "
            "허용 폴더: " + ", ".join(str(root) for root in resolved_roots)
        ) from exc
    if not path.is_file():
        raise AttachmentError("폴더나 특수 파일은 첨부할 수 없어요. 파일 하나만 놓아 주세요.")
    return str(path)

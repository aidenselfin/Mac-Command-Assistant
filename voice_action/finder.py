"""Show files the app just wrote in Finder (macOS).

Only the paths the app itself reports as changed are passed in, and each one is checked again
here: inside an allowed folder, not a link, still there. A failure only means nothing opens;
it never changes the result of the file work that came before.
"""

import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from voice_action.conversion import _inside
from voice_action.mcp_client import ToolRejectedError

MAX_REVEALED = 10


def _workspace():
    from AppKit import NSWorkspace

    return NSWorkspace.sharedWorkspace()


def reveal(
    paths: Iterable[str],
    roots: tuple[Path, ...],
    *,
    workspace: Callable[[], object] = _workspace,
) -> bool:
    """Select the files in Finder. True if a window was asked to show at least one of them."""
    if sys.platform != "darwin":
        return False
    checked = []
    for raw in list(paths)[:MAX_REVEALED]:
        try:
            checked.append(str(_inside(raw, tuple(root.resolve() for root in roots), exists=True)))
        except (ToolRejectedError, OSError):
            continue
    if not checked:
        return False
    try:
        from Foundation import NSURL

        urls = [NSURL.fileURLWithPath_(path) for path in checked]
        workspace().activateFileViewerSelectingURLs_(urls)
    except Exception:  # noqa: BLE001 - a missing framework or AppKit refusal must not matter
        return False
    return True

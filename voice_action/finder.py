"""Show files the app just wrote in Finder (macOS).

Only the paths the app itself reports as changed are passed in, and each one is checked again
here: inside an allowed folder, not a link, still there. A failure only means nothing opens;
it never changes the result of the file work that came before.
"""

import sys
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


# ---- placing the Finder window beside the app --------------------------------------------
#
# Finder's own "show in Finder" reuses a window that is already open, wherever it is, so the
# result can sit behind the app. Moving a window needs Accessibility (the permission the app
# already asks for), not Apple Events: scripting Finder would ask for Automation and can hang.

MIN_SIDE_WIDTH = 420  # a Finder window narrower than this is not worth showing
FINDER_WIDTH = 640
GAP = 8
MENU_BAR = 25
FINDER_BUNDLE = "com.apple.finder"


@dataclass(frozen=True)
class Frame:
    """A window rectangle in screen points, origin at the top left of the main screen."""

    x: int
    y: int
    w: int
    h: int


def side_by_side(app: Frame, screen: tuple[int, int]) -> Frame | None:
    """Where a Finder window goes next to the app: right if it fits, else left, else nowhere."""
    screen_w, screen_h = screen
    y = max(app.y, MENU_BAR)
    height = max(min(app.h, screen_h - y - GAP), 0)
    right = screen_w - (app.x + app.w + GAP)
    if right >= MIN_SIDE_WIDTH:
        return Frame(app.x + app.w + GAP, y, min(FINDER_WIDTH, right), height)
    left = app.x - GAP
    if left >= MIN_SIDE_WIDTH:
        width = min(FINDER_WIDTH, left)
        return Frame(app.x - GAP - width, y, width, height)
    return None


class FinderWindows(Protocol):
    def is_trusted(self) -> bool: ...

    def windows(self) -> list[tuple[str, object]]:
        """(title, handle) of Finder's windows, front to back."""

    def move(self, handle: object, frame: Frame) -> bool: ...


class AXFinderWindows:
    """Finder's windows through the Accessibility API (needs the Accessibility permission)."""

    def is_trusted(self) -> bool:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())

    def _application(self):
        from AppKit import NSRunningApplication
        from ApplicationServices import AXUIElementCreateApplication

        running = NSRunningApplication.runningApplicationsWithBundleIdentifier_(FINDER_BUNDLE)
        if not running:
            return None
        return AXUIElementCreateApplication(running[0].processIdentifier())

    def windows(self) -> list[tuple[str, object]]:
        from ApplicationServices import AXUIElementCopyAttributeValue

        application = self._application()
        if application is None:
            return []
        error, handles = AXUIElementCopyAttributeValue(application, "AXWindows", None)
        if error or not handles:
            return []
        found = []
        for handle in handles:
            error, title = AXUIElementCopyAttributeValue(handle, "AXTitle", None)
            found.append((str(title) if not error and title else "", handle))
        return found

    def move(self, handle: object, frame: Frame) -> bool:
        from ApplicationServices import (
            AXUIElementSetAttributeValue,
            AXValueCreate,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        from Quartz import CGPoint, CGSize

        point = AXValueCreate(kAXValueCGPointType, CGPoint(frame.x, frame.y))
        size = AXValueCreate(kAXValueCGSizeType, CGSize(frame.w, frame.h))
        # Size first: a window that is resized after it moved can run off the screen's edge.
        return (
            AXUIElementSetAttributeValue(handle, "AXSize", size) == 0
            and AXUIElementSetAttributeValue(handle, "AXPosition", point) == 0
        )


def arrange(
    folder: str,
    app: Frame,
    screen: tuple[int, int],
    *,
    windows: FinderWindows | None = None,
) -> str:
    """One attempt to put Finder's window for `folder` beside the app.

    "moved", "waiting" (the window is not there yet: try again shortly), "untrusted" (no
    Accessibility permission) or "no_space". Only a window titled like the folder is touched,
    so no unrelated Finder window is ever moved.
    """
    if sys.platform != "darwin":
        return "no_space"
    windows = windows or AXFinderWindows()
    try:
        if not windows.is_trusted():
            return "untrusted"
        target = side_by_side(app, screen)
        if target is None:
            return "no_space"
        wanted = unicodedata.normalize("NFC", folder)
        for title, handle in windows.windows():
            if unicodedata.normalize("NFC", title) == wanted:
                return "moved" if windows.move(handle, target) else "waiting"
    except Exception:  # noqa: BLE001 - placing a window is a nicety, never a failure
        return "waiting"
    return "waiting"

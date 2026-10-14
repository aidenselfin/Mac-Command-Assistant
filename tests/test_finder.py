"""Which files a command changed, and the Finder reveal that follows."""

import json
import shutil
import sys
import unicodedata
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from voice_action.conversion import CONVERT_TOOL
from voice_action.mcp_client import ToolDefinition, ToolResult
from voice_action.planner import ApprovalRequired, Completed, ModelTurn, Planner, ToolCall
from voice_action.undo import UndoJournal

TOOLS = [
    ToolDefinition(name, name, {"type": "object"})
    for name in ("filesystem__read_text_file", "filesystem__write_file")
]


def make(config, turns):
    gateway = AsyncMock(tools=[])
    gateway.list_tools.return_value = list(TOOLS)
    gateway.validate_result_path.side_effect = lambda value: Path(value)
    gateway.call_tool.return_value = ToolResult(True, "ok")

    async def normalize(_name, arguments):
        return dict(arguments)

    gateway.normalize_tool_arguments.side_effect = normalize
    model = AsyncMock()
    model.generate.side_effect = list(turns)
    root = config.allowed_directories[0]
    undo = UndoJournal(root / ".finder-undo.sqlite3", config.allowed_directories)
    return Planner(config, gateway, model, undo_journal=undo), gateway


def call(name, arguments):
    return ModelTurn(None, [ToolCall("c", name, json.dumps(arguments, ensure_ascii=False))])


async def test_write_success_reports_the_exact_path_the_app_wrote(config):
    root = config.allowed_directories[0]
    target = root / "한글 요약.txt"
    planner, _ = make(
        config,
        [call("filesystem__write_file", {"path": str(target), "content": "x"}),
         ModelTurn("저장했습니다. /etc/passwd에 있습니다.", [])],
    )
    pending = await planner.run("한글 요약.txt에 x 저장해줘")
    assert isinstance(pending, ApprovalRequired)

    done = await planner.approve(pending.context)

    # The model's sentence names another path; the result carries only what was written.
    assert isinstance(done, Completed) and done.changed_paths == (str(target),)


async def test_nothing_is_reported_before_approval_or_after_a_reject(config):
    root = config.allowed_directories[0]
    planner, gateway = make(
        config, [call("filesystem__write_file", {"path": str(root / "a.txt"), "content": "x"})]
    )
    pending = await planner.run("a.txt에 x 저장해줘")
    assert isinstance(pending, ApprovalRequired)  # not a Completed: nothing to reveal yet
    rejected = planner.reject(pending.context)
    assert rejected.changed_paths == ()
    gateway.call_tool.assert_not_awaited()


async def test_a_failed_write_reports_no_path(config):
    root = config.allowed_directories[0]
    planner, gateway = make(
        config,
        [call("filesystem__write_file", {"path": str(root / "a.txt"), "content": "x"}),
         ModelTurn("실패했습니다.", [])],
    )
    gateway.call_tool.return_value = ToolResult(False, "disk full", error="disk full")
    pending = await planner.run("a.txt에 x 저장해줘")
    done = await planner.approve(pending.context)
    assert getattr(done, "changed_paths", ()) == ()


async def test_a_read_only_answer_changes_nothing(config):
    root = config.allowed_directories[0]
    (root / "a.txt").write_text("x")
    planner, _ = make(
        config, [call("filesystem__read_text_file", {"path": str(root / "a.txt")}),
                 ModelTurn("x입니다.", [])]
    )
    assert (await planner.run("a.txt 읽어줘")).changed_paths == ()


async def test_delete_reports_no_path_to_reveal(config):
    root = config.allowed_directories[0]
    (root / "old.log").write_text("x")
    planner, _ = make(
        config, [call("local__delete_files", {"paths": [str(root / "old.log")]})]
    )
    pending = await planner.run("old.log 삭제해줘")
    assert isinstance(pending, ApprovalRequired) and pending.operation == "delete"
    done = await planner.approve(pending.context)
    assert isinstance(done, Completed) and done.changed_paths == ()
    assert not (root / "old.log").exists()


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("textutil"), reason="macOS")
async def test_conversion_reports_the_new_file_not_the_source(config):
    root = config.allowed_directories[0]
    (root / "메모.txt").write_text("hello")
    planner, _ = make(
        config,
        [call(CONVERT_TOOL, {"source": str(root / "메모.txt"), "target_format": "html"}),
         ModelTurn("변환했습니다.", [])],
    )
    pending = await planner.run("메모.txt를 html로 변환해줘")
    done = await planner.approve(pending.context)
    assert done.changed_paths == (str(root / "메모.html"),)
    assert Path(done.changed_paths[0]).exists()  # noqa: ASYNC240 - one stat in a test


# ---- reveal -----------------------------------------------------------------------------

from voice_action import finder  # noqa: E402

pytestmark_mac = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


class FakeWorkspace:
    def __init__(self, error=None):
        self.shown, self.error = [], error

    def activateFileViewerSelectingURLs_(self, urls):  # noqa: N802 - the Cocoa method name
        if self.error:
            raise self.error
        # Cocoa hands paths back decomposed (NFD); that is the same file, so compare as NFC.
        self.shown.append([unicodedata.normalize("NFC", str(url.path())) for url in urls])


@pytestmark_mac
def test_reveal_selects_exactly_the_given_files_including_hangul_and_spaces(tmp_path):
    root = tmp_path.resolve()
    first, second = root / "한글 요약.txt", root / "하위 폴더" / "b c.md"
    second.parent.mkdir()
    first.write_text("x")
    second.write_text("x")
    workspace = FakeWorkspace()

    assert finder.reveal((str(first), str(second)), (root,), workspace=lambda: workspace)
    nfc = unicodedata.normalize
    assert workspace.shown == [[nfc("NFC", str(first)), nfc("NFC", str(second))]]


@pytestmark_mac
def test_reveal_refuses_files_outside_the_allowed_folders_links_and_missing_files(tmp_path):
    (tmp_path / "work").mkdir()
    root = (tmp_path / "work").resolve()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    link = root / "link.txt"
    link.symlink_to(outside)
    inside = root / "ok.txt"
    inside.write_text("x")
    workspace = FakeWorkspace()

    assert not finder.reveal(
        (str(outside), str(link), str(root / "missing.txt"), "relative.txt"),
        (root,), workspace=lambda: workspace,
    )
    assert workspace.shown == []
    # One valid file among bad ones is still shown; the bad ones are dropped, not fatal.
    assert finder.reveal((str(outside), str(inside)), (root,), workspace=lambda: workspace)
    assert workspace.shown == [[str(inside)]]


@pytestmark_mac
def test_a_finder_failure_is_swallowed(tmp_path):
    root = tmp_path.resolve()
    (root / "a.txt").write_text("x")
    broken = FakeWorkspace(error=RuntimeError("AppKit refused"))
    assert finder.reveal((str(root / "a.txt"),), (root,), workspace=lambda: broken) is False


def test_nothing_to_show_is_not_an_error(tmp_path):
    assert finder.reveal((), (tmp_path,), workspace=lambda: FakeWorkspace()) is False


# ---- placing the window beside the app ---------------------------------------------------

from voice_action.finder import Frame, arrange, side_by_side  # noqa: E402

SCREEN = (1470, 956)


def test_the_finder_window_goes_right_of_the_app_at_the_same_height():
    app = Frame(100, 80, 700, 800)
    assert side_by_side(app, SCREEN) == Frame(808, 80, 640, 800)


def test_it_shrinks_to_the_room_that_is_left_and_uses_the_left_side_when_the_right_is_full():
    assert side_by_side(Frame(100, 80, 700, 800), (1400, 956)) == Frame(808, 80, 592, 800)
    app = Frame(780, 80, 700, 800)  # pushed against the right edge of the screen
    assert side_by_side(app, SCREEN) == Frame(132, 80, 640, 800)


def test_with_no_room_on_either_side_nothing_is_moved():
    assert side_by_side(Frame(300, 80, 860, 800), SCREEN) is None  # 410 free on each side


def test_the_window_stays_below_the_menu_bar_and_on_the_screen():
    frame = side_by_side(Frame(100, 0, 700, 2000), SCREEN)
    assert frame.y == 25 and frame.y + frame.h <= SCREEN[1]


class FakeWindows:
    def __init__(self, titles, trusted=True, move_ok=True):
        self.titles, self.trusted, self.move_ok, self.moved = titles, trusted, move_ok, []

    def is_trusted(self):
        return self.trusted

    def windows(self):
        return [(title, index) for index, title in enumerate(self.titles)]

    def move(self, handle, frame):
        self.moved.append((handle, frame))
        return self.move_ok


@pytestmark_mac
def test_only_the_window_titled_like_the_folder_is_moved():
    windows = FakeWindows(["Downloads", "voice-action-test", "voice-action-test 2"])
    app = Frame(100, 80, 700, 800)
    assert arrange("voice-action-test", app, SCREEN, windows=windows) == "moved"
    assert windows.moved == [(1, Frame(808, 80, 640, 800))]


@pytestmark_mac
def test_folder_names_match_across_unicode_forms():
    windows = FakeWindows([unicodedata.normalize("NFD", "자료 폴더")])
    app = Frame(100, 80, 700, 800)
    folder = unicodedata.normalize("NFC", "자료 폴더")
    assert arrange(folder, app, SCREEN, windows=windows) == "moved"


@pytestmark_mac
def test_no_matching_window_yet_means_try_again_and_nothing_else_is_touched():
    windows = FakeWindows(["Downloads", "Desktop"])
    app = Frame(100, 80, 700, 800)
    assert arrange("voice-action-test", app, SCREEN, windows=windows) == "waiting"
    assert windows.moved == []


@pytestmark_mac
def test_without_the_accessibility_permission_it_says_so_and_moves_nothing():
    windows = FakeWindows(["voice-action-test"], trusted=False)
    app = Frame(100, 80, 700, 800)
    assert arrange("voice-action-test", app, SCREEN, windows=windows) == "untrusted"
    assert windows.moved == []


@pytestmark_mac
def test_no_room_and_a_failing_accessibility_call_never_raise():
    crowded = FakeWindows(["voice-action-test"])
    full = Frame(300, 80, 860, 800)
    assert arrange("voice-action-test", full, SCREEN, windows=crowded) == "no_space"
    assert crowded.moved == []

    class Broken(FakeWindows):
        def windows(self):
            raise RuntimeError("AX error")

    app = Frame(100, 80, 700, 800)
    assert arrange("voice-action-test", app, SCREEN, windows=Broken([])) == "waiting"


@pytest.mark.parametrize(("window", "screen", "expected"), [
    (Frame(100, 80, 600, 700), Frame(0, 25, 1470, 900), Frame(708, 80, 360, 560)),
    (Frame(780, 80, 600, 700), Frame(0, 25, 1470, 900), Frame(412, 80, 360, 560)),
    (Frame(300, 80, 860, 700), Frame(0, 25, 1470, 900), None),
    (Frame(-1300, -100, 600, 700), Frame(-1440, -200, 1440, 900),
     Frame(-692, -100, 360, 560)),
    (Frame(100, 800, 600, 700), Frame(0, 25, 1470, 900), Frame(708, 337, 360, 560)),
    (Frame(100, 80, 600, 700), Frame(0, 25, 1470, 400), None),
])
def test_panel_placement_uses_visible_screen_and_leaves_finder_unchanged(window, screen, expected):
    assert finder.panel_beside(window, screen) == expected

"""Push-to-talk: only a fresh temp clip is accepted, transcribed, deleted and sent back as text."""

import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from voice_action import transcribe as tr
from voice_action.native_bridge import NativeBridge
from voice_action.runtime import RuntimeState
from voice_action.ui import UIMessage, UIMessageType


def clip(name=None, data=b"x"):
    path = Path(tempfile.gettempdir()) / (name or f"{tr.PREFIX}test-{id(data)}.m4a")
    path.write_bytes(data)
    return path


def bridge(key="k"):
    events, done = [], threading.Event()

    def emit(value):
        events.append(value)
        done.set()

    transport = NativeBridge(Mock(), emit, key)
    transport.message(UIMessage(UIMessageType.STATE, RuntimeState.IDLE))
    events.clear()
    done.clear()
    return transport, events, done


def test_clip_path_rejects_foreign_names_locations_symlinks_and_empty_or_huge_files(tmp_path):
    good = clip()
    assert tr.clip_path(str(good)) == good.resolve()
    assert tr.clip_path(None) is None
    other = tmp_path / f"{tr.PREFIX}x.m4a"
    other.write_bytes(b"x")
    assert tr.clip_path(str(other)) is None  # not in the temp directory root
    assert tr.clip_path(str(clip("notmine.m4a"))) is None
    assert tr.clip_path(str(clip(f"{tr.PREFIX}empty.m4a", b""))) is None
    link = Path(tempfile.gettempdir()) / f"{tr.PREFIX}link.m4a"
    link.unlink(missing_ok=True)
    link.symlink_to(good)
    assert tr.clip_path(str(link)) is None
    for path in (good, link):
        path.unlink(missing_ok=True)
    (tmp_path / "notmine.m4a").unlink(missing_ok=True)


def test_voice_command_transcribes_deletes_clip_and_reports_text():
    transport, events, done = bridge()
    path = clip(f"{tr.PREFIX}ok.m4a")
    with patch("voice_action.native_bridge.transcribe", return_value="고양이 사진 찾아줘"):
        assert transport.command({"action": "voice", "path": str(path)})
        assert done.wait(5)
    assert events[-1] == {"type": "transcript", "text": "고양이 사진 찾아줘", "error": None}
    assert not path.exists() and not transport.transcribing


def test_voice_command_reports_missing_key_empty_text_and_failure_without_submitting():
    for key, result, expected in [(None, "x", "GROQ_API_KEY"), ("k", "", "알아듣지"),
                                  ("k", RuntimeError("boom"), "RuntimeError")]:
        transport, events, done = bridge(key)
        path = clip(f"{tr.PREFIX}bad.m4a")
        key_ = "side_effect" if isinstance(result, Exception) else "return_value"
        side = {key_: result}
        with patch("voice_action.native_bridge.transcribe", **side):
            transport.command({"action": "voice", "path": str(path)})
            assert done.wait(5)
        assert expected in events[-1]["error"] and events[-1]["text"] == ""
        assert not path.exists()
        transport.worker.submit.assert_not_called()


def test_transcribe_refuses_a_clip_swapped_for_a_symlink_after_validation(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    link = Path(tempfile.gettempdir()) / f"{tr.PREFIX}swap.m4a"
    link.unlink(missing_ok=True)
    link.symlink_to(secret)
    with patch("voice_action.transcribe.OpenAI") as client:
        try:
            tr.transcribe(link, "k")
            raise AssertionError("symlink was opened")
        except OSError:
            pass
    client.return_value.audio.transcriptions.create.assert_not_called()
    assert secret.exists() and not link.exists()


def test_cancel_during_transcription_discards_the_text():
    transport, events, done = bridge()
    path = clip(f"{tr.PREFIX}cancel.m4a")

    def slow(*_):
        transport.command({"action": "cancel"})
        return "지워줘"

    with patch("voice_action.native_bridge.transcribe", side_effect=slow):
        transport.command({"action": "voice", "path": str(path)})
        assert done.wait(5)
    assert events[-1]["text"] == "" and "취소" in events[-1]["error"]


def test_voice_command_is_refused_when_busy_or_clip_is_not_ours():
    transport, events, _ = bridge()
    transport.state = RuntimeState.WAITING_APPROVAL
    path = clip(f"{tr.PREFIX}busy.m4a")
    transport.command({"action": "voice", "path": str(path)})
    assert events[-1]["type"] == "rejected" and not path.exists()
    transport.state = RuntimeState.IDLE
    transport.command({"action": "voice", "path": "/etc/passwd"})
    assert events[-1]["type"] == "rejected"
    assert Path("/etc/passwd").exists()


def test_purge_stale_removes_only_old_voice_clips():
    import os
    old, fresh = clip(f"{tr.PREFIX}old.m4a"), clip(f"{tr.PREFIX}fresh.m4a")
    os.utime(old, (1, 1))
    tr.purge_stale()
    assert not old.exists() and fresh.exists()
    fresh.unlink()

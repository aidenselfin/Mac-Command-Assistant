"""The preview launcher must sign its bundle and reuse an unchanged running app."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("zsh") is None, reason="launcher uses zsh")
def test_preview_launch_repairs_signature_and_does_not_rebuild_unchanged_app(tmp_path):
    project = tmp_path / "preview with spaces"
    project.mkdir()
    runner = project / "run.sh"
    source = Path(__file__).resolve().parents[1] / "native-preview"
    shutil.copy2(source / "run.sh", runner)
    shutil.copy2(source / "Info.plist", project / "Info.plist")
    binary = project / ".build" / "debug"
    binary.mkdir(parents=True)
    (binary / "FinderChatPreview").write_text("first build")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls"
    valid = tmp_path / "valid-signature"
    tool = f"""#!{sys.executable}
import os
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
log = Path(os.environ['FAKE_LOG'])
valid = Path(os.environ['FAKE_VALID'])
if name == 'swift':
    if '--show-bin-path' in args:
        print(os.environ['FAKE_BIN'])
elif name == 'codesign':
    if '--force' in args:
        with log.open('a') as f:
            f.write('sign\\n')
        valid.touch()
        # Signing changes the executable; repeat launch must compare the build source.
        with Path(args[-1], 'Contents/MacOS/FinderChatPreview').open('a') as f:
            f.write(' signed')
    elif not valid.exists():
        sys.exit(1)
elif name == 'open':
    assert valid.exists(), 'opening an unsigned bundle'
    assert '-n' not in args, 'must not create another app instance'
    with log.open('a') as f:
        f.write('open\\n')
elif name == 'ps':
    pass
"""
    for name in ("swift", "codesign", "open", "ps"):
        command = commands / name
        command.write_text(tool)
        command.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "FAKE_BIN": str(binary),
        "FAKE_LOG": str(log),
        "FAKE_VALID": str(valid),
        "VOICE_ACTION_PYTHON": sys.executable,
    }

    def launch():
        subprocess.run(["zsh", str(runner)], cwd=tmp_path, env=env, check=True,
                       capture_output=True, text=True)

    launch()
    launch()
    assert log.read_text().splitlines() == ["sign", "open", "open"]
    (binary / "FinderChatPreview").write_text("second build")
    launch()
    assert log.read_text().splitlines()[-2:] == ["sign", "open"]
    valid.unlink()  # damaged signature must be repaired even with the same source hash
    launch()
    assert log.read_text().splitlines()[-2:] == ["sign", "open"]


def test_run_sh_refuses_to_launch_from_a_worktree(tmp_path):
    main = tmp_path / "repo"
    (main / "native-preview").mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[1] / "native-preview/run.sh",
                 main / "native-preview/run.sh")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(git + args, cwd=main, check=True)
    tree = tmp_path / "feature_a"
    subprocess.run(git + ["worktree", "add", "-q", str(tree)], cwd=main, check=True)
    result = subprocess.run(["zsh", str(tree / "native-preview/run.sh")],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "worktree에서는 앱을 실행하지 않습니다" in result.stderr

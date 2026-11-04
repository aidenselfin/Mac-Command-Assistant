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


@pytest.mark.skipif(not Path("/usr/libexec/PlistBuddy").exists(), reason="macOS PlistBuddy")
def test_worktree_build_gets_its_own_bundle_id_so_it_runs_beside_main(tmp_path):
    main = tmp_path / "repo"
    (main / "native-preview").mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "native-preview"
    for name in ("run.sh", "Info.plist"):
        shutil.copy2(source / name, main / "native-preview" / name)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(git + args, cwd=main, check=True)
    tree = tmp_path / "feature_a"
    subprocess.run(git + ["worktree", "add", "-q", str(tree)], cwd=main, check=True)
    commands = tmp_path / "commands"
    commands.mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "FinderChatPreview").write_text("build")
    # swift prints the bin path; codesign, open and ps just succeed.
    tool = (f"#!{sys.executable}\nimport sys\n"
            f"if '--show-bin-path' in sys.argv: print({str(binary)!r})\n")
    for name in ("swift", "codesign", "open", "ps"):
        (commands / name).write_text(tool)
        (commands / name).chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "VOICE_ACTION_PYTHON": sys.executable}

    def bundle(repo):
        (repo / "native-preview/.build").mkdir(exist_ok=True)
        subprocess.run(["zsh", str(repo / "native-preview" / "run.sh")], env=env, check=True,
                       capture_output=True, text=True)
        plist = repo / "native-preview/.build/FinderChatPreview.app/Contents/Info.plist"
        read = lambda key: subprocess.run(  # noqa: E731
            ["/usr/libexec/PlistBuddy", "-c", f"Print :{key}", str(plist)],
            capture_output=True, text=True).stdout.strip()
        return read("CFBundleIdentifier"), read("SymbioLabel")

    assert bundle(main) == ("local.voiceaction.finder-chat-preview", "")
    ident, label = bundle(tree)
    assert label == "feature_a"
    assert ident.startswith("local.voiceaction.finder-chat-preview.feature-a-")

"""Exercise ownership and recovery against a disposable repository, never the real repo."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/vibe.py"


def test_vibe_ownership_and_snapshots(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args]).decode().strip()

    def vibe(command, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, "--repo", str(tmp_path), *args],
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "code.txt").write_text("original")
    (tmp_path / ".gitignore").write_text("*.env\n")
    git("add", "code.txt", ".gitignore")
    git("commit", "-qm", "initial")
    head = git("rev-parse", "HEAD")
    (tmp_path / "user.txt").write_text("private")
    (tmp_path / "key.env").write_text("secret")
    for invalid in [".", "../outside", ".git/config", "key.env"]:
        assert vibe("begin", "--agent", "A", "--goal", "fix", "--files", invalid).returncode
    outside = tmp_path.parent / "outside-vibe"
    outside.write_text("outside")
    (tmp_path / "link").symlink_to(outside)
    assert vibe("begin", "--agent", "A", "--goal", "fix", "--files", "link").returncode

    def begin():
        return vibe(
            "begin",
            "--agent",
            "A",
            "--goal",
            "fix",
            "--files",
            "code.txt",
            "new.txt",
            "literal*.txt",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: begin(), range(2)))
    assert sorted(result.returncode for result in results) == [0, 1]
    token = next(result.stdout.strip() for result in results if result.returncode == 0)
    assert json.loads(vibe("status").stdout)["token"] == token
    assert git("show", "refs/vibe/snapshots:code.txt") == "original"
    (tmp_path / "code.txt").write_text("staged")
    git("add", "code.txt")
    index = (tmp_path / ".git/index").read_bytes()
    (tmp_path / "code.txt").write_text("working")
    (tmp_path / "new.txt").write_text("new")
    (tmp_path / "literal*.txt").write_text("literal")
    (tmp_path / "literal-private.txt").write_text("private")
    saved = vibe("snapshot")
    assert saved.returncode == 0, saved.stderr
    assert git("rev-parse", "HEAD") == head
    assert (tmp_path / ".git/index").read_bytes() == index
    ref = "refs/vibe/snapshots"
    assert git("show", f"{ref}:code.txt") == "working"
    assert git("show", f"{ref}:new.txt") == "new"
    assert "user.txt" not in git("ls-tree", "--name-only", ref)
    assert "key.env" not in git("ls-tree", "--name-only", ref)
    assert "literal-private.txt" not in git("ls-tree", "--name-only", ref)
    assert git("show", f"{ref}:literal*.txt") == "literal"
    assert vibe("snapshot").stdout == saved.stdout
    (tmp_path / "code.txt").unlink()
    assert vibe("snapshot").returncode == 0
    assert "code.txt" not in git("ls-tree", "--name-only", ref)
    assert git("show", f"{saved.stdout.strip()}:code.txt") == "working"
    assert vibe("end", "--token", "wrong").returncode == 1
    assert vibe("end", "--token", token).returncode == 0
    assert json.loads(vibe("status").stdout) == {}
    assert vibe("end", "--token", token).returncode == 1

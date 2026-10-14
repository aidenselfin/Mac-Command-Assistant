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


def test_linked_worktrees_have_independent_sessions(tmp_path):
    main = tmp_path / "app"
    main.mkdir()

    def git(*args, cwd=main):
        return subprocess.check_output(["git", "-C", str(cwd), *args]).decode().strip()

    def vibe(command, *args, cwd=main):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, "--repo", str(cwd), *args],
            capture_output=True,
            text=True,
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (main / "code.txt").write_text("original")
    (main / "config.json").write_text("{}")  # untracked local file a worktree must see
    (main / ".gitignore").write_text("config.json\n")
    git("add", "code.txt", ".gitignore")
    git("commit", "-qm", "initial")

    made = vibe("worktree", "--name", "feature", "--base", "HEAD")
    assert made.returncode == 0, made.stderr
    other = tmp_path / "app-worktrees" / "feature"
    assert (other / "config.json").is_symlink()

    first = vibe("begin", "--agent", "A", "--goal", "one", "--files", "code.txt")
    assert first.returncode == 0, first.stderr
    # A session in the main checkout does not block one in the worktree ...
    second = vibe("begin", "--agent", "B", "--goal", "two", "--files", "code.txt", cwd=other)
    assert second.returncode == 0, second.stderr
    # ... but the shared file is reported, and each checkout keeps its own snapshot ref.
    assert "경고" in second.stderr
    assert "\tA\tone" in vibe("sessions", cwd=other).stdout
    third = vibe("begin", "--agent", "C", "--goal", "three", "--files", "code.txt", cwd=other)
    assert third.returncode == 1  # one writer per checkout
    (other / "code.txt").write_text("changed in worktree")
    assert vibe("snapshot", cwd=other).returncode == 0
    assert git("show", "refs/vibe/snapshots-feature:code.txt") == "changed in worktree"
    assert git("show", "refs/vibe/snapshots:code.txt") == "original"
    assert vibe("end", "--token", second.stdout.strip(), cwd=other).returncode == 0
    assert vibe("end", "--token", first.stdout.strip()).returncode == 0

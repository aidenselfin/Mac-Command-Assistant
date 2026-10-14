#!/usr/bin/env python3
"""Cooperative writer ownership and snapshots without touching HEAD or the real index."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

REF = "refs/vibe/snapshots"
SESSION = "vibe-session.json"


def ref_for(git_dir):
    """Each linked worktree keeps its own snapshots so parallel sessions never race on one ref."""
    return f"{REF}-{git_dir.name}" if git_dir.parent.name == "worktrees" else REF


def common_repo(repo):
    """The main checkout, also when called from a linked worktree."""
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).parent


def other_sessions(repo, git_dir):
    """(worktree path, session) of every other checkout that has an open session."""
    found = []
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):])
        try:
            other = Path(git(path, "rev-parse", "--absolute-git-dir"))
            if other != git_dir and (other / SESSION).exists():
                found.append((path, json.loads((other / SESSION).read_text())))
        except (subprocess.CalledProcessError, OSError, ValueError):
            continue  # a removed or broken worktree has no session to report
    return found


def git(repo, *args, env=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], env=env).decode().strip()


def snapshot(repo, git_dir, session):
    ref = ref_for(git_dir)
    # ponytail: periodic snapshots can span an edit; use begin/end for task boundaries.
    with tempfile.TemporaryDirectory(dir=git_dir) as temporary:
        env = dict(
            os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"), GIT_LITERAL_PATHSPECS="1"
        )
        git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "-u", env=env)
        for name in session.get("files", []):
            path = repo / name
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(repo):
                git(repo, "add", "--", name, env=env)
        tree = git(repo, "write-tree", env=env)
        previous = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", ref],
            capture_output=True,
            text=True,
        )
        parent = (
            previous.stdout.strip() if previous.returncode == 0 else git(repo, "rev-parse", "HEAD")
        )
        if previous.returncode == 0 and tree == git(repo, "rev-parse", f"{parent}^{{tree}}"):
            return parent
        label = session.get("token", "idle")
        commit = git(repo, "commit-tree", tree, "-p", parent, "-m", f"vibe snapshot: {label}")
        git(repo, "update-ref", ref, commit, previous.stdout.strip() or "0" * 40)
        return commit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["begin", "end", "status", "snapshot", "sessions", "worktree"]
    )
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--agent", default="")
    parser.add_argument("--goal", default="")
    parser.add_argument("--files", nargs="+", default=[])
    parser.add_argument("--token", default="")
    parser.add_argument("--name", default="", help="worktree 이름")
    parser.add_argument("--base", default="main", help="worktree를 만들 기준 브랜치")
    args = parser.parse_args()
    repo = Path(git(args.repo, "rev-parse", "--show-toplevel"))
    git_dir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    session_path = git_dir / SESSION
    if args.command == "worktree":
        if not args.name or "/" in args.name or args.name.startswith("."):
            parser.error("worktree에는 --name(경로 구분자 없는 이름)이 필요합니다")
        main = common_repo(repo)
        path = main.parent / f"{main.name}-worktrees" / args.name
        git(main, "worktree", "add", "-b", f"work/{args.name}", str(path), args.base)
        # Untracked local files a session needs; linked, so keys and the big tool tree stay single.
        for name in ("config.json", ".voice-action/config.env", "node_modules"):
            if (main / name).exists() and not (path / name).exists():
                (path / name).parent.mkdir(parents=True, exist_ok=True)
                (path / name).symlink_to(main / name)
        print(path)
        print("다음: cd 위 경로 && uv sync --locked  (.venv는 worktree마다 따로 만든다)")
        return
    # All participating sessions and the scheduler serialize metadata/snapshot updates.
    with (git_dir / "vibe.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session = json.loads(session_path.read_text()) if session_path.exists() else {}
        if args.command == "status":
            print(json.dumps(session, ensure_ascii=False, indent=2))
        elif args.command == "sessions":
            for path, other in other_sessions(repo, git_dir):
                print(f"{path}\t{other['agent']}\t{other['goal']}")
        elif args.command == "begin":
            if session:
                parser.exit(1, "다른 수정 세션이 있습니다. vibe.py status로 확인하세요.\n")
            if not args.agent or not args.goal or not args.files:
                parser.error("begin에는 --agent, --goal, --files가 필요합니다")
            for name in args.files:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    parser.error("--files에는 저장소 안의 개별 상대 파일 경로만 지정하세요")
                if path.parts[0] == ".git" or (repo / path).is_dir():
                    parser.error("Git 내부 경로와 디렉터리는 지정할 수 없습니다")
                if not (repo / path).resolve().is_relative_to(repo):
                    parser.error("저장소 밖으로 연결된 경로는 지정할 수 없습니다")
                ignored = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "check-ignore",
                        "-q",
                        "--",
                        name,
                    ]
                )
                if ignored.returncode == 0:
                    parser.error("gitignore 대상은 보존 파일로 지정할 수 없습니다")
                if ignored.returncode != 1:
                    parser.error("gitignore 확인에 실패했습니다")
            session = dict(
                token=uuid4().hex,
                agent=args.agent,
                goal=args.goal,
                started=datetime.now().astimezone().isoformat(),
                files=args.files,
            )
            snapshot(repo, git_dir, session)
            for path, other in other_sessions(repo, git_dir):
                shared = sorted(set(args.files) & set(other["files"]))
                if shared:
                    print(
                        f"경고: {path}의 {other['agent']} 세션도 {', '.join(shared)}을(를) "
                        "수정합니다. 통합 때 충돌하니 먼저 조율하세요.",
                        file=sys.stderr,
                    )
            session_path.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n")
            print(session["token"])
        elif args.command == "end":
            if not session or args.token != session["token"]:
                parser.exit(1, "소유 세션의 --token이 필요합니다. 잠금을 임의로 해제하지 마세요.\n")
            print(snapshot(repo, git_dir, session))
            session_path.unlink()
        else:
            print(snapshot(repo, git_dir, session))


if __name__ == "__main__":
    main()

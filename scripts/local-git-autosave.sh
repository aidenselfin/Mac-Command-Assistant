#!/bin/sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export GIT_TERMINAL_PROMPT=0

# Leave manual staging and in-progress Git operations to the user.
if ! git -C "$repo_dir" diff --cached --quiet; then
    exit 0
fi
if [ -e "$repo_dir/.git/MERGE_HEAD" ] || [ -d "$repo_dir/.git/rebase-merge" ] || [ -d "$repo_dir/.git/rebase-apply" ]; then
    exit 0
fi
if [ -e "$repo_dir/.git/index.lock" ]; then
    exit 0
fi
if [ -z "$(git -C "$repo_dir" status --porcelain --untracked-files=normal)" ]; then
    exit 0
fi

git -C "$repo_dir" add -A
if git -C "$repo_dir" diff --cached --quiet; then
    exit 0
fi
git -C "$repo_dir" commit -m "chore: local autosave"

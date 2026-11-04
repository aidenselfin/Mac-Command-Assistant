#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
# Only main's app runs; extra worktree apps made the machine run hot.
if [[ "$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-dir)" != \
      "$(git -C "$PROJECT_DIR" rev-parse --path-format=absolute --git-common-dir)" ]]; then
    print -u2 "worktree에서는 앱을 실행하지 않습니다. main에 병합한 뒤 메인 폴더에서 실행하세요."
    exit 1
fi
swift build --package-path "$PROJECT_DIR"
BIN_DIR=$(swift build --package-path "$PROJECT_DIR" --show-bin-path)
REPO_DIR="${PROJECT_DIR:h}"
PYTHON_BIN="${VOICE_ACTION_PYTHON:-$REPO_DIR/.venv/bin/python}"
COMMON_GIT_DIR=$(git -C "$REPO_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="${COMMON_GIT_DIR:h}/.venv/bin/python"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
    print -u2 "Python 환경이 없습니다. 프로젝트에서 uv sync --locked를 실행하세요."
    exit 1
fi
RUNTIME_FILE="$PROJECT_DIR/.build/runtime.json"
"$PYTHON_BIN" - "$REPO_DIR" "$PYTHON_BIN" > "$RUNTIME_FILE" <<'PYTHON'
import json
import sys
print(json.dumps({"project": sys.argv[1], "python": sys.argv[2]}))
PYTHON
APP_DIR="$PROJECT_DIR/.build/FinderChatPreview.app"
EXECUTABLE="$APP_DIR/Contents/MacOS/FinderChatPreview"
BACKEND_FILES=("$REPO_DIR"/voice_action/*.py(N))
BUILD_HASH=$(shasum -a 256 "$BIN_DIR/FinderChatPreview" "$PROJECT_DIR/Info.plist" \
    "$RUNTIME_FILE" "${BACKEND_FILES[@]}")
HASH_FILE="$PROJECT_DIR/.build/preview-source.sha256"
if [[ "$BUILD_HASH" != "$(cat "$HASH_FILE" 2>/dev/null || true)" ]] || \
   ! codesign --verify --deep --strict "$APP_DIR" >/dev/null 2>&1; then
    # Stop the running preview before replacing its signed app bundle.
    while read -r process_pid process_command; do
        if [[ "$process_command" == "$EXECUTABLE" ]]; then
            child_pids=("${(@f)$(pgrep -P "$process_pid" || true)}")
            kill -TERM "$process_pid" 2>/dev/null || true
            for cleanup_pid in "$process_pid" "${child_pids[@]}"; do
                [[ -z "$cleanup_pid" ]] && continue
                for attempt in {1..100}; do
                    kill -0 "$cleanup_pid" 2>/dev/null || break
                    sleep 0.1
                done
                if kill -0 "$cleanup_pid" 2>/dev/null; then
                    print -u2 "이전 앱·백엔드가 정리 중입니다. 종료 후 다시 실행하세요."
                    exit 1
                fi
            done
        fi
    done < <(ps -axo pid=,comm=)
    mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
    cp "$BIN_DIR/FinderChatPreview" "$EXECUTABLE.new"
    mv -f "$EXECUTABLE.new" "$EXECUTABLE"
    cp "$PROJECT_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
    cp "$RUNTIME_FILE" "$APP_DIR/Contents/Resources/runtime.json"
    codesign --force --sign - "$APP_DIR"
    printf '%s\n' "$BUILD_HASH" > "$HASH_FILE"
fi
codesign --verify --deep --strict "$APP_DIR"
if [[ -n "${VOICE_ACTION_BACKGROUND:-}" ]]; then
    open "$APP_DIR" --args --background  # login start: menu bar only, window stays hidden
else
    open "$APP_DIR"
fi

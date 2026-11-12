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
# The backend the app starts: symbio-bridge (Swift, JSON lines). `symbio` lands next to it.
BACKEND_DIR="$REPO_DIR/backend/App"
swift build --package-path "$BACKEND_DIR" -c release
BACKEND="$(swift build --package-path "$BACKEND_DIR" -c release --show-bin-path)/symbio-bridge"
RUNTIME_FILE="$PROJECT_DIR/.build/runtime.json"
# Which native-preview code this build holds; the backend warns when the checkout has moved past it.
APP_TREE=$(git -C "$REPO_DIR" rev-parse HEAD:native-preview 2>/dev/null || true)
if [[ -n "$(git -C "$REPO_DIR" status --porcelain -- native-preview 2>/dev/null || true)" ]]; then
    APP_TREE="$APP_TREE-dirty"
fi
json_string() {
    local value=${1//\\/\\\\}
    print -rn -- "\"${value//\"/\\\"}\""
}
print -r -- "{\"project\": $(json_string "$REPO_DIR"), \"backend\": $(json_string "$BACKEND"), \"app_tree\": $(json_string "$APP_TREE")}" > "$RUNTIME_FILE"
APP_DIR="$PROJECT_DIR/.build/FinderChatPreview.app"
EXECUTABLE="$APP_DIR/Contents/MacOS/FinderChatPreview"
BACKEND_FILES=("$REPO_DIR"/backend/**/*.swift(N))
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

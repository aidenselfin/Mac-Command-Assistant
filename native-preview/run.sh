#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
swift build --package-path "$PROJECT_DIR"
BIN_DIR=$(swift build --package-path "$PROJECT_DIR" --show-bin-path)
APP_DIR="$PROJECT_DIR/.build/FinderChatPreview.app"
EXECUTABLE="$APP_DIR/Contents/MacOS/FinderChatPreview"
BUILD_HASH=$(shasum -a 256 "$BIN_DIR/FinderChatPreview" "$PROJECT_DIR/Info.plist")
HASH_FILE="$PROJECT_DIR/.build/preview-source.sha256"
if [[ "$BUILD_HASH" != "$(cat "$HASH_FILE" 2>/dev/null || true)" ]] || \
   ! codesign --verify --deep --strict "$APP_DIR" >/dev/null 2>&1; then
    # Stop only this worktree's preview before replacing its signed app bundle.
    while read -r process_pid process_command; do
        if [[ "$process_command" == "$EXECUTABLE" ]]; then
            kill -TERM "$process_pid" 2>/dev/null || true
        fi
    done < <(ps -axo pid=,comm=)
    mkdir -p "$APP_DIR/Contents/MacOS"
    cp "$BIN_DIR/FinderChatPreview" "$EXECUTABLE.new"
    mv -f "$EXECUTABLE.new" "$EXECUTABLE"
    cp "$PROJECT_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
    codesign --force --sign - "$APP_DIR"
    printf '%s\n' "$BUILD_HASH" > "$HASH_FILE"
fi
codesign --verify --deep --strict "$APP_DIR"
open "$APP_DIR"

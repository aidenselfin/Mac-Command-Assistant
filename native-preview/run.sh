#!/bin/zsh
set -eu
PROJECT_DIR="${0:A:h}"
swift build --package-path "$PROJECT_DIR"
BIN_DIR=$(swift build --package-path "$PROJECT_DIR" --show-bin-path)
APP_DIR="$PROJECT_DIR/.build/FinderChatPreview.app"
mkdir -p "$APP_DIR/Contents/MacOS"
cp "$BIN_DIR/FinderChatPreview" "$APP_DIR/Contents/MacOS/FinderChatPreview"
cp "$PROJECT_DIR/Info.plist" "$APP_DIR/Contents/Info.plist"
open -n "$APP_DIR"

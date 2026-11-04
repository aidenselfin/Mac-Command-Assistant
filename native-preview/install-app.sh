#!/bin/zsh
# main 폴더에서 빌드된 앱을 ~/Applications/Symbio.app 으로 복사하고 실행한다.
# 앱이 꺼졌을 때 Spotlight·Launchpad·Finder에서 다시 열 수 있다. main에 병합할 때마다 다시 실행한다.
set -eu
HERE="${0:A:h}"
COMMON_GIT_DIR=$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)
MAIN_DIR="${COMMON_GIT_DIR:h}"
SRC="$MAIN_DIR/native-preview/.build/FinderChatPreview.app"
DEST="$HOME/Applications/Symbio.app"
VOICE_ACTION_BACKGROUND=1 "$MAIN_DIR/native-preview/run.sh"  # 늘 최신 코드로 빌드한다(옛 빌드 복사 방지)
pkill -f "$SRC/Contents/MacOS/FinderChatPreview" 2>/dev/null || true
pkill -f "$DEST/Contents/MacOS/FinderChatPreview" 2>/dev/null || true
sleep 1
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
ditto "$SRC" "$DEST"
/usr/libexec/PlistBuddy -c "Set :CFBundleName Symbio" "$DEST/Contents/Info.plist"  # main 빌드가 옛 이름이어도 맞춘다
codesign --force --sign - "$DEST"
open "$DEST"
print "설치됨: $DEST"

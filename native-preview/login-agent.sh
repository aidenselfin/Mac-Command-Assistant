#!/bin/zsh
# 로그인할 때 main 폴더의 최신 코드를 빌드해 메뉴 막대 앱으로 띄우는 LaunchAgent를 설치/제거한다.
#   ./native-preview/login-agent.sh install    (어느 worktree에서 실행해도 main 폴더를 가리킨다)
#   ./native-preview/login-agent.sh uninstall
set -eu
LABEL="com.aidenselfin.voice-action-ai.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERE="${0:A:h}"
COMMON_GIT_DIR=$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)
MAIN_DIR="${COMMON_GIT_DIR:h}"
case "${1:-}" in
install)
    [[ -x "$MAIN_DIR/native-preview/run.sh" ]] || { print -u2 "main 폴더에 run.sh가 없습니다: $MAIN_DIR"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$LABEL</string>
<key>ProgramArguments</key><array><string>/bin/zsh</string><string>$MAIN_DIR/native-preview/run.sh</string></array>
<key>EnvironmentVariables</key><dict>
<key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
<key>VOICE_ACTION_BACKGROUND</key><string>1</string>
</dict>
<key>RunAtLoad</key><true/>
<key>StandardErrorPath</key><string>/tmp/$LABEL.log</string>
<key>StandardOutPath</key><string>/tmp/$LABEL.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    print "설치됨: 로그인 때마다 $MAIN_DIR 를 빌드해 실행합니다."
    ;;
uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    print "제거됨"
    ;;
*) print -u2 "사용법: $0 install|uninstall"; exit 2 ;;
esac

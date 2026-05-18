#!/usr/bin/env bash
# install.sh — Smart-Homerow 터미널 명령어 설치
# 사용: bash install.sh [--uninstall]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD_NAME="smart-homerow"
INSTALL_BIN="/usr/local/bin/$CMD_NAME"
PLIST_SRC="$DIR/com.smartHomerow.daemon.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.smartHomerow.daemon.plist"

# ── 색상 ─────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 제거 모드 ──────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  info "Smart-Homerow 제거 중..."

  if launchctl list com.smartHomerow.daemon &>/dev/null; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    info "LaunchAgent 언로드 완료"
  fi
  [[ -f "$PLIST_DST" ]] && rm -f "$PLIST_DST" && info "plist 제거: $PLIST_DST"
  [[ -f "$INSTALL_BIN" ]] && sudo rm -f "$INSTALL_BIN" && info "명령어 제거: $INSTALL_BIN"

  info "제거 완료. 가상환경(.venv)·DB는 $DIR 에 그대로 남아 있습니다."
  exit 0
fi

# ── Python 선택 (Apple CLT 제외) ──────────────────────────────────
is_bad_python() {
  case "$1" in *CommandLineTools*|"/usr/bin/python3") return 0;; esac; return 1
}
pick_python() {
  local p
  for p in "${PYTHON:-}" \
    "/opt/homebrew/bin/python3" "/usr/local/bin/python3" \
    "$(command -v python3.13 2>/dev/null||true)" \
    "$(command -v python3.12 2>/dev/null||true)" \
    "$(command -v python3.11 2>/dev/null||true)" \
    "$(command -v python3 2>/dev/null||true)"; do
    [[ -z "$p" || ! -x "$p" ]] && continue
    is_bad_python "$p" && continue
    printf '%s' "$p"; return 0
  done; return 1
}

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  error "Homebrew Python이 필요합니다. 아래 명령으로 설치 후 다시 시도하세요:"
  error "  brew install python@3.12"
  exit 1
fi
info "사용할 Python: $PY"

# ── 가상환경 + 의존성 ──────────────────────────────────────────────
VENV="$DIR/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  info "가상환경 생성: $VENV"
  "$PY" -m venv "$VENV"
fi
info "의존성 설치 중..."
"$VENV/bin/python" -m pip install -U pip setuptools wheel -q
"$VENV/bin/python" -m pip install -r "$DIR/requirements.txt" -q
info "의존성 설치 완료"

# ── /usr/local/bin/smart-homerow 래퍼 생성 ────────────────────────
info "명령어 등록 중: $INSTALL_BIN"
sudo tee "$INSTALL_BIN" > /dev/null <<WRAPPER
#!/usr/bin/env bash
# smart-homerow — Auto-generated launcher
# 수정하지 마세요. install.sh 를 다시 실행하면 재생성됩니다.
set -euo pipefail

APP_DIR="$DIR"
VENV="$DIR/.venv"
CMD="\${1:-start}"

case "\$CMD" in
  start)
    shift || true
    echo "[smart-homerow] 시작..."
    exec "\$VENV/bin/python" "\$APP_DIR/main.py" "\$@"
    ;;
  stop)
    PID=\$(pgrep -f "\$APP_DIR/main.py" | head -1 || true)
    if [[ -n "\$PID" ]]; then
      kill "\$PID" && echo "[smart-homerow] 종료 (PID \$PID)"
    else
      echo "[smart-homerow] 실행 중인 프로세스 없음"
    fi
    ;;
  status)
    PID=\$(pgrep -f "\$APP_DIR/main.py" | head -1 || true)
    if [[ -n "\$PID" ]]; then
      echo "[smart-homerow] 실행 중 (PID \$PID)"
    else
      echo "[smart-homerow] 정지됨"
    fi
    ;;
  restart)
    "\$0" stop; sleep 1; "\$0" start "\${@:2}"
    ;;
  log)
    tail -f "\$APP_DIR/logs/smartHomerow.log" 2>/dev/null || echo "로그 파일 없음"
    ;;
  perms)
    exec "\$VENV/bin/python" "\$APP_DIR/main.py" --open-permissions-only
    ;;
  debug)
    shift || true
    exec "\$VENV/bin/python" "\$APP_DIR/main.py" --debug "\$@"
    ;;
  -h|--help|help)
    echo "사용법: smart-homerow <명령어> [옵션]"
    echo ""
    echo "  start      앱 실행 (기본값)"
    echo "  stop       실행 중인 앱 종료"
    echo "  restart    재시작"
    echo "  status     실행 상태 확인"
    echo "  log        로그 실시간 확인 (tail -f)"
    echo "  perms      손쉬운 사용 권한 설정 열기"
    echo "  debug      디버그 모드로 실행"
    echo ""
    echo "main.py 옵션 그대로 전달 가능:"
    echo "  smart-homerow start --no-overlay --no-logger"
    ;;
  *)
    # 알 수 없는 서브커맨드는 main.py 에 그대로 전달
    exec "\$VENV/bin/python" "\$APP_DIR/main.py" "\$@"
    ;;
esac
WRAPPER

sudo chmod +x "$INSTALL_BIN"
info "명령어 등록 완료: $INSTALL_BIN"

# ── LaunchAgent (자동 시작) 선택 설치 ────────────────────────────
echo ""
read -r -p "$(echo -e "${YELLOW}로그인 시 자동 시작(LaunchAgent)을 등록할까요? [y/N]${NC} ")" REGISTER_LAUNCH

if [[ "${REGISTER_LAUNCH,,}" == "y" ]]; then
  mkdir -p "$HOME/Library/LaunchAgents"

  # plist에서 INSTALL_PATH 치환
  sed "s|INSTALL_PATH|$DIR|g" "$PLIST_SRC" > "$PLIST_DST"

  # ProgramArguments를 venv python으로 교체
  /usr/bin/python3 - <<PY
import plistlib, pathlib
p = pathlib.Path("$PLIST_DST")
d = plistlib.loads(p.read_bytes())
d["ProgramArguments"] = ["$VENV/bin/python", "$DIR/main.py"]
p.write_bytes(plistlib.dumps(d))
PY

  launchctl load "$PLIST_DST"
  info "LaunchAgent 등록 완료 → 다음 로그인부터 자동 시작"
  info "수동 제어: launchctl start|stop com.smartHomerow.daemon"
else
  info "자동 시작 등록 건너뜀 (나중에 install.sh 다시 실행하면 등록 가능)"
fi

# ── 권한 확인 ─────────────────────────────────────────────────────
echo ""
info "손쉬운 사용 권한을 확인합니다..."
"$VENV/bin/python" "$DIR/macos_permissions.py" 2>/dev/null || warn "권한 확인 실패 — 'smart-homerow perms' 로 수동 설정하세요"

# ── 완료 안내 ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Smart-Homerow 설치 완료!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  smart-homerow start      # 앱 실행"
echo "  smart-homerow stop       # 종료"
echo "  smart-homerow status     # 상태 확인"
echo "  smart-homerow log        # 로그 보기"
echo "  smart-homerow --help     # 전체 도움말"
echo ""
echo -e "  설치 위치: ${YELLOW}$DIR${NC}"
echo -e "  명령어:    ${YELLOW}$INSTALL_BIN${NC}"
echo ""

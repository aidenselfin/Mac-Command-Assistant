#!/usr/bin/env bash
# Smart-Homerow: Apple Command Line Tools python3는 PyObjC 설치가 어렵습니다.
# Homebrew / conda / python.org 등의 Python으로 .venv를 만들고 실행합니다.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

is_bad_python() {
  case "$1" in
    *CommandLineTools*|"/usr/bin/python3") return 0 ;;
  esac
  return 1
}

pick_python() {
  local p
  # PATH의 python3는 Apple CLT일 수 있으므로 Homebrew 경로를 먼저 본다.
  for p in \
    "${PYTHON:-}" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "$(command -v python3.13 2>/dev/null || true)" \
    "$(command -v python3.12 2>/dev/null || true)" \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    [[ -z "$p" || ! -x "$p" ]] && continue
    is_bad_python "$p" && continue
    printf '%s' "$p"
    return 0
  done
  return 1
}

PY="$(pick_python || true)"
if [[ -z "$PY" ]]; then
  echo "[ERROR] Command Line Tools 전용 python 말고 다른 Python이 필요합니다." >&2
  echo "        예: brew install python@3.12  후 다시 이 스크립트 실행" >&2
  echo "        또는 conda 환경을 켠 뒤:  export PYTHON=\$(which python); ./run_macos.sh" >&2
  exit 1
fi

echo "[INFO] 사용할 Python: $PY"
VENV="${VENV:-$DIR/.venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[INFO] 가상환경 생성: $VENV"
  "$PY" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install -U pip setuptools wheel >/dev/null
"$VENV/bin/python" -m pip install -r "$DIR/requirements.txt"

exec "$VENV/bin/python" "$DIR/main.py" "$@"

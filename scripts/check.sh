#!/bin/sh
# 기능 단위 점검: scripts/check.sh quick | full | <기능> (기능 이름은 docs/features.md 참고)
set -eu
cd "$(dirname "$0")/.."
DEV="swift run --package-path backend/Dev symbio-dev"
t() { for m in "$@"; do swift test --package-path "backend/$m"; done; }
case "${1:-quick}" in
  full) $DEV check ;;
  quick)
    # 바뀐 파일(커밋 안 된 것 + 마지막 커밋 이후)에 맞는 패키지만. 모르겠으면 full을 쓴다.
    changed=$(git status --porcelain | awk '{print $2}')
    modules=$(printf '%s\n' $changed | sed -n 's|^backend/\([^/]*\)/.*|\1|p' | sort -u)
    case "$changed" in *native-preview*) swift build --package-path native-preview ;; esac
    if [ -z "$modules" ]; then
      case "$changed" in *native-preview*) exit 0 ;; esac
      echo "영역을 알 수 없어 전체를 실행합니다."; exec "$0" full
    fi
    t $modules ;;
  search|파일찾기) t Search ;;
  fileops|파일정리) t FileOps Undo ;;
  delete|삭제) t Delete ;;
  convert|변환) t Convert ;;
  write|쓰기) t Guard Undo ;;
  ui|finder) t Finder ;;
  native|네이티브) swift build --package-path native-preview ;;
  feedback|피드백) t Feedback ;;
  planner|계획) t Planner Runtime ;;
  eval|평가) $DEV eval ;;
  bench|점수) $DEV benchmark --no-save ;;
  *) echo "사용법: $0 quick|full|search|fileops|delete|convert|write|ui|native|feedback|planner|eval|bench"; exit 2 ;;
esac

#!/bin/zsh
# Finder 실행에서도 uv와 Node의 기본 설치 경로를 찾습니다.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
cd -- "${0:A:h}" || exit 1

uv run python main.py --ui
status_code=$?
if (( status_code != 0 )); then
    printf '\n실행 실패 (종료 코드 %s). 위 오류를 확인하세요.\n' "$status_code"
    read -r '?Enter를 누르면 종료합니다.'
fi
exit "$status_code"

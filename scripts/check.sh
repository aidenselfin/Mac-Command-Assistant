#!/bin/sh
# 기능 단위 점검: scripts/check.sh quick | full | <기능> (기능 이름은 docs/features.md 참고)
set -eu
cd "$(dirname "$0")/.."
RUN="uv run --locked"
case "${1:-quick}" in
  full) $RUN pytest -q && $RUN ruff check . ;;
  quick)
    # 바뀐 파일(커밋 안 된 것 + 마지막 커밋 이후)에 맞는 영역만. 모르겠으면 full을 쓴다.
    changed=$(git status --porcelain | awk '{print $2}')
    tests=""
    case "$changed" in *file_search*|*semantic_index*) tests="$tests tests/test_file_search.py tests/test_find_files_flow.py tests/test_semantic_index.py";; esac
    case "$changed" in *file_operations*|*undo.py*) tests="$tests tests/test_file_operations.py tests/test_undo.py";; esac
    case "$changed" in *deletion*) tests="$tests tests/test_deletion.py";; esac
    case "$changed" in *conversion*) tests="$tests tests/test_conversion.py";; esac
    case "$changed" in *native*|*App.swift*|*Backend.swift*) tests="$tests tests/test_native_bridge.py tests/test_native_protocol.py tests/test_native_preview.py";; esac
    case "$changed" in *voice_action/ui.py*|*finder.py*) tests="$tests tests/test_ui.py tests/test_finder.py";; esac
    case "$changed" in *planner.py*|*risk_guard.py*|*runtime.py*) tests="$tests tests/test_planner.py tests/test_risk_guard.py tests/test_runtime.py tests/test_file_operations.py";; esac
    case "$changed" in *vibe.py*) tests="$tests tests/test_vibe.py";; esac
    if [ -z "$tests" ]; then echo "영역을 알 수 없어 전체를 실행합니다."; exec "$0" full; fi
    $RUN pytest -q $tests tests/test_integration_features.py && $RUN ruff check . ;;
  search|파일찾기) $RUN pytest -q tests/test_file_search.py tests/test_find_files_flow.py ;;
  semantic|의미검색) $RUN pytest -q tests/test_semantic_index.py tests/test_integration_features.py ;;
  fileops|파일정리) $RUN pytest -q tests/test_file_operations.py tests/test_undo.py ;;
  delete|삭제) $RUN pytest -q tests/test_deletion.py ;;
  convert|변환) $RUN pytest -q tests/test_conversion.py tests/test_conversion_review.py ;;
  write|쓰기) $RUN pytest -q tests/test_risk_guard.py tests/test_integration_undo.py ;;
  ui|tk) $RUN pytest -q tests/test_ui.py tests/test_ui_review.py tests/test_finder.py ;;
  native|네이티브) $RUN pytest -q tests/test_native_bridge.py tests/test_native_protocol.py tests/test_native_preview.py && swift build --package-path native-preview ;;
  feedback|피드백) $RUN pytest -q tests/test_feedback.py tests/test_feedback_review.py tests/test_lesson_candidates.py ;;
  eval|평가) $RUN python evals/run_eval.py ;;
  *) echo "사용법: $0 quick|full|search|semantic|fileops|delete|convert|write|ui|native|feedback|eval"; exit 2 ;;
esac

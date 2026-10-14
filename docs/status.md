# 진행 중
- (없음)

# 현재 목표
SwiftUI + 기존 Python 백엔드를 codex/finder-chat-panel worktree에서 시험한다. main 미병합.

# 최근 검증
- 최종 전체 pytest 668 passed, ruff 전체·Swift 빌드·plist·zsh·diff check 통과.
- 네이티브 통신/launcher 회귀 11 passed, CLI/UI/Runtime 회귀 115 passed.
- 실제 --interactive 연결/exit, 네이티브 앱 연결/창 닫기/백엔드 종료/재실행 확인.
- 실제 모델로 native-ui-smoke.txt 승인 대기→거절→재요청→승인→내용 확인→Undo 제거 확인.
  유료 메인 모델·자동 피드백 분석을 포함한 소규모 smoke이며 전체 live 평가는 아니다.
- 승인 ID 재사용·잘못된 입력·허용 밖 첨부 차단, 질문 답변→실제 MCP 쓰기→Undo 회귀 확인.
- 실행 오류 162는 앱 전체 ad-hoc 서명으로 수정했다. strict 검사·반복 실행 통과.

# 다음 작업
- 사람이 검색/요약/선택/첨부/평가/한글 입력을 시험하고 main 병합 여부를 결정한다.
- 네이티브 Finder 자동 배치·독립 배포 패키징은 아직 구현하지 않았다.

# 인수인계
- native-preview/run.sh로 실행한다. 기존 Runtime/Planner/RiskGuard/Undo/피드백을 재사용한다.
- 승인 context는 Python에만 보관하고 UI 승인 ID는 한 번만 사용한다.
- 창 닫기와 코드 갱신은 기존 백엔드 정리를 기다린다. 실행 취소 후 메뉴에서 재연결한다.
- worktree 로컬 설정은 voice-action-test만 허용한다. 원본 코드/자료는 변경하지 않았다.
- 실제 앱 smoke DB는 worktree .voice-action에 기록했고 회귀 테스트 DB는 격리했다.
- 실제 드래그·한글 IME·네이티브 100개 삭제 목록은 사람이 확인하지 않았다.

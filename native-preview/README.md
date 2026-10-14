# 네이티브 macOS 대화 앱 (worktree 시험용)

SwiftUI 시스템 화면과 기존 Python Runtime을 연결한 360×560 앱이다.
macOS 15 이상과 Swift 6 도구, 설치된 Python·Node·Chromium 의존성이 필요하다.

```sh
./native-preview/run.sh
```

앱은 현재 프로젝트의 설정·API 키를 읽는다. 파일 접근 범위는 `config.json`의
`allowed_directories`다. worktree 로컬 설정은 worktree의 `voice-action-test`만 허용한다.
Python은 이 폴더의 `.venv`를 먼저 쓰고, 없으면 같은 저장소 원본의 `.venv`를 읽어 쓴다.
`VOICE_ACTION_PYTHON=/절대경로/python`으로 지정할 수도 있다. 원본 코드는 실행하지 않는다.
이 앱은 아직 Python·Node를 포함한 독립 배포 패키지는 아니다.

## 지원 동작
- 실제 명령·질문 답변·파일 후보 선택, Markdown 기본 표시
- 호출마다 승인·거절, 삭제 대상 전체 목록, 내용·대상 변경 시 재승인
- 파일 첨부(버튼 또는 한 파일 드롭), 1~5 평가와 낮은 점수의 이유
- `…` 메뉴의 Undo, 연결 종료 후 다시 시작
- 성공한 쓰기·변환 결과를 Finder에서 표시 (허용 경로·링크를 다시 검사)

실행 중 취소는 기존 Runtime과 같이 연결을 정리한다. 정리 후 메뉴에서 다시 연결한다.
창 닫기·Cmd+Q는 Python·MCP 정리가 끝날 때까지 종료를 기다린다.
승인 대기의 취소는 거절, 질문 대기의 취소는 요청 취소로 처리한다.

Finder 옆 자동 배치는 아직 네이티브 화면에 옮기지 않았다. 창을 직접 옆에 놓아 시험한다.
기존 `main.py --interactive`·Tk `--ui`는 그대로 사용할 수 있다.

## 실행 오류 복구
앱 패키지 전체를 로컬 ad-hoc 서명하고 strict 검사를 통과한 뒤 연다.
빌드 hash가 같으면 실행 중인 앱을 덮어쓰거나 새 인스턴스를 만들지 않는다.
코드가 바뀌면 이 worktree의 앱만 종료하고 갱신한다. 자동 소유권 스냅샷은 실행 검증과 별개다.

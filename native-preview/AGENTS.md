# native-preview (SwiftUI 앱) 작업 규칙

상위 규칙은 `../AGENTS.md`. 이 폴더는 Python 백엔드(`voice_action/native_bridge.py`)와 JSON-lines로만 통신하는 화면이다.
**승인·검증·파일 접근은 전부 Python이 한다.** Swift는 보여 주고 입력만 전달한다(승인 context를 Swift에 두지 않는다).

- 구성: `Sources/FinderChatPreview/App.swift`(화면), `Backend.swift`(Python 프로세스·메시지 수신), `run.sh`(빌드·서명·실행), `Info.plist`.
- 빌드·실행: `./native-preview/run.sh` (macOS 15+, Swift 6). 빌드만: `swift build --package-path native-preview`.
- 메시지를 추가·변경하면 `../docs/native-protocol.md`와 `Backend.swift`의 `switch`, `../voice_action/native_bridge.py`를
  함께 고친다. `tests/test_native_protocol.py`가 셋의 불일치를 잡는다.
- 새 승인 종류(`operation`)는 `App.swift`의 `approvalStyle`에 제목·버튼을 추가한다. 모르는 값은 일반 "파일 변경 승인"으로 보인다.
- 앱 번들은 로컬 ad-hoc 서명이 필요하다(`run.sh`가 처리). 서명 없이 SwiftPM 실행 파일만 복사하면 오류 162가 난다.
- Python은 이 폴더의 `.venv`, 없으면 저장소 원본의 `.venv`를 쓴다(`VOICE_ACTION_PYTHON`으로 지정 가능).
  의미 검색을 쓰려면 그 `.venv`에 `--extra semantic`이 설치되어 있고 `.voice-action/config.env`에 `VOICE_ACTION_SEMANTIC_SEARCH=on`이 있어야 한다.
- UI 문구·동작을 확인하는 사람 테스트(드래그, 한글 IME, 100개 삭제 목록)는 아직 자동화되어 있지 않다. 확인하지 않은 것을 확인했다고 쓰지 않는다.

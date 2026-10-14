# 네이티브 앱 ↔ Python 전송 규약

SwiftUI 앱(`native-preview/`)은 전용 Python 프로세스(`python -m voice_action.native_bridge`)를 띄우고 **한 줄에 JSON 하나**씩
주고받는다. 승인 내용(nonce 등)은 Python에만 있고 Swift에는 1회용 `approval_id`만 간다. 코드: `voice_action/native_bridge.py`,
`native-preview/Sources/FinderChatPreview/Backend.swift`(수신)·`App.swift`(화면). 아래 목록이 코드와 다르면
`tests/test_native_protocol.py`가 실패한다 — 이벤트를 추가하면 Swift의 `switch`와 이 문서를 함께 고친다.

## Python → Swift 이벤트 (`type`)

| type | 주요 필드 | 뜻 |
|---|---|---|
| `config` | `folders` | 시작 시 허용 폴더 |
| `state` | `state` | 런타임 상태 이름(IDLE, PLANNING, WAITING_APPROVAL …) |
| `tool` | `name` | 지금 실행 중인 도구 |
| `approval` | `approval_id` `operation` `target` `targets` `preview` `truncated` `characters` | 승인 요청. `operation`: `create` `overwrite` `delete` `move` `copy` `mkdir` `convert` … <br>이동·복사는 `targets[0]`=원본, `target`=목적지. 폴더 생성은 `target`만 |
| `clarification` | `text` `kind` `choices` | 질문 또는 후보 선택(의미 검색 후보 포함) |
| `completed` | `text` `changed_paths` `ratable` | 완료 |
| `error` | `text` | 오류·바쁨 |
| `attachment` | `path` `text` | 첨부 결과(`path`가 null이면 해제) |
| `rating_saved` | `score` `tags` | 평가 저장됨 |
| `rejected` | `text` | 현재 상태에서 처리할 수 없는 명령 |
| `notice` | `text` | 안내 |
| `stopped` | | 백엔드 종료 |

## Swift → Python 명령 (`action`)

`submit`(`text`) · `approve`/`reject`(`approval_id`) · `cancel` · `undo` · `attach`(`path`) · `detach` ·
`rate`(`score`, 선택 `tags`) · `shutdown`. 상태와 맞지 않거나 `approval_id`가 다르면 `rejected`.

## 새 승인 종류를 추가할 때

1. `risk_guard.py`가 `PreparedWrite.operation`을 정한다(Tk `ui.py`의 `APPROVAL_STYLE`도 맞춘다).
2. Swift `App.swift`의 `approvalStyle`에 제목·버튼을 추가한다(모르는 값은 "파일 변경 승인"으로 보인다).
3. 이 문서의 `operation` 목록과 `tests/test_integration_features.py`에 사례를 추가한다.

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
| `clarification` | `text` `kind` `choices` | 질문 또는 후보 선택 |
| `completed` | `text` `changed_paths` `found_paths` `offer_file` `offer_paths` (선택 `show_box`) | 완료. `found_paths`는 검색이 한 파일로 정한 경로(드래그 칩용), `offer_file`은 "파일 줘"처럼 파일 자체를 달라는 명령인지, `offer_paths`는 드래그 칩으로 줄 파일(만들거나 고친 파일, 없으면 "파일 줘"로 찾은 파일, 5개 이하). `show_box`면 대화창이 보여도 오른쪽 칩 상자를 띄운다 |
| `error` | `text` | 오류·바쁨 |
| `attachment` | `path` `text` | 첨부 결과(`path`가 null이면 해제) |
| `rejected` | `text` | 현재 상태에서 처리할 수 없는 명령 |
| `transcript` | `text` `error` | 녹음을 글로 바꾼 결과. `error`가 null이면 Swift가 `text`를 `submit`한다 |
| `notice` | `text` | 안내. 앱이 옛 빌드(`SYMBIO_APP_TREE`가 현재 `native-preview` 트리와 다름)면 시작할 때 재설치 안내 |
| `stopped` | | 백엔드 종료 |

## Swift → Python 명령 (`action`)

`submit`(`text`) · `approve`/`reject`(`approval_id`) · `cancel` · `undo` · `reset`(후속 질문 기억 삭제, IDLE에서만) · `quick_list`(`sort`: `modified`·`size`, 모델 없이 최근·큰 파일 5개를 `completed`의 `offer_paths`와 `show_box: true`로 보내 오른쪽 드래그 칩으로 띄움, IDLE에서만) · `attach`(`path`) · `detach` ·
`voice`(`path`: 임시 폴더의 `voice-action-voice-*.m4a`, 변환 뒤 지운다) · `shutdown`. 상태와 맞지 않거나 `approval_id`가 다르면 `rejected`.
`submit`의 `text`가 `/feedback 내용`이면 명령이 아니라 직전 답변에 대한 의견으로 피드백 루프(U1)에 들어가고 `completed`로 접수를 알린다. 질문 대기 중에는 `rejected`.

## 화면 시험 (`scripts/ui_check.py run`)

`SYMBIO_UI_TEST_RUNTIME`이 있을 때만 대본 백엔드가 `_test`(`do`: `snapshot` `option_double_tap` `click_toast` `key` `open_chat` `close_chat` `quit`)를 보낸다.
앱은 `_done`(`do` `ok`)과 `_snapshot`(`state`: 창·입력 칩·알림·선택 칩·파일 칩 상태)으로 답한다. 평소 앱에서는 무시된다.

## 새 승인 종류를 추가할 때

1. `risk_guard.py`가 `PreparedWrite.operation`을 정한다(Tk `ui.py`의 `APPROVAL_STYLE`도 맞춘다).
2. Swift `App.swift`의 `approvalStyle`에 제목·버튼을 추가한다(모르는 값은 "파일 변경 승인"으로 보인다).
3. 이 문서의 `operation` 목록과 `tests/test_integration_features.py`에 사례를 추가한다.

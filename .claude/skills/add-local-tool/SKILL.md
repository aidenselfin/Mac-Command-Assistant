---
name: add-local-tool
description: voice-action에 새 local__* 도구를 추가하거나 기존 도구의 위험 분류·승인 동작을 바꿀 때 쓴다. Guard 분류, Planner 노출, 테스트, eval, 문서 갱신 체크리스트.
---

# 새 도구 추가 체크리스트

안전 경계(허용 폴더·symlink·승인)는 약화하지 않는다. 먼저 `superpowers:test-driven-development`로 실패 테스트부터 쓴다.

1. 도구 패키지: `backend/FileOps`를 본보기로 `backend/<모듈>`(`Package.swift` path 의존)을 만든다. 허용 폴더 밖·symlink 우회를 막고 `SymbioCore.inside`·`O_NOFOLLOW` 하위 열기를 쓴다.
2. 분류: `backend/Guard`의 `classifyTool`에 넣는다. 쓰기·삭제·이동·변환이면 `writeTools`에도 넣어 1회용 승인을 거치게 한다.
3. Undo: 파일을 바꾸면 `backend/Undo` journal에 기록한다. 승인 없이 "했다"고 답하지 않는다.
4. 노출: `backend/Planner`의 노출 조건과 시스템 프롬프트를 고친다. 크니 해당 함수 주변만 읽는다. 도구 스키마에 상한·`oneOf`를 넣지 않고 코드에서 검증한다.
5. 승인 화면: 쓰기 도구면 SwiftUI에 표시한다. 메시지가 바뀌면 `docs/native-protocol.md`, `Backend.swift`, `backend/App`의 `Bridge.swift`를 함께 고친다.
6. 테스트: `backend/<모듈>/Tests/`와 `backend/Guard/Tests/`(Swift Testing). 선택 기능이면 꺼진 상태의 도구 정의가 그대로인지 `backend/App/Tests/…/BridgeTests.swift`로 확인한다.
7. eval: `evals/tasks/*.json`에 과제를 추가한다(성공 1개 이상, 하면 안 되는 `neg_*` 1개 이상).
8. 문서: `docs/features.md` 표를 같은 커밋에서 고친다. 구조가 달라지면 `update-work-records` 스킬로 구조 기록도 고친다.
9. 확인: `scripts/check.sh quick`, 끝에는 `scripts/check.sh full`. 결과는 `superpowers:verification-before-completion` 기준으로 실제 출력만 보고한다.

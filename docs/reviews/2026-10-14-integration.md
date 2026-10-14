# 2026-10-14 통합 보고서 (파일 정리 + 의미 검색 + 네이티브 SwiftUI)

## 입력
| 갈래 | 기준 | 내용 |
|---|---|---|
| `main` (`42987d0`) | | 파일 이동·이름 변경·복사·폴더 생성(승인·Undo) |
| `work/semantic-search` | `e250f48` | 의미 검색(EmbeddingGemma 2, 선택), 병렬 수정 세션 도구(`vibe.py worktree`) |
| `codex/finder-chat-panel` | `e250f48` | Tk Finder 옆 패널 개편, SwiftUI 네이티브 앱, `native_bridge.py` |
오래된 Codex worktree(`33fd`, `b9f6`)는 사용자 지시로 반영하지 않았다. 백업 태그 `pre-integration/{main,semantic,native}`.

## 순서와 충돌
`main` ← 의미 검색 ← 네이티브. 코드는 모두 자동 병합이었고 `docs/decisions.md`·`docs/status.md`만 직접 합쳤다.
통합 중 만든 것: 네이티브 승인 화면의 이동·복사·폴더 생성 표시(`App.swift`), 기능 조합 테스트(`test_integration_features.py`),
네이티브 프로토콜 일치 테스트(`test_native_protocol.py`), 문서(구조 지도·기능 등록부·프로토콜), `scripts/check.sh`.

## 통합 중 찾은 문제와 조치
1. 글로 사진을 찾으면 정답도 점수가 0.65 안팎이라 한 가지 컷오프로 걸러지거나 글 파일이 끼어들었다 → 글 0.69·사진 0.62로 분리(13/13).
2. 모델이 `query`를 거의 채우지 않았다 → 도구 설명에 "설명 구절만, 동작·목적지 제외"를 넣고 비어 있으면 사용자 원문으로 대체.
3. 색인이 모두 준비됐는데 재확인 중이면 "n/n개만 준비됨"으로 표시되던 버그 수정.
4. 네이티브 승인 화면이 이동·복사·폴더 생성을 "파일 변경 승인 / 0자"로만 보여 주던 문제 수정.

## 검증
pytest 722 passed, ruff, 평가 재생 42/42, `swift build`. live 평가(과제 33×3): 통합 90/99(INVALID 8), 통합 재측정 90/99(INVALID 7),
통합 전 main 95/99(INVALID 3). 프롬프트·도구 정의가 같은 코드라 변동으로 보이나, 이동 과제(`move_*`, `create_directory_undo`)는
통합 코드에서 INVALID가 더 많았다(모델이 폴더의 절대 경로를 되묻는 경우). 같은 코드 반복이 부족해 원인을 확정하지 못했다.
사람이 앱 화면을 눈으로 확인한 것은 없다.

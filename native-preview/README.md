# 네이티브 macOS 외형 시제품

SwiftUI + macOS 시스템 컨트롤을 사용한 360×560 대화 패널이다. macOS 15 이상과 Swift 6 도구가 필요하다.

```sh
./native-preview/run.sh
```

하단 전환 버튼 또는 오른쪽 위 `…`에서 대화·파일 선택·저장 승인 화면을 전환한다.
입력·전송·후보 선택·평가·승인 버튼은 화면 반응만 보여준다.
실제 모델 호출, 파일 읽기/쓰기, 권한 요청, Finder 추적, Python 연결은 없다.
창을 Finder 옆으로 직접 놓아 외형을 비교한다. 시스템 라이트·다크 모드를 따른다.
빌드 결과 앱은 `native-preview/.build/FinderChatPreview.app`이다.

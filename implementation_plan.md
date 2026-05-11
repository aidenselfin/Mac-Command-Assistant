# 외부 DB 연동 단축키 학습 힌트 시스템 구현 계획

사혁신님이 제안해주신 "마우스 클릭 시 해당하는 단축키를 찾아 1.5초간 시각적 힌트로 제공하는 학습 유도 시스템"의 구현 계획입니다.

## 1. 목적 (Objective)
사용자가 마우스로 특정 버튼이나 UI 요소를 클릭했을 때, 해당 기능에 매칭되는 단축키가 존재한다면 마우스 커서 위치(또는 클릭한 요소)에 단축키 힌트를 띄워주어 점진적인 키보드 사용을 유도합니다.

## 2. 데이터베이스 설계 (Local Cache)
외부 크라우드소싱 데이터를 모방하여, 프로젝트 폴더 내에 `shortcuts_db.json` 이라는 로컬 캐시 파일을 생성합니다.
(우선적으로 많이 사용되는 **VS Code**와 **Google Chrome**을 타겟으로 기초 데이터를 구성합니다.)

```json
{
  "com.microsoft.VSCode": {
    "Explorer": "Cmd + Shift + E",
    "Search": "Cmd + Shift + F",
    "Source Control": "Ctrl + Shift + G",
    "Extensions": "Cmd + Shift + X",
    "Toggle Sidebar": "Cmd + B"
  },
  "com.google.Chrome": {
    "New Tab": "Cmd + T",
    "Close Tab": "Cmd + W",
    "Reload": "Cmd + R"
  }
}
```

## 3. 코드 수정 계획 (`overlay_engine.py`)

### 3.1 마우스 클릭 이벤트 추출 강화 (`HotkeyManager`)
- `kCGEventLeftMouseUp` 이벤트 발생 시, `Quartz.CGEventGetLocation(event)`를 호출하여 마우스의 전역 좌표 `(X, Y)`를 추출합니다.
- 추출한 좌표를 `OverlayEngineController.trigger_rescan(x, y)` 로 전달합니다.

### 3.2 힛 테스팅 및 DB 매칭 (`AccessibilityScanner`)
- 이벤트 루프 차단을 막기 위해 별도 스레드(Thread)에서 힛 테스트를 수행합니다.
- `AXUIElementCopyElementAtPosition` 함수를 사용해 `(X, Y)` 좌표에 있는 구체적인 UI 요소(`AXUIElement`) 객체를 즉시 가져옵니다.
- 현재 활성화된 앱의 `Bundle Identifier`를 가져옵니다.
- 힛 테스트로 가져온 객체의 `AXTitle` 또는 `AXDescription`을 읽어, `shortcuts_db.json` 내부의 키값과 일치하는 항목이 있는지 검색합니다.

### 3.3 시각적 힌트 렌더링 (`OverlayWindowView`)
- 일치하는 단축키 문자열을 찾으면 뷰의 `temporary_tags` 리스트에 1.5초 수명(`expire`)을 주어 추가합니다.
- `drawRect_` 메서드 내부에서 `temporary_tags`가 존재할 경우:
  - 검은색 배경(`#1A1A1A`), 노란색 또는 흰색 텍스트로 눈에 띄게 렌더링합니다.
- 0.1초마다 실행되는 `refreshUI_` 타이머가 만료된 태그를 지우고 화면을 다시 그립니다.

## 4. 사용자 피드백 요청 (User Review Required)
> [!IMPORTANT]
> 1. JSON 기반의 로컬 DB 구조(`Bundle ID` -> `UI Name` -> `Shortcut`)에 동의하시나요?
> 2. 마우스 커서 위치에 바로 단축키 힌트(예: `Cmd + E`)가 1.5초간 깜빡이듯 뜨는 UI 방식이 구상하신 것과 일치하는지 확인 부탁드립니다.
> 승인해 주시면 즉시 `shortcuts_db.json`을 생성하고 로직에 통합하겠습니다!

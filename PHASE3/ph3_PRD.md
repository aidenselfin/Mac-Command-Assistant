
# 📄 PRD: 지능형 창/패널(Pane) 분할 인식 및 토글 오버레이 시스템

## 1. 프로젝트 목적 (Objective)
[cite_start]사용자가 지정된 단축키(`Ctrl + Cmd + K`)를 눌렀을 때, 화면에 떠 있는 서로 다른 애플리케이션 창뿐만 아니라 **단일 애플리케이션 내의 다중 창, 탭, 분할 패널(Split Panes)**까지 완벽하게 식별하여 노란색 테두리(오버레이 태그)로 시각화합니다[cite: 9]. 동일한 단축키를 다시 누르면 모든 오버레이가 즉시 화면에서 사라지는 **토글(Toggle) 로직**을 완벽하게 구현하는 것을 목표로 합니다.

## 2. 다루는 문제 (Problem Statement)
* [cite_start]**현재의 한계 (Intra-app 식별 불가):** 현재 로직은 `pyobjc-framework-Cocoa` 등을 활용해 최상위 애플리케이션 객체(App level)만 인식하고 있습니다[cite: 11]. [cite_start]따라서 Chrome 브라우저 창이 2개 떠 있거나, VS Code 내에서 화면이 좌우로 분할되어 있어도 이를 하나의 거대한 덩어리로 인식해 버리는 오류가 발생합니다[cite: 12].
* [cite_start]**인지적 오류:** 사용자는 눈으로 '두 개의 작업 공간'을 보고 있지만, 시스템은 '하나의 앱'으로만 태그를 부여하여 정밀한 타겟팅이 불가능합니다[cite: 13].

## 3. 선행 사례 분석 (Precedent Research)
[cite_start]이 문제를 성공적으로 해결한 상용/오픈소스 툴들은 다음과 같은 방식을 사용합니다[cite: 14].

* [cite_start]**Homerow & Shortcat:** Mac 전체 시스템을 제어하는 이 툴들은 단순히 앱 이름만 가져오는 것이 아니라, Mac의 **Accessibility API(손쉬운 사용 API)**를 깊게 파고들어 UI 요소를 실시간 분석합니다[cite: 15]. [cite_start]앱 내부에 숨겨진 `AXWindow`, `AXSplitGroup`, `AXTabGroup` 같은 하위 요소들의 좌표를 재귀적으로 추적하여 각각 별도의 태그를 붙입니다[cite: 16].
* [cite_start]**Vimium (브라우저 확장):** 웹 브라우저 내에서 화면 요소마다 태그를 붙이는 Vimium은 HTML의 DOM 트리를 분석합니다[cite: 17]. [cite_start]Mac 데스크탑 환경에서는 Accessibility 트리가 이 DOM 트리의 역할을 대신합니다[cite: 18].

## 4. 핵심 요구 사항 (Core Requirements)

### 4.1. 동작 시나리오 (User Flow)
1. 사용자가 `Ctrl + Cmd + K`를 누릅니다.
2. 시스템이 현재 화면 상태를 스캔하여 모든 앱의 내부 창/분할 패널 좌표를 추출합니다.
3. 추출된 개별 좌표마다 노란색 테두리 오버레이가 렌더링됩니다.
4. 사용자가 다시 `Ctrl + Cmd + K`를 누릅니다.
5. 화면에 렌더링되어 있던 모든 노란색 테두리 객체가 메모리에서 해제되며 즉시 사라집니다 (Toggle-Off).

### 4.2. 기술적 접근법 및 해결 로직 (Approach & Solution)
[cite_start]하나의 프로그램 내에 있는 여러 창을 완벽하게 구별하려면, 데이터 수집 방식을 '앱 단위'에서 'Accessibility 트리 탐색 단위'로 고도화해야 합니다[cite: 20].

* [cite_start]**UI 트리 재귀 탐색 (Recursive Tree Parsing):** 활성화된 앱을 찾은 후 탐색을 멈추지 않고, 해당 앱의 자식 노드(Children)로 계속 파고들어야 합니다[cite: 21]. [cite_start]`pyobjc-framework-ApplicationServices`를 사용하여, 역할(Role)이 `AXWindow`(독립된 창)이거나 `AXSplitGroup`(분할된 화면), `AXScrollArea`(스크롤 영역)인 요소들의 고유 좌표(Position)와 크기(Size) 데이터를 추출합니다[cite: 22].
* [cite_start]**가시성(Visibility) 필터링:** 화면에 그려지지 않은(가려져 있거나 최소화된) 창에도 테두리를 그리는 오류를 방지하기 위해, 현재 화면에 보이는 요소만 필터링하는 로직을 추가합니다[cite: 23].
* [cite_start]**오버레이 렌더링 독립성:** 추출된 각 하위 패널의 (x, y, width, height) 값을 바탕으로, 개별적인 노란색 테두리 오버레이를 독립적으로 렌더링합니다[cite: 24].

---

## 5. 무결점 구현을 위한 상세 기술 명세 (Technical Specifications)

이 프로젝트를 오류 없이 구현하기 위해 개발 단계에서 다음 아키텍처와 로직을 엄격하게 준수해야 합니다.

### 5.1. 글로벌 단축키 리스너 및 상태 관리 (Toggle Logic)
* **상태 변수 관리:** 프로그램 최상단에 `is_overlay_active = False` 형태의 전역 상태 변수(Boolean)를 두어야 합니다.
* **이벤트 후킹:** `pynput`의 Global Hotkey 기능이나 `CGEventTapCreate`를 사용하여 `Ctrl + Cmd + K` 입력을 백그라운드에서 감지합니다.
* **토글 스위치 로직:** * 단축키 입력 감지 시 `is_overlay_active` 값을 반전(`not is_overlay_active`)시킵니다.
  * 값이 `True`가 되면 **[5.2 트리 탐색]** 함수를 호출하여 화면을 그리고, `False`가 되면 **[5.3 메모리 해제]** 함수를 호출하여 화면을 지웁니다.

### 5.2. Accessibility API 기반 트리 탐색 (Core Engine)
앱 내부의 창을 쪼개기 위한 가장 핵심적인 재귀 함수(Recursive Function) 설계입니다.

1. **최상위 객체 획득:** `NSWorkspace.sharedWorkspace().runningApplications()`를 통해 현재 실행 중인 앱 목록을 가져옵니다.
2. **AXUIElement 변환:** 각 앱의 프로세스 ID(PID)를 이용해 `AXUIElementCreateApplication(pid)`를 호출하여 Accessibility 객체로 변환합니다.
3. **재귀 탐색 로직 (Pseudo-code 흐름):**
   * 특정 `AXUIElement` 객체의 `kAXChildrenAttribute`를 요청하여 자식 노드 리스트를 가져옵니다.
   * 각 자식 노드의 `kAXRoleAttribute`를 확인합니다.
   * Role이 `AXWindow`, `AXSplitGroup`, `AXTabGroup` 등 '논리적 구획'을 의미할 경우:
     * 해당 요소의 `kAXPositionAttribute`와 `kAXSizeAttribute`를 추출하여 배열에 저장합니다.
   * 해당 노드가 더 작은 패널로 쪼개질 수 있다면(자식이 있다면) 함수를 다시 자기 자신에게 호출(재귀)하여 더 깊이 파고듭니다.

### 5.3. 투명 오버레이 렌더링 및 해제 (Rendering Lifecycle)
* **렌더링:** 탐색된 좌표 배열 `[(x1, y1, w1, h1), (x2, y2, w2, h2), ...]`을 순회하며, Python의 GUI 라이브러리(예: `PyQt5`, `Tkinter`, 또는 macOS 네이티브 `NSWindow`)를 사용하여 **테두리만 있고 내부는 투명한(Click-through)** 창을 생성합니다.
* **참조(Reference) 유지:** 생성된 모든 오버레이 윈도우 객체는 `overlay_windows_list` 같은 전역 배열에 반드시 저장해 두어야 합니다. (이 배열이 없으면 토글 오프 시 창을 지울 수 없습니다.)
* **해제 (Toggle Off):** 단축키가 다시 눌려 `is_overlay_active`가 `False`가 되면, `overlay_windows_list`를 순회하며 각 윈도우 객체의 `close()` 또는 `destroy()` 메서드를 호출하여 화면에서 완벽하게 제거하고 배열을 비웁니다.

---

## 6. 요구사항 정의 (MoSCoW 우선순위)

* **Must Have (필수 구현)**
    * `Ctrl + Cmd + K` 단축키를 통한 오버레이 렌더링 켜기/끄기 (Toggle) 로직 완벽 구현.
    * [cite_start]단일 앱 내에서 분리된 여러 개의 `AXWindow` (예: 크롬 창 1, 크롬 창 2)를 서로 다른 객체로 인식하고 테두리를 생성한다[cite: 25].
    * [cite_start]Accessibility API를 활용한 하위 UI 노드(자식 요소) 탐색 로직을 적용한다[cite: 26].
* **Should Have (권장 구현)**
    * [cite_start]VS Code나 터미널 등에서 화면이 분할된 경우(`AXSplitGroup`), 분할된 각 구역에 별도의 테두리를 렌더링한다[cite: 27].
    * [cite_start]백그라운드에 가려져 있어 사용자 눈에 보이지 않는 창이나 패널은 테두리 렌더링에서 제외한다[cite: 28].
* **Could Have (선택 구현)**
    * [cite_start]앱 간의 구별을 더 명확히 하기 위해 애플리케이션 간 테두리 색상 미세 조정 (예: 주 앱은 짙은 노란색, 서브 패널은 연한 노란색)[cite: 29].
* **Won't Have (이번 페이즈 제외)**
    * [cite_start]앱 내부의 아주 작은 버튼이나 텍스트 링크 단위까지 테두리를 그리는 작업 (현재는 '창'과 '패널' 단위의 구분에만 집중)[cite: 30].
    * Phase 1의 데이터 로깅(`logger.py`) 시스템과의 연동 기능 일체 배제.
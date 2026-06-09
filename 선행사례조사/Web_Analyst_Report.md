충분한 데이터를 수집했습니다. 이제 종합 보고서를 작성합니다.

---

# 비전 없는 브라우저 DOM/JS 기반 웹 자동화: 선행 사례 심층 분석 보고서

## 목차

1. [전체 생태계 개요](#1-전체-생태계-개요)
2. [요소 추출 메커니즘](#2-요소-추출-메커니즘)
3. [액션 실행 파이프라인](#3-액션-실행-파이프라인)
4. [동적 렌더링 처리](#4-동적-렌더링-처리)
5. [대표 프로젝트 상세 분석](#5-대표-프로젝트-상세-분석)
6. [기술 비교 매트릭스](#6-기술-비교-매트릭스)
7. [아키텍처 다이어그램](#7-아키텍처-다이어그램)
8. [결론 및 시사점](#8-결론-및-시사점)

---

## 1. 전체 생태계 개요

비전(스크린샷) 없이 DOM과 JS만으로 웹을 자동화하는 접근법은 크게 세 계보로 나뉜다.

```
[계보 1] CDP 직접 활용 계열
  └─ natbot → CDP DOMSnapshot + Playwright 좌표 클릭

[계보 2] Accessibility Tree 계열
  └─ Playwright MCP → ARIA 역할/레이블 기반 직렬화
  └─ WebArena → textual accessibility tree
  └─ Agent-E → DOM distillation (3가지 표현) + mmid 주입

[계보 3] JS Injection + DOM 트리 재구축 계열
  └─ browser-use → buildDomTree.js 주입 → ClickableElementDetector
  └─ Vimium/SurfingKeys → addEventListener 후킹 + getBoundingClientRect
```

---

## 2. 요소 추출 메커니즘

### 2.1 natbot — CDP DOMSnapshot 기반 추출

natbot(nat/natbot)은 가장 단순하고 직관적인 접근법을 취한다. Chrome DevTools Protocol의 `DOMSnapshot.captureSnapshot` 명령으로 전체 DOM을 한 번에 캡처한 뒤, Python에서 필터링한다.

**CDP 스냅샷 캡처:**

```python
tree = self.client.send(
    "DOMSnapshot.captureSnapshot",
    {
        "computedStyles": [],
        "includeDOMRects": True,    # 좌표 정보 포함
        "includePaintOrder": True   # 렌더링 순서 포함
    },
)
```

**요소 필터링 알고리즘 (단계별):**

```python
# Step 1: 메타데이터 태그 블랙리스트 제거
black_listed_elements = set([
    "html", "head", "title", "meta", "iframe", "body",
    "script", "style", "path", "svg", "br", "::marker"
])

# Step 2: 뷰포트 범위 내 요소만 선택
# (win_upper_bound ~ win_lower_bound 사이)

# Step 3: 의미 있는 요소만 유지
if (converted_node_name != "button" or meta == "") and \
   converted_node_name != "link" and inner_text.strip() == "":
    continue

# Step 4: 앵커/버튼 내부 텍스트 중복 방지
```

**GPT용 XML 직렬화 (최대 4,500자 제한):**

```xml
<link id=1>About</link>
<button id=2>Search</button>
<input id=3 placeholder="Search the web"/>
<img id=4 alt="Company Logo"/>
```

노드 타입 변환 규칙:
- `<a>` → `<link>`
- `<input type="submit">` → `<button>`
- `onclick` 속성이 있는 `<div>` → `<button>`
- `placeholder`, `aria-label`, `title`, `alt` → `meta` 문자열로 포함

---

### 2.2 browser-use — JS 주입 기반 ClickableElementDetector

browser-use는 `buildDomTree.js`를 페이지에 주입하여 클라이언트 사이드에서 DOM 트리를 재구축하고, Python의 `ClickableElementDetector`로 후처리한다.

**interactive element 판별 4계층:**

**[계층 1] 네이티브 HTML 태그 기반**
```
button, input, select, textarea, a, details, summary
→ label은 중복 활성화 방지를 위해 제외
→ 대신 "nested form controls 검사"로 처리
```

**[계층 2] JS 이벤트 리스너 감지**

```python
# CDP getEventListeners 활용 (DevTools 콘솔 수준 권한 필요)
# page.evaluate 컨텍스트에서는 접근 불가 → 현재 한계점
# 대안: addEventListener 후킹 (Vimium PR #1859 접근법)
has_js_click_listener = check_event_listeners(element)
# click, mousedown, mouseup 핸들러 감지
```

**[계층 3] ARIA 속성 기반 판별**

```
포함 role: button, link, menuitem, checkbox,
           slider, combobox, tab, option

포함 속성: aria-checked, aria-expanded,
          aria-pressed, aria-selected

제외 조건: aria-disabled="true"
          aria-hidden="true"
```

**[계층 4] 특수 휴리스틱**

```python
# 검색 요소 감지
search_keywords = ["search", "magnify", "query", "find"]
# class, id, data-* 속성에서 키워드 탐색

# iframe 처리: 100×100px 초과만 interactive로 표시

# 폼 컨트롤 자손 탐색: 최대 깊이 2 레벨
# label > span > input 같은 중첩 패턴 처리
has_form_control_descendant(element, max_depth=2)
```

**요소 인덱싱 및 매핑:**

```python
# 최종적으로 순차 정수 인덱스 할당
selector_map = {
    0: {"target_id": "...", "backend_node_id": "..."},
    1: {"target_id": "...", "backend_node_id": "..."},
    # LLM이 "요소 0을 클릭" → CDP 실행용 ID로 변환
}
```

**`browser-user-highlight-id` 속성 주입 (buildDomTree.js 124번 라인):**

```javascript
// 요소에 고유 식별자 주입
element.setAttribute('browser-user-highlight-id', index);
// 이후 context.py에서 이 속성으로 요소를 재탐색
```

---

### 2.3 Playwright MCP — Accessibility Tree 직렬화

Playwright는 비전 없는 에이전트를 위한 가장 토큰 효율적인 방식을 제공한다. Playwright MCP 서버는 브라우저의 접근성 트리를 구조화된 텍스트로 전송한다.

**접근성 트리 직렬화 예시:**

```
role=button name="Submit"
role=textbox name="Email" value=""
role=link name="Home" url="https://..."
role=combobox name="Country" expanded=false
role=checkbox name="Accept Terms" checked=false
```

**토큰 효율성 비교:**

| 방식 | 데이터 크기 | 토큰 비용 |
|------|-------------|-----------|
| 스크린샷 | 100KB+ | 높음 |
| 원시 HTML | 50KB+ | 높음 |
| Accessibility Tree | 2~5KB | 낮음 (20~50배 절감) |

---

### 2.4 Agent-E — mmid 주입 + 3종 DOM Distillation

Agent-E는 각 HTML 요소에 `mmid` 사용자 정의 속성을 주입한 후, 작업 유형에 따라 세 가지 DOM 표현 중 하나를 선택한다.

```python
# mmid 속성 주입 (JS로 전체 DOM 순회)
document.querySelectorAll('*').forEach((el, i) => {
    el.setAttribute('mmid', i);
});

# DOM distillation 3종
DOM_REPRESENTATIONS = {
    "text_only":    # 텍스트 콘텐츠만 (요약 작업용)
    "input_fields": # 입력 필드만 (검색/폼 작업용)
    "all_fields":   # 모든 interactive 요소 (탐색 작업용)
}
```

---

### 2.5 Vimium/SurfingKeys — 힌트 모드 DOM 탐색

Vimium의 힌트 모드는 `f` 키 입력 시 전체 DOM을 순회하여 클릭 가능한 요소를 실시간으로 탐지한다.

**핵심 판별 로직:**

```javascript
// isVisible 판별
function isVisible(element) {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
           getComputedStyle(element).visibility !== 'hidden' &&
           getComputedStyle(element).display !== 'none';
}

// isClickable 판별 (휴리스틱 기반)
// - 네이티브 태그: a, button, input, select, textarea
// - 폰트 크기, 헤더 컨텍스트, strong 태그 고려
// - 부모 요소의 click handler 존재 여부
// - getBoundingClientRect로 절대 좌표 계산
```

**addEventListener 후킹 시도 (PR #1859 — 미병합):**

```javascript
// 페이지 컨텍스트에서 인라인 script 태그로 주입
// EventTarget.prototype.addEventListener를 래퍼로 교체
const originalAdd = EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener = function(type, handler, opts) {
    if (type === 'click' || type === 'mousedown') {
        // VimiumRegistrationElementEvent 발생 → 콘텐츠 스크립트로 버블링
        this.dispatchEvent(new CustomEvent('VimiumRegistrationElementEvent'));
    }
    return originalAdd.call(this, type, handler, opts);
};
```

이 PR은 기술적으로 혁신적이었으나 CSP 충돌, 유지보수 복잡도 이유로 2020년 폐기됐다.

---

## 3. 액션 실행 파이프라인

### 3.1 전체 파이프라인: 자연어 → DOM → 실행

```
[1] 자연어 명령 수신
    "검색창에 'AI 에이전트'를 입력하고 검색 버튼을 클릭해"
        ↓
[2] 페이지 상태 캡처
    DOM 스냅샷 / Accessibility Tree / buildDomTree.js 실행
        ↓
[3] 직렬화 & LLM 컨텍스트 주입
    XML/JSON/텍스트 트리 → 프롬프트에 포함
        ↓
[4] LLM 추론
    "input id=3에 타이핑, button id=7 클릭"
        ↓
[5] 요소 매핑
    id=3 → backend_node_id / mmid / CSS selector
        ↓
[6] 액션 실행
    JS 주입 / CDP 명령 / Playwright API 호출
        ↓
[7] 결과 관찰
    MutationObserver / DOM 변화 감지 → 다음 스텝
```

---

### 3.2 JS 주입 vs CDP 방식 상세 비교

**방식 A: 순수 JS 주입 (`element.click()`)**

```javascript
// Runtime.evaluate를 통한 JS 실행
const element = document.querySelector('[browser-user-highlight-id="42"]');
element.click();

// 또는 좌표 기반 이벤트 합성
element.dispatchEvent(new MouseEvent('click', {
    bubbles: true,
    cancelable: true,
    userGesture: true  // 제한된 API 활성화에 필요
}));
```

**방식 B: CDP Input.dispatchMouseEvent**

```python
# Puppeteer 방식 — 실제 마우스 이벤트 시뮬레이션
client.send("Input.dispatchMouseEvent", {
    "type": "mousePressed",
    "x": center_x,
    "y": center_y,
    "button": "left",
    "clickCount": 1
})
client.send("Input.dispatchMouseEvent", {
    "type": "mouseReleased",
    "x": center_x,
    "y": center_y,
    "button": "left"
})
```

**비교 분석:**

| 항목 | JS 주입 (`element.click()`) | CDP `dispatchMouseEvent` |
|------|------------------------------|--------------------------|
| 실행 속도 | 빠름 (직접 호출) | 보통 (3단계: move→press→release) |
| 이벤트 현실성 | 합성 이벤트 (일부 앱 미감지) | 실제 마우스 이벤트 시뮬레이션 |
| 좌표 의존성 | 없음 (요소 직접 참조) | 필요 (렌더링된 좌표 필요) |
| 사용자 활성화 | `userGesture: true` 필요 | 자동으로 실제 입력으로 인식 |
| WebXR/클립보드 접근 | 제한적 | 가능 |
| Shadow DOM 내 요소 | 직접 접근 가능 | 좌표 계산 필요 |
| Puppeteer 기본 방식 | 아님 | 기본 (`page.click()`) |
| Playwright 기본 방식 | 기본 (`element.click()`) | 옵션 |

**Puppeteer vs Playwright 성능 차이의 원인:**

```
Puppeteer: CDP 직접 통신 → 11KB 메시지
Playwright: 추상화 계층 존재 → 326KB 메시지
→ Puppeteer가 15~20% 빠르나 AI 에이전트에는 Playwright의 신뢰성이 우선
```

---

### 3.3 XPath vs CSS Selector vs Accessibility Tree 비교

```
XPath 방식:
  장점: 복잡한 계층 탐색 표현력
  단점: Shadow DOM 경계 통과 불가 (XPath 사양이 Shadow DOM보다 선행)
  예시: //div[@class='nav']//button[text()='Submit']

CSS Selector 방식:
  장점: 간결, 속성 기반 선택 용이
  단점: 복잡한 구조 표현 한계
  예시: [browser-user-highlight-id="42"]
        [data-testid="submit-btn"]

Accessibility Tree 방식:
  장점: 의미론적 (role/name), Shadow DOM 무관, 토큰 효율적
  단점: 동적 ARIA 속성 변경 시 불안정
  예시: role=button name="Submit"
```

**browser-use의 CSS Selector 생성 폴백 로직:**

```python
# context.py#L788
# 1순위: id 속성
# 2순위: name, type, aria-label, data-qa, data-testid
# 3순위: 클래스 속성 (정규식 유효성 검증 후)
# 최후: browser-user-highlight-id 속성 (buildDomTree.js가 주입한 값)
```

---

### 3.4 Shadow DOM & iframe 처리

**Shadow DOM 처리의 근본적 한계:**

```
XPath는 Shadow DOM 경계를 통과할 수 없다.
→ browser-use Issue #3820: x_path가 Shadow DOM 요소에서 작동 안 함

현재 browser-use의 특수 처리:
- CDP DOMSnapshot에 snapshot_node 데이터가 없는 요소 감지
- 로그인 폼, 커스텀 웹 컴포넌트 등 Shadow Root 내 요소를
  특수 예외 처리로 interactive로 표시
```

**제안된 Shadow DOM 경로 표현:**

```
현재 (미지원):
  x_path: //div[@id='host']//input

제안된 형식:
  shadow_path: #host::shadow input
  또는 frame_hierarchy: [frame_id_1, shadow_host_id, element_id]
```

**iframe 처리 (browser-use):**

```python
# 100×100 픽셀 초과 iframe만 interactive로 표시
if iframe_width > 100 and iframe_height > 100:
    mark_as_interactive(iframe)
# frame_id는 현재 None으로 유지됨 → Issue #3820의 미해결 문제
```

**Playwright의 iframe 접근:**

```python
# Playwright는 iframe을 별도 Frame 객체로 추상화
frame = page.frame(url="https://embedded.example.com")
await frame.click("button#submit")

# 중첩 iframe
frame = page.frame_locator('#outer-iframe').frame_locator('#inner-iframe')
```

---

## 4. 동적 렌더링 처리

### 4.1 React/Vue SPA 콘텐츠 로딩 대기 전략

**waitForSelector — 결정론적 대기 (권장):**

```javascript
// 특정 요소 출현 대기
await page.waitForSelector('[data-testid="results-container"]', {
    timeout: 5000,
    state: 'visible'  // 'attached' | 'visible' | 'detached' | 'hidden'
});

// Playwright의 자동 대기 (auto-wait)
// 모든 액션 전 actionability 확인:
// - visible, stable, enabled, receives events
await page.click('#submit');  // 내부적으로 위 조건 모두 확인
```

**networkidle — 주의해서 사용:**

```javascript
// networkidle0: 활성 네트워크 연결 0개
// networkidle2: 활성 네트워크 연결 2개 이하
await page.goto(url, { waitUntil: 'networkidle2' });

// 한계: 분석 추적, WebSocket, 오래 지속되는 요청 시 행(hang) 발생
// XHR이 "idle" 이후 재발사 시 너무 일찍 추출
```

**waitForFunction — 커스텀 조건:**

```javascript
// 최소 20개 항목 로드 확인
await page.waitForFunction(
    () => document.querySelectorAll('[data-item]').length >= 20,
    { timeout: 5000 }
);

// Angular의 $http 완료 감지
await page.waitForFunction(() => {
    const injector = window.getAllAngularRootElements()[0];
    return injector && !injector.classList.contains('ng-animate');
});
```

**MutationObserver — 실시간 DOM 변화 감지:**

```javascript
// Agent-E의 변화 감지 메커니즘
const observer = new MutationObserver((mutations) => {
    mutations.forEach(mutation => {
        // aria-expanded 변화 감지
        if (mutation.attributeName === 'aria-expanded') {
            reportChange(mutation.target, mutation.oldValue);
        }
        // 새 DOM 노드 추가 감지
        mutation.addedNodes.forEach(node => {
            if (node.nodeType === Node.ELEMENT_NODE) {
                reportNewElement(node);
            }
        });
    });
});

observer.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ['aria-expanded', 'aria-checked', 'class']
});
```

---

### 4.2 AJAX 완료 감지 방법

```javascript
// 방법 1: XHR 인터셉션 (네이티브 XHR 래핑)
const originalOpen = XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open = function(...args) {
    this.addEventListener('loadend', () => pendingRequests--);
    pendingRequests++;
    return originalOpen.apply(this, args);
};

// 방법 2: Fetch API 인터셉션
const originalFetch = window.fetch;
window.fetch = async (...args) => {
    pendingRequests++;
    try { return await originalFetch(...args); }
    finally { pendingRequests--; }
};

// 방법 3: Playwright의 네트워크 이벤트 활용
await page.waitForResponse(
    response => response.url().includes('/api/data') && 
                response.status() === 200
);
```

---

### 4.3 에러 복구 및 재시도 로직

**browser-use의 폴백 LLM 전환:**

```python
# 기본 LLM 실패 시 자동 폴백
if self._fallback_llm and not self._using_fallback_llm:
    self._using_fallback_llm = True
    # 폴백 모델로 재시도

# 단계당 최대 5개 액션 제한
max_actions_per_step: int = 5

# 외부 일시 중지 제어 (비동기 이벤트)
self._external_pause_event = asyncio.Event()
self._external_pause_event.set()
```

**Agent-E의 변화 관찰 피드백 루프:**

```python
# 각 액션 실행 후 DOM 변화 관찰
# "클릭이 mmid=25인 요소를 클릭했습니다.
#  결과적으로 팝업이 나타났습니다: [요소 목록]"
# → 다음 LLM 추론에 피드백으로 제공
```

---

## 5. 대표 프로젝트 상세 분석

### 5.1 browser-use 전체 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                   browser-use                        │
├─────────────────────────────────────────────────────┤
│  Agent Service (agent/service.py)                   │
│  ├─ MessageManager: 대화 기록 관리                   │
│  ├─ SystemPrompt: 모델별 최적화 프롬프트             │
│  ├─ Tools Registry: 동적 액션 모델 생성              │
│  ├─ TokenCost: 비용 추적                            │
│  └─ FallbackLLM: 오류 복구                          │
├─────────────────────────────────────────────────────┤
│  DOM 처리 계층 (browser/dom/)                        │
│  ├─ buildDomTree.js: 클라이언트 사이드 DOM 재구축   │
│  │   ├─ 요소별 browser-user-highlight-id 주입       │
│  │   ├─ JSON.stringify()로 직렬화 (성능 최적화)     │
│  │   └─ Shadow DOM 특수 처리                        │
│  ├─ ClickableElementDetector                        │
│  │   ├─ 4계층 판별 (태그/이벤트/ARIA/휴리스틱)     │
│  │   └─ _clickable_cache (중복 계산 방지)           │
│  ├─ DOMTreeSerializer                               │
│  │   └─ selector_map (index → CDP target ID)        │
│  └─ DOMInteractedElement                           │
│      ├─ x_path (Shadow DOM 한계 있음)               │
│      ├─ backend_node_id                             │
│      └─ frame_id (현재 미구현)                      │
├─────────────────────────────────────────────────────┤
│  실행 계층 (browser/context.py)                     │
│  ├─ CSS Selector 생성 (폴백 계층 구조)              │
│  ├─ Playwright 통합                                 │
│  └─ 에러 복구                                       │
└─────────────────────────────────────────────────────┘
```

**핵심 성능 이슈 및 해결:**

- buildDomTree.js가 Amazon 홈페이지에서 5~6초 소요
- `JSON.stringify()` + `json.loads()` 최적화로 1/10~1/100 단축
- `_clickable_cache`로 다중 패스 직렬화 시 중복 계산 제거

---

### 5.2 natbot 경량 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                    natbot                            │
├─────────────────────────────────────────────────────┤
│  단일 파일 구조 (natbot.py)                          │
│                                                     │
│  BrowserController                                  │
│  ├─ CDP DOMSnapshot.captureSnapshot()               │
│  │   ├─ includeDOMRects: True (좌표 정보)           │
│  │   └─ includePaintOrder: True (렌더링 순서)       │
│  ├─ 요소 필터링 (Python 레벨)                       │
│  │   ├─ 블랙리스트 태그 제거                        │
│  │   ├─ 뷰포트 범위 필터                            │
│  │   └─ 의미 없는 빈 요소 제거                      │
│  ├─ XML 직렬화 (최대 4,500자)                       │
│  │   └─ <tag id=N meta>text</tag> 형식             │
│  └─ 액션 실행                                       │
│      ├─ click(id): page.mouse.click(x, y)           │
│      └─ type(id, text): click → keyboard.type       │
│                                                     │
│  GPT 프롬프트 구조                                  │
│  ├─ 시스템: 명령어 집합 정의                        │
│  │   SCROLL UP/DOWN, CLICK X, TYPE X "TEXT",       │
│  │   TYPESUBMIT X "TEXT"                           │
│  └─ 사용자: 브라우저 콘텐츠 + 목표                  │
└─────────────────────────────────────────────────────┘
```

**natbot의 핵심 가치:** 200줄 내외의 단일 Python 파일로 웹 자동화를 구현한 경량성. 복잡한 ARIA 분석 없이 CDP 좌표 기반 접근으로 단순화.

---

### 5.3 Agent-E 계층적 아키텍처

```
┌─────────────────────────────────────────────────────┐
│                    Agent-E                           │
├─────────────────────────────────────────────────────┤
│  Planner Agent (Autogen 기반)                       │
│  ├─ 사용자 작업 → 부분작업 시퀀스 분해              │
│  ├─ DOM 세부사항과 격리                             │
│  └─ 전체 작업 계획 관리                             │
│          ↓ 부분작업 위임                            │
│  Browser Navigation Agent (fresh 인스턴스)          │
│  ├─ mmid 속성 주입 (전체 DOM 순회)                  │
│  ├─ DOM Distillation 선택                           │
│  │   ├─ text_only: 텍스트 콘텐츠만                 │
│  │   ├─ input_fields: 입력 필드만                  │
│  │   └─ all_fields: 모든 interactive 요소          │
│  ├─ Primitive Skills                               │
│  │   ├─ Get DOM (감지)                             │
│  │   ├─ Click Element (mmid 기반)                  │
│  │   ├─ Enter Text                                 │
│  │   ├─ Open URL                                   │
│  │   └─ Press Keys                                 │
│  └─ Change Observation                             │
│      ├─ MutationObserver: DOM 변화 감지            │
│      ├─ aria-expanded 변화 추적                    │
│      └─ 언어적 피드백 → 다음 추론                  │
│                                                     │
│  실행: Playwright + Autogen 멀티에이전트 프레임워크  │
│  성능: WebVoyager 73.2% (텍스트 전용 최고 성능)     │
└─────────────────────────────────────────────────────┘
```

---

### 5.4 WebArena/WebVoyager DOM 처리 비교

**WebArena (텍스트 접근성 트리):**

```yaml
# observation_type: accessibility_tree
# 에이전트 코드 예시
env_config:
  observation_type: "accessibility_tree"

# 직렬화 형태 (textual accessibility tree)
[1] RootWebArea 'Shopping Cart'
  [2] button 'Checkout' focused: False
  [3] textbox 'Search' value: ''
  [4] link 'Home' url: 'https://...'
```

한계: 복잡한 달력, 상호작용 컴포넌트에서 "매우 복잡하고 장황해짐"

**WebVoyager (비전 주도, DOM 보조):**

```
기본: 스크린샷 기반 (HTML DOM 처리 부담 회피)
보조: GPT-4-ACT JavaScript 도구로 요소 추출
     → 경계 상자에 수치 레이블 자동 부여
텍스트 전용 설정: WebArena textual accessibility tree 사용
성능 차이: 59.1% (비전) vs 40.1% (텍스트 전용)
```

---

## 6. 기술 비교 매트릭스

### 6.1 프로젝트별 DOM 처리 방식 비교

| 프로젝트 | 요소 추출 방식 | 직렬화 형식 | 실행 방식 | Shadow DOM | iframe |
|----------|----------------|-------------|-----------|------------|--------|
| natbot | CDP DOMSnapshot | XML (`<tag id=N>`) | 좌표 기반 클릭 | 미지원 | 블랙리스트 |
| browser-use | JS 주입 (buildDomTree.js) | JSON index map | CSS Selector / backend_node_id | 부분 지원 | 100px 초과만 |
| Playwright MCP | Accessibility Tree API | 역할/레이블 텍스트 | Playwright API | 자동 처리 | Frame 객체 |
| Agent-E | mmid 주입 + distillation | 3종 DOM 표현 | Playwright | 미상 | 미상 |
| WebArena | Accessibility Tree | 들여쓰기 텍스트 | Playwright | 제한적 | 제한적 |
| Vimium | JS 컨텐츠 스크립트 | 힌트 레이블 (a-z) | dispatchMouseEvent | 특수 처리 필요 | 별도 콘텍스트 |

### 6.2 대기 전략 비교

| 전략 | 신뢰성 | 성능 | 적합 상황 |
|------|--------|------|-----------|
| waitForSelector | 높음 | 빠름 | 명확한 결과 지시자 존재 시 |
| networkidle | 보통 | 느림 | 단순 페이지, 분석 스크립트 없을 때 |
| waitForFunction | 높음 | 보통 | 커스텀 조건 필요 시 |
| MutationObserver | 높음 | 빠름 | 실시간 변화 추적 |
| 앱 정의 이벤트 | 최고 | 최고 | 내부 앱 제어 가능 시 |

---

## 7. 아키텍처 다이어그램

### 7.1 DOM 기반 웹 자동화 전체 흐름

```
사용자 자연어 명령
       │
       ▼
┌──────────────┐
│  LLM 에이전트 │ ← 시스템 프롬프트 (명령어 집합 정의)
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────┐
│          페이지 상태 수집 계층            │
│                                          │
│  [A] CDP DOMSnapshot.captureSnapshot()  │
│      └→ 전체 DOM + 좌표 + 렌더링 순서   │
│                                          │
│  [B] JS 주입 (buildDomTree.js)          │
│      └→ interactive 요소 + 속성 주입    │
│                                          │
│  [C] Playwright Accessibility Tree      │
│      └→ role/name/state 구조화 텍스트   │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│          직렬화 & 필터링 계층             │
│                                          │
│  natbot:      XML 4500자 제한            │
│  browser-use: JSON index→node_id map    │
│  Playwright:  2~5KB 접근성 트리          │
│  Agent-E:     mmid 기반 3종 distillation│
└──────────────┬───────────────────────────┘
               │
               ▼
       LLM 추론 (다음 액션 결정)
               │
               ▼
┌──────────────────────────────────────────┐
│          액션 실행 계층                   │
│                                          │
│  JS 주입: element.click() / .value=     │
│  CDP:     Input.dispatchMouseEvent      │
│  Playwright: page.click() / fill()      │
└──────────────┬───────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────┐
│          결과 관찰 계층                   │
│                                          │
│  MutationObserver → DOM 변화 감지        │
│  networkidle / waitForSelector           │
│  aria-expanded 변화 추적 (Agent-E)       │
└──────────────────────────────────────────┘
```

### 7.2 interactive element 판별 의사결정 트리

```
DOM 노드
   │
   ├─ 블랙리스트 태그? (html/head/script/style...) → 제외
   │
   ├─ 뷰포트 외부? → 제외
   │
   ├─ aria-hidden="true"? → 제외
   ├─ aria-disabled="true"? → 제외
   │
   ├─ 네이티브 태그? (button/a/input/select/textarea) → 포함
   │
   ├─ ARIA role이 interactive? 
   │   (button/link/menuitem/checkbox/slider/combobox) → 포함
   │
   ├─ JS click 이벤트 리스너? 
   │   (getEventListeners CDP API - 권한 제한 있음) → 포함
   │
   ├─ onclick 속성 보유? → 포함
   │
   ├─ 검색 키워드 패턴?
   │   (class/id/data-*에 search/magnify/query 포함) → 포함
   │
   ├─ 100px 초과 iframe? → 포함
   │
   └─ 위 조건 미충족 → 제외
```

---

## 8. 결론 및 시사점

### 8.1 비전 없는 DOM 자동화의 핵심 트레이드오프

**토큰 효율성 vs 완전성:**
- 접근성 트리(2~5KB)는 토큰 효율적이지만 복잡한 컴포넌트 처리 어려움
- 전체 HTML은 완전하지만 LLM 컨텍스트 창 초과

**속도 vs 신뢰성:**
- CDP 직접 호출(Puppeteer)이 15~20% 빠르지만 추상화 부재
- Playwright auto-wait가 신뢰성은 높지만 오버헤드 존재

**단순성 vs 커버리지:**
- natbot의 200줄 접근법: 빠른 프로토타이핑 가능, Shadow DOM/iframe 미지원
- browser-use의 정교한 4계층 감지: 커버리지 높지만 5~6초 처리 시간

### 8.2 미해결 과제

1. **Shadow DOM 경로 표현:** XPath는 Shadow DOM 경계 통과 불가. CSS selector 기반 shadow-aware 경로 필요
2. **JS 이벤트 리스너 감지:** `getEventListeners`는 DevTools 권한 필요. `page.evaluate` 컨텍스트 불가 → CDP 우회 필요
3. **동적 SPA 안정성:** `networkidle`의 분석 스크립트 간섭 문제. 결정론적 `waitForFunction` 조합 필요
4. **iframe 중첩 처리:** frame_id 미구현(browser-use Issue #3820). 프레임 계층 정보 구조화 필요

### 8.3 비전 없는 접근의 성능 한계

WebVoyager 벤치마크 기준:
- 비전 포함: 59.1% 성공률
- 텍스트 전용 (접근성 트리): 40.1% 성공률
- Agent-E (텍스트 전용 + DOM distillation + 계층적 계획): 73.2% 성공률

Agent-E의 사례는 올바른 설계(계층적 계획 + 적응형 DOM 표현 + 변화 관찰 피드백)로 텍스트 전용이 비전 방식을 능가할 수 있음을 보여준다.

---

**Sources:**
- [DOM Processing Engine | browser-use/browser-use | DeepWiki](https://deepwiki.com/browser-use/browser-use/2.4-dom-processing-engine)
- [Interactive Element Detection | browser-use/browser-use | DeepWiki](https://deepwiki.com/browser-use/browser-use/5.3-interactive-element-detection)
- [Browser Tools for AI Agents Part 1: Playwright, Puppeteer — DEV Community](https://dev.to/stevengonsalvez/browser-tools-for-ai-agents-part-1-playwright-puppeteer-and-why-your-agent-picked-playwright-k71)
- [WebVoyager: Building an End-to-End Web Agent with Large Multimodal Models — arXiv](https://arxiv.org/html/2401.13919v3)
- [natbot/natbot.py at main · nat/natbot — GitHub](https://github.com/nat/natbot/blob/main/natbot.py)
- [browser-use agent/service.py — GitHub](https://github.com/browser-use/browser-use/blob/main/browser_use/agent/service.py)
- [buildDomTree.js getEventListeners Issue #832 — browser-use GitHub](https://github.com/browser-use/browser-use/issues/832)
- [DOMInteractedElement iframe/Shadow DOM Issue #3820 — browser-use GitHub](https://github.com/browser-use/browser-use/issues/3820)
- [browser-user-highlight-id Issue #745 — browser-use GitHub](https://github.com/browser-use/browser-use/issues/745)
- [Agent-E: From Autonomous Web Navigation to Foundational Design Principles — arXiv](https://arxiv.org/html/2407.13032v1)
- [CDP vs Playwright vs Puppeteer — Lightpanda Blog](https://lightpanda.io/blog/posts/cdp-vs-playwright-vs-puppeteer-is-this-the-wrong-question)
- [Enable link hints for all elements with click listeners PR #1859 — Vimium GitHub](https://github.com/philc/vimium/pull/1859)
- [Scraping React, Vue & Angular SPAs — Browserless](https://www.browserless.io/blog/web-scraping-api-react-vue-angular-spas)
- [GitHub — nat/natbot: Drive a browser with GPT-3](https://github.com/nat/natbot)
- [GitHub — web-arena-x/webarena](https://github.com/web-arena-x/webarena)
- [State-of-the-Art Autonomous Web Agents 2024-2025 — Medium](https://medium.com/@learning_37638/state-of-the-art-autonomous-web-agents-2024-2025-3d9d93a5dde2)
- [Chrome DevTools Protocol - Input domain](https://chromedevtools.github.io/devtools-protocol/tot/Input/)
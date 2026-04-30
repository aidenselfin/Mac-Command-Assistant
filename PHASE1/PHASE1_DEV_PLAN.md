# Phase 1 개발 계획서: 데이터 로거 (Data Logger)

> **프로젝트:** Smart-Homerow: Adaptive Keyboard Interface  
> **담당:** 김주현  
> **작성일:** 2026-04-30  
> **목표:** 마우스 클릭 이벤트를 백그라운드에서 감지하고, 클릭한 앱 이름과 버튼 이름을 로컬 `.csv` 파일에 자동 기록하는 파이썬 스크립트 완성

---

## 0. Phase 1 완료 기준 (Done Criteria)

Phase 1은 아래 3가지를 **모두** 충족했을 때 완료로 간주한다.

1. 터미널에서 `python logger.py` 를 실행하면 백그라운드에서 마우스 클릭 감지가 시작된다.
2. 사용자가 어떤 앱에서 어떤 버튼을 클릭하든, 클릭 직후 0.3초 이내에 `logs/click_log.csv` 에 아래 형식으로 한 줄이 추가된다.
   ```
   2026-04-30 22:45:01,Safari,New Tab Button,mouse_click
   ```
3. Accessibility API가 버튼 이름을 읽지 못한 경우(`None` 반환)에도 스크립트가 죽지 않고 `UNKNOWN` 으로 기록한 뒤 계속 실행된다.

---

## 1. 기술 스택 및 라이브러리 선택

### 1-1. 언어
- **Python 3.11+** (Mac 기본 Python 3이 아닌 brew 또는 pyenv로 설치된 버전 권장)

### 1-2. 핵심 라이브러리

| 라이브러리 | 버전 | 역할 | 설치 명령 |
|---|---|---|---|
| `pynput` | `>=1.7.6` | 마우스 클릭 이벤트 감지 (글로벌 훅) | `pip install pynput` |
| `pyobjc-framework-Cocoa` | `>=9.0` | Mac Accessibility API 접근 (`NSWorkspace` → 현재 앱 이름) | `pip install pyobjc-framework-Cocoa` |
| `pyobjc-framework-ApplicationServices` | `>=9.0` | `AXUIElement` API 호출 → 클릭 좌표의 UI 요소 추출 | `pip install pyobjc-framework-ApplicationServices` |
| `csv` | 표준 라이브러리 | `.csv` 파일 쓰기 | 설치 불필요 |
| `datetime` | 표준 라이브러리 | 타임스탬프 생성 | 설치 불필요 |
| `pathlib` | 표준 라이브러리 | `logs/` 디렉터리 자동 생성 | 설치 불필요 |

> **주의:** `pynput`은 Mac에서 **손쉬운 사용(Accessibility) 권한**을 시스템 환경설정에서 허용해야 동작한다. Phase 1 시작 전 반드시 확인.

### 1-3. 사용하지 않는 라이브러리 (이유 명시)

- `pyautogui` → 화면 제어 목적이라 불필요. 좌표 읽기 성능도 낮음.
- `opencv` → 이미지 기반 감지 방식은 CPU 낭비. PRD 조건 위반.
- `requests`, `httpx` 등 네트워크 라이브러리 → PRD 보안 조건 위반. 절대 사용 안 함.

---

## 2. 프로젝트 디렉터리 구조

```
Adaptive-Keyboard-Interface/
├── logger.py              ← Phase 1 메인 스크립트 (유일한 신규 파일)
├── logs/
│   └── click_log.csv      ← 클릭 기록 저장 파일 (자동 생성)
├── requirements.txt       ← 의존성 목록
├── PRD.md
├── README.md
└── PHASE1_DEV_PLAN.md     ← 이 파일
```

`logs/` 디렉터리와 `click_log.csv`는 `logger.py` 실행 시 자동 생성된다. Git에는 `logs/` 디렉터리 자체는 추적하되 `.csv` 파일은 `.gitignore`에 추가한다.

---

## 3. `.csv` 파일 스키마 정의

파일 경로: `logs/click_log.csv`

| 컬럼 이름 | 타입 | 예시 값 | 설명 |
|---|---|---|---|
| `timestamp` | `DATETIME` (문자열) | `2026-04-30 22:45:01` | 클릭 발생 시각. `YYYY-MM-DD HH:MM:SS` 형식 |
| `app_name` | `STRING` | `Safari` | 클릭 시점에 포커스된 앱의 이름 |
| `element_name` | `STRING` | `New Tab Button` | Accessibility API가 반환한 `AXTitle` 또는 `AXDescription` 값 |
| `action_type` | `STRING` (고정값) | `mouse_click` | Phase 1에서는 항상 `mouse_click` 고정. Phase 4에서 `keyboard_shortcut` 추가 예정 |

**첫 줄(헤더):** `timestamp,app_name,element_name,action_type`

파일이 이미 존재하면 헤더를 다시 쓰지 않고 기존 파일에 이어쓴다(`append` 모드).

---

## 4. 구현 단계별 세부 작업 (Step-by-Step)

### Step 1: 환경 설정 및 권한 확인 (예상 소요 시간: 20분)

**작업 목록:**
1. `pip install pynput pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices` 실행
2. `requirements.txt` 파일 생성 후 아래 내용 기록:
   ```
   pynput>=1.7.6
   pyobjc-framework-Cocoa>=9.0
   pyobjc-framework-ApplicationServices>=9.0
   ```
3. Mac **시스템 환경설정 → 개인 정보 보호 및 보안 → 손쉬운 사용** 에서 사용하는 터미널 앱(예: Terminal, iTerm2, VS Code)을 목록에 추가하고 체크 활성화.
4. 간단한 테스트 코드로 권한 확인:
   ```python
   from Cocoa import NSWorkspace
   app = NSWorkspace.sharedWorkspace().activeApplication()
   print(app['NSApplicationName'])  # 현재 앱 이름 출력되면 권한 OK
   ```

**완료 조건:** 터미널에서 위 테스트 코드 실행 시 현재 포커스된 앱 이름이 출력된다.

---

### Step 2: 현재 활성 앱 이름 읽기 함수 구현 (예상 소요 시간: 30분)

**구현 위치:** `logger.py` 내 `get_active_app_name()` 함수

**동작 방식:**
- `NSWorkspace.sharedWorkspace().activeApplication()` 호출
- 반환 딕셔너리의 `NSApplicationName` 키 값을 리턴
- 호출 실패 시 `"UNKNOWN_APP"` 리턴 (예외 처리 필수)

**코드 스펙:**
```python
def get_active_app_name() -> str:
    """
    현재 포커스된 앱의 이름을 반환한다.
    실패 시 "UNKNOWN_APP"을 반환하며 예외를 외부로 던지지 않는다.
    """
    try:
        from Cocoa import NSWorkspace
        app_info = NSWorkspace.sharedWorkspace().activeApplication()
        return app_info.get('NSApplicationName', 'UNKNOWN_APP')
    except Exception:
        return 'UNKNOWN_APP'
```

**완료 조건:** Safari를 열고 이 함수를 직접 호출하면 `"Safari"` 가 출력된다.

---

### Step 3: 클릭 좌표에서 UI 요소 이름 읽기 함수 구현 (예상 소요 시간: 60분)

> **이 단계가 Phase 1에서 가장 난이도가 높다.** Mac의 Accessibility API를 직접 사용해야 하기 때문이다.

**구현 위치:** `logger.py` 내 `get_element_name_at(x, y)` 함수

**동작 방식:**
1. 마우스 클릭 좌표 `(x, y)` 를 받는다.
2. `ApplicationServices`의 `AXUIElementCopyElementAtPosition(systemWideElement, x, y)` 를 호출하여 해당 좌표에 있는 UI 요소(`AXUIElement`)를 가져온다.
3. 해당 요소의 `AXTitle` 속성을 읽는다. `AXTitle`이 빈 문자열이거나 `None`이면 `AXDescription`을 읽는다. 둘 다 없으면 `"UNKNOWN_ELEMENT"` 리턴.
4. 호출 실패 또는 예외 발생 시 `"UNKNOWN_ELEMENT"` 리턴.

**코드 스펙:**
```python
def get_element_name_at(x: float, y: float) -> str:
    """
    주어진 (x, y) 화면 좌표에 위치한 UI 요소의 이름을 반환한다.
    AXTitle → AXDescription 순으로 시도하며, 둘 다 없으면 "UNKNOWN_ELEMENT" 반환.
    예외 발생 시에도 "UNKNOWN_ELEMENT"를 반환하며 스크립트를 중단하지 않는다.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateSystemWide,
            AXUIElementCopyElementAtPosition,
            AXUIElementCopyAttributeValue,
            kAXTitleAttribute,
            kAXDescriptionAttribute,
        )
        system_element = AXUIElementCreateSystemWide()
        error, element = AXUIElementCopyElementAtPosition(system_element, x, y, None)
        if error != 0 or element is None:
            return 'UNKNOWN_ELEMENT'
        
        # AXTitle 시도
        err_title, title = AXUIElementCopyAttributeValue(element, kAXTitleAttribute, None)
        if err_title == 0 and title:
            return str(title)
        
        # AXDescription 시도
        err_desc, desc = AXUIElementCopyAttributeValue(element, kAXDescriptionAttribute, None)
        if err_desc == 0 and desc:
            return str(desc)
        
        return 'UNKNOWN_ELEMENT'
    except Exception:
        return 'UNKNOWN_ELEMENT'
```

**완료 조건:** Safari의 '새 탭' 버튼 위치 좌표를 직접 넣고 호출하면 `"New Tab"` 또는 이에 상응하는 문자열이 출력된다.

---

### Step 4: `.csv` 파일 기록 함수 구현 (예상 소요 시간: 20분)

**구현 위치:** `logger.py` 내 `write_log(app_name, element_name)` 함수

**동작 방식:**
1. `logs/` 디렉터리가 없으면 `pathlib.Path("logs").mkdir(exist_ok=True)` 로 자동 생성.
2. `logs/click_log.csv` 가 없으면 헤더 행(`timestamp,app_name,element_name,action_type`)을 먼저 쓴 뒤 데이터 행 추가.
3. 파일이 이미 존재하면 헤더 없이 데이터 행만 `append` 모드로 추가.
4. 파일 쓰기는 `csv.writer`를 사용한다. 직접 문자열을 이어붙이는 방식(`+` 연산)은 쉼표나 줄바꿈이 포함된 버튼 이름에서 파일이 깨질 수 있으므로 사용하지 않는다.

**코드 스펙:**
```python
import csv
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "click_log.csv"
CSV_HEADER = ["timestamp", "app_name", "element_name", "action_type"]

def write_log(app_name: str, element_name: str) -> None:
    """
    클릭 이벤트 데이터를 click_log.csv에 한 줄 추가한다.
    logs/ 디렉터리와 헤더 행이 없으면 자동으로 생성한다.
    """
    LOG_DIR.mkdir(exist_ok=True)
    file_exists = LOG_FILE.exists()

    with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            app_name,
            element_name,
            'mouse_click'
        ])
```

**완료 조건:** 함수를 3번 수동 호출하면 `logs/click_log.csv`에 헤더 1행 + 데이터 3행이 정확히 기록된다.

---

### Step 5: 마우스 클릭 이벤트 리스너 구현 및 통합 (예상 소요 시간: 30분)

**구현 위치:** `logger.py` 메인 블록

**동작 방식:**
1. `pynput.mouse.Listener`를 이용해 글로벌 마우스 클릭을 감지한다.
2. 클릭 이벤트 콜백 함수(`on_click`)는 클릭이 **눌려지는 순간**(`pressed=True`)에만 동작한다. 마우스 버튼을 떼는 순간(`pressed=False`)에는 아무것도 하지 않는다.
3. 콜백 내부에서 `get_active_app_name()`과 `get_element_name_at(x, y)`를 순서대로 호출한다.
4. 두 함수의 결과를 `write_log()`에 전달해 기록한다.
5. 콜백 함수 내부에서 발생하는 모든 예외는 `try/except`로 잡아서 출력만 하고 리스너는 계속 실행한다.

**코드 스펙:**
```python
from pynput.mouse import Listener, Button

def on_click(x: float, y: float, button: Button, pressed: bool) -> None:
    """
    마우스 클릭 이벤트 콜백.
    pressed=True (버튼 누름) 순간에만 데이터를 수집하고 기록한다.
    """
    if not pressed:
        return  # 마우스 버튼을 떼는 이벤트는 무시

    try:
        app_name = get_active_app_name()
        element_name = get_element_name_at(x, y)
        write_log(app_name, element_name)
        print(f"[LOG] {app_name} | {element_name}")  # 터미널 실시간 확인용
    except Exception as e:
        print(f"[ERROR] on_click 처리 중 예외 발생: {e}")

if __name__ == '__main__':
    print("Smart-Homerow Data Logger 시작됨. 종료하려면 Ctrl+C 를 누르세요.")
    print(f"로그 저장 위치: {LOG_FILE.resolve()}")
    with Listener(on_click=on_click) as listener:
        listener.join()
```

**완료 조건:** `python logger.py` 실행 후 터미널에 `Smart-Homerow Data Logger 시작됨.` 메시지가 출력되고, 아무 앱의 버튼을 클릭할 때마다 `[LOG] 앱이름 | 버튼이름` 형식의 줄이 출력된다.

---

## 5. 예외 처리 시나리오 전체 목록

Phase 1에서 스크립트가 중단되면 안 되는 상황과 그 처리 방식을 명시한다.

| 상황 | 예외 종류 | 처리 방법 |
|---|---|---|
| 손쉬운 사용 권한 미허용 | `AXError` 또는 `ProcessLookupError` | `get_element_name_at`에서 `"UNKNOWN_ELEMENT"` 리턴, 콘솔 경고 출력 |
| 클릭 위치에 UI 요소 없음 (빈 배경) | `AXUIElementCopyElementAtPosition` error code ≠ 0 | `"UNKNOWN_ELEMENT"` 리턴 |
| 앱이 전환되는 순간 클릭 | `NSWorkspace` 일시적 `None` 반환 | `get_active_app_name`에서 `"UNKNOWN_APP"` 리턴 |
| `logs/` 디렉터리 쓰기 권한 없음 | `PermissionError` | `write_log`에서 예외 출력 후 해당 클릭 기록 skip, 스크립트는 계속 실행 |
| 버튼 이름에 쉼표(`,`) 또는 줄바꿈(`\n`) 포함 | 없음 | `csv.writer`가 자동으로 따옴표로 감싸서 처리 |
| 사용자가 `Ctrl+C` 입력 | `KeyboardInterrupt` | `Listener.join()`이 자연스럽게 종료됨. 추가 처리 불필요 |

---

## 6. 최종 `logger.py` 파일 구조 요약

```python
# logger.py
# 섹션 순서:
# 1. import 문 (표준 라이브러리 → 서드파티 순)
# 2. 상수 정의 (LOG_DIR, LOG_FILE, CSV_HEADER)
# 3. get_active_app_name() 함수
# 4. get_element_name_at(x, y) 함수
# 5. write_log(app_name, element_name) 함수
# 6. on_click(x, y, button, pressed) 콜백 함수
# 7. if __name__ == '__main__': 블록 (Listener 시작)
```

전체 파일 길이는 주석 포함 100줄 이내로 유지한다. 하나의 함수가 하나의 역할만 담당한다.

---

## 7. 테스트 시나리오 (Phase 1 완료 검증)

개발 완료 후 아래 테스트를 **순서대로** 직접 수행한다.

### 테스트 1: 기본 동작 확인
1. `python logger.py` 실행
2. Safari를 열고 주소창 클릭 → 터미널에 `[LOG] Safari | Address and Search Bar` (또는 유사 텍스트) 출력 확인
3. `logs/click_log.csv` 열어서 해당 행이 기록되었는지 확인

### 테스트 2: 앱 전환 중 클릭
1. `python logger.py` 실행
2. Safari → VS Code → Finder 순서로 빠르게 전환하며 각각 클릭
3. `logs/click_log.csv`에서 `app_name` 컬럼이 올바르게 각 앱 이름으로 기록되는지 확인

### 테스트 3: 빈 공간 클릭 (UI 요소 없는 영역)
1. 바탕화면 빈 곳 클릭
2. `element_name` 이 `"UNKNOWN_ELEMENT"` 로 기록되고 스크립트가 죽지 않는지 확인

### 테스트 4: 장시간 실행 안정성
1. `python logger.py` 실행 후 5분 동안 자유롭게 컴퓨터 사용
2. 스크립트가 중단 없이 계속 실행되는지 확인
3. `logs/click_log.csv` 행 수가 실제 클릭 횟수와 동일한지 확인

### 테스트 5: 재실행 후 데이터 누적 확인
1. `python logger.py` 실행 → 클릭 10회 → `Ctrl+C` 종료
2. 다시 `python logger.py` 실행 → 클릭 5회 → 종료
3. `logs/click_log.csv` 열어서 헤더가 1개이고 데이터가 15행인지 확인 (헤더 중복 없음)

---

## 8. `.gitignore` 추가 항목

```gitignore
# Phase 1 로그 파일 (개인 클릭 데이터는 Git에 올리지 않음)
logs/*.csv
```

`logs/` 디렉터리 자체는 추적하기 위해 `logs/.gitkeep` 파일을 추가한다.

---

## 9. Phase 1 → Phase 2 인계 사항

Phase 1이 완료되면 Phase 2에 다음 내용을 인계한다.

- `logs/click_log.csv` 의 스키마 (`timestamp`, `app_name`, `element_name`, `action_type`)
- `action_type` 컬럼은 Phase 1에서는 `mouse_click` 고정이지만, Phase 4에서 `keyboard_shortcut` 값이 추가될 예정임을 Phase 2 개발자가 알아야 한다.
- Phase 2에서 Pandas로 이 파일을 읽을 때 `encoding='utf-8'` 과 `parse_dates=['timestamp']` 옵션을 사용해야 한다.

---

*이 계획서는 Phase 1 구현 중 발견되는 문제에 따라 수정될 수 있습니다.*

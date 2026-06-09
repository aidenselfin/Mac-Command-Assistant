I now have comprehensive data from 15+ sources. Let me synthesize the full report.

---

# 비전 없이 OS 기본 API로 컴퓨터를 조작하는 경량 자동화 프로그램 — 선행 사례 분석 보고서

---

## 목차

1. OS별 UI 요소 식별 방식
   - 1-1. macOS: AXUIElement / NSAccessibility / AppleScript / JXA
   - 1-2. Windows: UIAutomation COM API / MSAA / WinAppDriver
   - 1-3. Linux: AT-SPI / pyatspi2 / xdotool
2. 터미널 제어 구조
3. 경량화 포인트
4. 대표 프로젝트 아키텍처
   - 4-1. Open Interpreter / CAMEL SubprocessInterpreter
   - 4-2. Hammerspoon
   - 4-3. DirectShell (Rust, Windows)
   - 4-4. atomacos / dogtail
5. 이벤트 드리븐 vs 폴링 방식 비교
6. 결론 및 설계 권장사항

---

## 1. OS별 UI 요소 식별 방식

### 1-1. macOS — AXUIElement / NSAccessibility / AppleScript / JXA

#### 핵심 API 구조

macOS의 접근성 API는 **Carbon-era AXUIElement** (C 레벨) 와 **Cocoa NSAccessibility** (Objective-C/Swift 레벨) 두 레이어로 구성된다. 두 레이어 모두 Mac OS X 10.2 이후 안정적으로 유지되었으며, 비전 모델 없이 UI 전체 트리를 직접 읽을 수 있다.

**접근성 권한 활성화 (필수 전제조건)**
```bash
# macOS System Settings > Privacy & Security > Accessibility 에서 앱 권한 부여
# 또는 TCC 데이터베이스 직접 수정 (엔터프라이즈/CI 환경)
```

**Python + pyobjc: 현재 포커스된 창 가져오기**
```python
import ApplicationServices as AS

# 시스템 전체 접근성 객체 획득
system_element = AS.AXUIElementCreateSystemWide()

# 현재 포커스된 앱의 AXUIElement 획득
err, focused_app = AS.AXUIElementCopyAttributeValue(
    system_element,
    AS.kAXFocusedApplicationAttribute,
    None
)

# 포커스된 창 획득
err, focused_window = AS.AXUIElementCopyAttributeValue(
    focused_app,
    AS.kAXFocusedWindowAttribute,
    None
)

# 창 제목
err, title = AS.AXUIElementCopyAttributeValue(
    focused_window,
    AS.kAXTitleAttribute,
    None
)
print("현재 포커스된 창:", title)
```

**버튼 목록 및 텍스트 입력창 추출**
```python
import ApplicationServices as AS
import AppKit

def get_window_elements(bundle_id="com.apple.TextEdit"):
    workspace = AppKit.NSWorkspace.sharedWorkspace()

    # 실행 중인 앱 검색
    running_apps = workspace.runningApplications()
    target_app = next(
        (a for a in running_apps if a.bundleIdentifier() == bundle_id),
        None
    )
    if not target_app:
        return

    # PID로 AXUIElement 생성
    app_elem = AS.AXUIElementCreateApplication(target_app.processIdentifier())

    # 창 목록
    err, windows = AS.AXUIElementCopyAttributeValue(
        app_elem, AS.kAXWindowsAttribute, None
    )

    for window in (windows or []):
        # 자식 요소 재귀 탐색
        err, children = AS.AXUIElementCopyAttributeValue(
            window, AS.kAXChildrenAttribute, None
        )
        for child in (children or []):
            err, role = AS.AXUIElementCopyAttributeValue(
                child, AS.kAXRoleAttribute, None
            )
            err, label = AS.AXUIElementCopyAttributeValue(
                child, AS.kAXTitleAttribute, None
            )
            if role == "AXButton":
                print(f"[Button] {label}")
            elif role == "AXTextField":
                err, value = AS.AXUIElementCopyAttributeValue(
                    child, AS.kAXValueAttribute, None
                )
                print(f"[TextField] value={value}")

# 특정 화면 좌표의 요소 히트테스트
def element_at_position(x, y):
    system = AS.AXUIElementCreateSystemWide()
    err, element = AS.AXUIElementCopyElementAtPosition(system, x, y, None)
    return element
```

**atomacos 라이브러리 (AXUIElement의 Python 래퍼)**
```python
import atomacos

# 앱 실행 및 참조 획득
atomacos.launchAppByBundleId('com.apple.TextEdit')
app = atomacos.getAppRefByBundleId('com.apple.TextEdit')

# 첫 번째 창
window = app.windows()[0]
print(window.AXTitle)  # 'Untitled'

# 버튼 검색 (와일드카드 지원)
close_btn = window.findFirst(AXRole='AXButton', AXTitle='Close')
close_btn.Press()  # 액션 실행

# 모든 버튼 나열
all_buttons = window.findAllR(AXRole='AXButton')
for btn in all_buttons:
    print(btn.AXTitle, btn.AXEnabled)

# 텍스트 필드 값 읽기/쓰기
text_field = window.findFirst(AXRole='AXTextField')
text_field.AXValue = "새 텍스트"
```

**주요 실패 모드 (프로덕션 환경)**

| 오류 코드 | 원인 | 대응 |
|-----------|------|------|
| `kAXErrorAPIDisabled` | 시스템 전체 접근성 비활성화 | System Preferences 수동 활성화 |
| `kAXErrorCannotComplete` | Qt/Python/OpenGL 앱 — AX 트리 없음 | 스크린샷 fallback 또는 AppleScript |
| Stale TCC cache | OS 업데이트 후 캐시 무효화 | `tccutil reset Accessibility` |
| In-process cache desync | AX 트리와 실제 UI 불일치 | 3회 재시도 후 재시작 에스컬레이션 |

#### AppleScript / JXA (JavaScript for Automation)

**AppleScript — 현재 포커스된 앱 및 창**
```applescript
tell application "System Events"
    set frontApp to name of first application process whose frontmost is true
    set windowTitle to name of front window of application process frontApp
end tell
```

**JXA — UI 요소 탐색**
```javascript
// osascript -l JavaScript 로 실행
const SystemEvents = Application('System Events')
const frontApp = SystemEvents.applicationProcesses.whose({frontmost: true})[0]
const windows = frontApp.windows()

windows.forEach(win => {
    const buttons = win.buttons()
    buttons.forEach(btn => console.log('Button:', btn.name()))

    const textFields = win.textFields()
    textFields.forEach(tf => console.log('TextField:', tf.value()))
})
```

**macos-automator-mcp — JXA/AppleScript MCP 서버 (2025)**
```bash
# https://github.com/steipete/macos-automator-mcp
# AppleScript/JXA를 MCP(Model Context Protocol)로 노출
# AI 에이전트가 직접 스크립트를 호출 가능
```

---

### 1-2. Windows — UIAutomation COM API / MSAA

#### Python-UIAutomation-for-Windows

```python
import uiautomation as auto

# 데스크톱 루트 컨트롤
root = auto.GetRootControl()

# 포커스된 창 획득
focused = auto.GetFocusedControl()
print(f"Focused: {focused.Name}, Role: {focused.ControlTypeName}")

# 특정 창 검색
notepad = auto.WindowControl(searchDepth=1, ClassName='Notepad')
if notepad.Exists(timeout=3, retryInterval=1):

    # 텍스트 편집창
    edit = notepad.EditControl()
    edit.GetValuePattern().SetValue('Hello from UIAutomation')

    # 모든 버튼 나열 (재귀)
    buttons = notepad.GetChildren()
    for ctrl in buttons:
        if ctrl.ControlTypeName == 'ButtonControl':
            print(f"[Button] {ctrl.Name}")

    # 키보드 입력
    edit.SendKeys('{Ctrl}a{Del}')
    edit.SendKeys('새 텍스트 입력{Enter}')

    # 창 닫기
    notepad.GetWindowPattern().Close()

# 명령행 UI 트리 덤프
# python automation.py -t 0 -n  (현재 포커스 창 전체 트리 출력)
# python automation.py -r -d 1   (데스크톱 1단계 깊이 창 목록)
```

**COM 레벨 직접 접근**
```python
import comtypes.client

UIAutomationCore = comtypes.client.GetModule("UIAutomationCore.dll")
uia = comtypes.client.CreateObject(
    "{e22ad333-b25f-460c-83d0-0581107395c9}",
    interface=UIAutomationCore.IUIAutomation
)

# 포커스된 요소
focused_elem = uia.GetFocusedElement()
print(focused_elem.CurrentName)
print(focused_elem.CurrentControlType)

# ValuePattern으로 텍스트 설정
value_pattern = focused_elem.GetCurrentPattern(
    UIAutomationCore.UIA_ValuePatternId
)
value_pattern.SetValue("새 값")
```

**이벤트 드리븐: FocusChanged 훅**
```python
import uiautomation as auto

class FocusHandler(auto.IUIAutomationFocusChangedEventHandler):
    def HandleFocusChangedEvent(self, sender):
        print(f"Focus changed to: {sender.CurrentName} ({sender.CurrentControlType})")

handler = FocusHandler()
auto.UIAutomationClient.AddFocusChangedEventHandler(None, handler)
auto.WaitForInputIdle()  # 메시지 루프 실행
```

**MSAA SetWinEventHook (구형 방식)**
```c
// C/Win32 레벨
HWINEVENTHOOK hHook = SetWinEventHook(
    EVENT_OBJECT_FOCUS,    // 포커스 변경
    EVENT_OBJECT_FOCUS,
    NULL,                  // DLL 없음 (out-of-process)
    WinEventProc,          // 콜백
    0, 0,                  // 모든 프로세스/스레드
    WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
);
```

---

### 1-3. Linux — AT-SPI2 / pyatspi2 / xdotool

#### AT-SPI2 아키텍처

AT-SPI2는 D-Bus 위에 구축된 접근성 프로토콜이다. 앱이 접근성 트리를 D-Bus 객체로 노출하고, `libatspi`가 스크린 리더/자동화 도구를 위한 클라이언트 라이브러리를 제공한다.

```
Application → ATK (Accessibility Toolkit) → AT-SPI2 D-Bus Bridge → libatspi → pyatspi2
```

**pyatspi2 기본 패턴**
```python
import pyatspi

# 데스크톱 전체 앱 목록
desktop = pyatspi.Registry.getDesktop(0)
for application in desktop:
    print(f"App: {application.name}")

# 특정 앱의 창 및 요소 탐색
def find_app(name):
    desktop = pyatspi.Registry.getDesktop(0)
    for app in desktop:
        if app.name == name:
            return app
    return None

app = find_app("gedit")
if app:
    for window in app:
        print(f"Window: {window.name}, role={window.roleName}")
        # 모든 자식 요소 탐색
        for child in window:
            print(f"  {child.roleName}: {child.name}")

# 포커스된 요소
focused = pyatspi.Registry.getDesktop(0)
# 이벤트 방식으로 포커스 추적
def on_focus(event):
    source = event.source
    print(f"Focused: {source.name} role={source.roleName}")

pyatspi.Registry.registerEventListener(on_focus, 'object:state-changed:focused')
pyatspi.Registry.start()
```

**dogtail (AT-SPI 고수준 래퍼)**
```python
from dogtail.tree import root
from dogtail.rawinput import typeText, pressKey

# 앱 루트 접근
shell = root.application("gnome-shell")
terminal_app = root.application("gnome-terminal-server")

# 메뉴 항목 탐색
system_menu = shell.child("System", "menu")
system_menu.click()

# 키보드 입력
pressKey("Super")  # Activities 오버뷰
typeText("Terminal")
pressKey("Enter")

# 속성 기반 검색
scroll_bar = terminal_app.findChild(
    lambda x: x.roleName == "scroll bar"
)
scroll_bar.value = scroll_bar.maxValue

# 버튼 찾기
button = terminal_app.findChild(
    lambda x: x.roleName == "push button" and "Close" in x.name
)
button.click()
```

#### xdotool / wmctrl — X11 기반 경량 방식

```bash
# 현재 포커스된 창 ID
xdotool getwindowfocus

# 현재 포커스된 창 이름
xdotool getactivewindow getwindowname

# 모든 창 목록
wmctrl -l
# 출력: 0x04200007  0 user-pc  Firefox

# 특정 창으로 포커스 이동
wmctrl -a "Firefox"

# 키 이벤트 전송 (특정 창)
xdotool key --window $(xdotool search --name "Firefox" | head -1) ctrl+t

# 텍스트 타이핑
xdotool type --clearmodifiers "Hello World"

# xprop으로 창 속성 조회
xprop -id $(xdotool getactivewindow) _NET_WM_NAME WM_CLASS
```

**Python에서 subprocess로 활용**
```python
import subprocess

def get_focused_window_title():
    win_id = subprocess.check_output(
        ["xdotool", "getactivewindow"], text=True
    ).strip()
    title = subprocess.check_output(
        ["xdotool", "getwindowname", win_id], text=True
    ).strip()
    return title

def get_all_windows():
    output = subprocess.check_output(["wmctrl", "-l"], text=True)
    windows = []
    for line in output.strip().split('\n'):
        parts = line.split(None, 3)
        if len(parts) >= 4:
            windows.append({"id": parts[0], "title": parts[3]})
    return windows

print(get_focused_window_title())
print(get_all_windows())
```

> **주의**: xdotool은 X11 전용이며 Wayland에서는 동작하지 않는다. Wayland 환경에서는 AT-SPI2가 필수이다.

---

## 2. 터미널 제어 구조

### 2-1. subprocess + asyncio 기반 백그라운드 명령 실행

#### 기본 패턴: asyncio.create_subprocess_exec

```python
import asyncio

async def run_command(cmd: list[str], timeout: float = 30.0) -> dict:
    """
    백그라운드 명령 실행 + stdout/stderr 캡처
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout
        )
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()  # 좀비 프로세스 방지
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": f"[TIMEOUT after {timeout}s]",
        }
```

#### 실시간 스트리밍 패턴 (AI에게 출력 점진 전달)

```python
import asyncio

async def stream_command(cmd: list[str], on_output, timeout=60.0):
    """
    stdout을 실시간으로 콜백에 스트리밍
    Open Interpreter의 코드 실행 패턴과 유사
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_stream(stream, tag):
        async for line in stream:
            on_output({"tag": tag, "content": line.decode("utf-8")})

    try:
        await asyncio.wait_for(
            asyncio.gather(
                read_stream(proc.stdout, "stdout"),
                read_stream(proc.stderr, "stderr"),
            ),
            timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        on_output({"tag": "error", "content": f"Timeout ({timeout}s)"})

    await proc.wait()
    return proc.returncode

# 사용 예시
async def main():
    def handle_output(chunk):
        print(f"[{chunk['tag']}] {chunk['content']}", end="")

    await stream_command(["python3", "-c", "for i in range(5): print(i)"],
                         on_output=handle_output)
```

### 2-2. CAMEL SubprocessInterpreter 구조 분석

```python
# github.com/camel-ai/camel/blob/master/camel/interpreters/subprocess_interpreter.py
import subprocess
import tempfile
import os

class SubprocessInterpreter:
    """
    코드를 임시 파일에 저장 → subprocess로 실행 → stdout/stderr 캡처
    """

    COMMAND_MAP = {
        "python": ["python3", "{file}"],
        "bash":   ["bash",    "{file}"],
        "r":      ["Rscript", "{file}"],
    }

    def __init__(self, execution_timeout: int = 60,
                 print_stdout: bool = True,
                 print_stderr: bool = True):
        self.execution_timeout = execution_timeout
        self.print_stdout = print_stdout
        self.print_stderr = print_stderr

    def run(self, code: str, language: str,
            require_confirm: bool = False) -> str:
        """메인 실행 진입점"""
        if require_confirm:
            if not self._confirm_execution(code):
                return "[Execution cancelled by user]"

        # 임시 파일 생성
        suffix = {"python": ".py", "bash": ".sh", "r": ".R"}.get(language, ".tmp")
        with tempfile.NamedTemporaryFile(
            mode='w', suffix=suffix, delete=False
        ) as f:
            f.write(code)
            tmp_path = f.name

        try:
            return self._run_file(tmp_path, language)
        finally:
            try:
                os.unlink(tmp_path)
            except PermissionError:
                pass  # Windows 파일 잠금 처리

    def _run_file(self, file_path: str, language: str) -> str:
        cmd_template = self.COMMAND_MAP[language]
        cmd = [c.replace("{file}", file_path) for c in cmd_template]

        env = os.environ.copy()
        if os.name == 'nt':  # Windows PATH 보정
            env["PATH"] = os.path.dirname(sys.executable) + ";" + env["PATH"]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,  # 보안: shell injection 방지
                env=env,
            )
            stdout, stderr = proc.communicate(timeout=self.execution_timeout)

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            stderr = f"[Execution timed out after {self.execution_timeout}s]"
            stdout = ""

        result = stdout
        if stderr:
            result += f"\n(stderr: {stderr})"
        if proc.returncode != 0:
            result += f"\n[Exit code: {proc.returncode}]"

        return result
```

### 2-3. Open Interpreter / 01 프로젝트의 커널 메시지 큐 구조

```python
# github.com/OpenInterpreter/01 kernel.py 구조 재현
import asyncio
import platform
import subprocess
import re

dmesg_proc = None  # 전역 dmesg 프로세스 (중복 방지)

def get_kernel_messages() -> list[str]:
    """플랫폼별 커널 메시지 수집"""
    system = platform.system()

    if system == "Darwin":  # macOS
        result = subprocess.run(
            ["syslog", "-k", "Sender", "kernel"],
            capture_output=True, text=True
        )
        return result.stdout.splitlines()

    elif system == "Linux":
        log_path = _get_dmesg_log_path()
        with open(log_path) as f:
            return f.readlines()

    return []

def _get_dmesg_log_path() -> str:
    global dmesg_proc
    if os.path.exists("/var/log/dmesg"):
        return "/var/log/dmesg"
    if dmesg_proc is None:
        # 실시간 dmesg 스트림을 파일로 tee
        dmesg_proc = subprocess.Popen(
            "dmesg --follow | tee /tmp/dmesg",
            shell=True
        )
    return "/tmp/dmesg"

def custom_filter(messages: list[str]) -> list[str]:
    """AI 에이전트 전용 메시지 파싱"""
    pattern = r"\{TO_INTERPRETER\{(.+?)\}TO_INTERPRETER\}"
    result = []
    for msg in messages:
        match = re.search(pattern, msg)
        if match:
            result.append(match.group(1))
    return result

async def put_kernel_messages_into_queue(
    queue: asyncio.Queue,
    interval: float = 5.0
):
    """커널 메시지를 AI 큐에 주입 (5초 폴링)"""
    while True:
        messages = get_kernel_messages()
        filtered = custom_filter(messages)
        for text in filtered:
            await queue.put({
                "role":    "computer",
                "type":    "console",
                "format":  "output",
                "content": text,
            })
        await asyncio.sleep(interval)
```

---

## 3. 경량화 포인트

### 3-1. 비전 모델 없이 OS 환경 스캔

#### 실행 중인 앱 목록 — 플랫폼별

**macOS (Python)**
```python
import AppKit

def get_running_apps():
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    apps = workspace.runningApplications()
    return [
        {
            "name":       a.localizedName(),
            "bundle_id":  a.bundleIdentifier(),
            "pid":        a.processIdentifier(),
            "active":     a.isActive(),
            "hidden":     a.isHidden(),
        }
        for a in apps
        if a.activationPolicy() == AppKit.NSApplicationActivationPolicyRegular
    ]

def get_menu_bar_state():
    """메뉴바 앱 목록 (background agents)"""
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    return [
        a.localizedName()
        for a in workspace.runningApplications()
        if a.activationPolicy() == AppKit.NSApplicationActivationPolicyAccessory
    ]
```

**Windows (Python)**
```python
import subprocess

def get_running_apps_windows():
    result = subprocess.run(
        ["tasklist", "/fo", "csv", "/nh"],
        capture_output=True, text=True
    )
    apps = []
    for line in result.stdout.strip().split('\n'):
        parts = line.strip('"').split('","')
        if len(parts) >= 2:
            apps.append({"name": parts[0], "pid": parts[1]})
    return apps

# uiautomation으로 포커스된 창 정보
import uiautomation as auto
def get_focused_window():
    ctrl = auto.GetFocusedControl()
    return {
        "name":    ctrl.Name,
        "type":    ctrl.ControlTypeName,
        "class":   ctrl.ClassName,
        "pid":     ctrl.ProcessId,
    }
```

**Linux (Python)**
```python
import subprocess

def get_focused_window_linux():
    win_id = subprocess.check_output(
        ["xdotool", "getactivewindow"], text=True
    ).strip()
    name = subprocess.check_output(
        ["xdotool", "getwindowname", win_id], text=True
    ).strip()
    pid = subprocess.check_output(
        ["xdotool", "getwindowpid", win_id], text=True
    ).strip()
    return {"window_id": win_id, "title": name, "pid": pid}

def get_all_windows_linux():
    output = subprocess.check_output(["wmctrl", "-l"], text=True)
    return [
        {"id": p[0], "title": p[3]}
        for line in output.strip().split('\n')
        if len((p := line.split(None, 3))) >= 4
    ]
```

### 3-2. 메모리/CPU 사용량 최소화 기법

| 기법 | 설명 | 효과 |
|------|------|------|
| **지연 트리 탐색** | 전체 UI 트리를 메모리에 올리지 않고 필요한 노드만 on-demand 쿼리 | 메모리 대폭 절감 |
| **캐시 무효화 전략** | 창 제목/PID 변경 시에만 트리 재탐색 | CPU 사용률 감소 |
| **이벤트 드리븐** | 폴링 대신 OS 이벤트 구독 | 유휴 시 CPU ≈ 0% |
| **직렬화 최소화** | JSON 전체 덤프 대신 `.snap` / `.a11y` 형식 (1–15 KB) | LLM 토큰 절감 |
| **shell=False** | subprocess injection 방지 + 자식 프로세스 오버헤드 감소 | 보안 + 성능 |
| **asyncio gather** | stdout/stderr를 동시에 비동기 읽기 | 데드락 방지 |

---

## 4. 대표 프로젝트 아키텍처

### 4-1. Open Interpreter — computer 모듈 구조

```
open-interpreter/
├── interpreter/
│   ├── core/
│   │   ├── computer/           ← computer 모듈 루트
│   │   │   ├── computer.py     ← Computer 클래스
│   │   │   ├── terminal/
│   │   │   │   ├── terminal.py         ← 터미널 세션 관리
│   │   │   │   ├── languages/
│   │   │   │   │   ├── python.py       ← Python 실행 엔진
│   │   │   │   │   ├── shell.py        ← Shell 실행 엔진
│   │   │   │   │   └── javascript.py
│   │   │   ├── display/        ← 화면 캡처 (선택적)
│   │   │   ├── keyboard.py     ← 키 입력
│   │   │   └── mouse.py        ← 마우스 제어
│   │   └── interpreter.py      ← 메인 인터프리터
```

**computer.run() 의 핵심 흐름**
```python
# 의사 코드 (실제 구조 기반)
class Computer:
    def run(self, language: str, code: str):
        """
        1. 언어별 실행 엔진 선택
        2. 코드를 stdin으로 주입 또는 임시 파일 실행
        3. stdout/stderr 실시간 yield
        4. 실행 결과를 {'type':'console','output':...} 딕셔너리로 반환
        """
        engine = self.terminal.languages[language]

        for chunk in engine.run(code):
            # 스트리밍: 각 줄이 나올 때마다 AI 컨텍스트에 추가
            yield {
                "role":   "computer",
                "type":   "console",
                "format": "output",
                "content": chunk,
            }
```

**01 프로젝트의 asyncio 큐 기반 아키텍처**
```
사용자 음성/입력
    ↓
[WebSocket Server] ←→ [LLM (LiteLLM)]
    ↓
[asyncio.Queue] ← put_kernel_messages_into_queue() (5초 폴링)
    ↓
Computer.run() → subprocess → stdout/stderr
    ↓
[응답 스트리밍]
```

### 4-2. Hammerspoon — Lua 기반 macOS 자동화

```
Hammerspoon
├── Objective-C 코어 (MJAppDelegate.m)  ← macOS API 브리징
│   ├── hs.window      ← NSWindow / AXUIElement 래핑
│   ├── hs.application ← NSRunningApplication
│   ├── hs.hotkey      ← CGEventTap (글로벌 키 후킹)
│   ├── hs.eventtap    ← CGEvent 레벨 입/출력 가로채기
│   └── hs.accessibility ← AXUIElement 직접 접근
└── Lua 스크립팅 레이어  ← ~/.hammerspoon/init.lua
```

**핵심 패턴: 이벤트 드리븐 자동화**
```lua
-- 앱 전환 감지 (이벤트 드리븐)
local appWatcher = hs.application.watcher.new(function(appName, event, app)
    if event == hs.application.watcher.activated then
        print("Activated:", appName)
        -- 접근성 API로 메뉴바 상태 읽기
        local menuBar = app:findMenuItem({"File", "New"})
        if menuBar then
            print("File > New 존재:", menuBar["enabled"])
        end
    end
end)
appWatcher:start()

-- 포커스된 창 접근
local win = hs.window.focusedWindow()
local app = win:application()
print(app:name(), win:title())

-- 접근성 API 직접 사용
local axApp = hs.axuielement.applicationElement(app)
local children = axApp:attributeValue("AXChildren")
for _, child in ipairs(children or {}) do
    print(child:attributeValue("AXRole"), child:attributeValue("AXTitle"))
end

-- CGEventTap으로 글로벌 키 모니터링 (폴링 없음)
local keyWatcher = hs.eventtap.new(
    {hs.eventtap.event.types.keyDown},
    function(event)
        local key = hs.keycodes.map[event:getKeyCode()]
        local mods = event:getFlags()
        print("Key:", key, "Mods:", hs.inspect(mods))
        return false  -- 이벤트 전파 계속
    end
)
keyWatcher:start()
```

**Hammerspoon 경량성 근거**
- 단일 상주 앱 (약 10 MB RAM)
- Lua 스크립트 핫리로드 (재시작 불필요)
- CGEventTap = OS 수준 이벤트 콜백 (폴링 0%)
- macOS API를 Lua에서 직접 호출 → Python 오버헤드 없음

### 4-3. DirectShell — Rust 기반 Windows 경량 자동화 (2026)

**아키텍처 개요**
```
DirectShell (~700KB, pure Rust)
├── UIA 스캐너 (500ms 폴링)
│   ├── 전체 접근성 트리 → SQLite (.db, 100KB–1.5MB)
│   ├── 인터랙티브 요소 → .snap (3–15KB)
│   ├── 컨텍스트 인지 → .a11y (3–10KB)
│   └── LLM 최적화 → .a11y.snap (1–5KB)
└── 액션 큐 (SQL INSERT 기반)
    └── inject 테이블 → UIA ValuePattern / click / key
```

**SQL 액션 큐 예시**
```sql
-- AI 에이전트가 이 SQL을 생성하여 DirectShell에 주입
INSERT INTO inject (action, text, target) VALUES ('text', '사용자이름', 'Username');
INSERT INTO inject (action, text, target) VALUES ('text', '비밀번호',   'Password');
INSERT INTO inject (action, target)       VALUES ('click', 'Login');
INSERT INTO inject (action, text)         VALUES ('key',  'ctrl+s');
```

**토큰 효율성 비교**
```
방식                    토큰/지각     문맥 유지 가능 액션 수
스크린샷 (비전)         1,200–5,000   ~10 회
JSON 전체 트리 덤프     5,000–15,000  ~3 회
DirectShell .a11y.snap  50–200        100+ 회
DirectShell SQL 쿼리    10–50         최대
```

**Chromium 앱 접근성 강제 활성화 (4단계)**
```rust
// Chromium 계열(Chrome, Edge, VS Code, Slack)의 AX 트리 강제 노출
// 1. 시스템 스크린 리더 플래그 설정
// 2. UIA FocusChanged 이벤트 핸들러 누수로 UiaClientsAreListening() 강제 true
// 3. MSAA 렌더러 창 직접 탐침
// 4. 지연 재시도 로직
// 결과: Claude Desktop 요소 수 handful → 11,454개
```

### 4-4. atomacos / dogtail 비교

| 항목 | atomacos (macOS) | dogtail (Linux) |
|------|-----------------|-----------------|
| 기반 API | AXUIElement (pyobjc) | AT-SPI2 (pyatspi2) |
| 설치 | `pip install atomacos` | `pip install dogtail` |
| 권한 | Accessibility 권한 필요 | AT-SPI 활성화 필요 |
| 검색 방식 | `findFirst(AXRole=, AXTitle=)` | `findChild(lambda x: ...)` |
| 재귀 검색 | `findAllR()` (R suffix) | `findChild` + recursive flag |
| 이벤트 | 폴링 기반 | `registerEventListener` |
| 주요 용도 | macOS GUI 테스트 자동화 | GNOME/GTK 앱 자동화 |

---

## 5. 이벤트 드리븐 vs 폴링 방식 비교

| 비교 항목 | 이벤트 드리븐 | 폴링 |
|-----------|-------------|------|
| **CPU 유휴 사용량** | ~0% (콜백 대기) | 지속적 소비 |
| **반응 지연** | 즉각 (ms 단위) | 폴링 간격의 절반 |
| **구현 복잡도** | 높음 (메시지 루프 필요) | 낮음 |
| **확장성** | 수천 이벤트 처리 가능 | 이벤트 폭발 시 처리 지연 |
| **안정성** | 이벤트 누락 가능성 있음 | 상태 항상 확인 가능 |
| **macOS 구현** | CGEventTap, AX 알림 | NSTimer + AXUIElement 쿼리 |
| **Windows 구현** | UIA FocusChangedEvent, SetWinEventHook | 주기적 GetFocusedControl() |
| **Linux 구현** | pyatspi Registry.registerEventListener | while loop + xdotool poll |

**플랫폼별 이벤트 드리븐 구현**

```python
# macOS: AX 알림 구독
import ApplicationServices as AS

def notification_callback(observer, element, notification, user_info):
    err, title = AS.AXUIElementCopyAttributeValue(
        element, AS.kAXTitleAttribute, None
    )
    print(f"[AX 이벤트] {notification}: {title}")

def subscribe_window_events(pid: int):
    app = AS.AXUIElementCreateApplication(pid)
    observer = AS.AXObserverCreate(pid, notification_callback, None)[1]

    for notif in [
        AS.kAXWindowCreatedNotification,
        AS.kAXFocusedWindowChangedNotification,
        AS.kAXUIElementDestroyedNotification,
    ]:
        AS.AXObserverAddNotification(observer, app, notif, None)

    AS.AXObserverGetRunLoopSource(observer)  # RunLoop에 등록
```

```python
# Linux: pyatspi 이벤트 드리븐
import pyatspi

def on_focus_changed(event):
    obj = event.source
    print(f"Focus: {obj.name}, role={obj.roleName}, "
          f"app={obj.application.name}")

def on_window_activated(event):
    win = event.source
    print(f"Window activated: {win.name}")

pyatspi.Registry.registerEventListener(
    on_focus_changed, 'object:state-changed:focused'
)
pyatspi.Registry.registerEventListener(
    on_window_activated, 'window:activate'
)
pyatspi.Registry.start()  # GLib 메인 루프 진입
```

```python
# Windows: UIA 이벤트 드리븐
import uiautomation as auto
import comtypes

class FocusChangedHandler(auto.IUIAutomationFocusChangedEventHandler):
    def HandleFocusChangedEvent(self, sender):
        try:
            print(f"Focus → {sender.CurrentName} "
                  f"[{sender.CurrentControlType}] "
                  f"PID={sender.CurrentProcessId}")
        except comtypes.COMError:
            pass  # 요소 소멸 race condition 처리

uia_client = auto.UIAutomationClient
handler = FocusChangedHandler()
uia_client.AddFocusChangedEventHandler(None, handler)

# 메시지 루프 (이벤트 수신 필수)
import win32gui
while True:
    win32gui.PumpWaitingMessages()
    auto.Wait(0.05)
```

---

## 6. 결론 및 설계 권장사항

### 플랫폼별 최적 기술 스택

| 플랫폼 | 권장 API | Python 라이브러리 | 이벤트 방식 |
|--------|----------|------------------|-------------|
| macOS | AXUIElement | atomacos / pyobjc | AXObserver + CGEventTap |
| Windows | UIAutomation | uiautomation (pip) | UIA FocusChangedEvent |
| Linux X11 | AT-SPI2 | pyatspi2 / dogtail | pyatspi.Registry 이벤트 |
| Linux Wayland | AT-SPI2 (D-Bus) | pyatspi2 | D-Bus signal 구독 |
| 크로스플랫폼 | subprocess + 플랫폼 분기 | — | asyncio 이벤트 루프 |

### 경량 자동화 에이전트 설계 원칙

1. **비전 우선 → API 우선**: 스크린샷 대신 접근성 트리를 1차 정보 소스로 사용 (응답속도 ~100배 향상)
2. **이벤트 드리븐 기본**: 폴링은 최후 수단. macOS CGEventTap, Windows UIA FocusChanged, Linux pyatspi 이벤트 활용
3. **직렬화 최소화**: 전체 트리 JSON 덤프 대신 역할(role)/이름(name)/상태(enabled)만 추출하여 LLM에 전달
4. **타임아웃 필수**: 모든 subprocess 실행에 `timeout` 파라미터 적용, TimeoutExpired 시 `proc.kill()` → `proc.communicate()`
5. **shell=False 원칙**: subprocess injection 방지 및 자식 프로세스 격리
6. **플랫폼 분기 최소화**: `platform.system()` 한 곳에서 분기, 나머지는 공통 인터페이스

---

**참고 소스**

- [macOS Accessibility Automation: Four Production Failure Modes](https://fazm.ai/t/macos-accessibility-automation)
- [Parsing macOS Application UI: Techniques and Tools](https://research.macpaw.com/publications/how-to-parse-macos-app-ui)
- [Python-UIAutomation-for-Windows (GitHub)](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)
- [AT-SPI2 (freedesktop.org)](https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/)
- [pyatspi2 (GNOME GitLab)](https://gitlab.gnome.org/GNOME/pyatspi2)
- [Open Interpreter (GitHub)](https://github.com/openinterpreter/open-interpreter)
- [OpenInterpreter/01 kernel.py](https://github.com/OpenInterpreter/01/blob/main/software/source/server/utils/kernel.py)
- [CAMEL SubprocessInterpreter (GitHub)](https://github.com/camel-ai/camel/blob/master/camel/interpreters/subprocess_interpreter.py)
- [Hammerspoon (공식 사이트)](https://www.hammerspoon.org/)
- [Hammerspoon Getting Started](https://www.hammerspoon.org/go/)
- [DirectShell: Accessibility Layer as Universal App Interface](https://dev.to/tlrag/-directshell-i-turned-the-accessibility-layer-into-a-universal-app-interface-no-screenshots-no-2457)
- [atomacos Documentation](https://daveenguyen.github.io/atomacos/readme.html)
- [atomacos (GitHub)](https://github.com/timurco/atomacos)
- [Automation through Accessibility — Fedora Magazine](https://fedoramagazine.org/automation-through-accessibility/)
- [automating-macOS-with-JXA (GitHub)](https://github.com/josh-/automating-macOS-with-JXA-presentation)
- [macos-automator-mcp (GitHub)](https://github.com/steipete/macos-automator-mcp)
- [macOS AI Agent: How Desktop Agents Work in 2026](https://fazm.ai/blog/macos-ai-agent)
- [Mac vs Windows for AI Desktop Automation](https://fazm.ai/t/mac-vs-windows-ai-desktop-automation)
- [SetWinEventHook — Microsoft Learn](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook)
- [xdotool Linux Command Library](https://linuxcommandlibrary.com/man/xdotool)
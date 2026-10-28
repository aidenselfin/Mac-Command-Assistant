# web·macOS MCP 보관함

웹(Playwright)과 macOS 활성 앱 MCP를 앱에서 끈 뒤 코드를 여기에 보관했다. pytest·ruff는 이 폴더를 보지 않는다.

- `voice_action/playwright_client.py`, `voice_action/macos_client.py`: 클라이언트 원본
- `tests/`: 두 클라이언트의 테스트와, `test_main`·`test_ui`에서 뺀 조각(`.txt`)
- git 태그 `archive/web-macos-mcp-before-removal`: 끄기 직전 전체 코드

## 다시 켜는 법
1. 두 클라이언트를 `voice_action/`로, 테스트를 `tests/`로 옮긴다.
2. `main.py`의 `_build_mcp_manager`와 `voice_action/ui.py`의 `_manager_factory`에 `"web": PlaywrightMCPClient(config)`, macOS에서는 `"macos": MacOSAXClient()`를 다시 등록한다.
3. `test_main`·`test_ui`의 `.txt` 조각을 되돌리고 `test_integration_filesystem.py`의 자식 프로세스 수를 2로 바꾼다.
4. `npm run playwright:install`로 Chromium을 받는다.

플래너의 웹 정책(`planner.py`)과 `risk_guard.py`의 도구 분류는 지우지 않았다. 웹 도구가 연결된 때만 켜진다(`Planner._web_enabled`).

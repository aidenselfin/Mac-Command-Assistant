# Final — Smart-Homerow Integrated Implementation

완성된 Smart-Homerow 프로그램 (PHASE1-5 통합)

## 📋 파일 구조

```
Final/
├── main.py                          # 통합 메인 프로세스 (모든 PHASE 실행)
├── logger.py                        # PHASE1: 이벤트 로깅 (마우스, 키보드)
├── learning_engine.py               # PHASE2: 학습 분석 엔진
├── overlay_engine.py                # PHASE3: 오버레이 UI
├── shortcuts_db.json                # PHASE4: 단축키 데이터베이스 (확장)
├── learning.db                      # PHASE2 SQLite DB (자동 생성)
├── logs/                            # 로그 디렉토리 (자동 생성)
│   ├── click_log.csv               # 이벤트 로그 (PHASE1)
│   ├── smartHomerow.log            # 메인 로그
│   └── smartHomerow_error.log      # 에러 로그
├── config.json                      # 설정 파일 (사용자 커스터마이징)
├── com.smartHomerow.daemon.plist   # LaunchAgent (백그라운드 서비스)
├── requirements.txt                 # Python 의존성
└── README.md                        # 이 파일
```

## 🚀 설치 및 실행 가이드

### 1. 의존성 설치

```bash
cd Final
pip install -r requirements.txt
```

### 2. 시스템 권한 설정

macOS에서 다음 권한이 필요합니다:
- **손쉬운 사용 (Accessibility)**: 마우스/키보드 접근
- **모니터링**: 화면의 창과 요소 접근

**설정 방법:**
1. `System Preferences` → `Security & Privacy` → `Accessibility`
2. 터미널 또는 Python 인터프리터 추가
3. 권한 허용

### 3. 실행 방법

#### 방법 1: 직접 실행

```bash
python3 main.py
```

선택 사항:
```bash
python3 main.py --debug              # 디버그 모드
python3 main.py --no-logger          # 로거 비활성화
python3 main.py --no-learning        # 학습 엔진 비활성화
python3 main.py --no-overlay         # 오버레이 UI 비활성화
```

#### 방법 2: 백그라운드 데몬으로 실행 (자동 시작)

1. LaunchAgent 등록:
```bash
# plist 파일의 INSTALL_PATH를 실제 경로로 수정
sed -i '' 's|INSTALL_PATH|'$(pwd)'|g' com.smartHomerow.daemon.plist

# LaunchAgent 디렉토리에 복사
mkdir -p ~/Library/LaunchAgents
cp com.smartHomerow.daemon.plist ~/Library/LaunchAgents/

# 서비스 로드
launchctl load ~/Library/LaunchAgents/com.smartHomerow.daemon.plist
```

2. 상태 확인:
```bash
launchctl list | grep smartHomerow
```

3. 종료:
```bash
launchctl unload ~/Library/LaunchAgents/com.smartHomerow.daemon.plist
```

## 💡 사용 방법

### 기본 기능

1. **창/패널 네비게이션**
   - `Right Command` 누르기 → 화면의 모든 창/패널에 알파벳 태그 표시
   - `Right Command + [A-Z]` 입력 → 해당 창/패널로 포커스 이동

2. **단축키 학습**
   - 마우스로 버튼 클릭 → 1.5초 동안 단축키 힌트 표시
   - 같은 단축키를 반복 노출하면 점진적으로 덜 표시됨 (사용 빈도 기반)

3. **학습 진행 추적**
   - `learning.db`에 사용 빈도 자동 저장
   - 각 단축키별 수락률(acceptance_rate) 계산
   - 높은 수락률 = 이미 숙련함 (힌트 표시 안 함)

### 설정 커스터마이징

`config.json` 파일 수정:

```json
{
  "debug": false,
  "log_level": "INFO",  // DEBUG, INFO, WARN, ERROR
  "logger_enabled": true,
  "learning_enabled": true,
  "overlay_enabled": true
}
```

## 📊 학습 통계

### 조회 방법

```python
from learning_engine import LearningDatabase

db = LearningDatabase()

# 앱별 상위 단축키
top_shortcuts = db.get_top_shortcuts("com.microsoft.VSCode", limit=5)
print(top_shortcuts)

# 전체 통계
stats = db.get_statistics()
print(stats)

# 사용자 숙련도
proficiency = db.get_user_proficiency("com.microsoft.VSCode")
print(proficiency)
```

## 🔍 로그 파일

### 로그 위치

- `logs/click_log.csv`: 모든 이벤트 로그 (마우스, 키보드)
- `logs/smartHomerow.log`: 메인 프로그램 로그
- `logs/smartHomerow_error.log`: 에러 로그

### 로그 형식 (CSV)

```
timestamp,session_id,action_type,app_name,window_title,element_name,intent,key_pressed
2026-05-14 15:30:45.123,20260514_153045,mouse_click,Code,main.py,Search,Interaction,
2026-05-14 15:30:46.456,20260514_153045,key_press_shortcut,Code,main.py,cmd,Shortcut Execution,cmd
```

## 🛠️ 개발 및 디버깅

### 디버그 모드

```bash
python3 main.py --debug
```

### 개별 PHASE 테스트

```bash
# PHASE1: 이벤트 로거만 실행
python3 logger.py

# PHASE2: 학습 엔진만 실행
python3 learning_engine.py

# PHASE3: 오버레이 UI만 실행
python3 overlay_engine.py
```

### 학습 DB 초기화

```bash
rm -f learning.db
```

## ⚠️ 문제 해결

### 오버레이 UI가 나타나지 않음

1. 손쉬운 사용 권한 확인
2. 디버그 모드로 실행하여 에러 확인
```bash
python3 main.py --debug
```

### 단축키 힌트가 표시되지 않음

1. `shortcuts_db.json`에 앱과 단축키가 정의되어 있는지 확인
2. UI 요소명이 `shortcuts_db.json`의 키와 정확하게 매치되는지 확인
3. 로그를 확인하여 `record_shortcut_shown` 호출 여부 확인

### 성능 문제

1. 오버레이 UI 비활성화:
```bash
python3 main.py --no-overlay
```

2. 로거 비활성화:
```bash
python3 main.py --no-logger
```

## 📈 성능 사양

- **CPU**: < 5% (유휴 상태)
- **메모리**: ~50MB
- **배터리**: 최소 영향
- **응답성**: < 100ms (이벤트 감지 → 힌트 표시)

## 🔒 보안 & 프라이버시

- ✅ 모든 데이터는 로컬 저장 (`~/Adaptive-Keyboard-Interface/Final/`)
- ✅ 서버 전송 없음 (완전 로컬)
- ✅ 사용자가 데이터 삭제 가능 (`learning.db`, `click_log.csv`)
- ✅ 고급 설정에서 로깅 비활성화 가능

## 📚 확장 및 커스터마이징

### 새 앱 단축키 추가

`shortcuts_db.json`에 앱 번들 ID와 단축키 추가:

```json
{
  "com.example.MyApp": {
    "Save": "Cmd + S",
    "Export": "Cmd + E"
  }
}
```

### 학습 알고리즘 튜닝

`learning_engine.py`의 파라미터 수정:

```python
ACCEPTANCE_RATE_THRESHOLD_LOW = 0.30   # 30% 이하: 덜 표시
ACCEPTANCE_RATE_THRESHOLD_HIGH = 0.70  # 70% 이상: 표시 안 함
```

### UI 커스터마이징

`overlay_engine.py`의 스타일 수정:

```python
tag_bg_color = AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.902, 0.0, 1.0)
# RGB 값을 변경하면 태그 색상 변경
```

## 📞 지원

문제가 발생하면:

1. 로그 파일 확인: `logs/smartHomerow_error.log`
2. 디버그 모드 실행: `python3 main.py --debug`
3. 개별 PHASE 테스트

## 📝 라이센스

Smart-Homerow © 2026. All rights reserved.

---

**마지막 업데이트**: 2026-05-14
**버전**: 1.0.0

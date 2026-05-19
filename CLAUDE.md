# Smart-Homerow — Claude Code 가이드

## 실행 방법

```bash
cd Final && bash run_macos.sh
```

> 처음 실행 시 자동으로 `.venv` 생성 + 의존성 설치 후 앱이 시작됩니다.

## 터미널 명령어 설치 (선택)

```bash
cd Final && bash install.sh
```

설치 후 어디서든 `smart-homerow` 명령어 사용 가능:

```bash
smart-homerow start    # 앱 실행
smart-homerow stop     # 종료
smart-homerow status   # 상태 확인
smart-homerow log      # 로그 보기 (tail -f)
smart-homerow debug    # 디버그 모드
```

## 프로젝트 구조

```
Final/
├── main.py              # 메인 진입점 (PHASE1~3 통합)
├── overlay_engine.py    # PHASE3: macOS 오버레이 UI + 단축키 힌트 뱃지
├── learning_engine.py   # PHASE2: 학습/분석 엔진
├── logger.py            # PHASE1: 마우스·키보드 이벤트 로거
├── shortcuts_db.json    # 앱별 단축키 DB (37개 앱, 844개 단축키)
├── run_macos.sh         # 가상환경 자동 설정 + 실행 스크립트
└── install.sh           # smart-homerow 터미널 명령어 설치
```

## 필요 권한

- **손쉬운 사용(Accessibility)**: 키보드·마우스 이벤트 감지 필수
- 시스템 환경설정 → 개인 정보 보호 및 보안 → 손쉬운 사용 → 터미널/Python 추가

## 개발 참고

- Python 3.11+ 권장 (Apple CLT python3 는 PyObjC 미지원 → Homebrew python 사용)
- 단축키 DB 확장: `Final/shortcuts_db.json` 에 앱 번들 ID 기준으로 추가
- 버튼 힌트 스캔은 백그라운드 스레드에서 5초 디바운스로 실행됨

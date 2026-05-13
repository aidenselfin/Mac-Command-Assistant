# ⌨️ Smart-Homerow: Adaptive Keyboard Interface

Adaptive Keyboard Interface는 사용자의 클릭 패턴과 UI 컨텍스트를 학습하여, 화면에 있는 모든 요소 대신 **현재 가장 유의미한 기능에만 단축키 힌트를 노출**하는 Mac용 보조 인터페이스입니다.

## 현재 구현 상태

- `PHASE1/logger.py`: 마우스 클릭, 앱 전환 등을 로컬 `PHASE1/logs/click_log.csv`에 기록합니다.
- `PHASE3/overlay_engine.py`: 현재 화면에서 실행 중인 창과 분할 패널을 실시간 스캔합니다.
- `Right Command + [알파벳]` 입력으로 선택한 창/패널에 포커스를 이동합니다.
- 마우스 클릭 시 `PHASE3/shortcuts_db.json`에서 매칭되는 단축키를 찾아 **1.5초 동안 화면에 힌트 태그**로 표시합니다.

## 주요 기능

- **창/패널 기반 오버레이:** 화면 내 창과 분할 패널을 각각 `A`, `S`, `D` 등 알파벳 태그로 표시합니다.
- **키보드 포커스 이동:** 오버레이 활성화 후 알파벳 키를 입력하면 해당 창/패널로 즉시 이동합니다.
- **학습 유도 힌트:** 마우스 클릭 시 단축키가 존재하면 화면에 임시 힌트를 렌더링하여 키보드 전환을 유도합니다.
- **로컬 단축키 DB:** `PHASE3/shortcuts_db.json`에 VS Code와 Chrome 단축키 매핑을 유지합니다.

## 사용 방법

1. Mac 시스템 환경설정 > 개인정보 보호 및 보안 > 손쉬운 사용에서 터미널 또는 Python 실행 환경에 접근 권한을 부여합니다.
2. `python PHASE3/overlay_engine.py`를 실행합니다.
3. `Right Command`를 누른 뒤 알파벳 키를 입력해 창/패널로 이동합니다.
4. 마우스로 클릭하면 로컬 DB에서 단축키가 있으면 잠시 힌트가 표시됩니다.

## 문서 구성

- `README.md`: 프로젝트 개요 및 현재 구현 상태
- `PRD.md`: 제품 요구사항 및 현재 개발 현황
- `dev_plan.md`: 프로젝트 전체 개발 계획 (수정 금지)
- `PHASE1/PHASE1_DEV_PLAN.md`: Phase 1 데이터 로깅 계획
- `PHASE3/`: 오버레이 엔진 소스 및 로컬 단축키 DB

## 정리된 문서

중복되거나 현재 내용과 맞지 않는 문서(`implementation_plan.md`, `task.md`, `walkthrough.md`, `프로그램 설명.md`, `PHASE3/ph3_PRD.md`)는 삭제하여 프로젝트 문서 수를 줄였습니다.

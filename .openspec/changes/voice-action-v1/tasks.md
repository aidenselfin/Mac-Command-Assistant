# Tasks: Voice-Action AI v1.0

> 상태: DONE  
> 작성일: 2026-06-09  
> 구현 순서: Phase 1 → 2 → 3 → 4

---

## Phase 1 — 핵심 파이프라인 (백엔드)

목표: UI 없이 CLI로 전체 파이프라인 동작 확인

### 1-1. scanner.py

- [x] `scan_directory(root, include_hidden) -> str` 구현
  - [x] `rglob("*")` 순회, 파일만, EXCLUDE_DIRS/FILES 필터링
  - [x] mtime 최신순 정렬 후 MAX_FILES=500 슬라이싱
  - [x] `path,size,mtime,sha8` CSV 포맷 출력
  - [x] `.fs.snap` 헤더 (root, scanned_at, files 수) 포함
- [x] `_sha256_prefix(p) -> str` 구현 (64KB 청크 읽기)
- [x] `_resolve_dst_conflict(dst) -> Path` 구현 (`_1`, `_2` 접미사)
- [x] `SnapCache` dataclass 정의
- [x] `get_snap(root, include_hidden) -> str` 구현
  - [x] mtime dict 비교 → 변경 없으면 캐시 반환
  - [x] 변경 있으면 재스캔 후 캐시 갱신

**검증**: `~/Documents`, `~/Downloads`, `~/Desktop` 3곳 스캔 → `.fs.snap` 포맷 출력 확인

---

### 1-2. llm.py

- [x] `SYSTEM_PROMPT` 상수 정의 (역할 + 포맷 + 제약 + few-shot 2개)
- [x] `call_llm(voice_text, fs_snap) -> list[dict]` 구현
  - [x] `anthropic.Anthropic()` 클라이언트 초기화
  - [x] `model="claude-haiku-4-5"`, `max_tokens=1024`
  - [x] 시스템 프롬프트에 `cache_control: {"type": "ephemeral"}` 적용
  - [x] 유저 메시지: `<snapshot>...<command>...` 포맷
  - [x] `json.loads()` 파싱
  - [x] 실패 시 정규식 `\[.*\]` 재추출 (re.DOTALL)
  - [x] `action` 값 화이트리스트 필터링 (move/rename/delete 외 제거)

**검증**: 실제 `.fs.snap` + 샘플 명령 → JSON 배열 반환, 비용 $0.001 미만 확인

---

### 1-3. executor.py

- [x] `execute_plan(actions, log_path) -> list[dict]` 구현
  - [x] `move`: `dst.parent.mkdir(parents=True)` + `shutil.move()`
  - [x] `rename`: `src.rename(dst)`
  - [x] `delete`: `_move_to_trash()` 경유 (os.unlink 금지)
  - [x] 각 액션 try/except: FileNotFoundError, PermissionError, Exception
  - [x] 결과 로그 `{**action, "status": "ok"/"error"}` 축적
- [x] `_move_to_trash(p) -> None` 구현
  - [x] `~/.Trash / p.name` 경로 충돌 시 `_resolve_dst_conflict()` 적용
  - [x] `shutil.move()` 로 Trash 이동
- [x] 로그 파일 저장 (`~/.voice-action/logs/<ISO_TIMESTAMP>.json`)

**검증**: 테스트 파일 3개 move/delete → 로그 파일 생성 확인

---

## Phase 2 — 입력 레이어 (오디오 + STT)

### 2-1. audio.py

- [x] `AudioRecorder` 클래스 구현
  - [x] `.start()`: `sounddevice.InputStream` 시작, numpy array 축적
  - [x] `.stop() -> bytes`: 스트림 종료, WAV bytes 반환
  - [x] `wave` 모듈로 WAV 변환 (16kHz mono)

**검증**: 3초 녹음 후 WAV 재생 확인

---

### 2-2. stt.py

- [x] `transcribe(wav_bytes) -> str` 구현
  - [x] `faster-whisper` WhisperModel("small") 초기화
  - [x] `language="ko"` 설정
  - [x] 결과가 빈 문자열이면 `SpeechRecognitionError` 발생

**검증**: "다운로드 폴더 정리해줘" 발화 → 80% 이상 인식 정확도 확인

---

### 2-3. main.py — 핫키 연결

- [x] `pynput.keyboard.Listener` 설정
  - [x] `on_press`: cmd_r 감지 → `recorder.start()`
  - [x] `on_release`: cmd_r 감지 → `recorder.stop()` + `asyncio.run(pipeline(wav))`
- [x] `pipeline(wav_bytes)` async 함수 구현
  - [x] `asyncio.gather(transcribe(wav), gather_snaps([...]))` 병렬 실행
  - [x] LLM 호출 → 미리보기 → 실행
- [x] 핫키 리스너 별도 스레드 실행 (`daemon=True`)
- [x] `--test "명령어"` CLI 플래그 지원 (오디오 없이 파이프라인 테스트)

**검증**: R-cmd hold/release → STT 텍스트 터미널 출력 확인

---

## Phase 3 — UI 레이어

### 3-1. ui/preview.py

- [x] `show_preview(actions, truncated=False) -> bool` 구현
  - [x] `tkinter.Toplevel` 별도 스레드에서 실행
  - [x] `move`/`rename` 항목: 일반 텍스트 렌더링
  - [x] `delete` 항목: 빨간색 강조 + "⚠️" 경고 문구
  - [x] delete 포함 시 경고 배너 표시
  - [x] MAX_FILES 초과 시 `⚠️ 최신 500개만 분석했습니다 (전체 N개)` 표시
  - [x] [실행] / [취소] 버튼
  - [x] ESC 바인딩 → 취소
  - [x] 외부 클릭(`<FocusOut>`) → 취소

---

### 3-2. ui/onboarding.py

- [x] `show_onboarding(permissions: dict) -> None` 구현
  - [x] 권한별 ✅/❌ 체크마크 표시
  - [x] `[시스템 설정→]` 버튼: `subprocess.run(["open", "x-apple.systempreferences:..."])`
  - [x] `[재시작]` 버튼: `os.execv(sys.executable, [sys.executable] + sys.argv)`
  - [x] API 키 미설정 시 입력 필드 + [저장] 버튼 추가

---

## Phase 4 — 설정 + 권한

### 4-1. config.py

- [x] `Config` dataclass 또는 dict 정의
  - [x] `default_scan_dirs`, `whisper_model`, `anthropic_api_key` 필드
- [x] `load_config() -> Config` 구현 (`~/.voice-action/config.json` 읽기)
- [x] `save_config(config) -> None` 구현
- [x] 환경변수 `ANTHROPIC_API_KEY` 우선 적용 로직

---

### 4-2. permissions.py

- [x] `check_permissions() -> dict[str, bool]` 구현
  - [x] `microphone`: `AVCaptureDevice` 인증 상태 확인
  - [x] `full_disk`: `~/Library/Application Support` 쓰기 테스트
  - [x] `accessibility`: `AXIsProcessTrusted()` 호출
- [x] 앱 시작 시 호출 → 하나라도 False면 온보딩 패널 표시

---

## Phase 4 완료 후 통합 검증

- [x] R-cmd → 음성 → 미리보기 → [실행] → 완료 배너 전체 플로우 수동 실행
- [x] delete 포함 시 confirm gate 동작 확인
- [x] Trash 이동 확인 (영구 삭제 없음)
- [x] 롤백: Trash에서 파일 복원 가능 확인
- [x] 로그 파일 생성 확인
- [x] 전체 지연 10초 이내 측정

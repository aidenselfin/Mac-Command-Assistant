# Design: Voice-Action AI v1.0

> 상태: PROPOSED  
> 작성일: 2026-06-09

---

## 아키텍처: 3단계 파이프라인

```
[Input]   R-cmd 누름 → sounddevice 녹음
          R-cmd 뗌   → 녹음 버퍼 확정
            ↓
[Think]   ┌─ faster-whisper STT (small, ko)    ─┐
          └─ 3곳 동시 선스캔 (.fs.snap 생성)     ─┘ asyncio.gather 병렬
          → 시스템 프롬프트(캐시) + .fs.snap + 음성 텍스트
          → Claude Haiku 단일 호출 → JSON 액션 플랜
            ↓
[Act]     미리보기 패널 → 사용자 확인
          → File Executor → 실행 로그 저장
```

---

## 파일 구조

```
voice-action/
├── main.py          # 진입점: 핫키 루프 + asyncio pipeline
├── audio.py         # sounddevice 녹음 + WAV 변환
├── stt.py           # faster-whisper STT
├── scanner.py       # 파일 스캔 + .fs.snap 생성 + mtime 캐시
├── llm.py           # Claude Haiku 호출 + JSON 파싱
├── executor.py      # 액션 실행 + Trash 이동 + 로그
├── config.py        # ~/.voice-action/config.json 읽기/쓰기
├── permissions.py   # 마이크·디스크·손쉬운사용 권한 확인
├── ui/
│   ├── preview.py   # 미리보기 패널 (tkinter)
│   └── onboarding.py# 권한 확인 + API 키 설정 패널
└── requirements.txt
```

---

## 모듈별 설계 결정

### scanner.py

**포맷 선택: .fs.snap (CSV-like 텍스트)**

전체 JSON 트리 대신 최소 필드만 포함한 줄 구분 텍스트.
~300 토큰 (파일 30개 기준) vs. JSON 트리 5,000~15,000 토큰.

```
root: /Users/juheon/Downloads
scanned_at: 2026-06-09T14:32:01
files: 31

path,size,mtime,sha8
report_final.pdf,245760,2026-06-08,a1b2c3d4
report_copy.pdf,245760,2026-06-07,a1b2c3d4
```

- `sha8`: SHA256 앞 8자리 — 중복 감지 전용
- `mtime`: YYYY-MM-DD만 (시간 제외 — 토큰 절약)
- `MAX_FILES=500`: 초과 시 mtime 최신순 상위 500개만

**캐시 전략: mtime dict 비교**

```python
@dataclass
class SnapCache:
    snap_text: str
    file_mtimes: dict[str, float]  # {상대경로: mtime}
```

폴더의 파일 mtime 집합이 동일하면 재스캔 없이 캐시 반환.

**스캔 대상: 항상 3곳 고정** (폴더 힌트 감지 없음)
- `~/Documents`, `~/Downloads`, `~/Desktop`

**제외 규칙**
- EXCLUDE_DIRS: `.git`, `node_modules`, `.Trash`, `__pycache__`
- EXCLUDE_FILES: `.DS_Store`, `Thumbs.db`, `desktop.ini`
- 숨김 파일 기본 제외 (`include_hidden=False`)

---

### llm.py

**모델**: `claude-haiku-4-5` — 단일 호출, JSON 응답

**프롬프트 캐싱**: 시스템 프롬프트 전체에 `cache_control: {"type": "ephemeral"}` 적용
- 캐시 TTL 5분 → 연속 명령 세션에서 시스템 프롬프트 비용 90% 절감
- 캐시 히트 시 비용 ~$0.0003/호출

**시스템 프롬프트 구조** (~800 토큰, 캐시 대상)
```
역할 정의 → 출력 포맷 명세 → 제약 → few-shot 예시 2개
```

**출력 포맷**
```json
[
  {"action": "move",   "src": "<절대경로>", "dst": "<절대경로>", "reason": "<한 줄>"},
  {"action": "rename", "src": "<절대경로>", "dst": "<절대경로>", "reason": "<한 줄>"},
  {"action": "delete", "src": "<절대경로>",                     "reason": "<한 줄>"}
]
```

**파싱 실패 처리 (3단계)**
1. `json.loads()` 실패 → 정규식 `\[.*\]` 재추출
2. 재시도 실패 → "명령을 이해하지 못했습니다" 에러 UI
3. `action` 값이 허용 목록 외 → 해당 항목만 제거 후 나머지 실행

---

### executor.py

**액션 분류**

| 액션 | 처리 |
|------|------|
| `move`, `rename` | 미리보기 표시 후 즉시 실행 가능 |
| `delete` | [실행] 클릭 필수 (confirm gate) |

**영구 삭제 금지**: 모든 delete는 `_move_to_trash()` 경유
- `~/.Trash` 직접 이동 (`os.unlink()` 호출 금지)
- Trash 내 동일 파일명 충돌 시 `_resolve_dst_conflict()` 적용

**파일명 충돌 처리**

| 상황 | 처리 |
|------|------|
| `move` 목적지 충돌 | `_1`, `_2` 접미사 붙여 이동 |
| `rename` 목적지 충돌 | 에러 처리 — 해당 항목만 건너뜀 |
| `delete` 대상 없음 | 조용히 건너뜀 |

**실행 로그**: `~/.voice-action/logs/<ISO_TIMESTAMP>.json`

---

### main.py

**핫키**: `pynput` Right Command(⌘) 글로벌 핫키 — hold-to-record

```python
def on_press(key):
    if key == keyboard.Key.cmd_r:
        recorder.start()

def on_release(key):
    if key == keyboard.Key.cmd_r:
        wav = recorder.stop()
        asyncio.run(pipeline(wav))
```

**pipeline() 병렬화**
```python
transcription, snaps = await asyncio.gather(
    transcribe(wav_bytes),
    gather_snaps([Documents, Downloads, Desktop])
)
```

STT와 파일 스캔을 동시에 실행 → 합산 지연 max(STT, 스캔) 으로 단축.

---

### ui/preview.py

- `tkinter Toplevel`, 별도 스레드에서 실행 (핫키 루프 스레드에서 직접 호출 금지)
- `delete` 항목: 빨간색 강조 + 경고 문구
- ESC / 외부 클릭 → 취소
- 반환값: `True` (실행) / `False` (취소)

**MAX_FILES 초과 경고**: 패널 하단에 `⚠️ 최신 500개만 분석했습니다 (전체 N개)` 표시

---

### config.py + permissions.py

**설정 파일**: `~/.voice-action/config.json`
```json
{
  "anthropic_api_key": "sk-ant-...",
  "default_scan_dirs": ["~/Documents", "~/Downloads", "~/Desktop"],
  "whisper_model": "small"
}
```
환경변수 `ANTHROPIC_API_KEY` 우선 적용.

**권한 3종**: 마이크(AVCaptureDevice), 전체 디스크(쓰기 테스트), 손쉬운 사용(AXIsProcessTrusted)
- 하나라도 False → 온보딩 패널 표시 + 기능 비활성화

---

## 의존성

```
faster-whisper
sounddevice
numpy
anthropic
pynput
pyobjc-framework-Cocoa
pyobjc-framework-AVFoundation
```

## 토큰 예산 (캐시 히트 기준)

| 구성 요소 | 토큰 | 과금 |
|-----------|------|------|
| 시스템 프롬프트 | ~800 | 캐시 읽기 (~$0.00006) |
| .fs.snap | ~300 | 매 호출 |
| 음성 텍스트 | ~30 | 매 호출 |
| LLM 출력 | ~150 | 매 호출 |
| **합계** | **~480** | **~$0.0003/호출** |

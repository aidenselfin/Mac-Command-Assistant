import argparse
import asyncio
import sys
import threading
from pathlib import Path

from pynput import keyboard

from audio import AudioRecorder
from config import load_config
from executor import execute_plan
from llm import call_llm
from permissions import check_permissions
from scanner import get_snap
from stt import SpeechRecognitionError, transcribe
from ui.dispatcher import run_gui_loop, stop_gui_loop
from ui.onboarding import show_onboarding
from ui.preview import show_preview

SCAN_DIRS = [
    Path.home() / "Documents",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
]

recorder = AudioRecorder()
_config = None


async def gather_snaps(dirs: list[Path]) -> str:
    loop = asyncio.get_event_loop()
    snaps = await asyncio.gather(
        *[loop.run_in_executor(None, get_snap, d) for d in dirs]
    )
    return "\n\n---\n\n".join(snaps)


async def pipeline(wav_bytes: bytes) -> None:
    print("[pipeline] STT + 파일 스캔 중...")

    try:
        transcription, fs_snap = await asyncio.gather(
            asyncio.get_event_loop().run_in_executor(None, transcribe, wav_bytes),
            gather_snaps(SCAN_DIRS),
        )
    except SpeechRecognitionError as e:
        print(f"[STT 오류] {e}")
        return

    print(f"[STT] {transcription}")
    print("[LLM] 플랜 생성 중...")

    try:
        actions = call_llm(transcription, fs_snap)
    except Exception as e:
        print(f"[LLM 오류] {e}")
        return

    if not actions:
        print("[결과] 수행할 작업이 없습니다.")
        return

    print(f"[플랜] {len(actions)}개 액션")
    for a in actions:
        print(f"  - {a}")

    # show_preview는 dispatcher를 통해 메인 스레드에서 실행됨
    confirmed = show_preview(actions)
    if not confirmed:
        print("[취소]")
        return

    results = execute_plan(actions)
    ok = sum(1 for r in results if r.get("status") == "ok")
    fail = len(results) - ok
    print(f"[완료] {ok}개 성공, {fail}개 실패")


def on_press(key):
    if key == keyboard.Key.cmd_r:
        recorder.start()
        print("[녹음 시작] R-cmd 누름")


def on_release(key):
    if key == keyboard.Key.cmd_r:
        print("[녹음 종료] R-cmd 뗌")
        wav = recorder.stop()
        threading.Thread(
            target=lambda: asyncio.run(pipeline(wav)),
            daemon=True,
        ).start()


def main():
    global _config
    _config = load_config()

    if not _config.anthropic_api_key:
        print("[오류] Anthropic API 키가 없습니다.")
        print("  ~/.voice-action/config.json 에 anthropic_api_key를 저장하거나")
        print("  ANTHROPIC_API_KEY 환경변수를 설정하세요.")
        sys.exit(1)

    perms = check_permissions()
    if not all(perms.values()):
        print("[권한 부족] 온보딩 패널을 표시합니다.")
        show_onboarding(perms, _config)
        if not all(check_permissions().values()):
            print("[오류] 필요한 권한이 없어 종료합니다.")
            stop_gui_loop()
            sys.exit(1)

    print("[Voice-Action] 준비 완료. R-cmd를 누른 채로 말하세요.")

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()

    # 메인 스레드가 GUI 루프를 담당
    run_gui_loop()


async def _test_pipeline(command: str) -> None:
    print("[파일 스캔 중...]")
    fs_snap = await gather_snaps(SCAN_DIRS)

    print("[LLM 호출 중...]")
    try:
        actions = call_llm(command, fs_snap)
    except Exception as e:
        print(f"[LLM 오류] {e}")
        stop_gui_loop()
        return

    if not actions:
        print("[결과] 수행할 작업이 없습니다.")
        stop_gui_loop()
        return

    print(f"[플랜] {len(actions)}개 액션")
    for a in actions:
        print(f"  - {a}")

    confirmed = show_preview(actions)
    if not confirmed:
        print("[취소]")
        stop_gui_loop()
        return

    results = execute_plan(actions)
    ok = sum(1 for r in results if r.get("status") == "ok")
    fail = len(results) - ok
    print(f"[완료] {ok}개 성공, {fail}개 실패")
    stop_gui_loop()


def run_test(command: str) -> None:
    load_config()
    print(f"[테스트 모드] 명령: {command}")
    threading.Thread(
        target=lambda: asyncio.run(_test_pipeline(command)),
        daemon=True,
    ).start()
    run_gui_loop()  # 메인 스레드에서 GUI 루프 실행


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Voice-Action AI")
    parser.add_argument("--test", metavar="명령어", help="오디오 없이 텍스트 명령으로 파이프라인 테스트")
    args = parser.parse_args()

    if args.test:
        run_test(args.test)
    else:
        main()

# main.py — Smart-Homerow Integrated Main Process
# 통합 메인 프로세스:
#   1. PHASE1: 이벤트 로거 (마우스, 키보드)
#   2. PHASE2: 학습 분석 엔진
#   3. PHASE3: 오버레이 UI
#   4. 모든 PHASE를 멀티스레드로 동시 실행

import os
import sys
import threading
import time
import signal
import argparse
from pathlib import Path
from datetime import datetime

# ── 모듈 임포트 ────────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(_BASE_DIR))

# PHASE1: Logger
try:
    from logger import (
        get_active_app_and_window, update_active_app_and_window,
        write_log, on_click, on_scroll, on_key_press, on_key_release,
        poll_active_app, SESSION_ID, current_keys_pressed
    )
    PHASE1_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] PHASE1 임포트 실패: {e}")
    PHASE1_AVAILABLE = False

# PHASE2: Learning Engine
try:
    from learning_engine import LearningDatabase, LearningAdvisor, CSVImporter
    PHASE2_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] PHASE2 임포트 실패: {e}")
    PHASE2_AVAILABLE = False

# PHASE3: Overlay — 지연 로드 (오버레이를 끄면 PyObjC 없이도 실행 가능)
_PHASE3_IMPORT_TRIED = False
_PHASE3_AVAILABLE = False


def phase3_is_available() -> bool:
    """PyObjC(AppKit) + overlay_engine 로드 여부 (최초 1회만 시도)."""
    global _PHASE3_IMPORT_TRIED, _PHASE3_AVAILABLE
    if _PHASE3_IMPORT_TRIED:
        return _PHASE3_AVAILABLE
    _PHASE3_IMPORT_TRIED = True
    try:
        import AppKit  # noqa: F401

        from overlay_engine import OverlayEngineController  # noqa: F401

        _PHASE3_AVAILABLE = True
    except ImportError as e:
        print(f"[WARN] PHASE3 임포트 실패: {e}")
        _PHASE3_AVAILABLE = False
    return _PHASE3_AVAILABLE

# ── 설정 ────────────────────────────────────────────────────────────────────
VERSION = "1.0.0"
LOG_DIR = _BASE_DIR / "logs"
CONFIG_FILE = _BASE_DIR / "config.json"

class Config:
    """프로그램 설정"""
    def __init__(self):
        self.debug = False
        self.learning_enabled = True
        self.overlay_enabled = True
        self.logger_enabled = True
        self.log_level = "INFO"
        
    def load_from_file(self):
        """config.json에서 설정 로드 (# 으로 시작하는 줄은 JSON 비표준이므로 무시)"""
        try:
            import json
            if CONFIG_FILE.exists():
                raw = CONFIG_FILE.read_text(encoding="utf-8")
                if raw and raw[0] == "\ufeff":
                    raw = raw[1:]
                lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
                text = "\n".join(lines).strip()
                if not text:
                    return
                data = json.loads(text)
                self.debug = data.get("debug", False)
                self.learning_enabled = data.get("learning_enabled", True)
                self.overlay_enabled = data.get("overlay_enabled", True)
                self.logger_enabled = data.get("logger_enabled", True)
                self.log_level = data.get("log_level", "INFO")
        except Exception as e:
            print(f"[WARN] config.json 로드 실패: {e}")

config = Config()

# ── 로깅 유틸리티 ──────────────────────────────────────────────────────────
class Logger:
    """통합 로거"""
    
    def __init__(self, name: str, log_level: str = "INFO"):
        self.name = name
        self.log_level = log_level
        self.levels = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
        
    def _format_message(self, level: str, message: str) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"[{timestamp}] [{self.name}] [{level}] {message}"
    
    def _should_log(self, level: str) -> bool:
        return self.levels.get(level, 1) >= self.levels.get(self.log_level, 1)
    
    def debug(self, msg: str):
        if self._should_log("DEBUG"):
            print(self._format_message("DEBUG", msg))
    
    def info(self, msg: str):
        if self._should_log("INFO"):
            print(self._format_message("INFO", msg))
    
    def warn(self, msg: str):
        if self._should_log("WARN"):
            print(self._format_message("WARN", msg))
    
    def error(self, msg: str):
        if self._should_log("ERROR"):
            print(self._format_message("ERROR", msg))

main_logger = Logger("MAIN", config.log_level)
phase1_logger = Logger("PHASE1", config.log_level)
phase2_logger = Logger("PHASE2", config.log_level)
phase3_logger = Logger("PHASE3", config.log_level)

# ── PHASE 1: 이벤트 로거 ────────────────────────────────────────────────────
class Phase1Manager:
    """PHASE1: 이벤트 로깅 관리"""
    
    def __init__(self):
        self.running = False
        self.thread = None
        
    def start(self):
        """PHASE1 시작"""
        if not PHASE1_AVAILABLE:
            phase1_logger.warn("PHASE1 모듈 사용 불가")
            return
        
        if not config.logger_enabled:
            phase1_logger.info("로거 비활성화됨")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name="PHASE1-Logger")
        self.thread.start()
        phase1_logger.info("PHASE1 로거 시작")
    
    def _run(self):
        """메인 로깅 루프"""
        try:
            from pynput.mouse import Listener as MouseListener
            from pynput.keyboard import Listener as KeyboardListener
            from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
            
            update_active_app_and_window()
            
            mouse_listener = MouseListener(on_click=on_click, on_scroll=on_scroll)
            mouse_listener.start()
            
            keyboard_listener = KeyboardListener(on_press=on_key_press, on_release=on_key_release)
            keyboard_listener.start()
            
            phase1_logger.info("이벤트 리스너 활성화")
            
            while self.running:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.1, False)
                poll_active_app()
                time.sleep(0.01)
            
            mouse_listener.stop()
            keyboard_listener.stop()
            phase1_logger.info("이벤트 리스너 종료")
            
        except Exception as e:
            phase1_logger.error(f"PHASE1 오류: {e}")
    
    def stop(self):
        """PHASE1 종료"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

# ── PHASE 2: 학습 분석 엔진 ─────────────────────────────────────────────────
class Phase2Manager:
    """PHASE2: 학습 분석 엔진 관리"""
    
    def __init__(self):
        self.running = False
        self.thread = None
        self.db = None
        self.advisor = None
        
    def start(self):
        """PHASE2 시작"""
        if not PHASE2_AVAILABLE:
            phase2_logger.warn("PHASE2 모듈 사용 불가")
            return
        
        if not config.learning_enabled:
            phase2_logger.info("학습 기능 비활성화됨")
            return
        
        try:
            self.db = LearningDatabase()
            self.advisor = LearningAdvisor(self.db)
            phase2_logger.info("학습 DB 초기화 완료")
            
            # CSV 임포트 (처음 시작 시)
            importer = CSVImporter(self.db)
            importer.import_csv()
            phase2_logger.info("CSV 임포트 완료")
            
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True, name="PHASE2-Learning")
            self.thread.start()
            phase2_logger.info("PHASE2 학습 엔진 시작")
        
        except Exception as e:
            phase2_logger.error(f"PHASE2 초기화 오류: {e}")
    
    def _run(self):
        """학습 분석 루프 (주기적 통계 계산)"""
        try:
            while self.running:
                # 10초마다 통계 계산 및 출력
                time.sleep(10)
                
                if self.db:
                    stats = self.db.get_statistics()
                    if stats.get('total_shortcuts', 0) > 0:
                        phase2_logger.debug(f"통계: {stats}")
        
        except Exception as e:
            phase2_logger.error(f"PHASE2 오류: {e}")
    
    def stop(self):
        """PHASE2 종료"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

# ── PHASE 3: 오버레이 UI ────────────────────────────────────────────────────
class Phase3Manager:
    """PHASE3: 오버레이 UI 관리"""
    
    def __init__(self):
        self.running = False
        self.app = None
        
    def start(self):
        """PHASE3 시작"""
        if not config.overlay_enabled:
            phase3_logger.info("오버레이 UI 비활성화됨")
            return

        if not phase3_is_available():
            phase3_logger.warn("PHASE3 모듈 사용 불가")
            return
        
        try:
            def run_ui():
                try:
                    self.app = AppKit.NSApplication.sharedApplication()
                    self.app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
                    
                    from overlay_engine import OverlayEngineController
                    controller = OverlayEngineController.alloc().init()
                    
                    class TimerObj(AppKit.NSObject):
                        def tick_(self, timer): pass
                    
                    timer_obj = TimerObj.alloc().init()
                    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                        0.1, timer_obj, "tick:", None, True
                    )
                    
                    from PyObjCTools import AppHelper
                    phase3_logger.info("PHASE3 오버레이 UI 시작")
                    AppHelper.runEventLoop(installInterrupt=False)
                    
                except Exception as e:
                    phase3_logger.error(f"PHASE3 UI 오류: {e}")
            
            self.running = True
            self.thread = threading.Thread(target=run_ui, daemon=True, name="PHASE3-Overlay")
            self.thread.start()
        
        except Exception as e:
            phase3_logger.error(f"PHASE3 초기화 오류: {e}")
    
    def stop(self):
        """PHASE3 종료"""
        self.running = False
        if hasattr(self, 'thread'):
            self.thread.join(timeout=2.0)

# ── 메인 제어기 ────────────────────────────────────────────────────────────
class SmartHomerowController:
    """통합 제어기"""
    
    def __init__(self):
        self.phase1 = Phase1Manager()
        self.phase2 = Phase2Manager()
        self.phase3 = Phase3Manager()
        self.running = False
        
    def start(self):
        """모든 PHASE 시작"""
        self.running = True
        
        main_logger.info("=" * 70)
        main_logger.info(f"Smart-Homerow v{VERSION} — Integrated Main Process")
        main_logger.info("=" * 70)
        
        # 순서대로 시작
        self.phase1.start()
        time.sleep(0.5)
        
        self.phase2.start()
        time.sleep(0.5)
        
        self.phase3.start()
        time.sleep(0.5)
        
        main_logger.info("모든 PHASE 시작 완료")
        main_logger.info("Ctrl+C를 누르면 프로그램을 종료합니다.")
    
    def stop(self):
        """모든 PHASE 종료"""
        main_logger.info("프로그램 종료 중...")
        
        self.phase3.stop()
        time.sleep(0.5)
        
        self.phase2.stop()
        time.sleep(0.5)
        
        self.phase1.stop()
        time.sleep(0.5)
        
        self.running = False
        main_logger.info("프로그램 종료 완료")

# ── 시그널 핸들러 ──────────────────────────────────────────────────────────
def signal_handler(signum, frame):
    """시그널 핸들러 (Ctrl+C)"""
    main_logger.info("\n프로그램을 종료합니다...")
    controller.stop()
    sys.exit(0)

# ── 메인 진입점 ────────────────────────────────────────────────────────────
def main():
    """메인 함수"""
    global controller
    
    # 설정 로드
    config.load_from_file()
    
    # 로그 디렉토리 생성
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    # 인자 파싱
    parser = argparse.ArgumentParser(description="Smart-Homerow Integrated Main Process")
    parser.add_argument("--debug", action="store_true", help="디버그 모드")
    parser.add_argument("--no-logger", action="store_true", help="로거 비활성화")
    parser.add_argument("--no-learning", action="store_true", help="학습 엔진 비활성화")
    parser.add_argument("--no-overlay", action="store_true", help="오버레이 UI 비활성화")
    parser.add_argument("--version", action="version", version=f"Smart-Homerow v{VERSION}")
    
    args = parser.parse_args()
    
    if args.debug:
        config.debug = True
        config.log_level = "DEBUG"
    
    if args.no_logger:
        config.logger_enabled = False
    
    if args.no_learning:
        config.learning_enabled = False
    
    if args.no_overlay:
        config.overlay_enabled = False

    if config.overlay_enabled and not phase3_is_available():
        req = _BASE_DIR / "requirements.txt"
        run_sh = _BASE_DIR / "run_macos.sh"
        print()
        print("[ERROR] 오버레이(태그)를 사용할 수 없습니다. PyObjC(AppKit)가 이 Python에 설치되어 있지 않습니다.")
        print(f"        현재 Python: {sys.executable}")
        if "CommandLineTools" in sys.executable or sys.executable.startswith("/usr/bin/python"):
            print()
            print("        Apple 'Command Line Tools' 또는 시스템 /usr/bin Python은 PyObjC 설치가 막히거나")
            print("        wheel이 제공되지 않는 경우가 많습니다.")
            print("        → Homebrew / conda / python.org Python으로 전환하세요.")
            if run_sh.is_file():
                print()
                print(f"        권장:  cd {_BASE_DIR} && chmod +x run_macos.sh && ./run_macos.sh")
        else:
            print()
            print(f"        설치 시도:  {sys.executable} -m pip install -r {req}")
        print()
        print("        태그 없이 실행:  python3 main.py --no-overlay")
        print()
        sys.exit(1)

    if config.logger_enabled and not PHASE1_AVAILABLE:
        print("[WARN] PHASE1(pynput) 없음 — 이벤트 로깅은 비활성화됩니다. pip install pynput 권장.")
        config.logger_enabled = False
    
    # 시그널 핸들러 등록
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 제어기 생성 및 시작
    controller = SmartHomerowController()
    controller.start()
    
    # 무한 루프 (시그널 대기)
    try:
        while controller.running:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

if __name__ == "__main__":
    controller = None
    main()

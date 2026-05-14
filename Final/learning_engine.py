# learning_engine.py — Smart-Homerow Phase 2: Learning Analysis Engine
# 주요 기능:
#   1. SQLite DB 설계 & 관리 (shortcut_usage, user_proficiency, hint_effectiveness)
#   2. PHASE1 CSV 로그 → SQLite 마이그레이션
#   3. 사용 빈도 추적 (노출 vs 실제 사용)
#   4. 통계 분석 (시간대별, 앱별, 단축키별)
#   5. 학습 유도 알고리즘 (우선순위 정렬, 적응형 필터링)

# ── 1. imports ────────────────────────────────────────────────────────────────
import sqlite3
import csv
import json
import threading
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

# ── 2. 상수 및 설정 ───────────────────────────────────────────────────────────
_BASE_DIR = Path(__file__).parent
DB_FILE = _BASE_DIR / "learning.db"
PHASE1_CSV = Path(__file__).parent.parent / "PHASE1" / "logs" / "click_log.csv"


def resolve_shortcuts_db_path() -> Path:
    """Final 폴더의 DB를 우선하고, 없으면 레포 PHASE3 사본을 사용한다."""
    candidates = [
        _BASE_DIR / "shortcuts_db.json",
        _BASE_DIR.parent / "PHASE3" / "shortcuts_db.json",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return candidates[0]


SHORTCUTS_DB = resolve_shortcuts_db_path()

# 학습 알고리즘 파라미터
ACCEPTANCE_RATE_THRESHOLD_LOW = 0.30  # 30% 이하: 사용자 관심 없음, 덜 표시
ACCEPTANCE_RATE_THRESHOLD_HIGH = 0.70  # 70% 이상: 이미 습득함, 표시 안 함
ADOPTION_RATE_THRESHOLD = 0.50  # 50% 이상: 단축키 사용 습관 형성

# ── 3. SQLite DB 초기화 ───────────────────────────────────────────────────────
class LearningDatabase:
    """학습 데이터 저장 및 관리"""
    
    def __init__(self, db_path: Path = DB_FILE):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
    
    def _init_db(self) -> None:
        """DB 초기화 & 테이블 생성"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 1. shortcut_usage 테이블: 단축키 사용 통계
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS shortcut_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_bundle TEXT NOT NULL,
                    element_name TEXT NOT NULL,
                    shortcut TEXT NOT NULL,
                    times_shown INTEGER DEFAULT 0,
                    times_used INTEGER DEFAULT 0,
                    acceptance_rate REAL DEFAULT 0.0,
                    first_shown TIMESTAMP,
                    last_shown TIMESTAMP,
                    first_used TIMESTAMP,
                    last_used TIMESTAMP,
                    context TEXT,
                    UNIQUE(app_bundle, element_name, shortcut)
                )
            """)
            
            # 2. user_proficiency 테이블: 사용자 숙련도
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_proficiency (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_bundle TEXT NOT NULL,
                    keyboard_usage_count INTEGER DEFAULT 0,
                    mouse_usage_count INTEGER DEFAULT 0,
                    adoption_rate REAL DEFAULT 0.0,
                    last_updated TIMESTAMP,
                    UNIQUE(app_bundle)
                )
            """)
            
            # 3. hint_effectiveness 테이블: 힌트 효과 분석
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS hint_effectiveness (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shortcut_id INTEGER NOT NULL,
                    times_shown INTEGER DEFAULT 0,
                    times_accepted INTEGER DEFAULT 0,
                    acceptance_rate REAL DEFAULT 0.0,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(shortcut_id) REFERENCES shortcut_usage(id)
                )
            """)
            
            # 4. user_session 테이블: 세션 기록 (추적용)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_session (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT UNIQUE NOT NULL,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    total_clicks INTEGER DEFAULT 0,
                    total_keystrokes INTEGER DEFAULT 0,
                    apps_used TEXT
                )
            """)
            
            # 인덱스 생성 (쿼리 성능 최적화)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_bundle ON shortcut_usage(app_bundle)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_acceptance_rate ON shortcut_usage(acceptance_rate)")
            
            conn.commit()
    
    def record_shortcut_shown(self, app_bundle: str, element_name: str, shortcut: str) -> None:
        """단축키 힌트가 사용자에게 표시됨"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                
                cursor.execute("""
                    INSERT INTO shortcut_usage 
                    (app_bundle, element_name, shortcut, times_shown, first_shown, last_shown)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(app_bundle, element_name, shortcut) DO UPDATE SET
                        times_shown = times_shown + 1,
                        last_shown = ?
                """, (app_bundle, element_name, shortcut, now, now, now))
                
                conn.commit()
    
    def record_shortcut_used(self, app_bundle: str, element_name: str, shortcut: str) -> None:
        """사용자가 실제로 단축키를 사용함"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                
                cursor.execute("""
                    INSERT INTO shortcut_usage 
                    (app_bundle, element_name, shortcut, times_used, first_used, last_used)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(app_bundle, element_name, shortcut) DO UPDATE SET
                        times_used = times_used + 1,
                        last_used = ?
                """, (app_bundle, element_name, shortcut, now, now, now))
                
                # acceptance_rate 계산
                self._update_acceptance_rate(app_bundle, element_name, shortcut)
                
                conn.commit()
    
    def _update_acceptance_rate(self, app_bundle: str, element_name: str, shortcut: str) -> None:
        """acceptance_rate 재계산"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT times_shown, times_used FROM shortcut_usage 
                WHERE app_bundle = ? AND element_name = ? AND shortcut = ?
            """, (app_bundle, element_name, shortcut))
            
            row = cursor.fetchone()
            if row:
                times_shown, times_used = row
                acceptance_rate = times_used / times_shown if times_shown > 0 else 0.0
                
                cursor.execute("""
                    UPDATE shortcut_usage SET acceptance_rate = ?
                    WHERE app_bundle = ? AND element_name = ? AND shortcut = ?
                """, (acceptance_rate, app_bundle, element_name, shortcut))
                
                conn.commit()
    
    def get_top_shortcuts(self, app_bundle: str, limit: int = 5) -> List[Dict]:
        """앱별 상위 단축키 조회 (사용 빈도 기준)"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT element_name, shortcut, times_shown, times_used, acceptance_rate
                FROM shortcut_usage
                WHERE app_bundle = ?
                ORDER BY times_used DESC, acceptance_rate DESC
                LIMIT ?
            """, (app_bundle, limit))
            
            rows = cursor.fetchall()
            return [
                {
                    "element": row[0],
                    "shortcut": row[1],
                    "times_shown": row[2],
                    "times_used": row[3],
                    "acceptance_rate": row[4]
                }
                for row in rows
            ]
    
    def get_shortcuts_to_show(self, app_bundle: str, limit: int = 5) -> List[Dict]:
        """
        표시할 단축키 필터링 (학습 알고리즘 적용)
        - acceptance_rate < 30%: 의도적으로 덜 표시
        - acceptance_rate 30-70%: 계속 표시
        - acceptance_rate > 70%: 습득 완료, 표시 안 함
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            # 1. 습득 완료한 것은 제외
            cursor.execute("""
                SELECT element_name, shortcut, acceptance_rate
                FROM shortcut_usage
                WHERE app_bundle = ? AND acceptance_rate < ?
                ORDER BY times_used DESC, acceptance_rate DESC
                LIMIT ?
            """, (app_bundle, ACCEPTANCE_RATE_THRESHOLD_HIGH, limit))
            
            rows = cursor.fetchall()
            return [
                {
                    "element": row[0],
                    "shortcut": row[1],
                    "priority": "high" if row[2] >= ACCEPTANCE_RATE_THRESHOLD_LOW else "medium"
                }
                for row in rows
            ]
    
    def get_user_proficiency(self, app_bundle: str) -> Optional[Dict]:
        """사용자 숙련도 조회"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT keyboard_usage_count, mouse_usage_count, adoption_rate
                FROM user_proficiency
                WHERE app_bundle = ?
            """, (app_bundle,))
            
            row = cursor.fetchone()
            if row:
                return {
                    "keyboard_usage": row[0],
                    "mouse_usage": row[1],
                    "adoption_rate": row[2]
                }
            return None
    
    def update_user_proficiency(self, app_bundle: str, keyboard_count: int, mouse_count: int) -> None:
        """사용자 숙련도 업데이트"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                total = keyboard_count + mouse_count
                adoption_rate = keyboard_count / total if total > 0 else 0.0
                now = datetime.now().isoformat()
                
                cursor.execute("""
                    INSERT INTO user_proficiency 
                    (app_bundle, keyboard_usage_count, mouse_usage_count, adoption_rate, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(app_bundle) DO UPDATE SET
                        keyboard_usage_count = ?,
                        mouse_usage_count = ?,
                        adoption_rate = ?,
                        last_updated = ?
                """, (app_bundle, keyboard_count, mouse_count, adoption_rate, now,
                      keyboard_count, mouse_count, adoption_rate, now))
                
                conn.commit()
    
    def get_statistics(self, app_bundle: str = None) -> Dict:
        """전체 또는 앱별 통계"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            
            if app_bundle:
                cursor.execute("""
                    SELECT COUNT(*), AVG(times_shown), AVG(times_used), AVG(acceptance_rate)
                    FROM shortcut_usage WHERE app_bundle = ?
                """, (app_bundle,))
            else:
                cursor.execute("""
                    SELECT COUNT(*), AVG(times_shown), AVG(times_used), AVG(acceptance_rate)
                    FROM shortcut_usage
                """)
            
            row = cursor.fetchone()
            if row:
                return {
                    "total_shortcuts": row[0],
                    "avg_times_shown": row[1],
                    "avg_times_used": row[2],
                    "avg_acceptance_rate": row[3]
                }
            return {}

# ── 4. CSV → SQLite 마이그레이션 ──────────────────────────────────────────────
class CSVImporter:
    """PHASE1 CSV 로그를 SQLite로 변환"""
    
    def __init__(
        self,
        db: LearningDatabase,
        csv_path: Path = PHASE1_CSV,
        shortcuts_path: Optional[Path] = None,
    ):
        self.db = db
        self.csv_path = csv_path
        self.shortcuts_path = shortcuts_path if shortcuts_path is not None else resolve_shortcuts_db_path()
        self.shortcuts_cache = self._load_shortcuts()
    
    def _load_shortcuts(self) -> Dict:
        """shortcuts_db.json 로드"""
        try:
            if not self.shortcuts_path.is_file():
                print(f"[WARN] shortcuts_db.json 없음: {self.shortcuts_path}")
                return {}
            with open(self.shortcuts_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                print("[WARN] shortcuts_db.json 루트가 JSON 객체가 아님")
                return {}
            return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}
        except json.JSONDecodeError as e:
            print(f"[WARN] shortcuts_db.json JSON 파싱 오류: {e}")
            return {}
        except Exception as e:
            print(f"[WARN] shortcuts_db.json 로드 실패: {e}")
            return {}
    
    def import_csv(self) -> None:
        """CSV를 DB로 임포트"""
        if not self.csv_path.exists():
            print(f"[INFO] CSV 파일 없음: {self.csv_path}")
            return
        
        try:
            with open(self.csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    try:
                        app_name = row.get('app_name', '')
                        element_name = row.get('element_name', '')
                        action_type = row.get('action_type', '')
                        
                        # 번들 ID 매핑 (간단한 매핑, 실제로는 더 정교해야 함)
                        app_bundle = self._map_app_to_bundle(app_name)
                        
                        if not app_bundle or not element_name:
                            continue
                        
                        # 마우스 클릭 이벤트만 처리
                        if action_type != 'mouse_click':
                            continue
                        
                        # shortcuts_db에서 해당 단축키 찾기
                        shortcuts = self.shortcuts_cache.get(app_bundle, {})
                        
                        # element_name과 매칭되는 단축키 찾기 (현재: 정확한 매칭만 지원)
                        for key, shortcut in shortcuts.items():
                            if element_name.lower() in key.lower() or key.lower() in element_name.lower():
                                self.db.record_shortcut_shown(app_bundle, element_name, shortcut)
                                break
                    
                    except Exception as e:
                        print(f"[WARN] CSV 행 처리 실패: {e}")
                        continue
        
        except Exception as e:
            print(f"[ERROR] CSV 임포트 실패: {e}")
    
    @staticmethod
    def _map_app_to_bundle(app_name: str) -> Optional[str]:
        """앱 이름 → 번들 ID 매핑"""
        mapping = {
            'Visual Studio Code': 'com.microsoft.VSCode',
            'Code': 'com.microsoft.VSCode',
            'Google Chrome': 'com.google.Chrome',
            'Chrome': 'com.google.Chrome',
            'Finder': 'com.apple.finder',
            'Terminal': 'com.apple.Terminal',
            'Mail': 'com.apple.mail',
            'Safari': 'com.apple.Safari',
            'Notes': 'com.apple.Notes',
            'Slack': 'com.tinyspeck.slackmacgap',
        }
        return mapping.get(app_name)

# ── 5. 학습 유도 알고리즘 ──────────────────────────────────────────────────────
class LearningAdvisor:
    """사용자에게 표시할 단축키 추천"""
    
    def __init__(self, db: LearningDatabase):
        self.db = db
    
    def get_recommended_shortcuts(self, app_bundle: str, max_count: int = 5) -> List[Dict]:
        """추천 단축키 목록 (우선순위 정렬)"""
        shortcuts = self.db.get_shortcuts_to_show(app_bundle, limit=max_count)
        
        # 우선순위 정렬
        # 1. 사용 빈도 높은 순 (times_used)
        # 2. acceptance_rate 높은 순 (학습 진행 상태)
        
        return sorted(shortcuts, key=lambda x: x.get('priority') == 'high', reverse=True)
    
    def should_show_hint(self, app_bundle: str, element_name: str, shortcut: str) -> bool:
        """이 단축키 힌트를 표시해야 하는가?"""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT acceptance_rate, times_shown FROM shortcut_usage
                WHERE app_bundle = ? AND element_name = ? AND shortcut = ?
            """, (app_bundle, element_name, shortcut))
            
            row = cursor.fetchone()
            if not row:
                # 처음 본 단축키 → 항상 표시
                return True
            
            acceptance_rate, times_shown = row
            
            # 습득 완료 (> 70%) → 표시 안 함
            if acceptance_rate > ACCEPTANCE_RATE_THRESHOLD_HIGH:
                return False
            
            # 사용자 관심 없음 (< 30%) → 50% 확률로만 표시
            if acceptance_rate < ACCEPTANCE_RATE_THRESHOLD_LOW:
                import random
                return random.random() > 0.5
            
            # 학습 중 (30-70%) → 항상 표시
            return True

# ── 6. 메인 진입점 ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 70)
    print("  Smart-Homerow Learning Engine — Phase 2")
    print("  (Database Initialization & CSV Import)")
    print("=" * 70)
    
    # DB 초기화
    db = LearningDatabase()
    print(f"[INFO] Learning DB 초기화 완료: {DB_FILE}")
    
    # CSV 임포트
    importer = CSVImporter(db)
    importer.import_csv()
    print(f"[INFO] CSV 임포트 완료")
    
    # 통계 출력
    stats = db.get_statistics()
    print(f"[INFO] 통계: {stats}")

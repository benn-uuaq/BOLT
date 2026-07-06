import sys
import time
from pathlib import Path
import sqlite3
from openpyxl import Workbook
from datetime import datetime

def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
        internal_dir = base_dir / "_internal"
        if internal_dir.exists() and (internal_dir / "DB").exists():
            return internal_dir
        return base_dir
    else:
        return Path(__file__).resolve().parent.parent

PROJECT_ROOT = get_app_root()

DB_PATH = PROJECT_ROOT / "DB" / "bolt_system.db"
BACKUP_XLSX = PROJECT_ROOT / "DB" / "backup.xlsx"


# =========================
# RobotDB 클래스
# =========================
class RobotDB:
    def __init__(self):
        self.db_path = DB_PATH
        self.backup_xlsx = BACKUP_XLSX

        print("DB_PATH :", self.db_path)
        self._init_db()

    # -------------------------
    # DB 연결
    # -------------------------
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    # -------------------------
    # DB 초기화
    # -------------------------
    def _init_db(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS connection (
            IP_name TEXT PRIMARY KEY,
            IP TEXT DEFAULT '0.0.0.0',
            PORT INTEGER DEFAULT 0
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_workload (
            work_date TEXT PRIMARY KEY,
            work_count INTEGER DEFAULT 0
        )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.commit()
        conn.close()
        self.export_to_excel()

    # -------------------------
    # 엑셀 내보내기 (백업)
    # -------------------------
    def export_to_excel(self):
        conn = self._get_connection()
        cursor = conn.cursor()

        wb = Workbook()
        wb.remove(wb.active) # 기본 시트 제거

        # 정렬 기준 정의
        sort_info = {
            "connection": "IP_name",     # [추가] Connection 테이블 정렬 기준
            "daily_workload": "work_date",
            "settings": "key",
        }

        # [수정] Connection 테이블 추가
        target_tables = ["connection", "daily_workload", "settings"]

        for table in target_tables:
            sort_col = sort_info.get(table, "rowid") 

            sql = f"SELECT * FROM {table} ORDER BY {sort_col} ASC" # 보통 이름은 ASC 정렬
            if table == "daily_workload":
                sql = f"SELECT * FROM {table} ORDER BY {sort_col} DESC" # 날짜는 최신순

            cursor.execute(sql)
            
            rows = cursor.fetchall()
            
            if cursor.description:
                col_names = [desc[0] for desc in cursor.description]
            else:
                col_names = []

            ws = wb.create_sheet(title=table)
            ws.append(col_names)
            for row in rows:
                ws.append(row)

        try:
            wb.save(self.backup_xlsx)
        except PermissionError:
            print("[WARN] 엑셀 파일이 열려있어 백업에 실패했습니다.")
            
        conn.close()

    # =========================================================
    # [Connection] IP 및 Port 관리 메서드
    # =========================================================
    def save_connection(self, name, ip, port):
        """
        로봇 연결 정보를 저장합니다.
        name: 식별자 (예: 'Robot1', 'PLC') - Primary Key
        ip: IP 주소 (문자열)
        port: 포트 번호 (정수)
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO connection (IP_name, IP, PORT)
                VALUES (?, ?, ?)
            """, (name, str(ip), int(port)))
            conn.commit()
            print(f"[DB] 연결 정보 저장: {name} | {ip}:{port}")
        except Exception as e:
            print(f"[DB] 연결 정보 저장 실패: {e}")
        finally:
            conn.close()
            self.export_to_excel()

    def load_connection(self, name):
        """
        특정 이름의 연결 정보를 불러옵니다.
        return: (ip, port) 튜플 또는 None
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT IP, PORT FROM connection WHERE IP_name = ?", (name,))
            row = cursor.fetchone()
            if row:
                return row[0], row[1] # (IP, PORT)
            return None, None
        except Exception as e:
            print(f"[DB] 연결 정보 로드 실패: {e}")
            return None, None
        finally:
            conn.close()

    def get_all_connections(self):
        """
        저장된 모든 연결 정보를 딕셔너리로 반환합니다.
        return: {'Robot1': {'ip': '...', 'port': 30002}, ...}
        """
        conn = self._get_connection()
        cursor = conn.cursor()
        result = {}
        try:
            cursor.execute("SELECT IP_name, IP, PORT FROM connection")
            rows = cursor.fetchall()
            for row in rows:
                result[row[0]] = {'ip': row[1], 'port': row[2]}
            return result
        except Exception as e:
            print(f"[DB] 전체 연결 정보 로드 실패: {e}")
            return {}
        finally:
            conn.close()

    # =========================================================
    # [Daily Workload] 작업량 관리
    # =========================================================
    def limit_daily_workload(self, limit=1000):
        """DB에 저장되는 작업량 기록을 최신 기준 지정된 개수(1000개)로 유지합니다."""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            # 날짜 내림차순(최신순)으로 limit 개수 안에 들지 못하는 오래된 데이터를 삭제
            cursor.execute("""
                DELETE FROM daily_workload
                WHERE work_date NOT IN (
                    SELECT work_date
                    FROM daily_workload
                    ORDER BY work_date DESC
                    LIMIT ?
                )
            """, (limit,))
            conn.commit()
        except Exception as e:
            print(f"[DB ERROR] 오래된 작업 기록 삭제 실패: {e}")
        finally:
            conn.close()

    def insert_datetime(self):
        today = datetime.now().strftime("%Y-%m-%d")
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO daily_workload (work_date, work_count)
            VALUES (?, ?)
            ON CONFLICT(work_date) DO NOTHING
        """, (today, 0))
        
        conn.commit()
        conn.close()
        
        # [추가] 데이터 삽입 후 1000개 제한 검사 및 백업
        self.limit_daily_workload(1000)
        self.export_to_excel()
        
    def insert_workload(self, workload):
        today = datetime.now().strftime("%Y-%m-%d")
        
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO daily_workload (work_date, work_count)
            VALUES (?, ?)
            ON CONFLICT(work_date) DO UPDATE SET
                work_count=excluded.work_count
        """, (today, workload))
        
        conn.commit()
        conn.close()
        
        # [추가] 데이터 삽입/업데이트 후 1000개 제한 검사 및 백업
        self.limit_daily_workload(1000)
        self.export_to_excel()
        
    # =========================================================
    # [Settings] 설정 값 관리
    # =========================================================
    def save_setting(self, key, value):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
            # print(f"[DB] 설정 저장 완료: {key} = {value}")
        except Exception as e:
            print(f"[DB] 설정 저장 실패: {e}")
        finally:
            conn.close()
            self.export_to_excel()

    def load_setting(self, key, default_value=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            result = cursor.fetchone()
            if result:
                return result[0]
            else:
                return default_value
        except Exception as e:
            print(f"[DB] 설정 로드 실패: {e}")
            return default_value
        finally:
            conn.close()
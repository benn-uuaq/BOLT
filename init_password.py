import sqlite3
import os
from pathlib import Path

def reset_admin_password():
    print("🔄 관리자 비밀번호 초기화를 시작합니다...")
    
    # 1. DB 파일 경로 설정 (현재 스크립트 위치 기준 DB/bolt_system.db)
    base_dir = Path(__file__).resolve().parent
    db_path = base_dir / "DB" / "bolt_system.db"

    # DB 파일 존재 여부 확인
    if not db_path.exists():
        print(f"❌ [오류] DB 파일을 찾을 수 없습니다: {db_path}")
        print("💡 프로그램을 한 번이라도 실행하여 DB 파일이 생성된 상태인지 확인하세요.")
        return

    try:
        # 2. DB 연결
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 3. 비밀번호 '1234'로 덮어쓰기 
        # (기존 값이 있으면 UPDATE, 아예 변경한 적이 없으면 INSERT)
        cursor.execute("UPDATE settings SET value = '1234' WHERE key = 'admin_password'")
        
        # 만약 업데이트된 행이 없다면 (DB에 admin_password 항목 자체가 없는 경우) 새로 삽입
        if cursor.rowcount == 0:
            cursor.execute("INSERT INTO settings (key, value) VALUES ('admin_password', '1234')")

        # 4. 저장 및 종료
        conn.commit()
        conn.close()

        print("✅ 성공: 관리자 비밀번호가 기본값인 '1234'로 초기화되었습니다!")

    except sqlite3.OperationalError as e:
        print(f"❌ [DB 오류] 테이블 구조에 문제가 있거나 파일이 잠겨있습니다: {e}")
    except Exception as e:
        print(f"❌ [알 수 없는 오류] 초기화 실패: {e}")

if __name__ == "__main__":
    reset_admin_password()
    
    # 작업자가 결과를 확인할 수 있도록 창 닫힘 방지 (더블클릭 실행 시 유용함)
    os.system("pause")
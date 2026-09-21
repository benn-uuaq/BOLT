import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import os
import ast
import csv
import sys
import subprocess
import json
import platform
import time
import traceback
import threading
import ctypes
import serial
import sqlite3
from pathlib import Path

from PyQt5.QtWidgets import *
from PyQt5 import QtGui
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5 import QtCore
from PyQt5.QtCore import Qt, pyqtSignal, QDateTime, pyqtSlot, QThread

def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
        internal_dir = base_dir / "_internal"
        if internal_dir.exists() and (internal_dir / "DB").exists():
            return internal_dir
        return base_dir
    else:
        return Path(__file__).resolve().parent

APP_ROOT = get_app_root()

sys.path.insert(0, str(APP_ROOT))

from UI.bolt_ui import Ui_MainWindow
from ROBOT.robot import Robot_29999, Robot_30001, AlarmManager, Robot_modbus
from DB.db import RobotDB
from IO.IOmodule import IO_Module_Class

VNC_PATH = r""
LOGO_PATH = APP_ROOT / "logo.png"


class DBManager(RobotDB):
    def __init__(self):
        super().__init__()
    
    def get_work_history(self):
        """
        RobotDB의 상태와 무관하게, DB 파일에 직접 다이렉트로 연결하여 데이터를 긁어옵니다.
        """
        try:
            # 1. DB 폴더에서 확장자가 .db 인 파일을 자동으로 찾습니다.
            db_dir = APP_ROOT / "DB"
            db_files = list(db_dir.glob("*.db"))
            
            if not db_files:
                print("[DB ERROR] DB 폴더에 .db 파일이 존재하지 않습니다.")
                return []
            
            db_path = db_files[0] # 찾아낸 db 파일 경로
            
            # 2. 독립적인 DB 연결 생성 (기존 연결과 충돌 방지)
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            # 3. 데이터 조회
            cursor.execute("SELECT work_date, work_count FROM daily_workload ORDER BY work_date DESC LIMIT 500")
            records = cursor.fetchall()
            
            # 4. 연결 닫기
            conn.close()
            
            print(f"[DB 성공] {db_path.name} 파일에서 {len(records)}개의 데이터를 성공적으로 읽어왔습니다.")
            return records
            
        except sqlite3.OperationalError as e:
            print(f"[DB ERROR] 테이블 이름이나 컬럼이 맞지 않습니다: {e}")
            return []
        except Exception as e:
            print(f"[DB ERROR] 알 수 없는 오류 발생: {e}")
            return []
        
    # ★ [추가/수정] 프로그램 시작 시 DB에서 오늘 날짜의 마지막 작업량을 불러옵니다.
    def get_today_workload(self):
        try:
            db_dir = APP_ROOT / "DB"
            db_files = list(db_dir.glob("*.db"))
            
            if not db_files: return 0
            
            db_path = db_files[0]
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            import datetime
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # [수정 핵심]
            # 1. 'id' 컬럼이 없을 경우를 대비해 에러 방지
            # 2. 1초마다 빈 값이 저장될 수 있으므로, 오늘 날짜 기록 중 가장 큰 'work_count' 값을 가져옵니다.
            query = "SELECT work_count FROM daily_workload WHERE work_date LIKE ? ORDER BY CAST(work_count AS INTEGER) DESC LIMIT 1"
            cursor.execute(query, (f"%{today_str}%",))
            row = cursor.fetchone()
            conn.close()
            
            if row and row[0] is not None:
                print(f"[DB 성공] 오늘({today_str}) 저장된 최대 작업량 {row[0]}개를 성공적으로 불러왔습니다.")
                return int(row[0])
                
            print(f"[DB INFO] 오늘({today_str})의 작업 기록이 없으므로 0부터 시작합니다.")
            return 0
            
        except Exception as e:
            print(f"[DB ERROR] 오늘 작업량 불러오기 실패: {e}")
            return 0

class EmittingStream:
    def __init__(self, append_callback):
        self.append_callback = append_callback

    def write(self, text):
        if text.strip():
            self.append_callback(text)

    def flush(self):
        pass

class CSVViewerDialog(QDialog):
    def __init__(self, file_path, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.setWindowTitle(f"과거 작업 기록 조회 - {Path(file_path).name}")
        
        # =========================================================================
        # ★ [수정] 다이얼로그를 일반 윈도우 창처럼 만들고, 최소/최대화 버튼을 활성화합니다.
        # =========================================================================
        self.setWindowFlags(Qt.Window | Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint)
        self.setStyleSheet("background-color: #2b2b2b; color: white;")

        layout = QVBoxLayout(self)

        # 상단 제목
        lbl_title = QLabel(f"📄 {Path(file_path).name} 작업 기록")
        lbl_title.setStyleSheet("font-size: 18pt; font-weight: bold; margin-bottom: 10px;")
        lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_title)

        # 테이블 위젯 설정 (7개 컬럼)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["시간", "C/T", "작업 위치", "제품", "판정", "불량 위치", "상세 사유"])

        self.table.setStyleSheet("""
            QTableWidget { background-color: #333333; color: white; font-size: 14pt; gridline-color: #555555; border: 2px solid #0078D7; }
            QHeaderView::section { background-color: #0078D7; color: white; font-weight: bold; font-size: 14pt; padding: 5px; border: 1px solid #005a9e; }
        """)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers) # 읽기 전용
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

        # 헤더 조절 모드 설정 (창 크기를 조절할 때마다 비율대로 늘어남)
        header = self.table.horizontalHeader()
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        header.setSectionResizeMode(6, QHeaderView.Stretch) # 사유 칸은 남은 공간 채우기
        
        # 테이블 크기 변화 감지를 위한 이벤트 필터 장착
        self.table.installEventFilter(self)

        # 하단 닫기 버튼
        btn_close = QPushButton("닫 기")
        btn_close.setFixedHeight(50)
        btn_close.setStyleSheet("""
            QPushButton { background-color: #555555; font-size: 14pt; font-weight: bold; border-radius: 5px; }
            QPushButton:hover { background-color: #0078D7; }
        """)
        btn_close.clicked.connect(self.close)
        layout.addWidget(btn_close)

        self.load_csv_data()
        
        # =========================================================================
        # ★ [수정] 창을 처음 띄울 때 기본적으로 전체화면(최대화)으로 엽니다.
        # =========================================================================
        self.showMaximized()
        QtCore.QTimer.singleShot(100, self.update_table_widths)

    # 이벤트 필터 추가 (창 크기를 줄이거나 키울 때 테이블 컬럼 비율 실시간 유지)
    def eventFilter(self, obj, event):
        if obj == self.table and event.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(10, self.update_table_widths)
        return super().eventFilter(obj, event)

    def update_table_widths(self):
        """테이블 크기에 맞춰 7개 컬럼 비율을 재계산합니다."""
        table_width = self.table.viewport().width()
        if table_width > 100: 
            self.table.setColumnWidth(0, int(table_width * 0.10)) # 시간
            self.table.setColumnWidth(1, int(table_width * 0.08)) # C/T
            self.table.setColumnWidth(2, int(table_width * 0.15)) # 위치
            self.table.setColumnWidth(3, int(table_width * 0.15)) # 제품
            self.table.setColumnWidth(4, int(table_width * 0.10)) # 판정
            self.table.setColumnWidth(5, int(table_width * 0.15)) # 불량 위치

    def load_csv_data(self):
        try:
            with open(self.file_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                rows = list(reader)

            if not rows: return

            # 첫 줄이 "시간"으로 시작하면 헤더이므로 제외하고 출력
            start_idx = 1 if rows[0] and rows[0][0] == "시간" else 0
            self.table.setRowCount(len(rows) - start_idx)

            for row_idx, row_data in enumerate(rows[start_idx:]):
                # 데이터가 짧을 경우를 대비해 빈칸으로 채워치기
                row_data += [""] * (7 - len(row_data))

                for col_idx, text in enumerate(row_data[:7]):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(Qt.AlignCenter)

                    # [판정] 열 색상 적용
                    if col_idx == 4:
                        if text == "OK": item.setForeground(QBrush(QColor("#28a745")))
                        elif text == "NG": item.setForeground(QBrush(QColor("#FFC107")))#FFC107
                        elif text == "ERROR": item.setForeground(QBrush(QColor("#FF3333")))
                        
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)

                    self.table.setItem(row_idx, col_idx, item)
                    
        except Exception as e:
            QMessageBox.warning(self, "파일 열기 오류", f"기록을 불러오는 중 오류가 발생했습니다:\n{e}")
            
# =========================================================
# [수정] 작업 기록 조회 팝업 클래스 (제품별 상세 조회 및 리셋 기능)
# =========================================================
class WorkHistoryDialog(QDialog):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.main = main_window
        
        self.setWindowTitle("금일 작업량 상세 기록")
        self.resize(700, 400)
        self.setStyleSheet("background-color: #2b2b2b; color: white;")
        
        layout = QVBoxLayout(self)
        
        lbl_title = QLabel("금일 제품별 작업량")
        lbl_title.setStyleSheet("font-size: 16pt; font-weight: bold; margin-bottom: 10px;")
        lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_title)
        
        self.table = QTableWidget()
        self.table.setColumnCount(3) 
        self.table.setHorizontalHeaderLabels(["작업 일자", "제품 명", "생산 수량"])
        self.table.setStyleSheet("""
            QTableWidget { background-color: white; color: black; font-size: 14pt; }
            QHeaderView::section { background-color: #0078D7; color: white; font-weight: bold; font-size: 14pt; }
        """)
        
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch) 
        header.setSectionResizeMode(1, QHeaderView.Stretch) 
        header.setSectionResizeMode(2, QHeaderView.Stretch) 
        
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False) # 왼쪽 행 번호 숨김
        layout.addWidget(self.table)
        
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(20)
        
        self.btn_reset = QPushButton("리셋 (0으로 초기화)")
        self.btn_export = QPushButton("엑셀(CSV) 파일로 저장")
        self.btn_close = QPushButton("닫 기")
        
        for btn in [self.btn_reset, self.btn_export, self.btn_close]:
            btn.setFixedHeight(50)
            btn.setStyleSheet("""
                QPushButton { background-color: #555555; font-size: 14pt; font-weight: bold; border-radius: 5px; }
                QPushButton:hover { background-color: #0078D7; }
            """)
            btn_layout.addWidget(btn)
            
        # 리셋 버튼은 빨간색 포인트
        self.btn_reset.setStyleSheet("""
            QPushButton { background-color: #883333; font-size: 14pt; font-weight: bold; border-radius: 5px; }
            QPushButton:hover { background-color: #CC0000; }
        """)
            
        self.btn_reset.clicked.connect(self.reset_data)
        self.btn_export.clicked.connect(self.export_to_csv)
        self.btn_close.clicked.connect(self.close)
        
        layout.addLayout(btn_layout)
        self.load_data()
        
    def load_data(self):
        self.table.clearContents()
        self.table.setRowCount(4) # 0번~3번 제품 (4줄)
        
        # JSON에서 제품명 불러오기
        config_name = f"{self.main.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name
        product_names = {}
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    product_names = config_data.get("CURRENT_PRODUCT", {})
            except:
                pass
                
        today = self.main.current_date_str
        
        for i in range(4):
            # 2열: 제품 명
            name = product_names.get(str(i), f"{i}번 제품")
            self.table.setItem(i, 1, QTableWidgetItem(name))
            
            # 3열: 해당 제품 생산 수량
            count = self.main.product_counts.get(i, 0)
            self.table.setItem(i, 2, QTableWidgetItem(str(count)))
            
            for col in range(1, 3):
                item = self.table.item(i, col)
                if item: item.setTextAlignment(Qt.AlignCenter)
                
        # 1열: 날짜 (4칸 통합)
        date_item = QTableWidgetItem(today)
        date_item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        self.table.setItem(0, 0, date_item)
        self.table.setSpan(0, 0, 4, 1) # (행, 열, 병합할 행 수, 병합할 열 수)

    def reset_data(self):
        reply = self.main.show_message(
            "작업량 초기화", 
            "오늘의 <b>모든 제품 작업량</b>을<br>0으로 초기화하시겠습니까?", 
            QMessageBox.Warning, 
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.main.reset_today_workload()
            self.load_data() # UI 테이블 새로고침

    def export_to_csv(self):
        default_name = f"금일_작업기록_{QDateTime.currentDateTime().toString('yyyyMMdd_HHmmss')}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "작업 기록 엑셀 저장", default_name, "CSV Files (*.csv)")
        if not path: return
        try:
            with open(path, "w", newline='', encoding='utf-8-sig') as f: 
                writer = csv.writer(f)
                writer.writerow(["작업 일자", "제품 명", "생산 수량"])
                
                date_val = self.table.item(0, 0).text() # 병합된 날짜 텍스트
                for row in range(self.table.rowCount()):
                    prod_name = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
                    count_val = self.table.item(row, 2).text() if self.table.item(row, 2) else "0"
                    writer.writerow([date_val, prod_name, count_val])
                    
            QMessageBox.information(self, "저장 완료", "파일이 성공적으로 저장되었습니다.")
        except Exception as e:
            QMessageBox.critical(self, "저장 실패", f"파일 저장 중 오류가 발생했습니다.\n{e}")

class BlinkController:
    def __init__(self):
        self.blink_thread = None
        self.blink_stop_event = threading.Event()
        self._blink_key = None

    def start_blink(self, target_func, on_status, off_status, on_time=0.5, off_time=None):
        if off_time is None:
            off_time = on_time

        # ★ [PATCH] robot_status_update가 100ms마다 호출되면서 깜빡임 스레드를 매번 재시작하던 문제 수정.
        #   (0.5초 주기가 끝나기 전에 리셋되어 사실상 상시 점등 + IO 큐에 초당 수십 건 쓰기 발생)
        key = (target_func, tuple(on_status), tuple(off_status), on_time, off_time)
        if self.blink_thread is not None and self.blink_thread.is_alive() and self._blink_key == key:
            return

        self.stop_blink()
        self._blink_key = key

        self.blink_stop_event.clear()

        self.blink_thread = threading.Thread(target=self._blink_loop, args=(target_func, on_status, off_status, on_time, off_time))
        self.blink_thread.daemon = True 
        self.blink_thread.start()

    def stop_blink(self):
        """깜빡임을 중지하고 상태를 초기화합니다."""
        if self.blink_thread is not None and self.blink_thread.is_alive():
            self.blink_stop_event.set() 
            self.blink_thread.join() 
            self.blink_thread = None
        self._blink_key = None

    def _blink_loop(self, target_func, on_status, off_status, on_time, off_time):
        """실제 깜빡임 동작을 수행하는 내부 함수"""
        while not self.blink_stop_event.is_set():
            target_func(on_status)
            if self.blink_stop_event.wait(on_time):
                break
            target_func(off_status)
            if self.blink_stop_event.wait(off_time):
                break

class HeadJobThread(QThread):
    # ★ [수정] 에러 메시지(str)와 C/T(float) 인자 추가
    finished_signal = pyqtSignal(int, str, list, str, float) 
    def __init__(self, ui_ref, target_cell=1): 
        super().__init__()
        self.ui = ui_ref
        self.target_cell = target_cell

    def run(self):
        print(f"[INFO] Head 작업 시작 (Cell {self.target_cell})")
        result_status = "ERROR"
        failed_list = [] 
        error_msg = "알 수 없는 에러"
        c_t = 0.0
        try:
            # ★ 4개 변수를 모두 받아옴
            result_status, failed_list, error_msg, c_t = self.ui.job_head(self.target_cell) 
        except Exception as e:
            print(f"[ERROR] Head 작업 에러: {e}")
            error_msg = str(e)
            import traceback
            traceback.print_exc()
        finally:
            print(f"[INFO] Head 작업 종료 (Cell {self.target_cell}, 상태: {result_status})")
            self.finished_signal.emit(self.target_cell, result_status, failed_list, error_msg, c_t)
            
    def stop(self):
        pass

class BackJobThread(QThread):
    # ★ [수정] 에러 메시지(str)와 C/T(float) 인자 추가
    finished_signal = pyqtSignal(int, str, list, str, float)
    def __init__(self, ui_ref):
        super().__init__()
        self.ui = ui_ref

    def run(self):
        print("[INFO] Back 작업 시작.")
        result_status = "ERROR"
        failed_list = []
        error_msg = "알 수 없는 에러"
        c_t = 0.0
        try:
            result_status, failed_list, error_msg, c_t = self.ui.job_back()
        except Exception as e:
            print(f"[ERROR] Back 작업 에러: {e}")
            error_msg = str(e)
        finally:
            print(f"[INFO] Back 작업 종료 (상태: {result_status})")
            self.finished_signal.emit(0, result_status, failed_list, error_msg, c_t)

    def stop(self):
        pass
    
# =========================================================
# [클래스] 볼트 자동 비우기 (Purge) 스레드
# =========================================================
class BoltPurgeThread(QThread):
    finished_signal = pyqtSignal(bool, str) # 성공 여부, 메시지

    def __init__(self, main_window):
        super().__init__()
        self.main = main_window

    def run(self):
        print("\n==================================")
        print("[PURGE] 센서(41번) 및 Ready(43번) 기반 볼트 자동 비우기 시작")
        print("==================================")
        try:
            # 1. 시스템 안전 검사
            if getattr(self.main, 'robot_disconnected', True) or not self.main.is_robot_power_on:
                raise Exception("로봇 연결 또는 전원 상태 불량")
            
            if self.main.io_module is None:
                raise Exception("IO 모듈 미연결")

            # 2. 설비 충돌 방지: 선행 홈(Home) 위치 복귀 요청 및 대기
            print("[PURGE] 0. 설비 충돌 방지: 선행 홈(Home) 위치 복귀 요청 및 대기")
            self.main.robot_29999.robot_play() 
            time.sleep(0.5) # 로봇 프로그램이 정상 기동될 통신 타임 확보
            
            self.main.move_robot_to_home_sync()
            print("[PURGE] 선행 홈(Home) 안착 완료 확인. 안전 구간이 확보되어 배출 시퀀스를 전개합니다.")

            # 3. 로봇 배출 위치(Change_job)로 이동 요청
            print("[PURGE] 1. 로봇 배출 위치(Change_job) 이동 요청 (Modbus 315=1)")
            self.main.set_variable_with_ui("Change_job", 1)
            
            # 4. 로봇 도착 대기 (Modbus 415=1)
            wait_time = 0
            while True:
                self.main.wait_check(ignore_auto_mode=True)
                ready_val = self.main.get_variable_with_ui("Change_ready")
                if str(ready_val).lower() in ["true", "1"]:
                    print("[PURGE] 2. 배출 위치 도착 확인 (Change_ready=1)")
                    self.main.set_variable_with_ui("Change_job", 0) # 315번 0으로 리셋
                    break
                
                time.sleep(0.5)
                wait_time += 0.5
                if wait_time > 30: 
                    raise Exception("배출 위치 이동 타임아웃 (30초)")

            # 5. 볼트 배출 루프 (센서 41번 + Ready 43번 기반)
            print("[PURGE] 3. 볼트 강제 배출 루프 진입 (41번 센서 미감지 3회 또는 Ready 미감지 시 종료)")
            cycle_cnt = 1
            sfd_miss_cnt = 0
            
            # 메인 스레드에서 41번 센서의 짧은 펄스를 놓치지 않도록 래치(Latch) 활성화
            self.main.is_waiting_for_bolt = True

            while True:
                self.main.wait_check(ignore_auto_mode=True)
                
                # =========================================================================
                # ★ [핵심 추가] 피더기가 다음 볼트를 공급할 준비가 되었는지(Ready 신호) 먼저 검사
                # =========================================================================
                print(f"[PURGE] 배출 {cycle_cnt}회차 - 피더기 Ready 신호(MDI 43) 대기 중...")
                ready_start_t = time.time()
                is_sfd_ready = False
                
                while time.time() - ready_start_t < 5.0: # 피더기 정렬 및 대기 시간 최대 5초 부여
                    self.main.wait_check(ignore_auto_mode=True)
                    inp_ready = self.main.io_module.Read_Input_Data()
                    
                    if inp_ready and len(inp_ready) > 43 and inp_ready[43] == 1:
                        is_sfd_ready = True
                        break
                    time.sleep(0.05)
                
                # 5초간 피더기 레디가 안 들어오면 볼트 소진(또는 피더기 정지)으로 판단하고 최종 탈출 시퀀스 진입
                if not is_sfd_ready:
                    print("[PURGE] ❌ 피더기 Ready 신호 타임아웃 (볼트 완전 소진 판단). 최종 파기 후 시퀀스를 마칩니다.")
                    
                    self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 1))
                    time.sleep(0.5)
                    self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 0))
                    time.sleep(0.5)
                    break
                # =========================================================================

                print(f"[PURGE] 배출 {cycle_cnt}회차 (미감지 누적: {sfd_miss_cnt}/3) - 피더기 Ready 승인. 슈팅 실행.")
                
                # (1) 피더기 슈팅 (SFD Start MDO 43)
                self.main._bolt_detect_latched = False # 쏠 때마다 센서 메모리 초기화
                self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(43, 1))
                time.sleep(0.1)
                self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(43, 0))
                
                # (2) 41번 센서 감지 대기 (볼트가 호스를 타고 날아올 시간 2.5초 대기)
                start_t = time.time()
                bolt_detected = False
                while time.time() - start_t < 2.5:
                    self.main.wait_check(ignore_auto_mode=True)
                    inp = self.main.io_module.Read_Input_Data()
                    
                    if inp and len(inp) > 41:
                        if inp[41] == 1 or getattr(self.main, '_bolt_detect_latched', False):
                            bolt_detected = True
                            time.sleep(0.1) # 팁에 완전히 안착할 때까지 잠깐 대기
                            break
                    time.sleep(0.05)

                # (3) 결과 판별 및 실린더 작동
                if bolt_detected:
                    print(f"[PURGE] ✓ 볼트 정상 감지됨. 실린더 파기 진행.")
                    sfd_miss_cnt = 0 # 볼트가 정상적으로 나왔으므로 에러 카운트 초기화
                    
                    self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 1)) # 실린더 전진(하강)
                    time.sleep(0.5)
                    self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 0)) # 실린더 복귀(상승)
                    time.sleep(0.5)
                    
                else:
                    sfd_miss_cnt += 1
                    print(f"[PURGE] ❌ 볼트 미감지. (누적 {sfd_miss_cnt}회)")
                    
                    # 3번 연속으로 안 나오면 호스가 완전히 비워졌다고 판단
                    if sfd_miss_cnt >= 3:
                        print("[PURGE] ✓ 3연속 볼트 미감지. 호스 비워짐 판단. 마지막 파기 후 종료.")
                        
                        # 마지막 실린더 전후진 1회
                        self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 1))
                        time.sleep(0.5)
                        self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(39, 0))
                        time.sleep(0.5)
                        
                        break # 배출 루프 탈출
                
                cycle_cnt += 1

            # 배출 감시 종료
            self.main.is_waiting_for_bolt = False
            
            # 6. 로봇 홈 복귀
            print("[PURGE] 4. 로봇 홈(Home) 위치로 복귀 요청")
            self.main.move_robot_to_home_sync()
            
            # =========================================================================
            # ★ [위치 수정] 로봇이 홈에 완벽히 도착한 후, 피더기 에러 리셋(MDO 44) 수행
            # =========================================================================
            print("[PURGE] 5. 최종 공정: 피더기 에러 리셋 펄스 송출 (MDO 44 ON)")
            self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(44, 1))
            time.sleep(0.5)
            self.main.io_module.Send_que.put(self.main.io_module.Write_DO_Data(44, 0))
            time.sleep(0.5) # 하드웨어 신호 안정화 대기
            
            self.finished_signal.emit(True, "볼트 비우기 시퀀스가 성공적으로 완료되었습니다.")
            
        except Exception as e:
            print(f"[PURGE ERROR] 볼트 비우기 실패: {e}")
            self.main.is_waiting_for_bolt = False
            self.main.set_variable_with_ui("Change_job", 0) # 안전을 위해 무조건 끔
            self.finished_signal.emit(False, str(e))
    
# -------------------------------------------------------------------------
# ★ [추가] 알람 리셋 통신을 백그라운드에서 처리하기 위한 스레드 클래스
# -------------------------------------------------------------------------
class ResetRobotThread(QThread):
    request_update_signal = pyqtSignal()
    def __init__(self, main_window):
        super().__init__()
        self.main = main_window

    def run(self):
        try:
            # 0.5초 대기 (하드웨어 펄스 안정화 시간)
            time.sleep(0.5)

            if self.main.is_robot_power_on:
                print("[CMD] 보호정지 해제 및 다이얼로그 닫기 (전원 유지 상태)")
                self.main.robot_29999.send_command_29999("unlockProtectiveStop")
                self.main.robot_29999.send_command_29999("closeSafetyDialog")
            else:
                print("[CMD] 로봇 전원 OFF 상태: 세이프티 시스템 전체 리셋 (safety -r)")
                self.main.robot_29999.send_command_29999("safety -r")
                self.main.robot_29999.send_command_29999("unlockProtectiveStop")
                self.main.robot_29999.send_command_29999("closeSafetyDialog")
            
            self.main.robot_29999.send_command_29999("popup -c")
            print("[RESET] 알람 리셋 로봇 통신 완료")
            
            # 0.3초 뒤 UI 업데이트 강제 호출을 메인 스레드에 위임
            # QtCore.QMetaObject.invokeMethod(self.main, "robot_status_update", Qt.QueuedConnection)
            self.request_update_signal.emit()
            
        except Exception as e:
            print(f"[RESET ERROR] 로봇 통신 중 오류: {e}")
    
# =========================================================
# [클래스] 알람 전용 팝업 (Non-blocking & ApplicationModal)
# =========================================================
class AlarmDialog(QDialog):
    closed_signal = pyqtSignal() # 팝업 닫힘 감지용 시그널

    def __init__(self, parent=None, is_error=True, msg=""):
        super().__init__(parent)
        
        # 1. Non-blocking Modal 설정
        self.setWindowModality(Qt.ApplicationModal)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)

        self.setMinimumWidth(500)
        self.setMinimumHeight(280)

        # 2. 에러/경고 조건에 따른 색상 및 제목 분기
        if is_error:
            window_title = "로봇 알람 발생"
            title_color = "#FF3333"   # 텍스트 빨강
            border_color = "#FF3333"  # 테두리 빨강
        else:
            window_title = "로봇 메시지 알림"
            title_color = "#FFC107"   # 텍스트 노랑
            border_color = "#FFC107"  # 테두리 노랑

        self.setWindowTitle(window_title)

        # 3. ★ 핵심 수정: 덮어쓰지 않도록 f-string을 사용해 한 번에 통합 적용
        self.setStyleSheet(f"""
            QDialog {{ 
                background-color: #2b2b2b; 
                border: 2px solid {border_color}; 
            }}
            QLabel {{ 
                background-color: transparent; 
            }}
            QPushButton {{
                background-color: #555555; 
                color: #FFFFFF;
                font-size: 18px; 
                font-weight: bold;
                border-radius: 5px; 
                border: 1px solid #888;
            }}
            QPushButton:hover {{ 
                background-color: #0078D7; 
                border: 1px solid #005a9e; 
            }}
            QPushButton:pressed {{ 
                background-color: #333333; 
            }}
        """)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 30, 20, 30)

        # 4. 메시지 라벨 세팅
        html_message = f"""
            <div style="text-align:left;">
                <b style="color:{title_color}; font-size:24px;">{window_title}</b>
                <br><br>
                <span style="color:white; font-size:20px;">{msg}</span>
                <br><br>
                <span style="color:#AAAAAA; font-size:16px;">알람 및 메세지 원인을 제거한 후</span><br>
                <span style="color:#AAAAAA; font-size:16px;">리셋 버튼을 눌러주세요.</span>
            </div>
        """

        lbl_msg = QLabel(html_message)
        lbl_msg.setAlignment(Qt.AlignCenter)
        lbl_msg.setWordWrap(True)
        layout.addWidget(lbl_msg)
        layout.addSpacing(30)

        # 5. 확인 버튼
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("확인 (닫기)")
        btn_ok.setFixedSize(130, 60)
        btn_ok.clicked.connect(self.close) 
        
        btn_layout.addStretch()
        btn_layout.addWidget(btn_ok)
        btn_layout.addStretch()
        
        layout.addLayout(btn_layout)
        self.setLayout(layout)

        # 6. 화면 중앙 상단 배치
        screen_geo = QApplication.desktop().screenGeometry()
        new_x = int(screen_geo.width() / 2 - 250) 
        new_y = int(screen_geo.height() * 0.25)
        self.move(new_x, new_y)

    def closeEvent(self, event):
        self.closed_signal.emit()
        super().closeEvent(event)
    
# =========================================================
# [클래스] 작업 완료 알림 팝업 (Non-blocking & 위치 자동 지정)
# =========================================================
class JobResultDialog(QDialog):
    closed = pyqtSignal(int)  # 닫힐 때 어떤 셀인지 알려주는 신호

    def __init__(self, parent=None, cell_num=0):
        super().__init__(parent)
        self.cell_num = cell_num
        
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.resize(320, 160)
        
        self.setStyleSheet("background-color: #2b2b2b; color: white; border: 2px solid #0078D7;")

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        
        if cell_num == 0:
            self.setWindowTitle("작업 완료 - BACK")
            msg_text = "<b>[BACK]</b> 작업이 완료되었습니다.<br>제품을 확인해주세요."
        else:
            self.setWindowTitle(f"작업 완료 - Cell {cell_num}")
            msg_text = f"<b>[Cell {cell_num}]</b> 작업이 완료되었습니다.<br>제품을 확인해주세요."

        msg_label = QLabel(msg_text)
        msg_label.setAlignment(Qt.AlignCenter)
        msg_label.setStyleSheet("font-size: 14pt; border: none; color: white;")
        layout.addWidget(msg_label)

        layout.addSpacing(10)

        btn_ok = QPushButton("확인 (닫기)")
        btn_ok.setFixedHeight(45)
        btn_ok.setStyleSheet("""
            QPushButton { 
                background-color: #555555; 
                color: white; 
                font-weight: bold; 
                border-radius: 5px; 
                font-size: 12pt; 
                border: 1px solid #888;
            }
            QPushButton:hover { 
                background-color: #0078D7; 
                border: 1px solid #005a9e;
            }
        """)
        btn_ok.clicked.connect(self.close)
        layout.addWidget(btn_ok)

        self.setLayout(layout)
        self.adjust_position()

    def adjust_position(self):
        screen_geo = QApplication.desktop().screenGeometry()
        scr_w, scr_h = screen_geo.width(), screen_geo.height()
        win_w, win_h = self.width(), self.height()

        if self.cell_num == 1:
            self.move(int(scr_w * 0.2) - int(win_w / 2), int(scr_h * 0.4))
        elif self.cell_num == 2:
            self.move(int(scr_w * 0.8) - int(win_w / 2), int(scr_h * 0.4))
        else:
            self.move(int((scr_w - win_w) / 2), int((scr_h - win_h) / 2))

    def closeEvent(self, event):
        self.closed.emit(self.cell_num)
        super().closeEvent(event)
        
# =========================================================
# ★ [추가/수정 클래스] 체결 불량(NG) 발생 알림 팝업 (Non-blocking)
# =========================================================
class JobNGDialog(QDialog):
    closed = pyqtSignal(int)

    def __init__(self, parent=None, cell_num=0, failed_bolts=[]):
        super().__init__(parent)
        self.cell_num = cell_num
        
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        
        # 불량 갯수에 따라 창 높이 유동적 조절
        base_height = 250
        extra_height = min(len(failed_bolts) * 30, 200) 
        self.resize(500, base_height + extra_height)
        
        self.setStyleSheet("background-color: #2b2b2b; color: white; border: 2px solid #FF3333;")

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 리스트에 있는 실패 정보(번호 및 사유)를 HTML 줄바꿈으로 깔끔하게 결합
        fail_str = "<br>".join([f"&nbsp;&nbsp;• 지점 {item}" for item in failed_bolts])
        
        if cell_num == 0:
            popup_title = "[BACK 모드] 작업 완료"
        else:
            popup_title = f"[HEAD 모드 - Cell {cell_num}] 작업 완료"

        msg_text = (f"<div style='text-align:center;'><span style='font-size:22px;'><b>{popup_title}</b></span><br><br>"
                    f"<span style='color:#FF3333; font-size:20px;'>다음 위치에서 <b>체결 실패(NG)</b>가 발생했습니다.</span><br><br></div>"
                    f"<div style='color:#FFC107; font-size:18px; font-weight:bold; text-align:left; background-color:#442222; padding:10px; border-radius:5px;'>"
                    f"{fail_str}</div><br>"
                    f"<div style='text-align:center;'><span style='font-size:16px;'>제품 상태를 확인해 주세요.</span></div>")

        msg_label = QLabel(msg_text)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("border: none; background-color: transparent;")
        layout.addWidget(msg_label)

        layout.addSpacing(10)

        btn_ok = QPushButton("확인 (닫기)")
        btn_ok.setFixedHeight(50)
        btn_ok.setStyleSheet("""
            QPushButton { 
                background-color: #555555; 
                color: white; 
                font-weight: bold; 
                border-radius: 5px; 
                font-size: 14pt; 
                border: 1px solid #888;
            }
            QPushButton:hover { 
                background-color: #FF3333; 
                border: 1px solid #CC0000;
            }
        """)
        btn_ok.clicked.connect(self.close)
        layout.addWidget(btn_ok)

        self.setLayout(layout)
        self.adjust_position()

    def adjust_position(self):
        screen_geo = QApplication.desktop().screenGeometry()
        scr_w, scr_h = screen_geo.width(), screen_geo.height()
        win_w, win_h = self.width(), self.height()

        if self.cell_num == 1:
            self.move(int(scr_w * 0.2) - int(win_w / 2), int(scr_h * 0.4))
        elif self.cell_num == 2:
            self.move(int(scr_w * 0.8) - int(win_w / 2), int(scr_h * 0.4))
        else:
            self.move(int((scr_w - win_w) / 2), int((scr_h - win_h) / 2))

    def closeEvent(self, event):
        self.closed.emit(self.cell_num)
        super().closeEvent(event)
    
class ConnectionThread(QThread):
    # 결과 신호 (성공여부, 메시지)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, robot_29999, robot_30001, modbus_client, io_ip, io_port):
        super().__init__()
        self.robot_29999 = robot_29999
        self.robot_30001 = robot_30001
        self.modbus_client = modbus_client
        self.io_ip = io_ip
        self.io_port = io_port

    def run(self):
        try:
            # 1. 로봇 기본 소켓 연결
            sock1 = self.robot_29999.connect_29999()
            sock2 = self.robot_30001.connect_30001()
            
            # 2. 모드버스 연결
            modbus_ok = False
            if self.modbus_client is not None:
                modbus_ok = self.modbus_client.connect()

            # 3. ★ [추가] IO 모듈 (TCP 포트 연결 테스트)
            io_ok = False
            import socket
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0) # 1초 대기
                if s.connect_ex((self.io_ip, self.io_port)) == 0:
                    io_ok = True

            # 4. 최종 결과 판별 (하나라도 실패하면 에러 반환)
            if sock1 is None:
                self.finished_signal.emit(False, "29999 포트 연결 실패")
                return
            if sock2 is None:
                self.finished_signal.emit(False, "30001 포트 연결 실패")
                return
            if not modbus_ok:
                self.finished_signal.emit(False, "Modbus(502) 연결 실패 (로봇 서버 확인)")
                return
            if not io_ok:
                self.finished_signal.emit(False, "IO 모듈 통신 실패 (전원 및 랜선을 확인하세요)")
                return
            
            self.finished_signal.emit(True, "모든 시스템(로봇 및 IO) 연결 성공")

        except Exception as e:
            self.finished_signal.emit(False, f"연결 중 예외 발생: {str(e)}")

class AlarmThread(QThread):
    alarm_received_signal = pyqtSignal(str, str)

    def __init__(self, main_window, alarm_manager):
        super().__init__()
        self.main = main_window # ★ BOLT 메인 클래스 전체를 참조
        self.alarm_manager = alarm_manager
        self.running = True

    def run(self):
        import queue
        while self.running:
            try:
                # 항상 최신의 로봇 객체를 동적으로 가져옴
                current_robot = getattr(self.main, 'robot_30001', None)
                
                # 로봇이 연결되어 있을 때만 알람 큐 확인
                if current_robot is not None and not getattr(self.main, 'robot_disconnected', True):
                    try:
                        # 1초만 기다려보고 큐가 비어있으면 루프 재시작 (스레드 멈춤 방지)
                        alarm = current_robot.alarm_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue 
                        
                    if alarm:
                        self.alarm_manager.process(alarm)
                        if alarm.code is not None:
                            alarm_msg = f"[ALARM {alarm.code}] Sub: {alarm.sub} - Level {alarm.level}"
                            level = "ERROR"
                        else:
                            alarm_msg = f"[MSG] {alarm.msg}"
                            level = "WARN"

                        self.alarm_received_signal.emit(alarm_msg, level)
                else:
                    time.sleep(1)

            except Exception as e:
                time.sleep(1)

    def stop(self):
        self.running = False
        self.quit()
        self.wait()

class BOLT(QMainWindow):
    log_signal = pyqtSignal(str)
    report_alarm_signal = pyqtSignal(str, str)
    update_var_signal = pyqtSignal(str, object)
    def __init__(self):
        super(BOLT, self).__init__()
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        
        self.logo_path = LOGO_PATH
        
        if os.path.exists(self.logo_path):
            self.ui.logo.setPixmap(QtGui.QPixmap(str(self.logo_path)))
        else:
            print(f"[WARN] 로고 파일을 찾을 수 없습니다: {self.logo_path}")
        
        self.db_manager = DBManager()
        # self.init_network_ui()
        
        self.blow_time = float(self.db_manager.load_setting("nr_blow_time", "0.0"))
        
        self.robot_ip = '0.0.0.0'
        self.robot_port1 = 29999
        self.robot_port2 = 30001
        
        self.io_ip = '0.0.0.0'
        self.io_port = 502
        
        self.nr_ser_port = 'COM1' 
        self.nr_baudrate = 9600
        
        self.modbus_ip = "0.0.0.0"
        self.modbus_port = 502
        self.cached_robot_vars = {}
        self.modbus_lock = threading.Lock()
        
        self.init_network_ui()
        
        self.create_robot_objects()
        
        self.datetime_timer = QtCore.QTimer(self)
        self.datetime_timer.setInterval(1000)
        self.datetime_timer.timeout.connect(self.set_current_datetime)
        self.datetime_timer.start()
        
        # ========================================================
        # ★ [수정] 무조건 0이 아니라 DB에서 오늘 작업량을 불러오도록 수정
        # ========================================================
        self.current_date_str = QDateTime.currentDateTime().toString("yyyy-MM-dd")
        
        self.init_history_file_path()
        
        self.current_workload = self.db_manager.get_today_workload()
        
        ng_str = self.db_manager.load_setting(f"ng_count_{self.current_date_str}", "0")
        self.current_ng_count = int(ng_str)
        
        self.product_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for i in range(4):
            val = self.db_manager.load_setting(f"workload_{self.current_date_str}_prod_{i}", "0")
            self.product_counts[i] = int(val)
        
        self.set_current_datetime()
        self.set_current_workload(self.current_workload)
        # ========================================================
        
        self.robot_disconnected = True
        self.update_connect_button_state()
        self.last_data_time = time.time()
        
        self.blink_tower = BlinkController()
        self.blink_buttons = BlinkController()
        
        self.alarm_manager = AlarmManager()
        self.alarm_thread = None
        self._speed_initialized = False
        self.alarm_popup_shown = False
        self.is_message_active = False
        self.active_alarm_dialog = None
        
        self._is_first_boot_init_done = False  # 자동 모드 진입 시 1회만 실행하도록 기억
        self.is_initializing = False           # 현재 초기화 작업이 진행 중인지 상태 표시
        self._post_init_cell = None
        
        self.seq_mute_active = False     
        self.back_mute_delaying = False  
        self._current_mute_state = False
        
        self.active_popups = {1: None, 2: None, 0: None} 
        self.robot_occupant = 0
        self.threads = {1: None, 2: None, 0: None}
        
        self.waiting_cell = None
        self.prev_button_pressed = False
        self.wait_unload_1 = False  # Cell 1 제품 뺄 때까지 대기
        self.wait_unload_2 = False  # Cell 2 제품 뺄 때까지 대기
        self.jig_motion_mute_counter = 0
        self.ready_to_cross_jig = False
        self.overlap_in_progress = False
        self.cross_trigger_cell = None
        
        self.last_pressed_status = []
        self.last_blink_pattern = None
        
        self.is_back_working = False
        
        self.is_buzzer_muted = False
        
        self.mdi_lamps = []
        self.mdo_btns = []

        self.io_module = None
        
        self._bolt_detect_latched = False
        self.is_waiting_for_bolt = False

        # ★ [PATCH] 볼트 통과 센서(MDI 41) 펄스 카운터 (전용 감시 스레드가 증가시킴)
        self._sfd_lock = threading.Lock()
        self._sfd_pulse_count = 0      # 프로그램 시작 후 감지된 통과 펄스 총 수
        self._sfd_pulse_mark = 0       # '여기까지는 설명된 펄스' 기준값 (초과분 = 예상 밖 볼트 통과)
        self._last_feed_status = None  # "DETECTED" / "NOT_DETECTED" / "EXTRA_PULSE_SKIP"
        self._start_sfd_sensor_monitor()
        
        last_mode = self.load_last_mode()
        print(f"\n==========================================")
        print(f"[INIT] 프로그램 시작 - 저장된 모드(DB): {last_mode}")
        print(f"==========================================\n")
        self.update_system_mode(last_mode)

        self.ui.speed_slider.valueChanged.connect(self.on_slider_changed)
        self.on_slider_changed(self.ui.speed_slider.value())
        self.ui.speed_slider.valueChanged.connect(self.on_speed_changed)
        self.pending_speed_value = None

        sys.stdout = EmittingStream(self.log_signal.emit)
        sys.stderr = EmittingStream(self.log_signal.emit)
        self.log_signal.connect(self.add_log)
        self.ui.log_delete_button.clicked.connect(lambda: self.ui.scrollArea.clear())
    
        try:
            self.ui.home_button.clicked.disconnect()
            self.ui.pause_button.clicked.disconnect()
            self.ui.stop_button.clicked.disconnect()
        except Exception:
            pass
        
        self.ui.auto_btn.setCheckable(True)
        self.ui.manual_btn.setCheckable(True)
        self.ui.manual_btn.setChecked(True)
        self.ui.auto_btn.toggled.connect(self.on_auto_mode_toggled)
        self.ui.manual_btn.toggled.connect(self.on_manual_mode_toggled)
        self.ui.home_button.clicked.connect(self.on_home_button_clicked)
        self.ui.pause_button.toggled.connect(self.on_pause_button_toggled)
        self.ui.stop_button.clicked.connect(self.on_stop_button_clicked)
        self.ui.power_on_button.clicked.connect(lambda: self.on_robot_power_on_button_clicked(True))
        self.ui.power_off_button.clicked.connect(lambda: self.on_robot_power_off_button_clicked(True))
        self.ui.robot_connect_button.clicked.connect(self.on_robot_connect_button_clicked)
        self.ui.alarm_reset_button.clicked.connect(self.robot_alarm_reset_button)
        self.ui.main_btn.clicked.connect(self.on_main_btn_clicked)
        self.ui.quality_btn.clicked.connect(self.on_quality_btn_clicked)
        self.ui.io_btn.clicked.connect(self.on_io_btn_clicked)
        self.ui.robot_btn.clicked.connect(self.on_robot_btn_clicked)
        
        if hasattr(self.ui, 'quality_log_btn'):
            try: 
                self.ui.quality_log_btn.clicked.disconnect() 
            except Exception: 
                pass
            self.ui.quality_log_btn.clicked.connect(self.on_quality_log_btn_clicked)
        
        if hasattr(self.ui, 'product_sellect_btn'):
            self.ui.product_sellect_btn.clicked.connect(self.show_product_selection)
            
        # DB에서 마지막으로 작업하던 제품 종류 불러오기 (기본값 0)
        self.current_product_type = int(self.db_manager.load_setting("product_type", 0))
        self.apply_product_type(self.current_product_type)
        
        self.ui.buzzer_off_button.setCheckable(True)
        try: self.ui.buzzer_off_button.clicked.disconnect()
        except: pass
        self.ui.buzzer_off_button.toggled.connect(self.on_buzzer_mute_toggled)
        
        toggle_buttons = [
            self.ui.home_pose_btn, self.ui.waypoint_pose_btn,
            self.ui.pose_1_btn, self.ui.pose_2_btn, self.ui.pose_3_btn,
            self.ui.pose_4_btn, self.ui.pose_5_btn, self.ui.pose_6_btn,
            self.ui.pose_7_btn, self.ui.pose_8_btn, self.ui.pose_9_btn,
            self.ui.pose_10_btn, self.ui.pose_11_btn,
            self.ui.cell_1_btn, self.ui.cell_2_btn
        ]
        for btn in toggle_buttons:
            btn.setCheckable(True)
            
        self.active_pose_btn = None
        self.last_pose_sequence = []
        self.init_sequence_mapping()
        
        self.job_thread = None
        self.auto_mode = False
        self.is_robot_power_on = False
        self.is_task_running = False
        self.is_task_paused = False
        self.safety_mode = 0
        self.robot_mode = 0
        self.robot_speed = None
        
        self.robot_status_29999 = None
        
        self.values = (self.is_robot_power_on, self.is_task_running, self.is_task_paused, self.safety_mode, self.robot_mode, self.robot_speed)
        self.actual_joint_base = 0.0; self.actual_joint_shoulder = 0.0; self.actual_joint_elbow = 0.0
        self.actual_joint_wrist1 = 0.0; self.actual_joint_wrist2 = 0.0; self.actual_joint_wrist3 = 0.0
        
        self.pause_event = threading.Event()
        self.pause_event.set()
        
        self.state_timer = QtCore.QTimer(self)
        self.state_timer.timeout.connect(self.poll_robot_state)
        self.state_timer.start(100)
        
        self.ui.logo.installEventFilter(self)
        self.logo_click_count = 0
        self.logo_timer = QtCore.QTimer(self)
        self.logo_timer.setInterval(10000) 
        self.logo_timer.setSingleShot(True) 
        self.logo_timer.timeout.connect(self.reset_logo_count)
        
        self.ui.workload_edit.installEventFilter(self)
        
        self.init_io_ui()
        self.init_robot_var_ui()
        
        self.ui.ip_save_button.clicked.connect(self.save_network_settings)
        
        default_vnc = VNC_PATH
        self.vnc_path = self.db_manager.load_setting("vnc_path", default_vnc)
        
        self.ui.btn_open_vnc.clicked.connect(self.open_vnc_viewer)
        
        QtCore.QTimer.singleShot(100, self.start_background_connection)
        
        self.report_alarm_signal.connect(self.add_alarm_log)
        
        self._is_first_boot_init_done = False
        
        self.update_var_signal.connect(self._do_update_robot_var_ui)
        
        self.history_table_init()
        
    def history_table_init(self):
        # 테이블 헤더 객체 가져오기
        header = self.ui.history_table.horizontalHeader()

        # ★ 7개 컬럼 셋팅
        self.ui.history_table.setColumnCount(7)
        self.ui.history_table.setHorizontalHeaderLabels(["시간", "C/T", "작업 위치", "제품", "판정", "불량 위치", "상세 사유"])

        # 0~5번 컬럼은 사용자가 조절 가능하게, 6번(사유)은 쫙 펴지게
        for i in range(6):
            header.setSectionResizeMode(i, QHeaderView.Interactive)
        header.setSectionResizeMode(6, QHeaderView.Stretch)

        # 테이블 크기가 바뀔 때마다 비율 재계산
        self.ui.history_table.installEventFilter(self)
        QtCore.QTimer.singleShot(50, self.update_history_table_widths)
        
    def update_history_table_widths(self):
        """테이블 크기에 맞춰 컬럼 비율을 재계산합니다."""
        table_width = self.ui.history_table.viewport().width()
        
        # UI가 렌더링되기 전이라 너비가 비정상적으로 작을 때(예: 100px 이하)는 무시
        if table_width > 100: 
            self.ui.history_table.setColumnWidth(0, int(table_width * 0.20)) # 시간
            self.ui.history_table.setColumnWidth(1, int(table_width * 0.10)) # 사이클 타임
            self.ui.history_table.setColumnWidth(2, int(table_width * 0.10)) # 작업 위치
            self.ui.history_table.setColumnWidth(3, int(table_width * 0.10)) # 제품
            self.ui.history_table.setColumnWidth(4, int(table_width * 0.10)) # 판정
            self.ui.history_table.setColumnWidth(5, int(table_width * 0.15)) # 불량 위치 (20%)
        
    def update_robot_var_ui(self, var_name, value):
        """테이블 업데이트를 요청하는 신호 발송 (어느 스레드에서든 호출 가능)"""
        self.update_var_signal.emit(var_name, value)
        
    @pyqtSlot(str, object)
    def _do_update_robot_var_ui(self, var_name, value):
        """[메인 스레드 전용] 실제 QTableWidget의 텍스트를 변경"""
        val_item = self.var_item_map.get(var_name)
        
        if val_item:
            # =======================================================
            # ★ [수정] 0 또는 1 값이 들어오면 강제로 True/False 텍스트로 변환
            # =======================================================
            if isinstance(value, bool):
                display_text = "True" if value else "False"
            elif isinstance(value, float):
                display_text = f"{value:.4f}"
            elif str(value).strip() in ["0", "1"]:
                display_text = "True" if str(value).strip() == "1" else "False"
            else:
                display_text = str(value)
            
            val_item.setText(display_text)
        
    def create_robot_objects(self):
        """객체만 생성하고 실제 소켓 연결은 하지 않음 (빠른 부팅용)"""
        # 기존 연결 객체가 있다면 정리
        # ★ [PATCH] self.robot_30001.__sock 은 BOLT 클래스 안에서 _BOLT__sock 으로 해석되어
        #   항상 실패(→ 소켓 누수)했습니다. 각 클래스의 close()를 사용합니다.
        if hasattr(self, 'robot_30001') and self.robot_30001:
            try: self.robot_30001.close()
            except Exception: pass
        if hasattr(self, 'robot_29999') and self.robot_29999:
            try: self.robot_29999.close()
            except Exception: pass
        if hasattr(self, 'modbus_client') and self.modbus_client is not None:
            try: self.modbus_client.disconnect()
            except: pass
        
        # 객체 생성 (IP, Port만 입력)
        self.robot_30001 = Robot_30001(self.robot_ip, self.robot_port2)
        self.robot_29999 = Robot_29999(self.robot_ip, self.robot_port1)
        self.modbus_client = Robot_modbus(host=self.modbus_ip, port=self.modbus_port)
        
    # [수정] 0.5초 뒤에 로봇 프로그램을 먼저 Play 시키고 초기화 시퀀스 시작
    def start_init(self):
        print("[INIT] 로봇 프로그램 실행 시도 (Play)")
        self.robot_29999.robot_play() 
        self.is_initializing = True   # 초기화 시작: 인터락 ON
        
        def delayed_init():
            time.sleep(1.0) 
            try:
                # 1. 배출 및 홈 복귀 명령 시퀀스 실행
                success = self.sequence_nr_init()
                
                if success:
                    # ★ [추가] 실제 로봇이 홈 위치에 도착할 때까지 여기서 스레드를 붙잡아둡니다.
                    print("[INIT] 시퀀스 명령 완료. 실제 홈 위치 도착 확인 중...")
                    arrival_timeout = 0
                    while not self.robot_at_home():
                        self.wait_check(ignore_auto_mode=True) # 중단 체크
                        time.sleep(0.2)
                        arrival_timeout += 0.2
                        if arrival_timeout > 15.0: # 15초 타임아웃
                            print("[INIT ERROR] 홈 도착 확인 타임아웃")
                            success = False
                            break
                
                if not success:
                    print("[INIT WARN] 초기화 실패로 인해 예약된 작업을 취소합니다.")
                    self._post_init_cell = None 
            finally:
                # ★ 홈 도착 확인이 끝난 후에야 인터락을 해제합니다.
                print("[INIT] 초기화 종료 및 홈 위치 확인 완료. 작업 인터락 해제.")
                self.is_initializing = False  

        init_thread = threading.Thread(target=delayed_init)
        init_thread.daemon = True
        init_thread.start()

    def start_background_connection(self):
        """프로그램 시작 후 자동으로 연결 시도 (스레드 사용)"""
        print("[System] 백그라운드 자동 연결 시작...")
        self.connect_robot(show_popup=False) # 팝업 없이 조용히 연결 시도
        
    def connect_systems(self):
        """IP 변수를 사용하여 로봇과 IO 모듈에 연결합니다. (재연결 시 기존 연결 종료)"""
        print("[System] 연결 시도 중...")
        
        if hasattr(self, 'alarm_thread') and self.alarm_thread is not None:
            self.alarm_thread.stop()
            self.alarm_thread = None

        # ★ [PATCH] self.robot_30001.__sock 은 BOLT 클래스 안에서 _BOLT__sock 으로 해석되어
        #   항상 실패(→ 소켓 누수)했습니다. 각 클래스의 close()를 사용합니다.
        if hasattr(self, 'robot_30001') and self.robot_30001:
            try: self.robot_30001.close()
            except Exception: pass
        if hasattr(self, 'robot_29999') and self.robot_29999:
            try: self.robot_29999.close()
            except Exception: pass
        if hasattr(self, 'modbus_client') and self.modbus_client is not None:
            try: self.modbus_client.disconnect()
            except: pass
        
        self.robot_30001 = Robot_30001(self.robot_ip, self.robot_port2)
        self.robot_29999 = Robot_29999(self.robot_ip, self.robot_port1)
        self.modbus_client = Robot_modbus(host=self.modbus_ip, port=self.modbus_port)
        
        sock1 = self.robot_30001.connect_30001()
        self.robot_29999.connect_29999()
        self.modbus_client.connect()
        
        if self.io_module is not None:
            if self.io_module.isRunning():
                self.io_module.stop_signal = True 
                self.io_module.quit()
                self.io_module.wait(500) 
            self.io_module = None
            
        # ★ [추가] IO 모듈을 새로 열기 전에 기존 Edge 감지 메모리 삭제
        if hasattr(self, '_prev_io'):
            delattr(self, '_prev_io')
            
        self.io_module = IO_Module_Class(self.io_ip, self.io_port)
        self.io_module.start()

        if sock1: self.robot_disconnected = False
        else: self.robot_disconnected = True
            
    def reset_all_button_lamps(self):
        """모드 변경 시 호출: 모든 버튼 램프(16~20)를 끄고 깜빡임을 초기화합니다."""
        if hasattr(self, 'blink_buttons'):
            self.blink_buttons.stop_blink()
        
        self.last_blink_key = None
        self.last_pressed_status = []

        if self.io_module is not None:
            target_indices = [16, 17, 18, 19, 20]
            
            for idx in target_indices:
                try:
                    if hasattr(self.io_module, 'Write_DO_Data'):
                        cmd = self.io_module.Write_DO_Data(idx, 0)
                        if self.io_module.Send_que is not None:
                            self.io_module.Send_que.put(cmd)
                except Exception as e:
                    print(f"[RESET ERROR] Lamp {idx} reset fail: {e}")

    def update_system_mode(self, mode):
        self.system_mode = mode

        if self.io_module is not None:
            if self.io_module.isRunning():
                self.io_module.stop_signal = True 
                self.io_module.quit()
                print(f"[SYSTEM] 기존 {self.system_mode} IO 모듈 정지 신호 전송 완료 (대기 안 함)")
            self.io_module = None
        
        # ★ [추가] 모드 변경으로 인한 IO 모듈 리셋 시 Edge 감지 메모리 삭제
        if hasattr(self, '_prev_io'):
            delattr(self, '_prev_io')
            
        if self.system_mode == "HEAD":
            self.io_module = IO_Module_Class(self.io_ip, self.io_port)
            print("[SYSTEM] Head용 IO 모듈 로드됨")
        else: # BACK
            self.io_module = IO_Module_Class(self.io_ip, self.io_port)
            print("[SYSTEM] Back용 IO 모듈 로드됨")
            
        self.io_module.start()
        time.sleep(0.1)
        self.reset_all_button_lamps()
        self.init_io_ui()
        self.init_robot_var_ui()
        self.save_current_mode()
        self.update_mode_image()
        is_head_mode = (self.system_mode == "HEAD") # HEAD면 True, BACK이면 False
        try:
            self.ui.cell_1_btn.setEnabled(is_head_mode)
            self.ui.cell_2_btn.setEnabled(is_head_mode)
            self.ui.pose_9_btn.setEnabled(is_head_mode)
            self.ui.pose_10_btn.setEnabled(is_head_mode)
            self.ui.pose_11_btn.setEnabled(is_head_mode)
            print(f"[UI] 버튼 활성화 상태 변경 완료 (is_head_mode: {is_head_mode})")
        except AttributeError as e:
            # ui 파일에 버튼 이름이 다를 경우를 대비한 예외 처리
            print(f"[WARN] 버튼 상태 변경 실패 (위젯 이름을 확인하세요): {e}")
        # ==========================================
        print(f"[SYSTEM] 모드 변경 시도: {self.system_mode} -> {mode}")
        
    def load_last_mode(self):
        """DB에서 system_mode 값을 읽어옵니다."""
        if hasattr(self, 'db_manager'):
            return self.db_manager.load_setting("system_mode", "HEAD")
        return "HEAD"

    def save_current_mode(self):
        """현재 system_mode를 DB에 저장합니다."""
        if hasattr(self, 'db_manager'):
            self.db_manager.save_setting("system_mode", self.system_mode)
            print(f"[SYSTEM] 현재 모드 '{self.system_mode}' DB 저장 완료")
            
    def update_mode_image(self):
        """현재 system_mode에 따라 image_frame의 이미지를 변경합니다."""
        if self.system_mode == "HEAD":
            img_name = "head_points.png"
        else:
            img_name = "back_points.png"
            
        img_path = APP_ROOT / img_name
        
        if os.path.exists(img_path):
            # Qt 스타일시트에서 경로를 인식할 수 있도록 슬래시(/)로 변환
            img_path_str = img_path.as_posix()
            
            # QFrame에 맞게 스타일시트로 배경 이미지 적용 (setScaledContents와 동일한 효과)
            self.ui.image_frame.setStyleSheet(f"border-image: url('{img_path_str}');")
            
            print(f"[UI] {self.system_mode} 모드 이미지 로드 완료: {img_name}")
        else:
            print(f"[WARN] 모드 이미지를 찾을 수 없습니다: {img_path}")
            self.ui.image_frame.setStyleSheet("") # 파일이 없으면 이미지 비우기

    def eventFilter(self, obj, event):
        if obj == self.ui.logo and event.type() == QtCore.QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                self.handle_logo_click()
                return True 
                
        # [추가] workload_edit 라벨 더블 클릭 감지
        if obj == self.ui.workload_edit and event.type() == QtCore.QEvent.MouseButtonDblClick:
            if event.button() == Qt.LeftButton:
                self.show_work_history_dialog()
                return True
            
        if obj == self.ui.history_table and event.type() == QtCore.QEvent.Resize:
            QtCore.QTimer.singleShot(10, self.update_history_table_widths)
                
        return super().eventFilter(obj, event)
    
    def show_work_history_dialog(self):
        dialog = WorkHistoryDialog(self)
        dialog.exec_()

    def handle_logo_click(self):
        if self.logo_click_count == 0:
            self.logo_timer.start()
        self.logo_click_count += 1
        
        if self.logo_click_count >= 5:
            self.reset_logo_count() 
            self.show_admin_login()
    
    def show_admin_login(self):
        """가상 키보드를 띄우고 커스텀 다이얼로그로 비밀번호 입력"""
        self.show_touch_keyboard()
        
        password, ok = self.show_custom_input_dialog(
            title="관리자 암호 입력", 
            label_text="비밀번호를 입력하세요:", 
            echo_mode=QLineEdit.Password
        )
        
        self.hide_touch_keyboard()
        
        if ok and password:
            # [수정] DB에서 현재 비밀번호를 불러옵니다. (없으면 기본값 1234)
            current_pw = self.db_manager.load_setting("admin_password", "1234")
            
            if password == current_pw: 
                self.show_mode_selection()
            else:
                self.show_message("오류", "비밀번호가 틀렸습니다.", QMessageBox.Critical)

    def reset_logo_count(self):
        self.logo_click_count = 0
        self.logo_timer.stop()

    def show_mode_selection(self):
        # 1. 커스텀 다이얼로그 생성
        dialog = QDialog(self)
        dialog.setWindowTitle("작업 위치 설정")
        dialog.setFixedSize(500, 300) # 높이를 살짝 늘림 (버튼 2줄)
        
        # 2. 레이아웃 설정
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 30, 20, 30)
        
        # 3. 라벨 (안내 문구)
        lbl_info = QLabel("현재 시스템의 작업을 선택해주세요.")
        lbl_info.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl_info)
        
        layout.addSpacing(20)

        # 4. 버튼 영역 레이아웃 (위: Head/Back, 아래: 비번변경/취소)
        btn_layout_top = QHBoxLayout()
        btn_layout_bottom = QHBoxLayout()
        btn_layout_top.setSpacing(15)
        btn_layout_bottom.setSpacing(15)
        
        btn_head = QPushButton("Head (헤드)")
        btn_back = QPushButton("Back (백)")
        btn_change_pw = QPushButton("비밀번호 변경")
        btn_cancel = QPushButton("닫 기")
        
        for btn in [btn_head, btn_back, btn_change_pw, btn_cancel]:
            btn.setFixedSize(140, 60) # 버튼 크기 통일

        btn_layout_top.addWidget(btn_head)
        btn_layout_top.addWidget(btn_back)
        btn_layout_bottom.addWidget(btn_change_pw)
        btn_layout_bottom.addWidget(btn_cancel)
        
        layout.addLayout(btn_layout_top)
        layout.addLayout(btn_layout_bottom)
        dialog.setLayout(layout)

        # 5. 스타일 적용 (기존과 동일)
        dialog.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                border: 2px solid #0078D7;
            }
            QLabel {
                color: white;
                font-size: 20px;
                font-weight: bold;
                background-color: transparent;
            }
            QPushButton {
                background-color: #555555;
                color: white;
                font-size: 18px;
                font-weight: bold;
                border-radius: 5px;
                border: 1px solid #888;
            }
            QPushButton:hover {
                background-color: #0078D7;
                border: 1px solid #005a9e;
            }
            QPushButton:pressed {
                background-color: #333333;
            }
        """)

        # 6. 위치 조정
        screen_geo = QApplication.desktop().screenGeometry()
        dialog.move(int(screen_geo.width() / 2 - dialog.width() / 2), int(screen_geo.height() * 0.25))

        # 7. 버튼 기능 연결
        selected_mode = [None] 

        def select_head():
            selected_mode[0] = "HEAD"
            dialog.accept() 

        def select_back():
            selected_mode[0] = "BACK"
            dialog.accept() 

        btn_head.clicked.connect(select_head)
        btn_back.clicked.connect(select_back)
        btn_cancel.clicked.connect(dialog.reject)
        
        # [추가] 비밀번호 변경 로직 연결
        btn_change_pw.clicked.connect(lambda: self.change_admin_password(dialog))

        # 8. 실행
        dialog.exec_()

        # 9. 결과 처리
        if selected_mode[0] == "HEAD":
            self.update_system_mode("HEAD")
            self.show_message("설정 완료", "시스템이 [Head] 모드로 설정되었습니다.")
            
        elif selected_mode[0] == "BACK":
            self.update_system_mode("BACK")
            self.show_message("설정 완료", "시스템이 [Back] 모드로 설정되었습니다.")
            
    def change_admin_password(self, parent_dialog):
        """비밀번호 변경 다이얼로그를 띄우고 DB에 저장합니다."""
        # 1. 새 비밀번호 입력창 띄우기
        self.show_touch_keyboard()
        new_pw, ok1 = self.show_custom_input_dialog(
            title="새 비밀번호 입력", 
            label_text="새로운 비밀번호를 입력하세요:", 
            echo_mode=QLineEdit.Password
        )
        self.hide_touch_keyboard()

        if not ok1 or not new_pw:
            return

        # 2. 새 비밀번호 확인창 띄우기
        self.show_touch_keyboard()
        confirm_pw, ok2 = self.show_custom_input_dialog(
            title="새 비밀번호 확인", 
            label_text="비밀번호를 다시 한 번 입력하세요:", 
            echo_mode=QLineEdit.Password
        )
        self.hide_touch_keyboard()

        if not ok2 or not confirm_pw:
            return

        # 3. 검증 및 DB 저장
        if new_pw == confirm_pw:
            try:
                self.db_manager.save_setting("admin_password", new_pw)
                self.show_message("변경 완료", "비밀번호가 성공적으로 변경되었습니다.")
                parent_dialog.accept() # 비밀번호 변경 성공 시 모드 창도 닫아줌
            except Exception as e:
                self.show_message("오류", f"DB 저장 중 오류가 발생했습니다.\n{e}", QMessageBox.Critical)
        else:
            self.show_message("오류", "입력한 두 비밀번호가 일치하지 않습니다.", QMessageBox.Warning)
        
    def on_auto_mode_toggled(self, checked):
        # 1. 로봇 연결 끊김 상태면 강제로 끄고 리턴
        if self.robot_disconnected:
            self.ui.auto_btn.blockSignals(True)
            self.ui.auto_btn.setChecked(False)
            self.ui.auto_btn.setDisabled(True)
            self.ui.auto_btn.blockSignals(False)
            return

        # 2. 체크된 경우 (Manual -> Auto 전환 시도)
        if checked:
            # =========================================================================
            # ★ [안전 인터락 추가] 자동 모드 시작 전 위험 IO 강제 검사
            # =========================================================================
            if self.io_module is not None:
                outputs = self.io_module.Read_Output_Data()
                if outputs and len(outputs) >= 41:
                    # 32: NR START, 39: 실린더 전진, 40: 흡착(진공)
                    if outputs[32] == 1 or outputs[39] == 1 or outputs[40] == 1:
                        print("[INTERLOCK] 위험 상태 감지! 자동 모드 진입 차단.")
                        
                        msg = "<b>위험: 자동 모드를 시작할 수 없습니다.</b><br><br>"
                        msg += "현재 너트러너 실린더가 전진해 있거나<br>흡착/START 신호가 켜져 있습니다.<br>"
                        msg += "수동으로 설비를 원위치시킨 후 다시 시도하세요."
                        
                        self.show_message("조작 불가 (안전 인터락)", msg, QMessageBox.Warning)
                        
                        # 버튼 강제 원복
                        self.ui.auto_btn.blockSignals(True)
                        self.ui.auto_btn.setChecked(False)
                        self.ui.auto_btn.blockSignals(False)
                        return

            print("[INFO] 로봇 자동 모드 선택됨")
            self.auto_mode = True
            self.robot_29999.robot_play()
            
            # 수동 버튼을 강제로 끄되, 수동 버튼의 이벤트가 발생하지 않도록 신호 차단
            self.ui.manual_btn.blockSignals(True)
            self.ui.manual_btn.setChecked(False)
            self.ui.manual_btn.blockSignals(False)
            
        # 3. 체크 해제된 경우 (이미 Auto인데 Auto를 또 눌러서 끄려는 시도)
        else:
            # 끄지 못하게 다시 켬 (신호 차단하여 재귀 호출 방지)
            self.ui.auto_btn.blockSignals(True)
            self.ui.auto_btn.setChecked(True)
            self.ui.auto_btn.blockSignals(False)
            
    def reset_all_robot_variables(self):
        """PC에서 로봇으로 쏘는 모든 Input 변수를 0(False)으로 초기화"""
        try:
            config_name = f"{self.system_mode.lower()}_config.json"
            config_path = APP_ROOT / "DB" / "config" / config_name
            
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    var_inputs = config_data.get("ROBOT_VAR_INPUT", {})
                    
                # JSON에 등록된 모든 INPUT 변수에 0을 쏩니다.
                for addr_str, var_name in var_inputs.items():
                    self.set_variable_with_ui(var_name, 0)
                    
            print("[INFO] 수동 전환: 로봇 전송 변수 전체 0으로 초기화 완료")
        except Exception as e:
            print(f"[ERROR] 로봇 변수 초기화 실패: {e}")
            
    def on_manual_mode_toggled(self, checked):
        # 1. 체크된 경우 (Auto -> Manual 전환 시도)
        if checked:
            print("[INFO] 로봇 수동 모드 선택됨")
            self.auto_mode = False
            self.robot_29999.robot_stop()
            self.reset_all_robot_variables()
            # 자동 버튼을 강제로 끄되, 이벤트 발생 차단
            self.ui.auto_btn.blockSignals(True)
            self.ui.auto_btn.setChecked(False)
            self.ui.auto_btn.blockSignals(False)
            
        # 2. 체크 해제된 경우 (이미 Manual인데 Manual을 또 눌러서 끄려는 시도)
        else:
            # 끄지 못하게 다시 켬
            self.ui.manual_btn.blockSignals(True)
            self.ui.manual_btn.setChecked(True)
            self.ui.manual_btn.blockSignals(False)
        
    def on_main_btn_clicked(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.main_page)
    
    def on_quality_btn_clicked(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.quality_page)
        QtCore.QTimer.singleShot(50, self.update_history_table_widths)

    def on_io_btn_clicked(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.io_page)
    
    def on_robot_btn_clicked(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.robot_page)
        
    def on_quality_log_btn_clicked(self):
        """[이전 기록 보기] 버튼 클릭 시 파일 탐색기를 열고 뷰어를 실행합니다."""
        
        # 1. 탐색기를 띄울 기본 폴더 지정 (이번 달 폴더 우선)
        month_str = QDateTime.currentDateTime().toString("yyyy-MM")
        
        # 파일이 "DB/history" 또는 "Logs/History" 중 어디에 저장되었는지 확인하여 경로 유동적 적용
        log_dir = APP_ROOT / "DB" / "history" 
        if not log_dir.exists():
            log_dir = APP_ROOT / "Logs" / "History"
            if not log_dir.exists():
                log_dir.mkdir(parents=True, exist_ok=True)
                
        start_dir = log_dir / month_str
        if not start_dir.exists():
            start_dir = log_dir # 이번 달 폴더가 없으면 상위 폴더 띄움

        # 2. 파일 선택 창 (다이얼로그) 열기
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "과거 작업 기록 파일 열기",
            str(start_dir),
            "CSV Files (*.csv)"
        )

        # 3. 파일을 선택했다면 뷰어 팝업 실행
        if file_path:
            viewer = CSVViewerDialog(file_path, self)
            viewer.exec_()
        
    def connect_robot(self, show_popup=False):
        if hasattr(self, 'conn_thread') and self.conn_thread.isRunning():
            return

        self.show_popup_on_fail = show_popup
        self.ui.robot_connect_button.setText("시스템 연결 중...")
        self.ui.robot_connect_button.setEnabled(False) 

        self.create_robot_objects()
        
        # IO 모듈 재시작
        if self.io_module is not None:
            if self.io_module.isRunning():
                self.io_module.stop_signal = True 
                self.io_module.quit()
                self.io_module.wait(200)
            self.io_module = None
            
        # ★ [추가] IO 모듈을 새로 열기 전에 기존 Edge 감지 메모리 삭제 (단선 알람 오작동 방지)
        if hasattr(self, '_prev_io'):
            delattr(self, '_prev_io')
            
        self.io_module = IO_Module_Class(self.io_ip, self.io_port)
        self.io_module.start()

        self.conn_thread = ConnectionThread(self.robot_29999, self.robot_30001, self.modbus_client, self.io_ip, self.io_port)
        self.conn_thread.finished_signal.connect(self.on_connect_finished)
        self.conn_thread.start()
        
    def on_connect_finished(self, success, message):
        if success:
            # 연결 성공 처리
            self.robot_disconnected = False
            self.update_connect_button_state()
            print(f"[INFO] 로봇 연결 성공 ({self.robot_ip})")
            self.ui.scrollArea.append("[INFO] 로봇 연결 성공")
            self.ui.power_on_button.setEnabled(True)
            
            self.init_robot_var_ui()
            
            if self.alarm_thread is None or not self.alarm_thread.isRunning():
                # ★ [수정] self.robot_30001 대신 self(메인 클래스 자체)를 넘김
                self.alarm_thread = AlarmThread(self, self.alarm_manager)
                self.alarm_thread.alarm_received_signal.connect(self.add_alarm_log)
                self.alarm_thread.start()
                print("[INFO] 알람 감지 스레드 시작됨")
                
            self.check_initial_robot_alarm()
            
            QtCore.QTimer.singleShot(1000, lambda: self.apply_product_type(self.current_product_type))
            
        else:
            # 연결 실패 처리
            self.robot_disconnected = True
            self.update_connect_button_state() # 버튼 원래대로 복구
            print(f"[ERROR] 로봇 연결 실패: {message}")
            self.ui.scrollArea.append(f"[ERROR] 로봇 연결 실패: {message}")

            # 사용자가 버튼 눌렀을 때만 팝업 띄움
            if self.show_popup_on_fail:
                self.show_message(
                    "연결 실패", 
                    f"로봇 연결에 실패했습니다.<br>IP: {self.robot_ip}<br>Error: {message}", 
                    QMessageBox.Critical
                )
    
    def check_initial_robot_alarm(self):
        """
        프로그램 시작(연결) 직후 29999 포트의 'status' 명령을 통해 
        이미 발생해 있는 로봇 세이프티 알람을 스캔합니다.
        """
        try:
            # 1. 상태 요청 (29999 포트)
            status_res = self.robot_29999.send_command_29999("status", multiline=True)
            if not status_res:
                print("[WARN] 초기 상태 읽기 실패 (응답 없음)")
                return

            safety_mode_str = None
            
            # 2. 결과 텍스트 라인별로 파싱하여 SafetyMode 찾기
            for line in status_res.split('\n'):
                if "SafetyMode:" in line:
                    # "SafetyMode: VIOLATION" 에서 "VIOLATION"만 추출
                    safety_mode_str = line.split(":")[1].strip()
                    break

            if safety_mode_str:
                print(f"[INIT CHECK] 현재 로봇 SafetyMode: {safety_mode_str}")
                
                # 정상 상태(알람이 아닌 상태) 목록
                # 문자로 올 경우와 숫자로 올 경우 모두 대비
                safe_modes = ["NORMAL", "REDUCED", "1", "2"]
                
                # 현재 상태가 정상 상태 목록에 없다면 알람으로 간주
                if safety_mode_str.upper() not in safe_modes:
                    print(f"[INIT ALARM] 로봇 안전 에러 상태 발견됨! -> {safety_mode_str}")
                    
                    # 즉시 메인 화면에 알람 로그 추가 및 팝업/인터락 발동
                    alarm_msg = f"로봇 세이프티 알람 (상태: {safety_mode_str})"
                    self.report_alarm_signal.emit(alarm_msg, "ERROR")
                    
        except Exception as e:
            print(f"[INIT ALARM ERROR] 로봇 초기 알람 확인 중 오류: {e}")

    def check_connection_alive(self):
        try:
            # 1. 29999 포트에 상태(status) 명령 전송
            status_res = self.robot_29999.send_command_29999("status", multiline=True)
            
            # 응답이 아예 없거나 "Error" 텍스트가 포함되어 있으면 통신 끊김
            if not status_res or "Error" in status_res:
                return False

            # 2. 실제 반환 포맷 검증
            # 반환값에 "RobotMode:" 와 "SafetyMode:" 가 정상적으로 들어있는지 확인
            if "RobotMode:" in status_res and "SafetyMode:" in status_res:
                # ★ [수정] 30001 타임아웃 검사 로직 삭제. 29999가 대답하면 살아있는 것임!
                return True 
            
            # 만약 RobotMode 글자가 없는 이상한 쓰레기값이 넘어왔다면 연결 불량으로 간주
            return False 
            
        except Exception:
            return False
        
    def button_lamp_control(self, status):
        if self.io_module is None: return
        if self.system_mode == "HEAD":
            target_indices = [16, 17, 18, 19]
        else:
            target_indices = [19, 20]
        if len(status) != len(target_indices):
            return
        for idx, val_str in zip(target_indices, status):
            bit_val = 1 if val_str == "ON" else 0
            if hasattr(self.io_module, 'Write_DO_Data'):
                cmd = self.io_module.Write_DO_Data(idx, bit_val)
                if self.io_module.Send_que is not None:
                    self.io_module.Send_que.put(cmd)
                    
    def control_light_curtain_mute(self, enable):
        """라이트 커튼 뮤팅 제어 (단 1회 시퀀스 실행 보장 및 무한 반복 방지)"""
        if self.io_module is None: return
        
        # ★ [중요] 이미 시퀀스가 진행 중이면 어떠한 간섭도 무시하고 즉시 리턴
        if getattr(self, '_mute_changing', False):
            return

        outputs = self.io_module.Read_Output_Data()
        if not outputs or len(outputs) <= 25:
            return

        if enable:
            # 켜야 하는데 둘 중 하나라도 꺼져있다면 시퀀스 '1회' 실행
            if outputs[24] == 0 or outputs[25] == 0:
                self._mute_changing = True # 락(Lock) 온
                
                # 1. 초기화 및 리셋 ON
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 0))
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 0))
                # self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 1))
                
                # def step_reset_off():
                #     if self.io_module is None: self._mute_changing = False; return
                #     self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 0))
                #     QtCore.QTimer.singleShot(100, step_mute_a_on)
                    
                def step_mute_a_on():
                    if self.io_module is None: self._mute_changing = False; return
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
                    QtCore.QTimer.singleShot(200, step_mute_b_on)
                    
                def step_mute_b_on():
                    if self.io_module is None: self._mute_changing = False; return
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 1))
                    
                    # 릴레이가 물리적으로 완전히 붙고 상태값이 업데이트될 여유 시간 제공
                    QtCore.QTimer.singleShot(200, release_lock)

                def release_lock():
                    # 모든 동작이 확실히 끝난 뒤에만 락 해제
                    self._mute_changing = False
                    
                # # 시퀀스 시작
                # QtCore.QTimer.singleShot(100, step_reset_off)
                QtCore.QTimer.singleShot(100, step_mute_a_on)
                
        else:
            # 감시 시작 (Mute OFF)
            if outputs[24] == 1 or outputs[25] == 1:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 0))
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 0))

    def force_sync_mute(self):
        """[강력한 뮤트 보장] 백그라운드 타이머에 의존하지 않고, 작업 스레드에서 직접 동기식으로 완벽하게 뮤트를 체결합니다."""
        if self.io_module is None: return
        
        # 1. 이미 백그라운드에서 진행 중이라면 충돌 방지를 위해 끝날 때까지 대기
        wait_cnt = 0
        while getattr(self, '_mute_changing', False) and wait_cnt < 10:
            time.sleep(0.1)
            wait_cnt += 1

        # 2. 현재 상태 확인
        outputs = self.io_module.Read_Output_Data()
        if outputs and len(outputs) > 25 and outputs[24] == 1 and outputs[25] == 1:
            # 이미 완벽하게 켜져 있다면 대기 시간만 주고 종료
            time.sleep(0.3)
            return

        # 3. 뮤트 시퀀스 강제 진행
        self._mute_changing = True # 백그라운드 폴링 간섭 락(Lock)
        
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 0))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 0))
        time.sleep(0.1)
        
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
        time.sleep(0.2) # A 접점 후 B 접점 연동 딜레이
        
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 1))
        
        # ★ 가장 핵심: 하드웨어 릴레이가 완벽하게 접점을 형성할 때까지 충분히(0.6초) 기다립니다.
        time.sleep(0.6) 
        
        self._mute_changing = False
        
    def execute_delayed_back_job(self, trigger_cell):
        """BACK 모드 전용: 양수버튼 1.5초 대기 후 초기화 또는 본 작업 실행 (예약 없음)"""
        self.back_mute_delaying = False
        print(f"[SYSTEM] 1.5초 대기 완료. 감시 모드 전환(Mute OFF) 및 작업 판단...")
        
        # 1. 프로그램 첫 실행 시 초기화(배출) 먼저
        if not getattr(self, '_is_first_boot_init_done', False):
            print(f"[SYSTEM] 백 모드 최초 양수 버튼! 배출 시퀀스 선행 실행")
            self._is_first_boot_init_done = True
            self._post_init_cell = trigger_cell
            self.start_init()
            
        # 2. 백 모드는 예약(Queue)이 없으므로 조건 없이 바로 작업 시작
        else:
            self.start_job(cell_num=trigger_cell)
        
    def poll_robot_state(self):
        # ============================================================
        # 1. 스레드 프리징 방지 (중복 실행 차단 락)
        # ============================================================
        if getattr(self, '_is_polling', False):
            return
        self._is_polling = True
        
        try:
            # 로봇이 끊겨있으면 상태만 업데이트하고 리턴
            if self.robot_disconnected:
                self.robot_status_update()
                # return
            
            # ============================================================
            # 2. [모드버스 데이터 스캔]
            # ============================================================
            if hasattr(self, 'modbus_client') and self.modbus_client is not None:
                try:
                    START_ADDR = 400
                    READ_COUNT = 20
                    with self.modbus_lock:
                        bits = self.modbus_client.get_all_coils(START_ADDR, READ_COUNT)
                    
                    if bits:
                        config_name = f"{self.system_mode.lower()}_config.json"
                        config_path = APP_ROOT / "DB" / "config" / config_name
                        if config_path.exists():
                            with open(config_path, 'r', encoding='utf-8') as f:
                                config_data = json.load(f)
                                var_outputs = config_data.get("ROBOT_VAR_OUTPUT", {})
                                
                            for addr_str, var_name in var_outputs.items():
                                if addr_str.isdigit():
                                    absolute_addr = int(addr_str)
                                    list_idx = absolute_addr - START_ADDR
                                    
                                    if 0 <= list_idx < len(bits):
                                        val = bits[list_idx]
                                        self.cached_robot_vars[var_name] = val
                                        self.update_robot_var_ui(var_name, val)
                except Exception as e:
                    pass # 통신 에러 무시 (다음 사이클에서 재시도)

            # ============================================================
            # 3. [로봇 데이터 수신 및 상태 업데이트]
            # ============================================================
            try:
                data = self.robot_30001.get_data()
                
                # 데이터가 안 들어올 때 (None)
                if data is None:
                    # 5초 이상 데이터가 없으면 생존 확인 시도
                    if time.time() - self.last_data_time > 5.0:
                        is_alive = self.check_connection_alive()
                        if is_alive:
                            # 29999 포트가 살아있다면, 로봇이 바빠서 30001을 못 보내는 것이므로 타임아웃 연장
                            self.last_data_time = time.time()
                        else:
                            # 29999 포트마저 죽었다면 진짜 끊긴 것
                            self.on_robot_disconnected()
                
                # 데이터가 정상적으로 들어올 때
                else:
                    self.last_data_time = time.time() # 타임아웃 리셋

                    self.is_robot_power_on = data.is_robot_power_on
                    self.is_task_running = data.is_task_running
                    self.is_task_paused = data.is_task_paused
                    self.safety_mode = data.safety_mode
                    self.robot_mode = data.robot_mode
                    self.robot_speed = data.target_speed_fraction * 100

                    self.values = (
                        self.is_robot_power_on, self.is_task_running, self.is_task_paused,
                        self.safety_mode, self.robot_mode, self.robot_speed
                    )
                    self.robot_status_update()
                    
                    self.actual_joint_base = data.actual_joint[0]
                    self.actual_joint_shoulder = data.actual_joint[1]
                    self.actual_joint_elbow = data.actual_joint[2]
                    self.actual_joint_wrist1 = data.actual_joint[3]
                    self.actual_joint_wrist2 = data.actual_joint[4]
                    self.actual_joint_wrist3 = data.actual_joint[5]

                    # ★ [PATCH] 수동 moveL 도착 검증용 TCP 좌표
                    self.actual_tcp = [data.tcp_x, data.tcp_y, data.tcp_z,
                                       data.rot_x, data.rot_y, data.rot_z]

            except Exception as e:
                pass

            # ============================================================
            # 4. [IO 제어 및 자동 모드 로직 판단]
            # ============================================================
            if self.io_module is not None:
                io_list = self.io_module.Read_Input_Data()
                
                # ★ [수정] IO 모듈 통신이 끊겼거나 데이터가 불량일 때 강력한 알람 처리
                if not io_list or len(io_list) <= 44:
                    if not getattr(self, '_io_error_reported', False):
                        # 중복 알람을 방지하며 1회 팝업 및 정지 처리
                        self.report_alarm_signal.emit("IO 모듈 통신 끊김! (전원/랜선 확인)", "ERROR")
                        self.on_stop_button_clicked()
                        self._io_error_reported = True
                        
                        # 연결 상태를 끊김으로 강제 전환
                        self.robot_disconnected = True
                        self.update_connect_button_state()
                        self.robot_status_update()
                    return
                else:
                    self._io_error_reported = False # 정상 복구 시 플래그 리셋

                if not getattr(self, '_mdo22_initialized', False):
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(22, 1))
                    self._mdo22_initialized = True

                # ----------------------------------------------------
                # ★ 상태 변수 사전 정의 (뮤트 및 작업 상태 판단용)
                # ----------------------------------------------------
                t1 = self.threads.get(1)
                t2 = self.threads.get(2)
                t0 = self.threads.get(0)
                
                is_working = (t1 and t1.isRunning()) or (t2 and t2.isRunning()) or (t0 and t0.isRunning())
                is_init = getattr(self, 'is_initializing', False)
                is_robot_occupied = (self.robot_occupant != 0)
                
                # [BACK 모드 전용 상태 변수]
                is_delaying = getattr(self, 'back_mute_delaying', False) # 1.5초 대기 깃발
                is_popup_0 = self.active_popups.get(0) is not None       # 팝업 활성화 여부
                is_reserved_0 = (getattr(self, '_post_init_cell', None) == 0)
                cell0_busy = self.is_back_working or (t0 and t0.isRunning()) or is_init or is_robot_occupied or is_reserved_0

                # [HEAD 모드 전용 상태 변수]
                is_reserved_1 = (self.waiting_cell == 1) or (getattr(self, '_post_init_cell', None) == 1)
                is_reserved_2 = (self.waiting_cell == 2) or (getattr(self, '_post_init_cell', None) == 2)
                cell1_busy = (t1 and t1.isRunning()) or is_reserved_1 or (is_init and getattr(self, '_post_init_cell', None) == 1)
                cell2_busy = (t2 and t2.isRunning()) or is_reserved_2 or (is_init and getattr(self, '_post_init_cell', None) == 2)

                # =====================================================================
                # ★ [HEAD 모드 뮤트 완벽 제어] 팝업 무시! 물리적 센서 및 모션 기반
                # =====================================================================
                # 제품이 센서에서 사라지면 언로드 대기 상태 해제 (MDI 6: Cell 1 제품, MDI 14: Cell 2 제품)
                if getattr(self, 'wait_unload_1', False) and io_list[6] == 0:
                    self.wait_unload_1 = False
                if getattr(self, 'wait_unload_2', False) and io_list[14] == 0:
                    self.wait_unload_2 = False

                # ★ [핵심 추가] 시스템이 뮤트를 의도하고 있는가? (알람 무시 판별용)
                is_mute_intended = False
                
                if getattr(self, 'is_resetting', False) or getattr(self, 'mute_block_after_reset', False):
                    is_mute_intended = False
                else:
                    if not self.auto_mode:
                        is_mute_intended = True # 수동 모드 무조건 뮤트
                    else:
                        if self.system_mode == "BACK":
                            is_mute_intended = is_delaying or is_popup_0 or not cell0_busy or getattr(self, 'seq_mute_active', False)
                        
                        elif self.system_mode == "HEAD":
                            any_jig_moving = getattr(self, 'is_jig_moving_1', False) or getattr(self, 'is_jig_moving_2', False)
                            # wait_any_unload = getattr(self, 'wait_unload_1', False) or getattr(self, 'wait_unload_2', False)
                            
                            # is_mute_intended = getattr(self, 'seq_mute_active', False) or any_jig_moving or wait_any_unload or (not cell1_busy and not cell2_busy)
                            is_mute_intended = getattr(self, 'seq_mute_active', False) or any_jig_moving 
                            
                # ----------------------------------------------------
                # 4-1. 라이트 커튼 뮤트 자동 판단 (중앙 통제)
                # ----------------------------------------------------
                if not self.auto_mode:
                    self.control_light_curtain_mute(is_mute_intended)
                    
                else:
                    if self.is_alarm_state():
                        self.seq_mute_active = False
                        self.control_light_curtain_mute(False)
                    else:
                        self.control_light_curtain_mute(is_mute_intended)

                # ----------------------------------------------------
                # 4-2. 하드웨어 알람 감시 (Edge -> Level + State 방식)
                # ----------------------------------------------------
                if not hasattr(self, '_prev_io') or len(self._prev_io) != len(io_list):
                    self._prev_io = list(io_list)
                
                curr_io = io_list

                if not getattr(self, 'is_resetting', False) and not getattr(self, '_is_software_error', False):
                    
                    # ========================================================
                    # [진짜 비상정지 그룹] - 시스템 에러(ERROR) 유발 및 로봇 차단
                    # ========================================================
                    # 1. 비상정지 버튼 (MDI 22)
                    if curr_io[22] == 1:
                        self.report_alarm_signal.emit("비상정지(E-STOP) 버튼 감지됨!", "ERROR")
                        self.ui.buzzer_off_button.setChecked(False)
                        self.on_stop_button_clicked()
                        
                    # 2. 라이트 커튼 (MDI 24)
                    elif curr_io[24] == 1 and not is_mute_intended:
                        self.report_alarm_signal.emit("라이트 커튼 감지!", "ERROR")
                        self.on_stop_button_clicked()

                    # 3. 세이프티 릴레이 단선 (MDI 21)
                    elif curr_io[21] == 0 and self._prev_io[21] == 1:
                        self.report_alarm_signal.emit("하드웨어 세이프티 릴레이 단선 감지!", "ERROR")
                        self.on_stop_button_clicked()

                # ========================================================
                # [경고 및 일반 정지 그룹] - 작업만 멈추고 로봇 에러(ERROR)는 안 띄움
                # ========================================================
                # 4. 너트러너 에러 (MDI 35) -> 정지 버튼 누르면 발생함
                if curr_io[35] == 1 and self._prev_io[35] == 0:
                    self.report_alarm_signal.emit("너트러너 시스템 에러 (경고)", "WARN")
                    self.on_stop_button_clicked() 
                    
                # 5. 피더기 에러 (MDI 44)
                if curr_io[44] == 1 and self._prev_io[44] == 0:
                    if getattr(self, 'is_purging', False):
                        print("[SYSTEM] 볼트 비우기 진행 중이므로 피더기 에러 알람을 무시합니다.")
                    else:
                        self.report_alarm_signal.emit("피더기(SFD) 에러 (경고)", "WARN")
                        self.on_stop_button_clicked()

                # 비상정지 복귀 시 부저 끄기 버튼 초기화
                if curr_io[22] == 0 and self._prev_io[22] == 1:
                    self.ui.buzzer_off_button.setChecked(False)

                self._prev_io = list(curr_io)

                # ----------------------------------------------------
                # 4-3. 램프 상태 분석 및 양수 버튼 대기열 확인
                # ----------------------------------------------------
                IS_INPUT_INVERTED = False 
                btn_pressed_list = []  
                target_lamp_states = [] 
                req_start_signals = {}  

                if self.system_mode == "HEAD":
                    raw_vals = [io_list[i] for i in [16, 17, 18, 19]]
                    btn_pressed_list = [(v == 0) if IS_INPUT_INVERTED else (v == 1) for v in raw_vals]
                    
                    is_reserved_1 = (self.waiting_cell == 1) or (getattr(self, '_post_init_cell', None) == 1)
                    is_reserved_2 = (self.waiting_cell == 2) or (getattr(self, '_post_init_cell', None) == 2)

                    cell1_busy = (t1 and t1.isRunning()) or is_reserved_1 or (is_init and self._post_init_cell == 1)
                    cell2_busy = (t2 and t2.isRunning()) or is_reserved_2 or (is_init and self._post_init_cell == 2)

                    is_popup_1 = self.active_popups.get(1) is not None
                    is_popup_2 = self.active_popups.get(2) is not None

                    cell1_ready = (io_list[6] == 1 and io_list[7] == 0)
                    cell2_ready = (io_list[14] == 1 and io_list[15] == 0)

                    # ==========================================================
                    # ★ [HEAD 추가] 완료 후 제품을 빼면 팝업 자동 종료 및 램프 리셋
                    # ==========================================================
                    if is_popup_1 and io_list[6] == 0:
                        try: self.active_popups[1].close()
                        except: pass
                        self.active_popups[1] = None
                        is_popup_1 = False
                        
                    if is_popup_2 and io_list[14] == 0:
                        try: self.active_popups[2].close()
                        except: pass
                        self.active_popups[2] = None
                        is_popup_2 = False

                    # ----------------------------------------------------------
                    # ★ [수정] 램프 로직 직관적으로 변경 (사용자 요청 반영)
                    # ----------------------------------------------------------
                    if cell1_busy: c1_st = "OFF"           # 작업 중이거나 예약됨: 꺼짐
                    elif is_popup_1: c1_st = "BLINK"       # 작업 완료: 깜빡임 (제품을 빼주세요)
                    elif cell1_ready: c1_st = "ON"         # 작업물 있음 (정상 안착): 켜짐 (ON)
                    else: c1_st = "BLINK"                  # 작업물 없음 (투입 대기): 깜빡임

                    if cell2_busy: c2_st = "OFF"
                    elif is_popup_2: c2_st = "BLINK"  
                    elif cell2_ready: c2_st = "ON"
                    else: c2_st = "BLINK"

                    target_lamp_states = [c1_st, c1_st, c2_st, c2_st]

                    req_start_signals[1] = btn_pressed_list[0] and btn_pressed_list[1]
                    req_start_signals[2] = btn_pressed_list[2] and btn_pressed_list[3]
                    
                else: # BACK 모드
                    raw_vals = [io_list[i] for i in [19, 20]]
                    btn_pressed_list = [(v == 0) if IS_INPUT_INVERTED else (v == 1) for v in raw_vals]

                    is_t0_running = (t0 and t0.isRunning())
                    is_robot_occupied = (self.robot_occupant != 0)
                    is_reserved_0 = (getattr(self, '_post_init_cell', None) == 0)
                    
                    cell0_busy = self.is_back_working or is_t0_running or is_init or is_robot_occupied or is_reserved_0
                    
                    is_popup_0 = self.active_popups.get(0) is not None

                    cell0_ready = (io_list[16] == 1 and io_list[17] == 1)

                    # ==========================================================
                    # ★ [BACK 추가] 완료 후 제품을 빼면 팝업 자동 종료 및 램프 리셋
                    # (센서 둘 중 하나라도 꺼지면 제품을 뺀 것으로 간주)
                    # ==========================================================
                    if is_popup_0 and (io_list[16] == 0 or io_list[17] == 0):
                        try: self.active_popups[0].close()
                        except: pass
                        self.active_popups[0] = None
                        is_popup_0 = False

                    # ----------------------------------------------------------
                    # ★ [수정] 램프 로직 직관적으로 변경 (사용자 요청 반영)
                    # ----------------------------------------------------------
                    if cell0_busy: c0_st = "OFF"           # 작업 중이거나 예약됨: 꺼짐
                    elif is_popup_0: c0_st = "BLINK"       # 작업 완료: 깜빡임 (제품을 빼주세요)
                    elif cell0_ready: c0_st = "ON"         # 작업물 있음 (정상 안착): 켜짐 (ON)
                    else: c0_st = "BLINK"                  # 작업물 없음 (투입 대기): 깜빡임
                    
                    target_lamp_states = [c0_st, c0_st]

                    req_start_signals[0] = btn_pressed_list[0] and btn_pressed_list[1]

                # ----------------------------------------------------
                # 4-4. 버튼 램프 점등 제어 (깜빡임 포함)
                # ----------------------------------------------------
                if self.auto_mode and not self.is_alarm_state():
                    final_on_pattern = []   
                    final_off_pattern = [] 

                    for i, state in enumerate(target_lamp_states):
                        if state == "OFF":
                            final_on_pattern.append("OFF")
                            final_off_pattern.append("OFF")
                        elif state == "ON": 
                            # ★ [추가] 제품이 안착되어 대기 중일 때는 점멸 없이 계속 켜둠
                            final_on_pattern.append("ON")
                            final_off_pattern.append("ON")
                        else: # "BLINK"
                            if btn_pressed_list[i]: 
                                final_on_pattern.append("ON")
                                final_off_pattern.append("ON")
                            else:
                                final_on_pattern.append("ON")
                                final_off_pattern.append("OFF")

                    target_key = str(final_on_pattern) + str(final_off_pattern)
                    last_key = getattr(self, 'last_blink_key', None)
                    is_blinking = self.blink_buttons.blink_thread is not None and self.blink_buttons.blink_thread.is_alive()

                    if not is_blinking or (last_key != target_key):
                        self.blink_buttons.start_blink(
                            target_func=self.button_lamp_control,
                            on_status=final_on_pattern,
                            off_status=final_off_pattern,
                            on_time=0.5, off_time=0.5
                        )
                        self.last_blink_key = target_key
                else:
                    if self.blink_buttons.blink_thread is not None and self.blink_buttons.blink_thread.is_alive():
                        self.blink_buttons.stop_blink()
                        self.button_lamp_control(["OFF"] * len(target_lamp_states))
                    self.last_blink_key = None

                # ----------------------------------------------------
                # 4-5. 예약된 작업 자동 실행 (Post Init / Queue)
                # ----------------------------------------------------
                if getattr(self, 'cross_trigger_cell', None) is not None:
                    c_cell = self.cross_trigger_cell
                    self.cross_trigger_cell = None
                    print(f"[SYSTEM] 오버랩(크로스) 예약 감지 -> 메인 스레드에서 즉시 Cell {c_cell} 시작")
                    self.start_job(cell_num=c_cell)
                    return
                
                if getattr(self, '_post_init_cell', None) is not None and not getattr(self, 'is_initializing', False):
                    is_robot_busy = (t1 and t1.isRunning()) or (t2 and t2.isRunning())
                    
                    if self.robot_at_home() and not is_robot_busy:
                        cell_to_start = self._post_init_cell
                        self._post_init_cell = None 
                        print(f"[SYSTEM] 예약된 Cell {cell_to_start} 시작")
                        
                        if self.system_mode == "BACK":
                            self.back_mute_delaying = True
                            QtCore.QTimer.singleShot(1500, lambda: self.execute_delayed_back_job(cell_to_start))
                        else:
                            self.start_job(cell_num=cell_to_start)
                    return

                # ----------------------------------------------------
                # 4-6. 신규 양수 버튼 트리거 실행 검사
                # ----------------------------------------------------
                if not self.auto_mode or self.is_alarm_state(): 
                    return

                trigger_cell = None
                if self.system_mode == "HEAD":
                    # [HEAD 모드] 다른 셀이 작업 중이어도 예약을 위해 입력 허용
                    can_trigger_1 = not (t1 and t1.isRunning()) and self.waiting_cell != 1 and getattr(self, '_post_init_cell', None) != 1
                    can_trigger_2 = not (t2 and t2.isRunning()) and self.waiting_cell != 2 and getattr(self, '_post_init_cell', None) != 2
                    
                    if req_start_signals.get(1, False) and can_trigger_1: trigger_cell = 1
                    elif req_start_signals.get(2, False) and can_trigger_2: trigger_cell = 2
                else: 
                    # [BACK 모드] 진행 중인 작업(초기화 포함)이 없을 때만 입력 허용
                    can_trigger_0 = not cell0_busy
                    if req_start_signals.get(0, False) and can_trigger_0: trigger_cell = 0

                if trigger_cell is not None:
                    # 양수버튼이 눌렸을 때 해당 셀의 팝업이 켜져있다면 강제로 닫음
                    if self.active_popups.get(trigger_cell) is not None:
                        try: self.active_popups[trigger_cell].close()
                        except: pass
                        self.active_popups[trigger_cell] = None

                    # ================================================
                    # ★ [최적화 1] HEAD 모드: 양수버튼 누르자마자 '즉시' 클램프! (Pre-clamping)
                    # ================================================
                    if self.system_mode == "HEAD":
                        if trigger_cell == 1:
                            self.io_module.Send_que.put(self.io_module.Write_DO_Data(2, 1)) # C1 클램프 ON
                            print("[OPT] Cell 1 양수버튼 입력 감지 -> 즉시 선행 클램프 작동!")
                        elif trigger_cell == 2:
                            self.io_module.Send_que.put(self.io_module.Write_DO_Data(10, 1)) # C2 클램프 ON
                            print("[OPT] Cell 2 양수버튼 입력 감지 -> 즉시 선행 클램프 작동!")

                    # ================================================
                    # [BACK 모드] 모든 시작은 무조건 1.5초 지연으로 보냄!
                    # ================================================
                    if self.system_mode == "BACK" and trigger_cell == 0:
                        if not getattr(self, 'back_mute_delaying', False):
                            self.is_back_working = True
                            self.back_mute_delaying = True
                            print(f"[SYSTEM] BACK 모드: 손 뺄 시간 1.5초 대기 시작 (Mute 유지)")
                            QtCore.QTimer.singleShot(1500, lambda: self.execute_delayed_back_job(trigger_cell))
                            
                    # ================================================
                    # [HEAD 모드] 기존 방식대로 즉시 분기 (예약 포함)
                    # ================================================
                    elif self.system_mode == "HEAD":
                        if not getattr(self, '_is_first_boot_init_done', False):
                            print(f"[SYSTEM] 최초 양수 버튼(Cell {trigger_cell})! 배출 시퀀스 선행 실행")
                            self._is_first_boot_init_done = True
                            self._post_init_cell = trigger_cell
                            self.start_init()
                            
                        elif getattr(self, 'is_initializing', False):
                            if self.waiting_cell is None:
                                print(f"[QUEUE] 초기화 진행 중. Cell {trigger_cell} 대기열 예약.")
                                self.waiting_cell = trigger_cell
                                
                        else:
                            is_robot_busy = (t1 and t1.isRunning()) or (t2 and t2.isRunning())
                            if is_robot_busy:
                                if self.waiting_cell is None:
                                    print(f"[QUEUE] 로봇 작업 중. Cell {trigger_cell} 대기열 예약.")
                                    self.waiting_cell = trigger_cell
                            else:
                                self.start_job(cell_num=trigger_cell)

        except Exception as e:
            print(f"[IO POLL ERROR] {e}")
            import traceback
            traceback.print_exc()

        finally:
            # ============================================================
            # 5. 폴링 종료 후 락 해제 (필수)
            # ============================================================
            self._is_polling = False
    
    # =========================================================================
    # [제품 선택] 너트러너 파라미터 세트 IO 제어 (Binary 로직)
    # =========================================================================
    def show_product_selection(self):
        """제품/너트러너 세트 선택 팝업창"""
        # 오토 모드 중에는 제품 변경 불가 (안전 장치)
        if not self.check_auto_mode_restriction():
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("작업 제품 선택")
        dialog.setFixedSize(600, 450) # ★ 볼트 비우기 버튼이 들어가므로 높이를 살짝 늘림
        dialog.setStyleSheet("background-color: #2b2b2b; color: white; border: 2px solid #0078D7;")
        
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 30, 20, 30)
        
        lbl_info = QLabel("작업할 제품 번호(너트러너 파라미터 세트)를 선택하세요.")
        lbl_info.setAlignment(Qt.AlignCenter)
        lbl_info.setStyleSheet("font-size: 20px; font-weight: bold; border: none;")
        layout.addWidget(lbl_info)
        layout.addSpacing(20)

        # JSON 파일에서 제품명 로드
        config_name = f"{self.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name
        
        product_names = {}
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    product_names = config_data.get("CURRENT_PRODUCT", {})
            except Exception as e:
                print(f"[ERROR] 팝업 제품명 JSON 읽기 실패: {e}")

        # JSON에 없으면 기본값 세팅
        name_0 = product_names.get("0", "0번 제품")
        name_1 = product_names.get("1", "1번 제품")
        name_2 = product_names.get("2", "2번 제품")
        name_3 = product_names.get("3", "3번 제품")

        # 2x2 그리드로 제품 선택 버튼 배치
        grid_layout = QGridLayout()
        grid_layout.setSpacing(15)
        
        btn_0 = QPushButton(f"{name_0}")
        btn_1 = QPushButton(f"{name_1}")
        btn_2 = QPushButton(f"{name_2}")
        btn_3 = QPushButton(f"{name_3}")
        
        buttons = [btn_0, btn_1, btn_2, btn_3]
        for idx, btn in enumerate(buttons):
            btn.setFixedSize(250, 70)
            btn.setStyleSheet("""
                QPushButton { background-color: #555555; font-size: 16px; font-weight: bold; border-radius: 5px; }
                QPushButton:hover { background-color: #0078D7; }
            """)
            btn.clicked.connect(lambda _, val=idx: dialog.done(val))
            grid_layout.addWidget(btn, idx // 2, idx % 2)

        layout.addLayout(grid_layout)
        
        layout.addSpacing(10) # 간격 추가
        
        # =========================================================
        # ★ [추가] 볼트 비우기 (Purge) 버튼
        # =========================================================
        btn_purge = QPushButton("볼트 비우기 (자동 파기)")
        btn_purge.setFixedHeight(50)
        btn_purge.setStyleSheet("""
            QPushButton { background-color: #d39e00; color: #000000; font-size: 18px; font-weight: bold; border-radius: 5px; }
            QPushButton:hover { background-color: #e0a800; }
        """)
        # 볼트 비우기 기능 실행 (결과값은 100으로 임의 지정하여 제품 변경 로직과 분리)
        btn_purge.clicked.connect(lambda: dialog.done(100))
        layout.addWidget(btn_purge)
        
        # 취소 버튼
        btn_cancel = QPushButton("취 소 (변경 안함)")
        btn_cancel.setFixedHeight(50)
        btn_cancel.setStyleSheet("background-color: #883333; font-size: 18px; font-weight: bold; border-radius: 5px; margin-top:5px;")
        btn_cancel.clicked.connect(lambda: dialog.done(-1)) # 취소는 -1 반환
        layout.addWidget(btn_cancel)
        
        dialog.setLayout(layout)
        
        # 화면 중앙+상단 배치
        screen_geo = QApplication.desktop().screenGeometry()
        dialog.move(int(screen_geo.width() / 2 - dialog.width() / 2), int(screen_geo.height() * 0.25))

        # 팝업 실행 및 결과 반환
        result = dialog.exec_()
        
        # =========================================================
        # ★ 결과 처리 분기
        # =========================================================
        if result == 100: 
            # 볼트 비우기(Purge) 버튼을 눌렀을 때
            reply = self.show_message(
                "볼트 강제 배출", 
                "로봇이 배출 위치로 이동하여<br>호스 안의 볼트를 모두 비웁니다.<br>계속하시겠습니까?", 
                QMessageBox.Warning, 
                QMessageBox.Yes | QMessageBox.No
            )
            
            if reply == QMessageBox.Yes:
                # 스레드가 이미 돌고 있다면 무시
                if hasattr(self, 'purge_thread') and self.purge_thread.isRunning():
                    self.show_message("진행 중", "이미 볼트 비우기가 진행 중입니다.", QMessageBox.Warning)
                    return
                
                # 퍼지 모드 ON: 메인 스레드가 피더기 에러를 무시하도록 지시
                self.is_purging = True
                
                # 볼트 비우기 팝업 (로딩창 역할) 띄우기
                self.purge_progress_dialog = QProgressDialog("볼트를 비우는 중입니다... (에러 발생 시까지 대기)", None, 0, 0, self)
                self.purge_progress_dialog.setWindowTitle("볼트 비우기")
                self.purge_progress_dialog.setWindowModality(Qt.ApplicationModal)
                self.purge_progress_dialog.setCancelButton(None) # 취소 불가
                self.purge_progress_dialog.setStyleSheet("background-color: #2b2b2b; color: white; border: 2px solid #0078D7;")
                self.purge_progress_dialog.show()

                # 스레드 생성 및 연결
                self.purge_thread = BoltPurgeThread(self)
                self.purge_thread.finished_signal.connect(self._on_purge_finished)
                self.purge_thread.start()
                
        elif result != -1: 
            # 일반 제품(0~3)을 선택했을 때 (기존 로직)
            self.apply_product_type(result)
            applied_name = product_names.get(str(result), f"{result}번 제품")
            self.show_message("설정 완료", f"작업 제품이 <b>[{applied_name}]</b>(으)로 변경되었습니다.", QMessageBox.Information)

    @pyqtSlot(bool, str)
    def _on_purge_finished(self, success, message):
        """볼트 비우기 스레드 종료 시 호출되는 함수"""
        if hasattr(self, 'purge_progress_dialog') and self.purge_progress_dialog:
            self.purge_progress_dialog.close()
            
        # ========================================================
        # ★ [추가] 퍼지 모드 OFF: 정상적인 피더기 에러 감시 재개
        # ========================================================
        self.is_purging = False
            
        if success:
            self.show_message("배출 완료", message, QMessageBox.Information)
        else:
            self.show_message("배출 실패", f"볼트 비우기 중 에러가 발생했습니다:<br>{message}", QMessageBox.Critical)


    def apply_product_type(self, product_type):
        """선택된 제품 번호(0~3)에 따라 MDO 37, 38 출력을 제어하고 라벨을 업데이트합니다."""
        self.current_product_type = product_type
        self.db_manager.save_setting("product_type", self.current_product_type)
        
        # 바이너리 로직 판별 (1번, 3번일 때 A 온 / 2번, 3번일 때 B 온)
        val_a = 1 if product_type in [1, 3] else 0
        val_b = 1 if product_type in [2, 3] else 0
        
        # -----------------------------------------------------
        # 1. 물리적 IO 명령 전송 (연결되어 있을 때만 큐에 삽입)
        # -----------------------------------------------------
        if self.io_module is not None and getattr(self.io_module, 'Send_que', None) is not None:
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(37, val_a))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(38, val_b))
            
        # -----------------------------------------------------
        # 2. UI 상태 즉각 동기화 (MDO 버튼 파란색 점등)
        # -----------------------------------------------------
        if len(self.mdo_btns) > 38:
            if self.mdo_btns[37]:
                self.mdo_btns[37].blockSignals(True)
                self.mdo_btns[37].setChecked(bool(val_a))
                self.mdo_btns[37].blockSignals(False)
            if self.mdo_btns[38]:
                self.mdo_btns[38].blockSignals(True)
                self.mdo_btns[38].setChecked(bool(val_b))
                self.mdo_btns[38].blockSignals(False)
                    
        # -----------------------------------------------------
        # 3. JSON 파일에서 제품명 읽어와서 메인 라벨 텍스트 변경
        # -----------------------------------------------------
        config_name = f"{self.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name
        
        product_names = {}
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    product_names = config_data.get("CURRENT_PRODUCT", {})
            except Exception as e:
                print(f"[ERROR] 라벨 제품명 JSON 읽기 실패: {e}")
        
        name = product_names.get(str(product_type), f"{product_type}번 제품")
        
        if hasattr(self.ui, 'current_product'):
            self.ui.current_product.setText(name)
            
        print(f"[SYSTEM] 제품 적용 완료: {name} (MDO 37:{val_a}, MDO 38:{val_b})")
        
    # ====================================================================
    # [Nutrunner] RS232 통신 및 에러 정의
    # ====================================================================    
    # [PDF Page 25] NG Code 리스트 [cite: 408]
    NR_NG_CODES = {
        "01": "TIME OUT (시간 초과)",
        "02": "1차 체결 토크 미달",
        "06": "2차 체결 토크 미달",
        "07": "2차 체결 각도 하한 에러",
        "08": "2차 체결 각도 상한 에러",
        "10": "3차 체결 토크 미달",
        "11": "3차 체결 각도 하한 에러",
        "12": "3차 체결 각도 상한 에러",
        "13": "3차 체결 토크 하한 에러 (Check Min)",
        "14": "3차 체결 토크 상한 에러 (Check Max)",
        "15": "3차 체결 시간 하한 에러",
        "16": "3차 체결 시간 상한 에러",
        "17": "전체 체결 각도 하한 에러",
        "18": "전체 체결 각도 상한 에러",
        "19": "전체 체결 시간 초과"
    }

    def make_nr_packet(self, cmd_int, data_str=""):
        """
        [프로토콜 생성 로직] [cite: 151, 152]
        STX(02) + CMD + DATA + CS + PARITY + ETX(03)
        모든 바이트는 2글자 ASCII Hex 문자열로 변환됨.
        """
        # 1. Command 및 Data 합치기
        # 예: cmd=0x15 -> "15"
        payload = f"{cmd_int:02X}{data_str}"
        
        # 2. CheckSum & Parity 계산
        # payload 문자열을 2글자씩 쪼개서 16진수 숫자로 변환 후 연산
        chk_sum = 0
        parity = 0
        
        # 바이트 단위로 루프
        # payload가 "1501" 이면 -> 0x15, 0x01 각각 연산
        for i in range(0, len(payload), 2):
            byte_val = int(payload[i:i+2], 16)
            chk_sum += byte_val
            parity ^= byte_val
            
        chk_sum &= 0xFF # Overflow 무시 [cite: 148]
        
        # 3. 최종 패킷 조립 (STX, ETX는 바이트로, 나머지는 문자열로)
        # 패킷 형태: b'\x02' + b'15' + b'CS' + b'PR' + b'\x03'
        packet_str = f"{payload}{chk_sum:02X}{parity:02X}"
        final_packet = b'\x02' + packet_str.encode('ascii') + b'\x03'
        
        return final_packet

    def query_nr_error_code(self, query_type):
        """
        RS232로 너트러너에게 상세 에러 코드를 요청합니다.
        (포트를 매번 열고 닫지 않고 유지하여 버퍼 초기화 현상을 방지합니다)
        """
        log_head = "너트러너 에러"
        
        try:
            # 1. 명령 패킷 생성 
            if query_type == "NG":
                req = self.make_nr_packet(0x15)
                log_head = "체결불량(NG)"
            elif query_type == "ERR":
                req = self.make_nr_packet(0x04)
                log_head = "시스템에러(ERR)"
            else:
                return "Unknown Type"

            # =========================================================
            # ★ [핵심 수정] 포트를 매번 열고 닫지 않고, 열려있는지 확인 후 계속 재사용!
            # =========================================================
            if not hasattr(self, 'nr_serial') or self.nr_serial is None or not self.nr_serial.is_open:
                try:
                    self.nr_serial = serial.Serial(
                        port=self.nr_ser_port, 
                        baudrate=self.nr_baudrate, 
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=1.5 # 타임아웃 넉넉히 부여
                    )
                    print(f"[NR-COM] 시리얼 포트({self.nr_ser_port}) 최초 개방 및 연결 유지")
                except Exception as comm_e:
                    print(f"[NR-COM ERROR] {self.nr_ser_port} 포트 열기 실패: {comm_e}")
                    return f"{log_head} (COM포트 미연결)"

            # 3. 버퍼에 쌓인 찌꺼기 비우기
            self.nr_serial.reset_input_buffer()
            self.nr_serial.reset_output_buffer()

            # 디버깅 출력: 무엇을 보냈는지 확인
            print(f"[NR-COM TX] 전송: {req.hex()}")
            
            # 4. 전송 및 물리적 전송 완료 대기
            self.nr_serial.write(req)
            self.nr_serial.flush() 
            
            # 장비가 응답을 준비할 시간을 약간 줍니다.
            time.sleep(0.1)

            # 5. 수신 (STX ~ ETX)
            raw_res = self.nr_serial.read_until(b'\x03') 
            
            # 🚨 주의: 여기서 self.nr_serial.close()를 절대 하지 않습니다! 포트 살려둠!

            print(f"[NR-COM RX] 수신: {raw_res.hex() if raw_res else '응답없음'}")

            if not raw_res:
                return f"{log_head} (응답없음)"

            # =========================================================
            # [수정 1] STX(0x02) 위치를 찾아 버퍼에 섞인 쓰레기 데이터 걸러내기
            # =========================================================
            stx_idx = raw_res.find(b'\x02')
            if stx_idx == -1:
                return f"{log_head} (패킷오류: STX 없음)"
            
            # STX부터 잘라내어 깨끗한 패킷만 확보
            clean_res = raw_res[stx_idx:]
            
            if len(clean_res) < 6: 
                return f"{log_head} (패킷오류: 너무 짧음)"

            # 명령어 에코(04, 15 등) 다음의 데이터(2바이트) 추출
            code_hex_str = clean_res[3:5].decode('ascii', errors='ignore')
            
            # =========================================================
            # [수정 2] 16진수(HEX)를 10진수(DEC) 정수로 변환하여 매핑
            # =========================================================
            try:
                # 컨트롤러가 '0A'를 보내면 10진수 10으로 자동 변환됨
                error_code_int = int(code_hex_str, 16)
                # 기존 딕셔너리 키 형태("01", "10" 등)에 맞게 2자리 문자열로 포맷팅
                error_code_key = f"{error_code_int:02d}"
            except ValueError:
                error_code_key = code_hex_str

            # 7. 코드 매핑 확인
            if query_type == "NG":
                msg = self.NR_NG_CODES.get(error_code_key, f"알수없는 코드({code_hex_str} -> {error_code_key})")
            else:
                if error_code_int == 0: msg = "에러 없음"
                else: msg = f"드라이버 에러({error_code_key})"
            
            return f"{log_head} - {msg}"

        except Exception as e:
            print(f"[NR-COM ERROR] 예외 발생: {e}")
            return f"{log_head} 확인 불가"
            
    def show_message(self, title, message, icon=QMessageBox.Information,
                     buttons=QMessageBox.Ok,    
                     text_color="#FFFFFF",      
                     font_size=22,              
                     button_color="#0078D7",    
                     button_text_color="#FFFFFF"):
        """
        QMessageBox 대신 QDialog를 사용하여 커스텀 디자인과 위치(상단 1/4)를 적용한 메시지창
        """
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        
        # 1. [크기 통일] 500x280 (내용이 길면 높이는 늘어날 수 있게 최소 크기만 지정)
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(280)
        
        # 2. 레이아웃 설정
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 30, 20, 30)
        
        # 3. 메시지 라벨 (HTML 지원)
        lbl_msg = QLabel(message)
        lbl_msg.setAlignment(Qt.AlignCenter)
        lbl_msg.setWordWrap(True)
        lbl_msg.setStyleSheet(f"color: {text_color}; font-size: {font_size}px; font-weight: bold;")
        
        layout.addWidget(lbl_msg)
        layout.addSpacing(30)

        # 4. 버튼 생성 (비트마스크 확인)
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(15)
        
        # 버튼 매핑 (QMessageBox 플래그 -> 한글 텍스트)
        button_map = [
            (QMessageBox.Ok, "확인"),
            (QMessageBox.Yes, "예"),
            (QMessageBox.No, "아니오"),
            (QMessageBox.Cancel, "취소"),
            (QMessageBox.Close, "닫기")
        ]
        
        # 생성된 버튼을 담을 리스트 (나중에 포커스 처리를 위해)
        created_btns = []

        for flag, text in button_map:
            if buttons & flag:
                btn = QPushButton(text)
                btn.setFixedSize(130, 60) # 버튼 크기 통일
                
                # 버튼 클릭 시 해당 플래그 값을 반환하며 닫기
                # lambda에 flag=flag를 넣어주어야 루프 마지막 값이 아닌 해당 값을 캡처함
                btn.clicked.connect(lambda _, result=flag: dialog.done(result))
                
                btn_layout.addWidget(btn)
                created_btns.append(btn)

        layout.addLayout(btn_layout)
        dialog.setLayout(layout)

        # 5. [스타일 통일] 다크 테마 (#2b2b2b 배경, 파란 테두리)
        dialog.setStyleSheet(f"""
            QDialog {{
                background-color: #2b2b2b;
                border: 2px solid {button_color};
            }}
            QLabel {{
                background-color: transparent;
            }}
            QPushButton {{
                background-color: #555555;
                color: {button_text_color};
                font-size: 18px;
                font-weight: bold;
                border-radius: 5px;
                border: 1px solid #888;
            }}
            QPushButton:hover {{
                background-color: {button_color};
                border: 1px solid #005a9e;
            }}
            QPushButton:pressed {{
                background-color: #333333;
            }}
        """)

        # 6. [위치 통일] 화면 상단 1/4 지점으로 이동
        screen_geo = QApplication.desktop().screenGeometry()
        scr_w, scr_h = screen_geo.width(), screen_geo.height()
        dlg_w = dialog.sizeHint().width()
        dlg_h = dialog.sizeHint().height()
        
        new_x = int(scr_w / 2 - dlg_w / 2)
        new_y = int(scr_h * 0.25) 
        
        dialog.move(new_x, new_y)

        # 7. 실행 및 결과 반환 (클릭된 버튼의 플래그 반환)
        return dialog.exec_()
    
    # =========================================================================
    # [Helper] 오토 모드 접근 제한 검사
    # =========================================================================
    def check_auto_mode_restriction(self):
        """
        현재 오토 모드인지 확인하고, 오토 모드라면 경고 팝업을 띄웁니다.
        Return: True(접근 허용/수동모드), False(접근 차단/오토모드)
        """
        if self.auto_mode:
            self.show_message(
                "조작 불가", 
                "<b>자동(Auto) 모드</b> 실행 중에는<br>설정을 변경할 수 없습니다.<br><br>수동(Manual) 모드로 전환해 주세요.", 
                QMessageBox.Warning
            )
            return False
        return True
 
    @pyqtSlot(str, str)
    def add_alarm_log(self, message, level="INFO"):
        time_str = QDateTime.currentDateTime().toString("HH:mm:ss")
        full_text = f"[{time_str}] {message}"
        
        item = QListWidgetItem(full_text)
        
        if level == "ERROR":
            item.setForeground(Qt.red) 
            item.setBackground(QtGui.QColor("#FFEEEE"))
            
            # =========================================================
            # ★ [핵심 수정 1] 로봇이 꺼져있을 때 여기서 통신 에러가 나서 
            # 아래쪽의 팝업 띄우는 로직이 실행 안 되는 문제 해결
            # =========================================================
            try:
                if not getattr(self, 'robot_disconnected', True):
                    self.set_variable_with_ui("system_NG", 1)
            except Exception as e:
                print(f"[WARN] 로봇 연결 끊김으로 system_NG 변수 전송 생략: {e}")
            
            # ★ 소프트웨어 알람 플래그 ON (무조건 실행됨)
            self._is_software_error = True

            # 알람 발생 시 부저 음소거 상태 강제 해제
            if getattr(self, 'is_buzzer_muted', False):
                print("[ALARM] 알람 발생! 부저 음소거를 강제 해제합니다.")
                self.is_buzzer_muted = False
                self.ui.buzzer_off_button.blockSignals(True)
                self.ui.buzzer_off_button.setChecked(False)
                self.ui.buzzer_off_button.blockSignals(False)

            if self.io_module is not None:
                print(f"[ALARM STOP] 시스템 에러로 인한 너트러너 정지 요청 (MDO 34 ON)")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(34, 1))
            
            if getattr(self, 'auto_mode', False):
                print("[INFO] 알람 발생으로 인한 수동(Manual) 모드 강제 전환")
                self.ui.manual_btn.setChecked(True)
            
        elif level == "WARN":
            item.setForeground(Qt.darkYellow)
            item.setBackground(QtGui.QColor("#FFFFE0"))
            self.is_message_active = True
            
        else:
            item.setForeground(Qt.black) 
            
        self.ui.alarmListWidget.addItem(item)
        self.ui.alarmListWidget.scrollToBottom()

        if not self.alarm_popup_shown:
            self.alarm_popup_shown = True
            
            self.robot_status_update()
            QApplication.processEvents() # UI 즉시 갱신
            
            if level == "ERROR":
                self.show_alarm_popup(is_error=True, msg=message)
            elif level == "WARN":
                self.show_alarm_popup(is_error=False, msg=message)
        
    # [추가] ScrollArea에 표시된 전체 로그를 파일로 저장하는 함수
    def save_all_logs_to_txt(self):
        log_content = self.ui.scrollArea.toPlainText() # ScrollArea의 전체 텍스트 가져오기
        if not log_content.strip():
            return 

        # 파일 저장 경로 설정
        log_dir = APP_ROOT / "Logs" / "SystemLogs" # 시스템 로그 폴더 별도 분리
        log_dir.mkdir(parents=True, exist_ok=True)
        
        date_str = QDateTime.currentDateTime().toString("yyyyMMdd")
        time_str = QDateTime.currentDateTime().toString("HHmmss")
        file_path = log_dir / f"Full_Log_{date_str}_{time_str}.txt"
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"====================================\n")
                f.write(f" 시스템 전체 로그 백업 시점: {QDateTime.currentDateTime().toString('yyyy-MM-dd HH:mm:ss')}\n")
                f.write(f"====================================\n\n")
                f.write(log_content)
            print(f"[LOG] 전체 로그가 백업되었습니다. ({file_path})")
        except Exception as e:
            print(f"[ERROR] 전체 로그 백업 실패: {e}")
        
    # [추가] 알람 텍스트를 파일로 저장하는 함수
    def save_alarms_to_txt(self):
        count = self.ui.alarmListWidget.count()
        if count == 0:
            return # 지울 알람이 없으면 아무것도 안 함
        
        # 파일 저장 경로 설정 (프로젝트 폴더 내에 Logs 폴더 생성)
        log_dir = APP_ROOT / "Logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        date_str = QDateTime.currentDateTime().toString("yyyyMMdd")
        file_path = log_dir / f"Alarm_Log_{date_str}.txt"
        reset_time = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        
        try:
            # 텍스트 파일 열고 덧붙이기(a 모드)
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(f"\n====================================\n")
                f.write(f" 리셋 발생 시간: {reset_time}\n")
                f.write(f"====================================\n")
                # 리스트에 있는 모든 알람 내용을 파일에 쓰기
                for i in range(count):
                    item_text = self.ui.alarmListWidget.item(i).text()
                    f.write(f"{item_text}\n")
            print(f"[LOG] 알람 내역이 텍스트 파일로 백업되었습니다. ({file_path})")
        except Exception as e:
            print(f"[ERROR] 알람 백업 저장 실패: {e}")

    def robot_alarm_reset_button(self):
        curr_time = time.time()
        if curr_time - getattr(self, '_last_reset_click', 0) < 2.0:
            print("[WARN] 알람 리셋 연속 클릭 방지")
            return
        self._last_reset_click = curr_time

        print("[RESET] 알람 리셋 시퀀스 시작 (안전 릴레이 1초 펄스 적용)")
        self.is_resetting = True
        try:
            # =========================================================
            # ★ [수정] 0순위: 다른 로직보다 먼저 타워램프 깜빡임을 멈추고 부저 물리적 OFF
            # (상태 초기화 전 깜빡임 스레드가 찰나에 부저를 켜는 것을 방지)
            # =========================================================
            self.blink_tower.stop_blink()
            if self.io_module is not None:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 0)) # 부저 즉시 확실히 끄기
            
            # 1. 즉시 처리해야 할 UI 팝업 및 알람 내역 정리
            if getattr(self, 'active_alarm_dialog', None) is not None:
                self.active_alarm_dialog.close()
                self.active_alarm_dialog = None
                
            self.save_alarms_to_txt()
            self.save_all_logs_to_txt()
            self.ui.alarmListWidget.clear()
            self.alarm_manager.active_alarms.clear()
            self.ui.buzzer_off_button.setChecked(False)
            
            # 플래그 즉시 차단 (폴링 스레드가 다시 알람을 띄우지 않도록)
            self._is_software_error = False 
            self.alarm_popup_shown = False 
            self.is_message_active = False
            self.set_variable_with_ui("system_NG", 0)

            # ★ [PATCH] terminate() 제거 → 협조적 종료(requestInterruption)
            #   terminate는 finally를 건너뛰고 락/소켓을 잡은 채 스레드를 죽여
            #   뮤트 고착, modbus_lock 데드락, 29999 응답 밀림을 유발할 수 있습니다.
            self._request_job_threads_stop()
            
            self.active_popups = {1: None, 2: None, 0: None}
            self.robot_occupant = 0
            self.is_back_working = False
            self.waiting_cell = None

            # 2. 물리적 출력(부저 등) 즉시 정지 및 리셋 릴레이 구동 (ON)
            if self.io_module is not None:
                print("[CMD] 하위 장치 리셋 펄스 ON (MDO 23, 33, 44)")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(34, 0)) # NR 강제정지 신호 OFF
                
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 1)) # 라이트 커튼 리셋
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(33, 1)) # NR 리셋
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 1)) # SFD 리셋
                
                # 하위 장치들이 스스로를 리셋하고 안전 신호를 출력할 시간 0.1초 확보
                QtCore.QTimer.singleShot(100, lambda: self.io_module.Send_que.put(self.io_module.Write_DO_Data(21, 1)))

            # 프리징 방지를 위해 스레드로 로봇 통신 위임
            def step2_hardware_pulse_off():
                """[중간] 하드웨어 릴레이 펄스 원복 (1초 ON 유지 후 OFF)"""
                if self.io_module is not None:
                    print("[CMD] 하드웨어 릴레이 리셋 펄스 OFF (1초 유지 완료)")
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(21, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(33, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 0))
                
                # UI 스레드 멈춤(Freezing)을 막기 위해 백그라운드 스레드에서 로봇 명령 전송
                self.reset_thread = ResetRobotThread(self)
                self.reset_thread.request_update_signal.connect(self.robot_status_update)
                self.reset_thread.finished.connect(self._on_reset_sequence_finished)
                self.reset_thread.start()

            # 세이프티 릴레이의 "1초 이상 ON" 조건을 만족시키기 위해 1000ms 대기 후 OFF 실행
            QtCore.QTimer.singleShot(1000, step2_hardware_pulse_off)

        except Exception as e:
            print(f"[ERROR] 알람 리셋 실패: {e}")
    
    def on_buzzer_mute_toggled(self, checked):
        self.is_buzzer_muted = checked 
        if checked:
            print("[UI] 부저 음소거 활성화 (Mute ON)")
            if self.io_module is not None:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 0))
        else:
            print("[UI] 부저 음소거 해제 (Mute OFF)")
            # ★ 음소거를 풀었는데 현재 알람 상태라면 즉시 울리도록 추가
            if self.is_alarm_state() and self.io_module is not None:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 1))
                
    @pyqtSlot()
    def show_alarm_popup(self, is_error=True, msg=""):
        # 이미 팝업이 띄워져 있다면 중복 실행 방지
        if self.active_alarm_dialog is not None:
            return

        # 새 팝업 인스턴스 생성 및 Non-blocking으로 표시
        self.active_alarm_dialog = AlarmDialog(self, is_error, msg)
        self.active_alarm_dialog.closed_signal.connect(self.on_alarm_popup_closed)
        self.active_alarm_dialog.show()
        
    def on_alarm_popup_closed(self):
        """알람 팝업이 닫힐 때 객체 초기화"""
        self.active_alarm_dialog = None
        # 주의: 창을 닫았다고 해서 에러가 해결된 것은 아니므로 
        # self.alarm_popup_shown = False 처리는 여기서 하지 않습니다.
        
    @pyqtSlot()
    def on_robot_disconnected(self):
        self.robot_disconnected = True
        try:
            self.ui.scrollArea.append("[WARN] 로봇 연결 끊김")
        except:
            pass  
        self.robot_status_update()
        
    def button_enable(self):
        buttons = [
            self.ui.auto_btn,
            self.ui.home_button,
            self.ui.power_off_button,
            self.ui.pause_button,
            self.ui.stop_button
        ]
        enable = self.robot_mode in (4, 7)

        for btn in buttons:
            btn.setEnabled(enable)
            
    def button_disable(self):
        buttons = [
            self.ui.auto_btn,
            self.ui.home_button,
            self.ui.power_off_button,
            self.ui.pause_button,
            self.ui.stop_button
        ]
        for btn in buttons:
            btn.setDisabled(not bool(self.is_robot_power_on))
        if self.auto_mode:
            print("[INFO] 안전을 위해 수동(Manual) 모드로 자동 전환됩니다.")
            self.on_manual_mode(True)
        
    def on_manual_mode(self, checked):
        if checked:
            self.auto_mode = False
            self.ui.auto_btn.setChecked(False)
            self.ui.manual_btn.setChecked(True)
            
    def is_alarm_state(self) -> bool:
        if getattr(self, 'is_resetting', False):
            return False
            
        current_io_id = id(self.io_module) if getattr(self, 'io_module', None) is not None else None
        
        if getattr(self, '_last_io_id', None) != current_io_id:
            self._last_io_id = current_io_id
            self._io_connect_time = time.time()
            
        if current_io_id is not None and (time.time() - self._io_connect_time) < 2.0:
            return False

        try:
            # 1. 소프트웨어 래치 (Edge 감지용)
            if getattr(self, '_is_software_error', False):
                return True
                
            # 2. 로봇 자체 세이프티 모드 체크
            s_mode = int(float(self.safety_mode)) if self.safety_mode is not None else 0
            SAFETY_MODE_STOP = [3, 4, 5, 6, 7, 8, 9, 11, 12, 13]
            if s_mode in SAFETY_MODE_STOP:
                return True
                
            # 3. 주변 장치 IO "현재 상태(Level)" 직접 체크
            if getattr(self, 'io_module', None) is not None:
                inp = self.io_module.Read_Input_Data()
                
                if not inp or len(inp) <= 44:
                    return True # 통신 단절은 알람 처리
                    
                if inp and len(inp) > 44:
                    # ★ 진짜 비상정지 2가지만 다이렉트 알람으로 인정 (라이트 커튼은 위에서 엣지로 처리)
                    # 21: 릴레이 끊김, 22: 비상정지 버튼
                    if inp[21] == 0 or inp[22] == 1:
                        return True
                        
        except Exception as e:
            print(f"[ALARM CHECK ERROR] {e}")
            
        return False
    
    @pyqtSlot()
    def _on_reset_sequence_finished(self):
        print("[RESET] 로봇 통신 및 릴레이 펄스 출력 완료. 하드웨어 안정화 대기...")
        
        # (선택) 스레드 객체 메모리 정리
        if hasattr(self, 'reset_thread'):
            self.reset_thread.deleteLater()

        # =================================================================
        # 🕒 [시간 설정 1] 리셋 펄스(1초) 종료 후 하드웨어 장비들이 안정화될 시간
        # 기본값: 400 (0.4초) / 너무 짧아서 부저가 튀면 이 숫자를 늘리세요.
        # =================================================================
        HARDWARE_STABILIZE_MS = 400
        
        QtCore.QTimer.singleShot(HARDWARE_STABILIZE_MS, self._finalize_reset)

    def _finalize_reset(self):
        """안정화 대기 후 안전하게 하드웨어 감시를 재개하는 함수"""
        self.is_resetting = False
        
        # =================================================================
        # 🕒 [시간 설정 2] 안정화가 끝난 후, 라이트 커튼 '뮤트'를 켜기까지 기다리는 시간
        # 기본값: 500 (0.5초) / 1000 = 1초 / 시퀀스 에러가 나면 이 숫자를 늘리세요.
        # =================================================================
        MUTE_DELAY_MS = 1000 
        
        # 뮤트 켜짐 방지 깃발(Flag) 세우기
        self.mute_block_after_reset = True
        print(f"[RESET] 알람 감시 재개. (뮤트 {MUTE_DELAY_MS}ms 지연 시작)")
        
        # 설정한 시간 뒤에 뮤트 블락 해제 함수 호출
        QtCore.QTimer.singleShot(MUTE_DELAY_MS, self._release_mute_block)
        
        self.robot_status_update()

    def _release_mute_block(self):
        """지연 타이머가 끝나면 호출되어 뮤트를 허용함"""
        print("[RESET] 뮤트 지연 완료. 라이트 커튼 뮤트(대기 상태) 허용.")
        self.mute_block_after_reset = False
    
    def is_idle_state(self):
        """
        로봇 전원은 켜져 있으나, 아직 브레이크가 풀리지 않은 IDLE(5) 상태인지 확인합니다.
        (세이프티 에러 검사는 is_alarm_state에서 선행되므로 여기선 생략합니다.)
        """
        try:
            # robot_mode가 문자열이거나 None일 경우를 대비해 안전하게 형변환
            r_mode = int(float(self.robot_mode)) if self.robot_mode is not None else 0
            
            # 전원이 켜져 있고, 로봇 모드가 5(Idle)일 때만 대기 상태로 판단
            return self.is_robot_power_on and r_mode == 5
            
        except Exception as e:
            print(f"[WARN] is_idle_state 검사 오류: {e}")
            return False
        
    def _put_do_if_changed(self, idx, val):
        """★ [PATCH] 현재 출력 상태와 같으면 큐에 넣지 않음.
        robot_status_update(100ms 주기)가 램프/부저를 매번 써서 초당 수십 건이 Send_que에 쌓이면,
        같은 큐를 쓰는 너트러너 START / 피더기 펄스의 타이밍이 밀리거나 펄스 폭이 줄어듭니다."""
        if self.io_module is None:
            return
        try:
            outputs = self.io_module.Read_Output_Data()
            if outputs and len(outputs) > idx and int(outputs[idx]) == int(val):
                return
        except Exception:
            pass
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(idx, val))

    def lamp_control(self, status):
        """
        status 리스트에 따라 타워램프(MDO 28~31)를 제어합니다.
        Write_DO_Data가 '단일 비트 제어' 함수이므로, 개별적으로 큐에 넣어야 합니다.
        """
        if self.io_module is None:
            return

        # 굳이 전체 DO를 읽어올 필요 없이, 제어하려는 핀만 확실하게 쏘면 됩니다.
        target_indices = [28, 29, 30] # 적, 황, 녹
        
        for i, idx in enumerate(target_indices):
            # 1. 켜야 할지 꺼야 할지 결정 (1 or 0)
            bit_val = 0
            if i < len(status) and status[i] == "ON":
                bit_val = 1
            
            # 2~3. ★ [PATCH] 상태가 바뀔 때만 명령 큐에 삽입
            self._put_do_if_changed(idx, bit_val)
    
    def buzzer_control(self, status):
        if self.io_module is None:
            return

        # =======================================================================
        # ★ [수정] 부저 ON 차단 조건 추가 (음소거 상태 또는 리셋 시퀀스 진행 중)
        # =======================================================================
        if status[0] == "ON":
            # 1. 사용자가 음소거를 누른 상태면 차단
            if getattr(self, 'is_buzzer_muted', False):
                return
            
            # 2. 알람 리셋 버튼을 눌러 리셋 시퀀스가 돌아가는 찰나에는 부저 울림(삑 소리) 방지
            if getattr(self, 'is_resetting', False):
                # 단, 정말 확실하게 부저를 끄는 명령(OFF)을 한 번 더 보냅니다.
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 0))
                return

        target_indices = [31] # 부저 번호
        
        for i, idx in enumerate(target_indices):
            bit_val = 0
            if i < len(status) and status[i] == "ON":
                bit_val = 1
            
            self._put_do_if_changed(idx, bit_val)   # ★ [PATCH] 상태가 바뀔 때만
            
    def set_alarm_output(self, status):
        """깜빡일 때 램프와 부저를 동시에 제어하는 함수"""
        # 램프는 음소거와 상관없이 무조건 깜빡임
        self.lamp_control(status)
        
        # 부저 로직: 적색(0) 또는 황색(1)이 켜질 타이밍이면 부저 ON 명령 전송
        # (단, 위 buzzer_control 내부에서 음소거 상태면 알아서 차단됨)
        if status[0] == "ON" or status[1] == "ON": 
             self.buzzer_control(["ON"])
        else:
             self.buzzer_control(["OFF"])
    
    @pyqtSlot()
    def robot_status_update(self):
        state_values = self.values
        try:
            if state_values is not None:

                if not self.is_alarm_state():
                    self.alarm_popup_shown = False

                if not self._speed_initialized and self.robot_speed is not None:
                    self._speed_initialized = True
                    self.ui.speed_slider.blockSignals(True)
                    self.ui.speed_slider.setValue(int(self.robot_speed))
                    self.ui.speed_slider.blockSignals(False)
                    self.ui.speed_label.setText(f"{int(self.robot_speed)}%")
                    print(f"[SYNC] Speed synced: {self.robot_speed}%")

                # ============================================================
                # 🚨 [UI 상태 표시 최우선 순위 정렬] 🚨
                # ============================================================
                
                # 1순위. 알람 발생 (로봇 전원/연결 유무와 상관없이 하드웨어 알람이 최우선!)
                if self.is_alarm_state():
                    color = "red"
                    text = "알람(Error) 발생"
                    self.button_disable()
                    self.blink_tower.start_blink(self.set_alarm_output, ["ON", "OFF", "OFF"], ["OFF", "OFF", "OFF"], on_time=0.5, off_time=0.5)

                # 2순위. 연결 끊김 (알람은 없는데 통신만 끊긴 경우)
                elif self.robot_disconnected:
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) # 깜빡임 중단 후 부저 확실히 끄기
                    color = "red"
                    text = "로봇 연결 끊김"
                    
                    self.button_disable()
                    self.ui.robot_connect_button.setEnabled(True)
                    self.ui.robot_connect_button.setText("로봇 연결 하기")
                    self.lamp_control(["ON","OFF","OFF"])

                # 3순위. 전원 꺼짐 (알람은 아니지만 조작 불가능한 상태)
                elif not self.is_robot_power_on:
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) 
                    color = "gray"
                    text = "전원 꺼짐"
                    self.button_disable()
                    self.ui.power_on_button.setEnabled(True)
                    self.lamp_control(["ON","OFF","OFF"])

                # 4. 메시지 확인 필요 (Warning 수준, 동작은 가능할 수 있음)
                elif self.is_message_active:
                    color = "yellow" 
                    text = "메시지(Warning) 확인 필요"
                    self.blink_tower.start_blink(self.set_alarm_output, ["OFF", "ON", "OFF"], ["OFF", "OFF", "OFF"], on_time=0.5, off_time=0.5)

                # 5. 일시 정지 상태 (Pause)
                elif self.is_task_paused:
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) # ★ 깜빡임 중단 후 부저 확실히 끄기
                    color = "green"
                    text = "작업 일시 정지"
                    self.lamp_control(["OFF","ON","OFF"])
                    
                    # 자동/수동 버튼은 활성화 유지 (원할 때 모드 바꿀 수 있게)
                    self.ui.auto_btn.setEnabled(True)
                    self.ui.manual_btn.setEnabled(True)
                    
                    self.ui.home_button.setEnabled(False)
                    self.ui.power_off_button.setEnabled(False)
                    self.ui.pause_button.setEnabled(True)
                    self.ui.stop_button.setEnabled(True)
                    self.ui.power_on_button.setEnabled(False)

                # 6. 자동 모드 실행 중
                elif self.auto_mode:
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) # ★ 깜빡임 중단 후 부저 확실히 끄기
                    color = "blue"
                    text = "자동 모드"
                    self.lamp_control(["OFF","OFF","ON"])
                    
                    self.ui.auto_btn.setEnabled(True)
                    self.ui.manual_btn.setEnabled(True)
                    
                    self.ui.home_button.setEnabled(False)
                    self.ui.power_off_button.setEnabled(False)
                    self.ui.pause_button.setEnabled(True)
                    self.ui.stop_button.setEnabled(True)
                    self.ui.power_on_button.setEnabled(False)

                # 7. 로봇 대기 상태 (Idle = 5)
                elif self.is_idle_state():
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) # ★ 깜빡임 중단 후 부저 확실히 끄기
                    color = "yellow"
                    text = "대기 상태"
                    self.ui.auto_btn.setEnabled(False)
                    self.ui.home_button.setEnabled(False)
                    self.ui.power_off_button.setEnabled(True)
                    self.ui.pause_button.setEnabled(False)
                    self.ui.stop_button.setEnabled(False)
                    self.ui.power_on_button.setEnabled(True)
                    self.lamp_control(["OFF","ON","OFF"])

                # 8. 수동 모드 조작 중 (나머지 모든 정상 상태)
                else: 
                    self.blink_tower.stop_blink()
                    self.buzzer_control(["OFF"]) # ★ 깜빡임 중단 후 부저 확실히 끄기
                    color = "green"
                    text = "수동 모드"
                    self.lamp_control(["OFF","ON","OFF"])
                    
                    self.ui.auto_btn.setEnabled(True)
                    self.ui.manual_btn.setEnabled(True)
                    self.ui.home_button.setEnabled(True)
                    self.ui.power_off_button.setEnabled(True)
                    self.ui.pause_button.setEnabled(True)
                    self.ui.stop_button.setEnabled(True)
                    self.ui.power_on_button.setEnabled(False)

                # 최종 UI 적용
                self.ui.status_circle.setStyleSheet(
                    f"border-radius: 30px; background-color: {color};"
                )
                self.ui.status_label.setText(text)
                
                try:
                    if getattr(self, 'robot_disconnected', True):
                        # [연결 끊김] 통신 불가 시 회색(비활성화) 처리
                        self.ui.robot_home_circle.setStyleSheet("border-radius: 30px; background-color: #888888;") 
                        self.ui.robot_home_label.setText("홈 상태 알 수 없음")
                    else:
                        # [연결 정상] is_home 변수값 가져오기
                        is_home_val = self.get_variable_with_ui("is_home")
                        is_home_true = (str(is_home_val).lower() in ["true", "1"]) or (is_home_val == 1) or (is_home_val is True)
                        
                        if is_home_true:
                            # 홈 도착: 초록색 원, 완료 텍스트
                            self.ui.robot_home_circle.setStyleSheet("border-radius: 30px; background-color: #28a745;") 
                            self.ui.robot_home_label.setText("로봇 홈 복귀 완료")
                        else:
                            # 홈 이탈: 주황색 원, 미복귀 텍스트
                            self.ui.robot_home_circle.setStyleSheet("border-radius: 30px; background-color: #ffc107;") 
                            self.ui.robot_home_label.setText("로봇 홈 미복귀")
                except AttributeError:
                    pass

        except Exception as e:
            import traceback
            print("[WARN] robot_status_update 오류:", e)
            traceback.print_exc()

    def on_home_button_clicked(self):
        # 1. 스팸 클릭 방지 (1초 이내 중복 클릭 완전 무시)
        curr_time = time.time()
        if curr_time - getattr(self, '_last_home_click_time', 0) < 1.0:
            print("[WARN] 홈 버튼 연속 클릭 방지")
            return
        self._last_home_click_time = curr_time

        try:
            print("[INFO] 로봇 홈으로 이동 요청 시작")
            
            if getattr(self, 'is_manual_moving', False):
                self.show_message("조작 불가", "현재 이동 중입니다.<br>동작이 끝난 후 눌러주세요.", QMessageBox.Warning)
                return
                
            self.is_manual_moving = True # 이동 락(Lock) 걸기

            # ============================================================
            # ★ [요청 반영] 1. 모드버스 변수 전부 초기화 (False)
            # ============================================================
            self.reset_all_robot_variables()
            
            # ============================================================
            # ★ [요청 반영] 2. 로봇 태스크 스탑 (정지)
            # ============================================================
            self.robot_29999.robot_stop()
            print("[CMD] 로봇 프로그램 강제 정지(Stop) 완료")
            
            # ============================================================
            # ★ [요청 반영] 3. 재시작(Play) 후 home_req 1 켜기 
            # (명령 충돌을 막기 위해 0.5초의 간격을 두고 순차 실행)
            # ============================================================
            def start_and_go_home():
                if getattr(self, 'robot_disconnected', True):
                    self.is_manual_moving = False
                    return
                    
                print("[CMD] 로봇 프로그램 재시작(Play) 및 홈 이동 시퀀스 진입")
                self.robot_29999.robot_play()
                
                # 프로그램이 켜지고 안정화될 시간 0.5초 부여 후 명령 전송
                QtCore.QTimer.singleShot(500, self._execute_home_move)

            QtCore.QTimer.singleShot(500, start_and_go_home)

        except Exception as e:
            print(f"[ERROR] 홈 이동 초기화 명령 실패: {e}")
            self.is_manual_moving = False # 에러 시 락 해제
            

    def _execute_home_move(self):
        try:
            print("[CMD] home_req = 1 전송")
            self.set_variable_with_ui("home_req", 1)
            
            self.ui.pause_button.setChecked(False)
            self.ui.pause_button.setText("일시 정지")

            if hasattr(self, 'home_check_timer') and self.home_check_timer.isActive():
                self.home_check_timer.stop()
                
            # ★ 목적지(홈) 관절 좌표를 미리 가져와서 저장해둡니다. (근처 도달 확인용)
            raw_home = self.robot_29999.get_variable_cached("J_home")   # ★ [PATCH] 캐시
            # ★ [PATCH] "NOT_FOUND" 등 문자열이 오면 literal_eval 예외로 home_req=1이 남던 문제 수정
            self._target_home_joints = raw_home if isinstance(raw_home, list) else None
                
            self.home_check_timer = QtCore.QTimer(self)
            self.home_check_timer.timeout.connect(self.check_home_arrival)
            self.home_timeout_count = 0
            self.home_check_timer.start(100) # 0.1초 간격으로 빠르게 확인
            
        except Exception as e:
            print(f"[ERROR] 홈 이동 변수 전송 실패: {e}")
            self.is_manual_moving = False

    def check_home_arrival(self):
        """타이머에 의해 0.1초마다 호출되어 홈 도착 여부를 모니터링하는 함수"""
        try:
            self.home_timeout_count += 1
            
            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() in ["true", "1"]) or (is_home_val == 1) or (is_home_val is True)
            
            near_home = False
            # ★ [요청 반영] 홈 위치 '근처'에 도달했는지 확인 (오차 0.05 라디안 허용)
            if hasattr(self, '_target_home_joints') and isinstance(self._target_home_joints, list) and len(self._target_home_joints) == 6:
                cur_j = self.get_current_joints()
                dist = sum(abs(c - h) for c, h in zip(cur_j, self._target_home_joints))
                if dist < 0.05:
                    near_home = True
            
            # ================================================================
            # ★ [요청 반영] 홈 근처에 도달하거나, is_home이 들어오면 무조건 끔
            # ================================================================
            if is_home_true or near_home:
                print(f"[INFO] 로봇 홈 복귀 완료. (is_home={is_home_true}, 근처도달={near_home}) -> home_req = 0 전송")
                self.set_variable_with_ui("home_req", 0)
                
                self.home_check_timer.stop()
                    
                # 홈 버튼 UI 초기화 및 포즈 기억 지우기
                self._reset_pose_memory()
                if hasattr(self.ui, 'home_pose_btn'):
                    self.ui.home_pose_btn.blockSignals(True)
                    self.ui.home_pose_btn.setChecked(False)
                    self.ui.home_pose_btn.blockSignals(False)
                    
                self.is_manual_moving = False # 락 해제
                return

            # 타임아웃 (30초 = 100ms * 300)
            if self.home_timeout_count > 300: 
                print("[WARN] 홈 이동 대기 시간 초과(30초). 강제로 home_req를 0으로 변경합니다.")
                self.set_variable_with_ui("home_req", 0)
                self.home_check_timer.stop()
                
                if getattr(self, 'auto_mode', False) == False:
                    self.robot_29999.robot_stop()
                    
                self.is_manual_moving = False
                
        except Exception as e:
            print(f"[ERROR] 홈 도착 감시 중 오류 발생: {e}")
            self.home_check_timer.stop()
            self.is_manual_moving = False
    
    def on_pause_button_toggled(self, checked=False):
        if checked:
            try:
                self.robot_29999.robot_pause()
                self.pause_event.clear()
                self.ui.pause_button.setText("재시작")
                print("[INFO] 로봇 작업 일시 정지")
            except Exception as e:
                print(f"[ERROR] 작업 일시 정지 오류: {e}")
        else:
            try:
                self.robot_29999.robot_play()
                self.pause_event.set()
                self.ui.pause_button.setText("일시 정지")
                print("[INFO] 로봇 작업 재시작")
            except Exception as e:
                print(f"[ERROR] 작업 재시작 오류: {e}")
            
    def _request_job_threads_stop(self):
        """★ [PATCH] 작업 스레드 협조적 종료 요청 + terminate 시절 고착되던 플래그 초기화"""
        self._user_stop_requested = True   # 이 종료로 인한 '작업 비정상 종료' 알람 중복 방지
        for key in (1, 2, 0):
            t = self.threads.get(key)
            if t is not None and t.isRunning():
                t.requestInterruption()
        # 일시정지 대기 중인 스레드도 깨워서 wait_check()에서 빠져나가게 함
        self.pause_event.set()
        self._reset_sequence_flags()

    def _reset_sequence_flags(self):
        """스레드가 비정상 종료되어도 뮤트/지그 상태 플래그가 남지 않도록 강제 초기화"""
        self.is_jig_moving_1 = False
        self.is_jig_moving_2 = False
        self._mute_changing = False
        self.seq_mute_active = False
        self.overlap_in_progress = False
        self.cross_trigger_cell = None
        self.is_waiting_for_bolt = False

    def on_stop_button_clicked(self):
        try:
            try:
                if not getattr(self, 'robot_disconnected', True):
                    self.robot_29999.robot_stop()
                    print("[INFO] 로봇 작업 정지 명령 전송")
            except Exception as e:
                print(f"[WARN] 로봇 미연결 상태로 정지 명령 생략: {e}")
            
            # 수동 모드로 강제 전환하여 오토 루프 진입 차단
            self.auto_mode = False
            self.ui.auto_btn.setChecked(False)
            self.ui.manual_btn.setChecked(True)
            
            # ★ [PATCH] 수동 포즈 이동 대기 루프 중단 요청 (정지를 '도착'으로 오인하던 문제)
            self._manual_abort = True

            # ★ [PATCH] terminate() 제거 → 협조적 종료. 스레드는 다음 wait_check()에서 빠져나갑니다.
            self._request_job_threads_stop()
            
            # 큐 및 변수 찌꺼기 초기화
            self.waiting_cell = None
            self.robot_occupant = 0
            self.is_back_working = False
            self.active_popups = {1: None, 2: None, 0: None}
            self._post_init_cell = None
            
            # =========================================================
            # ★ 너트러너 작업 정지 신호(MDO 34) 정상 송출
            # =========================================================
            if getattr(self, 'io_module', None) is not None:
                print("[CMD] 너트러너 작업 정지 신호 전송 (MDO 34 ON)")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(34, 1))
                QtCore.QTimer.singleShot(500, lambda: self.io_module.Send_que.put(self.io_module.Write_DO_Data(34, 0)))
                
                # 추가로 진행 중일지 모르는 실린더/공압/피더기 작동도 안전하게 OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(32, 0)) # NR Start OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(39, 0)) # 실린더 하강 OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(40, 0)) # 진공 OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(43, 0)) # SFD Start OFF
            
        except Exception as e:
            print(f"[ERROR] 작업 정지 오류: {e}")
        finally:
            self.pause_event.set()
            self.ui.pause_button.setChecked(False)
            self.ui.pause_button.setText("일시 정지")
            self.robot_status_update()
        
    def on_robot_power_on_button_clicked(self, pressed):
        if not pressed:
            return

        # ★ 1초 이내 연속 클릭 방지
        curr_time = time.time()
        if curr_time - getattr(self, '_last_power_click', 0) < 1.0:
            self.ui.power_on_button.blockSignals(True)
            self.ui.power_on_button.setChecked(False)
            self.ui.power_on_button.blockSignals(False)
            return
        self._last_power_click = curr_time

        print(f"[INFO] Power sequence start. Current: Power={self.is_robot_power_on}, Mode={self.robot_mode}")
        
        # 이미 켜져 있고 브레이크도 풀린 상태(7)면 무시
        if self.is_robot_power_on and self.robot_mode == 7:
            print("[INFO] 로봇이 이미 Ready(Running) 상태입니다.")
            self.ui.power_on_button.blockSignals(True)
            self.ui.power_on_button.setChecked(False)
            self.ui.power_on_button.blockSignals(False)
            return

        # ★ 타이머 객체 재사용 로직으로 교체 (프로그램 튕김 방지)
        if not hasattr(self, 'power_on_timer'):
            self.power_on_timer = QtCore.QTimer(self)
            self.power_on_timer.timeout.connect(self.robot_power_on)
        else:
            self.power_on_timer.stop()

        # 딜레이 및 카운터 변수 초기화
        self.retry_counter = 0 

        # 시작 단계 설정
        if self.is_robot_power_on and self.robot_mode == 5:
            print("[INFO] Idle 상태 감지 → 브레이크 해제 단계(STEP 3)부터 시작")
            self.power_on_step = 3
        else:
            print("[INFO] 전원 OFF 상태 감지 → 전체 시퀀스(STEP 0) 시작")
            self.power_on_step = 0

        # 타이머 시작 (0.2초 간격)
        self.power_on_timer.start(200)
        
    def robot_power_on(self):
        try:
            # 로컬 변수로 상태 매핑
            power = self.is_robot_power_on
            mode = self.robot_mode

            # =========================================================
            # STEP 0: 제어권 요청 및 전원 ON 명령
            # =========================================================
            if self.power_on_step == 0:
                self.robot_29999.robot_remote_mode_on()
                self.robot_29999.robot_power_on()
                print("[STEP 0] Remote 모드 전환 및 Power ON 명령 전송")
                self.power_on_step = 1
                return

            # =========================================================
            # STEP 1: 전원 켜기
            # =========================================================
            if self.power_on_step == 1:
                # 전원이 꺼져있거나 상태를 못 읽었으면 지속적으로 명령 전송
                if not power: 
                    self.robot_29999.robot_power_on()
                else:
                    print(f"[STEP 1] Power ON 확인됨 (Power: {power}) → Idle 대기 진입")
                    self.power_on_step = 2
                    self.retry_counter = 0
                return

            # =========================================================
            # STEP 2: Idle 모드 대기
            # =========================================================
            if self.power_on_step == 2:
                # Idle 상태 (5) 확인
                if mode == 5:
                    print(f"[STEP 2] Idle 모드 진입 확인 (Mode: {mode}) → 브레이크 해제 진입")
                    self.power_on_step = 3
                    self.retry_counter = 0
                else:
                    # 로그 폭주 방지: 10틱(2초)마다 한 번씩만 출력
                    if self.retry_counter % 10 == 0:
                        print(f"[STEP 2] Idle 모드 대기 중... 현재 Mode: {mode}")
                    self.retry_counter += 1
                return

            # =========================================================
            # STEP 3: 브레이크 해제
            # =========================================================
            if self.power_on_step == 3:
                # 1. Ready(7)가 되었다면 성공
                if mode == 7:
                    print("[STEP 3] 로봇 Ready 상태 변경 확인! → 완료 단계로 이동")
                    self.power_on_step = 4
                    return

                # 2. 아직 7이 아니라면 5틱(1초)마다 한 번씩 브레이크 해제 반복 전송
                if self.retry_counter % 5 == 0:
                    self.robot_29999.robot_brakeRelease()
                    print(f"[STEP 3] 브레이크 해제 명령 전송... (현재 Mode: {mode})")
                
                self.retry_counter += 1
                
                # 75틱(15초) 이상 무한 대기 시 에러 간주
                if self.retry_counter > 75:
                    raise Exception("브레이크 해제 대기 시간 초과 (15초)")
                return

            # =========================================================
            # STEP 4: 최종 완료 처리
            # =========================================================
            if self.power_on_step == 4:
                print("[STEP 4] 로봇 부팅 시퀀스 완료.")
                self.power_on_timer.stop()

                # 토글 버튼이 눌린 채로 남아있지 않게 원복 (UI 시각화)
                self.ui.power_on_button.blockSignals(True)
                self.ui.power_on_button.setChecked(False)
                self.ui.power_on_button.blockSignals(False)
                return

        except Exception as e:
            print(f"[ERROR] Power ON 시퀀스 오류: {e}")
            import traceback
            traceback.print_exc()
            
            self.power_on_timer.stop()
            self.ui.power_on_button.blockSignals(True)
            self.ui.power_on_button.setChecked(False)
            self.ui.power_on_button.blockSignals(False)

    def on_robot_power_off_button_clicked(self, pressed):
        if pressed:    
            try:
                self.robot_29999.robot_power_off()
                self.button_disable()
                print("[INFO] 전원 끄기")
            except Exception as e:
                print(f"[ERROR] 전원 끄기 실패: {e}")
                
    def update_connect_button_state(self):
        if not self.robot_disconnected:
            self.ui.robot_connect_button.setText("시스템 연결 성공")
            self.ui.robot_connect_button.setEnabled(False)
        else:
            self.ui.robot_connect_button.setText("시스템 연결 하기")
            self.ui.robot_connect_button.setEnabled(True)

    def on_robot_connect_button_clicked(self):
        print("[USER] 로봇 수동 연결 시도")
        self.connect_robot(show_popup=True)

    def on_slider_changed(self, value):
        self.ui.speed_label.setText(f"{value}%")
            
    def on_speed_changed(self, value):
        self.ui.speed_label.setText(f"{value}%")
        self.robot_29999.set_robot_speed(speed=value)
        print(f"[INFO] Speed set to {value}%")
                
    def set_current_datetime(self):
        current_time = QDateTime.currentDateTime()
        self.ui.dateTimeEdit.setDateTime(current_time)
        self.ui.dateEdit.setDateTime(current_time)
        self.db_manager.insert_datetime()
        
        # ★ [추가] 켜둔 상태로 자정(12시)이 지나 날짜가 바뀌는 것을 실시간 감지
        new_date_str = current_time.toString("yyyy-MM-dd")
        if hasattr(self, 'current_date_str') and self.current_date_str != new_date_str:
            print(f"[SYSTEM] 날짜 변경 감지 ({self.current_date_str} -> {new_date_str}). 금일 작업량을 0으로 초기화합니다.")
            self.current_date_str = new_date_str
            self.current_workload = 0
            self.current_ng_count = 0
            self.product_counts = {0: 0, 1: 0, 2: 0, 3: 0} # 제품별 카운터도 0으로 리셋
            self.set_current_workload(self.current_workload)
            
            if hasattr(self.ui, 'history_table'):
                self.ui.history_table.setRowCount(0) 
            self.roll_history_file_path()

    def set_current_workload(self, val):
        try:
            self.current_workload = val
            
            # ★ OK 수량 계산 (총량 - NG)
            ok_count = self.current_workload - getattr(self, 'current_ng_count', 0)
            if ok_count < 0: ok_count = 0
            
            # ★ 수율(Yield) 계산
            if self.current_workload > 0:
                yield_rate = (ok_count / self.current_workload) * 100.0
            else:
                yield_rate = 0.0
                
            # UI 라벨 일괄 적용
            if hasattr(self.ui, 'workload_edit'): self.ui.workload_edit.setText(f"{self.current_workload}")
            if hasattr(self.ui, 'quantity_label'): self.ui.quantity_label.setText(f"{self.current_workload}")
            if hasattr(self.ui, 'ng_label'): self.ui.ng_label.setText(f"{getattr(self, 'current_ng_count', 0)}")
            if hasattr(self.ui, 'ok_label'): self.ui.ok_label.setText(f"{ok_count}")
            if hasattr(self.ui, 'yield_label'): self.ui.yield_label.setText(f"{yield_rate:.1f}%")
            
            # 총 작업량 DB 기록
            self.db_manager.insert_workload(self.current_workload)
            
        except Exception as e:
            print(f"[ERROR] 작업 수량/수율 UI 업데이트 실패: {e}")
            
    def increment_workload(self, is_ok=True):
        """작업 1사이클 종료 시 총 작업량을 증가하고, 결과에 따라 NG 또는 제품 카운트를 올립니다."""
        self.current_workload += 1
        
        if not is_ok:
            # ★ 사이클 중 에러나 NG가 발생한 경우 NG 카운트 증가 및 DB 저장
            self.current_ng_count = getattr(self, 'current_ng_count', 0) + 1
            self.db_manager.save_setting(f"ng_count_{self.current_date_str}", str(self.current_ng_count))
        else:
            # 정상(OK) 완료된 경우 제품별 카운트 증가
            prod_type = getattr(self, 'current_product_type', 0)
            self.product_counts[prod_type] = self.product_counts.get(prod_type, 0) + 1
            self.db_manager.save_setting(f"workload_{self.current_date_str}_prod_{prod_type}", str(self.product_counts[prod_type]))
        
        # 메인 라벨(Total, OK, NG, 수율) UI 업데이트 및 저장
        self.set_current_workload(self.current_workload) 

    def reset_today_workload(self):
        """리셋 버튼 클릭 시 오늘 작업량(총량, NG, 제품별)을 모두 0으로 강제 초기화"""
        self.current_workload = 0
        self.current_ng_count = 0 # ★ NG 카운트 초기화
        self.db_manager.save_setting(f"ng_count_{self.current_date_str}", "0")
        
        for i in range(4):
            self.product_counts[i] = 0
            self.db_manager.save_setting(f"workload_{self.current_date_str}_prod_{i}", "0")
            
        self.set_current_workload(0)
        print("[SYSTEM] 금일 작업량이 0으로 강제 초기화되었습니다.")
        
        if hasattr(self.ui, 'history_table'):
            self.ui.history_table.setRowCount(0) # 화면 표 지우기
        self.roll_history_file_path()

    def add_log(self, text):
        # 1. 스크롤바 객체 가져오기
        scrollbar = self.ui.scrollArea.verticalScrollBar()
        
        # 2. 현재 사용자가 스크롤바를 조작 중이거나 위를 보고 있는지 상태 확인
        # - isSliderDown(): 터치/마우스로 스크롤바 손잡이를 누르고 있는 상태인가?
        # - value() < maximum() - 10: 현재 스크롤바가 맨 밑이 아닌 과거 로그 영역에 있는가? (10은 픽셀 오차 여유)
        is_user_reading = scrollbar.isSliderDown() or scrollbar.value() < (scrollbar.maximum() - 10)
        
        # 3. 텍스트가 추가되기 직전의 현재 스크롤 위치 저장
        saved_scroll_value = scrollbar.value()

        # 4. 로그 텍스트 추가
        self.ui.scrollArea.append(text)

        # 5. 최대 줄 수(250줄) 제한 메모리 관리 (기존 유지)
        max_lines = 250
        if self.ui.scrollArea.document().blockCount() > max_lines:
            cursor = self.ui.scrollArea.textCursor()
            cursor.movePosition(QtGui.QTextCursor.Start)
            cursor.select(QtGui.QTextCursor.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
        
        # =====================================================================
        # ★ [핵심] 사용자의 행동에 따른 스마트 스크롤 제어
        # =====================================================================
        if is_user_reading:
            # 사용자가 스크롤을 잡고 있거나 과거 로그를 읽는 중이라면 억지로 내리지 않고 제자리 유지!
            scrollbar.setValue(saved_scroll_value)
        else:
            # 평상시(스크롤바가 맨 밑에 있을 때)에는 새 로그가 보일 수 있도록 맨 밑으로 쫓아감
            scrollbar.setValue(scrollbar.maximum())
        
    # [Helper] 스마트 입력 대기 (타임아웃 보정 기능 포함)
    def wait_for_input(self, input_idx, target_val=1, timeout=5.0, msg=""):
        start_time = time.time()
        
        while True:
            was_paused = not self.pause_event.is_set()
            self.wait_check() 
            
            if was_paused:
                start_time = time.time()

            current_val = self.io_module.Read_Input_Data()[input_idx]
            if current_val == target_val:
                return True
            
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(f"[TIMEOUT] {msg} 대기 시간 초과 ({timeout}s)")
                return False
                
            time.sleep(0.05)
            
    # [Helper] 일시 정지 및 안전 상태 체크 (Event 방식 - 최적화됨)
    def wait_check(self, ignore_auto_mode=False):
        if not self.pause_event.is_set():
            print("[PAUSE] 스레드 대기 상태 진입 (Event Wait)...")
            self.pause_event.wait()
            print("[RESUME] 스레드 재개")
            
        if self.is_alarm_state(): raise Exception("작업 중 알람 발생")
        
        # ★ [수정] ignore_auto_mode가 True일 때는 오토모드 검사를 무시함
        if not ignore_auto_mode and not self.auto_mode: 
            raise Exception("작업 중 오토 모드 해제")
            
        if self.robot_disconnected: raise Exception("작업 중 로봇 연결 끊김")
        
        if self.is_robot_power_on and not self.is_task_running:
            raise Exception("로봇 프로그램(Task) 중단됨")
        
        # 스레드 타입 안전 검사
        curr_thread = QThread.currentThread()
        if hasattr(curr_thread, 'isInterruptionRequested') and curr_thread.isInterruptionRequested():
            raise Exception("프로그램 종료 요청")
            
    # BOLT 클래스 내부에 추가
    def sequence_nr_init(self):
        print("\n==================================")
        print("[SEQ] NR 초기화(배출) 작업 시작")
        print("==================================")
        
        try:
            if self.robot_disconnected or not self.is_robot_power_on:
                raise Exception("로봇 연결 또는 전원 상태 불량")
            
            if self.io_module is not None:
                print("[SEQ] 이동 전 NR 실린더 강제 상승 및 안전 확인 중...")
                # 혹시 켜져 있을지 모르니 하강 및 진공 출력 강제 OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(39, 0)) # 실린더 전진(하강) OFF
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(40, 0)) # 흡착(진공) OFF
                
                # mdi 39번 (NR 실린더 상승 감지 센서)가 1이 될 때까지 대기
                if not self.wait_for_input(39, 1, 3.0, "초기화 전 NR 실린더 상승 확인"):
                    raise Exception("NR 실린더가 상승하지 않아 로봇을 이동할 수 없습니다. (충돌 위험)")

            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() in ["true", "1"])
            
            if not (is_home_true and self.robot_at_home()):
                print("[SEQ] 로봇이 홈 위치에 없습니다. 먼저 홈 복귀를 명령합니다.")
                self.move_robot_to_home_sync()
                print("[SEQ] 선행 홈 복귀 완료. 배출 시퀀스를 이어갑니다.")
            else:
                print("[SEQ] 로봇 홈 위치 확인 완료.")

            self.set_variable_with_ui("NR_init_start", 1)
            print("[SEQ] NR_init_start = 1 전송 완료. 배출 위치 도착 대기 중...")

            wait_time = 0
            while True:
                # ★ [수정] 초기화 중이므로 수동모드여도 통과시킴
                self.wait_check(ignore_auto_mode=True)
                
                req_val = self.get_variable_with_ui("NR_cyl_req")
                if str(req_val).lower() in ["true", "1"]:
                    print("[SEQ] 배출 위치 도착 확인 (NR_cyl_req=1). 볼트 배출 시작.")
                    break
                    
                time.sleep(0.5)
                wait_time += 0.5
                if wait_time > 30: 
                    raise Exception("NR 초기화 위치 이동 타임아웃")

            # =========================================================
            # ★ [수정] 호스 내 잔류 볼트 완전 제거를 위한 2단 배출 시퀀스
            # (SFD 1회 -> 실린더 2회 -> SFD 1회 -> 실린더 2회)
            # =========================================================
            def discharge_cycle(cycle_num):
                print(f"[SEQ] 볼트 배출 {cycle_num}차: 피더기(SFD) 강제 공급 작동")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(43, 1)) # SFD Start(MDO 43) ON
                time.sleep(0.2)
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(43, 0)) # SFD Start OFF
                
                # 피더기에서 볼트가 공압을 타고 팁에 안착할 때까지 대기
                for _ in range(10): # 1.0초 대기
                    self.wait_check(ignore_auto_mode=True) # 비상정지 감시
                    time.sleep(0.1)

                # 실린더 전/후진 2회 반복
                for i in range(1, 3):
                    print(f"[SEQ] 볼트 배출 {cycle_num}차: NR 실린더 전진/후진 ({i}/2)")
                    
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(39, 1)) # 실린더 하강(MDO 39)
                    for _ in range(10): # 1.0초 유지
                        self.wait_check(ignore_auto_mode=True)
                        time.sleep(0.1)
                        
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(39, 0)) # 실린더 상승
                    for _ in range(10): # 1.0초 유지
                        self.wait_check(ignore_auto_mode=True)
                        time.sleep(0.1)

            # 1차 배출 사이클 실행 (SFD 1회 -> 실린더 2회)
            discharge_cycle(1)
            
            # 2차 배출 사이클 실행 (SFD 1회 -> 실린더 2회)
            discharge_cycle(2)
            # =========================================================

            print("[SEQ] 볼트 배출 완료. 로봇 홈 복귀 요청")
            self.set_variable_with_ui("NR_init_start", 0)
            
            self.move_robot_to_home_sync() 
            print("[SEQ] NR 초기화 전체 시퀀스 정상 완료\n")
            return True # ★ [추가] 성공적으로 끝남을 알림

        except Exception as e:
            print(f"[ERROR] NR 초기화 실패: {e}")
            self.set_variable_with_ui("NR_init_start", 0)
            self.report_alarm_signal.emit(f"NR 초기화 실패: {e}", "ERROR")
            return False # ★ [추가] 실패했음을 알림
            
    def start_job(self, cell_num=0):
        try:
            print(f"[START] 스레드 생성 요청 - Cell {cell_num}")
            
            # 기존 스레드 객체 확인
            existing_thread = self.threads.get(cell_num)
            
            if existing_thread is not None:
                if existing_thread.isRunning():
                    print(f"[WARN] Cell {cell_num}은 이미 작업 중입니다.")
                    return
                else:
                    # ★ 완전히 종료될 때까지 잠시 대기하여 메모리 정리(Join 역할)
                    existing_thread.wait()

            # 스레드 생성 (HEAD / BACK 구분)
            if self.system_mode == "HEAD":
                thread = HeadJobThread(self, target_cell=cell_num)
            else:
                thread = BackJobThread(self)

            # =========================================================
            # ★ [핵심 수정] 시그널 연결 방식 변경
            # 이전: thread.finished_signal.connect(lambda: self.on_job_finished(cell_num))
            # 현재: 스레드에서 셀 번호와 완료 상태(FINISHED/ERROR)를 모두 보내주므로 직접 연결
            # =========================================================
            thread.finished_signal.connect(self.on_job_finished)
            
            # 스레드 저장 및 시작
            self._user_stop_requested = False
            self.threads[cell_num] = thread
            thread.start()
            
        except Exception as e:
            print(f"[ERROR] 시작 실패: {e}")
            
    def job_head(self, target_cell):
        paused = self.ui.pause_button.isChecked()
        if paused:
            print("[WARN] 일시 정지 상태입니다.")
            return "PAUSED", [], "일시정지됨", 0.0

        result, failed_bolts, err_msg, c_t = self.job_command_head(target_cell) 
        
        is_ok = (result == "FINISHED" and len(failed_bolts) == 0)
        self.increment_workload(is_ok=is_ok)
        
        if result == "FINISHED":
            print(f"[INFO] Head Job(Cell {target_cell}): 작업 사이클 정상 종료.")
        else:
            print(f"[ERROR] Head Job(Cell {target_cell}): 작업 실패 또는 중단됨.")
            
        return result, failed_bolts, err_msg, c_t

    def job_back(self):
        paused = self.ui.pause_button.isChecked()
        if paused:
            print("[WARN] 일시 정지 상태입니다.")
            return "PAUSED", [], "일시정지됨", 0.0

        result, failed_bolts, err_msg, c_t = self.job_command_back()
        
        is_ok = (result == "FINISHED" and len(failed_bolts) == 0)
        self.increment_workload(is_ok=is_ok)
        
        if result == "FINISHED":
            print("[INFO] Back Job: 작업 사이클 정상 종료.")
        else:
            print(f"[ERROR] Back Job: 작업 실패 또는 중단됨.")
            
        return result, failed_bolts, err_msg, c_t

   #### 변수명 규칙 ####

    # 접두어: mdo_ (출력), mdi_ (입력), var_ (로봇 변수)
    # 장비명: nr_ (너트러너), sfd_ (볼트피더), jig_ (지그), prod_ (제품)
    # 동작: start, adv (전진), ret (후진), clamp, detect
    
    # ====================================================================
    # [SECTION 1] 공통 서브 태스크 (Common Sub-Tasks)
    # ====================================================================
    
    # ====================================================================
    # ★ [PATCH] 볼트 통과 센서(MDI 41) 전용 감시 스레드
    #   센서가 공급 호스 중간에 있어 볼트가 지나가는 순간에만 짧게 ON 됩니다.
    #   UI 타이머(30ms)나 작업 루프의 폴링에 의존하면 펄스를 놓치므로,
    #   별도 스레드가 짧은 주기로 입력을 읽어 상승 에지를 '카운트'합니다.
    #   공급 판정은 "슈팅 전후 카운트가 늘었는가"로 하므로 타이밍과 무관하게 잡힙니다.
    #   ※ 단, IO 모듈(IOmodule.py)이 입력을 갱신하는 주기보다 짧은 펄스는
    #     어떤 소프트웨어로도 볼 수 없습니다 → 센서 앰프 OFF 딜레이 병행 권장.
    # ====================================================================
    SFD_DETECT_MDI    = 41     # 볼트 통과 센서 입력 번호
    SFD_MON_INTERVAL  = 0.002  # 감시 주기 [s]
    SFD_EDGE_DEBOUNCE = 0.10   # 이 시간 안의 재상승은 같은 볼트(채터링)로 간주 [s]

    def _start_sfd_sensor_monitor(self):
        th = getattr(self, '_sfd_mon_thread', None)
        if th is not None and th.is_alive():
            return
        # Windows 기본 sleep 해상도(약 15.6ms)를 1ms로 → 짧은 주기 감시 가능
        if platform.system() == "Windows":
            try:
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception as e:
                print(f"[SFD MON] timeBeginPeriod 설정 실패(무시): {e}")
        self._sfd_mon_stop = threading.Event()
        self._sfd_mon_thread = threading.Thread(target=self._sfd_sensor_monitor_loop,
                                                name="SFD_SENSOR_MON", daemon=True)
        self._sfd_mon_thread.start()
        print("[SFD MON] 볼트 통과 센서 감시 스레드 시작")

    def _sfd_sensor_monitor_loop(self):
        idx = self.SFD_DETECT_MDI
        prev = 0
        rise_t = None
        last_rise_t = 0.0
        stat_n, stat_sum, stat_max, stat_done = 0, 0.0, 0.0, False
        last_io = None

        while not self._sfd_mon_stop.is_set():
            io = self.io_module
            if io is None:
                prev, rise_t = 0, None
                time.sleep(0.1)
                continue
            if io is not last_io:            # IO 재연결 시 상태/통계 초기화
                last_io = io
                prev, rise_t = 0, None
                stat_n, stat_sum, stat_max, stat_done = 0, 0.0, 0.0, False
            try:
                t0 = time.perf_counter()
                inp = io.Read_Input_Data()
                call_dt = time.perf_counter() - t0
            except Exception:
                time.sleep(0.05)
                continue
            if not inp or len(inp) <= idx:
                time.sleep(0.05)
                continue

            # 진단: 입력 읽기 1회 소요시간 (통신을 직접 하는 구조면 이 값이 곧 감시 주기)
            if not stat_done:
                stat_n += 1
                stat_sum += call_dt
                stat_max = max(stat_max, call_dt)
                if stat_n >= 2000:
                    stat_done = True
                    print(f"[SFD MON] Read_Input_Data 소요: 평균 {stat_sum / stat_n * 1000:.2f}ms, "
                          f"최대 {stat_max * 1000:.2f}ms (감시 주기 {self.SFD_MON_INTERVAL * 1000:.0f}ms)")

            val = 1 if inp[idx] == 1 else 0
            now = time.time()
            if val == 1 and prev == 0:
                rise_t = now
                if now - last_rise_t >= self.SFD_EDGE_DEBOUNCE:
                    with self._sfd_lock:
                        self._sfd_pulse_count += 1
                        n = self._sfd_pulse_count
                    # 기존 퍼지(배출) 시퀀스 호환: 대기 구간이면 래치도 세움
                    if getattr(self, 'is_waiting_for_bolt', False):
                        self._bolt_detect_latched = True
                    print(f"[SFD MON] 볼트 통과 펄스 감지 #{n}")
                last_rise_t = now
            elif val == 0 and prev == 1 and rise_t is not None:
                print(f"[SFD MON]   └ 관측된 펄스 폭 약 {(now - rise_t) * 1000:.0f}ms")
                rise_t = None
            prev = val
            time.sleep(self.SFD_MON_INTERVAL)

    def _sfd_get_pulse_count(self):
        with self._sfd_lock:
            return self._sfd_pulse_count

    def _sfd_sync_mark(self):
        """현재까지의 펄스를 모두 '설명된 것'으로 간주 (작업 시작 시 호출)"""
        self._sfd_pulse_mark = self._sfd_get_pulse_count()

    # ====================================================================
    # ★ [PATCH] 볼트 공급 설정값 - 현장에서 조정
    # ====================================================================
    SFD_READY_TIMEOUT   = 5.0   # 피더 Ready 대기 [s] (1회당)
    SFD_READY_ATTEMPTS  = 3     # Ready 대기 재시도 횟수 (슈팅 전이므로 볼트 누적 위험 없음)
    SFD_START_PULSE     = 0.5   # 피더 START 출력 유지 시간 [s] (기존값)
    SFD_DETECT_TIMEOUT  = 2.0   # START OFF 후 통과 감지 대기 [s]
    SFD_SETTLE_TIME     = 0.05  # 감지 후 팁 안착 대기 [s] (기존값)
    SFD_SKIP_FEED_ON_EXTRA_PULSE = True
    # True: 직전 공급 이후 예상 밖 통과 펄스(지연 도착/이중 공급)가 있었으면
    #       팁에 볼트가 있다고 보고 이번 슈팅을 생략 (볼트 누적 → 파이프 파손 방지)

    # 1-1. 볼트 공급
    # ★ [PATCH] 슈팅은 위치당 1회만. 미감지여도 작업을 멈추지 않고 체결을 시도합니다.
    #   (볼트가 실제로 없으면 너트러너가 토크 미도달 NG → 작업 종료 후 NG 목록으로 수동 체결)
    def task_feed_bolt(self, mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err, max_retries=None):
        """
        반환: True  = 체결 진행 (감지 결과는 self._last_feed_status 참고)
              False = 작업 정지 (피더 에러 / Ready 불가 → 슈팅 자체를 못 한 경우)
        max_retries 인자는 호환용으로 남겨두었으며 사용하지 않습니다 (슈팅은 항상 1회).
        """
        print("[AUTO FEED] 볼트 공급 시작 (SFD)")
        self._last_feed_status = None

        if self._read_inputs_checked()[mdi_sfd_err] == 1:
            msg = "피더기(SFD) 에러 상태입니다. 리셋이 필요합니다."
            print(f"[ERROR] {msg}")
            self.report_alarm_signal.emit(msg, "ERROR")
            return False

        # (0) 직전 공급 이후 예상 밖 통과 펄스 확인 → 팁에 볼트가 이미 있을 수 있음
        now_cnt = self._sfd_get_pulse_count()
        extra = now_cnt - self._sfd_pulse_mark
        if extra > 0 and self.SFD_SKIP_FEED_ON_EXTRA_PULSE:
            msg = f"직전 공급 이후 예상 밖 볼트 통과 {extra}회 감지 → 팁에 볼트 있음으로 판단, 이번 공급 생략"
            print(f"[AUTO FEED WARN] {msg}")
            self.report_alarm_signal.emit(msg, "INFO")
            self._sfd_pulse_mark = now_cnt
            self.is_bolt_loaded = True
            self._last_feed_status = "EXTRA_PULSE_SKIP"
            return True

        # (1) 피더 Ready 대기 (아직 쏘지 않았으므로 재시도해도 안전)
        is_ready = False
        for r_try in range(1, self.SFD_READY_ATTEMPTS + 1):
            print(f"[AUTO FEED] 피더기 Ready 신호 대기 중... ({r_try}/{self.SFD_READY_ATTEMPTS})")
            t0 = time.time()
            while True:
                paused = self._wait_check_timed()
                if paused > 0.05:
                    t0 += paused
                inputs = self._read_inputs_checked()
                if inputs[mdi_sfd_err] == 1:
                    msg = "Ready 대기 중 피더기(SFD) 에러 발생!"
                    print(f"[ERROR] {msg}")
                    self.report_alarm_signal.emit(msg, "ERROR")
                    return False
                if inputs[mdi_sfd_ready] == 1:
                    is_ready = True
                    break
                if time.time() - t0 > self.SFD_READY_TIMEOUT:
                    break
                time.sleep(0.05)
            if is_ready:
                break
            print(f"[WARN] 피더기 Ready 신호 타임아웃 ({r_try}/{self.SFD_READY_ATTEMPTS})")
            if r_try < self.SFD_READY_ATTEMPTS:
                time.sleep(1.0)

        if not is_ready:
            msg = "피더기(SFD) Ready 신호 없음 - 볼트 소진 또는 피더 정지 확인 필요"
            print(f"[ERROR] {msg}")
            self.report_alarm_signal.emit(msg, "ERROR")
            return False

        if self._read_inputs_checked()[mdi_sfd_bolt_detect] == 1:
            print("[AUTO FEED WARN] 슈팅 전인데 통과 센서가 ON 상태입니다 (센서 앞 볼트 걸림/센서 이상 의심)")

        # (2) 슈팅 1회 + 통과 펄스 대기 (카운터 기준 → 짧은 펄스도 감시 스레드가 잡음)
        print("[AUTO FEED] 피더기 Ready 확인 완료. 공급 펄스 전송 (1회)")
        base_cnt = self._sfd_get_pulse_count()
        self._bolt_detect_latched = False
        self.is_waiting_for_bolt = True
        detected = False
        start_off_sent = False
        t_shot = time.time()
        try:
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_sfd_start, 1))
            while True:
                paused = self._wait_check_timed()
                if paused > 0.05:
                    t_shot += paused
                elapsed = time.time() - t_shot

                if not start_off_sent and elapsed >= self.SFD_START_PULSE:
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_sfd_start, 0))
                    start_off_sent = True

                if self._sfd_get_pulse_count() > base_cnt:
                    detected = True
                    print(f"[AUTO FEED] 볼트 통과 감지됨 (슈팅 후 {elapsed:.2f}s)")
                    break

                if self._read_inputs_checked()[mdi_sfd_err] == 1:
                    msg = "공급 중 피더기(SFD) 에러 발생!"
                    print(f"[ERROR] {msg}")
                    self.report_alarm_signal.emit(msg, "ERROR")
                    return False

                if elapsed > self.SFD_START_PULSE + self.SFD_DETECT_TIMEOUT:
                    break
                time.sleep(0.01)

            # 감지 직후 START를 끄지 않았다면 최소 펄스 폭은 채운 뒤 OFF
            if not start_off_sent:
                remain = self.SFD_START_PULSE - (time.time() - t_shot)
                if remain > 0:
                    time.sleep(remain)
        finally:
            try:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_sfd_start, 0))
            except Exception:
                pass
            self.is_waiting_for_bolt = False

        if detected:
            time.sleep(self.SFD_SETTLE_TIME)
            # 이번 슈팅으로 설명되는 펄스는 1개. 그 이상(이중 공급/뒤늦은 도착)은 다음 공급 때 '예상 밖'으로 잡힘
            self._sfd_pulse_mark = base_cnt + 1
            self.is_bolt_loaded = True
            self._last_feed_status = "DETECTED"
        else:
            # 재슈팅 금지 (볼트가 호스에 걸려 있다 늦게 오면 2개 누적되어 파이프 파손)
            self._sfd_pulse_mark = base_cnt
            self.is_bolt_loaded = False
            self._last_feed_status = "NOT_DETECTED"
            msg = "볼트 공급 미감지 - 재공급 없이 체결 시도 (볼트 없으면 NG 처리 후 계속 진행)"
            print(f"[AUTO FEED WARN] {msg}")
            self.report_alarm_signal.emit(msg, "INFO")
        return True

    # ====================================================================
    # ★ [PATCH] 너트러너 회전(RUN) 감시 설정값 - 현장에서 조정
    # ====================================================================
    NR_RUN_CLEAR_TIMEOUT = 1.0    # START 전, 이전 사이클의 RUN(MDI 33)이 꺼질 때까지 대기 [s]
    NR_RUN_START_TIMEOUT = 1.0    # START 후 RUN이 켜질 때까지 대기 [s]
    NR_START_MIN_PULSE   = 0.15   # START 최소 유지 시간 [s] (기존 50ms 큐 펄스는 누락 가능)
    NR_START_ATTEMPTS    = 3      # START→RUN 확인 최대 시도 횟수 (모두 실패 시 알람)
    NR_RETRY_INTERVAL    = 0.3    # 재시도 전 START OFF 유지 시간 [s]
    NR_RUN_DROP_GRACE    = 0.5    # 체결 중 RUN이 꺼진 뒤 OK/NG 결과를 기다리는 허용 시간 [s]
    NR_TIGHTEN_TIMEOUT   = 5.0    # 하강 후 체결 결과 대기 [s] (일시정지 시간 제외)
    NR_ABORT_IDLE_WAIT   = 6.0    # 비정상 종료 시 NR이 스스로 사이클을 끝낼 때까지 대기 [s]
    NR_NO_RUN_STOP_JOB   = True   # True : 회전 미시작 시 작업 정지(알람)
                                  # False: 해당 볼트만 NG 처리, 실린더는 내리지 않고 볼트는 팁에 유지한 채 다음 위치 진행

    def _wait_check_timed(self, ignore_auto_mode=False):
        """wait_check() 수행 후 그 안에서 머문 시간(일시정지 시간)을 반환 → 타임아웃 계산에서 제외"""
        t0 = time.time()
        self.wait_check(ignore_auto_mode=ignore_auto_mode)
        return time.time() - t0

    def _read_inputs_checked(self, min_len=45):
        inp = self.io_module.Read_Input_Data() if self.io_module is not None else None
        if not inp or len(inp) < min_len:
            raise Exception("IO 입력 읽기 실패 (IO 모듈 통신 확인)")
        return inp

    def _nr_wait_run_clear(self, mdi_nr_run, timeout, label):
        """RUN 신호가 0이 될 때까지 대기. 시간 내 해제되지 않으면 예외."""
        start_t = time.time()
        while True:
            paused = self._wait_check_timed()
            if paused > 0.05:
                start_t += paused
            if self._read_inputs_checked()[mdi_nr_run] == 0:
                return True
            if time.time() - start_t > timeout:
                raise Exception(f"너트러너 RUN 신호가 해제되지 않음 ({label}, {timeout:.1f}s)")
            time.sleep(0.02)

    def _nr_start_and_confirm_run(self, mdo_nr_start, mdi_nr_run, mdi_nr_err):
        """
        START를 켜고 RUN(회전) 신호를 확인한 뒤 START를 끕니다.
        반환: "RUN"(회전 확인) / "NO_RUN"(재시도 후에도 회전 없음) / "ERR"(NR 에러)
        """
        for attempt in range(1, self.NR_START_ATTEMPTS + 1):
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_nr_start, 1))
            t_on = time.time()
            run_ok = False
            err = False
            while True:
                paused = self._wait_check_timed()
                if paused > 0.05:
                    t_on += paused
                inp = self._read_inputs_checked()
                if inp[mdi_nr_err] == 1:
                    err = True
                    break
                if inp[mdi_nr_run] == 1:
                    run_ok = True
                    run_detect_t = time.time() - t_on
                    break
                if time.time() - t_on > self.NR_RUN_START_TIMEOUT:
                    break
                time.sleep(0.01)

            # 최소 펄스 폭 보장 후 START OFF (큐 처리 지연으로 펄스가 사라지는 것 방지)
            remain = self.NR_START_MIN_PULSE - (time.time() - t_on)
            if remain > 0:
                time.sleep(remain)
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_nr_start, 0))

            if err:
                return "ERR"
            if run_ok:
                print(f"[NR] 회전(RUN) 확인 (START 후 {run_detect_t:.2f}s, 시도 {attempt})")
                return "RUN"

            print(f"[NR WARN] START 후 {self.NR_RUN_START_TIMEOUT:.1f}s 동안 RUN 미감지 "
                  f"(시도 {attempt}/{self.NR_START_ATTEMPTS})")
            if attempt < self.NR_START_ATTEMPTS:
                print(f"[NR] {self.NR_RETRY_INTERVAL:.1f}s 후 START 재시도")
                time.sleep(self.NR_RETRY_INTERVAL)
        return "NO_RUN"

    def _nr_monitor_tightening(self, mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err, mdi_nr_down_chk):
        """
        하강 후 체결 결과 감시. 반환: (res, reason, nr_may_still_run)
        RUN이 결과 신호 없이 꺼지면 '회전 중단'으로 NG 처리합니다.
        """
        print("[NR] 체결 프로세스 감시 중 (RUN 신호 포함)...")
        start_t = time.time()
        last_run_on_t = time.time()
        while True:
            paused = self._wait_check_timed()
            if paused > 0.05:
                start_t += paused
                last_run_on_t += paused

            inp = self._read_inputs_checked()
            now = time.time()
            if inp[mdi_nr_run] == 1:
                last_run_on_t = now

            if inp[mdi_nr_ok] == 1:
                if inp[mdi_nr_down_chk] == 0:
                    return "NG", "실린더 오작동 (체결 중 들림)", False
                return "OK", "", False

            if inp[mdi_nr_ng] == 1:
                error_msg = self.query_nr_error_code("NG")
                reason = error_msg.split("-")[1].strip() if "-" in error_msg else error_msg
                print(f"[NG 감지] 체결 실패 (사유: {reason})")
                return "NG", reason, False

            if inp[mdi_nr_err] == 1:
                error_msg = self.query_nr_error_code("ERR")
                self.report_alarm_signal.emit(f"너트러너 시스템 에러: {error_msg}", "WARN")
                raise Exception(f"Nutrunner System Error: {error_msg}")

            if now - last_run_on_t > self.NR_RUN_DROP_GRACE:
                return "NG", "너트러너 회전 중단 (결과 신호 없이 RUN 해제)", False

            if now - start_t > self.NR_TIGHTEN_TIMEOUT:
                return "NG", "체결 통신 타임아웃 (회전 지속)", True

            time.sleep(0.02)

    # 1-2. 물리적 체결 동작 (NR: Nut Runner 제어)
    # ★ [PATCH] 순서 변경: 공압 ON → START → RUN(회전) 확인 → 실린더 하강 → 결과 감시
    #   기존: START(50ms 큐 펄스)와 실린더 하강을 동시에 출력하고 회전 여부를 보지 않아
    #         회전 없이 하강 → 5초 타임아웃 NG → 다음 위치로 넘어가는 문제가 있었습니다.
    def task_tightening(self, v_done, v_call, v_screw_working, 
                        mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk,
                        mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err):
        
        print("[SEQ] 체결 동작 수행 시작 (Nut Runner)")

        self.set_variable_with_ui(v_done, 0)
        self.set_variable_with_ui(v_screw_working, 1) # 로봇 락(Lock)
        time.sleep(0.1)

        res = "NONE"
        reason = ""
        nr_may_still_run = False

        try:
            # (0) 이전 사이클의 RUN이 남아 있으면 새 START가 무시되거나 오판되므로 먼저 확인
            self._nr_wait_run_clear(mdi_nr_run, self.NR_RUN_CLEAR_TIMEOUT, "START 전")

            # (1) 공압(진공) ON
            print("[NR] 공압(진공) ON")
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_nr_air_run, 1))
            time.sleep(0.1)

            # (2) START → RUN 확인 (확인 전에는 실린더를 내리지 않음)
            run_state = self._nr_start_and_confirm_run(mdo_nr_start, mdi_nr_run, mdi_nr_err)

            if run_state == "ERR":
                error_msg = self.query_nr_error_code("ERR")
                self.report_alarm_signal.emit(f"너트러너 시스템 에러: {error_msg}", "WARN")
                raise Exception(f"Nutrunner System Error: {error_msg}")

            if run_state == "NO_RUN":
                reason = f"너트러너 회전 미시작 ({self.NR_START_ATTEMPTS}회 시도 모두 RUN 신호 없음)"
                # 실린더를 내리지 않았으므로 볼트는 팁에 그대로 → is_bolt_loaded 유지
                if self.NR_NO_RUN_STOP_JOB:
                    # 다음 시작 시 기존 NR 초기화(배출) 시퀀스가 다시 돌도록 하여 잔류 볼트 제거
                    self._is_first_boot_init_done = False
                    # 3회 모두 실패 시 ERROR 알람 (부저/적색등, 수동 모드 전환)
                    self.report_alarm_signal.emit(f"{reason} - 팁에 볼트가 남아 있습니다. 다음 시작 시 배출 시퀀스가 실행됩니다.", "ERROR")
                    raise Exception(reason)
                res = "NG"
            else:
                # (3) 회전 확인 후 실린더 하강
                print("[NR] 회전 확인 → 실린더 하강")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_nr_cyl_run, 1))

                if not self.wait_for_input(mdi_nr_down_chk, 1, 1.5, "너트러너 하강 완료"):
                    print("[NR] 너트러너 실린더 하강 실패 감지 -> NG 처리")
                    res = "NG"
                    reason = "실린더 오작동 (하강 실패)"
                    nr_may_still_run = True
                    # ★ 하강하지 못했으므로 볼트는 팁에 남아 있음 → is_bolt_loaded 유지 (이중 공급 방지)
                else:
                    # 볼트가 체결부에 닿았으므로 결과와 무관하게 소모된 것으로 처리
                    self.is_bolt_loaded = False
                    res, reason, nr_may_still_run = self._nr_monitor_tightening(
                        mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err, mdi_nr_down_chk)

        finally:
            # 정상/예외 모두 출력 원복 (기존에는 wait_check 예외 시 실린더/공압이 켜진 채 남을 수 있었음)
            for idx in (mdo_nr_start, mdo_nr_air_run, mdo_nr_cyl_run):
                try:
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(idx, 0))
                except Exception:
                    pass
            try:
                self.set_variable_with_ui(v_screw_working, 0) # 로봇 락 해제
            except Exception as fe:
                print(f"[WARN] {v_screw_working} 해제 실패: {fe}")

        print(f"[NR] 체결 결과: {res} (사유: {reason})")

        # NR이 아직 돌고 있을 수 있는 경우(하강 실패/타임아웃), 스스로 사이클을 끝낼 때까지 대기.
        # 끝나지 않으면 다음 볼트 START가 무시되므로 작업 정지.
        if nr_may_still_run:
            self._nr_wait_run_clear(mdi_nr_run, self.NR_ABORT_IDLE_WAIT, "비정상 체결 후")

        # 상승 실패 시 물리적 충돌 위험이 있으므로 예외 발생(정지)
        if not self.wait_for_input(mdi_nr_up_chk, 1, 1.5, "너트러너 상승 완료"):
            raise Exception("너트러너 실린더 상승 센서 감지 실패")

        # 로봇과 신호 동기화 (Handshaking)
        if res in ["OK", "NG"]: 
            print(f"[SYNC] {v_done} = 1 전송. 로봇 응답 대기...")
            self.set_variable_with_ui(v_done, 1)
            sync_start_t = time.time()
            while True:
                paused = self._wait_check_timed()
                if paused > 0.05:
                    sync_start_t += paused
                current_call = self.get_variable_with_ui(v_call)
                if str(current_call).lower() in ["false", "0", "none", ""]:
                    reset_retry = 0
                    while reset_retry < 5:
                        self.set_variable_with_ui(v_done, 0)
                        time.sleep(0.05)
                        if str(self.get_variable_with_ui(v_done)).lower() in ["0", "false"]:
                            break
                        reset_retry += 1
                    break 
                
                if time.time() - sync_start_t > 10.0:
                    self.set_variable_with_ui(v_done, 0)
                    raise Exception("로봇 동기화 타임아웃 (로봇 이동 안 함)")
                time.sleep(0.05)

        print("[SEQ] 체결 작업 사이클 정상 종료")
        
        # ★ 결과와 사유를 같이 반환
        return res, reason
        
    # 1-3. 통합 체결 루프
    def process_tightening_loop(self, step_name, var_cell_done, var_screw_call, 
                                var_robot_working, var_robot_screw_working, var_screw_done,
                                mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk,
                                mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err,
                                mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err,
                                start_bolt_idx=0): 
        print(f"[{step_name}] 체결 루프 진입 (시작 번호: {start_bolt_idx + 1}번부터)")
        
        bolt_index = start_bolt_idx
        failed_bolts = []
        
        while True:
            self.wait_check() 
            if self.is_alarm_state(): return False, failed_bolts, bolt_index
            
            # [1] 전체 완료 확인 (로봇이 모든 좌표를 돌고 Work_done을 1로 주었을 때)
            cell_done_val = self.get_variable_with_ui(var_cell_done)
            if str(cell_done_val).lower() in ["1", "true"]: 
                print(f"[{step_name}] 전체 완료 신호 수신 (마지막 작업 번호: {bolt_index}번)")
                return True, failed_bolts, bolt_index

            # [2] 위치 도착 (Screw_call = 1) 확인
            call_val = self.get_variable_with_ui(var_screw_call)
            if str(call_val).lower() in ["1", "true"]:
                
                # ==============================================================
                # ★ [핵심] 로봇이 목적지에 도착해서 멈춘 이 순간! 볼트가 없다면 장전합니다.
                # ==============================================================
                feed_note = ""
                if not getattr(self, 'is_bolt_loaded', False):
                    print(f"[{step_name}] 로봇 위치 도착 완료. 볼트 공급 시작...")
                    if not self.task_feed_bolt(mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err):
                        print(f"[{step_name}] SFD 에러/Ready 불가로 인한 작업 중단")
                        return False, failed_bolts, bolt_index
                    # ★ [PATCH] 미감지여도 멈추지 않고 체결 시도 → 결과가 NG면 사유에 표시
                    feed_status = getattr(self, '_last_feed_status', None)
                    if feed_status == "NOT_DETECTED":
                        feed_note = "볼트 공급 미감지"
                    elif feed_status == "EXTRA_PULSE_SKIP":
                        feed_note = "예상 밖 볼트 통과로 공급 생략"

                # 볼트 장전 성공 시 체결 시작
                bolt_index += 1
                print(f"[{step_name}] {bolt_index}번째 볼트 체결 시작")

                res, reason = self.task_tightening(var_screw_done, var_screw_call, var_robot_screw_working,
                                     mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk,
                                     mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err)
                
                # 체결 결과 판별 및 기록
                if res == "NG":
                    full_reason = f"{feed_note} / {reason}" if feed_note else reason
                    failed_bolts.append(f"{bolt_index} ({full_reason})")
                    print(f"[{step_name}] {bolt_index}번 NG 기록 - 작업 계속 진행 (종료 후 수동 체결)")
                elif feed_note:
                    print(f"[{step_name}] {bolt_index}번: {feed_note} 상태였으나 체결 {res} "
                          f"(센서가 통과를 놓친 것으로 판단)")
            
            # ★ 기존에 있던 "elif not getattr(self, 'is_bolt_loaded', False):" 
            # (이동 중 볼트를 미리 쏘는 로직) 전체를 삭제했습니다!
            
            time.sleep(0.05)
            
    def robot_at_waypoint(self, var_name, tolerance=0.1) -> bool:
        """이동 중 특정 관절 좌표 변수(Waypoint)를 통과했는지 확인합니다."""
        try:
            current_joints = [
                self.actual_joint_base,
                self.actual_joint_shoulder,
                self.actual_joint_elbow,
                self.actual_joint_wrist1,
                self.actual_joint_wrist2,
                self.actual_joint_wrist3
            ]
            
            raw_wp = self.robot_29999.get_variable_cached(var_name)   # ★ [PATCH] 캐시
            
            if raw_wp is None or raw_wp == "NOT_FOUND":
                return False

            wp_joints = ast.literal_eval(raw_wp) if isinstance(raw_wp, str) else raw_wp
            
            if isinstance(wp_joints, list) and len(wp_joints) == 6:
                # 관절값 오차 확인 (허용 오차 진입 시 True 반환)
                return all(abs(c - w) <= tolerance for c, w in zip(current_joints, wp_joints))
                
            return False
        except Exception:
            return False

    # ====================================================================
    # [SECTION 2] Job Head (Main)
    # ====================================================================
    def job_command_head(self, target_cell):
        print(f"[SEQ] Cell {target_cell} 작업 시퀀스 시작")
        start_t = time.time() # ★ C/T 측정 시작!
        error_msg = "-"       # ★ 에러 메시지 초기화

        # -------------------------------------------------------------
        # 1. IO 및 변수 매핑 (Head 전용)
        # -------------------------------------------------------------
        if target_cell == 1:
            mdo_jig_adv, mdo_jig_ret = 0, 1
            mdo_jig_clamp = 2
            
            mdi_jig_adv_chk, mdi_jig_ret_chk = 0, 1
            mdi_jig_clamp_chk_1, mdi_jig_unclamp_chk_1 = 2, 3
            mdi_jig_clamp_chk_2, mdi_jig_unclamp_chk_2 = 4, 5
            mdi_prod_detect_sen, mdi_prod_pos_sen = 6, 7
            
            var_start = "Cell_1_start"
            var_working = "Cell_1_working"
            var_done = "Cell_1_done"
        else:
            mdo_jig_adv, mdo_jig_ret = 8, 9
            mdo_jig_clamp = 10
            
            mdi_jig_adv_chk, mdi_jig_ret_chk = 8, 9
            mdi_jig_clamp_chk_1, mdi_jig_unclamp_chk_1 = 10, 11
            mdi_jig_clamp_chk_2, mdi_jig_unclamp_chk_2 = 12, 13
            mdi_prod_detect_sen, mdi_prod_pos_sen = 14, 15
            
            var_start = "Cell_2_start"
            var_working = "Cell_2_working"
            var_done = "Cell_2_done"

        # [공통 IO 매핑]
        mdo_sensor_reset = 23
        mdo_sensor_mute_a = 24
        mdo_sensor_mute_b = 25  
        mdo_nr_start = 32       
        mdo_nr_cyl_run = 39     
        mdo_nr_air_run = 40     
        mdo_sfd_start = 43
        
        mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err = 32, 33, 34, 35
        mdi_nr_up_chk = 39
        mdi_nr_down_chk = 40     
        mdi_sfd_bolt_detect = 41 
        mdi_sfd_ready, mdi_sfd_err = 43, 44    

        # [로봇 공통 변수]
        var_screw_call = "Screw_call"
        var_screw_done = "Screw_done"
        var_screw_working = "Screw_working"

        # -------------------------------------------------------------
        # 2. [지그 고정]
        # -------------------------------------------------------------
        if not self.sequence_jig_head_ready(target_cell, mdo_jig_clamp, 
                                            mdi_jig_clamp_chk_1, mdi_jig_clamp_chk_2,
                                            mdo_jig_adv, mdo_jig_ret, mdi_jig_adv_chk, 
                                            mdo_sensor_reset, mdo_sensor_mute_a, mdo_sensor_mute_b, mdi_prod_detect_sen, mdi_prod_pos_sen):
            # ★ [PATCH] job_head는 4개 값을 언패킹하므로 2개 반환 시 ValueError 발생
            return "ERROR", [], f"Cell {target_cell} 지그 고정 실패 (제품/클램프/인터락 확인)", time.time() - start_t

        # -------------------------------------------------------------
        # 3. [로봇 권한 획득] 
        # -------------------------------------------------------------
        print(f"[SEQ] Cell {target_cell} 로봇 사용 권한 대기 중...")
        while True:
            self.wait_check()
            if self.is_alarm_state() or not self.auto_mode: 
                self.cleanup_cell(target_cell, var_start,
                                  mdo_jig_adv, mdo_jig_ret, mdi_jig_ret_chk,
                                  mdo_jig_clamp, mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2)
                return "ERROR", [], "로봇 권한 대기 중 알람/오토 해제", time.time() - start_t
            
            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() == "true" or is_home_val == 1 or is_home_val is True)
            
            if self.robot_occupant == 0 and is_home_true and self.robot_at_home():
                self.robot_occupant = target_cell
                print(f"[SEQ] Cell {target_cell} 로봇 권한 획득 (Home 상태 확인)")
                break
            
            if self.robot_occupant == target_cell: break
            time.sleep(0.05)
            
        # 3.5 작업 시작 명령 전 무조건 홈 위치로 명령 전송 및 확인
        self.move_robot_to_home_sync()

        # 4. [메인 작업]
        job_final_status = "FINISHED"
        failed_bolts = []
        
        # ★ [수정] 시작할 때는 팁에 볼트가 없다고 명시 (선행 공급 로직 삭제됨)
        self.is_bolt_loaded = False 
        self._sfd_sync_mark()   # ★ [PATCH] 작업 전(배출/수동 조작) 펄스는 제외하고 새로 카운트
        
        try:
            # (1) 로봇 출발 및 Handshaking (선행 공급 코드가 이 위치에서 삭제되었습니다)
            print(f"[SEQ] Cell {target_cell} 로봇 작업 시작")
            self.set_variable_with_ui(var_start, 1)
            start_sync_t = time.time()
            while True:
                self.wait_check()
                is_working = self.get_variable_with_ui(var_working)
                if str(is_working).lower() in ["1", "true"]:
                    print(f"[SYNC] 로봇 작업 시작 확인. 요청 신호 리셋.")
                    self.set_variable_with_ui(var_start, 0)
                    break
                if time.time() - start_sync_t > 5.0:
                    self.set_variable_with_ui(var_start, 0)
                    raise Exception("로봇 작업 시작 응답 없음 (Timeout)")
                time.sleep(0.05)
            
            # (2) 체결 루프
            loop_success, failed_bolts, _ = self.process_tightening_loop(f"Cell_{target_cell}", var_done, var_screw_call, 
                                                var_working, var_screw_working, var_screw_done,
                                                mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk,
                                                mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err,
                                                mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err,
                                                start_bolt_idx=0) # HEAD는 항상 0부터 시작 (1번 볼트)
            
            if not loop_success:
                raise Exception("체결 작업 중 오류 발생")

            # -------------------------------------------------------------
            # ★ [최적화] 사이클 타임 극단축 (거리 연산 삭제, 0.5초 펄스 트리거)
            # -------------------------------------------------------------
            print(f"[SEQ] Cell {target_cell} 작업 완료. 로봇 홈 복귀 및 회피 감시 시작...")
            
            # =======================================================
            # ★ [타이밍 수정] 로봇이 홈으로 출발하기 위해 위로 상승하는 즉시 허공에 대고 블로우!
            # 다음 셀로 넘어가기 전에 미리 볼트를 파기하여 간섭을 막습니다.
            # =======================================================
            if getattr(self, 'blow_time', 0.0) > 0 and self.io_module is not None:
                print(f"[CMD] NR 볼트 파기 (에어 블로우) 조기 비동기 작동: {self.blow_time}초")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(41, 1))
                print("[CMD] 작업 완료 후 피더기(SFD) 자동 리셋 (MDO 44 ON)")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 1))
                
                def stop_blow():
                    if self.io_module is not None:
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(41, 0))
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 0))
                
                threading.Timer(self.blow_time, stop_blow).start()

            self.set_variable_with_ui("home_req", 1)
            home_pulse_start_t = time.time()
            home_req_reset_done = False # 0.5초 뒤 0으로 껐는지 확인하는 깃발

            overlap_triggered = False
            timeout_cnt = 0
            waypoint_name = f"J_jig{target_cell}_waypoint"

            while True:
                self.wait_check()
                
                if not home_req_reset_done and (time.time() - home_pulse_start_t >= 0.5):
                    self.set_variable_with_ui("home_req", 0)
                    home_req_reset_done = True
                    
                is_home_val = self.get_variable_with_ui("is_home")
                is_home_true = (str(is_home_val).lower() in ["true", "1"] or is_home_val == 1 or is_home_val is True)

                # ===============================================================
                # [1] 지그 교차(Overlap) 시작: 로봇이 웨이포인트 통과 OR 조기 도착 시
                # ===============================================================
                if not overlap_triggered:
                    if self.robot_at_waypoint(waypoint_name, tolerance=0.15) or is_home_true:
                        print(f"[INFO] 🚀 로봇 {waypoint_name} 도달! 즉시 지그 교차(Overlap) 시작!")
                        overlap_triggered = True
                        
                        has_next = False
                        self.ready_to_cross_jig = False

                        if getattr(self, 'waiting_cell', None) is not None:
                            next_cell = self.waiting_cell
                            self.waiting_cell = None 
                            has_next = True
                            
                            self.overlap_in_progress = True 
                            
                            print(f"[SEQ] 🚀 예약된 Cell {next_cell} 바통 전달 (메인 스레드에 시작 요청)")
                            self.cross_trigger_cell = next_cell 

                        # 지그 후진 시작
                        if not self.cleanup_cell(target_cell, var_start,
                                                 mdo_jig_adv, mdo_jig_ret, mdi_jig_ret_chk, 
                                                 mdo_jig_clamp, mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2, has_next=has_next):
                             print(f"[CRITICAL] Cell {target_cell} 설비 초기화 실패")
                             job_final_status = "ERROR"
                             break 

                # ===============================================================
                # [2] 지연 예약 캐치: 웨이포인트를 지나 후진 중 뒤늦게 예약을 눌렀을 경우
                # ===============================================================
                if overlap_triggered and getattr(self, 'waiting_cell', None) is not None:
                    next_cell = self.waiting_cell
                    self.waiting_cell = None 
                    self.overlap_in_progress = True 
                    print(f"[SEQ] 🚀 지연 예약 감지! Cell {next_cell} 바통 전달 (즉각 병렬 실행)")
                    self.cross_trigger_cell = next_cell

                # ===============================================================
                # [3] 최종 종료: 홈 도착 시 즉시 탈출 (블로우 로직은 위로 이동됨)
                # ===============================================================
                if overlap_triggered and (is_home_true or self.robot_at_home()):
                    print("[SUCCESS] 로봇 물리적 홈 도착 및 스레드 정상 종료 조건 완벽 충족.")
                    break # ★ 이미 출발할 때 에어를 쐈으므로 미련 없이 즉시 종료!

                timeout_cnt += 1
                if timeout_cnt > 300: # 30초 타임아웃
                    print("[WARN] 홈 이동 대기 타임아웃!")
                    break
                    
                time.sleep(0.1)

            # (백업 로직 및 후처리)
            if not overlap_triggered and job_final_status != "ERROR":
                print("[INFO] 홈 도착 완료. (Waypoint 미감지) 설비 초기화 뒤늦게 진입.")
                has_next = False
                self.ready_to_cross_jig = False
                self.overlap_in_progress = False

                if getattr(self, 'waiting_cell', None) is not None:
                    next_cell = self.waiting_cell
                    self.waiting_cell = None 
                    has_next = True
                    self.overlap_in_progress = True
                    self.cross_trigger_cell = next_cell

                if not self.cleanup_cell(target_cell, var_start,
                                         mdo_jig_adv, mdo_jig_ret, mdi_jig_ret_chk, 
                                         mdo_jig_clamp, mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2, has_next=has_next):
                     print(f"[CRITICAL] Cell {target_cell} 설비 초기화 실패")
                     job_final_status = "ERROR"

            self.robot_occupant = 0 

            if job_final_status == "FINISHED" and len(failed_bolts) > 0:
                print(f"[SEQ] 작업은 완료되었으나 NG 발생됨. 부저 3회 알림. 실패 위치: {failed_bolts}")
                if self.io_module is not None:
                    for _ in range(3):
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 1)) 
                        time.sleep(0.3)
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 0)) 
                        time.sleep(0.2)

        except Exception as e:
            print(f"[ERROR] 작업 중단: {e}")
            job_final_status = "ERROR"
            error_msg = str(e) # ★ 에러 메시지 캡처
            try:
                self.set_variable_with_ui(var_start, 0) 
            except Exception as seq_err:
                pass
        
        finally:
            self.robot_occupant = 0 
            self.is_back_working = False
            
            c_t = time.time() - start_t # ★ C/T(사이클 타임) 최종 계산
            
            print(f"[SEQ] 시퀀스 종료 (최종상태: {job_final_status}, C/T: {c_t:.1f}s)")

        return job_final_status, failed_bolts, error_msg, c_t
    
    # ====================================================================
    # [SECTION 3] Job Head Sub-Tasks
    # ====================================================================
    def sequence_jig_head_ready(self, target_cell_num, mdo_jig_clamp, mdi_jig_clamp_chk_1, mdi_jig_clamp_chk_2, 
                                mdo_jig_cyl_adv, mdo_jig_cyl_ret, mdi_jig_adv_chk, 
                                mdo_sensor_reset, mdo_sensor_mute_a, mdo_sensor_mute_b, mdi_product_detect_sen, mdi_product_pos_sen):
        print(f"[SEQ] Cell {target_cell_num} 지그 고정 시퀀스 시작")
        self.wait_check()
        
        inputs = self.io_module.Read_Input_Data()
        
        if inputs[mdi_product_detect_sen] == 0:
            msg = f"Cell {target_cell_num} 제품이 감지되지 않았습니다."
            print(f"[ERROR] {msg}")
            self.report_alarm_signal.emit(msg, "WARN")
            return False
            
        if inputs[mdi_product_pos_sen] == 1:
            msg = f"Cell {target_cell_num} 제품 안착 위치가 불량합니다."
            print(f"[ERROR] {msg}")
            self.report_alarm_signal.emit(msg, "WARN")
            return False
            
        # 1. Clamp
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_clamp, 1))
        
        if not self.wait_for_input(mdi_jig_clamp_chk_1, 1, 3.0, "클램프 센서 1"): return False
        if not self.wait_for_input(mdi_jig_clamp_chk_2, 1, 3.0, "클램프 센서 2"): return False

        self.ready_to_cross_jig = True

        if getattr(self, 'overlap_in_progress', False):
            print(f"[SEQ] 웨이포인트 오버랩 진입 모드! 홈 도착 검사 생략 후 즉시 교차 전진.")
            self.overlap_in_progress = False 
        else:
            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() == "true" or is_home_val == 1 or is_home_val is True)
            
            if not (is_home_true and self.robot_at_home()):
                msg = f"로봇이 홈 위치에 없어 Cell {target_cell_num} 지그를 전진할 수 없습니다. (인터락)"
                print(f"[INTERLOCK] {msg}")
                self.report_alarm_signal.emit(msg, "ERROR")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_clamp, 0))
                return False

        # 2. Advance (독립 플래그 Mute 적용)
        if target_cell_num == 1: self.is_jig_moving_1 = True
        elif target_cell_num == 2: self.is_jig_moving_2 = True
        
        try:
            self.force_sync_mute()
            
            time.sleep(0.3) 
            
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_cyl_ret, 0))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_cyl_adv, 1))
            
            if not self.wait_for_input(mdi_jig_adv_chk, 1, 5.0, "지그 전진"):
                return False
            
            time.sleep(0.2) 
            
            print(f"[SEQ] Cell {target_cell_num} 지그 고정 완료")
            return True
            
        finally:
            # 에러/타임아웃으로 중단되어도 무조건 자기 셀의 플래그만 안전하게 해제
            if target_cell_num == 1: self.is_jig_moving_1 = False
            elif target_cell_num == 2: self.is_jig_moving_2 = False

    def cleanup_cell(self, target_cell_num, var_start,
                     mdo_jig_cyl_adv, mdo_jig_cyl_ret, mdi_jig_ret_chk, 
                     mdo_jig_clamp, mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2, has_next=False):
        print(f"[SEQ] Cell {target_cell_num} 복귀 및 초기화")
        
        self.set_variable_with_ui(var_start, 0)

        # 독립 플래그 ON
        if target_cell_num == 1: self.is_jig_moving_1 = True
        elif target_cell_num == 2: self.is_jig_moving_2 = True
        
        try:
            self.force_sync_mute()
            
            time.sleep(0.3)
            
            # 지그 후진 및 언클램프 명령 전송
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_cyl_adv, 0))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_cyl_ret, 1))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_clamp, 0))
            
            start_t = time.time()
            all_ret = False
            while time.time() - start_t < 5.0:
                self.wait_check()
                inp = self.io_module.Read_Input_Data()
                if inp[mdi_jig_ret_chk] == 1 and inp[mdi_jig_unclamp_chk_1] == 1 and inp[mdi_jig_unclamp_chk_2] == 1:
                    all_ret = True
                    break
                time.sleep(0.1)
                
            time.sleep(0.2)
            
            if all_ret:
                if target_cell_num == 1: self.wait_unload_1 = True
                elif target_cell_num == 2: self.wait_unload_2 = True
                
            if not all_ret:
                print(f"[ERROR] Cell {target_cell_num} 복귀 센서 확인 실패")
                return False
                
            return True
            
        finally:
            # 안전하게 플래그 OFF 보장
            if target_cell_num == 1: self.is_jig_moving_1 = False
            elif target_cell_num == 2: self.is_jig_moving_2 = False

    # ====================================================================
    # [SECTION 4] Job Back (Main)
    # ====================================================================
    def job_command_back(self):
        print(f"[SEQ] BACK 작업 시퀀스 시작")
        
        start_t = time.time() # ★ C/T 측정 시작!
        error_msg = "-"       # ★ 에러 메시지 초기화
        
        # [하드웨어 IO]
        mdo_rot_work, mdo_rot_home = 0, 1
        mdo_jig_left_adv = 2
        mdo_jig_right_adv = 6
        mdo_jig_anti_rot = 10
        mdo_jig_clamp = 12
        
        mdi_rot_work_chk, mdi_rot_home_chk = 0, 1
        mdi_jig_left_1_adv, mdi_jig_left_1_ret = 2, 3
        mdi_jig_left_2_adv, mdi_jig_left_2_ret = 4, 5
        mdi_jig_right_1_adv, mdi_jig_right_1_ret = 6, 7
        mdi_jig_right_2_adv, mdi_jig_right_2_ret = 8, 9
        mdi_jig_anti_rot_adv, mdi_jig_anti_rot_ret = 10, 11
        mdi_jig_clamp_chk_1, mdi_jig_unclamp_chk_1 = 12, 13
        mdi_jig_clamp_chk_2, mdi_jig_unclamp_chk_2 = 14, 15
        
        mdi_prod_detect_1, mdi_prod_detect_2 = 16, 17

        mdo_nr_start = 32
        mdo_nr_cyl_run = 39     
        mdo_nr_air_run = 40     
        mdo_sfd_start = 43
        
        mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err = 32, 33, 34, 35
        mdi_nr_up_chk = 39
        mdi_nr_down_chk = 40    
        mdi_sfd_bolt_detect = 41
        mdi_sfd_ready, mdi_sfd_err = 43, 44

        # [로봇 변수명 통합 및 정리]
        var_start = "Work_start"
        var_working = "Working"
        var_rot_done = "Rot_done"
        var_done_1 = "Work_1_done"
        var_done_2 = "Work_2_done"
        
        var_screw_call = "Screw_call"
        var_screw_done = "Screw_done"
        var_screw_working = "Screw_working"

        # 2. [지그 고정]
        if not self.sequence_jig_back_ready(mdi_prod_detect_1, mdi_prod_detect_2,
                                            mdo_jig_anti_rot, mdi_jig_anti_rot_adv,
                                            mdo_jig_left_adv, mdo_jig_right_adv,
                                            mdi_jig_left_1_adv, mdi_jig_left_2_adv,
                                            mdi_jig_right_1_adv, mdi_jig_right_2_adv):
            # ★ [PATCH] job_back은 4개 값을 언패킹하므로 2개 반환 시 ValueError 발생
            return "ERROR", [], "BACK 지그 고정 실패 (제품/지그/인터락 확인)", time.time() - start_t

        # -------------------------------------------------------------
        # 3. [로봇 권한 획득] 
        # -------------------------------------------------------------
        print("[SEQ] BACK 로봇 Ready 대기 중...")
        while True:
            self.wait_check()
            if self.is_alarm_state() or not self.auto_mode: 
                self.cleanup_back(mdo_jig_left_adv, mdo_jig_right_adv, mdo_jig_anti_rot, mdo_jig_clamp,
                                  mdo_rot_work, mdo_rot_home, 
                                  mdi_jig_left_1_ret, mdi_jig_left_2_ret, 
                                  mdi_jig_right_1_ret, mdi_jig_right_2_ret, 
                                  mdi_jig_anti_rot_ret, mdi_rot_home_chk,
                                  mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2)
                return "ERROR", [], "로봇 권한 대기 중 알람/오토 해제", time.time() - start_t

            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() == "true" or is_home_val == 1 or is_home_val is True)

            if self.robot_occupant == 0 and is_home_true and self.robot_at_home():
                self.robot_occupant = 1 
                print("[SEQ] 로봇 권한 획득")
                break
            time.sleep(0.2)
        
        # 3.5 작업 시작 명령 전 무조건 홈 위치로 명령 전송 및 확인
        self.move_robot_to_home_sync()
        job_final_status = "FINISHED"
        failed_bolts = []

        # ★ [수정] 시작할 때는 팁에 볼트가 없다고 명시 (선행 공급 로직 삭제됨)
        self.is_bolt_loaded = False 
        self._sfd_sync_mark()   # ★ [PATCH] 작업 전(배출/수동 조작) 펄스는 제외하고 새로 카운트

        try:
            # (1) Work 1 (전면) - 선행 공급 코드 삭제됨
            print("[SEQ] Work 1 시작")
            
            self.set_variable_with_ui(var_start, 1)
            start_sync_t = time.time()
            while True:
                self.wait_check()
                is_working = self.get_variable_with_ui(var_working)
                if str(is_working).lower() in ["1", "true"]:
                    print(f"[SYNC] 로봇 작업 시작 확인. 요청 신호 리셋.")
                    self.set_variable_with_ui(var_start, 0)
                    break
                if time.time() - start_sync_t > 5.0:
                    self.set_variable_with_ui(var_start, 0)
                    raise Exception("로봇 작업 시작 응답 없음 (Timeout)")
                time.sleep(0.1)

            loop_success, failed_bolts_1, last_idx = self.process_tightening_loop("Work1", var_done_1, var_screw_call, 
                                                var_working, var_screw_working, var_screw_done,
                                                mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk, 
                                                mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err,
                                                mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err,
                                                start_bolt_idx=0)
            if not loop_success:
                raise Exception("Work 1 실패")
            
            print(f"[SEQ] Work 1 완료 (마지막 볼트: {last_idx}번). 회전 공정 진입.")

            # (2) Rotation (회전)
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_rot_work, 1))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_rot_home, 0))
            
            if not self.wait_for_input(mdi_rot_work_chk, 1, 5.0, "JIG 회전"):
                raise Exception("JIG 회전 타임아웃")
            time.sleep(0.5)
            
            print("[SEQ] 회전 완료. 그리퍼 클램프 시작.")

            # (3) Gripper Clamp
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_clamp, 1))
            if not self.wait_for_input(mdi_jig_clamp_chk_1, 1, 3.0, "그리퍼 1 클램프"): 
                raise Exception("그리퍼 1 클램프 실패")
            if not self.wait_for_input(mdi_jig_clamp_chk_2, 1, 3.0, "그리퍼 2 클램프"): 
                raise Exception("그리퍼 2 클램프 실패")

            print("[SEQ] 그리퍼 클램프 완료. 로봇에 회전 완료 신호(Rot_done=1) 전송.")
            
            self.set_variable_with_ui(var_rot_done, 1)

            # 로봇이 Rot_done=1을 받고 Work_1_done을 0으로 내릴 때까지 대기
            print(f"[SYNC] 로봇이 다음 작업을 위해 {var_done_1}을 0으로 초기화할 때까지 대기...")
            wait_sync_t = time.time()
            while True:
                self.wait_check()
                val = self.get_variable_with_ui(var_done_1)
                
                if str(val).lower() in ["0", "false", "none", ""]:
                    print(f"[SYNC] 로봇 {var_done_1}=0 확인됨. {var_rot_done}=0 리셋 및 Work 2 진입.")
                    self.set_variable_with_ui(var_rot_done, 0)
                    break
                    
                if time.time() - wait_sync_t > 15.0: # 넉넉하게 15초 타임아웃
                    raise Exception(f"로봇 {var_done_1} 초기화 응답 타임아웃")
                    
                time.sleep(0.1)

            # (4) Work 2 (후면)
            print("[SEQ] Work 2 시작.")

            loop_success, failed_bolts_2, _ = self.process_tightening_loop("Work2", var_done_2, var_screw_call, 
                                                var_working, var_screw_working, var_screw_done,
                                                mdo_nr_cyl_run, mdo_nr_start, mdo_nr_air_run, mdi_nr_down_chk, mdi_nr_up_chk, 
                                                mdi_nr_ok, mdi_nr_run, mdi_nr_ng, mdi_nr_err,
                                                mdo_sfd_start, mdi_sfd_bolt_detect, mdi_sfd_ready, mdi_sfd_err,
                                                start_bolt_idx=last_idx)
            if not loop_success:
                raise Exception("Work 2 실패")

            # 전면/후면 불량 리스트 통합
            failed_bolts = failed_bolts_1 + failed_bolts_2
            
            # -------------------------------------------------------------
            # ★ [최적화] 사이클 타임 극단축 (거리 연산 삭제, 0.5초 펄스 트리거)
            # -------------------------------------------------------------
            print("[SEQ] Work 2 체결 완료. 로봇 홈 복귀 및 회피 감시 시작...")
            
            self.set_variable_with_ui(var_start, 0)
            self.set_variable_with_ui(var_rot_done, 0)
            
            # =======================================================
            # ★ [타이밍 수정] 로봇이 홈으로 출발하기 위해 위로 상승하는 즉시 허공에 대고 블로우!
            # =======================================================
            if getattr(self, 'blow_time', 0.0) > 0 and self.io_module is not None:
                print(f"[CMD] NR 볼트 파기 (에어 블로우) 조기 비동기 작동: {self.blow_time}초")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(41, 1))
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 1))
                
                def stop_blow():
                    if self.io_module is not None:
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(41, 0))
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(44, 0))
                
                threading.Timer(self.blow_time, stop_blow).start()
            
            #홈 출발 신호 1 전송
            self.set_variable_with_ui("home_req", 1)
            home_pulse_start_t = time.time()
            home_req_reset_done = False # 0.5초 뒤 0으로 껐는지 확인하는 깃발

            overlap_triggered = False
            timeout_cnt = 0
            waypoint_name = "J_jig_waypoint"

            while True:
                self.wait_check()
                
                if not home_req_reset_done and (time.time() - home_pulse_start_t >= 0.5):
                    self.set_variable_with_ui("home_req", 0)
                    home_req_reset_done = True
                
                is_home_val = self.get_variable_with_ui("is_home")
                is_home_true = (str(is_home_val).lower() in ["true", "1"] or is_home_val == 1 or is_home_val is True)

                # [1] 지그 해제(Cleanup) 시작: 웨이포인트 도달 시!
                if not overlap_triggered:
                    if self.robot_at_waypoint(waypoint_name, tolerance=0.15) or is_home_true:
                        print(f"[INFO] 🚀 로봇 {waypoint_name} 도달! 즉시 지그 해제(Cleanup) 시작!")
                        overlap_triggered = True
                        
                        if not self.cleanup_back(mdo_jig_left_adv, mdo_jig_right_adv, mdo_jig_anti_rot, mdo_jig_clamp,
                                                 mdo_rot_work, mdo_rot_home, 
                                                 mdi_jig_left_1_ret, mdi_jig_left_2_ret, 
                                                 mdi_jig_right_1_ret, mdi_jig_right_2_ret, 
                                                 mdi_jig_anti_rot_ret, mdi_rot_home_chk,
                                                 mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2):
                             print("[CRITICAL] BACK 설비 초기화 실패")
                             job_final_status = "ERROR"
                             break

                # [2] 최종 종료: 모든 조건 완벽 충족 시 탈출
                if overlap_triggered and (is_home_true or self.robot_at_home()):
                    print("[SUCCESS] 로봇 물리적 홈 도착 및 스레드 정상 종료 조건 완벽 충족.")
                    break # ★ 이미 출발할 때 에어를 쐈으므로 미련 없이 즉시 종료!

                timeout_cnt += 1
                if timeout_cnt > 300: # 30초 타임아웃
                    print("[WARN] 홈 이동 대기 타임아웃!")
                    break
                    
                time.sleep(0.1)

            if not overlap_triggered and job_final_status != "ERROR":
                print("[INFO] 홈 도착 완료. (Waypoint 미감지) 설비 초기화 뒤늦게 진입.")
                if not self.cleanup_back(mdo_jig_left_adv, mdo_jig_right_adv, mdo_jig_anti_rot, mdo_jig_clamp,
                                         mdo_rot_work, mdo_rot_home, 
                                         mdi_jig_left_1_ret, mdi_jig_left_2_ret, 
                                         mdi_jig_right_1_ret, mdi_jig_right_2_ret, 
                                         mdi_jig_anti_rot_ret, mdi_rot_home_chk,
                                         mdi_jig_unclamp_chk_1, mdi_jig_unclamp_chk_2):
                     print("[CRITICAL] BACK 설비 초기화 실패")
                     job_final_status = "ERROR"

            self.robot_occupant = 0 

            if job_final_status == "FINISHED" and len(failed_bolts) > 0:
                print(f"[SEQ] 작업은 완료되었으나 NG 발생됨. 부저 3회 알림. 실패 위치: {failed_bolts}")
                if self.io_module is not None:
                    for _ in range(3):
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 1)) 
                        time.sleep(0.3)
                        self.io_module.Send_que.put(self.io_module.Write_DO_Data(31, 0)) 
                        time.sleep(0.2)
                 
        except Exception as e:
            print(f"[ERROR] 작업 중단: {e}")
            job_final_status = "ERROR"
            error_msg = str(e) # ★ 에러 메시지 캡처
            try:
                self.set_variable_with_ui(var_start, 0) 
            except Exception as seq_err:
                pass
        
        finally:
            self.robot_occupant = 0 
            self.is_back_working = False
            
            c_t = time.time() - start_t # ★ C/T(사이클 타임) 최종 계산
            
            print(f"[SEQ] 시퀀스 종료 (최종상태: {job_final_status}, C/T: {c_t:.1f}s)")

        return job_final_status, failed_bolts, error_msg, c_t

    # ====================================================================
    # [SECTION 5] Job Back Sub-Tasks
    # ====================================================================
    def sequence_jig_back_ready(self, mdi_prod_1, mdi_prod_2, 
                                mdo_jig_anti_rot, mdi_jig_anti_rot_chk, 
                                mdo_jig_left, mdo_jig_right, 
                                mdi_l1_chk, mdi_l2_chk, mdi_r1_chk, mdi_r2_chk):
        
        print("[SEQ] BACK 지그 고정 시작 (좌우 밀착 및 회전 방지)")
        self.wait_check()
        
        # 0. 제품 감지
        inputs = self.io_module.Read_Input_Data()
        
        if inputs[mdi_prod_1] == 0 or inputs[mdi_prod_2] == 0:
            msg = "BACK 작업 영역에 제품이 정상적으로 감지되지 않았습니다."
            print(f"[ERROR] {msg}")
            self.report_alarm_signal.emit(msg, "WARN")
            return False
            
        # [자동 모드 인터락 - 타입 검사 완벽 대응]
        is_home_val = self.get_variable_with_ui("is_home")
        is_home_true = (str(is_home_val).lower() in ["true", "1"]) or (is_home_val == 1) or (is_home_val is True)
        
        if not (is_home_true and self.robot_at_home()):
            msg = "로봇이 홈 위치에 없어 BACK 지그를 전진할 수 없습니다. (인터락)"
            print(f"[INTERLOCK] {msg}")
            self.report_alarm_signal.emit(msg, "ERROR")
            return False
            
        # 1. 회전 방지 (단동: ON)
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_anti_rot, 1))
        
        if not self.wait_for_input(mdi_jig_anti_rot_chk, 1, 3.0, "회전 방지 전진"):
            return False

        # 2. 좌/우 밀착 (단동: ON)
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_left, 1))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_right, 1))
        
        # 4개 센서 동시 확인
        start_t = time.time()
        success = False
        while time.time() - start_t < 5.0:
            self.wait_check()
            inp = self.io_module.Read_Input_Data()
            if (inp[mdi_l1_chk]==1 and inp[mdi_l2_chk]==1 and 
                inp[mdi_r1_chk]==1 and inp[mdi_r2_chk]==1):
                success = True
                break
            time.sleep(0.1)
            
        if not success:
            print("[ERROR] 좌/우 밀착 확인 실패")
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_left, 0))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_right, 0))
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_anti_rot, 0))
            return False
            
        print("[SEQ] BACK 1차 지그 고정 완료 (그리퍼 제외)")
        return True

    def cleanup_back(self, mdo_jig_left, mdo_jig_right, mdo_jig_anti_rot, mdo_jig_clamp, mdo_rot_work, mdo_rot_home, 
                     mdi_l1_ret, mdi_l2_ret, mdi_r1_ret, mdi_r2_ret, mdi_anti_ret, mdi_rot_home_chk,
                     mdi_unclamp_1_chk, mdi_unclamp_2_chk):
        
        print("[SEQ] BACK 설비 순차 초기화 시작")
        
        self.set_variable_with_ui("Work_start", 0)
        self.robot_occupant = 0

        # [STEP 1] 그리퍼 언클램프
        print("[SEQ] 1단계: 그리퍼 언클램프")
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_clamp, 0))
        
        if not self.wait_for_input(mdi_unclamp_1_chk, 1, 5.0, "그리퍼 1 언클램프"): 
            print("[ERROR] 그리퍼 1 언클램프 실패")
            return False
        if not self.wait_for_input(mdi_unclamp_2_chk, 1, 5.0, "그리퍼 2 언클램프"): 
            print("[ERROR] 그리퍼 2 언클램프 실패")
            return False

        # [STEP 2] JIG 회전 원복
        self.seq_mute_active = True
        
        print("[SEQ] 2단계: JIG 회전 원복")
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_rot_work, 0))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_rot_home, 1))
        
        if not self.wait_for_input(mdi_rot_home_chk, 1, 5.0, "회전 원복"):
            print("[ERROR] 회전 원복 실패")
            return False

        # [STEP 3] 좌/우/회전방지 지그 후진
        print("[SEQ] 3단계: 좌/우 및 회전방지 지그 후진")
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_left, 0))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_right, 0))
        self.io_module.Send_que.put(self.io_module.Write_DO_Data(mdo_jig_anti_rot, 0))
        
        start_t = time.time()
        all_ret = False
        while time.time() - start_t < 5.0:
            self.wait_check()
            inp = self.io_module.Read_Input_Data()
            if (inp[mdi_l1_ret] == 1 and inp[mdi_l2_ret] == 1 and 
                inp[mdi_r1_ret] == 1 and inp[mdi_r2_ret] == 1 and 
                inp[mdi_anti_ret] == 1):
                all_ret = True
                break
            time.sleep(0.1)
            
        if all_ret:
            print("[SEQ] 설비 복귀 완료")
            return True
        else:
            print("[ERROR] 좌/우/회전방지 지그 복귀 센서 확인 실패")
            return False
        
    def move_robot_to_home_sync(self):
        """작업 시작 전 로봇을 무조건 홈 위치로 보내는 함수 (확실한 0.5초 펄스 사용)"""
        print("[CMD] 작업 시작 전 로봇 홈 위치 강제 동기화 (home_req = 1 펄스)")
        
        if self.robot_at_home():
            print("[INFO] 이미 홈 위치에 있습니다. (이동 생략, home_req = 0 강제 전송)")
            self.set_variable_with_ui("home_req", 0)
            return
            
        self.set_variable_with_ui("home_req", 1)
        time.sleep(0.5) 
        self.set_variable_with_ui("home_req", 0)
        
        timeout_cnt = 0
        
        while True:
            if not self.pause_event.is_set(): self.pause_event.wait()
            if QThread.currentThread().isInterruptionRequested(): raise Exception("프로그램 종료 요청")
            
            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() in ["true", "1"]) or (is_home_val == 1) or (is_home_val is True)
            
            if is_home_true or self.robot_at_home():
                print("[INFO] 로봇 홈 위치 도착 확인 완료.")
                break
                
            timeout_cnt += 1
            if timeout_cnt > 100: # 10초 타임아웃
                print("[WARN] 홈 이동 대기 타임아웃!")
                break
                
            time.sleep(0.1)
    
    def robot_at_home(self) -> bool:
        try:
            current_joints = [
                self.actual_joint_base,
                self.actual_joint_shoulder,
                self.actual_joint_elbow,
                self.actual_joint_wrist1,
                self.actual_joint_wrist2,
                self.actual_joint_wrist3
            ]
            
            home_joints = self.robot_29999.get_variable_cached("J_home")   # ★ [PATCH] 캐시
            
            if not isinstance(home_joints, list):
                print(f"[ERROR] J_home 변수를 가져올 수 없습니다. (수신값: {home_joints})") 
                return False

            if len(home_joints) != 6:
                return False

            tolerance = 0.05  
            
            for curr, home in zip(current_joints, home_joints):
                if abs(curr - home) > tolerance:
                    # print(f"[Check] 관절 위치 불일치 (Diff: {abs(curr - home):.4f})")
                    return False
            
            is_home_val = self.get_variable_with_ui("is_home")
            
            if str(is_home_val).lower() not in ("true", "1"):   # ★ [PATCH] 1도 허용 (다른 곳과 판정 통일)
                # print(f"[Check] is_home 변수가 True가 아님: {is_home_val}")
                return False

            return True

        except Exception as e:
            print(f"[ERROR] 홈 위치 확인 실패: {e}")
            return False
             
    def _alarm_action(self, status):
        self.lamp_control(status)
        
        if len(status) > 2 and status[2] == "ON":
            self.buzzer_control(["ON"])
        else:
            self.buzzer_control(["OFF"])
    
    @pyqtSlot(int, str, list, str, float)
    def on_job_finished(self, cell_num, status, failed_bolts, error_msg, c_t):
        """작업 완료 시 호출 -> UI 기록 추가 및 대기열 처리"""
        self.current_working_cell = 0 
        
        # ========================================================
        # ★ 1. 작업 완료 기록 UI 및 CSV 누적 저장!
        # ========================================================
        self.add_history_record(cell_num, status, failed_bolts, error_msg, c_t)
        
        # 2. 정상적으로 완료되었을 때 (OK든 NG든 사이클을 마쳤을 때)
        if status == "FINISHED":
            if self.active_popups.get(cell_num) is not None:
                try: self.active_popups[cell_num].close()
                except: pass

            if failed_bolts:
                dlg = JobNGDialog(self, cell_num, failed_bolts)
                dlg.closed.connect(self.on_popup_closed)
                dlg.show()
                self.active_popups[cell_num] = dlg
            else:
                dlg = JobResultDialog(self, cell_num)
                dlg.closed.connect(self.on_popup_closed)
                dlg.show()
                self.active_popups[cell_num] = dlg
            
            self.seq_mute_active = False 

            if self.waiting_cell is not None:
                next_cell = self.waiting_cell
                self.waiting_cell = None 
                QtCore.QTimer.singleShot(0, lambda: self.start_next_job_from_queue(next_cell))
                
        # 3. 에러 또는 강제 중단으로 끝났을 때
        else:
            self.active_popups[cell_num] = None 
            cell_name = "BACK" if cell_num == 0 else f"Cell {cell_num}"
            
            # 여기서 발송되는 알람 로그가 우측 하단 에러 리스트에도 쌓입니다.
            alarm_str = f"[{cell_name}] 작업 비정상 종료 (사유: {error_msg})"
            if getattr(self, '_user_stop_requested', False):
                # ★ [PATCH] 정지 버튼/알람 리셋으로 인한 협조적 종료는 알람을 다시 띄우지 않음
                print(f"[INFO] {alarm_str} - 사용자 정지/리셋에 의한 종료")
            elif not self.is_alarm_state() and not getattr(self, '_is_software_error', False):
                self.report_alarm_signal.emit(alarm_str, "ERROR")
            
            if self.waiting_cell is not None:
                self.waiting_cell = None
            if getattr(self, '_post_init_cell', None) is not None:
                self._post_init_cell = None
                
    def init_history_file_path(self):
        """[부팅 시 사용] 오늘 날짜의 가장 마지막(최신) CSV 파일을 찾아 이어서 씁니다."""
        now = QDateTime.currentDateTime()
        date_str = now.toString("yyyy-MM-dd")
        month_str = now.toString("yyyy-MM")
        
        base_dir = APP_ROOT / "Logs" / "History" / month_str
        base_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. 기본 파일(순번 없음) 확인
        latest_file = base_dir / f"{date_str}.csv"
        
        # 2. 만약 기본 파일이 있다면, 꼬리표(_1, _2...)가 붙은 최신 파일이 있는지 끝까지 추적
        if latest_file.exists():
            idx = 1
            while True:
                next_file = base_dir / f"{date_str}_{idx}.csv"
                if next_file.exists():
                    latest_file = next_file # 존재하는 가장 큰 번호로 계속 덮어씀
                    idx += 1
                else:
                    break # 더 이상 번호가 없으면 루프 종료
                    
        self.current_history_csv_path = latest_file
        print(f"[SYSTEM] 작업 이력 기록 파일 연결 (이어서 쓰기): {self.current_history_csv_path.name}")

    def roll_history_file_path(self):
        """[리셋/자정 시 사용] 기존 기록을 보존하고 새로운 순번의 CSV 파일을 즉시 생성합니다."""
        now = QDateTime.currentDateTime()
        date_str = now.toString("yyyy-MM-dd")
        month_str = now.toString("yyyy-MM")
        
        base_dir = APP_ROOT / "Logs" / "History" / month_str
        base_dir.mkdir(parents=True, exist_ok=True)
        
        base_file = base_dir / f"{date_str}.csv"
        
        # 1. 오늘 처음 만드는 거라면 순번 없이 깔끔하게 생성
        if not base_file.exists():
            self.current_history_csv_path = base_file
        else:
            # 2. 이미 파일이 존재하면 비어있는 다음 순번(_1, _2...)을 찾아서 할당
            idx = 1
            while True:
                next_file = base_dir / f"{date_str}_{idx}.csv"
                if not next_file.exists():
                    self.current_history_csv_path = next_file
                    break
                idx += 1
                
        # =================================================================
        # ★ [추가] 경로만 지정하는 게 아니라, 실제로 CSV 빈 파일을 즉시 생성!
        # 작업 내역이 하나도 없더라도 리셋을 누르면 헤더가 포함된 파일이 만들어집니다.
        # =================================================================
        try:
            # 'w' 모드로 열어서 즉시 파일을 만들고 머리말(헤더)을 기록해 둡니다.
            with open(self.current_history_csv_path, "w", newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(["시간", "C/T", "작업 위치", "제품", "판정", "불량 위치", "상세 사유"])
                
            print(f"[SYSTEM] 작업 이력 기록 파일 갱신 및 물리적 파일 생성 완료: {self.current_history_csv_path.name}")
        except Exception as e:
            print(f"[ERROR] 빈 이력 파일 생성 실패: {e}")

    # =========================================================================
    # ★ [추가] 테이블 데이터 추가 및 CSV 저장 (월별 폴더 / 일별 파일 자동화)
    # =========================================================================
    def add_history_record(self, cell_num, status, failed_bolts, error_msg, c_t):
        now = QDateTime.currentDateTime()
        time_str = now.toString("HH:mm:ss")
        
        # [1] 작업 위치
        pos_map = {0: "백커버", 1: "헤드/왼쪽(셀1)", 2: "헤드/오른쪽(셀2)"}
        pos_str = pos_map.get(cell_num, "알수없음")
        
        # [2] 제품명 파싱
        prod_idx = getattr(self, 'current_product_type', 0)
        config_name = f"{self.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name
        prod_str = f"{prod_idx}번 제품"
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    prod_str = config_data.get("CURRENT_PRODUCT", {}).get(str(prod_idx), prod_str)
            except: pass
        
        # [3] C/T
        ct_str = f"{c_t:.1f}s"
        
        # [4] 판정 / 불량위치 / 상세사유 분석
        if status == "FINISHED" and len(failed_bolts) == 0:
            judge_str = "OK"
            ng_pos_str = "-"
            reason_str = "-"
            color = QColor("#28a745") # 쨍한 초록색
            
        elif status == "FINISHED" and len(failed_bolts) > 0:
            judge_str = "NG"
            ng_positions = []
            ng_reasons = []
            # failed_bolts 예시: "1 (1차 토크 미달)"
            for fb in failed_bolts:
                if "(" in fb and ")" in fb:
                    parts = fb.split("(", 1)   # ★ [PATCH] 사유에 괄호가 있어도 잘리지 않도록
                    ng_positions.append(parts[0].strip() + "번")
                    ng_reasons.append(parts[1].rsplit(")", 1)[0].strip())
                else:
                    ng_positions.append(fb)
                    ng_reasons.append("체결 불량")
            
            ng_pos_str = ", ".join(ng_positions)
            reason_str = ", ".join(ng_reasons)
            color = QColor("#FFC107") # FFC107
            
        else: # ERROR
            judge_str = "ERROR"
            ng_pos_str = "-"
            reason_str = error_msg if error_msg and error_msg != "-" else "시스템/작업 중단"
            color = QColor("#FF3333") # FF3333
        
        # [5] UI QTableWidget에 행 추가 (최신 데이터를 0번 줄에 꽂아 넣기)
        table = self.ui.history_table
        table.insertRow(0)
        table.setRowHeight(0, 45) # 넉넉한 높이
        
        row_data = [time_str, ct_str, pos_str, prod_str, judge_str, ng_pos_str, reason_str]
        for col, text in enumerate(row_data):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            
            # 판정 열(5번째 칸) 색상 및 볼드 처리
            if col == 4: 
                item.setForeground(QBrush(color))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                
            table.setItem(0, col, item)
            
        # 최대 1000줄까지만 테이블 유지 (메모리 방어)
        if table.rowCount() > 1000:
            table.removeRow(1000)
            
        # ====================================================================
        # [6] CSV 파일 역순 저장 (가장 최신 데이터가 엑셀의 헤더 바로 밑으로 오게 밀어내기)
        # ====================================================================
        if not hasattr(self, 'current_history_csv_path') or self.current_history_csv_path is None:
            # 혹시라도 경로가 지정 안 되어 있으면 부팅 경로 한 번 찔러줌
            self.init_history_file_path()

        file_exists = self.current_history_csv_path.exists()
        existing_rows = []
        
        # 1단계: 기존 파일의 내용을 전부 읽어옵니다.
        if file_exists:
            try:
                with open(self.current_history_csv_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.reader(f)
                    existing_rows = list(reader)
            except Exception as e:
                print(f"[WARN] 기존 CSV 읽기 실패 (덮어씁니다): {e}")

        # 2단계: 새 파일로 덮어쓰면서 (헤더 -> 방금 한 작업 -> 옛날 작업) 순으로 씁니다.
        try:
            # 'a'(Append)가 아니라 'w'(Write) 모드로 엽니다.
            with open(self.current_history_csv_path, "w", newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                
                if existing_rows and len(existing_rows) > 0:
                    # 원래 있던 표의 머리말(헤더) 쓰기
                    writer.writerow(existing_rows[0]) 
                    old_data = existing_rows[1:]
                else:
                    # 파일이 처음 만들어진 경우 새 헤더 만들기
                    writer.writerow(["시간", "C/T", "작업 위치", "제품", "판정", "불량 위치", "상세 사유"])
                    old_data = []
                    
                # ★ 방금 끝난 최신 데이터를 두 번째 줄(헤더 바로 밑)에 삽입!
                writer.writerow(row_data)
                
                # ★ 그 밑으로 옛날 데이터들을 차례대로 밀어내며 저장
                writer.writerows(old_data)
                
        except Exception as e:
            print(f"[ERROR] CSV 이력 역순 저장 실패: {e}")
            
    def open_history_folder(self):
        """이전 기록 보기 버튼 클릭 시 CSV 저장 폴더를 탐색기로 엽니다."""
        base_dir = APP_ROOT / "DB" / "history"
        base_dir.mkdir(parents=True, exist_ok=True)
        
        try:
            if platform.system() == "Windows":
                os.startfile(str(base_dir))
            else:
                subprocess.Popen(["xdg-open", str(base_dir)])
        except Exception as e:
            self.show_message("폴더 열기 실패", f"폴더를 열 수 없습니다.\n{e}", QMessageBox.Warning)

    def start_next_job_from_queue(self, cell_num):
        """대기열 작업 실행"""
        if self.active_popups.get(cell_num) is None:
            self.start_job(cell_num)
        else:
            print(f"[SKIP] Cell {cell_num} 대기 작업 취소 (팝업 미확인)")

    def on_popup_closed(self, cell_num):
        """팝업이 닫혔을 때 호출됨"""
        print(f"[UI] Cell {cell_num} 팝업 닫힘 확인")
        self.blink_tower.stop_blink()
        self.active_popups[cell_num] = None
    
    def init_io_ui(self):
        """
        Qt Designer(.ui)에 생성된 MDI_xx, MDO_xx 위젯을 찾아 리스트로 연결합니다.
        """
        self.mdi_lamps = [] # MDI 위젯 리스트
        self.mdo_btns = []  # MDO 버튼 리스트

        # 00 ~ 47번 위젯 찾기 (규칙: MDI_00, MDO_00)
        for i in range(48):
            # 1. MDI 위젯 찾기 (QLabel)
            mdi_name = f"MDI_{i:02d}"
            if hasattr(self.ui, mdi_name):
                widget = getattr(self.ui, mdi_name)
                self.mdi_lamps.append(widget)
            else:
                self.mdi_lamps.append(None) 

            # 2. MDO 버튼 찾기 (QPushButton)
            mdo_name = f"MDO_{i:02d}"
            if hasattr(self.ui, mdo_name):
                btn = getattr(self.ui, mdo_name)
                btn.setCheckable(True) # 토글 설정
                
                # 기존 연결 해제 후 재연결 (중복 방지)
                try: btn.clicked.disconnect() 
                except: pass
                
                btn.clicked.connect(lambda checked, idx=i: self.on_mdo_clicked(idx, checked))
                self.mdo_btns.append(btn)
                
            else:
                self.mdo_btns.append(None)
                
        self.apply_io_labels()

        # UI 업데이트 타이머 재설정
        if hasattr(self, 'io_ui_timer'):
            self.io_ui_timer.stop()
            
        self.io_ui_timer = QtCore.QTimer(self)
        self.io_ui_timer.setInterval(30) # 0.03초 간격
        self.io_ui_timer.timeout.connect(self.update_io_ui_state)
        self.io_ui_timer.start()

    def update_io_ui_state(self):
        """IO 모듈 데이터 -> UI 위젯 색상 업데이트 (다크 모드 스타일 적용)"""
        
        if self.io_module is None:
            return

        # --------------------------------------------------------
        # 1. MDI (Input) 업데이트
        # --------------------------------------------------------
        input_data = self.io_module.Read_Input_Data()
        
        if input_data and len(input_data) >= 48:
            # ============================================================
            # [추가] 대기 구간(Window)일 때만 센서(41번) 상승 펄스를 캡처
            # ============================================================
            if getattr(self, 'is_waiting_for_bolt', False) and input_data[41] == 1:
                self._bolt_detect_latched = True
                
            for i, val in enumerate(input_data):
                lbl = self.mdi_lamps[i]
                if lbl is None: continue

                if val == 1:
                    # [ON 상태] ★ 쫀득한 녹색 LED 스타일 ★
                    # 밝은 녹색에 진한 녹색 그림자(border-bottom)를 줘서 튀어나와 보임
                    lbl.setStyleSheet("""
                        background-color: #28a745;       /* 선명한 LED 초록색 */
                        color: #ffffff;                  /* 흰색 글자 */
                        border: 1px solid #1e7e34;       /* 테두리 */
                        border-bottom: 4px solid #155724;/* ★ 핵심: 두꺼운 바닥 (입체감) */
                        border-radius: 6px;              /* 모서리 둥글게 */
                        font-weight: bold;
                        margin: 5px;                     /* 여백 */
                    """)
                else:
                    # [OFF 상태] ★ 입체적인 꺼진 램프 ★
                    # 밝은 회색으로 착시는 없애되, 입체감은 남겨둠
                    lbl.setStyleSheet("""
                        background-color: #F5F7FA;       /* 아주 밝은 회색 (착시 방지) */
                        color: #AAAAAA;                  /* 글자도 흐리게 (꺼진 느낌) */
                        border: 1px solid #CED4DA;
                        border-bottom: 4px solid #ADB5BD;/* ★ 꺼져있어도 입체감 유지 */
                        border-radius: 6px;
                        font-weight: bold;
                        margin: 5px;
                    """)
        # --------------------------------------------------------
        # 2. MDO (Output) 상태 동기화
        # --------------------------------------------------------
        output_data = self.io_module.Read_Output_Data()
        
        if output_data and len(output_data) >= 48:
            for i, val in enumerate(output_data):
                btn = self.mdo_btns[i]
                if btn is None: continue

                # 버튼 상태 동기화 (사용자 조작 외 로직에 의한 변경 반영)
                btn.blockSignals(True)
                btn.setChecked(val == 1)
                btn.blockSignals(False)
                
                if val == 1:
                    # ON 상태: 파란색 (XML 생성 코드의 Checked 스타일과 매칭)
                    btn.setStyleSheet("""
                        QPushButton {
                            background-color: #0078D7;       /* 파란색 */
                            color: white; 
                            border: 1px solid #005a9e;       /* 테두리 얇게 */
                            border-top: 3px solid #004080;   /* 위쪽 그림자 (눌린 느낌) */
                            border-left: 3px solid #004080;
                            border-radius: 10px; 
                            font-weight: bold;
                            margin: 5px;
                        }
                        QPushButton:hover {
                            background-color: #006CC1;       /* 마우스 올리면 살짝 어두워짐 */
                            border-top: 3px solid #003366;/* 그림자 줄어듦 */
                            border-left: 3px solid #003366;
                            margin-top: 6px;                 /* 살짝 눌리는 애니메이션 */
                            margin-bottom: 4px;
                        }
                        QPushButton:pressed {
                            background-color: #005a9e;
                            border-top: 1px solid #003366;/* 완전히 눌림 (납작해짐) */
                            border-left: 1px solid #003366;
                            margin-top: 8px;                 /* 실제 위치 이동 */
                            margin-bottom: 2px;
                        }
                    """)
                else:
                    # OFF 상태: 다크 그레이 (기본 스타일 복구)
                    btn.setStyleSheet("""
                        QPushButton {
                            background-color: #F5F7FA;       /* 아주 밝은 쿨그레이 (착시 방지) */
                            color: #333333;                  /* 진한 회색 글자 */
                            border: 1px solid #CED4DA;       /* 기본 테두리 */
                            border-bottom: 4px solid #ADB5BD;/* ★ 핵심: 두꺼운 바닥 (입체감) */
                            border-right: 4px solid #ADB5BD;
                            border-radius: 6px;
                            font-weight: bold;
                            margin: 5px;
                        }
                        QPushButton:hover {
                            background-color: #E2E6EA;       /* 마우스 올리면 살짝 어두워짐 */
                            border-bottom: 4px solid #868E96;/* 그림자도 진해짐 */
                            border-right: 4px solid #868E96;
                            margin-top: 6px;                 /* 살짝 눌리는 애니메이션 효과 흉내 */
                            margin-bottom: 4px;
                            border-bottom: 3px solid #868E96;
                        }
                        QPushButton:pressed {
                            background-color: #DDE2E6;
                            border-bottom: 1px solid #ADB5BD;/* 누르면 납작해짐 */
                            border-right: 1px solid #ADB5BD;
                            margin-top: 8px;                 /* 실제로 눌린 위치로 이동 */
                            margin-bottom: 2px;
                        }
                    """)

    def on_mdo_clicked(self, index, checked):
        """MDO 버튼 클릭 이벤트 (안전 로직 및 하드웨어 인터락 포함)"""
        
        btn = self.mdo_btns[index]
        if btn is None: return
        
        if index == 42:
            # 42번은 단순 팝업 버튼이므로 눌려있는 상태(Toggle ON)가 되지 않도록 즉시 원복
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False) 
            
            # 오토 모드 중에는 설정창을 띄우지 않음 (안전 장치)
            if not self.check_auto_mode_restriction():
                return
                
            # 설정 팝업 호출 후 함수 즉시 종료 (모드버스 IO 출력 생략)
            self.show_blow_time_setting()
            return

        # 1. 오토 모드 차단 (안전 장치)
        if not self.check_auto_mode_restriction():
            # 버튼 상태 강제 원복
            btn.blockSignals(True)
            btn.setChecked(not checked)
            btn.blockSignals(False)
            return

        # 2. 연결 확인
        if self.io_module is None:
            print("[IO ERROR] 모듈 미연결")
            btn.blockSignals(True)
            btn.setChecked(not checked)
            btn.blockSignals(False)
            return
        
        # =========================================================================
        # 🚨 [강력 인터락] 모든 지그 출력(0~15번)에 대해 로봇 홈 위치 검사
        # =========================================================================
        # 지그 전진(ON)뿐만 아니라 복귀/후진(OFF) 조작 시에도 로봇이 홈이어야 함
        if index <= 12:
            is_home_val = self.get_variable_with_ui("is_home")
            is_home_true = (str(is_home_val).lower() == "true" or is_home_val == 1 or is_home_val is True)
            
            if not (is_home_true and self.robot_at_home()):
                print(f"[INTERLOCK] 로봇 위치 위험! 지그 조작 차단 (MDO {index})")
                
                self.show_message(
                    "장비 파손 주의 (인터락)", 
                    "<b>로봇이 안전한 홈 위치에 있지 않습니다.</b><br><br>"
                    "지그를 전진하거나 복귀시키기 전에<br>"
                    "반드시 로봇을 [HOME]으로 이동시켜 주세요.", 
                    QMessageBox.Warning
                )
                
                # 버튼 상태 강제 원복 (조작 무효화)
                btn.blockSignals(True)
                btn.setChecked(not checked)
                btn.blockSignals(False)
                return
        # =========================================================================

        # =========================================================================
        # 🚨 [하드웨어 파손 방지 인터락] BACK 모드 회전(MDO 0, 1) 동작 제한 🚨
        # =========================================================================
        if self.system_mode == "BACK" and index in [0, 1]:
            inputs = self.io_module.Read_Input_Data()
            outputs = self.io_module.Read_Output_Data()
            
            # 데이터 통신이 정상일 때만 검사 진행
            if inputs and len(inputs) >= 48 and outputs and len(outputs) >= 48:
                # [조건] MDO 12(출력), MDI 12(센서1), MDI 14(센서2) 중 하나라도 1(ON)이면
                if outputs[12] == 1 or inputs[12] == 1 or inputs[14] == 1:
                    print("[INTERLOCK] 그리퍼 클램프 감지 -> 회전 동작 차단됨!")
                    
                    # 작업자에게 경고창 띄우기
                    self.show_message(
                        "장비 파손 방지 (인터락)", 
                        "<b>그리퍼가 클램프(고정)된 상태</b>에서는<br>회전 동작을 할 수 없습니다.<br><br>반드시 먼저 <b>그리퍼를 언클램프</b> 해주세요.", 
                        QMessageBox.Warning
                    )
                    
                    # 버튼이 눌린 상태를 다시 원래대로 되돌림 (명령 무시)
                    btn.blockSignals(True)
                    btn.setChecked(not checked)
                    btn.blockSignals(False)
                    return
                
        # =========================================================================
        # 🚨 [하드웨어 파손 방지 인터락] HEAD 모드 지그 클램프(MDO 2, 10) 동작 제한 🚨
        # =========================================================================
        if self.system_mode == "HEAD" and index in [2, 10]:
            # [핵심] 사용자가 켜려고 할 때(ON 요청)만 인터락을 검사합니다. (끄는 건 무조건 허용)
            if checked:
                inputs = self.io_module.Read_Input_Data()
                
                # 데이터 통신이 정상일 때만 검사 진행
                if inputs and len(inputs) >= 48:
                    
                    # [지그 1] MDO 2번 클릭 시 -> MDI 6, 7번 확인
                    if index == 2 and (inputs[6] == 1 and inputs[7] == 1):
                        print("[INTERLOCK] 지그 1 제품 위치 불량 -> 클램프(ON) 차단됨!")
                        
                        self.show_message(
                            "장비 파손 방지 (인터락)", 
                            "<b>제품 위치가 불량인 상태</b>에서는<br>Cell 1 고정 동작을 할 수 없습니다.<br>제품을 바르게 안착해 주세요.", 
                            QMessageBox.Warning
                        )
                        
                        # 명령 무시 및 버튼 원복
                        btn.blockSignals(True)
                        btn.setChecked(False)
                        btn.blockSignals(False)
                        return
                        
                    # [지그 2] MDO 10번 클릭 시 -> MDI 14, 15번 확인
                    elif index == 10 and (inputs[14] == 1 and inputs[15] == 1):
                        print("[INTERLOCK] 지그 2 제품 위치 불량 -> 클램프(ON) 차단됨!")
                        
                        self.show_message(
                            "장비 파손 방지 (인터락)", 
                            "<b>제품 위치가 불량인 상태</b>에서는<br>Cell 2 고정 동작을 할 수 없습니다.<br>제품을 바르게 안착해 주세요.", 
                            QMessageBox.Warning
                        )
                        
                        # 명령 무시 및 버튼 원복
                        btn.blockSignals(True)
                        btn.setChecked(False)
                        btn.blockSignals(False)
                        return
                    
        # =========================================================================
        # 🚨 [하드웨어 파손 방지 인터락] HEAD 모드 지그 전진(MDO 0, 8) 로봇 홈 확인 🚨
        # =========================================================================
        if self.system_mode == "HEAD" and index in [0, 8]:
            if checked: # 작업자가 전진(ON) 시키려 할 때만 검사
                is_home_val = self.get_variable_with_ui("is_home")
                is_home_true = (str(is_home_val).lower() == "true" or is_home_val == 1 or is_home_val is True)
                
                # 로봇 변수가 홈이 아니거나, 실제 관절값이 홈이 아니면 차단!
                if not (is_home_true and self.robot_at_home()):
                    print("[INTERLOCK] 로봇 홈 위치 이탈 -> 지그 전진(ON) 차단됨!")
                    
                    self.show_message(
                        "장비 파손 방지 (인터락)", 
                        "<b>로봇이 홈 위치에 없는 상태</b>에서는<br>지그를 전진시킬 수 없습니다.<br>로봇을 먼저 홈으로 이동시켜 주세요.", 
                        QMessageBox.Warning
                    )
                    
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
                    return
        # =========================================================================
        # ★ [수정] HEAD 모드 수동 상태에서 라이트 커튼(24, 25번) 개별/연동 제어
        # =========================================================================
        if index == 24:
            if checked and self.system_mode == "HEAD" and not self.auto_mode:
                print("[MUTE MANUAL] Mute A(24) ON 요청 -> 50ms 후 B(25) 연동 점등")
                # 1. A 먼저 ON
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
                
                # 2. 통신 에러(A 꺼짐 현상) 방지를 위해 50ms 뒤 B를 켤 때 A도 1로 다시 쐐기 박음
                def turn_on_b_safe():
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1))
                    time.sleep(0.2)
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 1))
                    # UI 버튼 상태 동기화
                    if self.mdo_btns[25]:
                        self.mdo_btns[25].blockSignals(True)
                        self.mdo_btns[25].setChecked(True)
                        self.mdo_btns[25].blockSignals(False)
                        
                QtCore.QTimer.singleShot(200, turn_on_b_safe)
                return
            else:
                print("[MUTE MANUAL] Mute A(24) 단독 소등/작동")
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(24, 1 if checked else 0))
                return

        if index == 25:
            # B 버튼은 켤 때나 끌 때 무조건 자기 자신만 동작 (각자 끈다고 하셨으므로)
            print(f"[MUTE MANUAL] Mute B(25) {'ON' if checked else 'OFF'} 단독 제어")
            self.io_module.Send_que.put(self.io_module.Write_DO_Data(25, 1 if checked else 0))
            return
        
        # =========================================================================
        # ★ [핵심 추가] HEAD 모드 수동 조작 시 Cell 1, 2 지그 전/후진 자동 뮤트 적용
        # =========================================================================
        if self.system_mode == "HEAD" and index in [0, 1, 8, 9]:
            if checked:
                # 다른 수동 이동(포즈 이동 등)이 진행 중이면 중복 클릭 차단
                if getattr(self, 'is_manual_moving', False):
                    print("[WARN] 현재 지그/로봇이 이동 중입니다. 중복 명령 무시.")
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
                    return
                
                # 즉시 신호 전송을 중단하고 백그라운드 뮤팅 스레드를 호출합니다.
                self._manual_jig_move_with_mute(index)
                return
            else:
                # 사용자가 버튼을 수동으로 끌 때(공압 제거)는 안전하므로 그대로 통과시킵니다.
                pass

        # 3. 명령 전송 (위의 안전 검사를 모두 통과했을 때만 실행됨)
        print(f"[UI] MDO {index:02d} -> {'ON' if checked else 'OFF'} 요청")
            
        # IO 스레드 큐에 전송
        self.io_module.Send_que.put(
            self.io_module.Write_DO_Data(index, 1 if checked else 0)
        )
        
    def show_blow_time_setting(self):
        """5초간 꾹 눌렀을 때 실행되는 설정 팝업창"""
        self._is_long_pressed = True # ★ 5초 도달! 롱클릭(꾹 누름)으로 인정함
        
        # UI 버튼이 눌려 켜져(ON) 버린 상태를 강제로 원상복구(OFF) 시킴
        btn = self.mdo_btns[41]
        if btn:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            if self.io_module is not None:
                self.io_module.Send_que.put(self.io_module.Write_DO_Data(41, 0))

        self.show_touch_keyboard()
        val_str, ok = self.show_custom_input_dialog(
            title="NR 볼트 파기 (에어 블로우) 설정",
            label_text="홈 복귀 시 블로우 작동 시간(초)을 입력하세요:\n(※ 0 입력 시 블로우 기능 꺼짐)",
            echo_mode=QLineEdit.Normal,
            default_value=str(getattr(self, 'blow_time', 0.0))
        )
        self.hide_touch_keyboard()

        if ok and val_str:
            try:
                new_time = float(val_str)
                if new_time < 0: new_time = 0.0 # 음수 방지
                
                self.blow_time = new_time
                self.db_manager.save_setting("nr_blow_time", str(self.blow_time)) # DB 저장
                
                self.show_message("설정 완료", f"NR 블로우(파기) 시간이 <b>{self.blow_time}초</b>로 저장되었습니다.")
            except ValueError:
                self.show_message("입력 오류", "숫자(소수점 포함)만 입력해 주세요.", QMessageBox.Warning)
        
    def _manual_jig_move_with_mute(self, index):
        """수동 모드에서 지그 전진/후진 시 자동으로 라이트 커튼 뮤팅을 적용하는 백그라운드 함수"""
        def task():
            try:
                self.is_manual_moving = True  # 중복 조작 방지 락(Lock)
                print(f"[MANUAL MUTE] 지그 조작(MDO {index}) 자동 뮤팅 시퀀스 시작")

                # # 1. 센서 리셋 (MDO 23) 펄스 발생 (뮤트 활성화 준비)
                # self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 1))
                # time.sleep(0.05)
                # self.io_module.Send_que.put(self.io_module.Write_DO_Data(23, 0))
                # time.sleep(0.05)

                # 2. 뮤트 상태 ON (폴링 스레드가 이를 감지해 MDO 24, 25번 릴레이를 켭니다)
                self.seq_mute_active = True
                
                self.force_sync_mute()
                
                # 3. 뮤트 릴레이가 확실히 붙고 통신이 반영될 시간 부여
                time.sleep(0.6)

                # 4. 실제 밸브 제어 (반대쪽 공압을 먼저 끄고, 목적지 공압을 켭니다)
                if index == 0:   # C1 전진
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(1, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(0, 1))
                    target_mdi = 0
                elif index == 1: # C1 후진
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(0, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(1, 1))
                    target_mdi = 1
                elif index == 8: # C2 전진
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(9, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(8, 1))
                    target_mdi = 8
                elif index == 9: # C2 후진
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(8, 0))
                    self.io_module.Send_que.put(self.io_module.Write_DO_Data(9, 1))
                    target_mdi = 9

                # 5. 해당 위치의 실린더 센서가 감지될 때까지 대기
                start_t = time.time()
                while time.time() - start_t < 5.0: # 5초 타임아웃
                    if self.is_alarm_state():
                        print("[MANUAL MUTE] 알람 발생으로 센서 감시 중단")
                        break
                    
                    inp = self.io_module.Read_Input_Data()
                    if inp and len(inp) > target_mdi and inp[target_mdi] == 1:
                        print(f"[MANUAL MUTE] 지그 도착 확인 (MDI {target_mdi} ON)")
                        break
                    time.sleep(0.1)

                # 6. 안정화 대기 (센서 펄스가 튀고 지그가 완전히 정지할 여유)
                time.sleep(0.3)
                
            except Exception as e:
                print(f"[MANUAL MUTE ERROR] {e}")
            finally:
                # 7. 성공하든, 에러가 나든 무조건 뮤트 해제 및 이동 락(Lock) 해제
                self.seq_mute_active = False
                self.is_manual_moving = False
                print("[MANUAL MUTE] 지그 조작 종료, 뮤팅 해제 (감시 재개)")

        # 백그라운드 스레드로 실행 (UI 프리징 완전 방지)
        import threading
        t = threading.Thread(target=task)
        t.daemon = True
        t.start()
        
    def apply_io_labels(self):
        """현재 시스템 모드에 맞는 JSON 파일을 로드하여 UI 라벨(Text)을 업데이트합니다."""
        config_name = f"{self.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name

        try:
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                
                # MDI 라벨 업데이트
                mdi_labels = config_data.get("MDI", {})
                for i in range(48):
                    idx_str = f"{i:02d}"
                    if idx_str in mdi_labels and self.mdi_lamps[i]:
                        self.mdi_lamps[i].setText(mdi_labels[idx_str])
                    elif self.mdi_lamps[i]:
                        self.mdi_lamps[i].setText(f"MDI_{idx_str}") # 기본값

                # MDO 라벨 업데이트
                mdo_labels = config_data.get("MDO", {})
                for i in range(48):
                    idx_str = f"{i:02d}"
                    if idx_str in mdo_labels and self.mdo_btns[i]:
                        self.mdo_btns[i].setText(mdo_labels[idx_str])
                    elif self.mdo_btns[i]:
                        self.mdo_btns[i].setText(f"MDO_{idx_str}") # 기본값
                        
                print(f"[UI] {self.system_mode} IO 라벨 적용 완료")
            else:
                print(f"[WARN] 설정 파일을 찾을 수 없습니다: {config_path}")
        except Exception as e:
            print(f"[ERROR] IO 라벨 로드 실패: {e}")
            
    def init_robot_var_ui(self, font_size=14):
        """설정 파일을 로드하고, QTableWidget을 초기화합니다."""
        try:
            # 1. 테이블 기본 설정
            table = self.ui.robot_var_table 
            
            table.setRowCount(0) # 기존 데이터 클리어
            table.setColumnCount(2) # 컬럼 2개 (명, 값)
            table.setHorizontalHeaderLabels(["변수 명", "변수 값"])
            
            # 헤더 디자인 (꽉 채우기)
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.Stretch) # 변수명 칸 늘리기
            header.setSectionResizeMode(1, QHeaderView.Stretch) # 값 칸 늘리기
            
            self.var_item_map = {} # 업데이트를 위한 맵 (Key -> Value Item)

            # 2. 설정 파일 로드
            config_name = f"{self.system_mode.lower()}_config.json"
            config_path = APP_ROOT / "DB" / "config" / config_name

            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                
                # [수정] 분리된 INPUT, OUTPUT 변수명을 모두 추출하여 합침
                var_inputs = config_data.get("ROBOT_VAR_INPUT", {})
                var_outputs = config_data.get("ROBOT_VAR_OUTPUT", {})
                
                all_var_names = list(var_inputs.values()) + list(var_outputs.values())
                # 중복 제거 (순서 유지)
                all_var_names = list(dict.fromkeys(all_var_names))
                
                # 폰트 및 높이 설정
                base_font = QFont("Arial", font_size)
                row_height = font_size + 20

                # 3. 데이터 채우기
                for var_name in all_var_names:
                    # (A) 초기값 읽기 (Modbus 캐시 또는 29999 포트 통일 함수 사용)
                    val_text = "-"
                    if not self.robot_disconnected:
                        try:
                            # 기존 직접 get_variable() 호출 대신 공용 Wrapper 함수 사용
                            val = self.get_variable_with_ui(var_name)
                            if val is not None:
                                if isinstance(val, float):
                                    val_text = f"{val:.4f}"
                                else:
                                    val_text = str(val)
                        except Exception:
                            pass

                    # (B) 행 추가
                    row_idx = table.rowCount()
                    table.insertRow(row_idx)
                    table.setRowHeight(row_idx, row_height)

                    # (C) [1열] 변수 명 아이템 생성 (수정 불가)
                    name_item = QTableWidgetItem(var_name)
                    name_item.setFont(base_font)
                    name_item.setTextAlignment(Qt.AlignCenter) 
                    name_item.setFlags(name_item.flags() ^ Qt.ItemIsEditable) # 수정 불가
                    table.setItem(row_idx, 0, name_item)

                    # (D) [2열] 변수 값 아이템 생성
                    val_item = QTableWidgetItem(val_text)
                    val_item.setFont(base_font)
                    val_item.setTextAlignment(Qt.AlignCenter) 
                    val_item.setForeground(QBrush(QColor(0, 0, 0))) 
                    
                    # ★ 중요: 수정 시 이 키(var_name)를 참조하여 Modbus Write 수행
                    val_item.setData(Qt.UserRole, var_name)
                    
                    table.setItem(row_idx, 1, val_item)

                    # (E) 맵핑 저장
                    self.var_item_map[var_name] = val_item
            
            # 테이블 속성 설정 (키보드 입력 방지 등)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)

            # # 더블 클릭 시그널 연결 (기존 연결 해제 후 재연결)
            # try: table.cellDoubleClicked.disconnect()
            # except: pass
            # table.cellDoubleClicked.connect(self.on_robot_var_table_double_click)
            
            print(f"[UI] 로봇 변수 테이블 초기화 완료")

        except Exception as e:
            print(f"[ERROR] 변수 UI 초기화 실패: {e}")
            
    # def on_robot_var_table_double_click(self, row, col):
    #     """로봇 변수 테이블 더블 클릭 핸들러"""
        
    #     # 1. 오토 모드인지 확인 (오토면 차단)
    #     if not self.check_auto_mode_restriction():
    #         self.ui.robot_var_table.clearFocus() 
    #         return
        
    #     # 2. 변수 값(1열)인지 확인 (0열은 변수명이므로 수정 불가)
    #     if col != 1:
    #         return
            
    #     item = self.ui.robot_var_table.item(row, col)
    #     if not item:
    #         return
            
    #     # 저장해둔 변수 키 가져오기
    #     var_key = item.data(Qt.UserRole)
    #     old_value = item.text()
        
    #     # 3. 가상 키보드 실행
    #     self.show_touch_keyboard()
        
    #     # 4. 커스텀 다이얼로그 호출
    #     new_value, ok = self.show_custom_input_dialog(
    #         title=f"변수 값 수정 ({var_key})", 
    #         label_text="새로운 값을 입력하세요:", 
    #         echo_mode=QLineEdit.Normal, 
    #         default_value=old_value
    #     )
        
    #     # 5. 키보드 숨김
    #     self.hide_touch_keyboard()
        
    #     # 6. 값 적용 및 로봇 전송
    #     if ok and new_value:
    #         # 입력값이 숫자인지 문자열인지 판별하여 변환
    #         try:
    #             if "." in new_value:
    #                 final_val = float(new_value)
    #             else:
    #                 final_val = int(new_value)
    #         except ValueError:
    #             final_val = new_value # 변환 실패 시 문자열 그대로 사용

    #         print(f"[USER] 변수 변경 요청: {var_key} -> {final_val}")
            
    #         # 로봇에 전송 (이 함수 내부에서 update_robot_var_ui를 호출하여 UI도 갱신됨)
    #         self.set_variable_with_ui(var_key, final_val)

    #     # 포커스 해제
    #     self.ui.robot_var_table.clearFocus()
    #     self.setFocus()
            
    # =========================================================================
    # [WRAPPER] 로봇 변수 읽기 + UI 자동 업데이트 함수
    # =========================================================================
    def get_variable_with_ui(self, var_name):
        # J_home은 배열 데이터이므로 기존 29999 포트 유지
        if var_name == "J_home":
            value = self.robot_29999.get_variable_cached(var_name)   # ★ [PATCH] 캐시
        else:
            # 캐싱된 Modbus 데이터(OUTPUT)에서 읽기
            value = self.cached_robot_vars.get(var_name, 0)
        
        self.update_robot_var_ui(var_name, value)
        return value

    def set_variable_with_ui(self, var_name, value):
        # JSON의 ROBOT_VAR_INPUT에 정의된 주소 매핑 참조
        config_name = f"{self.system_mode.lower()}_config.json"
        config_path = APP_ROOT / "DB" / "config" / config_name
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
            var_inputs = config_data.get("ROBOT_VAR_INPUT", {})
            
        target_addr = None
        for addr_str, name in var_inputs.items():
            if name == var_name:
                target_addr = int(addr_str)
                break
                
        if target_addr is not None:
            if str(value).strip().lower() in ['0', 'false', 'none', '']:
                send_val = False
            else:
                send_val = True
                
            print(f"[Modbus Write Coil] {var_name} (Addr: {target_addr}) = {send_val}")
            
            try:
                # ========================================================
                # ★ [핵심 추가] 쓰기 작업 중에는 다른 곳에서 통신하지 못하게 막음
                # ========================================================
                with self.modbus_lock:
                    self.modbus_client.set_coil(target_addr, send_val)
            except Exception as e:
                print(f"[ERROR] Coil {target_addr} 쓰기 실패: {e}")
                
        else:
            print(f"[WARN] {var_name}은(는) ROBOT_VAR_INPUT에 정의되지 않았습니다.")
            
        # =======================================================
        # ★ [수정] UI 업데이트 호출 시 변환된 boolean(send_val) 전송
        # =======================================================
        self.update_robot_var_ui(var_name, send_val if target_addr is not None else value)
        
    # =========================================================================
    # [Network & Serial] 설정 관리 (IP/Port/COM/Baudrate)
    # =========================================================================
    def init_network_ui(self, font_size=14):
        """
        DB에서 연결 정보(Ethernet + RS232)를 불러와 테이블을 초기화합니다.
        """
        # 0~2: Ethernet, 3: RS232 (Map 확장)
        self.network_map = {
            0: {"key": "ROBOT_29999", "default_ip": "192.168.227.131", "default_port": 29999, "name": "로봇 제어 (29999)"},
            1: {"key": "ROBOT_30001", "default_ip": "192.168.227.131", "default_port": 30001, "name": "로봇 제어 (30001)"},
            2: {"key": "ROBOT_MODBUS", "default_ip": "192.168.227.131", "default_port": 502, "name": "로봇 제어 (Modbus)"},
            3: {"key": "IO_MODULE",   "default_ip": "127.0.0.1",    "default_port": 502,   "name": "IO 모듈 (Modbus)"},
            4: {"key": "NR_RS232",    "default_ip": "COM1",            "default_port": 9600,  "name": "너트러너 (RS232)"}
        }

        # 1. 테이블 기본 설정
        table = self.ui.ip_table
        table.setRowCount(0)
        table.setColumnCount(2)
        # 헤더 명칭 변경 (IP/COM 공용)
        table.setHorizontalHeaderLabels(["주소 / COM Port", "Port / Baudrate"])
        
        # 헤더 디자인
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch) 
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        
        base_font = QFont("Arial", font_size)
        row_height = font_size + 20
        row_labels = []

        # 2. 데이터 로드 및 행 생성
        for idx, info in self.network_map.items():
            key = info["key"]
            row_labels.append(info["name"])

            # (A) DB에서 값 불러오기
            # IP컬럼에 COM포트명, Port컬럼에 Baudrate가 저장됨
            val1, val2 = self.db_manager.load_connection(key) 

            # DB에 없으면 기본값 사용 및 저장
            if val1 is None:
                val1 = info["default_ip"]
                val2 = info["default_port"]
                self.db_manager.save_connection(key, val1, val2)

            # 멤버 변수 업데이트
            if key == "ROBOT_29999":
                self.robot_ip = val1
                self.robot_port1 = int(val2)
            elif key == "ROBOT_30001":
                self.robot_port2 = int(val2)
            elif key == "ROBOT_MODBUS":
                self.modbus_ip = val1
                self.modbus_port= int(val2)
            elif key == "IO_MODULE":
                self.io_ip = val1
                self.io_port = int(val2)
            elif key == "NR_RS232":
                self.nr_ser_port = val1      # 예: "COM3"
                self.nr_baudrate = int(val2) # 예: 9600

            # (B) 행 추가
            row_idx = table.rowCount()
            table.insertRow(row_idx)
            table.setRowHeight(row_idx, row_height)

            # (C) 1열 (IP or COM)
            item1 = QTableWidgetItem(str(val1))
            item1.setFont(base_font)
            item1.setTextAlignment(Qt.AlignCenter)
            table.setItem(row_idx, 0, item1)

            # (D) 2열 (Port or Baudrate)
            item2 = QTableWidgetItem(str(val2))
            item2.setFont(base_font)
            item2.setTextAlignment(Qt.AlignCenter)
            table.setItem(row_idx, 1, item2)

        table.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.AnyKeyPressed)
        table.setVerticalHeaderLabels(row_labels)
        
        try: table.cellDoubleClicked.disconnect()
        except: pass
        
        table.cellDoubleClicked.connect(self.on_ip_table_double_click)
        
        print("[Network] 설정 로드 및 테이블 초기화 완료")

    def on_ip_table_double_click(self, row, col):
        if not self.check_auto_mode_restriction():
            self.ui.ip_table.clearFocus()
            return
        
        item = self.ui.ip_table.item(row, col)
        if not item: return
            
        old_value = item.text()
        
        # 1. 가상 키보드 실행
        self.show_touch_keyboard()
        
        # 2. 다이얼로그 제목 설정 (RS232 행 구분)
        is_rs232 = (row == 3) # 3번 행이 너트러너 RS232
        
        if col == 0:
            title = "COM 포트 입력" if is_rs232 else "IP 주소 입력"
        else:
            title = "Baudrate 입력" if is_rs232 else "Port 번호 입력"

        # 3. 커스텀 다이얼로그 호출
        new_value, ok = self.show_custom_input_dialog(
            title=title, 
            label_text="값을 입력하세요:", 
            echo_mode=QLineEdit.Normal, 
            default_value=old_value
        )
        
        # 4. 키보드 숨김
        self.hide_touch_keyboard()
        
        # 5. 값 적용
        if ok and new_value:
            item.setText(new_value)

        self.ui.ip_table.clearFocus()
        self.setFocus()
        
    def save_network_settings(self):
        if not self.check_auto_mode_restriction():
            return
        """
        테이블 값을 읽어서 DB에 저장하고, 변수 업데이트 및 시스템 재연결.
        """
        print("[Network] 설정 저장 및 재연결 시도...")
        
        try:
            # 1. 테이블 값 읽어서 DB 저장
            for row, info in self.network_map.items():
                key = info["key"]
                
                # 테이블 아이템 가져오기
                item1 = self.ui.ip_table.item(row, 0) # IP or COM
                item2 = self.ui.ip_table.item(row, 1) # Port or Baud
                
                if item1 and item2:
                    val1 = item1.text().strip()
                    val2 = item2.text().strip()
                    
                    # 2열(Port/Baud)은 숫자 변환 체크
                    new_port_or_baud = int(val2) 
                    
                    # DB 저장
                    self.db_manager.save_connection(key, val1, new_port_or_baud)
                    
                    # 멤버 변수 업데이트
                    if key == "ROBOT_29999":
                        self.robot_ip = val1
                        self.robot_port1 = new_port_or_baud
                    elif key == "ROBOT_30001":
                        self.robot_port2 = new_port_or_baud
                    elif key == "ROBOT_MODBUS":
                        self.modbus_ip = val1
                        self.modbus_port = new_port_or_baud
                    elif key == "IO_MODULE":
                        self.io_ip = val1
                        self.io_port = new_port_or_baud
                    elif key == "NR_RS232":
                        self.nr_ser_port = val1      # "COMx"
                        self.nr_baudrate = new_port_or_baud # 9600

            self.show_message(
                "저장 완료", 
                "설정이 저장되었습니다.\n시스템을 재연결합니다.", 
                QMessageBox.Information
            )
            
            # 2. 시스템 재연결
            self.connect_robot(show_popup=True)

        except ValueError:
            self.show_message(
                "입력 오류", 
                "Port 번호와 Baudrate는 숫자여야 합니다.", 
                QMessageBox.Warning
            )
        except Exception as e:
            self.show_message(
                "오류 발생", 
                f"저장 중 오류 발생: \n{e}", 
                QMessageBox.Critical
            )

    def open_vnc_viewer(self):
        # 1. 오토 모드 제한 확인
        if not self.check_auto_mode_restriction():
            return
            
        target_ip = self.robot_ip 
        
        # 2. 현재 설정된 경로에 파일이 있는지 확인
        if not os.path.exists(self.vnc_path):
            # 파일이 없으면 사용자에게 선택 요청
            self.show_message(
                "VNC 경로 설정", 
                "VNC Viewer 실행 파일을 찾을 수 없습니다.\n실행 파일(.exe) 위치를 지정해주세요.",
                QMessageBox.Information
            )
            
            # 파일 탐색기 열기
            fname, _ = QFileDialog.getOpenFileName(
                self, 
                "VNC Viewer 실행 파일 선택", 
                "C:\\", 
                "Executable (*.exe)"
            )
            
            if fname:
                # 선택한 경로 저장 (메모리 + DB)
                self.vnc_path = fname
                self.db_manager.save_setting("vnc_path", fname)
                print(f"[SYSTEM] VNC 경로 업데이트됨: {self.vnc_path}")
            else:
                # 취소 누르면 실행 안 함
                print("[SYSTEM] VNC 경로 선택 취소됨")
                return

        # 3. VNC 실행
        try:
            print(f"[SYSTEM] VNC 뷰어 실행 시도: {self.vnc_path} -> {target_ip}")
            subprocess.Popen([self.vnc_path, target_ip])
            
        except Exception as e:
            print(f"[ERROR] VNC 실행 실패: {e}")
            self.show_message(
                "오류 발생", 
                f"VNC 실행 중 에러 발생:\n{e}", 
                QMessageBox.Critical
            )
            
    def show_touch_keyboard(self):
        """Windows 가상 키보드 실행"""
        if platform.system() == "Windows":
            try:
                subprocess.Popen("osk", shell=True)
            except Exception as e:
                print(f"[ERROR] 가상 키보드 실행 실패: {e}")

    def hide_touch_keyboard(self):
        """Windows 가상 키보드(osk.exe) 종료 - 프로세스 킬 & 윈도우 메시지 전송"""
        # 1. IP 설정 테이블 초기화
        if hasattr(self.ui, 'ip_table'):
            self.ui.ip_table.clearSelection()  # 파란색 선택 박스 제거 (핵심)
            self.ui.ip_table.clearFocus()      # 위젯 포커스 제거
            self.ui.ip_table.setCurrentItem(None) # 현재 아이템 포커스도 제거

        # 2. 로봇 변수 테이블 초기화
        if hasattr(self.ui, 'robot_var_table'):
            self.ui.robot_var_table.clearSelection()
            self.ui.robot_var_table.clearFocus()
            self.ui.robot_var_table.setCurrentItem(None)

        # 3. 포커스를 메인 윈도우 배경(centralwidget)으로 강제 이동
        if hasattr(self.ui, 'centralwidget'):
            self.ui.centralwidget.setFocus()
        else:
            self.setFocus()
            
        if platform.system() == "Windows":
            # ----------------------------------------------------------
            # 방법 1: Win32 API로 창을 찾아서 '닫기' 명령 보내기 (가장 확실함)
            # ----------------------------------------------------------
            try:
                # "OSKMainClass"는 osk.exe의 윈도우 클래스 이름입니다.
                hwnd = ctypes.windll.user32.FindWindowW(u"OSKMainClass", None)
                
                if hwnd:
                    # 0x0112: WM_SYSCOMMAND (시스템 명령)
                    # 0xF060: SC_CLOSE (닫기)
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0112, 0xF060, 0)
                    print("[OSK] 윈도우 메시지로 닫기 성공")
                    return # 성공했으면 종료
            except Exception as e:
                print(f"[WARN] 윈도우 메시지 전송 실패: {e}")

            # ----------------------------------------------------------
            # 방법 2: 프로세스 강제 종료 (Taskkill) - 관리자 권한 필요할 수 있음
            # ----------------------------------------------------------
            try:
                subprocess.call("taskkill /IM osk.exe /F", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                # TabTip(태블릿 키보드)이 켜져 있을 경우를 대비해 같이 종료
                subprocess.call("taskkill /IM TabTip.exe /F", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            except Exception as e:
                print(f"[ERROR] 가상 키보드 프로세스 종료 실패: {e}")
                
    # =========================================================================
    # [UI Helper] 커스텀 입력 다이얼로그 (크기, 색상, 위치 제어)
    # =========================================================================
    def show_custom_input_dialog(self, title, label_text, echo_mode=QLineEdit.Normal, default_value=""):
        """
        QInputDialog를 커스텀하여 실행하고 입력값을 반환합니다.
        - 크기 키움
        - 입력창: 흰 배경 / 검은 글씨
        - 위치: 화면 상단 1/4 지점 (가상키보드 간섭 방지)
        """
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label_text)
        dialog.setTextValue(default_value)
        dialog.setTextEchoMode(echo_mode)
        
        # 1. 스타일시트 적용 (다크 테마 + 입력창 흰색/검은글씨 + 크기 증가)
        dialog.setStyleSheet("""
            QInputDialog {
                background-color: #2b2b2b;
                border: 2px solid #0078D7;
            }
            QLabel {
                color: white;
                font-size: 20px;
                font-weight: bold;
                background-color: #2b2b2b;
                margin-bottom: 10px;
            }
            QLineEdit {
                background-color: white;    /* 배경 흰색 */
                color: black;               /* 글씨 검정 */
                font-size: 24px;            /* 글씨 크게 */
                padding: 10px;
                border-radius: 5px;
                border: 1px solid #ccc;
            }
            QPushButton {
                background-color: #555555;
                color: white;
                font-size: 18px;
                font-weight: bold;
                border-radius: 5px;
                min-width: 120px;
                min-height: 50px;
                margin: 5px;
            }
            QPushButton:hover {
                background-color: #0078D7;
            }
        """)

        # 2. 다이얼로그 크기 강제 설정
        dialog.setFixedSize(500, 280)

        # 3. 위치 조정 (화면 상단 1/4 지점)
        screen_geo = QApplication.desktop().screenGeometry()
        scr_w, scr_h = screen_geo.width(), screen_geo.height()
        dlg_w, dlg_h = dialog.width(), dialog.height()
        
        # x: 중앙, y: 상단 25% 지점
        new_x = int(scr_w / 2 - dlg_w / 2)
        new_y = int(scr_h * 0.25) 
        
        dialog.move(new_x, new_y)
        
        result = dialog.exec_()
        self.hide_touch_keyboard()
        self.setFocus()

        # 4. 실행 및 결과 반환
        text = dialog.textValue()
        ok = (result == QDialog.Accepted)
        
        return text, ok
    
    # =========================================================================
    # [Sequence Mapping & Pose Movement] JSON 로드 및 포인트 이동 로직
    # =========================================================================
    def init_sequence_mapping(self):
        """JSON 맵핑 파일을 로드하고 모든 포즈 관련 버튼의 클릭 이벤트를 연결합니다."""
        config_path = APP_ROOT / "DB" / "config" / "sequence_mapping.json"
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.seq_map = json.load(f)
            print("[INIT] Sequence 맵핑 JSON 로드 완료")
        except Exception as e:
            print(f"[ERROR] Sequence 맵핑 로드 실패 (파일 없음): {e}")
            self.seq_map = {"COMMON": {}, "HEAD": {}, "BACK": {}}

        # 1. 홈 버튼 연결 (기존 홈 이동 + 포즈 기억 리셋)
        try: self.ui.home_pose_btn.clicked.disconnect()
        except: pass
        self.ui.home_pose_btn.clicked.connect(self.on_home_button_clicked)
        self.ui.home_pose_btn.clicked.connect(self._reset_pose_memory)

        # 2. Cell 버튼 연결 (인터락 포함)
        self.ui.cell_1_btn.setCheckable(True)
        self.ui.cell_2_btn.setCheckable(True)
        try:
            self.ui.cell_1_btn.clicked.disconnect()
            self.ui.cell_2_btn.clicked.disconnect()
        except: pass
        self.ui.cell_1_btn.clicked.connect(lambda checked: self.on_cell_btn_clicked("CELL_1", checked))
        self.ui.cell_2_btn.clicked.connect(lambda checked: self.on_cell_btn_clicked("CELL_2", checked))

        # 3. 일반 포즈 버튼들 연결
        pose_btns = [
            "waypoint_pose_btn",
            "pose_1_btn", "pose_2_btn", "pose_3_btn", "pose_4_btn", "pose_5_btn",
            "pose_6_btn", "pose_7_btn", "pose_8_btn", "pose_9_btn", "pose_10_btn", "pose_11_btn"
        ]

        for btn_name in pose_btns:
            btn_widget = getattr(self.ui, btn_name, None)
            if btn_widget:
                btn_widget.setCheckable(True)
                try: btn_widget.clicked.disconnect()
                except: pass
                btn_widget.clicked.connect(lambda checked, name=btn_name: self.on_pose_btn_clicked(name, checked))

    def _reset_pose_memory(self):
        """홈 버튼 클릭이나 강제 초기화 시 포즈 상태를 초기화합니다."""
        if self.active_pose_btn:
            prev_btn = getattr(self.ui, self.active_pose_btn, None)
            if prev_btn:
                prev_btn.blockSignals(True)
                prev_btn.setChecked(False)
                prev_btn.blockSignals(False)
        self.active_pose_btn = None
        self.last_pose_sequence = []

    def on_cell_btn_clicked(self, cell_name, checked):
        """Cell 1/2 버튼 클릭 시 상호 배타성 및 로봇 홈 위치를 검증합니다."""
        btn_clicked = self.ui.cell_1_btn if cell_name == "CELL_1" else self.ui.cell_2_btn
        other_btn = self.ui.cell_2_btn if cell_name == "CELL_1" else self.ui.cell_1_btn

        # [안전 검사] 이동 중 조작 차단
        if getattr(self, 'is_manual_moving', False):
            self.show_message("이동 중", "현재 로봇이 이동 중입니다.", QMessageBox.Warning)
            btn_clicked.blockSignals(True); btn_clicked.setChecked(not checked); btn_clicked.blockSignals(False)
            return

        if getattr(self, 'robot_disconnected', True):
            self.show_message("조작 불가", "로봇이 연결되어 있지 않습니다.", QMessageBox.Warning)
            btn_clicked.blockSignals(True); btn_clicked.setChecked(not checked); btn_clicked.blockSignals(False)
            return

        if not self.check_auto_mode_restriction():
            btn_clicked.blockSignals(True); btn_clicked.setChecked(not checked); btn_clicked.blockSignals(False)
            return

        if checked: 
            if other_btn.isChecked():
                self.show_message("조작 불가", "다른 셀이 이미 선택되어 있습니다.<br>먼저 기존 셀 선택을 취소해주세요.", QMessageBox.Warning)
                btn_clicked.blockSignals(True); btn_clicked.setChecked(False); btn_clicked.blockSignals(False)
                return

            is_physically_home = False
            try:
                import ast
                # 현재 로봇의 6축 관절 데이터
                current_joints = [
                    self.actual_joint_base, self.actual_joint_shoulder, self.actual_joint_elbow,
                    self.actual_joint_wrist1, self.actual_joint_wrist2, self.actual_joint_wrist3
                ]
                
                # 로봇에 저장된 J_home 데이터 호출
                raw_home = self.robot_29999.get_variable_cached("J_home")   # ★ [PATCH] 캐시
                home_joints = raw_home if isinstance(raw_home, list) else None
                
                # 현재 위치와 J_home의 오차가 0.05 라디안(약 2.8도) 이내인지 확인
                if isinstance(home_joints, list) and len(home_joints) == 6:
                    is_physically_home = all(abs(c - h) <= 0.05 for c, h in zip(current_joints, home_joints))
                    
            except Exception as e:
                print(f"[ERROR] J_home 비교 실패: {e}")

            # 물리적으로 홈 위치가 아니라면 튕겨냄
            if not is_physically_home:
                self.show_message("조작 불가", "로봇이 <b>HOME 위치</b>에 있어야만 셀을 선택할 수 있습니다.<br>[홈으로 이동] 버튼을 먼저 눌러주세요.", QMessageBox.Warning)
                btn_clicked.blockSignals(True); btn_clicked.setChecked(False); btn_clicked.blockSignals(False)
                return
                
            print(f"[UI] {cell_name} 선택 완료")
        else:
            print(f"[UI] {cell_name} 선택 취소됨")
            self._reset_pose_memory()

    def on_pose_btn_clicked(self, btn_name, checked):
        btn_widget = getattr(self.ui, btn_name)

        # [다중 클릭 차단 방패] 현재 다른 이동이 진행 중이면 무시
        if getattr(self, 'is_manual_moving', False):
            self.show_message("이동 중", "현재 로봇이 이동 중입니다. 완료 후 조작해주세요.", QMessageBox.Warning)
            btn_widget.blockSignals(True); btn_widget.setChecked(not checked); btn_widget.blockSignals(False)
            return

        self.is_manual_moving = True  # 진입 시 락 온
        
        try:
            if getattr(self, 'robot_disconnected', True) or not self.check_auto_mode_restriction():
                btn_widget.blockSignals(True); btn_widget.setChecked(not checked); btn_widget.blockSignals(False)
                return

            # 1. 버튼 끄기 (역순 빠져나오기)
            if not checked:
                if self.active_pose_btn == btn_name:
                    success = self.execute_reverse_sequence()
                    if not success:
                        # I/O 조건 불충족 등으로 빠져나오기 실패 시 버튼 상태 원상복구(ON)
                        btn_widget.blockSignals(True); btn_widget.setChecked(True); btn_widget.blockSignals(False)
                return
                
            # 2. 다른 버튼 누름 (이전 포인트 먼저 빠져나오기)
            if self.active_pose_btn is not None and self.active_pose_btn != btn_name:
                print(f"[SEQ] 이전 포인트({self.active_pose_btn}) 역순 복귀 먼저 실행")
                success = self.execute_reverse_sequence()
                if not success:
                    # 이전 포인트 빠져나오기 실패하면 새 포인트 진입도 무효화(OFF)
                    btn_widget.blockSignals(True); btn_widget.setChecked(False); btn_widget.blockSignals(False)
                    return
                    
                prev_btn_widget = getattr(self.ui, self.active_pose_btn, None)
                if prev_btn_widget:
                    prev_btn_widget.blockSignals(True); prev_btn_widget.setChecked(False); prev_btn_widget.blockSignals(False)

            # JSON 시퀀스 로드
            sequence_list = self.get_sequence_for_btn(btn_name)
            if not sequence_list:
                self.show_message("조작 불가", "해당 위치로 이동할 수 없습니다.", QMessageBox.Warning)
                btn_widget.blockSignals(True); btn_widget.setChecked(False); btn_widget.blockSignals(False)
                return

            print(f"[SEQ] '{btn_name}' 진입 시퀀스 시작: {sequence_list}")
            
            # 3. 순서대로 이동 수행 (내부에서 I/O 검사 진행됨)
            for var_name in sequence_list:
                if not self.move_to_variable(var_name, btn_name=btn_name, is_reverse=False):
                    print(f"[SEQ ERROR] {var_name} 이동 실패 (I/O 조건 미달 또는 통신 에러)")
                    btn_widget.blockSignals(True); btn_widget.setChecked(False); btn_widget.blockSignals(False)
                    return
                
            # 모든 이동 성공 시 상태 저장
            self.active_pose_btn = btn_name
            self.last_pose_sequence = sequence_list

        finally:
            self.is_manual_moving = False # 성공하든 에러나든 무조건 락 해제

    def execute_reverse_sequence(self):
        """기억해둔 경로를 역순으로 빠져나옵니다. 성공 시 True 반환"""
        if not self.last_pose_sequence:
            return True
            
        # 예: [웨이포인트, 보조, 목적지] -> 목적지를 제외한 [보조, 웨이포인트] 순으로
        reverse_path = self.last_pose_sequence[::-1][1:]
        print(f"[SEQ] 역순 복귀 시퀀스 실행: {reverse_path}")
        
        for var_name in reverse_path:
            # 빠져나올 때도 현재 켜져있는 버튼(active_pose_btn)의 이름과 is_reverse=True를 전달
            if not self.move_to_variable(var_name, btn_name=self.active_pose_btn, is_reverse=True):
                print(f"[SEQ ERROR] 역순 복귀 중 {var_name} 이동 실패")
                return False
            
        self.active_pose_btn = None
        self.last_pose_sequence = []
        return True

    def get_sequence_for_btn(self, btn_name):
        """현재 모드에 맞춰 JSON에서 시퀀스 배열을 찾아 반환합니다."""
        if self.system_mode == "HEAD":
            target_cell = None
            if self.ui.cell_1_btn.isChecked(): target_cell = "CELL_1"
            elif self.ui.cell_2_btn.isChecked(): target_cell = "CELL_2"
            else:
                self.show_message("조작 오류", "이동 전 <b>Cell 1</b> 또는 <b>Cell 2</b>를 먼저 선택해주세요.", QMessageBox.Warning)
                return []
            return self.seq_map.get("HEAD", {}).get(target_cell, {}).get(btn_name, [])
            
        elif self.system_mode == "BACK":
            back_dict = self.seq_map.get("BACK", {})
            if btn_name in back_dict.get("NORMAL", {}): return back_dict["NORMAL"][btn_name]
            elif btn_name in back_dict.get("ROTATION", {}): return back_dict["ROTATION"][btn_name]
            
        return []
    
    def get_current_joints(self):
        """현재 로봇의 관절 각도를 리스트로 반환합니다."""
        return [
            self.actual_joint_base, self.actual_joint_shoulder, self.actual_joint_elbow,
            self.actual_joint_wrist1, self.actual_joint_wrist2, self.actual_joint_wrist3
        ]
        
    def validate_pose_io_condition(self, btn_name, var_name, is_reverse):
        """
        각 버튼(포즈) 및 이동할 위치(var_name)별로 I/O 안전 조건을 검사합니다.
        조건을 만족하면 True, 위반하면 경고창을 띄우고 False를 반환합니다.
        """
        if self.io_module is None:
            return False
            
        inputs = self.io_module.Read_Input_Data()
        if not inputs or len(inputs) < 48:
            return False

        # =====================================================================
        # 🚧 [여기에 I/O 조건 작성] btn_name과 is_reverse(진입/복귀)에 따라 분기
        # =====================================================================
        
        # 1. HEAD 모드 조건
        if self.system_mode == "HEAD":
            if self.ui.cell_1_btn.isChecked():
                if btn_name in ["waypoint_pose_btn", "pose_1_btn", "pose_2_btn", "pose_3_btn", "pose_4_btn", "pose_5_btn", "pose_6_btn", "pose_7_btn", "pose_8_btn", "pose_9_btn", "pose_10_btn", "pose_11_btn"]:
                    if not is_reverse: 
                        if inputs[0] == 0 or inputs[2] == 0 or inputs[4] == 0:
                            self.show_message("진입 불가", "Cell 1 지그가 전진 및 클램프 되지 않았습니다.", QMessageBox.Warning)
                            return False
                    else: 
                        if inputs[0] == 0 or inputs[2] == 0 or inputs[4] == 0:
                            self.show_message("진입 불가", "Cell 1 지그가 전진 및 클램프 되지 않았습니다.", QMessageBox.Warning)
                            return False
                        
            elif self.ui.cell_2_btn.isChecked():
                # [Cell 2] 검사 로직
                if btn_name in ["waypoint_pose_btn", "pose_1_btn", "pose_2_btn", "pose_3_btn", "pose_4_btn", "pose_5_btn", "pose_6_btn", "pose_7_btn", "pose_8_btn", "pose_9_btn", "pose_10_btn", "pose_11_btn"]:
                    if not is_reverse: 
                        if inputs[8] == 0 or inputs[10] == 0 or inputs[12] == 0:
                            self.show_message("진입 불가", "Cell 2 지그가 전진 및 클램프 되지 않았습니다.", QMessageBox.Warning)
                            return False
                    else: 
                        if inputs[8] == 0 or inputs[10] == 0 or inputs[12] == 0:
                            self.show_message("진입 불가", "Cell 2 지그가 전진 및 클램프 되지 않았습니다.", QMessageBox.Warning)
                            return False

        # 2. BACK 모드 조건
        elif self.system_mode == "BACK":
            if btn_name in ["waypoint_pose_btn"]:
                if not is_reverse: 
                    if inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "제품이 투입되어 있지 않거나, 지그가 고정되지 않았습니다.", QMessageBox.Warning)
                        return False
                else: 
                    if inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "제품이 투입되어 있지 않거나, 지그가 고정되지 않았습니다", QMessageBox.Warning)
                        return False
            elif btn_name in ["pose_1_btn", "pose_2_btn", "pose_3_btn", "pose_4_btn"]:
                if not is_reverse: 
                    # [진입할 때] 예: 지그 전진(MDI 0) 및 클램프 1, 2(MDI 2, 4) 확인
                    if inputs[1] == 0 or inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "회전 지그가 복귀 위치가 아닙니다.", QMessageBox.Warning)
                        return False
                else: 
                    # [빠져나올 때] (필요 시 작성)
                    if inputs[1] == 0 or inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "회전 지그가 복귀 위치가 아닙니다.", QMessageBox.Warning)
                        return False
            elif btn_name in ["pose_5_btn", "pose_6_btn", "pose_7_btn", "pose_8_btn"]:
                if not is_reverse: 
                    # [진입할 때] 예: 지그 전진(MDI 0) 및 클램프 1, 2(MDI 2, 4) 확인
                    if inputs[0] == 0 or inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[12] == 0 or inputs[14] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "회전 지그가 회전 위치가 아니거나 클램프가 고정되지 않았습니다.", QMessageBox.Warning)
                        return False
                else: 
                    # [빠져나올 때] (필요 시 작성)
                    if inputs[0] == 0 or inputs[2] == 0 or inputs[4] == 0 or inputs[6] == 0 or inputs[10] == 0 or inputs[12] == 0 or inputs[14] == 0 or inputs[16] == 0 or inputs[17] == 0:
                        self.show_message("진입 불가", "회전 지그가 회전 위치가 아니거나 클램프가 고정되지 않았습니다.", QMessageBox.Warning)
                        return False

        # 명시된 조건이 없거나, 검사를 무사히 통과하면 True 반환 (이동 허가)
        return True

    def move_to_variable(self, var_name, btn_name="", is_reverse=False):
        """로봇에게 변수명 좌표를 요청하고 변수명 접두사에 따라 J 또는 L 이동을 수행합니다."""
        
        # ★ 이동 전 I/O 안전 조건 검사 통과 못하면 이동 취소
        if not self.validate_pose_io_condition(btn_name, var_name, is_reverse):
            return False
            
        try:
            raw_coords = self.robot_29999.get_variable(var_name)
            
            # [안전 검증 1] "Stopping task" 같은 쓰레기 값이 오면 걸러내고 파이썬 리스트로 변환
            coords = None
            if isinstance(raw_coords, str):
                try:
                    coords = ast.literal_eval(raw_coords)
                except Exception:
                    pass
            elif isinstance(raw_coords, list):
                coords = raw_coords

            # [안전 검증 2] 정상적인 6축 좌표 리스트인지 확인
            if isinstance(coords, list) and len(coords) == 6:
                print(f"[MOVE] -> 목적지: '{var_name}' / 좌표 수신 정상: {coords}")
                
                if var_name.startswith("J_"):
                    success = self.send_moveJ_and_wait(coords)
                else:
                    success = self.send_moveL_and_wait(coords)
                
                if success:
                    print(f"[MOVE] '{var_name}' 도착 완료.")
                    return True
                else:
                    print(f"[MOVE ERROR] '{var_name}' 이동 중단 또는 실패.")
                    return False
            else:
                print(f"[ERROR] 로봇 수신 데이터 불량: {raw_coords}")
                self.show_message("좌표 읽기 실패", f"로봇에서 유효한 좌표를 읽지 못했습니다.<br>수신값: {raw_coords}", QMessageBox.Warning)
                return False
                
        except Exception as e:
             print(f"[ERROR] {var_name} 이동 실패: {e}")
             return False
         
    def send_moveL(self, pose, a=1.2, v=0.5):
        # L 이동 시에는 반드시 [x,y,z,rx,ry,rz] 포맷을 사용해야 합니다.
        pose_str = f"[{pose[0]}, {pose[1]}, {pose[2]}, {pose[3]}, {pose[4]}, {pose[5]}]"
        cmd = f"movel({pose_str}, a={a}, v={v})\n"
        script = f"def m():\n    {cmd}end\n"
        self.robot_30001.send_command(script)

    def send_moveJ(self, pose, a=1.4, v=1.1):
        # J 이동 시에는 [j1,j2,j3,j4,j5,j6] 포맷을 사용합니다.
        joint_str = f"[{pose[0]}, {pose[1]}, {pose[2]}, {pose[3]}, {pose[4]}, {pose[5]}]"
        cmd = f"movej({joint_str}, a={a}, v={v})\n"
        script = f"def m():\n    {cmd}end\n"
        self.robot_30001.send_command(script)

    # ★ [PATCH] 수동 이동 도착 판정 설정
    MANUAL_JOINT_TOL  = 0.02    # [rad] moveJ 도착 판정 (관절별 최대 오차)
    MANUAL_TCP_TOL    = 0.003   # [m]   moveL 도착 판정 (XYZ 거리)
    MANUAL_VERIFY_TCP = True    # moveL 목표 변수와 30001 TCP 좌표의 단위/좌표계가 다르면 False로

    def send_moveL_and_wait(self, target_pose, a=1.2, v=0.5, timeout=20.0):
        if getattr(self, 'auto_mode', False) or self.is_alarm_state():
            return False
        print(f"[INFO] moveL_wait started")
        self._manual_abort = False
        self.send_moveL(target_pose, a, v)
        tcp = target_pose if self.MANUAL_VERIFY_TCP else None
        return self._wait_for_motion_complete(target_tcp=tcp, timeout=timeout)

    def send_moveJ_and_wait(self, target_pose_j, a=1.4, v=1.1, timeout=20.0):
        if getattr(self, 'auto_mode', False) or self.is_alarm_state():
            return False
        print(f"[INFO] moveJ_wait started")
        self._manual_abort = False
        self.send_moveJ(target_pose_j, a, v)
        return self._wait_for_motion_complete(target_joints=target_pose_j, timeout=timeout)

    def _motion_target_error(self, target_joints=None, target_tcp=None):
        """(도착 여부, 오차 설명 문자열) 반환. 목표가 없으면 (None, '')"""
        if target_joints:
            cur = self.get_current_joints()
            err = max(abs(c - t) for c, t in zip(cur, target_joints))
            return err <= self.MANUAL_JOINT_TOL, f"관절 최대오차 {err:.4f} rad"
        if target_tcp:
            cur = getattr(self, 'actual_tcp', None)
            if not cur:
                return False, "TCP 좌표 없음"
            dist = sum((c - t) ** 2 for c, t in zip(cur[:3], target_tcp[:3])) ** 0.5
            return dist <= self.MANUAL_TCP_TOL, f"TCP 거리오차 {dist * 1000:.1f} mm"
        return None, ""

    def _pump_state_updates(self, duration=0.3):
        """GUI 스레드에서 대기하는 동안 poll 타이머가 돌아 관절/TCP 값이 갱신되도록 이벤트 처리"""
        end_t = time.time() + duration
        while time.time() < end_t:
            QApplication.processEvents()
            time.sleep(0.05)

    def _wait_for_motion_complete(self, target_joints=None, target_tcp=None, timeout=20.0):
        """
        ★ [PATCH] 이동 완료 판정 강화
          - 2초 내 모션 시작 미감지: 이미 목표 위치가 아니면 '실패' (기존: 무조건 '도착' 처리)
          - 모션 정지 후 목표 위치 검증: 정지 버튼/보호정지로 중간에 멈춘 경우 '실패'
            (기존: running=False면 무조건 '도착' → 다음 웨이포인트를 전송해 경유점을 건너뜀)
          - 정지 버튼 플래그(_manual_abort) 확인
        """
        start_time = time.time()
        motion_started = False

        at_target, info = self._motion_target_error(target_joints, target_tcp)
        if at_target:
            print(f"[INFO] 이미 목적지에 위치함. ({info})")
            return True

        while True:
            # ★ 핵심: 무한 루프 중에도 UI가 멈추지 않도록 이벤트 강제 펌핑
            QApplication.processEvents()

            if getattr(self, '_manual_abort', False):
                print("[INFO] 정지 버튼 → 이동 대기 중단 (실패 처리)")
                return False

            # 안전 검사 (수동 모드 해제 시, 알람 시 즉시 중단)
            if getattr(self, 'auto_mode', False) or self.is_alarm_state():
                print("[INFO] 작업 모드 변경 또는 알람 -> 이동 대기 취소")
                self.robot_29999.robot_stop()
                return False

            if getattr(self, 'robot_disconnected', True):
                print("[ERROR] 로봇 연결 끊김 -> 이동 대기 취소")
                return False
                
            # 일시 정지 시 무한 대기 (이때도 UI는 살려둠)
            if not self.pause_event.is_set():
                print("[INFO] 이동 중 일시 정지됨...")
                while not self.pause_event.is_set():
                    QApplication.processEvents()
                    if getattr(self, '_manual_abort', False):
                        return False
                    time.sleep(0.1)
                print("[INFO] 일시 정지 해제 -> 이동 재개")
                start_time = time.time()
                
            if time.time() - start_time > timeout:
                print(f"[ERROR] 이동 대기 시간 초과 ({timeout}s)")
                self.robot_29999.robot_stop()
                return False
                
            running = getattr(self, 'is_task_running', False)
            
            if not motion_started:
                if running:
                    motion_started = True
                    print("[INFO] 로봇 모션 시작됨.")
                elif time.time() - start_time > 2.0:
                    self._pump_state_updates(0.3)
                    at_target, info = self._motion_target_error(target_joints, target_tcp)
                    if at_target:
                        print(f"[INFO] 짧은 이동으로 시작 미감지, 목표 위치 확인됨 ({info})")
                        return True
                    print(f"[ERROR] 모션 시작 미감지 - 이동 명령이 실행되지 않았습니다. ({info})")
                    return False
            else:
                if not running:
                    # 관절 진동 안정화 + 최신 상태 수신
                    self._pump_state_updates(0.3)
                    at_target, info = self._motion_target_error(target_joints, target_tcp)
                    if at_target is None:
                        print("[INFO] ✓ 모션 정지 확인 (목표 좌표 검증 생략)")
                        return True
                    if at_target:
                        print(f"[INFO] ✓ 모션 정지(도착) 확인 완료. ({info})")
                        return True
                    print(f"[ERROR] 목표 도달 전에 모션이 정지했습니다. ({info})")
                    return False
                    
            time.sleep(0.05)
            
if __name__ == "__main__":
    def qt_excepthook(exc_type, exc_value, exc_tb):
        print("[UNCAUGHT EXCEPTION]")
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = qt_excepthook
    app = QApplication(sys.argv)
    window = BOLT()
    window.showMaximized()
    sys.exit(app.exec_())
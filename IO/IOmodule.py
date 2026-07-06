from PyQt5.QtCore import QThread
from pyModbusTCP.client import ModbusClient
from queue import Queue
import time

class IO_Module_Class(QThread):
    def __init__(self, IP, Port):
        super().__init__()
        # Lock은 불필요하므로 제거됨 (Queue가 스레드 안전을 보장)
        self.Send_que = Queue()
        self.stop_signal = False
        
        # 데이터 저장 공간 (초기값 0으로 채움)
        self.Read_DI = [0] * 48
        self.Read_DO = [0] * 48

        # auto_open=True 이므로 수동으로 open() 할 필요 없이 알아서 연결 관리됨
        self.IO_Module = ModbusClient(host=IP, port=Port, timeout=1.0, auto_open=True)
        self.Input_Address = 0x07D0  # 2000
        self.Output_Address = 0x07D0 # 2000

    def Read_Input_Data(self):
        """MDI 리스트 반환 (메인 스레드에서 UI 업데이트 시 호출)"""
        return self.Read_DI

    def Read_Output_Data(self):
        """MDO 리스트 반환 (메인 스레드에서 UI 업데이트 시 호출)"""
        return self.Read_DO

    def Write_DO_Data(self, index, bit):
        """
        메인 스레드에서 호출하여 큐에 명령을 적재함.
        (함수명이 Date로 되어 있으나, 기존 호환성을 위해 유지)
        """
        def command():
            # auto_open=True이므로 바로 write를 시도하면 알아서 연결 후 전송함
            result = self.IO_Module.write_single_coil(self.Output_Address + index, bit)
            if not result:
                print(f"[IO Write Fail] Index: {index}, Bit: {bit}")
        return command

    def run(self):
        print("[IO Thread] 백그라운드 통신 시작됨")
        
        while not self.stop_signal:
            try:
                # 1. 쓰기 명령 큐 처리 (모인 명령들을 먼저 쏴줌)
                while not self.Send_que.empty():
                    cmd = self.Send_que.get()
                    cmd() # 생성된 write_single_coil 실행
                
                # 2. MDI (입력) 데이터 읽기
                di_data = self.IO_Module.read_discrete_inputs(self.Input_Address, 48)
                if di_data: 
                    self.Read_DI = di_data
                
                # 3. MDO (출력) 상태 읽기 (피드백용)
                do_data = self.IO_Module.read_coils(self.Output_Address, 48)
                if do_data: 
                    self.Read_DO = do_data
                    
            except Exception as e:
                print(f"[IO Thread Error] 통신 예외 발생: {e}")
                
            # CPU 점유율 방지 (초당 50회 통신, 현장 설비에 충분히 빠름)
            time.sleep(0.02) 

        # 스레드 종료 시 소켓 닫기
        self.IO_Module.close()
        print("[IO Thread] 종료됨")
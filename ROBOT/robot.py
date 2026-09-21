import struct
import socket
import select
import time
import queue
import threading

from pyModbusTCP.server import ModbusServer
from pyModbusTCP.client import ModbusClient


HOST = "192.168.1.101"
PORT1 = 29999
PORT2 = 30001
PORT3 = 502

PC_IP = "192.168.1.100"
PC_PORT = 30010

DEFAULT_TIMEOUT = 10.0
MESSAGE_TYPE_ROBOT_STATE = 16
MESSAGE_TYPE_ROBOT_MESSAGE = 20

FMT_HEADER = 'IB'
FMT_ROBOT_MODE = 'IBQ???????BBdddB??I'
FMT_JOINT_HEADER = 'IB'     
FMT_JOINT_DATA = 'dddiiiffffBI'
FMT_CARTESIAN = 'IBdddddddddddd'
FMT_CONFIG = 'IB'+'dd'*6+'dd'*6+'ddddd'+'d'*6+'d'*6+'d'*6+'d'*6+'IIIBBBB'
FMT_MASTERBOARD = 'IBIIBBBdddBBBdddffffB???B'
FMT_ADDITIONAL = 'IB????B'
FMT_TOOL = 'IBBBddfBffB'
FMT_SAFETY = 'IBIbBdddd'
FMT_TOOL_COMM = 'IB?III?Bff'

class RobotDataConfig():
    def __init__(self):
        # 1. 관절 앞부분 이름들
        self.names_pre = [
            'total_message_len', 'total_message_type',
            'mode_sub_len', 'mode_sub_type', 'timestamp', 'reserved_1', 'reserved_2',
            'is_robot_power_on', 'is_emergency_stopped', 'is_robot_protective_stopped',
            'is_task_running', 'is_task_paused', 'robot_mode', 'robot_control_mode',
            'target_speed_fraction', 'speed_scaling', 'target_speed_fraction_limit',
            'get_robot_speed_mode', 'reserved_3', 'is_in_package_mode', 'reserved_4',
            
            'joint_sub_len', 'joint_sub_type' # 관절 헤더까지 포함
        ]

        # 2. 관절 반복 데이터 이름들 (6번 반복될 대상)
        self.names_joint = [
            'actual_joint', 'target_joint', 'actual_velocity', 
            'joint_reserved_1', 'joint_reserved_2', 'joint_reserved_3',
            'current', 'voltage', 'temperature', 'torques', 'mode', 'joint_reserved_4'
        ]

        # 3. 관절 뒷부분 이름들
        self.names_post = [
            'cartesial_sub_len', 'cartesial_sub_type',
            'tcp_x', 'tcp_y', 'tcp_z', 'rot_x', 'rot_y', 'rot_z',
            'offset_px', 'offset_py', 'offset_pz', 'offset_rotx', 'offset_roty', 'offset_rotz',
            
            # ★ [PATCH] Config 섹션은 배열 필드가 많으므로 (이름, 개수) 튜플로 매핑합니다.
            #   기존에는 'dd'*6 같은 배열(60개 값)을 이름 20개로 1:1 매핑해서
            #   이후 Masterboard 섹션의 safety_mode 등이 전부 밀려 있었습니다.
            'configuration_sub_len', 'configuration_sub_type',
            ('joint_limit_min_max', 12),     # [min0, max0, min1, max1, ...]
            ('joint_max_vel_acc', 12),       # [vel0, acc0, vel1, acc1, ...]
            'default_velocity_joint', 'default_acc_joint', 'default_tool_velocity', 'default_tool_acc',
            'internal_use',
            ('dh_a', 6), ('dh_d', 6), ('dh_alpha', 6), ('reserved_cfg', 6),
            'masterboard_version', 'control_box_type', 'robot_type', 'robot_structure', 'tool_io_type',
            'reserved_cfg2', 'reserved_cfg3',
            'masterboard_sub_len', 'masterboard_sub_type',
            'digital_input_bits', 'digital_output_bits',
            'standard_analog_input_domain0', 'standard_analog_input_domain1', 'tool_analog_input_domain',
            'standard_analog_input_value0', 'standard_analog_input_value1', 'tool_analog_input_value',
            'standard_analog_output_domain0', 'standard_analog_output_domain1', 'tool_analog_output_domain',
            'standard_analog_output_value0', 'standard_analog_output_value1', 'tool_analog_output_value',
            'masterrbord_temperature', 'robot_voltage', 'robot_current', 'io_current',
            'safety_mode', 'is_robot_in_reduced_mode', 'operational_mode_selector_input',
            'threeposition_enabling_device_input', 'internal_use_mb',
            'additional_sub_len', 'additional_sub_type',
            'is_freedrive_button_pressed', 'reserved_add', 'is_freedrive_io_enabled', 'is_dynamic_collision_detect_enabled', 'reserved_add2',
            'tool_sub_len', 'tool_sub_type',
            'tool_analog_output_domain', 'tool_analog_input_domain', 'tool_analog_output_value', 'tool_analog_input_value',
            'tool_voltage', 'tool_output_voltage', 'tool_current', 'tool_temperature', 'tool_mode',
            'safe_sub_len', 'safe_sub_type',
            'safety_crc_num', 'safety_operational_mode', 'reserved_safe',
            'current_elbow_position_x', 'current_elbow_position_y', 'current_elbow_position_z', 'elbow_radius',
            'tool_comm_sub_len', 'tool_comm_sub_type',
            'is_enable', 'baudrate', 'parity', 'stopbits', 'tci_modbus_status', 'tci_usage', 'reserved_tc1', 'reserved_tc2'
        ]
        
        # [핵심] 전체 포맷 문자열 조합
        self.fmt = (
            '>' +
            FMT_HEADER +
            FMT_ROBOT_MODE +
            FMT_JOINT_HEADER + (FMT_JOINT_DATA * 6) + # 관절 데이터 6번 반복
            FMT_CARTESIAN +
            FMT_CONFIG +
            FMT_MASTERBOARD +
            FMT_ADDITIONAL +
            FMT_TOOL +
            FMT_SAFETY +
            FMT_TOOL_COMM
        )

        # ★ [PATCH] 포맷 필드 수와 이름 수가 다르면 즉시 알 수 있도록 검증
        n_fields = len(struct.unpack(self.fmt, bytes(struct.calcsize(self.fmt))))
        n_names = (len(self.names_pre) + len(self.names_joint) * 6 +
                   sum(e[1] if isinstance(e, tuple) else 1 for e in self.names_post))
        if n_fields != n_names:
            raise ValueError(f"[RobotDataConfig] 필드 수({n_fields})와 이름 수({n_names}) 불일치")
# ----------- RobotData for Elite CS robots -------------

class RobotHeader():
    __slots__ = ['type', 'size',]
    @staticmethod
    def unpack(buf):
        rmd = RobotHeader()
        (rmd.size, rmd.type) = struct.unpack_from('>iB', buf)
        return rmd

class RobotData():
    @staticmethod
    def unpack(buf, config):
        data = RobotData()
        
        try:
            # 1. 전체 데이터 한 번에 언팩 (포맷 길이 검증 포함)
            unpacked = struct.unpack(config.fmt, buf)
            
            # 2. 이터레이터 생성 (하나씩 뽑아쓰기 위해)
            it = iter(unpacked)
            
            # [A] 관절 앞부분 매핑
            for name in config.names_pre:
                setattr(data, name, next(it))
            
            # [B] 관절 데이터 매핑 (리스트로 초기화 후 6번 반복)
            # 먼저 관절 변수들을 빈 리스트로 생성
            for name in config.names_joint:
                setattr(data, name, [])
                
            # 6번 반복하며 데이터 채우기
            for _ in range(6):
                for name in config.names_joint:
                    val = next(it)
                    getattr(data, name).append(val)
            
            # [C] 관절 뒷부분 매핑 (나머지 전부) - (이름, 개수) 튜플은 리스트로 저장
            for entry in config.names_post:
                if isinstance(entry, tuple):
                    name, cnt = entry
                    setattr(data, name, [next(it) for _ in range(cnt)])
                else:
                    setattr(data, entry, next(it))
                
            return data

        except struct.error:
            # 버퍼 크기가 부족하거나 포맷이 안 맞을 경우
            return None
        except StopIteration:
            # 데이터가 중간에 끊겼을 경우
            return None
    
class AlarmData:
    def __init__(self, code=None, sub=None, level=None, msg=None):
        self.code = code
        self.sub = sub
        self.level = level
        self.msg = msg
        self.timestamp = time.time()
        self.active = True
    
class ReadAlarm():
    @staticmethod
    def unpack(buf):
        data_length = struct.unpack(">i", buf[0:4])[0]
        data, buf = buf[0:data_length], buf[data_length:]
        msg_type = data[14]

        if msg_type == 10:
            b = bytearray(data[23:data_length-1])
            msg = b.decode()
            
            return AlarmData(msg=msg)

        if msg_type == 6:
            error_code = struct.unpack(">i", data[15:19])[0]
            sub_error_code = struct.unpack(">i", data[19:23])[0]
            level = struct.unpack(">i", data[23:27])[0]
            
            return AlarmData(code=error_code, sub=sub_error_code, level=level)

        return None
        
class Robot_30001():
    MAX_PACKET_SIZE = 65536

    def __init__(self, ip, port2) -> None:
        config = RobotDataConfig()
        self.__data_config = config
        self.ip = ip
        self.port2 = port2
        self.__sock = None
        self.__buf = b""
        
        self.alarm_queue = queue.Queue()

    def connect_30001(self):
        self.close()   # ★ [PATCH] 재연결 시 기존 소켓 정리
        try:
            self.__sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.__sock.settimeout(0.5)
            self.__sock.connect((self.ip, self.port2))
            self.__sock.settimeout(10.0) 
            print(f"Connected to {self.ip} on port {self.port2}")
            self.__buf = b""
            return self.__sock
        except Exception as e:
            print(f"Error connecting to {self.ip} on port {self.port2}: {e}")
            self.close()
            return None 

    def close(self):
        """★ [PATCH] 외부(BOLT)에서 안전하게 소켓을 닫기 위한 메서드.
        기존 BOLT 코드의 `self.robot_30001.__sock.close()`는 이름 맹글링 때문에
        실제로는 `_BOLT__sock`을 찾다가 실패하여 소켓이 계속 누수되었습니다."""
        sock = getattr(self, '_Robot_30001__sock', None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        self.__sock = None
        self.__buf = b""
        
    def disconnect_30001(self):
        self.close()

    def get_data(self):
        return self.__recv()

    def __recv(self):
        # 1. 소켓에 쌓인 데이터를 '몽땅' 읽어서 버퍼에 넣습니다. (타임아웃 대기 X)
        try:
            self.__read_socket_no_wait()
        except Exception:
            return None

        # 2. 버퍼에서 패킷 파싱 (기존 로직 유지, 최신 패킷만 남김)
        last_valid_data = None

        while len(self.__buf) >= 5:
            try:
                head = RobotHeader.unpack(self.__buf)
            except:
                # 헤더 파싱 실패 시 버퍼 초기화 (오류 방지)
                self.__buf = b""
                break

            # ★ [PATCH] 깨진 헤더(size<=0 또는 비정상적으로 큰 값)면 버퍼를 버리고 재동기화.
            #   기존에는 size=0일 때 버퍼가 줄지 않아 GUI 스레드에서 무한루프가 발생할 수 있었습니다.
            if head.size < 5 or head.size > self.MAX_PACKET_SIZE:
                self.__buf = b""
                break

            if len(self.__buf) < head.size:
                break

            # 패킷 추출
            payload = self.__buf[:head.size]
            self.__buf = self.__buf[head.size:] # 버퍼에서 제거

            # 알람 패킷 처리
            if head.type == MESSAGE_TYPE_ROBOT_MESSAGE:
                try:
                    alarm = ReadAlarm.unpack(payload)
                    if alarm:
                        self.alarm_queue.put(alarm)
                except:
                    pass
                continue

            # 상태 패킷 처리 (가장 최신 것만 last_valid_data에 저장)
            if head.type == MESSAGE_TYPE_ROBOT_STATE:
                try:
                    last_valid_data = RobotData.unpack(payload, self.__data_config)
                except:
                    pass

        # 반복문이 끝나면 가장 최신 데이터만 반환 (랙 방지 핵심)
        return last_valid_data

    def __read_socket_no_wait(self):
        """
        타임아웃 없이(0초), 현재 소켓 버퍼에 있는 모든 데이터를 읽어옵니다.
        """
        if self.__sock is None:
            raise ConnectionError("30001 not connected")
        while True:
            # select 타임아웃 0 -> 데이터가 있으면 즉시 True, 없으면 즉시 False
            readable, _, _ = select.select([self.__sock], [], [], 0)
            
            if not readable:
                break # 읽을 데이터가 없으면 루프 종료
            
            try:
                more = self.__sock.recv(4096)
                if not more:
                    raise ConnectionError("Socket closed")
                self.__buf += more
            except BlockingIOError:
                break # Non-blocking 소켓인 경우 대기 없이 종료
            except Exception:
                break
    
    def send_command(self, command):
        try:
            if self.__sock is None:
                raise RuntimeError("socket is not connected")
            self.__sock.sendall(f"{command}\n".encode("utf-8"))
        except Exception as e:
            print(f"Error sending command: {e}")
            
class AlarmManager:
    def __init__(self):
        self.active_alarms = {}  # key = (code, sub, msg)

    def process(self, alarm):
        key = (alarm.code, alarm.sub, alarm.msg)
        
        if key in self.active_alarms:
            return False

        self.active_alarms[key] = alarm
        print("================================")
        print("[ALARM TRIGGERED]")

        if alarm.msg:
            print(f"[ALARM MSG] {alarm.msg}")
        else:
            print(f"[ALARM CODE] E{alarm.code} S{alarm.sub} (level={alarm.level})")
        print("================================")

        return True

    def clear(self, key):
        if key in self.active_alarms:
            self.active_alarms[key].active = False
            del self.active_alarms[key]

#### robot_mode ####
'''
ROBOT_MODE_DISCONNECTED = 0
ROBOT_MODE_CONFIRM_SAFETY = 1
ROBOT_MODE_BOOTING = 2
ROBOT_MODE_POWER_OFF = 3
ROBOT_MODE_POWER_ON = 4
ROBOT_MODE_IDLE = 5
ROBOT_MODE_BACKDRIVE = 6
ROBOT_MODE_RUNNING = 7
ROBOT_MODE_UPDATING_FIRMWARE = 8
ROBOT_MODE_WAITING_CALIBRATION = 9
'''


#### safety_mode ####
'''
Mode
SAFETY_MODE_NORMAL = 1
SAFETY_MODE_REDUCED = 2
SAFETY_MODE_PROTECTIVE_STOP = 3
SAFETY_MODE_RECOVERY = 4
SAFETY_MODE_SAFEGUARD_STOP = 5
SAFETY_MODE_SYSTEM_EMERGENCY_STOP = 6
SAFETY_MODE_ROBOT_EMERGENCY_STOP = 7
SAFETY_MODE_VIOLATION = 8
SAFETY_MODE_FAULT = 9
SAFETY_MODE_VALIDATE_JOINT_ID = 10
SAFETY_MODE_UNDEFINED_SAFETY_MODE = 11
SAFETY_MODE_AUTOMATIC_MODE_SAFEGUARD_STOP = 12
SAFETY_MODE_SYSTEM_THREE_POSITION_ENABLING_STOP = 13

'''

#### 29999 port command ####
'''
brakeRelease
closeSafetyDialog
echo
help
help command1 command2 command3
log -a this is a test log message
popup -s Hello
popup -c
quit
reboot
robot -t
robot -s
robotControl -on
robotControl -off
robotMode
shutdown
status
usage shutdown robotControl play
version
unlockProtectiveStop
configuration -p
location/file_name.configuration
configuration -s
pause
play
safety -s
safety -m
safety -r
speed
speed -v 50
stop
task -p location/file_name.task
task -s
task -r
task -ss
remoteControl -status
remoteControl -s
remoteControl -on
remoteControl -off
variable -set variable value
variable -get variable
'''

class Robot_29999():
    """
    ★ [PATCH] 29999 대시보드 소켓은 GUI 스레드, 작업 스레드(최대 2개), 초기화/퍼지/리셋 스레드가
    동시에 사용합니다. 기존에는 락이 없고 recv(4096) 1회로 응답을 받아서
      - 다른 스레드의 응답을 가져가거나 (예: 변수 읽기에 "Stopping task"가 돌아옴)
      - 늦게 온 응답이 버퍼에 남아 이후 모든 명령이 한 칸씩 밀리는
    문제가 있었습니다. 아래 구현은
      1) RLock으로 '송신 + 수신'을 원자적으로 묶고
      2) 송신 전에 남아 있는 늦은 응답을 버리고
      3) 줄바꿈(\n)까지 읽어 응답 경계를 맞추며
      4) 실패 시 소켓을 닫아 다음 호출에서 자동 재연결되게 합니다.
    """
    DEFAULT_TIMEOUT = 2.0      # 기존 10초 → GUI 스레드 호출 시 프리징 최소화
    MULTILINE_WAIT = 0.05      # status 등 여러 줄 응답의 추가 수신 대기

    def __init__(self, ip, port1):
        self.sock = None
        self.ip = ip
        self.port1 = port1
        self._lock = threading.RLock()
        self._var_cache = {}   # J_home, 웨이포인트 등 거의 변하지 않는 배열 변수 캐시

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def close(self):
        with self._lock:
            self._close()

    def connect_29999(self):
        with self._lock:
            self._close()
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(0.5)  # 타임아웃 설정 (중요)
                self.sock.connect((self.ip, self.port1))
                print(f"Connected to {self.ip} on port {self.port1}")
                # 접속 배너가 있으면 버림 (없으면 0.5초 후 통과)
                try:
                    self.sock.recv(4096)
                except Exception:
                    pass
                self.sock.settimeout(self.DEFAULT_TIMEOUT)
                return self.sock
            except Exception as e:
                print(f"Error connecting to {self.ip} on port {self.port1}: {e}")
                self._close()   # ★ 실패한 소켓을 남겨두면 자동 재연결이 영원히 안 됨
                return None

    def _drain(self):
        """송신 전에 소켓에 남아 있는 늦은 응답을 모두 버립니다."""
        self.sock.setblocking(False)
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("29999 closed by peer")
                print(f"[29999] 남아 있던 응답 폐기: {chunk!r}")
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            if self.sock is not None:
                self.sock.setblocking(True)

    def send_command_29999(self, command, multiline=False, timeout=None):
        timeout = self.DEFAULT_TIMEOUT if timeout is None else timeout
        with self._lock:
            try:
                if self.sock is None:
                    if self.connect_29999() is None:
                        print("[WARN] 29999 not connected; cannot send command")
                        return None
                self._drain()
                self.sock.settimeout(timeout)
                self.sock.sendall(f"{command}\n".encode("utf-8"))

                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("29999 closed by peer")
                    buf += chunk

                if multiline:
                    # status 처럼 여러 줄로 오는 응답은 짧게 추가 수신
                    self.sock.settimeout(self.MULTILINE_WAIT)
                    try:
                        while True:
                            chunk = self.sock.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                    except socket.timeout:
                        pass
                    finally:
                        if self.sock is not None:
                            self.sock.settimeout(timeout)

                return buf.decode("utf-8", errors="replace").strip()
            except Exception as e:
                print(f"Error sending command '{command}': {e}")
                self._close()
                return None

    def get_variable_cached(self, var_name):
        """
        ★ [PATCH] J_home, 웨이포인트처럼 운전 중 바뀌지 않는 배열 변수 전용.
        정상적인 리스트를 받은 경우에만 캐시하며, 객체가 재생성(재연결)되면 초기화됩니다.
        티칭펜던트에서 값을 수정했다면 재연결하거나 clear_variable_cache()를 호출하세요.
        """
        cached = self._var_cache.get(var_name)
        if cached is not None:
            return list(cached)
        value = self.get_variable(var_name)
        if isinstance(value, list) and len(value) == 6:
            self._var_cache[var_name] = list(value)
        return value

    def clear_variable_cache(self):
        self._var_cache.clear()

    def disconnect_29999(self):
        self.close()

    def robot_mode(self):
        return self.send_command_29999("robotMode")
    
    def robot_status(self):
        return self.send_command_29999("status")
    
    def robot_remote_mode_chk(self):
        return self.send_command_29999("remoteControl -s")
        
    def robot_remote_mode_on(self):
        return self.send_command_29999("remoteControl -on")
        
    def robot_power_on(self):
        return self.send_command_29999("robotControl -on")

    def robot_power_off(self):
        return self.send_command_29999("robotControl -off")
    
    def robot_brakeRelease(self):
        return self.send_command_29999("brakeRelease")

    def robot_play(self):
        return self.send_command_29999("play")

    def robot_pause(self):
        return self.send_command_29999("pause")

    def robot_stop(self):
        return self.send_command_29999("stop")

    def set_robot_speed(self, speed):
        return self.send_command_29999(f"speed -v {speed}")
    
    def get_robot_speed(self):
        try:
            response = self.send_command_29999("speed")
            if response is None:
                return 0
            if ":" in response:
                return int(response.split(":")[-1].strip())
            return int(response.strip())
        except Exception as e:
            print(f"[Speed Check Error] {e}")
            return 0
        
    def set_variable(self, name, value):
        return self.send_command_29999(f"variable -set {name} {value}")
    
    def get_variable(self, var_name):
        """
        로봇 변수 값을 읽어와서 적절한 파이썬 타입으로 자동 변환합니다.
        - 리스트 형태 ("[1.1, 2.2]") -> Python list [1.1, 2.2]
        - 불리언 ("True") -> Python bool True
        - 숫자 ("123", "12.34") -> Python int/float
        - 문자열 -> Python str
        """
        try:
            command = f"variable -get {var_name}"
            response = self.send_command_29999(command)
            
            # 1. 통신 실패 (연결 끊김)
            if response is None: 
                return None 

            # 2. 변수 없음 에러 처리
            if "Error" in response or "undefined" in response or "Can not find" in response:
                return "NOT_FOUND"

            # -----------------------------------------------------------
            # [Type A] 리스트 파싱 로직 (대괄호가 포함된 경우)
            # -----------------------------------------------------------
            if "[" in response and "]" in response:
                try:
                    start_index = response.find("[")
                    end_index = response.find("]")
                    
                    # 대괄호 안의 내용 추출 (예: "0.0, -1.57, 3.14")
                    content = response[start_index+1 : end_index]
                    
                    # 콤마로 분리
                    str_list = content.split(",")
                    result_list = []
                    
                    for s in str_list:
                        s = s.strip()
                        if s: # 빈 문자열이 아니면
                            try:
                                result_list.append(float(s)) # 기본적으로 float 변환 시도
                            except ValueError:
                                result_list.append(s) # 숫자가 아니면 문자열로 저장
                                
                    return result_list # [0.0, -1.57, ...] 반환

                except Exception as e:
                    print(f"[WARN] 리스트 파싱 실패 ({var_name}): {e}")
                    return "NOT_FOUND"

            # -----------------------------------------------------------
            # [Type B] 일반 스칼라 값 파싱 로직 (숫자, 불리언, 문자열)
            # -----------------------------------------------------------
            try:
                # (1) 값 추출: "var_name, value" 형태라면 콤마 뒤만 가져옴
                raw_val = response
                if "," in response:
                    raw_val = response.split(",")[-1].strip()
                else:
                    raw_val = response.strip()
                
                # (2) 불리언(Boolean) 체크
                if raw_val.lower() == "true":
                    return True
                if raw_val.lower() == "false":
                    return False
                
                # (3) 숫자(Int/Float) 체크
                try:
                    return int(raw_val) # 정수 시도
                except ValueError:
                    try:
                        return float(raw_val) # 실수 시도
                    except ValueError:
                        pass # 숫자가 아니면 패스

                # (4) 문자열(String) 따옴표 제거
                if (raw_val.startswith('"') and raw_val.endswith('"')) or \
                   (raw_val.startswith("'") and raw_val.endswith("'")):
                    return raw_val[1:-1]
                
                # (5) 그 외는 그냥 문자열로 반환
                return raw_val

            except Exception as e:
                print(f"[WARN] 변수 값 파싱 오류 ({response}) -> {e}")
                return "NOT_FOUND"

        except Exception as e:
            print(f"[Get Var Error] {e}")
            return None # 통신 에러
        
    def check_task_running(self):
        response = self.send_command_29999("task -s")
        if response is None:
            return False
        if "is running" in response:
            return True
        return False
    
    def call_task(self, task_path):
        return self.send_command_29999(f"task -p {task_path}")

class Robot_modbus():       
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.client = None
        self.is_running = False

    def connect(self):
        """메인 스레드/연결 스레드에서 명시적으로 호출하는 연결 함수"""
        self.client = ModbusClient(host=self.host, port=self.port,timeout=1.0)
        print(f"[PC Modbus Client] 로봇 서버({self.host}:{self.port}, ID:255)에 코일 통신 연결 시도 중...")
        
        # [수정 2] 이미 열려있으면 닫고 다시 엽니다. (포트 재사용 에러 방지)
        if self.client.is_open:
            self.client.close()
            
        is_open = self.client.open()
        
        if is_open:
            print("[PC Modbus Client] 로봇 서버 접속 성공!")
            self.is_running = True
            return True
        else:
            print("[PC Modbus Client] 로봇 서버 접속 실패")
            return False

    def disconnect(self):
        self.client.close()
        self.is_running = False
        print("[PC Modbus Client] 접속 종료")

    def set_coil(self, address, state):
        safe_bool = False if str(state).strip().lower() in ['0', 'false', 'none', ''] else True
        success = self.client.write_single_coil(address, safe_bool)
        if not success:
            print(f"[Modbus Error] Coil {address} 쓰기 실패")
        return bool(success)   # ★ [PATCH] 호출부에서 실패를 판단할 수 있도록 반환

    def get_all_coils(self, start_address, count) -> list:
        bits = self.client.read_coils(start_address, count)
        if bits:
            return bits
        return []
    
    def get_coil(self, address) -> bool:
        result = self.client.read_coils(address, 1)
        if result and len(result) > 0:
            return result[0]
        return False

if __name__ == "__main__":
    ip = HOST
    # ip = "192.168.2.202"
    port1 = PORT1
    port2 = PORT2
    port3 = PORT3
    
    robot_29999 = Robot_29999(ip, port1)
    robot_30001 = Robot_30001(ip, port2)
    robot_29999.connect_29999()
    robot_30001.connect_30001()
    
    robot_modbus = Robot_modbus(ip, port3)
    robot_modbus.connect()
    print(robot_modbus.get_coil(50))
    robot_modbus.set_coil(60, 1)
    # robot_29999.send_command_29999("remoteControl -on")
    # robot_29999.send_command_29999("robotControl -on")
    # robot_29999.send_command_29999("brakeRelease")
    # robot_29999.send_command_29999("safety -r")
    # robot_29999.send_command_29999("reboot")
    # robot_29999.send_command_29999("play")
    # robot_29999.send_command_29999("pause")
    # robot_29999.send_command_29999("stop")
    # print(f'target_speed_fraction:{data.target_speed_fraction}')
    # print(f'robot_mode:{data.robot_mode}')
    
    # time.sleep(0.1)
    
    # robot_30001.send_command("movel([0.22618, -0.02683, 1.0, -3.14, 0.0, 0.0], a=0.8, v=0.1)")
    
    # print(robot_29999.get_variable("is_home"))
    # print(robot_29999.get_variable("J_home"))
    # print(robot_29999.get_variable("today_workload"))
    # print(robot_29999.send_command_29999("echo"))
    # print(robot_29999.send_command_29999("robotMode"))
    # print(robot_29999.send_command_29999("status"))
    # print(robot_29999.send_command_29999("speed"))
    # print(robot_29999.get_robot_speed())
    # print(robot_29999.check_task_running())
    # print(robot_29999.send_command_29999("variable -set home [1, 1, 1, 1, 1, 1]"))
    
    # alarm_manager = AlarmManager()
    
    # while True:
    #     data = robot_30001.get_data()
    #     if data == None:
    #         # print("Data is None")
    #         continue
        
        # print(f'tcp_x:{data.tcp_x}')
        # print(f'tcp_y:{data.tcp_y}')
        # print(f'tcp_z:{data.tcp_z}')
        # print(f'rot_x:{data.rot_x}')
        # print(f'rot_y:{data.rot_y}')
        # print(f'rot_z:{data.rot_z}')
        
        
        # print(f'actual_joint:{data.actual_joint[0]}')
        # print(f'actual_joint:{data.actual_joint[1]}')
        # print(f'actual_joint:{data.actual_joint[2]}')
        # print(f'actual_joint:{data.actual_joint[3]}')
        # print(f'actual_joint:{data.actual_joint[4]}')
        # print(f'actual_joint:{data.actual_joint[5]}')
        # time.sleep(1.0)

        # print(robot_29999.send_command_29999("robotMode"))
        # print(robot_29999.send_command_29999("status"))
        # print(robot_29999.send_command_29999("speed"))
            
    #     # print(f'is_task_running:{data.is_task_running}')
    #     # print(f'is_task_paused:{data.is_task_paused}')
    #     # print(f'is_emergency_stopped:{data.is_emergency_stopped}')
    #     # print(f'is_robot_protective_stopped:{data.is_robot_protective_stopped}')
    #     # print(f'robot_mode:{data.robot_mode}')
    #     # print(f'safety_mode:{data.safety_mode}')
    
        # time.sleep(1.0)
        
        # try:
        #     # 알람이 있으면 가져옴
        #     alarm = robot_30001.alarm_queue.get_nowait()
            
        #     # 알람이 있을 때만 실행됨
        #     alarm_manager.process(alarm)
            
        # except queue.Empty:
        #     # 큐가 비어있으면(알람이 없으면) 아무것도 안 하고 넘어감
        #     pass
        
        # # CPU 점유율을 낮추기 위해 약간의 딜레이를 주는 것이 좋습니다 (선택사항)
        # time.sleep(0.01)
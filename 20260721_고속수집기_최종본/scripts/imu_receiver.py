#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
IMU 시리얼 수신기 + 수집 세션 관리
================================================================================

Nano 33 BLE Rev2에서 시리얼로 IMU 데이터를 수신하고
beacon_scanner.py의 RSSI 데이터와 타임스탬프 기준으로 병합하여 저장합니다.

수집 시작 전 입력:
  - 장소명
  - 회전 시퀀스 (예: 우90,좌90,180)

출력 CSV 포맷:
  timestamp, ax, ay, az, gx, gy, gz, rotation_hint
  rotation_hint: 해당 타임스탬프에 회전 이벤트가 감지되면 기록

사용법:
  python3 imu_receiver.py [--port /dev/ttyACM0] [--baud 921600]

================================================================================
"""

import os
import sys
import time
import signal
import serial
import serial.tools.list_ports
import threading
import csv
from datetime import datetime
import argparse

# =============================================================================
# 상수
# =============================================================================
DEFAULT_PORT  = "/dev/ttyACM0"
DEFAULT_BAUD  = 921600
OUTPUT_DIR    = "./data"

# 자이로 회전 감지 임계값 (deg/s)
# Z축 기준 회전 감지 (수평 회전)
ROTATION_THRESHOLD_DEG_S = 30.0

# 회전 완료 판정: 연속으로 임계값 이하로 내려오면 회전 종료
ROTATION_END_FRAMES = 5


# =============================================================================
# 회전 시퀀스 입력
# =============================================================================

def input_rotation_sequence() -> list:
    """
    회전 시퀀스를 입력받습니다.
    예시: 우90,좌90,180  →  [('R', 90), ('L', 90), ('U', 180)]
    """
    print()
    print("=" * 60)
    print("  회전 시퀀스 입력")
    print("=" * 60)
    print("  형식: 방향+각도 를 쉼표로 구분")
    print("  방향: 우(R), 좌(L), 180(U)")
    print()
    print("  예시:")
    print("    우90,좌90       → 오른쪽 90도, 왼쪽 90도")
    print("    우90,180,우90   → 오른쪽 90도, 유턴, 오른쪽 90도")
    print("    (없으면 Enter)  → 직선 경로")
    print("=" * 60)

    raw = input("  회전 시퀀스: ").strip()

    if not raw:
        print("  → 직선 경로 (회전 없음)")
        return []

    sequence = []
    tokens = [t.strip() for t in raw.replace('，', ',').split(',')]

    for token in tokens:
        if not token:
            continue
        token_upper = token.upper()

        try:
            if token_upper.startswith('우') or token_upper.startswith('R'):
                angle = int(''.join(filter(str.isdigit, token)))
                sequence.append(('R', angle))
            elif token_upper.startswith('좌') or token_upper.startswith('L'):
                angle = int(''.join(filter(str.isdigit, token)))
                sequence.append(('L', angle))
            elif '180' in token or token_upper.startswith('U'):
                sequence.append(('U', 180))
            else:
                # 숫자만 있으면 각도로 처리 (방향 미지정)
                angle = int(''.join(filter(str.isdigit, token)))
                sequence.append(('?', angle))
        except ValueError:
            print(f"  ⚠ '{token}' 파싱 실패, 건너뜁니다.")

    print()
    print("  입력된 회전 시퀀스:")
    for i, (direction, angle) in enumerate(sequence, 1):
        dir_str = {'R': '오른쪽', 'L': '왼쪽', 'U': '유턴', '?': '미지정'}.get(direction, '?')
        print(f"    {i}번째: {dir_str} {angle}도")

    return sequence


# =============================================================================
# IMU 데이터 버퍼 (스레드 안전)
# =============================================================================

class IMUBuffer:
    def __init__(self):
        self.rows = []
        self.lock = threading.Lock()
        self.ready = False  # READY 신호 수신 여부

    def append(self, row: dict):
        with self.lock:
            self.rows.append(row)

    def flush(self) -> list:
        with self.lock:
            data = self.rows[:]
            self.rows.clear()
            return data


# =============================================================================
# 회전 감지기
# =============================================================================

class RotationDetector:
    """
    자이로 Z축 값으로 회전 이벤트를 감지합니다.
    미리 입력된 회전 시퀀스와 순서대로 매핑합니다.
    """

    def __init__(self, sequence: list):
        self.sequence  = sequence       # [('R', 90), ...]
        self.seq_index = 0              # 현재 몇 번째 회전인지
        self.in_rotation = False
        self.end_counter = 0
        self.current_event = None

    def update(self, gz: float) -> str | None:
        """
        gz: 자이로 Z축 값 (deg/s)
        반환: 회전 이벤트 문자열 or None
        """
        rotating = abs(gz) > ROTATION_THRESHOLD_DEG_S

        if rotating and not self.in_rotation:
            # 회전 시작
            self.in_rotation = True
            self.end_counter = 0

            if self.seq_index < len(self.sequence):
                direction, angle = self.sequence[self.seq_index]
                self.current_event = f"ROT_{direction}{angle}_{self.seq_index + 1}"
                self.seq_index += 1
            else:
                self.current_event = f"ROT_EXTRA_{self.seq_index + 1}"
                self.seq_index += 1

            return self.current_event

        elif not rotating and self.in_rotation:
            self.end_counter += 1
            if self.end_counter >= ROTATION_END_FRAMES:
                self.in_rotation = False
                self.end_counter = 0
                return None

        return None


# =============================================================================
# IMU 시리얼 수신 스레드
# =============================================================================

def imu_reader_thread(port: str, baud: int, buffer: IMUBuffer,
                      detector: RotationDetector, running_flag: list):
    """
    Nano 33 BLE Rev2에서 IMU 데이터를 시리얼로 수신합니다.
    """
    try:
        ser = serial.Serial(port, baud, timeout=1.0)
        print(f"  ✓ 시리얼 연결: {port} ({baud} baud)")
        ser.reset_input_buffer()

        while running_flag[0]:
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                if line.startswith("READY"):
                    buffer.ready = True
                    print(f"  ✓ Nano 33 준비 완료: {line}")
                    continue

                if line.startswith("INFO"):
                    print(f"  {line}")
                    continue

                if line.startswith("ERROR"):
                    print(f"  ❌ {line}")
                    continue

                # IMU 데이터 파싱
                # 포맷: IMU,<timestamp_ms>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
                if line.startswith("IMU,"):
                    parts = line.split(',')
                    if len(parts) == 8:
                        ts_ms = int(parts[1])
                        ax = float(parts[2])
                        ay = float(parts[3])
                        az = float(parts[4])
                        gx = float(parts[5])
                        gy = float(parts[6])
                        gz = float(parts[7])

                        # 회전 이벤트 감지
                        rotation_hint = detector.update(gz) or ""

                        row = {
                            'timestamp'     : datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                            'board_ts_ms'   : ts_ms,
                            'ax'            : ax,
                            'ay'            : ay,
                            'az'            : az,
                            'gx'            : gx,
                            'gy'            : gy,
                            'gz'            : gz,
                            'rotation_hint' : rotation_hint,
                        }
                        buffer.append(row)

            except (ValueError, IndexError):
                pass
            except Exception:
                pass

    except serial.SerialException as e:
        print(f"  ❌ 시리얼 오류: {e}")
    finally:
        if 'ser' in locals():
            ser.close()


# =============================================================================
# CSV 기록기
# =============================================================================

class IMUCSVWriter:
    FIELDNAMES = [
        'timestamp', 'board_ts_ms',
        'ax', 'ay', 'az',
        'gx', 'gy', 'gz',
        'rotation_hint'
    ]

    def __init__(self, filepath: str):
        self.filepath  = filepath
        self.row_count = 0
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        self._file   = open(filepath, 'w', newline='', encoding='utf-8')
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDNAMES)
        self._writer.writeheader()
        self._file.flush()

    def write_rows(self, rows: list):
        for row in rows:
            self._writer.writerow(row)
            self.row_count += 1
        if rows:
            self._file.flush()

    def close(self):
        self._file.close()


# =============================================================================
# 메인
# =============================================================================

def find_nano33_port() -> str | None:
    """Nano 33 BLE 포트 자동 탐색"""
    for port_info in serial.tools.list_ports.comports():
        desc = port_info.description.lower()
        if 'arduino' in desc or 'nano' in desc or 'acm' in port_info.device:
            return port_info.device
    # 기본값
    if os.path.exists(DEFAULT_PORT):
        return DEFAULT_PORT
    return None


def main():
    parser = argparse.ArgumentParser(description='IMU 시리얼 수신기')
    parser.add_argument('--port',  default=None,         help='시리얼 포트 (기본: 자동 탐색)')
    parser.add_argument('--baud',  type=int, default=921600, help='Baud rate')
    parser.add_argument('--output', default=OUTPUT_DIR,  help='출력 디렉토리')
    parser.add_argument('-l', '--location', default=None, help='장소명 (예: 공학관_3층)')
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  IMU 수신기 + 수집 세션 관리")
    print("  Nano 33 BLE Rev2 (BMI270)")
    print("=" * 60)

    # 장소명 (인자 없으면 입력)
    location = args.location
    if not location:
        location = input("\n  장소명 (예: 공학관_3층): ").strip() or "unknown"
    location = location.replace(' ', '_')

    # 회전 시퀀스 없음
    sequence = []

    # 포트 결정
    port = args.port or find_nano33_port()
    if not port:
        print("  ❌ Nano 33 포트를 찾을 수 없습니다.")
        print("     --port /dev/ttyACM0 로 직접 지정하세요.")
        sys.exit(1)

    # 출력 파일
    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"imu_{location}_{ts_str}.csv"
    filepath = os.path.join(args.output, filename)

    print(f"\n  포트   : {port}")
    print(f"  출력   : {filepath}")
    print()

    # 초기화
    buffer   = IMUBuffer()
    detector = RotationDetector(sequence)
    writer   = IMUCSVWriter(filepath)
    running  = [True]

    def sig_handler(s, f):
        running[0] = False

    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # IMU 수신 스레드 시작
    reader = threading.Thread(
        target=imu_reader_thread,
        args=(port, args.baud, buffer, detector, running),
        daemon=True
    )
    reader.start()

    # Nano 33 준비 대기
    print("  Nano 33 초기화 대기 중...")
    for _ in range(50):
        if buffer.ready:
            break
        time.sleep(0.1)

    if not buffer.ready:
        print("  ⚠ Nano 33 READY 신호 미수신 (계속 진행)")

    print()
    print("  ✅ 수집 시작! (종료: Ctrl+C)")
    print("=" * 60)

    sample_count = 0
    try:
        while running[0]:
            rows = buffer.flush()
            if rows:
                writer.write_rows(rows)
                sample_count += len(rows)

            # 상태 출력
            print(f"\r  샘플: {sample_count:6d} | "
                  f"{'수집중...' if running[0] else '종료'}",
                  end="", flush=True)

            time.sleep(0.1)

    except KeyboardInterrupt:
        running[0] = False

    finally:
        # 남은 버퍼 저장
        rows = buffer.flush()
        writer.write_rows(rows)
        writer.close()

        print()
        print()
        print("=" * 60)
        print("  수집 완료")
        print(f"  총 샘플: {writer.row_count}")
        print(f"  저장 파일: {filepath}")
        print("=" * 60)


if __name__ == '__main__':
    main()

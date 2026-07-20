#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
WiFi 2.4GHz 수집기 - ESP32-S3 x3 시리얼 수신 + CSV 저장
================================================================================

ESP32-S3 3개 보드에서 시리얼로 RSSI 데이터를 수신하고
Wide 포맷 CSV로 저장합니다.

출력 포맷 (Wide CSV):
  timestamp, MAC1, MAC2, MAC3, ...
  감지 안 된 AP는 빈칸

사용법:
  python3 wifi_2ghz_receiver.py [--location 공학관_3층]

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
from collections import defaultdict
import argparse

# =============================================================================
# 상수
# =============================================================================
BAUD_RATE  = 921600
OUTPUT_DIR = "./data"

# =============================================================================
# Wide 포맷 CSV 기록기 (스레드 안전)
# =============================================================================

class WideCSVWriter:
    """
    Wide 포맷 CSV 기록기
    헤더: timestamp, MAC1, MAC2, ...
    새 MAC 발견 시 헤더 동적 추가
    """

    def __init__(self, filepath: str):
        self.filepath    = filepath
        self.lock        = threading.Lock()
        self.known_macs  = []
        self.current_ts  = None
        self.current_row = {}
        self.row_count   = 0
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)
        self._file   = open(filepath, 'w+', newline='', encoding='utf-8')
        self._writer = csv.writer(self._file)

    def write(self, ts: str, mac: str, rssi: str):
        with self.lock:
            is_new_mac = mac not in self.known_macs
            if is_new_mac:
                self.known_macs.append(mac)
                self._rewrite_header()

            # 0.1초 단위로 잘라서 비교 (보드별 타이밍 차이 흡수)
            ts_100ms  = ts[:21]  # 'YYYY-MM-DD HH:MM:SS.X'
            cur_100ms = self.current_ts[:21] if self.current_ts else None

            if ts_100ms == cur_100ms:
                self.current_row[mac] = rssi
            else:
                if self.current_ts is not None:
                    self._flush_row()
                self.current_ts  = ts
                self.current_row = {mac: rssi}

    def _rewrite_header(self):
        self._file.flush()
        self._file.seek(0)
        lines = self._file.readlines()
        self._file.seek(0)
        self._file.truncate()
        writer = csv.writer(self._file)
        writer.writerow(['timestamp'] + self.known_macs)
        for line in lines[1:]:
            self._file.write(line)
        self._file.flush()

    def _flush_row(self):
        self._file.seek(0, 2)
        row = [self.current_ts] + [self.current_row.get(mac, '') for mac in self.known_macs]
        self._writer.writerow(row)
        self._file.flush()
        self.row_count += 1

    def close(self):
        with self.lock:
            if self.current_ts is not None:
                self._flush_row()
            self._file.close()


# =============================================================================
# 보드 수신 스레드
# =============================================================================

def read_board(port: str, board_num: int, writer: WideCSVWriter,
               stats: dict, running_flag: list):
    """ESP32 보드 하나에서 시리얼 수신 (boardID는 ESP32가 보내는 값으로 동적 결정)"""
    board_id = board_num          # 임시값 (첫 수신 후 ESP32 boardID로 덮어씀)
    board_id_confirmed = False

    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=0.1)

        ser.reset_input_buffer()

        print(f"  ✓ {port} 연결 — Board ID 대기 중...")

        scan_start = time.time()
        scan_count = 0

        while running_flag[0]:
            try:
                line = ser.readline().decode('utf-8', errors='ignore').strip()
                if not line or line.startswith('#'):
                    continue

                parts = line.split(',')

                try:
                    # 첫 번째 필드가 숫자여야 유효한 라인
                    recv_id = int(parts[0].strip())

                    # 최초 수신 시 boardID 확정
                    if not board_id_confirmed:
                        board_id = recv_id
                        board_id_confirmed = True
                        print(f"  ✓ Board {board_id} 확인 ({port})")

                    # SCANEND: "boardID,SCANEND"
                    if len(parts) >= 2 and parts[1].strip() == 'SCANEND':
                        scan_count += 1
                        elapsed = time.time() - scan_start
                        if elapsed > 0:
                            stats[board_id]['hz']    = scan_count / elapsed
                            stats[board_id]['scans'] = scan_count
                        continue

                    # 데이터 라인: "boardID,MAC,SSID,RSSI,Channel,SCAN"
                    if len(parts) < 4:
                        continue

                    if ':' in parts[1]:
                        mac  = parts[1].strip()
                        rssi = parts[3].strip()

                        collection_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                        writer.write(collection_time, mac, rssi)

                        stats[board_id]['records'] += 1

                except (ValueError, IndexError):
                    pass

            except Exception:
                pass

    except serial.SerialException as e:
        print(f"  ❌ {port} 오류: {e}")
    finally:
        if 'ser' in locals():
            ser.close()


# =============================================================================
# 상태 출력 스레드
# =============================================================================

def print_status(stats: dict, writer: WideCSVWriter, running_flag: list):
    while running_flag[0]:
        time.sleep(1)
        parts    = []
        hz_list  = []
        total    = sum(s['records'] for s in stats.values())

        for board_id in sorted(stats.keys()):
            hz = stats[board_id]['hz']
            symbol = '✅' if hz >= 1.8 else ('⚠️' if hz >= 1.5 else '❌')
            parts.append(f"B{board_id}:{hz:.1f}Hz{symbol}")
            if hz > 0:
                hz_list.append(hz)

        avg_hz = sum(hz_list) / len(hz_list) if hz_list else 0.0
        print(f"\r  {' | '.join(parts)} | Avg:{avg_hz:.1f}Hz | "
              f"Records:{total:,} | 행:{writer.row_count}",
              end='', flush=True)


# =============================================================================
# 메인
# =============================================================================

def find_esp32_ports() -> list:
    """ESP32 포트 탐색 (/dev/ttyUSB0~7)"""
    ports = []
    for i in range(8):
        port = f'/dev/ttyUSB{i}'
        if os.path.exists(port):
            ports.append(port)
    return ports


def main():
    parser = argparse.ArgumentParser(description='2.4GHz ESP32 수신기')
    parser.add_argument('-l', '--location', default=None, help='장소명 (예: 공학관_3층)')
    parser.add_argument('-o', '--output',   default=OUTPUT_DIR)
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  WiFi 2.4GHz 수집기 (ESP32-S3 x3)")
    print("=" * 60)

    location = args.location
    if not location:
        location = input("\n  장소명 입력 (예: 공학관_3층): ").strip() or "unknown"
    location = location.replace(' ', '_')

    ports = find_esp32_ports()
    if not ports:
        print("  ❌ ESP32 포트 없음! USB 연결 확인하세요.")
        sys.exit(1)

    print(f"\n  감지된 보드: {len(ports)}개 → {ports}")

    # 출력 파일
    ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"rssi_2.4ghz_{location}_{ts_str}.csv"
    filepath = os.path.join(args.output, filename)
    print(f"  출력 파일: {filepath}")

    writer  = WideCSVWriter(filepath)
    stats   = defaultdict(lambda: {'scans': 0, 'records': 0, 'hz': 0.0})
    running = [True]

    def sig_handler(s, f):
        running[0] = False

    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # 보드별 수신 스레드 시작
    threads = []
    for i, port in enumerate(ports):
        t = threading.Thread(
            target=read_board,
            args=(port, i, writer, stats, running),
            daemon=True
        )
        t.start()
        threads.append(t)
        time.sleep(0.3)

    # 상태 출력 스레드
    st = threading.Thread(target=print_status, args=(stats, writer, running), daemon=True)
    st.start()

    print()
    print("  ✅ 수집 시작! (종료: Ctrl+C)")
    print("=" * 60)

    try:
        while running[0]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        running[0] = False

    finally:
        writer.close()
        total = sum(s['records'] for s in stats.values())
        print()
        print()
        print("=" * 60)
        print("  수집 완료")
        for board_id in sorted(stats.keys()):
            s = stats[board_id]
            print(f"  Board {board_id}: {s['records']}건, {s['hz']:.2f}Hz")
        print(f"  총 레코드: {total:,}")
        print(f"  저장 파일: {filepath}")
        print("=" * 60)


if __name__ == '__main__':
    main()

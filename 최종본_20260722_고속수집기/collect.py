#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
고속수집기 통합 런처
================================================================================

5GHz RSSI + 2.4GHz RSSI + IMU 센서 데이터를 동시에 수집합니다.

실행:
  sudo python3 collect.py

sudo 필요 이유:
  - beacon_scanner.py (5GHz 모니터 모드) → root 권한 필요

================================================================================
"""

import os
import sys
import time
import signal
import subprocess
from datetime import datetime

# =============================================================================
# 설정
# =============================================================================

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
COLLECTOR_DIR = SCRIPT_DIR  # collect.py 위치 = collector/

SCANNER_5GHZ  = os.path.join(COLLECTOR_DIR, "scanner_5GHz/beacon_scanner.py")
SCANNER_2GHZ  = os.path.join(COLLECTOR_DIR, "scanner_2.4GHz/wifi_2ghz_receiver.py")
IMU_RECEIVER  = os.path.join(COLLECTOR_DIR, "scripts/imu_receiver.py")

OUTPUT_DIR    = os.path.join(COLLECTOR_DIR, "data")

# =============================================================================
# 인터페이스 자동 탐색
# =============================================================================

def find_wifi_interface() -> str:
    """AWUS036AXML 인터페이스 자동 탐색 (wlx 우선)"""
    try:
        result = subprocess.run(['iw', 'dev'], capture_output=True, text=True, timeout=5)
        interfaces = []
        for line in result.stdout.split('\n'):
            if 'Interface' in line:
                iface = line.split()[-1]
                if iface != 'lo':
                    interfaces.append(iface)
        # wlx로 시작하는 인터페이스 우선 반환 (AWUS036AXML)
        for iface in interfaces:
            if iface.startswith('wlx'):
                return iface
        # 없으면 첫 번째 반환
        return interfaces[0] if interfaces else None
    except:
        pass
    return None


# =============================================================================
# 메인
# =============================================================================

def main():
    print()
    print("=" * 60)
    print("  고속수집기 통합 런처")
    print("  5GHz + 2.4GHz + IMU 동시 수집")
    print("=" * 60)

    # root 권한 확인
    if os.geteuid() != 0:
        print()
        print("  ❌ root 권한이 필요합니다.")
        print("     sudo python3 collect.py 로 실행하세요.")
        sys.exit(1)

    # 장소명 입력
    location = input("\n  장소명 입력 (예: 공학관_3층): ").strip()
    if not location:
        location = "unknown"
    location = location.replace(' ', '_')

    # WiFi 인터페이스 탐색
    iface = find_wifi_interface()
    if not iface:
        print("  ❌ WiFi 인터페이스를 찾을 수 없습니다.")
        sys.exit(1)
    print(f"\n  WiFi 인터페이스: {iface}")

    # 출력 디렉토리 생성
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print()
    print("=" * 60)
    print("  프로세스 시작 중...")
    print("=" * 60)

    processes = []

    # 1. 5GHz 스캐너 (sudo 필요)
    cmd_5ghz = [
        'python3', SCANNER_5GHZ,
        '-i', iface,
        '--location', location,
        '-o', OUTPUT_DIR
    ]
    p1 = subprocess.Popen(cmd_5ghz)
    processes.append(('5GHz 스캐너', p1))
    print(f"  ✓ 5GHz 스캐너 시작 (PID: {p1.pid})")
    time.sleep(1)

    # 2. 2.4GHz 수신기
    cmd_2ghz = [
        'python3', SCANNER_2GHZ,
        '--location', location,
        '-o', OUTPUT_DIR
    ]
    p2 = subprocess.Popen(cmd_2ghz)
    processes.append(('2.4GHz 수신기', p2))
    print(f"  ✓ 2.4GHz 수신기 시작 (PID: {p2.pid})")
    time.sleep(0.5)

    # 3. IMU 수신기
    cmd_imu = [
        'python3', IMU_RECEIVER,
        '--location', location,
        '--output', OUTPUT_DIR
    ]
    p3 = subprocess.Popen(cmd_imu)
    processes.append(('IMU 수신기', p3))
    print(f"  ✓ IMU 수신기 시작 (PID: {p3.pid})")

    print()
    print("=" * 60)
    print("  ✅ 전체 수집 시작!")
    print("  종료: Ctrl+C")
    print("=" * 60)
    print()

    # 종료 핸들러
    def sig_handler(s, f):
        print()
        print()
        print("=" * 60)
        print("  수집 종료 중...")
        for name, p in processes:
            try:
                p.terminate()
                p.wait(timeout=3)
                print(f"  ✓ {name} 종료")
            except:
                p.kill()
                print(f"  ✓ {name} 강제 종료")
        print()
        print(f"  저장 위치: {OUTPUT_DIR}")
        print("=" * 60)
        sys.exit(0)

    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    # 프로세스 모니터링
    while True:
        time.sleep(2)
        for name, p in processes:
            if p.poll() is not None:
                print(f"  ⚠ {name} 종료됨 (코드: {p.returncode})")


if __name__ == '__main__':
    main()

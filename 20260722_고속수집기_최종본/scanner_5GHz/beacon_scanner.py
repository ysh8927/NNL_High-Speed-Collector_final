#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
WiFi 5GHz 고속 RSSI 스캐너 - 모니터 모드 + tcpdump 방식
================================================================================

2Hz 이상 RSSI 갱신 보장
- 모니터 모드에서 tcpdump로 Beacon 캡처
- 캐시 없음
- AWUS036AXML (MT7921AUN) 지원
- 장소명 입력 → 파일명에 반영

사용법:
  sudo python3 beacon_scanner.py -i wlx00c0cab90114 --location 공학관_3층

================================================================================
"""

import os
import sys
import time
import signal
import subprocess
import re
import csv
import threading
from datetime import datetime
from typing import List, Dict, Optional
from collections import defaultdict
import select


# =============================================================================
# 상수
# =============================================================================

# 기존 비DFS 9채널 (UNII-1 + UNII-3) — 폴백/비교용으로 유지
CHANNELS_5GHZ_NO_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]

# DFS 포함 전체 25채널 (2026.07.10 미팅 액션아이템: DFS 채널 스캔 가능 여부 확인)
# monitor mode는 수신 전용(TX 없음)이라 CAC(레이더 감지) 없이 iw set freq로
# 튜닝만 하면 되므로 이론적으로는 그대로 동작해야 함 — 실기 확인 필요
CHANNELS_5GHZ_WITH_DFS = [
    36, 40, 44, 48,           # UNII-1
    52, 56, 60, 64,           # UNII-2A (DFS)
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,  # UNII-2C (DFS)
    149, 153, 157, 161, 165   # UNII-3
]

# 실제 사용할 채널 리스트 (여기서 전환: DFS 없이 되돌리려면 CHANNELS_5GHZ_NO_DFS로 변경)
CHANNELS_5GHZ = CHANNELS_5GHZ_WITH_DFS

def channel_to_freq(ch: int) -> int:
    return 5000 + ch * 5


# =============================================================================
# AP 데이터 저장소
# =============================================================================

class APDataStore:
    """스레드 안전한 AP 데이터 저장소

    [2026.06.30 수정] 캐싱으로 인한 RSSI 신선도(staleness) 문제 해결
    ------------------------------------------------------------
    기존: get_recent(max_age_sec=2.0) 가 "최근 2초 이내 잡힌 적 있는 모든 AP"를
          매 배치(약 333ms)마다 반복해서 돌려줌 → 같은 비콘 1번을 여러 배치에
          걸쳐 최대 6번까지 재사용 → 옛날 값이 몇 초씩 그대로 찍히는 현상 발생.
    변경: 각 AP 레코드에 'reported' 플래그를 둬서, 이미 한 번 배치에 포함된
          값은 새 비콘이 안 들어오는 한 다시 반환하지 않음.
          → 캐시값 반복 없이, 진짜 새로 잡힌 값만 사용 (2.4GHz와 동일한 철학:
            실측값 아니면 빈칸 처리, 후처리에서 -100 제외 후 평균).
    """

    def __init__(self):
        self.aps  = {}
        self.lock = threading.Lock()

    def update(self, bssid: str, ssid: str, rssi: int, freq: int):
        with self.lock:
            self.aps[bssid] = {
                'bssid'    : bssid,
                'ssid'     : ssid,
                'rssi'     : rssi,
                'freq'     : freq,
                'channel'  : (freq - 5000) // 5 if freq >= 5000 else 0,
                'timestamp': time.time(),
                'reported' : False,   # 아직 어느 배치에도 포함 안 된 새 값
            }

    def get_recent(self, max_age_sec: float = 2.0) -> List[Dict]:
        """[수정] 아직 보고되지 않은(reported=False) 새 값만 반환하고,
        반환한 값은 즉시 reported=True로 표시해 다음 배치에서 재사용하지 않음.
        max_age_sec는 너무 오래된 미보고 값(스레드 타이밍 이슈 등 예외 상황)을
        걸러내는 안전장치로만 사용."""
        now    = time.time()
        result = []
        with self.lock:
            for bssid, info in self.aps.items():
                if info['reported']:
                    continue
                if now - info['timestamp'] <= max_age_sec:
                    result.append(info.copy())
                    info['reported'] = True
        return result

    def get_unique_count(self) -> int:
        with self.lock:
            return len(self.aps)


# =============================================================================
# tcpdump 기반 Beacon 캡처
# =============================================================================

class TcpdumpBeaconCapture:
    """tcpdump로 Beacon 프레임 캡처"""

    def __init__(self, interface: str, ap_store: APDataStore):
        self.interface = interface
        self.ap_store  = ap_store
        self.process   = None
        self.running   = False
        self.thread    = None

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()

    def _capture_loop(self):
        cmd = [
            'tcpdump', '-i', self.interface,
            '-e', '-l', '-n',
            'type', 'mgt', 'subtype', 'beacon'
        ]
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1
            )
            while self.running:
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        break
                    continue
                self._parse_tcpdump_line(line)
        except Exception as e:
            print(f"[tcpdump 오류] {e}")

    def _parse_tcpdump_line(self, line: str):
        try:
            signal_match = re.search(r'(-?\d+)dBm?\s*signal', line, re.I)
            if not signal_match:
                signal_match = re.search(r'(-?\d+)dB\s', line)
            rssi = int(signal_match.group(1)) if signal_match else -100

            bssid_match = re.search(r'BSSID[:\s]+([0-9a-fA-F:]{17})', line, re.I)
            if not bssid_match:
                bssid_match = re.search(r'SA[:\s]+([0-9a-fA-F:]{17})', line, re.I)
            if not bssid_match:
                return

            bssid = bssid_match.group(1).upper()

            freq_match = re.search(r'(\d{4})\s*MHz', line)
            freq = int(freq_match.group(1)) if freq_match else 0
            if freq and not (5150 <= freq <= 5850):
                return

            # tcpdump beacon 출력은 "... Beacon (SSID) [rates] CH: xx ..." 형태로,
            # SSID 뒤에 데이터 레이트/채널 정보가 더 이어지므로 줄 끝(anchor $) 매칭은 항상 실패한다.
            # "Beacon (" 바로 뒤 괄호를 명시적으로 찾아야 한다.
            ssid_match = re.search(r'Beacon\s*\(([^)]*)\)', line)
            ssid = ssid_match.group(1) if ssid_match else ""

            self.ap_store.update(bssid, ssid, rssi, freq)
        except Exception:
            pass


# =============================================================================
# 채널 호퍼
# =============================================================================

class ChannelHopper:
    """채널 순환"""

    def __init__(self, interface: str, channels: List[int], dwell_ms: int = 50):
        self.interface       = interface
        self.channels        = channels
        self.dwell_ms        = dwell_ms
        self.running         = False
        self.thread          = None
        self.current_channel = 0

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._hop_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def _hop_loop(self):
        idx = 0
        while self.running:
            ch   = self.channels[idx]
            freq = channel_to_freq(ch)
            try:
                subprocess.run(
                    ['iw', self.interface, 'set', 'freq', str(freq)],
                    capture_output=True, timeout=1
                )
                self.current_channel = ch
            except:
                pass
            time.sleep(self.dwell_ms / 1000.0)
            idx = (idx + 1) % len(self.channels)


# =============================================================================
# CSV 기록기 (Wide 포맷)
# =============================================================================

class CSVWriter:
    """
    Wide 포맷 CSV 기록기
    헤더: timestamp, COL1, COL2, ...
    각 컬럼 헤더는 "WIFI_{SSID}/CH{channel}/{freq}MHz/{bssid}" 형식으로,
    스마트폰 수집 앱(PDR_RF.csv)과 동일한 명명 규칙을 따른다.
    (단, WiFi 표준 세대(WiFi4/5/6 등)는 802.11 캡ability IE 파싱이 추가로 필요해
    이 버전에서는 포함하지 않는다.)
    셀 값은 순수 RSSI 정수값 그대로이며, 채널 정보는 컬럼 헤더에만 존재한다.
    """

    def __init__(self, filepath: str):
        self.filepath   = filepath
        self.row_count  = 0
        self.known_cols = []          # "WIFI_{ssid}/CH{ch}/{freq}MHz/{bssid}" 문자열 목록
        self.rows       = []          # (ts, {col_key: rssi})
        os.makedirs(os.path.dirname(filepath) or '.', exist_ok=True)

    @staticmethod
    def _col_key(ap: Dict) -> str:
        ssid = ap.get('ssid', '') or ''
        return f"WIFI_{ssid}/CH{ap['channel']}/{ap['freq']}MHz/{ap['bssid']}"

    def write_batch(self, cycle: int, aps: List[Dict]):
        ts       = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        snapshot = {self._col_key(ap): ap['rssi'] for ap in aps}

        new_cols = [c for c in snapshot if c not in self.known_cols]
        self.known_cols.extend(new_cols)

        self.rows.append((ts, snapshot))
        self.row_count += 1

        # 새 컬럼 발견 시 또는 10행마다 파일 재작성
        if new_cols or self.row_count % 10 == 0:
            self._rewrite()

    def _rewrite(self):
        with open(self.filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp'] + self.known_cols)
            for ts, snapshot in self.rows:
                row = [ts] + [snapshot.get(col, '') for col in self.known_cols]
                writer.writerow(row)

    def close(self):
        self._rewrite()


# =============================================================================
# 메인 스캐너
# =============================================================================

class BeaconScanner:
    def __init__(self, interface: str, output_dir: str = "./data",
                 target_hz: float = 2.5, dwell_ms: int = 50, location: str = "unknown"):
        self.interface  = interface
        self.output_dir = output_dir
        self.target_hz  = target_hz
        self.dwell_ms   = dwell_ms
        self.location   = location.replace(' ', '_')

        self.running    = False
        self.cycle      = 0
        self.start_time = None

        self.ap_store = APDataStore()
        self.capture  = None
        self.hopper   = None
        self.writer   = None

    def _setup_monitor_mode(self) -> bool:
        try:
            subprocess.run(['ip', 'link', 'set', self.interface, 'down'],
                           capture_output=True, timeout=5)
            result = subprocess.run(['iw', self.interface, 'set', 'type', 'monitor'],
                                    capture_output=True, timeout=5)
            subprocess.run(['ip', 'link', 'set', self.interface, 'up'],
                           capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception as e:
            print(f"[오류] 모니터 모드 전환 실패: {e}")
            return False

    def _restore_managed_mode(self):
        try:
            subprocess.run(['ip', 'link', 'set', self.interface, 'down'],
                           capture_output=True, timeout=5)
            subprocess.run(['iw', self.interface, 'set', 'type', 'managed'],
                           capture_output=True, timeout=5)
            subprocess.run(['ip', 'link', 'set', self.interface, 'up'],
                           capture_output=True, timeout=5)
        except:
            pass

    def setup(self) -> bool:
        print("=" * 65)
        print("  🚀 WiFi 5GHz Beacon 스캐너 (모니터 모드, 2Hz+)")
        print("  📡 AWUS036AXML (MT7921AUN)")
        print(f"  📍 장소: {self.location}")
        print("=" * 65)

        if os.geteuid() != 0:
            print("❌ root 권한이 필요합니다!")
            return False

        try:
            subprocess.run(['which', 'tcpdump'], capture_output=True, check=True)
        except:
            print("❌ tcpdump가 설치되어 있지 않습니다!")
            return False

        try:
            os.makedirs(self.output_dir, exist_ok=True)

            print(f"[1/4] 인터페이스: {self.interface}")
            print("[2/4] 모니터 모드 전환...")
            if not self._setup_monitor_mode():
                print("❌ 모니터 모드 실패!")
                return False
            print("      ✓ 모니터 모드 활성화")

            print("[3/4] tcpdump 시작...")
            self.capture = TcpdumpBeaconCapture(self.interface, self.ap_store)
            self.capture.start()
            print("      ✓ Beacon 캡처 시작")

            print("[4/4] 채널 호퍼 시작...")
            self.hopper = ChannelHopper(self.interface, CHANNELS_5GHZ, self.dwell_ms)
            self.hopper.start()
            n_ch = len(CHANNELS_5GHZ)
            cycle_ms = n_ch * self.dwell_ms
            dfs_on = CHANNELS_5GHZ is CHANNELS_5GHZ_WITH_DFS
            print(f"      ✓ 채널 홉핑 ({self.dwell_ms}ms/채널, 총 {n_ch}채널, "
                  f"1바퀴 {cycle_ms}ms, DFS 포함: {dfs_on})")

            # 장소명을 파일명에 포함
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"rssi_5ghz_{self.location}_{ts}.csv"
            filepath = os.path.join(self.output_dir, filename)
            self.writer = CSVWriter(filepath)
            print(f"      파일: {filepath}")

            return True

        except Exception as e:
            print(f"❌ 설정 실패: {e}")
            return False

    def run(self):
        self.running    = True
        self.start_time = time.time()
        interval        = 1.0 / self.target_hz

        print()
        print("=" * 65)
        print("  📶 모니터링 시작! (종료: Ctrl+C)")
        print("=" * 65)

        time.sleep(1.0)

        try:
            while self.running:
                cycle_start = time.time()
                self.cycle += 1

                # [수정] 새로 잡힌 AP가 없어도(aps가 빈 리스트여도) 매 사이클 행을 기록.
                #   타임스탬프 간격을 일정하게 유지하고, 값이 없는 AP는 빈칸으로
                #   남겨서 2.4GHz와 동일하게 "실측 아니면 빈칸" 규칙을 따르게 함.
                aps = self.ap_store.get_recent(max_age_sec=2.0)
                self.writer.write_batch(self.cycle, aps)

                elapsed    = time.time() - self.start_time
                current_hz = self.cycle / elapsed if elapsed > 0 else 0
                hz_ok      = "✓" if current_hz >= 2.0 else "✗"
                ch         = self.hopper.current_channel if self.hopper else 0

                print(f"\r[{self.cycle:5d}] "
                      f"Hz: {current_hz:.2f} {hz_ok} | "
                      f"AP: {len(aps):2d} (총 {self.ap_store.get_unique_count():2d}) | "
                      f"CH: {ch:3d} | 행: {self.writer.row_count}",
                      end="", flush=True)

                remaining = interval - (time.time() - cycle_start)
                if remaining > 0:
                    time.sleep(remaining)

        except KeyboardInterrupt:
            print("\n\n⏹️  사용자 중단")
        finally:
            self.stop()

    def stop(self):
        self.running = False
        if self.capture: self.capture.stop()
        if self.hopper:  self.hopper.stop()

        if self.start_time:
            total = time.time() - self.start_time
            hz    = self.cycle / total if total > 0 else 0
            print()
            print("=" * 65)
            print(f"  사이클: {self.cycle} | 시간: {total:.2f}초 | Hz: {hz:.2f}")
            print(f"  AP: {self.ap_store.get_unique_count()}개 | 행: {self.writer.row_count if self.writer else 0}")
            print("  ✅ 2Hz 달성!" if hz >= 2.0 else "  ⚠️  2Hz 미달")
            print("=" * 65)

        self._restore_managed_mode()
        if self.writer:
            self.writer.close()
            print(f"  📁 저장: {self.writer.filepath}")


# =============================================================================
# 메인
# =============================================================================

def find_interface() -> Optional[str]:
    try:
        result = subprocess.run(['iw', 'dev'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.split('\n'):
            if 'Interface' in line:
                return line.split()[-1]
    except:
        pass
    return None


def main():
    import argparse

    parser = argparse.ArgumentParser(description='WiFi 5GHz Beacon 스캐너 (2Hz+)')
    parser.add_argument('-i', '--interface', default=None)
    parser.add_argument('-o', '--output',    default='./data')
    parser.add_argument('-l', '--location',  default=None,  help='장소명 (예: 공학관_3층)')
    parser.add_argument('--target-hz',       type=float, default=3.0)
    parser.add_argument('--dwell-ms',        type=int,   default=50)
    args = parser.parse_args()

    interface = args.interface or find_interface()
    if not interface:
        print("❌ WiFi 인터페이스 없음!")
        sys.exit(1)

    # 장소명 미입력 시 터미널에서 입력
    location = args.location
    if not location:
        location = input("장소명 입력 (예: 공학관_3층): ").strip() or "unknown"

    scanner = BeaconScanner(
        interface  = interface,
        output_dir = args.output,
        target_hz  = args.target_hz,
        dwell_ms   = args.dwell_ms,
        location   = location,
    )

    def sig_handler(s, f):
        scanner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    if scanner.setup():
        scanner.run()
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()

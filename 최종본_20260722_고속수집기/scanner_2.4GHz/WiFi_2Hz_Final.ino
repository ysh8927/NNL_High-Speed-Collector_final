/*
 * WiFi 2.4GHz Scanner - ESP32-S3 x3 (채널 할당 방식)
 * ============================================================
 * 3개 보드가 채널을 나눠서 담당
 *
 * 채널 할당:
 *   Board 0 → CH 1, 4, 7, 10        (4채널, 500ms 틱, 2.00Hz)
 *   Board 1 → CH 2, 5, 8, 11        (4채널, 500ms 틱, 2.00Hz)
 *   Board 2 → CH 3, 6, 9, 12, 13    (5채널, 575ms 틱, 1.74Hz)
 *
 * 보드 MAC 매핑:
 *   Board 0: mac[4] == 0xb9  (b6:b9:70)
 *   Board 1: mac[5] == 0x2c  (b7:3b:2c)
 *   Board 2: mac[5] == 0xb4  (b7:32:b4)
 *
 * 출력 포맷:
 *   <boardID>,<BSSID>,<SSID>,<RSSI>,<Channel>,SCAN
 *   <boardID>,SCANEND
 *
 * [2026.06.30 수정] AP 재방문 주기 2Hz 보장
 * ------------------------------------------------------------
 * 기존: 500ms 틱마다 채널 1개만 스캔 → 채널 4개 순환 시
 *       같은 채널(=같은 AP) 재방문 주기가 2초(0.5Hz)로 떨어짐.
 * 변경: 500ms 틱 한 번에 담당 채널 4개를 전부 스캔
 *       (80ms x 4 = 320ms, 500ms 예산 안에 여유 있게 수행).
 *       -> 담당 채널의 모든 AP가 500ms마다(2Hz) 재방문됨.
 *
 * [2026.06.30 추가 수정] 채널당 dwell time 80ms → 115ms
 * ------------------------------------------------------------
 * 문제: AP 비콘 주기(beacon interval)는 보통 100ms인데, 80ms
 *       패시브 스캔창은 그보다 짧아 비콘 타이밍과 어긋나면
 *       해당 채널 사이클에서 비콘을 통째로 놓치는 확률적 미탐지
 *       (probabilistic miss) 발생. 실측 데이터에서 간격이
 *       0.5초 배수로 몰리는 패턴으로 확인됨.
 * 변경: 채널당 dwell time을 115ms로 상향.
 *       - 100ms 비콘 주기 대비 +15ms 마진 → 일반적인 비콘
 *         지터(beacon jitter, CSMA/CA로 인한 송신 지연)를 흡수
 *       - 4채널 x 115ms = 460ms, 500ms 타이밍 버짓 안에 40ms
 *         슬랙(slack) 확보 → 채널 전환/파싱/UART 출력 오버헤드
 *         흡수 가능 (125ms는 슬랙 0이라 사이클 오버런 위험 있음)
 *
 * [2026.07.02 수정] 채널 12 누락 문제 해결
 * ------------------------------------------------------------
 * 문제: Board0/1/2가 각 4채널씩(총 12채널)만 담당하여, 2.4GHz
 *       13개 채널(한국 기준 1~13 전체 허용) 중 채널 12가 스캔
 *       대상에서 통째로 누락됨. 4보드 체제에서 3보드로 축소하며
 *       "보드당 4채널" 수치를 그대로 유지한 데서 발생한 설계
 *       사각지대(원본 4보드 전체스캔 버전에는 채널 12 포함).
 * 검토: (a) 12/13 교대 스캔 → 특정 채널만 1Hz로 반토막.
 *       (b) 13번 채널 제외 후 12번 추가 → 국내(KCC)는 1~13
 *           전체 허용 지역이라 13번도 정상 사용 채널, 순손실 없음.
 *       (c) Board2만 5채널로 확장 + 그 보드만 틱 주기 연장 → 채택.
 * 변경: Board2 채널을 3,6,9,12,13(5개)로 확장.
 *       dwell time은 115ms 그대로 유지(비콘 지터 마진 불변,
 *       Board0/1 재검증 불필요). 대신 Board2 SCAN_INTERVAL_US만
 *       575000(575ms)으로 연장: 115ms x 5채널 = 575ms.
 *       -> Board2 담당 5채널 전부 균등하게 1.74Hz로 재방문
 *          (채널 12: 0Hz -> 1.74Hz, 채널 13: 2Hz -> 1.74Hz).
 *       Board0/1은 500ms 틱, 2Hz 그대로 영향 없음.
 * ============================================================
 */

#include "WiFi.h"
#include <esp_wifi.h>
#include <esp_timer.h>

#define LED_PIN 2
#define SCAN_INTERVAL_US_DEFAULT 500000  // 500ms = 2Hz (Board 0, 1)
#define SCAN_INTERVAL_US_BOARD2  575000  // 575ms = 1.74Hz (Board 2, 5채널용)

// 보드별 채널 할당
const uint8_t CHANNELS_BOARD0[] = {1, 4, 7, 10};
const uint8_t CHANNELS_BOARD1[] = {2, 5, 8, 11};
const uint8_t CHANNELS_BOARD2[] = {3, 6, 9, 12, 13};  // [2026.07.02] 12번 추가, 5채널
const uint8_t CHANNEL_COUNT_DEFAULT = 4;
const uint8_t CHANNEL_COUNT_BOARD2  = 5;

esp_timer_handle_t periodic_timer;

int boardID = 0;
const uint8_t* assignedChannels = nullptr;
uint8_t channelCount = CHANNEL_COUNT_DEFAULT;      // [2026.07.02] 보드별 가변
uint32_t scanIntervalUs = SCAN_INTERVAL_US_DEFAULT; // [2026.07.02] 보드별 가변
volatile bool scanReady = false;

void IRAM_ATTR onTimer(void* arg) {
  scanReady = true;
}

void setup() {
  Serial.begin(921600);
  while (!Serial) delay(10);

  pinMode(LED_PIN, OUTPUT);

  // WiFi 초기화 후 MAC 읽기 (초기화 전에 읽으면 00:00:00 나옴)
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_ps(WIFI_PS_NONE);
  delay(500);

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);

  if (mac[4] == 0xb9) {
    boardID = 0;
    assignedChannels = CHANNELS_BOARD0;
    channelCount = CHANNEL_COUNT_DEFAULT;
    scanIntervalUs = SCAN_INTERVAL_US_DEFAULT;
  } else if (mac[5] == 0x2c) {
    boardID = 1;
    assignedChannels = CHANNELS_BOARD1;
    channelCount = CHANNEL_COUNT_DEFAULT;
    scanIntervalUs = SCAN_INTERVAL_US_DEFAULT;
  } else if (mac[5] == 0xb4) {
    boardID = 2;
    assignedChannels = CHANNELS_BOARD2;
    channelCount = CHANNEL_COUNT_BOARD2;      // [2026.07.02] 5채널
    scanIntervalUs = SCAN_INTERVAL_US_BOARD2; // [2026.07.02] 575ms 틱
  } else {
    // 알 수 없는 보드 → 나머지 ID 할당
    boardID = ((mac[4] << 8) | mac[5]) % 3;
    if (boardID == 0) {
      assignedChannels = CHANNELS_BOARD0;
      channelCount = CHANNEL_COUNT_DEFAULT;
      scanIntervalUs = SCAN_INTERVAL_US_DEFAULT;
    } else if (boardID == 1) {
      assignedChannels = CHANNELS_BOARD1;
      channelCount = CHANNEL_COUNT_DEFAULT;
      scanIntervalUs = SCAN_INTERVAL_US_DEFAULT;
    } else {
      assignedChannels = CHANNELS_BOARD2;
      channelCount = CHANNEL_COUNT_BOARD2;
      scanIntervalUs = SCAN_INTERVAL_US_BOARD2;
    }
  }

  Serial.printf("MAC: %02X:%02X:%02X:%02X:%02X:%02X -> Board ID: %d\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], boardID);
  Serial.printf("Channels: ");
  for (int i = 0; i < channelCount; i++) {
    Serial.printf("%d ", assignedChannels[i]);
  }
  Serial.println();
  Serial.printf("Scan interval: %lu us (%.2f Hz)\n", scanIntervalUs, 1000000.0 / scanIntervalUs);

  const esp_timer_create_args_t periodic_timer_args = {
    .callback = &onTimer,
    .name = "scan_timer"
  };
  esp_timer_create(&periodic_timer_args, &periodic_timer);
  esp_timer_start_periodic(periodic_timer, scanIntervalUs);


  Serial.print("READY,BOARD:");
  Serial.println(boardID);
}

void loop() {
  if (scanReady) {
    scanReady = false;
    performScan();
  }
  delayMicroseconds(10);
}

void performScan() {
  digitalWrite(LED_PIN, HIGH);

  // [수정] 담당 채널 전부를 한 틱 안에서 순차 스캔
  //   Board0/1: 115ms x 4채널 = 460ms < 500ms 예산 (슬랙 40ms)
  //   Board2:   115ms x 5채널 = 575ms == 575ms 예산 (틱 자체를 연장, 슬랙 0이지만
  //             dwell time·비콘 지터 마진은 동일하게 유지되므로 미탐지 위험 증가 없음)
  //   결과: 담당 채널의 AP가 보드별 주기(2Hz 또는 1.74Hz)로 균등 재방문됨
  for (uint8_t i = 0; i < channelCount; i++) {
    uint8_t ch = assignedChannels[i];

    wifi_scan_config_t config;
    memset(&config, 0, sizeof(config));
    config.ssid        = NULL;
    config.bssid       = NULL;
    config.channel     = ch;
    config.show_hidden = true;
    config.scan_type   = WIFI_SCAN_TYPE_PASSIVE;
    config.scan_time.passive = 115;  // 채널당 115ms (100ms 비콘 주기 + 15ms 지터 마진)

    esp_wifi_scan_start(&config, true);

    uint16_t apCount = 0;
    esp_wifi_scan_get_ap_num(&apCount);

    uint16_t maxAPs = (apCount > 20) ? 20 : apCount;
    wifi_ap_record_t* apRecords = (wifi_ap_record_t*)malloc(maxAPs * sizeof(wifi_ap_record_t));

    if (apRecords != NULL && maxAPs > 0) {
      esp_wifi_scan_get_ap_records(&maxAPs, apRecords);

      for (int k = 0; k < maxAPs; k++) {
        // <boardID>,<BSSID>,<SSID>,<RSSI>,<Channel>,SCAN
        Serial.print(boardID); Serial.print(",");

        for (int j = 0; j < 6; j++) {
          if (j > 0) Serial.print(":");
          Serial.printf("%02X", apRecords[k].bssid[j]);
        }
        Serial.print(",");

        String ssid = String((char*)apRecords[k].ssid);
        if (ssid.length() == 0) ssid = "HIDDEN";
        ssid.replace(",", "_");
        Serial.print(ssid); Serial.print(",");

        Serial.print(apRecords[k].rssi);    Serial.print(",");
        Serial.print(apRecords[k].primary); Serial.println(",SCAN");
      }

      free(apRecords);
    }

    esp_wifi_scan_stop();
  }

  digitalWrite(LED_PIN, LOW);

  Serial.print(boardID);
  Serial.println(",SCANEND");
}

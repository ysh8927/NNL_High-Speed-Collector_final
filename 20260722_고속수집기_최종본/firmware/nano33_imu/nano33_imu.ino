/*
 * Nano 33 BLE Rev2 - IMU 센서 데이터 출력
 * ============================================================
 * IMU: BMI270 (가속도 + 자이로)
 * 라이브러리: Arduino_BMI270_BMM150
 *
 * 축 설정 (케이스 부착 기준):
 *   X축 → 진행방향 (정면)
 *   Y축 → 왼쪽
 *   Z축 → 위
 *
 * 출력 포맷 (100Hz):
 *   IMU,<timestamp_ms>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
 *   단위: 가속도 [g], 자이로 [deg/s]
 *
 * 통신: 921600 baud
 * ============================================================
 */

#include <Arduino_BMI270_BMM150.h>

#define SAMPLE_INTERVAL_MS 20  // 50Hz
#define LED_PIN LED_BUILTIN

unsigned long lastSampleTime = 0;
unsigned long sampleCounter  = 0;

void setup() {
  Serial.begin(921600);
  while (!Serial) delay(10);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  if (!IMU.begin()) {
    Serial.println("ERROR,IMU_INIT_FAILED");
    // 초기화 실패 시 LED 빠르게 깜빡임
    while (1) {
      digitalWrite(LED_PIN, HIGH); delay(200);
      digitalWrite(LED_PIN, LOW);  delay(200);
    }
  }

  Serial.println("READY,NANO33_IMU");
  Serial.print("INFO,ACCEL_RATE,"); Serial.println(IMU.accelerationSampleRate());
  Serial.print("INFO,GYRO_RATE,");  Serial.println(IMU.gyroscopeSampleRate());

  // 준비 완료 LED 점멸
  digitalWrite(LED_PIN, HIGH); delay(500);
  digitalWrite(LED_PIN, LOW);
}

void loop() {
  unsigned long now = millis();

  if (now - lastSampleTime >= SAMPLE_INTERVAL_MS) {
    lastSampleTime = now;

    float ax, ay, az;
    float gx, gy, gz;

    bool accelReady = IMU.accelerationAvailable();
    bool gyroReady  = IMU.gyroscopeAvailable();

    if (accelReady) IMU.readAcceleration(ax, ay, az);
    else            ax = ay = az = 0.0;

    if (gyroReady)  IMU.readGyroscope(gx, gy, gz);
    else            gx = gy = gz = 0.0;

    if (accelReady || gyroReady) {
      sampleCounter++;

      // IMU,<timestamp_ms>,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
      Serial.print("IMU,");
      Serial.print(now);    Serial.print(",");
      Serial.print(ax, 4);  Serial.print(",");
      Serial.print(ay, 4);  Serial.print(",");
      Serial.print(az, 4);  Serial.print(",");
      Serial.print(gx, 4);  Serial.print(",");
      Serial.print(gy, 4);  Serial.print(",");
      Serial.println(gz, 4);

      // 1000샘플마다 LED 깜빡임 (동작 확인)
      if (sampleCounter % 1000 == 0) {
        digitalWrite(LED_PIN, HIGH); delay(10);
        digitalWrite(LED_PIN, LOW);
      }
    }
  }
}

#!/bin/bash
# ========================================
# AWUS036AXML (MT7921/MT7961) 펌웨어 설치
# ========================================

set -e

echo "========================================"
echo "  AWUS036AXML 펌웨어 설치"
echo "  MediaTek MT7921AUN (MT7961)"
echo "========================================"
echo

# root 확인
if [ "$EUID" -ne 0 ]; then
    echo "❌ root 권한이 필요합니다."
    echo "   sudo $0"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIRMWARE_DIR="$(dirname "$SCRIPT_DIR")/AXML_Firmware_Linux_15APR2025"

echo "[1/4] 펌웨어 파일 확인..."
if [ -d "$FIRMWARE_DIR" ]; then
    echo "      ✓ 펌웨어 디렉토리: $FIRMWARE_DIR"
    ls -la "$FIRMWARE_DIR"/*.bin 2>/dev/null || echo "      (펌웨어 파일 없음)"
else
    echo "❌ 펌웨어 디렉토리를 찾을 수 없습니다: $FIRMWARE_DIR"
    exit 1
fi

echo
echo "[2/4] 시스템 펌웨어 형식 확인..."
if ls /lib/firmware/mediatek/*.zst 2>/dev/null | head -1 > /dev/null; then
    COMPRESS="zstd"; EXT=".zst"
    echo "      시스템: zstd 압축 사용"
elif ls /lib/firmware/mediatek/*.xz 2>/dev/null | head -1 > /dev/null; then
    COMPRESS="xz"; EXT=".xz"
    echo "      시스템: xz 압축 사용"
elif ls /lib/firmware/mediatek/*.gz 2>/dev/null | head -1 > /dev/null; then
    COMPRESS="gzip"; EXT=".gz"
    echo "      시스템: gzip 압축 사용"
else
    COMPRESS="none"; EXT=""
    echo "      시스템: 압축 없음"
fi

echo
echo "[3/4] 펌웨어 설치..."
mkdir -p /lib/firmware/mediatek

for fw in "$FIRMWARE_DIR"/*.bin; do
    if [ -f "$fw" ]; then
        fname=$(basename "$fw")
        if [ "$COMPRESS" = "zstd" ]; then
            zstd -fq "$fw" -o "/lib/firmware/mediatek/${fname}${EXT}"
        elif [ "$COMPRESS" = "xz" ]; then
            xz -fkc "$fw" > "/lib/firmware/mediatek/${fname}${EXT}"
        elif [ "$COMPRESS" = "gzip" ]; then
            gzip -c "$fw" > "/lib/firmware/mediatek/${fname}${EXT}"
        else
            cp "$fw" "/lib/firmware/mediatek/"
        fi
        echo "      ✓ 설치됨: ${fname}${EXT}"
    fi
done

echo
echo "[4/4] 설치 확인..."
ls -la /lib/firmware/mediatek/*MT7961* 2>/dev/null || echo "      (MT7961 파일 없음)"

echo
echo "========================================"
echo "  ✅ 펌웨어 설치 완료!"
echo ""
echo "  다음 단계:"
echo "    1. sudo reboot"
echo "    2. iw dev 로 인터페이스 확인"
echo "========================================"

#!/bin/bash
# STS2 Tracker 실행 - Python 서버 + Swift 오버레이
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Starting STS2 Tracker..."

# 기존 인스턴스 정리
pkill -f "$DIR/server.py" 2>/dev/null || true
pkill -f "$DIR/overlay/STS2Overlay" 2>/dev/null || true
sleep 0.5

# Python 서버 시작
source "$DIR/.venv/bin/activate"
python "$DIR/server.py" &
SERVER_PID=$!
echo "Server started (PID: $SERVER_PID)"

# 서버 준비 대기
sleep 2

# Swift 오버레이 실행
"$DIR/overlay/STS2Overlay" &
OVERLAY_PID=$!
echo "Overlay started (PID: $OVERLAY_PID)"

echo ""
echo "STS2 Tracker running!"
echo "  - Server: http://127.0.0.1:9999"
echo "  - Overlay: transparent window"
echo ""
echo "Press Ctrl+C to stop"

# 종료 시 정리
cleanup() {
    echo ""
    echo "Stopping..."
    kill $OVERLAY_PID 2>/dev/null
    kill $SERVER_PID 2>/dev/null
    exit 0
}
trap cleanup INT TERM

# 대기
wait

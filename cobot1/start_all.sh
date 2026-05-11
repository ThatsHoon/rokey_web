#!/bin/bash
# ROBO CHEF — 전체 서버 시작 스크립트

BASE="$(cd "$(dirname "$0")" && pwd)"
LOCAL_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')

# 기존 프로세스 정리
kill -9 $(lsof -t -i:3001) 2>/dev/null
kill -9 $(lsof -t -i:3002) 2>/dev/null
kill -9 $(lsof -t -i:3003) 2>/dev/null
kill -9 $(lsof -t -i:5000) 2>/dev/null
pkill -9 -f "cloudflared" 2>/dev/null
pkill -9 -f "app.py" 2>/dev/null
pkill -9 -f "ros2_order_publisher" 2>/dev/null
pkill -9 -f "fk_worker.py" 2>/dev/null
sleep 2

echo "🤖 ROBO CHEF 서버 시작 중..."

# firebase_config.js 각 패널에 동기화
cp "$BASE/panel/firebase_config.js" "$BASE/panel/kiosk/firebase_config.js"
cp "$BASE/panel/firebase_config.js" "$BASE/panel/customer_status/firebase_config.js"
cp "$BASE/panel/firebase_config.js" "$BASE/panel/admin_monitor/firebase_config.js"

# UI 정적 서버
python3 -m http.server 3001 --directory "$BASE/panel/kiosk"           --bind 0.0.0.0 > /tmp/rc_kiosk.log   2>&1 &
python3 -m http.server 3002 --directory "$BASE/panel/customer_status"  --bind 0.0.0.0 > /tmp/rc_status.log  2>&1 &
python3 -m http.server 3003 --directory "$BASE/panel/admin_monitor"    --bind 0.0.0.0 > /tmp/rc_admin.log   2>&1 &

# Flask 백엔드 (tmux 세션으로 실행 — 로그: tmux attach -t flask)
if [ -f "$BASE/backend/serviceAccountKey.json" ]; then
    tmux kill-session -t flask 2>/dev/null
    tmux new-session -d -s flask -c "$BASE/backend" "python3 app.py 2>&1 | tee /tmp/rc_flask.log; echo '--- Flask 종료 ---'; read"
fi

# FK Worker (joint_positions → TCP 재계산 → Firebase /robot_state.tcp_position)
#   tmux 세션으로 실행 — 로그: tmux attach -t fk  또는  tail -f /tmp/rc_fk.log
if [ -f "$BASE/backend/serviceAccountKey.json" ] && [ -f "$BASE/backend/fk_worker.py" ]; then
    tmux kill-session -t fk 2>/dev/null
    tmux new-session -d -s fk -c "$BASE/backend" "python3 -u fk_worker.py 2>&1 | tee /tmp/rc_fk.log; echo '--- fk_worker 종료 ---'; read"
    echo "  FK Worker 시작 → /robot_state.tcp_position @ 2Hz"
fi

# ROS2 Order Publisher (서브노드 → 메인노드 토픽 발행)
if [ -f "$BASE/backend/serviceAccountKey.json" ]; then
    nohup bash "$BASE/backend/run_ros2_publisher.sh" > /tmp/rc_ros2_pub.log 2>&1 &
    echo "  ROS2 Publisher 시작 → /robo_chef/order_request (domain=$ROS_DOMAIN_ID)"
fi

# Named Tunnel (고정 도메인)
nohup cloudflared tunnel run robo-chef > /tmp/cf_named.log 2>&1 &

sleep 2

echo ""
echo "✅ 서버 실행 완료"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  로컬 네트워크 (${LOCAL_IP})"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🛒 키오스크        →  http://${LOCAL_IP}:3001"
echo "  📺 조리 현황       →  http://${LOCAL_IP}:3002"
echo "  🖥  관리자 모니터  →  http://${LOCAL_IP}:3003"
echo "  ⚙️  Flask API       →  http://${LOCAL_IP}:5000"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  외부 접속 (고정 도메인 · 전 세계)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🛒 키오스크        →  https://kiosk.thatshoon.com"
echo "  📺 조리 현황       →  https://status.thatshoon.com"
echo "  🖥  관리자 모니터  →  https://admin.thatshoon.com"
echo "  ⚙️  Flask API       →  https://api.thatshoon.com"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "종료: kill \$(lsof -t -i:3001 -i:3002 -i:3003 -i:5000) && pkill -f cloudflared"

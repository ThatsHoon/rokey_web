#!/bin/bash
# ──────────────────────────────────────────────────────────────
#  ROBO CHEF — ROS2 Order Publisher 실행 스크립트
#  Firebase 주문 감시 → /robo_chef/order_request 토픽 발행
# ──────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ROS2 환경 소싱
source /opt/ros/humble/setup.bash

# ROS_DOMAIN_ID 설정 (config.py 와 동일하게)
export ROS_DOMAIN_ID=25

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ROBO CHEF — ROS2 Order Publisher"
echo "  Topic : /robo_chef/order_request"
echo "  Domain: ROS_DOMAIN_ID=${ROS_DOMAIN_ID}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$SCRIPT_DIR"
python3 ros2_order_publisher.py

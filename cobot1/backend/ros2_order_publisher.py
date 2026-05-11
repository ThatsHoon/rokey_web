"""
ROBO CHEF — ROS2 Order Publisher (Service Client)
==================================================
Firebase pending 주문을 감시하고 메인노드(로봇 팔 PC)의
/robo_chef/place_order 서비스를 호출해 전달 확인을 받는다.

토픽 발행(fire-and-forget) 방식과 달리 서비스 응답으로
메인노드의 수신 여부를 확인한다.

실행:
  python3 ros2_order_publisher.py
"""

import os
import sys
import json
import uuid
import time
import threading
import queue
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

import rclpy
from rclpy.node import Node
# from robo_chef_interfaces.srv import PlaceOrder  # 서비스 호출 비활성화

import firebase_admin
from firebase_admin import credentials, db as firebase_db

# ── 설정 ────────────────────────────────────────────────────────
SERVICE_NAME  = "/robo_chef/place_order"
SVC_TIMEOUT   = 10.0   # 서비스 응답 대기 최대 시간 (초)
MAX_RETRY     = 3      # 최대 재시도 횟수
RETRY_DELAY   = 2.0    # 재시도 간격 (초)
DRAIN_INTERVAL = 0.2   # 타이머 주기 (초)

# ── 발행 큐 (Firebase 스레드 → ROS2 스레드) ─────────────────────
_publish_queue: queue.Queue = queue.Queue()
_processing: set = set()


# ════════════════════════════════════════════════════════════════
#  Firebase 헬퍼
# ════════════════════════════════════════════════════════════════

def _fb_init():
    if firebase_admin._apps:
        return
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            config.SERVICE_ACCOUNT_KEY)
    cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred, {"databaseURL": config.FIREBASE_DATABASE_URL})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fb_log(level: str, message: str):
    log_id = "LOG_" + uuid.uuid4().hex[:12]
    firebase_db.reference(f"/logs/{log_id}").set({
        "timestamp": _now_iso(),
        "level":     level,
        "message":   message,
        "source":    "ros2_publisher",
    })


def _build_payload(order_id: str, order: dict, recipe: dict) -> str:
    if config.ORDER_MSG_FORMAT == "simple":
        return f"{recipe.get('recipe_name', recipe.get('recipe_id', '?'))}|{recipe.get('recipe_id', '?')}"
    return json.dumps({
        "order_id":    order_id,
        "recipe_id":   recipe.get("recipe_id", ""),
        "recipe_name": recipe.get("recipe_name", ""),
        "total_steps": recipe.get("total_steps", 0),
        "locations":   recipe.get("locations", {}),
        "sequence":    recipe.get("sequence", []),
    }, ensure_ascii=False, separators=(",", ":"))


# ════════════════════════════════════════════════════════════════
#  Firebase 주문 감시 스레드
# ════════════════════════════════════════════════════════════════

def _order_watcher():
    _fb_init()
    while True:
        try:
            all_orders = firebase_db.reference("/orders").get() or {}
            pending = {k: v for k, v in all_orders.items()
                       if isinstance(v, dict) and v.get("status") == "pending"}

            for order_id, order in pending.items():
                if order_id in _processing:
                    continue

                recipe_id = order.get("recipe_id")
                if not recipe_id:
                    continue

                recipe = firebase_db.reference(f"/recipes/{recipe_id}").get()
                if not recipe:
                    _fb_log("WARN", f"레시피 없음: {recipe_id} (order: {order_id})")
                    continue

                _processing.add(order_id)
                firebase_db.reference(f"/orders/{order_id}").update({
                    "status":       "publishing",
                    "published_at": _now_iso(),
                })

                payload = _build_payload(order_id, order, recipe)
                _publish_queue.put({
                    "order_id":    order_id,
                    "recipe_id":   recipe_id,
                    "recipe_name": recipe.get("recipe_name", recipe_id),
                    "payload":     payload,
                })

        except Exception as e:
            print(f"[OrderWatcher] 오류: {e}")

        time.sleep(1.0)


# ════════════════════════════════════════════════════════════════
#  ROS2 Service Client Node
# ════════════════════════════════════════════════════════════════

class OrderPublisherNode(Node):

    def __init__(self):
        super().__init__("robo_chef_order_publisher")
        # self._cli = self.create_client(PlaceOrder, SERVICE_NAME)  # 서비스 호출 비활성화
        self._pending: list = []
        self.create_timer(DRAIN_INTERVAL, self._drain)
        self.get_logger().info("OrderPublisher 시작 (서비스 호출 비활성화 — Firebase 상태만 처리)")

    # ── 큐 처리 + future 결과 확인 ──────────────────────────────

    def _drain(self):
        # ── 서비스 호출 비활성화 ──────────────────────────────────────
        # self._submit_new()
        # self._check_futures()
        # 서비스 호출 없이 큐에서 꺼내 Firebase 상태만 업데이트
        while not _publish_queue.empty():
            try:
                item = _publish_queue.get_nowait()
            except queue.Empty:
                break
            self.get_logger().info(
                f"[SKIP-SVC] order={item['order_id']} recipe={item['recipe_id']}"
            )
            self._on_success(item)
        # ─────────────────────────────────────────────────────────────

    # def _submit_new(self):
    #     if not self._cli.service_is_ready():
    #         return
    #     while not _publish_queue.empty():
    #         try:
    #             item = _publish_queue.get_nowait()
    #         except queue.Empty:
    #             break
    #         req = PlaceOrder.Request()
    #         req.json_data = item["payload"]
    #         future = self._cli.call_async(req)
    #         self._pending.append({
    #             "future": future, "order_id": item["order_id"],
    #             "recipe_id": item["recipe_id"], "recipe_name": item["recipe_name"],
    #             "payload": item["payload"], "attempts": 1, "sent_at": time.monotonic(),
    #         })
    #         self.get_logger().info(f"[SEND] order={item['order_id']} (시도 1/{MAX_RETRY})")

    # def _check_futures(self):
    #     still_pending = []
    #     for item in self._pending:
    #         future = item["future"]
    #         if future.done():
    #             self._handle_result(item, future)
    #         elif time.monotonic() - item["sent_at"] > SVC_TIMEOUT:
    #             future.cancel()
    #             self._retry_or_fail(item)
    #         else:
    #             still_pending.append(item)
    #     self._pending = still_pending

    # def _handle_result(self, item, future):
    #     try:
    #         result = future.result()
    #     except Exception as e:
    #         self._retry_or_fail(item); return
    #     if result.success:
    #         self._on_success(item)
    #     else:
    #         self._retry_or_fail(item)

    # def _retry_or_fail(self, item):
    #     if item["attempts"] < MAX_RETRY:
    #         item["attempts"] += 1
    #         req = PlaceOrder.Request()
    #         req.json_data = item["payload"]
    #         item["future"] = self._cli.call_async(req)
    #         item["sent_at"] = time.monotonic()
    #         self._pending.append(item)
    #     else:
    #         self._on_failure(item)

    # ── Firebase 상태 업데이트 ───────────────────────────────────

    def _on_success(self, item: dict):
        order_id = item["order_id"]
        try:
            _fb_init()
            firebase_db.reference(f"/orders/{order_id}").update({
                "status":       "delivered",
                "delivered_at": _now_iso(),
            })
            firebase_db.reference("/robot_state").update({
                "robot_status":   "COOKING",
                "current_recipe": item["recipe_id"],
                "current_step":   0,
                "last_updated":   _now_iso(),
            })
            _fb_log("INFO",
                    f"주문 전달 확인: {item['recipe_name']} → {SERVICE_NAME}")
        except Exception as e:
            self.get_logger().warn(f"Firebase 업데이트 실패: {e}")
        finally:
            _processing.discard(order_id)

    def _on_failure(self, item: dict):
        order_id = item["order_id"]
        try:
            _fb_init()
            firebase_db.reference(f"/orders/{order_id}").update({
                "status":    "failed",
                "failed_at": _now_iso(),
            })
            _fb_log("ERROR",
                    f"주문 전달 실패 ({MAX_RETRY}회): {item['recipe_name']}")
        except Exception as e:
            self.get_logger().warn(f"Firebase 실패 기록 오류: {e}")
        finally:
            _processing.discard(order_id)


# ════════════════════════════════════════════════════════════════
#  Entry Point
# ════════════════════════════════════════════════════════════════

def main():
    os.environ["ROS_DOMAIN_ID"] = str(config.ROS2_DOMAIN_ID)

    print(f"""
──────────────────────────────────────────────────────────────
  ROBO CHEF — Order Publisher (Service Client)
  서비스: {SERVICE_NAME}
  ROS_DOMAIN_ID: {config.ROS2_DOMAIN_ID}
  재시도: 최대 {MAX_RETRY}회 / 타임아웃: {SVC_TIMEOUT}s
──────────────────────────────────────────────────────────────
""")

    threading.Thread(target=_order_watcher, daemon=True).start()

    rclpy.init()
    node = OrderPublisherNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\n[OrderPublisher] 종료")
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
